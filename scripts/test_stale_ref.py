"""Unit tests for the safe stale-ref handling (click / fill / type).

Pure — no browser. actions._resolve_ref / take_snapshot / _refresh_ref_map are
monkeypatched so the gating logic is exercised without a live page. The fake
resolver returns a sentinel locator iff the ref key is in the map it is handed.

Key safety property under test (codex HIGH-2): @eN is an ordinal into a
per-snapshot map, so we auto-refresh+act ONLY when the current map is empty;
a non-empty map missing the ref returns snapshot_required instead of guessing.

Run: python scripts/test_stale_ref.py   (exit 0 = all pass)
"""

import asyncio
import sys

import actions

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"FAIL: {name}")


SENTINEL = object()

_orig_resolve = actions._resolve_ref
_orig_snap = actions.take_snapshot
_orig_refresh = actions._refresh_ref_map


async def fake_resolve(page, ref_str, ref_map):
    """Resolve iff the ref key is present in the supplied map."""
    return SENTINEL if ref_str in (ref_map or {}) else None


actions._resolve_ref = fake_resolve


def run(session, ref="@e1"):
    return asyncio.run(actions._resolve_ref_with_refresh(None, ref, session))


# --- present in map → resolve, no refresh, no snapshot --------------------
_snap_calls = {"n": 0}


async def snap_counting(page, **kw):
    _snap_calls["n"] += 1
    return {"success": True, "refs": {}}


actions.take_snapshot = snap_counting
sess = {"ref_map": {"@e1": {}}, "session_id": "s"}
loc, refreshed, err = run(sess)
check("present resolves", loc is SENTINEL)
check("present not refreshed", refreshed is False)
check("present no error", err is None)
check("present skips snapshot", _snap_calls["n"] == 0)


# --- absent + NON-EMPTY map → snapshot_required, NO snapshot (HIGH-2) ------
_snap_calls["n"] = 0
sess = {"ref_map": {"@e2": {}, "@e3": {}}, "session_id": "s"}
loc, refreshed, err = run(sess)
check("nonempty-miss no locator", loc is None)
check("nonempty-miss not refreshed", refreshed is False)
check("nonempty-miss snapshot_required", err and err.get("snapshot_required") is True)
check("nonempty-miss success false", err and err.get("success") is False)
check("nonempty-miss takes NO snapshot", _snap_calls["n"] == 0)
check("nonempty-miss leaves map untouched", sess.get("_ref_map_dirty") is None)


# --- absent + EMPTY map → safe refresh → resolves -------------------------
async def snap_ok(page, **kw):
    return {"success": True, "refs": {"@e1": {"role": "button"}}}


actions.take_snapshot = snap_ok
sess = {"ref_map": {}, "session_id": "s"}
loc, refreshed, err = run(sess)
check("empty-recover resolves", loc is SENTINEL)
check("empty-recover refreshed true", refreshed is True)
check("empty-recover no error", err is None)
check("empty-recover updates map", sess["ref_map"] == {"@e1": {"role": "button"}})
check("empty-recover marks dirty", sess.get("_ref_map_dirty") is True)


# --- absent + EMPTY map → snapshot fails → ref_refresh_attempted -----------
async def snap_fail(page, **kw):
    return {"success": False, "refs": {}}


actions.take_snapshot = snap_fail
sess = {"ref_map": {}, "session_id": "s"}
loc, refreshed, err = run(sess)
check("snapfail no locator", loc is None)
check("snapfail attempted flag", err and err.get("ref_refresh_attempted") is True)
check("snapfail not dirty", sess.get("_ref_map_dirty") is None)


# --- absent + EMPTY map → snapshot raises → swallowed ----------------------
async def snap_raise(page, **kw):
    raise RuntimeError("aria snapshot blew up")


actions.take_snapshot = snap_raise
sess = {"ref_map": {}, "session_id": "s"}
loc, refreshed, err = run(sess)
check("snapraise no locator", loc is None)
check("snapraise attempted flag", err and err.get("ref_refresh_attempted") is True)


# --- absent + EMPTY map → rebuilt map still lacks ref ----------------------
async def snap_other(page, **kw):
    return {"success": True, "refs": {"@e9": {}}}


actions.take_snapshot = snap_other
sess = {"ref_map": {}, "session_id": "s"}
loc, refreshed, err = run(sess)
check("rebuilt-miss no locator", loc is None)
check("rebuilt-miss refreshed true", refreshed is True)
check("rebuilt-miss attempted flag", err and err.get("ref_refresh_attempted") is True)
check("rebuilt-miss dirty set", sess.get("_ref_map_dirty") is True)
check("rebuilt-miss map replaced", sess["ref_map"] == {"@e9": {}})


# --- bounded: empty-map recovery snapshots exactly once -------------------
_snap2 = {"n": 0}


async def snap_count2(page, **kw):
    _snap2["n"] += 1
    return {"success": True, "refs": {}}  # never resolves @e1


actions.take_snapshot = snap_count2
sess = {"ref_map": {}, "session_id": "s"}
run(sess)
check("empty-recover single snapshot", _snap2["n"] == 1)


# --- _refresh_ref_map directly -------------------------------------------
async def rsnap_ok(page, **kw):
    return {"success": True, "refs": {"@e5": {}}}


actions.take_snapshot = rsnap_ok
sess = {"ref_map": {}, "session_id": "s"}
m = asyncio.run(actions._refresh_ref_map(None, sess))
check("refresh returns new map", m == {"@e5": {}})
check("refresh sets session map", sess["ref_map"] == {"@e5": {}})
check("refresh sets dirty", sess.get("_ref_map_dirty") is True)

actions.take_snapshot = snap_fail
sess = {"ref_map": {}, "session_id": "s"}
m = asyncio.run(actions._refresh_ref_map(None, sess))
check("refresh none on snapshot fail", m is None)
check("refresh not dirty on fail", sess.get("_ref_map_dirty") is None)

actions.take_snapshot = snap_raise
sess = {"ref_map": {}, "session_id": "s"}
m = asyncio.run(actions._refresh_ref_map(None, sess))
check("refresh none on snapshot raise", m is None)


# --- _looks_stale_action classification ----------------------------------
check("stale: timeout", actions._looks_stale_action(Exception("Timeout 30000ms exceeded")))
check("stale: detached", actions._looks_stale_action(Exception("Element is not attached to the DOM")))
check("stale: waiting for", actions._looks_stale_action(Exception("waiting for locator")))
check("stale: plain logic error not stale", actions._looks_stale_action(Exception("invalid selector")) is False)


# --- _stale_action_error: refresh only on stale-looking failures ----------
_refresh_calls = {"n": 0}


async def fake_refresh(page, session):
    _refresh_calls["n"] += 1
    session["_ref_map_dirty"] = True
    return {"@e1": {}}


actions._refresh_ref_map = fake_refresh

sess = {"ref_map": {"@e1": {}}, "session_id": "s"}
err = asyncio.run(actions._stale_action_error(None, "@e1", sess, Exception("Timeout 30000ms exceeded")))
check("staleerr success false", err.get("success") is False)
check("staleerr snapshot_required", err.get("snapshot_required") is True)
check("staleerr refreshed map", _refresh_calls["n"] == 1)

_refresh_calls["n"] = 0
sess = {"ref_map": {"@e1": {}}, "session_id": "s"}
err = asyncio.run(actions._stale_action_error(None, "@e1", sess, Exception("invalid selector")))
check("non-stale success false", err.get("success") is False)
check("non-stale no snapshot_required", "snapshot_required" not in err)
check("non-stale no refresh", _refresh_calls["n"] == 0)


actions._resolve_ref = _orig_resolve
actions.take_snapshot = _orig_snap
actions._refresh_ref_map = _orig_refresh

print(f"\n{_PASS} passed, {_FAIL} failed")
sys.exit(1 if _FAIL else 0)
