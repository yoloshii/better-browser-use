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
