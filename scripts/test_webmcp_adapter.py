#!/usr/bin/env python3
"""Stub-level tests for the WebMCP dual-path adapter JS.

Exercises the EXACT shipped strings (actions._WEBMCP_DISCOVER_JS / _WEBMCP_CALL_JS) and
the interceptor (browser_engine.WEBMCP_INIT_SCRIPT) against a FAKE document.modelContext /
navigator.modelContextTesting / window.__webmcp injected into a real (bundled) Chromium
page. This validates the Origin-Trial (Chrome 149+) branch, in-page tool-object resolution,
untrustedContentHint capture, the navigator fallback (146-148), and the requestUserInteraction
gating — WITHOUT a real Chrome 149+ build (which isn't available locally).

NOTE: this is stub coverage of OUR adapter logic, NOT real-OT conformance. Real-browser
conformance (headless vs headful, cross-origin, OT tokens, flag name) is deferred until a
Chrome 149+ channel is available.

Run (conda base python): python scripts/test_webmcp_adapter.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from actions import _WEBMCP_DISCOVER_JS, _WEBMCP_CALL_JS   # noqa: E402
from browser_engine import WEBMCP_INIT_SCRIPT              # noqa: E402
from playwright.async_api import async_playwright          # noqa: E402

_PASS = _FAIL = 0


def chk(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


# Fake OT API: document.modelContext.getTools()/executeTool(toolObject, jsonString)
_INJECT_DOCUMENT = """
() => {
    window.__calls = { executeToolArg: null };
    window.document.modelContext = {
        getTools: async () => ([{
            name: 'addTodo',
            description: 'Add a new item to the to-do list',
            inputSchema: '{"type":"object","properties":{"text":{"type":"string"}}}',
            annotations: { readOnlyHint: false, untrustedContentHint: true },
            origin: 'https://example.com',
        }]),
        executeTool: async (tool, argsJson) => {
            // Record what we received so the test can prove it was the OBJECT, not the name.
            window.__calls.executeToolArg = {
                isObject: (typeof tool === 'object' && tool !== null),
                name: tool && tool.name,
                argsJson: argsJson,
            };
            return 'Added to-do: ' + JSON.parse(argsJson).text;
        },
    };
}
"""

# Fake pre-OT testing API: navigator.modelContextTesting.listTools()/executeTool(name, json)
_INJECT_NAVIGATOR = """
() => {
    navigator.modelContextTesting = {
        listTools: () => ([{
            name: 'search',
            description: 'Search the catalog',
            inputSchema: '{"type":"object","properties":{"q":{"type":"string"}}}',
            annotations: { readOnlyHint: true },
        }]),
        executeTool: async (name, argsJson) => 'searched:' + name + ':' + JSON.parse(argsJson).q,
    };
}
"""

# Minimal publisher namespace so the interceptor (WEBMCP_INIT_SCRIPT) engages.
_INJECT_PUBLISHER_ONLY = """
() => { window.document.modelContext = { registerTool: function(){} }; }
"""

# Register a mutating tool + a read-only tool whose execute() both gate on requestUserInteraction.
_REGISTER_GATED_TOOLS = """
() => {
    document.modelContext.registerTool({
        name: 'buyNow',
        description: 'Complete a purchase',
        annotations: { readOnlyHint: false },
        execute: async (args, client) => {
            await client.requestUserInteraction(async () => {});
            return 'purchased';
        },
    });
    document.modelContext.registerTool({
        name: 'getStatus',
        description: 'Read order status',
        annotations: { readOnlyHint: true },
        execute: async (args, client) => {
            await client.requestUserInteraction(async () => {});
            return 'status-ok';
        },
    });
}
"""


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()

        # --- Scenario A: Origin Trial document.modelContext path -----------------
        print("--- Scenario A: document.modelContext (Chrome 149+ OT) ---")
        page = await ctx.new_page()
        await page.goto("about:blank")
        await page.evaluate(_INJECT_DOCUMENT)

        disc = await page.evaluate(_WEBMCP_DISCOVER_JS)
        chk("discover source == document", disc.get("source") == "document", str(disc)[:160])
        tools = disc.get("tools", [])
        chk("one tool discovered", len(tools) == 1, str(tools)[:120])
        t0 = tools[0] if tools else {}
        chk("untrustedContentHint captured (true)", t0.get("untrustedContentHint") is True, str(t0))
        chk("readOnlyHint captured (false)", t0.get("readOnlyHint") is False, str(t0))
        chk("origin captured", t0.get("origin") == "https://example.com", str(t0))
        chk("inputSchema parsed to object", isinstance(t0.get("inputSchema"), dict)
             and "properties" in t0.get("inputSchema", {}), str(t0.get("inputSchema"))[:120])

        call = await page.evaluate(_WEBMCP_CALL_JS, ["addTodo", json.dumps({"text": "milk"}), None, False])
        chk("call result parsed", isinstance(call, dict) and call.get("text") == "Added to-do: milk", str(call)[:120])
        rec = await page.evaluate("() => window.__calls.executeToolArg")
        chk("executeTool received the OBJECT (not the name)", rec and rec.get("isObject") is True, str(rec))
        chk("resolved object is the right tool", rec and rec.get("name") == "addTodo", str(rec))

        # Fix 1: origin mismatch must FAIL CLOSED (no same-name-other-origin execution)
        mism = await page.evaluate(_WEBMCP_CALL_JS, ["addTodo", json.dumps({"text": "x"}), "https://evil.example", False])
        chk("origin mismatch fails closed (error)", isinstance(mism, dict) and "error" in mism, str(mism)[:140])
        okc = await page.evaluate(_WEBMCP_CALL_JS, ["addTodo", json.dumps({"text": "eggs"}), "https://example.com", False])
        chk("matching origin still executes", isinstance(okc, dict) and okc.get("text") == "Added to-do: eggs", str(okc)[:140])
        await page.close()

        # --- Scenario B: pre-OT navigator.modelContextTesting fallback -----------
        print("--- Scenario B: navigator.modelContextTesting (Chrome 146-148) ---")
        page = await ctx.new_page()
        await page.goto("about:blank")
        await page.evaluate(_INJECT_NAVIGATOR)

        disc = await page.evaluate(_WEBMCP_DISCOVER_JS)
        chk("discover source == native", disc.get("source") == "native", str(disc)[:160])
        nt = disc.get("tools", [{}])[0]
        chk("native readOnlyHint captured (true)", nt.get("readOnlyHint") is True, str(nt))
        call = await page.evaluate(_WEBMCP_CALL_JS, ["search", json.dumps({"q": "boots"}), None, False])
        chk("native call executes by name", isinstance(call, dict)
             and call.get("text") == "searched:search:boots", str(call)[:120])
        await page.close()

        # --- Scenario C: interceptor fallback + requestUserInteraction gating -----
        print("--- Scenario C: interceptor gating (mutating vs read-only) ---")
        page = await ctx.new_page()
        await page.goto("about:blank")
        await page.evaluate(_INJECT_PUBLISHER_ONLY)   # document.modelContext.registerTool only
        await page.evaluate(WEBMCP_INIT_SCRIPT)        # patches registerTool, builds window.__webmcp
        await page.evaluate(_REGISTER_GATED_TOOLS)

        # mutating tool, allow_sensitive=False -> must be gated, NOT silently executed
        gated = await page.evaluate(_WEBMCP_CALL_JS, ["buyNow", "{}", None, False])
        chk("mutating tool gated (requires_user_interaction)",
            isinstance(gated, dict) and gated.get("_requires_user_interaction") is True, str(gated)[:160])

        # mutating tool, allow_sensitive=True -> proceeds
        ok = await page.evaluate(_WEBMCP_CALL_JS, ["buyNow", "{}", None, True])
        chk("mutating tool proceeds with allow_sensitive", ok == "purchased", str(ok)[:120])

        # read-only tool auto-proceeds even without allow_sensitive
        ro = await page.evaluate(_WEBMCP_CALL_JS, ["getStatus", "{}", None, False])
        chk("read-only tool auto-proceeds (no gate)", ro == "status-ok", str(ro)[:120])
        await page.close()

        # --- Scenario D: action_webmcp_call param handling (FakePage, no browser) ---
        print("--- Scenario D: allow_sensitive coercion + origin threading ---")
        from actions import action_webmcp_call

        class RecordingPage:
            def __init__(self):
                self.url = "https://shop.example/"
                self.evaluated = None
            async def evaluate(self, js, arg=None):
                self.evaluated = arg   # [name, argsJson, known_origin, allow_sensitive]
                return {"ok": True}
            async def title(self):
                return "t"

        sess = {"webmcp_tools": {"buyNow": {"name": "buyNow", "origin": "https://shop.example/"}}}
        p1 = RecordingPage()
        await action_webmcp_call(p1, {"tool": "buyNow", "args": {}, "allow_sensitive": "false"}, sess)
        chk("string 'false' coerced to allow_sensitive=False (gate not bypassed)",
            p1.evaluated and p1.evaluated[3] is False, str(p1.evaluated))
        chk("known_origin threaded from session", p1.evaluated and p1.evaluated[2] == "https://shop.example/", str(p1.evaluated))
        p2 = RecordingPage()
        await action_webmcp_call(p2, {"tool": "buyNow", "args": {}, "allow_sensitive": True}, sess)
        chk("bool True kept as allow_sensitive=True", p2.evaluated and p2.evaluated[3] is True, str(p2.evaluated))

        await browser.close()

    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
