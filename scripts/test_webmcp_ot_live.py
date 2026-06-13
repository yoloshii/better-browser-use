#!/usr/bin/env python3
"""Real Origin-Trial E2E for the WebMCP adapter.

Drives the SHIPPED action_webmcp_discover / action_webmcp_call against a REAL
Chrome Beta (>= 149) running the WebMCP Origin Trial — not a stub. Registers a
WebMCP tool via the real `document.modelContext` API on a secure page, then
verifies discovery (document path, untrustedContentHint + origin capture) and
execution through the actual server action handlers.

Requires: google-chrome-beta >= 149 + `--enable-features=WebMCPTesting`
(the exact flag browser_engine.py passes when a Chrome channel is set).

Run (conda base): python scripts/test_webmcp_ot_live.py
Skips cleanly (exit 0) if Chrome Beta < 149 or the OT API isn't exposed.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import actions  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

_PASS = _FAIL = 0


def chk(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


# Register a read-only WebMCP tool (read-only → executeTool runs without a user prompt).
_REGISTER_JS = """
() => {
    document.modelContext.registerTool({
        name: 'lookup_order',
        description: 'Look up an order status by id',
        inputSchema: { type: 'object', properties: { id: { type: 'string' } }, required: ['id'] },
        annotations: { readOnlyHint: true, untrustedContentHint: true },
        execute: async ({ id }) => 'Order ' + id + ': shipped',
    });
}
"""


async def main():
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                channel="chrome-beta", headless=True,
                args=["--enable-features=WebMCPTesting"],
            )
        except Exception as e:
            print(f"  SKIP — chrome-beta not launchable: {str(e)[:120]}")
            return 0

        page = await (await browser.new_context()).new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded", timeout=20000)

        api = await page.evaluate("() => typeof (document.modelContext && document.modelContext.getTools)")
        if api != "function":
            print(f"  SKIP — document.modelContext.getTools not present (api={api}); Chrome < 149 or OT off")
            await browser.close()
            return 0

        ver = await page.evaluate("() => navigator.userAgent")
        print(f"  Chrome OT live. UA: {ver}")
        await page.evaluate(_REGISTER_JS)

        # --- discover through the shipped handler ---
        session = {"session_id": "ot-live", "webmcp_tools": {}}
        disc = await actions.action_webmcp_discover(page, {}, session)
        chk("discover success", disc.get("success") is True, json.dumps(disc)[:160])
        chk("webmcp_available", disc.get("webmcp_available") is True)
        content = json.loads(disc.get("extracted_content", "{}"))
        chk("source == document (real OT path, not fallback)", content.get("source") == "document",
            f"source={content.get('source')}")
        tools = {t["name"]: t for t in content.get("tools", [])}
        chk("lookup_order discovered", "lookup_order" in tools, str(list(tools)))
        t = tools.get("lookup_order", {})
        chk("untrustedContentHint captured from real OT", t.get("untrustedContentHint") is True, str(t))
        chk("readOnlyHint captured from real OT", t.get("readOnlyHint") is True, str(t))
        chk("origin captured from real OT", t.get("origin") == "https://example.com", str(t.get("origin")))
        chk("inputSchema parsed to object", isinstance(t.get("inputSchema"), dict)
            and "properties" in t.get("inputSchema", {}), str(t.get("inputSchema"))[:120])
        # session state populated for the call handler
        chk("session.webmcp_tools populated", "lookup_order" in session.get("webmcp_tools", {}))

        # --- call through the shipped handler (real document.modelContext.executeTool) ---
        call = await actions.action_webmcp_call(page, {"tool": "lookup_order", "args": {"id": "A1"}}, session)
        chk("call success on real OT", call.get("success") is True, json.dumps(call)[:200])
        chk("real executeTool returned expected result",
            "Order A1: shipped" in json.dumps(call), json.dumps(call)[:200])

        # --- origin fail-closed against the real tool set ---
        session2 = {"session_id": "ot-live",
                    "webmcp_tools": {"lookup_order": {"name": "lookup_order", "origin": "https://evil.example"}}}
        mism = await actions.action_webmcp_call(page, {"tool": "lookup_order", "args": {"id": "A1"}}, session2)
        chk("origin mismatch fails closed on real OT", mism.get("success") is False
            and "re-run webmcp_discover" in (mism.get("error") or ""), json.dumps(mism)[:200])

        await browser.close()

    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
