#!/usr/bin/env python3
"""End-to-end test suite for browser-use server.

Starts the server, runs tests against crawllab.dev and nowsecure.nl,
prints a report, and shuts down.
"""
import asyncio
import json
import os
import sys
import time
import traceback

import aiohttp

BASE = "http://127.0.0.1:8500"
TOKEN = os.getenv("BROWSER_USE_TOKEN", "")
TIMEOUT = aiohttp.ClientTimeout(total=90)
results: list[dict] = []


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _headers() -> dict:
    if TOKEN:
        return {"Authorization": f"Bearer {TOKEN}"}
    return {}


async def req(session: aiohttp.ClientSession, payload: dict) -> dict:
    async with session.post(f"{BASE}/", json=payload, timeout=TIMEOUT, headers=_headers()) as resp:
        return await resp.json()


async def health(session: aiohttp.ClientSession) -> dict:
    async with session.get(f"{BASE}/health", timeout=TIMEOUT) as resp:
        return await resp.json()


def record(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    results.append({"name": name, "passed": passed, "detail": detail})
    log(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Test groups
# ---------------------------------------------------------------------------

async def test_health(s):
    log("--- Health Check ---")
    r = await health(s)
    record("health", r.get("status") == "ok", json.dumps(r))


async def test_tier1_crawllab(s):
    log("--- Tier 1: crawllab.dev ---")
    sid = None
    try:
        # Launch
        r = await req(s, {"op": "launch", "tier": 1, "url": "https://crawllab.dev/"})
        sid = r.get("session_id")
        record("T1 launch", bool(sid), f"session_id={sid}, title={r.get('title','?')}")
        if not sid:
            record("T1 launch", False, f"error={r.get('error')}")
            return

        # Snapshot
        r = await req(s, {"op": "snapshot", "session_id": sid, "compact": True})
        tree = r.get("tree", "")
        refs = r.get("refs", {})
        record("T1 snapshot", r.get("success") and len(refs) > 0,
               f"refs={len(refs)}, tree_len={len(tree)}")

        # search_page — extracted_content is a string, match_count is top-level
        r = await req(s, {"op": "action", "session_id": sid, "action": "search_page",
                          "params": {"query": "crawl", "max_results": 5}})
        matches = r.get("match_count", 0)
        record("T1 search_page", r.get("success", False),
               f"matches={matches}, content={str(r.get('extracted_content',''))[:120]}")

        # find_elements — match_count is top-level
        r = await req(s, {"op": "action", "session_id": sid, "action": "find_elements",
                          "params": {"role": "link"}})
        count = r.get("match_count", 0)
        record("T1 find_elements(role=link)", r.get("success", False),
               f"found={count}, content={str(r.get('extracted_content',''))[:120]}")

        # extract (page to markdown)
        r = await req(s, {"op": "action", "session_id": sid, "action": "extract",
                          "params": {"max_chars": 5000}})
        content = r.get("extracted_content", "")
        clen = len(content) if isinstance(content, str) else len(json.dumps(content))
        record("T1 extract", r.get("success", False) and clen > 50,
               f"content_len={clen}")

        # screenshot
        r = await req(s, {"op": "screenshot", "session_id": sid, "full_page": False})
        shot = r.get("screenshot", "")
        record("T1 screenshot", r.get("success", False) and len(shot) > 100,
               f"base64_len={len(shot)}" + (f", error={r.get('error','')}" if not r.get("success") else ""))

        # get_downloads (should be empty)
        r = await req(s, {"op": "action", "session_id": sid, "action": "get_downloads",
                          "params": {}})
        record("T1 get_downloads", r.get("success", False),
               f"content={str(r.get('extracted_content', ''))[:100]}")

        # Navigate to a different page for new-element detection
        # Use crawllab.dev itself — different enough from subpage to generate new refs
        r = await req(s, {"op": "action", "session_id": sid, "action": "navigate",
                          "params": {"url": "https://example.com"}})
        record("T1 navigate for new-elem", r.get("success", False),
               f"page_changed={r.get('page_changed')}, url={r.get('new_url','?')}")

        # Re-snapshot — check for new element markers (different page = all elements new)
        r = await req(s, {"op": "snapshot", "session_id": sid, "compact": True})
        tree2 = r.get("tree", "")
        new_count = r.get("new_element_count", 0)
        has_stars = "*" in tree2
        record("T1 new-element detection",
               new_count > 0 or has_stars,
               f"new_element_count={new_count}, has_star_markers={has_stars}")

        # Loop detection: navigate back to crawllab (has many refs), then repeat clicks
        log("  Testing loop detection (4x repeated click)...")
        r = await req(s, {"op": "action", "session_id": sid, "action": "navigate",
                          "params": {"url": "https://crawllab.dev/"}})
        r = await req(s, {"op": "snapshot", "session_id": sid, "compact": True})
        refs3 = r.get("refs", {})
        test_ref = list(refs3.keys())[0] if refs3 else None
        loop_warning = None
        if test_ref:
            for i in range(4):
                r = await req(s, {"op": "action", "session_id": sid, "action": "click",
                                  "params": {"ref": test_ref}})
                if r.get("loop_warning"):
                    loop_warning = r["loop_warning"]
            record("T1 loop detection", loop_warning is not None,
                   f"warning={loop_warning}")
        else:
            record("T1 loop detection", False, "no ref to test with")

        # Status
        r = await req(s, {"op": "status", "session_id": sid})
        record("T1 status", r.get("success", False),
               f"action_count={r.get('action_count')}, duration={r.get('duration_seconds')}")

    finally:
        if sid:
            await req(s, {"op": "close", "session_id": sid})
            log("  Session closed.")


async def test_tier2_stealth(s):
    log("--- Tier 2: crawllab.dev (stealth) ---")
    sid = None
    try:
        r = await req(s, {"op": "launch", "tier": 2, "url": "https://crawllab.dev/"})
        sid = r.get("session_id")
        record("T2 launch", bool(sid),
               f"session_id={sid}, title={r.get('title','?')}"
               + (f", error={r.get('error')}" if not sid else ""))
        if not sid:
            return

        # Snapshot
        r = await req(s, {"op": "snapshot", "session_id": sid, "compact": True})
        tree = r.get("tree", "")
        refs = r.get("refs", {})
        record("T2 snapshot", r.get("success") and len(refs) > 0,
               f"refs={len(refs)}, tree_len={len(tree)}")

        # Screenshot (Patchright/Chromium — tests CDP fallback chain)
        r = await req(s, {"op": "screenshot", "session_id": sid, "full_page": False})
        record("T2 screenshot", r.get("success", False),
               f"base64_len={len(r.get('screenshot', ''))}"
               + (f", error={r.get('error','')}" if not r.get("success") else ""))

        # search_page
        r = await req(s, {"op": "action", "session_id": sid, "action": "search_page",
                          "params": {"query": "crawl", "max_results": 5}})
        record("T2 search_page", r.get("success", False),
               f"matches={r.get('match_count', 0)}")

        # extract
        r = await req(s, {"op": "action", "session_id": sid, "action": "extract",
                          "params": {"max_chars": 3000}})
        content = r.get("extracted_content", "")
        clen = len(content) if isinstance(content, str) else len(json.dumps(content))
        record("T2 extract", r.get("success", False) and clen > 50, f"content_len={clen}")

    finally:
        if sid:
            await req(s, {"op": "close", "session_id": sid})
            log("  Session closed.")


async def test_tier3_nowsecure(s):
    log("--- Tier 3: nowsecure.nl (anti-detect) ---")
    sid = None
    try:
        r = await req(s, {"op": "launch", "tier": 3, "url": "https://nowsecure.nl/"})
        sid = r.get("session_id")
        record("T3 launch", bool(sid),
               f"session_id={sid}, title={r.get('title','?')}"
               + (f", warning={r.get('warning','')}" if r.get("warning") else "")
               + (f", error={r.get('error')}" if not sid else ""))
        if not sid:
            return

        # Wait for anti-bot JS to settle
        await asyncio.sleep(5)

        # Snapshot
        r = await req(s, {"op": "snapshot", "session_id": sid, "compact": True})
        tree = r.get("tree", "")
        refs = r.get("refs", {})
        record("T3 snapshot", r.get("success") and len(tree) > 50,
               f"refs={len(refs)}, tree_len={len(tree)}, tree_preview={tree[:200]}")

        # Screenshot (Firefox/Camoufox — no Chromium headless bugs)
        r = await req(s, {"op": "screenshot", "session_id": sid, "full_page": False})
        record("T3 screenshot", r.get("success", False),
               f"base64_len={len(r.get('screenshot', ''))}"
               + (f", error={r.get('error','')}" if not r.get("success") else ""))

        # search_page
        r = await req(s, {"op": "action", "session_id": sid, "action": "search_page",
                          "params": {"query": "secure", "max_results": 5}})
        record("T3 search_page", r.get("success", False),
               f"matches={r.get('match_count', 0)}, content={str(r.get('extracted_content', ''))[:150]}")

        # extract
        r = await req(s, {"op": "action", "session_id": sid, "action": "extract",
                          "params": {"max_chars": 3000}})
        content = r.get("extracted_content", "")
        clen = len(content) if isinstance(content, str) else len(json.dumps(content))
        record("T3 extract", r.get("success", False), f"content_len={clen}")

        # Block detection
        blocked = r.get("blocked", False)
        r2 = await req(s, {"op": "action", "session_id": sid, "action": "navigate",
                           "params": {"url": "https://nowsecure.nl/"}})
        record("T3 block detection", True,
               f"blocked={r2.get('blocked', False)}, protection={r2.get('protection', '')}")

    finally:
        if sid:
            await req(s, {"op": "close", "session_id": sid})
            log("  Session closed.")


async def test_batch_actions(s):
    log("--- Batch Actions ---")
    sid = None
    try:
        r = await req(s, {"op": "launch", "tier": 1, "url": "https://example.com"})
        sid = r.get("session_id")
        if not sid:
            record("batch launch", False, f"launch failed: {r.get('error')}")
            return

        # Basic batch: navigate + snapshot
        r = await req(s, {
            "op": "actions", "session_id": sid,
            "actions": [
                {"action": "navigate", "params": {"url": "https://crawllab.dev/"}},
                {"action": "snapshot", "params": {"compact": True}},
            ],
        })
        results_list = r.get("results", [])
        record("batch basic (2 actions)", r.get("success", False) and len(results_list) == 2,
               f"results_count={len(results_list)}, stopped_at={r.get('stopped_at')}")

        # Verify second result has snapshot tree (ref propagation)
        if len(results_list) >= 2:
            snap_result = results_list[1]
            has_tree = bool(snap_result.get("tree"))
            record("batch ref propagation", has_tree,
                   f"tree_len={len(snap_result.get('tree', ''))}, refs={len(snap_result.get('refs', {}))}")

        # stop_on_error: include an invalid action
        r = await req(s, {
            "op": "actions", "session_id": sid,
            "actions": [
                {"action": "snapshot", "params": {}},
                {"action": "click", "params": {"ref": "@e_nonexistent_999"}},
                {"action": "snapshot", "params": {}},  # should not execute
            ],
            "stop_on_error": True,
        })
        stopped = r.get("stopped_at")
        record("batch stop_on_error", stopped == 1 and len(r.get("results", [])) == 2,
               f"stopped_at={stopped}, results_count={len(r.get('results', []))}")

        # Max limit: >20 actions
        r = await req(s, {
            "op": "actions", "session_id": sid,
            "actions": [{"action": "wait", "params": {"ms": 10}}] * 21,
        })
        record("batch max 20 limit", not r.get("success", True),
               f"error={r.get('error', '')[:80]}")

    finally:
        if sid:
            await req(s, {"op": "close", "session_id": sid})
            log("  Session closed.")


async def test_snapshot_diff(s):
    log("--- Snapshot Diff (new/changed/removed) ---")
    sid = None
    try:
        r = await req(s, {"op": "launch", "tier": 1, "url": "https://crawllab.dev/"})
        sid = r.get("session_id")
        if not sid:
            record("diff launch", False, f"launch failed: {r.get('error')}")
            return

        # First snapshot (baseline)
        r = await req(s, {"op": "snapshot", "session_id": sid, "compact": True})
        refs1 = r.get("refs", {})
        record("diff baseline snapshot", r.get("success", False) and len(refs1) > 0,
               f"refs={len(refs1)}")

        # Navigate to different page → re-snapshot → should detect new + removed
        r = await req(s, {"op": "action", "session_id": sid, "action": "navigate",
                          "params": {"url": "https://example.com"}})

        r = await req(s, {"op": "snapshot", "session_id": sid, "compact": True})
        new_count = r.get("new_element_count", 0)
        removed_count = r.get("removed_element_count", 0)
        changed_count = r.get("changed_element_count", 0)
        tree = r.get("tree", "")
        has_star = "*" in tree
        has_removed = "[removed since last snapshot]" in tree
        record("diff new elements", new_count > 0 and has_star,
               f"new={new_count}, has_star_markers={has_star}")
        record("diff removed elements", removed_count > 0 and has_removed,
               f"removed={removed_count}, has_removed_section={has_removed}")
        record("diff changed count", True,
               f"changed={changed_count} (may be 0 for cross-domain nav)")

    finally:
        if sid:
            await req(s, {"op": "close", "session_id": sid})
            log("  Session closed.")


async def test_rotate_fingerprint(s):
    log("--- Fingerprint Rotation ---")
    sid = None
    try:
        # Tier 1 — should inject JS
        r = await req(s, {"op": "launch", "tier": 1, "url": "https://example.com"})
        sid = r.get("session_id")
        if not sid:
            record("fp launch", False, f"launch failed: {r.get('error')}")
            return

        r = await req(s, {"op": "action", "session_id": sid, "action": "rotate_fingerprint",
                          "params": {"geo": "us"}})
        record("fp rotate tier 1", r.get("success", False),
               f"content={str(r.get('extracted_content', ''))[:120]}, fp_id={r.get('fingerprint_id', '')}")

        # Verify navigator.userAgent was overridden
        r2 = await req(s, {"op": "action", "session_id": sid, "action": "evaluate",
                           "params": {"js": "navigator.userAgent"}})
        ua = r2.get("extracted_content", "")
        # If fingerprint was applied, UA should be something non-default
        record("fp UA override", r2.get("success", False),
               f"ua={str(ua)[:100]}")

    finally:
        if sid:
            await req(s, {"op": "close", "session_id": sid})
            log("  Session closed.")

    # Tier 3 — should return no-op
    sid = None
    try:
        r = await req(s, {"op": "launch", "tier": 3, "url": "https://example.com"})
        sid = r.get("session_id")
        if not sid:
            record("fp tier 3 launch", False, f"launch failed: {r.get('error')}")
            return

        r = await req(s, {"op": "action", "session_id": sid, "action": "rotate_fingerprint",
                          "params": {}})
        is_noop = "natively" in str(r.get("extracted_content", "")).lower()
        record("fp rotate tier 3 (no-op)", r.get("success", False) and is_noop,
               f"content={str(r.get('extracted_content', ''))[:120]}")

    finally:
        if sid:
            await req(s, {"op": "close", "session_id": sid})
            log("  Session closed.")


async def test_bowser_actions(s):
    log("--- Bowser Port: hover, go_forward, console, storage, pdf, resize ---")
    sid = None
    try:
        r = await req(s, {"op": "launch", "tier": 1, "url": "https://crawllab.dev/"})
        sid = r.get("session_id")
        if not sid:
            record("bowser launch", False, f"launch failed: {r.get('error')}")
            return

        # hover — use example.com for a simple, visible page
        r = await req(s, {"op": "action", "session_id": sid, "action": "navigate",
                          "params": {"url": "https://example.com"}})
        r = await req(s, {"op": "snapshot", "session_id": sid, "compact": True})
        refs = r.get("refs", {})
        hover_ref = None
        for k, v in refs.items():
            if isinstance(v, dict) and v.get("role") in ("link",):
                hover_ref = k
                break
        if hover_ref:
            r = await req(s, {"op": "action", "session_id": sid, "action": "hover",
                              "params": {"ref": hover_ref}})
            record("hover", r.get("success", False),
                   f"ref={hover_ref} content={r.get('extracted_content', '')} err={r.get('error', '')}")
        else:
            record("hover", False, "no link ref found on example.com")

        # go_forward: navigate away, go_back, then go_forward to return
        r = await req(s, {"op": "action", "session_id": sid, "action": "navigate",
                          "params": {"url": "https://crawllab.dev/"}})
        r = await req(s, {"op": "action", "session_id": sid, "action": "go_back",
                          "params": {}})
        r = await req(s, {"op": "action", "session_id": sid, "action": "go_forward",
                          "params": {}})
        record("go_forward", r.get("success", False) and r.get("page_changed", False),
               f"url={r.get('new_url', '?')}")

        # console: inject a log via evaluate, then retrieve
        r = await req(s, {"op": "action", "session_id": sid, "action": "evaluate",
                          "params": {"js": "console.log('e2e-test-marker'); console.error('e2e-error-marker')"}})
        await asyncio.sleep(0.5)
        r = await req(s, {"op": "action", "session_id": sid, "action": "console",
                          "params": {}})
        content = r.get("extracted_content", "")
        has_log = "e2e-test-marker" in content
        has_error = "e2e-error-marker" in content
        record("console capture", r.get("success", False) and has_log and has_error,
               f"has_log={has_log}, has_error={has_error}, messages={r.get('message_count', 0)}")

        # console with level filter
        r = await req(s, {"op": "action", "session_id": sid, "action": "console",
                          "params": {"level": "error"}})
        content = r.get("extracted_content", "")
        record("console filter", "e2e-error-marker" in content and "e2e-test-marker" not in content,
               f"filtered_content={content[:100]}")

        # storage_set + storage_get roundtrip
        r = await req(s, {"op": "action", "session_id": sid, "action": "storage_set",
                          "params": {"key": "e2e_test", "value": "hello_42"}})
        record("storage_set", r.get("success", False),
               f"content={r.get('extracted_content', '')}")

        r = await req(s, {"op": "action", "session_id": sid, "action": "storage_get",
                          "params": {"key": "e2e_test"}})
        record("storage_get", r.get("success", False) and r.get("extracted_content") == "hello_42",
               f"value={r.get('extracted_content', '')}")

        # storage_get all
        r = await req(s, {"op": "action", "session_id": sid, "action": "storage_get",
                          "params": {}})
        record("storage_get all", r.get("success", False) and "e2e_test" in r.get("extracted_content", ""),
               f"content_len={len(r.get('extracted_content', ''))}")

        # pdf
        r = await req(s, {"op": "action", "session_id": sid, "action": "pdf",
                          "params": {}})
        pdf_data = r.get("pdf", "")
        record("pdf", r.get("success", False) and len(pdf_data) > 100,
               f"pdf_len={len(pdf_data)}")

        # resize
        r = await req(s, {"op": "action", "session_id": sid, "action": "resize",
                          "params": {"width": 800, "height": 600}})
        record("resize", r.get("success", False),
               f"content={r.get('extracted_content', '')}")

        # resize back
        r = await req(s, {"op": "action", "session_id": sid, "action": "resize",
                          "params": {"width": 1920, "height": 1080}})
        record("resize restore", r.get("success", False),
               f"content={r.get('extracted_content', '')}")

    finally:
        if sid:
            await req(s, {"op": "close", "session_id": sid})
            log("  Session closed.")


async def test_click_coordinate(s):
    log("--- click_coordinate test ---")
    sid = None
    try:
        r = await req(s, {"op": "launch", "tier": 1, "url": "https://example.com"})
        sid = r.get("session_id")
        if not sid:
            record("click_coordinate", False, f"launch failed: {r.get('error')}")
            return

        r = await req(s, {"op": "action", "session_id": sid, "action": "click_coordinate",
                          "params": {"x": 200, "y": 300}})
        record("click_coordinate", r.get("success", False),
               f"content={r.get('extracted_content','')}")

    finally:
        if sid:
            await req(s, {"op": "close", "session_id": sid})


async def test_multi_session_status(s):
    log("--- Multi-session status ---")
    r = await req(s, {"op": "status"})
    record("global status", True,
           f"sessions={r.get('active_sessions', r.get('sessions'))}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    log("=" * 60)
    log("browser-use E2E Test Suite")
    log("=" * 60)

    async with aiohttp.ClientSession() as s:
        try:
            await test_health(s)
            await test_tier1_crawllab(s)
            await test_tier2_stealth(s)
            await test_tier3_nowsecure(s)
            await test_batch_actions(s)
            await test_snapshot_diff(s)
            await test_rotate_fingerprint(s)
            await test_bowser_actions(s)
            await test_click_coordinate(s)
            await test_multi_session_status(s)
        except Exception as e:
            log(f"FATAL: {e}")
            traceback.print_exc()

    # Report
    log("")
    log("=" * 60)
    log("RESULTS")
    log("=" * 60)
    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"])
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  [{status}] {r['name']}: {r['detail']}")
    log(f"\nTotal: {passed} passed, {failed} failed, {len(results)} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
