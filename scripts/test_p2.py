"""Unit tests for P2: ref-aware snapshot paging + Camoufox headless-mode resolver.

Pure — no browser. paginate_tree is exercised directly; the Camoufox resolver is
exercised by monkeypatching Config on the browser_engine module.

Run: python scripts/test_p2.py   (exit 0 = all pass)
"""

import sys

from snapshot import paginate_tree
import browser_engine as be

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"FAIL: {name}")


# ===========================================================================
# paginate_tree
# ===========================================================================
REFS = {"@e1": {"role": "a"}, "@e2": {"role": "b"}, "@e3": {"role": "c"}}
# @e1 near the start, @e2 buried in the middle, @e3 at the very end (the nav tail).
TREE = "START @e1 " + ("." * 200) + " @e2 MIDDLE " + ("." * 200) + " @e3 END"
BASE = {"success": True, "tree": TREE, "refs": REFS, "url": "u", "title": "t"}

# --- no-op paths ------------------------------------------------------------
r = paginate_tree(BASE, max_chars=0)
check("noop when max_chars=0 returns same object", r is BASE)
check("noop has no paged key", "paged" not in r)

r = paginate_tree({"success": True, "tree": "short", "refs": {}}, max_chars=1000)
check("noop when tree fits", r.get("paged") is None and r["tree"] == "short")

# --- first window (offset 0) -----------------------------------------------
p = paginate_tree(BASE, offset=0, max_chars=100, tail_chars=30)
check("paged flag set", p.get("paged") is True)
check("page_offset 0", p["page_offset"] == 0)
check("next_offset = max_chars", p["next_offset"] == 100)
check("total_chars = full len", p["total_chars"] == len(TREE))
check("window keeps @e1", "@e1" in p["tree"])
check("hidden middle @e2 absent from text", "@e2" not in p["tree"])
check("nav tail surfaces @e3", "@e3" in p["tree"])
check("note present", "paged snapshot" in p["tree"] and "nav tail" in p["tree"])
# refs filtered to what's visible (window + tail), full map stays server-side
check("refs filtered: @e1 kept", "@e1" in p["refs"])
check("refs filtered: @e3 kept (tail)", "@e3" in p["refs"])
check("refs filtered: @e2 dropped", "@e2" not in p["refs"])
check("preserves other fields", p["url"] == "u" and p["success"] is True)
check("does not mutate input", "paged" not in BASE and BASE["refs"] is REFS)

# --- final window (no more, no tail) ---------------------------------------
last = paginate_tree(BASE, offset=len(TREE) - 40, max_chars=100, tail_chars=30)
check("final next_offset None", last["next_offset"] is None)
check("final window note", "final window" in last["tree"])
check("final window shows @e3", "@e3" in last["tree"])
check("final window: no nav-tail block", "nav tail" not in last["tree"])

# --- offset clamped past end → empty final window, no crash ----------------
over = paginate_tree(BASE, offset=99_999, max_chars=100)
check("over-offset clamps, next_offset None", over["next_offset"] is None)
check("over-offset paged flag", over.get("paged") is True)

# --- middle window has a next_offset and excludes start --------------------
mid = paginate_tree(BASE, offset=100, max_chars=80, tail_chars=20)
check("mid page_offset", mid["page_offset"] == 100)
check("mid has next_offset", mid["next_offset"] == 180)
check("mid window excludes START token", "START @e1" not in mid["tree"])

# --- @eN token exactness: a window with @e10 must NOT pull in @e1 ----------
REFS_COLL = {"@e1": {"r": "a"}, "@e10": {"r": "b"}}
# @e10 in the window, @e1 buried in the hidden middle (and no ref in the tail).
TREE_COLL = "HEAD @e10 " + ("z" * 100) + " @e1 MIDDLE " + ("z" * 100) + " FOOTER"
COLL = {"success": True, "tree": TREE_COLL, "refs": REFS_COLL}
pc = paginate_tree(COLL, offset=0, max_chars=40, tail_chars=20)
check("collision: @e10 visible", "@e10" in pc["refs"])
check("collision: @e1 NOT a false-positive of @e10", "@e1" not in pc["refs"])

# --- param coercion: JSON strings must not crash --------------------------
ps = paginate_tree(BASE, offset="0", max_chars="100", tail_chars="30")
check("coerce str offset/max/tail → paged", ps.get("paged") is True)
check("coerce str next_offset numeric", ps["next_offset"] == 100)
pb = paginate_tree(BASE, max_chars="not-an-int")
check("coerce bad max_chars → no-op", pb.get("paged") is None)
po = paginate_tree(BASE, offset="bad", max_chars=100)
check("coerce bad offset → default 0", po["page_offset"] == 0)


# ===========================================================================
# Camoufox headless-mode resolver
# ===========================================================================
_orig_ch = be.Config.CAMOUFOX_HEADLESS
_orig_hl = be.Config.HEADLESS


def set_cfg(ch, hl=True):
    be.Config.CAMOUFOX_HEADLESS = ch
    be.Config.HEADLESS = hl


set_cfg("", hl=True)
check("empty → HEADLESS True → mode True", be._camoufox_headless_mode() is True)
check("empty → label headless", be._camoufox_headless_label() == "headless")

set_cfg("", hl=False)
check("empty → HEADLESS False → mode False", be._camoufox_headless_mode() is False)
check("empty → label headful", be._camoufox_headless_label() == "headful")

set_cfg("virtual")
check("virtual → mode 'virtual'", be._camoufox_headless_mode() == "virtual")
check("virtual → label virtual", be._camoufox_headless_label() == "virtual")

set_cfg("true")
check("true → mode True", be._camoufox_headless_mode() is True)
set_cfg("1")
check("1 → mode True", be._camoufox_headless_mode() is True)
set_cfg("false")
check("false → mode False", be._camoufox_headless_mode() is False)
set_cfg("0")
check("0 → mode False", be._camoufox_headless_mode() is False)
set_cfg("nonsense", hl=True)
check("unknown → falls back to HEADLESS", be._camoufox_headless_mode() is True)

be.Config.CAMOUFOX_HEADLESS = _orig_ch
be.Config.HEADLESS = _orig_hl

print(f"\n{_PASS} passed, {_FAIL} failed")
sys.exit(1 if _FAIL else 0)
