"""
Action implementations for browser-use.

Core (26 actions) + humanization layer via behavior module.
When session["humanize"] is True, click/type/scroll use
Bezier curves, Gaussian typing delays, and eased scrolling.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import sys
import traceback
from typing import Any, Callable, Coroutine

from behavior import HumanBehavior
from errors import to_ai_friendly_error
from snapshot import take_snapshot

# Type for action handlers
ActionHandler = Callable[..., Coroutine[Any, Any, dict]]


# ---------------------------------------------------------------------------
# Ref resolution
# ---------------------------------------------------------------------------

def parse_ref(ref_str: str) -> str | None:
    """Parse ref argument into canonical form (e.g. 'e1').

    Accepts: @e1, ref=e1, e1
    """
    ref_str = ref_str.strip()
    if ref_str.startswith("@"):
        return ref_str[1:]
    if ref_str.startswith("ref="):
        return ref_str[4:]
    if ref_str.startswith("e") and ref_str[1:].isdigit():
        return ref_str
    return None


async def _resolve_ref(page, ref_str: str, ref_map: dict) -> Any:
    """Resolve a ref to a Playwright Locator.

    Args:
        page: Playwright Page
        ref_str: Ref like '@e1'
        ref_map: Dict from snapshot, mapping @eN -> {role, name, selector, nth}

    Returns:
        Playwright Locator or None
    """
    parsed = parse_ref(ref_str)
    if parsed is None:
        return None
    canonical = f"@{parsed}"
    ref_data = ref_map.get(canonical)
    if ref_data is None:
        return None

    role = ref_data.get("role", "")
    name = ref_data.get("name")
    nth = ref_data.get("nth")
    css_selector = ref_data.get("selector", "")

    # Cursor-interactive elements use CSS selector directly
    if role in ("clickable", "focusable"):
        return page.locator(css_selector)

    # ARIA-based elements use getByRole
    role_opts = {}
    if name:
        role_opts["name"] = name
        role_opts["exact"] = True

    locator = page.get_by_role(role, **role_opts)

    if nth is not None:
        locator = locator.nth(nth)

    return locator


# ---------------------------------------------------------------------------
# Core actions (Phase 1)
# ---------------------------------------------------------------------------

async def action_navigate(page, params: dict, session: dict) -> dict:
    """Navigate to a URL."""
    url = params.get("url")
    if not url:
        return {"success": False, "error": "Missing required param: url"}

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        return {
            "success": True,
            "extracted_content": f"Navigated to {page.url}",
            "page_changed": True,
            "new_url": page.url,
            "new_title": await page.title(),
        }
    except Exception as e:
        return {
            "success": False,
            "error": to_ai_friendly_error(e),
            "new_url": page.url,
        }


async def action_click(page, params: dict, session: dict) -> dict:
    """Click an element by ref.

    When session["humanize"] is True, moves mouse along a Bezier curve
    to the element before clicking, with random delays.
    """
    ref = params.get("ref")
    if not ref:
        return {"success": False, "error": "Missing required param: ref"}

    ref_map = session.get("ref_map", {})
    locator = await _resolve_ref(page, ref, ref_map)
    if locator is None:
        return {"success": False, "error": f"Ref {ref} not found. Take a new snapshot."}

    old_url = page.url
    old_tab_count = len(page.context.pages)
    try:
        if session.get("humanize"):
            hb = HumanBehavior(intensity=session.get("humanize_intensity", 1.0))
            try:
                await asyncio.wait_for(
                    hb.move_to_element(page, locator, click=True),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                # Humanize timed out — fall back to plain click
                await locator.click(timeout=10_000)
        else:
            await locator.click(timeout=10_000)
    except Exception as e:
        new_url = page.url
        if new_url != old_url:
            return {
                "success": True,
                "extracted_content": f"Clicked {ref} — page navigated",
                "page_changed": True,
                "new_url": new_url,
                "new_title": await page.title(),
            }
        return {"success": False, "error": to_ai_friendly_error(e)}

    settle = random.uniform(0.2, 0.5) if session.get("humanize") else 0.3
    await asyncio.sleep(settle)
    new_url = page.url
    new_tab_count = len(page.context.pages)

    result: dict[str, Any] = {
        "success": True,
        "extracted_content": f"Clicked {ref}",
        "page_changed": new_url != old_url,
    }

    if new_url != old_url:
        result["new_url"] = new_url
        result["new_title"] = await page.title()

    if new_tab_count > old_tab_count:
        new_page = page.context.pages[-1]
        result["new_tab_opened"] = True
        result["new_tab_url"] = new_page.url
        result["extracted_content"] = f"Clicked {ref} — opened new tab: {new_page.url}"

    return result


async def action_fill(page, params: dict, session: dict) -> dict:
    """Atomic fill — clears field then sets value. For forms."""
    ref = params.get("ref")
    value = params.get("value", "")
    if not ref:
        return {"success": False, "error": "Missing required param: ref"}

    ref_map = session.get("ref_map", {})
    locator = await _resolve_ref(page, ref, ref_map)
    if locator is None:
        return {"success": False, "error": f"Ref {ref} not found. Take a new snapshot."}

    try:
        await locator.fill(value, timeout=10_000)
        return {
            "success": True,
            "extracted_content": f"Filled {ref} with value",
        }
    except Exception as e:
        return {"success": False, "error": to_ai_friendly_error(e)}


async def action_type(page, params: dict, session: dict) -> dict:
    """Character-by-character typing. For search boxes, compose areas.

    When session["humanize"] is True, uses Gaussian inter-key delays
    with digraph optimization. Otherwise uses fixed delay.
    """
    ref = params.get("ref")
    text = params.get("text", "")
    delay = params.get("delay_ms", 50)
    if not ref:
        return {"success": False, "error": "Missing required param: ref"}

    ref_map = session.get("ref_map", {})
    locator = await _resolve_ref(page, ref, ref_map)
    if locator is None:
        return {"success": False, "error": f"Ref {ref} not found. Take a new snapshot."}

    try:
        if session.get("humanize"):
            hb = HumanBehavior(intensity=session.get("humanize_intensity", 1.0))
            try:
                await asyncio.wait_for(
                    hb.human_type(page, locator, text, clear_first=False),
                    timeout=max(15.0, len(text) * 0.2),
                )
            except asyncio.TimeoutError:
                # Humanize timed out — fall back to plain typing
                await locator.click(timeout=5_000)
                await locator.press_sequentially(text, delay=delay, timeout=10_000)
        else:
            await locator.press_sequentially(text, delay=delay, timeout=10_000)
        return {
            "success": True,
            "extracted_content": f"Typed {len(text)} chars into {ref}",
        }
    except Exception as e:
        return {"success": False, "error": to_ai_friendly_error(e)}


async def action_scroll(page, params: dict, session: dict) -> dict:
    """Scroll the page.

    When session["humanize"] is True, uses eased acceleration/deceleration
    with reading pauses after scrolling.
    """
    direction = params.get("direction", "down")
    amount = params.get("amount", 300)  # pixels or "page"

    if amount == "page":
        vp = page.viewport_size
        amount = vp["height"] if vp else 800

    try:
        if session.get("humanize"):
            hb = HumanBehavior(intensity=session.get("humanize_intensity", 1.0))
            await hb.smooth_scroll(page, direction=direction, amount=int(amount))
        else:
            delta_y = int(amount) if direction == "down" else -int(amount)
            await page.mouse.wheel(0, delta_y)
            await asyncio.sleep(0.3)
        return {
            "success": True,
            "extracted_content": f"Scrolled {direction} {abs(int(amount))}px",
        }
    except Exception as e:
        return {"success": False, "error": to_ai_friendly_error(e)}


async def action_snapshot(page, params: dict, session: dict) -> dict:
    """Take an ARIA snapshot of the current page."""
    compact = params.get("compact", True)
    max_depth = params.get("max_depth", 10)
    cursor = params.get("cursor_interactive", True)
    webmcp_tools = session.get("webmcp_tools") or None

    result = await take_snapshot(
        page, compact=compact, max_depth=max_depth,
        cursor_interactive=cursor, webmcp_tools=webmcp_tools,
        session_id=session.get("session_id"),
    )

    # Store ref_map in session for subsequent actions
    if result.get("success"):
        session["ref_map"] = result.get("refs", {})

    return result


async def _firefox_screenshot_fallback(url: str, full_page: bool = False) -> bytes | None:
    """Screenshot via temporary Firefox instance (no Chromium headless bugs).

    Last-resort fallback. No auth/cookies carried over — public pages only.
    """
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        try:
            browser = await pw.firefox.launch(headless=True)
            pg = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await pg.goto(url, wait_until="domcontentloaded", timeout=15000)
            data = await pg.screenshot(full_page=full_page, type="png")
            return data
        finally:
            try:
                await browser.close()
            except Exception:
                pass
            try:
                await pw.stop()
            except Exception:
                pass
    except Exception:
        return None


async def action_screenshot(page, params: dict, session: dict) -> dict:
    """Take a screenshot (base64 PNG).

    Three-tier fallback:
    1. Playwright native (15s timeout, font-wait disabled via env var)
    2. CDP Page.captureScreenshot with optimizeForSpeed (10s timeout)
    3. Firefox screenshot of same URL (15s, Chromium sessions only, no auth)
    """
    full_page = params.get("full_page", False)

    # Tier 1: Playwright native
    try:
        data = await page.screenshot(full_page=full_page, type="png", timeout=15000)
    except Exception:
        data = None

    # Tier 2: CDP fallback with optimizeForSpeed
    if data is None:
        try:
            async def _cdp_shot():
                context = page.context
                cdp = await context.new_cdp_session(page)
                try:
                    await cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": False})
                except Exception:
                    pass
                cdp_params: dict = {"format": "png", "optimizeForSpeed": True}
                if full_page:
                    cdp_params["captureBeyondViewport"] = True
                result = await cdp.send("Page.captureScreenshot", cdp_params)
                await cdp.detach()
                return base64.b64decode(result["data"])
            data = await asyncio.wait_for(_cdp_shot(), timeout=10.0)
        except Exception:
            data = None

    # Tier 3: Firefox fallback (skip if already Firefox/Tier 3)
    if data is None:
        tier = session.get("tier", 1)
        if tier in (1, 2):
            data = await _firefox_screenshot_fallback(page.url, full_page)

    if data is None:
        return {"success": False, "error": "Screenshot failed: Playwright, CDP, and Firefox fallback all timed out (WSL2 headless bug)"}

    b64 = base64.b64encode(data).decode("ascii")
    return {
        "success": True,
        "screenshot": b64,
        "extracted_content": f"Screenshot taken ({len(data)} bytes)",
    }


async def action_wait(page, params: dict, session: dict) -> dict:
    """Explicit wait."""
    ms = params.get("ms", 1000)
    await asyncio.sleep(ms / 1000)
    return {
        "success": True,
        "extracted_content": f"Waited {ms}ms",
    }


async def action_evaluate(page, params: dict, session: dict) -> dict:
    """Execute JavaScript on the page and return the result.

    Gated behind BROWSER_USE_EVALUATE env var (default: enabled).
    """
    from config import Config

    if not Config.EVALUATE_ENABLED:
        return {
            "success": False,
            "error": "evaluate action is disabled. Set BROWSER_USE_EVALUATE=1 to enable.",
        }

    js = params.get("js", "")
    if not js:
        return {"success": False, "error": "Missing required param: js"}

    frame_url = params.get("frame_url", "")
    timeout_s = params.get("timeout_s", 30)
    try:
        target = page
        if frame_url:
            # Find matching frame by URL substring
            for frame in page.frames:
                if frame_url in frame.url:
                    target = frame
                    break
            else:
                return {"success": False, "error": f"No frame matching '{frame_url}' found. Frames: {[f.url[:80] for f in page.frames]}"}
        result = await asyncio.wait_for(target.evaluate(js), timeout=timeout_s)
        # Serialize result safely
        if result is None:
            content = "null"
        elif isinstance(result, (str, int, float, bool)):
            content = str(result)
        else:
            content = json.dumps(result, indent=2, default=str)

        # Cap output size
        if len(content) > 50_000:
            content = content[:50_000] + "\n... [truncated]"

        return {
            "success": True,
            "extracted_content": content,
        }
    except asyncio.TimeoutError:
        return {
            "success": False,
            "error": f"evaluate timed out after {timeout_s}s",
        }
    except Exception as e:
        return {"success": False, "error": to_ai_friendly_error(e)}


async def action_done(page, params: dict, session: dict) -> dict:
    """Mark the task as complete."""
    return {
        "success": params.get("success", True),
        "extracted_content": params.get("result", "Task completed."),
    }


# ---------------------------------------------------------------------------
# Extended actions (Phase 1.5)
# ---------------------------------------------------------------------------

async def action_press(page, params: dict, session: dict) -> dict:
    """Press a keyboard key, optionally focused on a ref."""
    key = params.get("key", "")
    ref = params.get("ref")

    if not key:
        return {"success": False, "error": "Missing required param: key"}

    try:
        if ref:
            ref_map = session.get("ref_map", {})
            locator = await _resolve_ref(page, ref, ref_map)
            if locator is None:
                return {"success": False, "error": f"Ref {ref} not found. Take a new snapshot."}
            await locator.press(key, timeout=10_000)
        else:
            await page.keyboard.press(key)

        return {
            "success": True,
            "extracted_content": f"Pressed {key}",
        }
    except Exception as e:
        return {"success": False, "error": to_ai_friendly_error(e)}


async def action_select(page, params: dict, session: dict) -> dict:
    """Select a dropdown option."""
    ref = params.get("ref")
    value = params.get("value", "")
    if not ref:
        return {"success": False, "error": "Missing required param: ref"}

    ref_map = session.get("ref_map", {})
    locator = await _resolve_ref(page, ref, ref_map)
    if locator is None:
        return {"success": False, "error": f"Ref {ref} not found. Take a new snapshot."}

    try:
        await locator.select_option(value, timeout=10_000)
        return {
            "success": True,
            "extracted_content": f"Selected '{value}' in {ref}",
        }
    except Exception as e:
        return {"success": False, "error": to_ai_friendly_error(e)}


async def action_go_back(page, params: dict, session: dict) -> dict:
    """Navigate back. Returns failure if there is no history to go back to."""
    old_url = page.url
    try:
        response = await page.go_back(wait_until="domcontentloaded", timeout=30_000)
        # go_back returns None when there is no history entry to go back to
        if response is None and page.url == old_url:
            return {
                "success": False,
                "error": "No browser history to go back to.",
            }
        return {
            "success": True,
            "extracted_content": f"Navigated back to {page.url}",
            "page_changed": True,
            "new_url": page.url,
            "new_title": await page.title(),
        }
    except Exception as e:
        return {"success": False, "error": to_ai_friendly_error(e)}


async def action_cookies_get(page, params: dict, session: dict) -> dict:
    """Get cookies, optionally filtered by domain."""
    domain = params.get("domain")
    try:
        urls = [f"https://{domain}"] if domain else None
        cookies = await page.context.cookies(urls)
        return {
            "success": True,
            "extracted_content": json.dumps(cookies, indent=2, default=str),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def action_cookies_set(page, params: dict, session: dict) -> dict:
    """Set cookies."""
    cookies = params.get("cookies", [])
    if not cookies:
        return {"success": False, "error": "Missing required param: cookies"}
    try:
        await page.context.add_cookies(cookies)
        return {
            "success": True,
            "extracted_content": f"Set {len(cookies)} cookie(s)",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Tab actions
# ---------------------------------------------------------------------------

async def action_tab_new(page, params: dict, session: dict) -> dict:
    """Open a new tab."""
    import browser_engine
    url = params.get("url")
    session_id = session.get("session_id", "")
    new_page = await browser_engine.new_page(session_id, url)
    if new_page is None:
        return {"success": False, "error": "Failed to create new tab"}
    return {
        "success": True,
        "extracted_content": f"New tab opened" + (f" at {url}" if url else ""),
        "page_changed": True,
        "new_url": new_page.url,
    }


async def action_tab_switch(page, params: dict, session: dict) -> dict:
    """Switch to a tab by index (0-based)."""
    import browser_engine
    index = params.get("index", 0)
    session_id = session.get("session_id", "")
    switched = await browser_engine.switch_page(session_id, index)
    if switched is None:
        return {"success": False, "error": f"Tab index {index} not found"}
    return {
        "success": True,
        "extracted_content": f"Switched to tab {index}",
        "page_changed": True,
        "new_url": switched.url,
    }


async def action_tab_close(page, params: dict, session: dict) -> dict:
    """Close a tab by index."""
    import browser_engine
    index = params.get("index", 0)
    session_id = session.get("session_id", "")
    ok = await browser_engine.close_page(session_id, index)
    if not ok:
        return {"success": False, "error": f"Tab index {index} not found"}
    return {
        "success": True,
        "extracted_content": f"Closed tab {index}",
    }


# ---------------------------------------------------------------------------
# WebMCP actions (Chrome 146+ structured tool interaction)
# ---------------------------------------------------------------------------

async def action_webmcp_discover(page, params: dict, session: dict) -> dict:
    """Discover WebMCP tools on the current page.

    Prefers the native Chrome testing API (navigator.modelContextTesting.listTools)
    which returns both imperative and declarative tools. Falls back to the init
    script interceptor if the testing API isn't available.
    """
    js = """
    () => {
        // Prefer native Chrome testing API (available with --enable-features=WebMCPTesting)
        if (navigator.modelContextTesting && typeof navigator.modelContextTesting.listTools === 'function') {
            const tools = navigator.modelContextTesting.listTools();
            return {
                available: true,
                source: 'native',
                tools: tools.map(t => ({
                    name: t.name,
                    description: t.description,
                    inputSchema: typeof t.inputSchema === 'string'
                        ? JSON.parse(t.inputSchema) : (t.inputSchema || {}),
                })),
            };
        }

        // Fallback: init script interceptor + declarative form scan
        if (window.__webmcp) {
            if (typeof window.__webmcp.rescanDeclarative === 'function') {
                window.__webmcp.rescanDeclarative();
            }
            const allTools = [];
            for (const [name, t] of Object.entries(window.__webmcp.tools || {})) {
                allTools.push({
                    name: t.name,
                    description: t.description,
                    inputSchema: t.inputSchema,
                    type: 'imperative',
                });
            }
            for (const [name, t] of Object.entries(window.__webmcp.declarative || {})) {
                allTools.push({
                    name: t.name,
                    description: t.description,
                    inputSchema: t.inputSchema,
                    type: 'declarative',
                });
            }
            return { available: window.__webmcp.available, source: 'interceptor', tools: allTools };
        }

        return { available: false, source: 'none', tools: [] };
    }
    """
    try:
        result = await page.evaluate(js)
        available = result.get("available", False)
        source = result.get("source", "none")

        # Update session state
        session["webmcp_available"] = available
        session["webmcp_source"] = source
        tool_map = {}
        for t in result.get("tools", []):
            tool_map[t["name"]] = t
        session["webmcp_tools"] = tool_map

        # Also update browser_engine session state
        import browser_engine
        sid = session.get("session_id", "")
        be_session = browser_engine._sessions.get(sid)
        if be_session:
            be_session["webmcp_available"] = available
            be_session["webmcp_tools"] = tool_map

        tool_count = len(result.get("tools", []))
        summary = json.dumps(result, indent=2)

        return {
            "success": True,
            "extracted_content": summary,
            "webmcp_available": available,
            "tool_count": tool_count,
        }
    except Exception as e:
        session["webmcp_available"] = False
        return {"success": False, "error": to_ai_friendly_error(e)}


async def action_webmcp_call(page, params: dict, session: dict) -> dict:
    """Call a WebMCP tool by name with structured arguments.

    Prefers native Chrome testing API (executeTool takes JSON string).
    Falls back to init script interceptor (executeTool takes object).
    """
    tool_name = params.get("tool")
    args = params.get("args", {})
    if not tool_name:
        return {"success": False, "error": "Missing required param: tool"}

    # Validate tool exists in session state
    known = session.get("webmcp_tools", {})
    if tool_name not in known:
        available = list(known.keys()) if known else []
        hint = f"Available: {', '.join(available)}" if available else "Run webmcp_discover first"
        return {"success": False, "error": f"Tool '{tool_name}' not found. {hint}"}

    old_url = page.url
    try:
        # Use native testing API when available (executeTool takes JSON string arg)
        # Falls back to interceptor's executeTool (takes object arg)
        # Wrap in asyncio.wait_for because executeTool can trigger cross-document
        # navigation which destroys the JS context — page.evaluate() would hang.
        try:
            result = await asyncio.wait_for(
                page.evaluate(
                    """async ([name, argsJson]) => {
                        // Prefer native Chrome testing API
                        if (navigator.modelContextTesting &&
                            typeof navigator.modelContextTesting.executeTool === 'function') {
                            const r = await navigator.modelContextTesting.executeTool(name, argsJson);
                            // null = navigation was triggered (cross-document)
                            if (r === null) return { _navigated: true };
                            // result is a JSON string
                            try { return JSON.parse(r); } catch { return { text: r }; }
                        }
                        // Fallback: init script interceptor
                        if (window.__webmcp && typeof window.__webmcp.executeTool === 'function') {
                            return await window.__webmcp.executeTool(name, JSON.parse(argsJson));
                        }
                        return { error: 'No WebMCP execution path available' };
                    }""",
                    [tool_name, json.dumps(args)],
                ),
                timeout=15.0,
            )
        except (asyncio.TimeoutError, Exception) as eval_err:
            # Navigation during evaluate destroys the JS context.
            # Check if the URL changed — if so, the tool triggered navigation.
            await asyncio.sleep(1.0)
            new_url = page.url
            if new_url != old_url:
                return {
                    "success": True,
                    "extracted_content": "Tool triggered navigation (cross-document)",
                    "page_changed": True,
                    "new_url": new_url,
                    "new_title": await page.title(),
                }
            raise eval_err

        # Allow page time to update after tool execution
        await asyncio.sleep(0.5)
        new_url = page.url

        out: dict[str, Any] = {
            "success": True,
            "page_changed": new_url != old_url,
        }

        if result and isinstance(result, dict):
            if result.get("_navigated"):
                out["extracted_content"] = "Tool triggered navigation"
                out["page_changed"] = True
            elif result.get("error"):
                out["success"] = False
                out["error"] = result["error"]
            else:
                out["extracted_content"] = json.dumps(result, indent=2, default=str)
        else:
            out["extracted_content"] = str(result) if result else "Tool executed (no return value)"

        if new_url != old_url:
            out["new_url"] = new_url
            out["new_title"] = await page.title()

        return out
    except Exception as e:
        # Final fallback: check if navigation happened despite error
        try:
            new_url = page.url
            if new_url != old_url:
                return {
                    "success": True,
                    "extracted_content": "Tool triggered navigation",
                    "page_changed": True,
                    "new_url": new_url,
                    "new_title": await page.title(),
                }
        except Exception:
            pass
        return {"success": False, "error": to_ai_friendly_error(e)}


# ---------------------------------------------------------------------------
# Search & Discovery actions (Phase 2)
# ---------------------------------------------------------------------------

async def action_search_page(page, params: dict, session: dict) -> dict:
    """Search for text on the current page.

    Returns matching text snippets with context. Read-only — does NOT
    interact with any elements.

    Params:
        query (str): Text to search for (case-insensitive substring).
        max_results (int): Maximum matches to return (default 10).
    """
    query = (params.get("query") or "").strip()
    if not query:
        return {"success": False, "error": "Missing required param: query"}

    max_results = params.get("max_results", 10)

    js = """
    (args) => {
        const query = args.query.toLowerCase();
        const maxResults = args.maxResults;
        const results = [];
        const walker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_TEXT, null,
        );
        let node;
        while ((node = walker.nextNode()) && results.length < maxResults) {
            const text = node.textContent.trim();
            if (!text || text.length < 3) continue;
            const idx = text.toLowerCase().indexOf(query);
            if (idx === -1) continue;
            const start = Math.max(0, idx - 60);
            const end = Math.min(text.length, idx + query.length + 60);
            let snippet = text.slice(start, end).trim();
            if (start > 0) snippet = '...' + snippet;
            if (end < text.length) snippet = snippet + '...';
            const el = node.parentElement;
            const tag = el ? el.tagName.toLowerCase() : '?';
            const role = el ? (el.getAttribute('role') || '') : '';
            results.push({snippet, tag, role});
        }
        return results;
    }
    """
    try:
        matches = await page.evaluate(js, {"query": query, "maxResults": max_results})
        if not matches:
            return {
                "success": True,
                "extracted_content": f"No matches found for '{query}' on this page.",
            }
        formatted = [f"  [{i+1}] ({m['tag']}) {m['snippet']}" for i, m in enumerate(matches)]
        return {
            "success": True,
            "extracted_content": f"Found {len(matches)} match(es) for '{query}':\n" + "\n".join(formatted),
            "match_count": len(matches),
        }
    except Exception as e:
        return {"success": False, "error": to_ai_friendly_error(e)}


async def action_find_elements(page, params: dict, session: dict) -> dict:
    """Find elements matching criteria in the current snapshot's ref map.

    Searches the existing ref map — does NOT re-snapshot. Take a snapshot
    first if you haven't already.

    Params:
        text (str): Substring to match against element names (case-insensitive).
        role (str): ARIA role to filter by (e.g. 'button', 'link', 'textbox').
        At least one of text or role must be provided.
    """
    text_query = (params.get("text") or "").strip().lower()
    role_query = (params.get("role") or "").strip().lower()

    if not text_query and not role_query:
        return {"success": False, "error": "Provide at least one of: text, role"}

    ref_map = session.get("ref_map", {})
    if not ref_map:
        return {"success": False, "error": "No snapshot taken yet. Take a snapshot first."}

    matches = []
    for ref_key, ref_data in ref_map.items():
        ref_role = (ref_data.get("role") or "").lower()
        ref_name = (ref_data.get("name") or "").lower()

        if role_query and ref_role != role_query:
            continue
        if text_query and text_query not in ref_name:
            continue
        matches.append(f"  {ref_key} ({ref_data.get('role', '')}) \"{ref_data.get('name', '')}\"")

    if not matches:
        return {
            "success": True,
            "extracted_content": f"No elements found matching criteria (text='{text_query}', role='{role_query}').",
        }

    return {
        "success": True,
        "extracted_content": f"Found {len(matches)} matching element(s):\n" + "\n".join(matches),
        "match_count": len(matches),
    }


async def action_extract(page, params: dict, session: dict) -> dict:
    """Extract page content as clean markdown.

    Returns the full visible text converted to markdown. Use when the ARIA
    snapshot lacks detail (e.g. article text, table data, structured content).

    Params:
        max_chars (int): Maximum characters to return (default 30000).
        include_links (bool): Include link URLs in markdown (default false).
    """
    import re

    max_chars = params.get("max_chars", 30_000)
    include_links = params.get("include_links", False)

    try:
        from markdownify import markdownify as md
    except ImportError:
        return {
            "success": False,
            "error": "markdownify not installed. Run: pip install markdownify",
        }

    try:
        html = await page.evaluate("() => document.body.innerHTML")
        if not html:
            return {"success": False, "error": "Empty page body"}

        content = md(
            html,
            heading_style="ATX",
            strip=["script", "style", "noscript", "svg"],
            bullets="-",
            escape_asterisks=False,
            escape_underscores=False,
            escape_misc=False,
            autolinks=False,
        )

        if not include_links:
            content = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', content)

        # Light cleanup: collapse whitespace, remove JSON blobs
        content = re.sub(r'\n{4,}', '\n\n\n', content)
        content = re.sub(r'\{"\$type":[^}]{100,}\}', '', content)
        content = re.sub(r'\{"[^"]{5,}":\{[^}]{100,}\}', '', content)

        lines = content.split('\n')
        lines = [l for l in lines if len(l.strip()) > 2
                 or not l.strip()
                 or l.strip().startswith('#')]
        content = '\n'.join(lines).strip()

        truncated = False
        if len(content) > max_chars:
            cut = content.rfind('\n\n', max_chars - 500, max_chars)
            if cut < 0:
                cut = content.rfind('.', max_chars - 200, max_chars)
            if cut < 0:
                cut = max_chars
            content = content[:cut]
            truncated = True

        result = {
            "success": True,
            "extracted_content": content,
            "char_count": len(content),
            "url": page.url,
        }
        if truncated:
            result["truncated"] = True
            result["hint"] = "Content truncated. Increase max_chars or extract specific sections."
        return result

    except Exception as e:
        return {"success": False, "error": to_ai_friendly_error(e)}


# ---------------------------------------------------------------------------
# File & coordinate actions (Phase 2)
# ---------------------------------------------------------------------------

async def action_upload_file(page, params: dict, session: dict) -> dict:
    """Upload a file to a file input element.

    Params:
        ref (str): Ref near the upload area (button or file input).
        path (str): Absolute path to the file on the server filesystem.
    """
    import os

    ref = params.get("ref")
    file_path = params.get("path", "")

    if not ref:
        return {"success": False, "error": "Missing required param: ref"}
    if not file_path:
        return {"success": False, "error": "Missing required param: path"}
    if not os.path.isfile(file_path):
        return {"success": False, "error": f"File not found: {file_path}"}

    ref_map = session.get("ref_map", {})
    locator = await _resolve_ref(page, ref, ref_map)
    if locator is None:
        return {"success": False, "error": f"Ref {ref} not found. Take a new snapshot."}

    js_find_file_input = """
    (el) => {
        if (el.tagName === 'INPUT' && el.type === 'file') return true;
        if (el.querySelector('input[type="file"]')) return true;
        let current = el;
        for (let i = 0; i < 3 && current; i++) {
            current = current.parentElement;
            if (!current) break;
            if (current.querySelector('input[type="file"]')) return true;
        }
        return false;
    }
    """

    try:
        # Check if ref itself or nearby is a file input
        has_file_input = await locator.evaluate(js_find_file_input)

        if has_file_input:
            # Find file input scoped to the ref element or its ancestors
            js_get_file_input = """
            (el) => {
                if (el.tagName === 'INPUT' && el.type === 'file') return null;  // el itself
                let fi = el.querySelector('input[type="file"]');
                if (fi) return null;  // child — use locator-scoped search below
                let current = el;
                for (let i = 0; i < 3 && current; i++) {
                    current = current.parentElement;
                    if (!current) break;
                    fi = current.querySelector('input[type="file"]');
                    if (fi) {
                        // Return a selector for this specific input
                        if (fi.id) return '#' + CSS.escape(fi.id);
                        if (fi.name) return 'input[type="file"][name="' + CSS.escape(fi.name) + '"]';
                        return null;  // fallback to locator-scoped
                    }
                }
                return null;
            }
            """
            specific_selector = await locator.evaluate(js_get_file_input)

            if specific_selector:
                file_locator = page.locator(specific_selector).first
            else:
                # Ref is or contains the file input — scope search to locator
                child_input = locator.locator('input[type="file"]')
                if await child_input.count() > 0:
                    file_locator = child_input.first
                else:
                    # Ref itself is the file input
                    file_locator = locator
            await file_locator.set_input_files(file_path)
        else:
            # Last resort: any file input on the page
            file_locator = page.locator('input[type="file"]')
            count = await file_locator.count()
            if count == 0:
                return {"success": False, "error": "No file input found on page"}
            if count > 1:
                return {
                    "success": False,
                    "error": f"Found {count} file inputs on page. Use a ref closer to the target upload area.",
                }
            await file_locator.first.set_input_files(file_path)

        filename = os.path.basename(file_path)
        return {
            "success": True,
            "extracted_content": f"Uploaded {filename} via file input near {ref}",
            "page_changed": True,
        }
    except Exception as e:
        return {"success": False, "error": f"Upload failed: {to_ai_friendly_error(e)}"}


async def action_click_coordinate(page, params: dict, session: dict) -> dict:
    """Click at specific x,y coordinates on the page.

    Use as last resort when ref-based click fails. Coordinates are
    relative to the viewport (not the document).

    Params:
        x (int|float): X coordinate in viewport pixels.
        y (int|float): Y coordinate in viewport pixels.
    """
    x = params.get("x")
    y = params.get("y")
    if x is None or y is None:
        return {"success": False, "error": "Missing required params: x, y"}

    try:
        x = float(x)
        y = float(y)
    except (TypeError, ValueError):
        return {"success": False, "error": "x and y must be numeric"}

    old_url = page.url
    try:
        if session.get("humanize"):
            hb = HumanBehavior(intensity=session.get("humanize_intensity", 1.0))
            await page.mouse.move(x, y, steps=random.randint(5, 12))
            await asyncio.sleep(random.uniform(0.05, 0.15))
            await page.mouse.click(x, y)
        else:
            await page.mouse.click(x, y)

        await asyncio.sleep(0.3)
        new_url = page.url
        return {
            "success": True,
            "extracted_content": f"Clicked at ({x}, {y})",
            "page_changed": new_url != old_url,
            "new_url": new_url if new_url != old_url else None,
            "new_title": await page.title() if new_url != old_url else None,
        }
    except Exception as e:
        return {"success": False, "error": f"Coordinate click failed: {to_ai_friendly_error(e)}"}


async def action_get_downloads(page, params: dict, session: dict) -> dict:
    """List files downloaded during this session. Read-only."""
    downloads = session.get("downloads", [])
    if not downloads:
        return {
            "success": True,
            "extracted_content": "No files downloaded in this session.",
        }

    formatted = [
        f"  [{i+1}] {d['filename']} ({d['size']} bytes) -> {d['path']}"
        for i, d in enumerate(downloads)
    ]
    return {
        "success": True,
        "extracted_content": f"{len(downloads)} file(s) downloaded:\n" + "\n".join(formatted),
        "downloads": downloads,
    }


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------

ACTION_HANDLERS: dict[str, ActionHandler] = {
    # Core (Phase 1)
    "navigate": action_navigate,
    "click": action_click,
    "fill": action_fill,
    "type": action_type,
    "scroll": action_scroll,
    "snapshot": action_snapshot,
    "screenshot": action_screenshot,
    "wait": action_wait,
    "evaluate": action_evaluate,
    "done": action_done,
    # Extended (Phase 1.5)
    "press": action_press,
    "select": action_select,
    "go_back": action_go_back,
    "cookies_get": action_cookies_get,
    "cookies_set": action_cookies_set,
    "tab_new": action_tab_new,
    "tab_switch": action_tab_switch,
    "tab_close": action_tab_close,
    # WebMCP (Chrome 146+ structured tool discovery)
    "webmcp_discover": action_webmcp_discover,
    "webmcp_call": action_webmcp_call,
    # Search & Discovery (Phase 2)
    "search_page": action_search_page,
    "find_elements": action_find_elements,
    "extract": action_extract,
    # File Operations (Phase 2)
    "upload_file": action_upload_file,
    "get_downloads": action_get_downloads,
    # Coordinate Actions (Phase 2)
    "click_coordinate": action_click_coordinate,
}


async def execute_action(
    page,
    action_name: str,
    params: dict,
    session: dict,
) -> dict:
    """Execute a browser action by name.

    Args:
        page: Playwright Page
        action_name: Name of the action (e.g. 'click', 'navigate')
        params: Action parameters
        session: Mutable session dict (holds ref_map, session_id, etc.)

    Returns:
        ActionResult-like dict
    """
    handler = ACTION_HANDLERS.get(action_name)
    if handler is None:
        return {
            "success": False,
            "error": f"Unknown action: {action_name}. "
                     f"Available: {', '.join(sorted(ACTION_HANDLERS))}",
        }

    try:
        return await handler(page, params, session)
    except Exception as e:
        return {
            "success": False,
            "error": to_ai_friendly_error(e),
        }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    async def main():
        request = json.loads(sys.stdin.read())
        action_name = request.get("action")
        params = request.get("params", {})
        session_id = request.get("session_id")

        if not action_name:
            print(json.dumps({"success": False, "error": "Missing action name"}))
            return
        if not session_id:
            print(json.dumps({"success": False, "error": "Missing session_id"}))
            return

        import browser_engine
        page = await browser_engine.get_page(session_id)
        if page is None:
            print(json.dumps({"success": False, "error": f"Session {session_id} not found"}))
            return

        # Build session context
        session_ctx = {
            "session_id": session_id,
            "ref_map": request.get("ref_map", {}),
        }

        result = await execute_action(page, action_name, params, session_ctx)

        # Include updated ref_map if snapshot was taken
        if "ref_map" in session_ctx and action_name == "snapshot":
            result["refs"] = session_ctx["ref_map"]

        print(json.dumps(result, default=str))

    asyncio.run(main())
