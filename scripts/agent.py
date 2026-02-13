#!/usr/bin/env python3
"""
Main entry point for browser-use skill.

Single script invoked via SSH. Reads JSON from stdin, executes the
requested operation, returns JSON on stdout.

Operations:
  launch    — Start a new browser session
  action    — Execute a browser action (click, fill, navigate, etc.)
  snapshot  — Take an ARIA snapshot of the current page
  screenshot — Take a screenshot (base64 PNG)
  close     — Close a browser session
  save      — Save session state to profile
  profile   — Manage identity profiles (create/load/list/delete)
  status    — Get session info
"""

from __future__ import annotations

import asyncio
import json
import sys
import os

# Ensure scripts/ is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config


async def handle_request(request: dict) -> dict:
    """Route a request to the appropriate handler."""
    op = request.get("op", "")

    # ------------------------------------------------------------------
    # launch: Start a new browser session
    # ------------------------------------------------------------------
    if op == "launch":
        import browser_engine
        return await browser_engine.launch(
            tier=request.get("tier", 1),
            profile=request.get("profile"),
            viewport=request.get("viewport"),
            url=request.get("url"),
        )

    # ------------------------------------------------------------------
    # action: Execute a browser action
    # ------------------------------------------------------------------
    elif op == "action":
        import browser_engine
        import actions

        session_id = request.get("session_id")
        if not session_id:
            return {"success": False, "error": "Missing session_id"}

        page = await browser_engine.get_page(session_id)
        if page is None:
            return {"success": False, "error": f"Session {session_id} not found or expired"}

        action_name = request.get("action")
        params = request.get("params", {})

        # Build session context with ref_map (if provided)
        session_ctx = {
            "session_id": session_id,
            "ref_map": request.get("ref_map", {}),
        }

        result = await actions.execute_action(page, action_name, params, session_ctx)

        # If snapshot was taken, include updated ref_map
        if action_name == "snapshot" and "ref_map" in session_ctx:
            result["refs"] = session_ctx["ref_map"]

        return result

    # ------------------------------------------------------------------
    # snapshot: Convenience shortcut for action=snapshot
    # ------------------------------------------------------------------
    elif op == "snapshot":
        import browser_engine
        from snapshot import take_snapshot

        session_id = request.get("session_id")
        if not session_id:
            return {"success": False, "error": "Missing session_id"}

        page = await browser_engine.get_page(session_id)
        if page is None:
            return {"success": False, "error": f"Session {session_id} not found or expired"}

        return await take_snapshot(
            page,
            compact=request.get("compact", True),
            max_depth=request.get("max_depth", 10),
            cursor_interactive=request.get("cursor_interactive", True),
        )

    # ------------------------------------------------------------------
    # screenshot: Convenience shortcut for action=screenshot
    # ------------------------------------------------------------------
    elif op == "screenshot":
        import browser_engine
        import base64

        session_id = request.get("session_id")
        if not session_id:
            return {"success": False, "error": "Missing session_id"}

        page = await browser_engine.get_page(session_id)
        if page is None:
            return {"success": False, "error": f"Session {session_id} not found or expired"}

        try:
            data = await page.screenshot(
                full_page=request.get("full_page", False),
                type="png",
            )
            return {
                "success": True,
                "screenshot": base64.b64encode(data).decode("ascii"),
                "size": len(data),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # close: Close a browser session
    # ------------------------------------------------------------------
    elif op == "close":
        import browser_engine

        session_id = request.get("session_id")
        if not session_id:
            return {"success": False, "error": "Missing session_id"}

        # Optionally save state before closing
        if request.get("save_profile"):
            await browser_engine.save_state(session_id, request.get("save_profile"))

        return await browser_engine.close(session_id)

    # ------------------------------------------------------------------
    # save: Save session state to profile
    # ------------------------------------------------------------------
    elif op == "save":
        import browser_engine

        session_id = request.get("session_id")
        if not session_id:
            return {"success": False, "error": "Missing session_id"}

        return await browser_engine.save_state(
            session_id,
            request.get("profile"),
        )

    # ------------------------------------------------------------------
    # status: Get session info
    # ------------------------------------------------------------------
    elif op == "status":
        import browser_engine

        session_id = request.get("session_id")
        if session_id:
            info = await browser_engine.get_session_info(session_id)
            if info is None:
                return {"success": False, "error": f"Session {session_id} not found"}
            return {"success": True, **info}
        else:
            sessions = await browser_engine.list_sessions()
            return {"success": True, "sessions": sessions}

    # ------------------------------------------------------------------
    # profile: Manage identity profiles
    # ------------------------------------------------------------------
    elif op == "profile":
        from session import SessionManager
        mgr = SessionManager()
        sub = request.get("action", "list")

        if sub == "create":
            return mgr.create_profile(
                name=request["name"],
                domain=request["domain"],
                tier=request.get("tier", 1),
            )
        elif sub == "load":
            profile = mgr.load_profile(request["name"])
            return {"success": profile is not None, "profile": profile}
        elif sub == "list":
            return {"success": True, "profiles": mgr.list_profiles()}
        elif sub == "delete":
            return mgr.delete_profile(request["name"])
        else:
            return {"success": False, "error": f"Unknown profile action: {sub}"}

    # ------------------------------------------------------------------
    # Unknown operation
    # ------------------------------------------------------------------
    else:
        return {
            "success": False,
            "error": f"Unknown op: {op}. "
                     "Valid: launch, action, snapshot, screenshot, close, save, status, profile",
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


async def main():
    """Read JSON from stdin, process, write JSON to stdout."""
    Config.ensure_dirs()

    raw = sys.stdin.read()
    if not raw.strip():
        print(json.dumps({"success": False, "error": "Empty input"}))
        return

    try:
        request = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"success": False, "error": f"Invalid JSON: {e}"}))
        return

    try:
        result = await handle_request(request)
    except Exception as e:
        result = {
            "success": False,
            "error": f"Unhandled error: {e}",
        }

    # Serialize and output
    output = json.dumps(result, default=str)

    # Hard cap output size to prevent context corruption
    if len(output) > Config.MAX_SNAPSHOT_BYTES:
        result = _truncate_result(result, len(output))
        output = json.dumps(result, default=str)
        # Re-check — nested data (refs, etc.) may keep it over limit
        if len(output) > Config.MAX_SNAPSHOT_BYTES:
            result = {
                "success": result.get("success", False),
                "error": result.get("error", ""),
                "truncated": True,
                "original_bytes": len(output),
                "message": "Response exceeded size limit even after field truncation. "
                           "Use a more targeted request to reduce output size.",
            }
            output = json.dumps(result, default=str)

    print(output)


if __name__ == "__main__":
    asyncio.run(main())
