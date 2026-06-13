"""Unit tests for the browser process-tree memory monitor + orphan reaper.

Pure — no live psutil walk or browser. psutil / _sessions / _last_launch_at /
_launches_in_flight are monkeypatched on the browser_engine module so the
REAP-ONLY logic is exercised deterministically. No threads (the reaper uses a
non-blocking terminate→grace→kill), so this runs in restricted sandboxes too.

Run: python scripts/test_resource_reaper.py   (exit 0 = all pass)
"""

import asyncio
import sys
import time

import browser_engine as be
from config import Config

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"FAIL: {name}")


MB = 1024 * 1024


class FakeMem:
    def __init__(self, rss):
        self.rss = rss


class FakeProc:
    def __init__(self, name, rss, survives_terminate=False):
        self._name = name
        self._rss = rss
        self._survives = survives_terminate
        self.running = True
        self.terminated = False
        self.killed = False

    def name(self):
        return self._name

    def memory_info(self):
        return FakeMem(self._rss)

    def is_running(self):
        return self.running

    def terminate(self):
        self.terminated = True
        if not self._survives:
            self.running = False

    def kill(self):
        self.killed = True
        self.running = False


class FakeRoot:
    def __init__(self, kids):
        self._kids = kids

    def children(self, recursive=False):
        return list(self._kids)


class FakePsutil:
    def __init__(self, kids):
        self._kids = kids

    def Process(self, pid=None):
        return FakeRoot(self._kids)


# Save originals so the module is left clean for any later import.
_orig_psutil = be.psutil
_orig_sessions = be._sessions
_orig_last = be._last_launch_at
_orig_inflight = be._launches_in_flight


# --- _iter_browser_descendants: classification by name ----------------------
be.psutil = FakePsutil([FakeProc("chrome", 100 * MB),
                        FakeProc("node", 50 * MB),
                        FakeProc("camoufox", 200 * MB)])
descs = be._iter_browser_descendants()
check("iter picks browser procs", {p.name() for p in descs} == {"chrome", "camoufox"})
check("iter excludes node driver", all(p.name() != "node" for p in descs))

# --- collect_resource_snapshot: sums only browser RSS -----------------------
be._sessions = {"s1": {}}
snap = be.collect_resource_snapshot()
check("snap sums browser rss only", snap["browser_rss_mb"] == 300)
check("snap proc count excludes node", snap["browser_proc_count"] == 2)
check("snap reports active sessions", snap["active_sessions"] == 1)
check("snap psutil flag true", snap["psutil"] is True)

# psutil unavailable → graceful None
be.psutil = None
snap_n = be.collect_resource_snapshot()
check("snap none rss when no psutil", snap_n["browser_rss_mb"] is None)
check("snap none count when no psutil", snap_n["browser_proc_count"] is None)
check("snap psutil flag false", snap_n["psutil"] is False)

# --- reap_orphan_browsers: gating -------------------------------------------
# psutil unavailable
be.psutil = None
r = asyncio.run(be.reap_orphan_browsers())
check("reap noop without psutil", r == {"reaped": 0, "psutil": False})

# active sessions → never reap (would kill a live session's browser)
be.psutil = FakePsutil([FakeProc("chrome", 10 * MB)])
be._sessions = {"s1": {}}
be._last_launch_at = 0.0
be._launches_in_flight = 0
r = asyncio.run(be.reap_orphan_browsers())
check("reap skips with active session", r["reaped"] == 0 and r.get("skipped") == "active_or_recent_launch")

# launch in flight → never reap (browser spawned, session not yet registered)
be._sessions = {}
be._last_launch_at = 0.0
be._launches_in_flight = 1
r = asyncio.run(be.reap_orphan_browsers())
check("reap skips during in-flight launch", r.get("skipped") == "active_or_recent_launch")

# recent launch (within grace) → stay hand even with no sessions / counter clear
be._sessions = {}
be._launches_in_flight = 0
be._last_launch_at = time.monotonic()
r = asyncio.run(be.reap_orphan_browsers())
check("reap skips during launch grace", r.get("skipped") == "active_or_recent_launch")

# no sessions, old launch, but nothing to reap
be.psutil = FakePsutil([])
be._sessions = {}
be._last_launch_at = 0.0
be._launches_in_flight = 0
r = asyncio.run(be.reap_orphan_browsers())
check("reap zero when no orphans", r == {"reaped": 0})

# --- reap: orphans present → terminate; clean exit needs no kill ------------
clean = [FakeProc("chrome", 10 * MB), FakeProc("camoufox", 20 * MB)]
be.psutil = FakePsutil(clean)
be._sessions = {}
be._last_launch_at = 0.0
be._launches_in_flight = 0
r = asyncio.run(be.reap_orphan_browsers())
check("reap counts orphans", r["reaped"] == 2)
check("reap terminates all orphans", all(p.terminated for p in clean))
check("reap skips kill when terminate worked", all(not p.killed for p in clean))

# survivor of terminate → force-killed
stubborn = [FakeProc("chrome", 10 * MB, survives_terminate=True)]
be.psutil = FakePsutil(stubborn)
be._sessions = {}
be._last_launch_at = 0.0
be._launches_in_flight = 0
r = asyncio.run(be.reap_orphan_browsers())
check("reap force-kills survivors", stubborn[0].killed is True)

# Config knobs wired
check("grace constant present", isinstance(Config.LAUNCH_REAP_GRACE_SEC, int))
check("warn threshold present", isinstance(Config.BROWSER_RSS_WARN_THRESHOLD_MB, int))

# Restore module globals
be.psutil = _orig_psutil
be._sessions = _orig_sessions
be._last_launch_at = _orig_last
be._launches_in_flight = _orig_inflight

print(f"\n{_PASS} passed, {_FAIL} failed")
sys.exit(1 if _FAIL else 0)
