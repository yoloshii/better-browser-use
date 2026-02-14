"""
Action implementations for browser-use.

Core (18 actions) + humanization layer via behavior module.
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

    result = await take_snapshot(page, compact=compact, max_depth=max_depth, cursor_interactive=cursor)

    # Store ref_map in session for subsequent actions
    if result.get("success"):
        session["ref_map"] = result.get("refs", {})

    return result


async def action_screenshot(page, params: dict, session: dict) -> dict:
    """Take a screenshot (base64 PNG)."""
    full_page = params.get("full_page", False)

    try:
        data = await page.screenshot(full_page=full_page, type="png")
        b64 = base64.b64encode(data).decode("ascii")
        return {
            "success": True,
            "screenshot": b64,
            "extracted_content": f"Screenshot taken ({len(data)} bytes)",
        }
    except Exception as e:
        return {"success": False, "error": to_ai_friendly_error(e)}


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
                return {
                    "success": False,
                    "error": f"No frame matching '{frame_url}' found. "
                    f"Frames: {[f.url[:80] for f in page.frames]}",
                }
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
