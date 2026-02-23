#!/usr/bin/env python3
"""
Lightweight HTTP server for browser-use skill.

Runs on VM 202, keeps browser sessions alive between requests.
Exposes the same JSON API as agent.py but over HTTP.

Usage:
    ~/.venvs/scraper/bin/python3 ~/browser-use/scripts/server.py [--port 8500]

All requests: POST / with JSON body (same format as agent.py stdin).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import sys
from http import HTTPStatus

# Ensure scripts/ is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config

# Import aiohttp for lightweight server
from aiohttp import web


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.StreamResponse:
    """Bearer token auth middleware.

    Skips auth for /health endpoint and when no token is configured.
    """
    # Health check is always unauthenticated
    if request.path == "/health":
        return await handler(request)

    token = Config.AUTH_TOKEN
    if not token:
        # No token configured — auth disabled (dev mode)
        return await handler(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return web.json_response(
            {"success": False, "error": "Missing or malformed Authorization header"},
            status=HTTPStatus.UNAUTHORIZED,
        )

    provided = auth_header[7:]  # strip "Bearer "
    if not secrets.compare_digest(provided, token):
        return web.json_response(
            {"success": False, "error": "Invalid token"},
            status=HTTPStatus.FORBIDDEN,
        )

    return await handler(request)


async def _with_session_lock(session_id: str, coro_fn):
    """Execute a coroutine factory while holding the session lock.

    Args:
        session_id: Session to lock.
        coro_fn: Zero-arg async callable (NOT a pre-created coroutine).
                 Called only after the lock is acquired.

    Also touches the session's last_activity timestamp.
    Returns the coroutine result, or an error dict if session not found.
    """
    import browser_engine

    lock = browser_engine.get_session_lock(session_id)
    if lock is None:
        return {"success": False, "error": f"Session {session_id} not found or expired"}

    async with lock:
        browser_engine.touch_session(session_id)
        return await coro_fn()


async def handle_request_inner(request_data: dict) -> dict:
    """Route request to handler (same logic as agent.py)."""
    op = request_data.get("op", "")

    if op == "launch":
        import browser_engine
        return await browser_engine.launch(
            tier=request_data.get("tier", 1),
            profile=request_data.get("profile"),
            viewport=request_data.get("viewport"),
            url=request_data.get("url"),
        )

    elif op in ("action", "actions"):
        import browser_engine
        import actions
        from rate_limiter import get_rate_limiter, EXEMPT_ACTIONS

        session_id = request_data.get("session_id")
        if not session_id:
            return {"success": False, "error": "Missing session_id"}

        async def _execute_single_action(
            session_id: str,
            action_name: str,
            params: dict,
            req_ref_map: dict | None = None,
        ) -> dict:
            """Execute one action with full pipeline (rate limit, loop detect, block detect).

            Shared by single-action and batch-action handlers.
            """
            page = await browser_engine.get_page(session_id)
            if page is None:
                return {"success": False, "error": f"Session {session_id} not found or expired"}

            # Rate limiting (exempt read-only actions)
            if action_name not in EXEMPT_ACTIONS:
                from urllib.parse import urlparse
                domain = urlparse(page.url).netloc.lower()
                limiter = get_rate_limiter()
                if not limiter.check(domain):
                    wait = limiter.wait_time(domain)
                    return {
                        "success": False,
                        "error": f"Rate limited on {domain}. Wait {wait:.1f}s.",
                        "code": "RATE_LIMITED",
                        "wait_seconds": round(wait, 1),
                    }

            # Use request-provided ref_map, or fall back to server-side session ref_map
            if req_ref_map:
                ref_map = req_ref_map
            else:
                ref_map = browser_engine.get_session_ref_map(session_id)

            # Build session context with humanize flag from session state
            session_data = browser_engine._sessions.get(session_id, {})
            humanize_intensity = session_data.get("humanize_intensity", 1.0)

            # Auto-boost intensity on sensitive domains (smarter, not slower)
            if session_data.get("humanize"):
                from urllib.parse import urlparse as _parse
                _dom = _parse(page.url).netloc.lower()
                if _dom in Config.SENSITIVE_RATE_LIMITS and _dom != "default":
                    humanize_intensity = max(humanize_intensity, 1.3)

            session_ctx = {
                "session_id": session_id,
                "ref_map": ref_map,
                "tier": session_data.get("tier", 1),
                "humanize": session_data.get("humanize", False),
                "humanize_intensity": humanize_intensity,
                "webmcp_available": session_data.get("webmcp_available"),
                "webmcp_tools": session_data.get("webmcp_tools", {}),
                "downloads": session_data.get("downloads", []),
            }

            old_url = page.url
            result = await actions.execute_action(page, action_name, params, session_ctx)

            # Increment action counter
            session_data["action_count"] = session_data.get("action_count", 0) + 1

            # Loop detection (skip read-only actions)
            _loop_skip = {"snapshot", "screenshot", "done", "wait", "search_page",
                          "find_elements", "extract", "get_downloads"}
            loop_detector = session_data.get("loop_detector")
            if loop_detector and action_name not in _loop_skip:
                from models import PageFingerprint
                fingerprint = None
                current_refs = session_ctx.get("ref_map") or browser_engine.get_session_ref_map(session_id)
                if current_refs:
                    fingerprint = PageFingerprint.from_snapshot(
                        url=page.url,
                        refs=current_refs,
                        tab_count=len(page.context.pages),
                    )
                warning = loop_detector.record(action_name, params, fingerprint)
                if warning:
                    result["loop_warning"] = warning

            # Reset loop detector on cross-domain navigation
            if result.get("page_changed") and loop_detector:
                from urllib.parse import urlparse as _urlp
                old_domain = _urlp(old_url).netloc
                new_domain = _urlp(page.url).netloc
                if old_domain != new_domain:
                    loop_detector.reset()

            # Record rate-limit usage only after successful action execution
            if action_name not in EXEMPT_ACTIONS and result.get("success", False):
                from urllib.parse import urlparse as _urlparse
                _domain = _urlparse(page.url).netloc.lower()
                get_rate_limiter().record(_domain)

            # Persist updated ref_map to session state after snapshot
            if action_name == "snapshot" and "ref_map" in session_ctx:
                browser_engine.set_session_ref_map(session_id, session_ctx["ref_map"])
                result["refs"] = session_ctx["ref_map"]

            # Lightweight block detection after page-changing actions
            if result.get("page_changed"):
                try:
                    from detection import is_blocked
                    active_page = await browser_engine.get_page(session_id)
                    if active_page:
                        protection = await is_blocked(active_page)
                        if protection:
                            result["blocked"] = True
                            result["protection"] = protection

                            # Auto-solve CAPTCHA if solver keys are configured
                            if protection in ("captcha", "cloudflare") and (
                                Config.CAPSOLVER_API_KEY or Config.TWOCAPTCHA_API_KEY
                            ):
                                try:
                                    from captcha_solver import solve_captcha
                                    solve_result = await solve_captcha(active_page)
                                    if solve_result.get("success"):
                                        result["captcha_solved"] = True
                                        result["solver"] = solve_result.get("solver")
                                        result["solve_time_s"] = solve_result.get("solve_time_s")
                                        result["blocked"] = False
                                    else:
                                        result["captcha_solve_failed"] = True
                                        result["captcha_error"] = solve_result.get("error", "")
                                except Exception:
                                    pass
                except Exception:
                    pass

            return result

        if op == "action":
            # Single action
            async def _do_action():
                return await _execute_single_action(
                    session_id,
                    request_data.get("action"),
                    request_data.get("params", {}),
                    req_ref_map=request_data.get("ref_map"),
                )
            return await _with_session_lock(session_id, _do_action)

        else:
            # Batch actions (op == "actions")
            action_list = request_data.get("actions")
            if not action_list or not isinstance(action_list, list):
                return {"success": False, "error": "Missing or invalid 'actions' list"}
            if len(action_list) > 20:
                return {"success": False, "error": "Batch limited to 20 actions"}
            stop_on_error = request_data.get("stop_on_error", True)

            async def _do_batch():
                results = []
                stopped_at = None
                for i, step in enumerate(action_list):
                    a_name = step.get("action")
                    a_params = step.get("params", {})
                    if not a_name:
                        r = {"success": False, "error": f"Action at index {i} missing 'action' field"}
                    else:
                        r = await _execute_single_action(session_id, a_name, a_params)
                    results.append(r)
                    if not r.get("success", False) and stop_on_error:
                        stopped_at = i
                        break
                overall = stopped_at is None
                out = {"success": overall, "results": results, "stopped_at": stopped_at}
                if stopped_at is not None:
                    out["error"] = results[stopped_at].get("error", "Action failed")
                return out

            return await _with_session_lock(session_id, _do_batch)

    elif op == "snapshot":
        import browser_engine
        from snapshot import take_snapshot

        session_id = request_data.get("session_id")
        if not session_id:
            return {"success": False, "error": "Missing session_id"}

        async def _do_snapshot():
            page = await browser_engine.get_page(session_id)
            if page is None:
                return {"success": False, "error": f"Session {session_id} not found or expired"}

            # Pass WebMCP tools to snapshot for header display
            session_data = browser_engine._sessions.get(session_id, {})
            webmcp_tools = session_data.get("webmcp_tools") or None

            result = await take_snapshot(
                page,
                compact=request_data.get("compact", True),
                max_depth=request_data.get("max_depth", 10),
                cursor_interactive=request_data.get("cursor_interactive", True),
                webmcp_tools=webmcp_tools,
                session_id=session_id,
            )

            # Persist ref_map to session state for subsequent actions
            if result.get("success") and "refs" in result:
                browser_engine.set_session_ref_map(session_id, result["refs"])

            # Surface dismissed popups and downloads in snapshot header
            if result.get("success"):
                extra_header = ""
                dismissed = session_data.get("dismissed_popups", [])
                if dismissed:
                    recent = dismissed[-3:]
                    popup_lines = [
                        f"  [{p['type']}] {p['message'][:80]} -> {p['action']}"
                        for p in recent
                    ]
                    extra_header += "Dismissed popups:\n" + "\n".join(popup_lines) + "\n\n"

                downloads = session_data.get("downloads", [])
                if downloads:
                    dl_lines = [
                        f"  {d['filename']} ({d['size']} bytes)"
                        for d in downloads[-5:]
                    ]
                    extra_header += "Downloaded files:\n" + "\n".join(dl_lines) + "\n\n"

                if extra_header:
                    result["tree"] = extra_header + result["tree"]

            return result

        return await _with_session_lock(session_id, _do_snapshot)

    elif op == "screenshot":
        import browser_engine
        import base64

        session_id = request_data.get("session_id")
        if not session_id:
            return {"success": False, "error": "Missing session_id"}

        async def _do_screenshot():
            page = await browser_engine.get_page(session_id)
            if page is None:
                return {"success": False, "error": f"Session {session_id} not found or expired"}

            import asyncio as _aio
            from actions import _firefox_screenshot_fallback
            full_page = request_data.get("full_page", False)

            # Tier 1: Playwright native
            data = None
            try:
                data = await page.screenshot(full_page=full_page, type="png", timeout=15000)
            except Exception:
                pass

            # Tier 2: CDP with optimizeForSpeed
            if data is None:
                try:
                    async def _cdp_shot():
                        context = page.context
                        cdp = await context.new_cdp_session(page)
                        try:
                            await cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": False})
                        except Exception:
                            pass
                        cdp_params = {"format": "png", "optimizeForSpeed": True}
                        if full_page:
                            cdp_params["captureBeyondViewport"] = True
                        result = await cdp.send("Page.captureScreenshot", cdp_params)
                        await cdp.detach()
                        return base64.b64decode(result["data"])
                    data = await _aio.wait_for(_cdp_shot(), timeout=10.0)
                except Exception:
                    pass

            # Tier 3: Firefox fallback (skip if session is already Firefox)
            if data is None:
                session_data = browser_engine._sessions.get(session_id, {})
                tier = session_data.get("tier", 1)
                if tier in (1, 2):
                    data = await _firefox_screenshot_fallback(page.url, full_page)

            if data is None:
                return {"success": False, "error": "Screenshot failed: Playwright, CDP, and Firefox fallback all timed out"}

            return {
                "success": True,
                "screenshot": base64.b64encode(data).decode("ascii"),
                "size": len(data),
            }

        return await _with_session_lock(session_id, _do_screenshot)

    elif op == "close":
        import browser_engine

        session_id = request_data.get("session_id")
        if not session_id:
            return {"success": False, "error": "Missing session_id"}

        async def _do_close():
            if request_data.get("save_profile"):
                await browser_engine.save_state(session_id, request_data.get("save_profile"))
            return await browser_engine.close(session_id)

        return await _with_session_lock(session_id, _do_close)

    elif op == "save":
        import browser_engine

        session_id = request_data.get("session_id")
        if not session_id:
            return {"success": False, "error": "Missing session_id"}

        async def _do_save():
            return await browser_engine.save_state(
                session_id,
                request_data.get("profile"),
            )

        return await _with_session_lock(session_id, _do_save)

    elif op == "status":
        import browser_engine

        session_id = request_data.get("session_id")
        if session_id:
            info = await browser_engine.get_session_info(session_id)
            if info is None:
                return {"success": False, "error": f"Session {session_id} not found"}
            return {"success": True, **info}
        else:
            sessions = await browser_engine.list_sessions()
            return {"success": True, "sessions": sessions}

    elif op == "profile":
        from session import SessionManager
        mgr = SessionManager()
        sub = request_data.get("action", "list")

        if sub == "create":
            return mgr.create_profile(
                name=request_data["name"],
                domain=request_data["domain"],
                tier=request_data.get("tier", 1),
            )
        elif sub == "load":
            profile = mgr.load_profile(request_data["name"])
            return {"success": profile is not None, "profile": profile}
        elif sub == "list":
            return {"success": True, "profiles": mgr.list_profiles()}
        elif sub == "delete":
            return mgr.delete_profile(request_data["name"])
        else:
            return {"success": False, "error": f"Unknown profile action: {sub}"}

    elif op == "ping":
        return {"success": True, "message": "pong"}

    else:
        return {
            "success": False,
            "error": f"Unknown op: {op}. "
                     "Valid: launch, action, actions, snapshot, screenshot, close, save, status, profile, ping",
        }


def _truncate_result(result: dict, original_bytes: int) -> dict:
    """Truncate oversized result while preserving success/error semantics.

    Instead of replacing the entire result with a new success=True dict,
    truncate the largest string fields and add truncation metadata.
    """
    max_bytes = Config.MAX_SNAPSHOT_BYTES
    truncated_fields = []

    # Identify string fields that can be truncated, sorted by size descending
    trunc_candidates = []
    for key, val in result.items():
        if isinstance(val, str) and key not in ("success", "error"):
            trunc_candidates.append((key, len(val)))
    trunc_candidates.sort(key=lambda x: x[1], reverse=True)

    # Truncate largest fields until output fits
    out = dict(result)
    for key, size in trunc_candidates:
        serialized = json.dumps(out, default=str)
        if len(serialized) <= max_bytes:
            break
        # Estimate how much to cut from this field
        overshoot = len(serialized) - max_bytes
        field_val = out[key]
        new_len = max(0, len(field_val) - overshoot - 200)  # extra margin for metadata
        out[key] = field_val[:new_len] + f"... [truncated from {len(field_val)} chars]"
        truncated_fields.append(key)

    out["truncated"] = True
    out["truncated_fields"] = truncated_fields
    out["original_bytes"] = original_bytes
    return out


async def handle_http(request: web.Request) -> web.Response:
    """HTTP request handler."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, web.HTTPBadRequest) as e:
        return web.json_response(
            {"success": False, "error": f"Invalid JSON: {e}"},
            status=HTTPStatus.BAD_REQUEST,
        )
    except Exception as e:
        # Catches aiohttp ContentTypeError and other unexpected parse errors
        return web.json_response(
            {"success": False, "error": f"Request parse error: {e}"},
            status=HTTPStatus.BAD_REQUEST,
        )

    try:
        result = await handle_request_inner(body)
    except Exception as e:
        result = {"success": False, "error": f"Unhandled error: {e}"}

    # Truncate oversized responses
    output = json.dumps(result, default=str)
    if len(output) > Config.MAX_SNAPSHOT_BYTES:
        result = _truncate_result(result, len(output))
        # Re-check after truncation — nested data (refs, etc.) may keep it over limit
        output = json.dumps(result, default=str)
        if len(output) > Config.MAX_SNAPSHOT_BYTES:
            result = {
                "success": result.get("success", False),
                "error": result.get("error", ""),
                "truncated": True,
                "original_bytes": len(output),
                "message": "Response exceeded size limit even after field truncation. "
                           "Use a more targeted request to reduce output size.",
            }

    return web.json_response(result)


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    import browser_engine
    sessions = await browser_engine.list_sessions()
    return web.json_response({
        "status": "ok",
        "active_sessions": len(sessions),
    })


async def _session_sweeper(app: web.Application) -> None:
    """Periodic background task that reaps idle sessions."""
    import browser_engine

    interval = Config.SESSION_SWEEP_INTERVAL
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                reaped = await browser_engine.sweep_idle_sessions()
                if reaped:
                    print(f"[gc] reaped {len(reaped)} idle session(s): {reaped}",
                          file=sys.stderr)
            except Exception as e:
                print(f"[gc] sweep error: {e}", file=sys.stderr)
    except asyncio.CancelledError:
        pass


async def on_startup(app: web.Application) -> None:
    """Start background tasks on server startup."""
    app["sweeper_task"] = asyncio.create_task(_session_sweeper(app))


async def cleanup(app: web.Application) -> None:
    """Clean up all browser sessions on shutdown."""
    # Stop the sweeper first
    sweeper = app.get("sweeper_task")
    if sweeper:
        sweeper.cancel()
        try:
            await sweeper
        except asyncio.CancelledError:
            pass

    import browser_engine
    sessions = await browser_engine.list_sessions()
    for s in sessions:
        try:
            await browser_engine.close(s["session_id"])
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="browser-use HTTP server")
    parser.add_argument("--port", type=int, default=8500, help="Port (default: 8500)")
    parser.add_argument("--host", default=Config.DEFAULT_HOST,
                        help=f"Host (default: {Config.DEFAULT_HOST})")
    args = parser.parse_args()

    Config.ensure_dirs()

    app = web.Application(middlewares=[auth_middleware])
    app.router.add_post("/", handle_http)
    app.router.add_get("/health", handle_health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(cleanup)

    auth_status = "enabled (token set)" if Config.AUTH_TOKEN else "disabled (no BROWSER_USE_TOKEN)"
    print(f"browser-use server starting on {args.host}:{args.port} [auth: {auth_status}]",
          file=sys.stderr)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
