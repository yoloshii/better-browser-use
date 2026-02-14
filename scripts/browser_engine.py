"""
Multi-tier browser engine with BrowserTier ABC pattern.

Tier 1: Vanilla Playwright Chromium — no stealth, fastest startup.
Tier 2: Patchright — patched Chromium with stealth (no user_agent override).
Tier 3: Camoufox — anti-detect Firefox with fingerprint spoofing + GeoIP.

All tiers implement BrowserTier ABC: detect() → init() → teardown().
"""

from __future__ import annotations

import abc
import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from config import Config, validate_profile_name, safe_profile_path, get_geo_config

import logging
import subprocess

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auto-install helpers
# ---------------------------------------------------------------------------

def _pip_install(*packages: str) -> None:
    """Install Python packages via pip. Raises on failure."""
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", *packages]
    log.info("Auto-installing: %s", " ".join(packages))
    subprocess.check_call(cmd)


def _run_cmd(*args: str) -> None:
    """Run a shell command. Raises on failure."""
    log.info("Running: %s", " ".join(args))
    subprocess.check_call(args)


def _ensure_playwright_chromium() -> None:
    """Install playwright + Chromium browser if missing."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        _pip_install("playwright")
    _run_cmd(sys.executable, "-m", "playwright", "install", "chromium")


def _ensure_patchright() -> None:
    """Install patchright + Chromium browser if missing."""
    try:
        import patchright  # noqa: F401
    except ImportError:
        _pip_install("patchright")
    _run_cmd(sys.executable, "-m", "patchright", "install", "chromium")


def _ensure_camoufox() -> None:
    """Install camoufox[geoip] + playwright + fetch Firefox binary if missing."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        _pip_install("playwright")
    try:
        import camoufox  # noqa: F401
    except ImportError:
        _pip_install("camoufox[geoip]")
    _run_cmd(sys.executable, "-m", "camoufox", "fetch")


# ---------------------------------------------------------------------------
# Tracker/fingerprinter blocking (applied to Tier 2 and 3)
# ---------------------------------------------------------------------------

TRACKER_PATTERNS: list[str] = [
    "**/analytics.js",
    "**/gtag/js*",
    "**/ga.js",
    "**/fingerprint*.js",
    "**/fp.js",
    "**/tracking*.js",
    "**/pixel*.js",
    "**/beacon*.js",
    "**/collect*",
    "**/_vercel/insights/**",
    "**/clarity.js",
    "**/hotjar*.js",
    "**/hj-*.js",
    "**/fullstory*.js",
    "**/mouseflow*.js",
    "**/cdn.segment.com/**",
    "**/cdn.amplitude.com/**",
    "**/cdn.mxpnl.com/**",
    "**/sentry.io/**",
    "**/browser-intake-datadoghq.com/**",
    "**/google-analytics.com/**",
    "**/googletagmanager.com/**",
    "**/connect.facebook.net/**",
    "**/googlesyndication.com/**",
    "**/doubleclick.net/**",
]


async def _block_trackers(context: Any) -> None:
    """Set up route interception to block tracker/analytics/fingerprinter scripts.

    Uses Playwright's context.route() with glob patterns to abort matching requests.
    """
    for pattern in TRACKER_PATTERNS:
        await context.route(pattern, lambda route: route.abort())


# ---------------------------------------------------------------------------
# BrowserTier ABC (browser-ai Provider pattern)
# ---------------------------------------------------------------------------

class BrowserTier(abc.ABC):
    """Abstract base class for browser tiers.

    Each tier implements the lifecycle:
      detect() → can this tier run on this system?
      init()   → launch browser, context, page
      teardown() → clean shutdown
    """

    @property
    @abc.abstractmethod
    def tier_number(self) -> int:
        ...

    @property
    @abc.abstractmethod
    def name(self) -> str:
        ...

    @abc.abstractmethod
    async def detect(self) -> bool:
        """Check if this tier's dependencies are available."""
        ...

    @abc.abstractmethod
    async def init(
        self,
        profile_path: str | None = None,
        viewport: dict | None = None,
        **kwargs: Any,
    ) -> tuple[Any, Any, Any]:
        """Launch browser. Returns (pw_or_handle, browser, context)."""
        ...

    @abc.abstractmethod
    async def teardown(self, handle: Any, browser: Any) -> None:
        """Clean shutdown of browser resources."""
        ...


class Tier1Playwright(BrowserTier):
    """Vanilla Playwright Chromium — no stealth, fastest startup."""

    @property
    def tier_number(self) -> int:
        return 1

    @property
    def name(self) -> str:
        return "playwright"

    async def detect(self) -> bool:
        try:
            import playwright  # noqa: F401
            return True
        except ImportError:
            return False

    async def init(
        self,
        profile_path: str | None = None,
        viewport: dict | None = None,
        **kwargs: Any,
    ) -> tuple[Any, Any, Any]:
        # _ensure_playwright_chromium()  # Skip — already installed, blocks event loop
        from playwright.async_api import async_playwright

        geo = get_geo_config()
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=Config.HEADLESS)

        context_opts: dict[str, Any] = {
            "viewport": viewport or Config.DEFAULT_VIEWPORT,
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "locale": geo["locale"],
            "timezone_id": geo["timezone"],
        }

        if profile_path:
            storage_path = Path(profile_path) / "storage.json"
            if storage_path.exists():
                context_opts["storage_state"] = str(storage_path)

        context = await browser.new_context(**context_opts)
        return pw, browser, context

    async def teardown(self, handle: Any, browser: Any) -> None:
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await handle.stop()
        except Exception:
            pass


class Tier2Patchright(BrowserTier):
    """Patchright — patched Chromium with stealth.

    Key differences from Tier 1:
      - Imports from patchright.async_api (auto-installed if missing)
      - No custom user_agent (Patchright's default Chrome UA is stealthier)
      - Proxy support via Config env vars
    """

    @property
    def tier_number(self) -> int:
        return 2

    @property
    def name(self) -> str:
        return "patchright"

    async def detect(self) -> bool:
        try:
            import patchright  # noqa: F401
            return True
        except ImportError:
            return False

    async def init(
        self,
        profile_path: str | None = None,
        viewport: dict | None = None,
        **kwargs: Any,
    ) -> tuple[Any, Any, Any]:
        # _ensure_patchright()  # Skip — already installed, blocks event loop
        from patchright.async_api import async_playwright

        geo = get_geo_config()
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=Config.HEADLESS)

        context_opts: dict[str, Any] = {
            "viewport": viewport or Config.DEFAULT_VIEWPORT,
            # No custom user_agent — Patchright's default is stealthier
            "locale": geo["locale"],
            "timezone_id": geo["timezone"],
        }

        if Config.PROXY_SERVER:
            proxy: dict[str, str] = {"server": Config.PROXY_SERVER}
            if Config.PROXY_USERNAME:
                proxy["username"] = Config.PROXY_USERNAME
            if Config.PROXY_PASSWORD:
                proxy["password"] = Config.PROXY_PASSWORD
            context_opts["proxy"] = proxy

        if profile_path:
            storage_path = Path(profile_path) / "storage.json"
            if storage_path.exists():
                context_opts["storage_state"] = str(storage_path)

        context = await browser.new_context(**context_opts)

        # Block trackers/fingerprinters on stealth tiers
        await _block_trackers(context)

        return pw, browser, context

    async def teardown(self, handle: Any, browser: Any) -> None:
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await handle.stop()
        except Exception:
            pass


class Tier3Camoufox(BrowserTier):
    """Camoufox — anti-detect Firefox with C++ fingerprint spoofing + GeoIP.

    Uses AsyncNewBrowser with manual Playwright control (not the context
    manager) so the session can persist across multiple requests.

    GeoIP auto-detects timezone/locale from proxy exit IP when a proxy
    is configured.  Humanize adds human-like input delays.
    """

    @property
    def tier_number(self) -> int:
        return 3

    @property
    def name(self) -> str:
        return "camoufox"

    async def detect(self) -> bool:
        try:
            import camoufox  # noqa: F401
            return True
        except ImportError:
            return False

    async def init(
        self,
        profile_path: str | None = None,
        viewport: dict | None = None,
        **kwargs: Any,
    ) -> tuple[Any, Any, Any]:
        import os

        _ensure_camoufox()
        from playwright.async_api import async_playwright
        from camoufox import AsyncNewBrowser

        # Camoufox (Firefox) can fail with X11 errors in WSL/headless — unset DISPLAY
        saved_display = os.environ.pop("DISPLAY", None)

        try:
            pw = await async_playwright().start()

            camoufox_opts: dict[str, Any] = {
                "headless": Config.HEADLESS,
                "humanize": Config.DEFAULT_HUMANIZE or None,
                "geoip": bool(Config.PROXY_SERVER),
            }

            if Config.PROXY_SERVER:
                proxy: dict[str, str] = {"server": Config.PROXY_SERVER}
                if Config.PROXY_USERNAME:
                    proxy["username"] = Config.PROXY_USERNAME
                if Config.PROXY_PASSWORD:
                    proxy["password"] = Config.PROXY_PASSWORD
                camoufox_opts["proxy"] = proxy

            browser = await AsyncNewBrowser(pw, **camoufox_opts)

            geo = get_geo_config()
            context_opts: dict[str, Any] = {
                "locale": geo["locale"],
                "timezone_id": geo["timezone"],
            }
            if viewport:
                context_opts["viewport"] = viewport

            if profile_path:
                storage_path = Path(profile_path) / "storage.json"
                if storage_path.exists():
                    context_opts["storage_state"] = str(storage_path)

            context = await browser.new_context(**context_opts)

            # Block trackers/fingerprinters on stealth tiers
            await _block_trackers(context)

            return pw, browser, context
        finally:
            if saved_display is not None:
                os.environ["DISPLAY"] = saved_display

    async def teardown(self, handle: Any, browser: Any) -> None:
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await handle.stop()
        except Exception:
            pass


# Tier registry
TIERS: dict[int, BrowserTier] = {
    1: Tier1Playwright(),
    2: Tier2Patchright(),
    3: Tier3Camoufox(),
}


# ---------------------------------------------------------------------------
# Session store (in-memory, persisted to /tmp file per session)
# ---------------------------------------------------------------------------

_sessions: dict[str, dict[str, Any]] = {}


def _session_file(session_id: str) -> Path:
    Config.ensure_dirs()
    return Config.SESSION_DIR / f"{session_id}.json"


def _save_session_meta(session_id: str, meta: dict) -> None:
    """Persist minimal session metadata to disk for cross-invocation access."""
    path = _session_file(session_id)
    path.write_text(json.dumps(meta, indent=2))


def _load_session_meta(session_id: str) -> dict | None:
    path = _session_file(session_id)
    if path.exists():
        return json.loads(path.read_text())
    return None


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_session_lock(session_id: str) -> asyncio.Lock | None:
    """Get the asyncio.Lock for a session, or None if session doesn't exist."""
    session = _sessions.get(session_id)
    if session:
        return session["lock"]
    return None


def touch_session(session_id: str) -> None:
    """Update last_activity timestamp for a session."""
    session = _sessions.get(session_id)
    if session:
        session["last_activity"] = time.monotonic()


def get_session_ref_map(session_id: str) -> dict:
    """Get the server-owned ref_map for a session."""
    session = _sessions.get(session_id)
    if session:
        return session.get("ref_map", {})
    return {}


def set_session_ref_map(session_id: str, ref_map: dict) -> None:
    """Update the server-owned ref_map for a session."""
    session = _sessions.get(session_id)
    if session:
        session["ref_map"] = ref_map


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def launch(
    tier: int = 1,
    profile: str | None = None,
    viewport: dict | None = None,
    url: str | None = None,
) -> dict:
    """Launch a new browser session.

    Args:
        tier: Stealth tier (1=Playwright, 2=Patchright, 3=Camoufox).
        profile: Profile name to load (from ~/.browser-use/profiles/<name>/).
        viewport: Override viewport dict.
        url: Navigate to this URL after launch.

    Returns dict: {success, session_id, tier, url, title}
    """
    session_id = uuid.uuid4().hex[:12]

    # Resolve profile path (with traversal protection)
    profile_path = None
    if profile:
        err = validate_profile_name(profile)
        if err:
            return {"success": False, "error": err}
        safe = safe_profile_path(Config.PROFILE_DIR, profile)
        if safe is None:
            return {"success": False, "error": f"Invalid profile path: {profile}"}
        profile_path = str(safe)

    tier_impl = TIERS.get(tier)
    if tier_impl is None:
        return {"success": False, "error": f"Unknown tier: {tier}"}

    try:
        pw, browser, context = await tier_impl.init(
            profile_path=profile_path,
            viewport=viewport,
        )
    except NotImplementedError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"Browser launch failed: {e}"}

    page = await context.new_page()

    _sessions[session_id] = {
        "pw": pw,
        "browser": browser,
        "context": context,
        "page": page,
        "tier": tier,
        "tier_impl": tier_impl,
        "profile": profile,
        "lock": asyncio.Lock(),
        "created_at": time.monotonic(),
        "last_activity": time.monotonic(),
        "action_count": 0,
        "ref_map": {},
        "humanize": Config.HUMANIZE_ACTIONS,  # Disabled auto-humanize for Tier 2 (timeout issues)
        "humanize_intensity": Config.DEFAULT_HUMANIZE,
    }

    _save_session_meta(session_id, {
        "session_id": session_id,
        "tier": tier,
        "profile": profile,
        "pid": browser.process.pid if hasattr(browser, "process") and browser.process else None,
    })

    result = {
        "success": True,
        "session_id": session_id,
        "tier": tier,
    }

    if url:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=Config.DEFAULT_TIMEOUT)
            result["url"] = page.url
            result["title"] = await page.title()
        except Exception as e:
            result["url"] = page.url
            result["title"] = await page.title()
            result["warning"] = f"Navigation issue: {e}"

    return result


async def get_page(session_id: str):
    """Get the active Page for a session.

    Returns None if the session doesn't exist or is closing.
    """
    session = _sessions.get(session_id)
    if session and not session.get("closing"):
        return session["page"]
    return None


async def get_context(session_id: str):
    """Get the BrowserContext for a session.

    Returns None if the session doesn't exist or is closing.
    """
    session = _sessions.get(session_id)
    if session and not session.get("closing"):
        return session["context"]
    return None


async def get_session_info(session_id: str) -> dict | None:
    """Get session info dict."""
    session = _sessions.get(session_id)
    if not session or session.get("closing"):
        return None
    page = session["page"]
    now = time.monotonic()
    return {
        "session_id": session_id,
        "tier": session["tier"],
        "profile": session.get("profile"),
        "url": page.url,
        "title": await page.title(),
        "tab_count": len(session["context"].pages),
        "action_count": session.get("action_count", 0),
        "duration_seconds": round(now - session.get("created_at", now)),
        "humanize": session.get("humanize", False),
        "humanize_intensity": session.get("humanize_intensity", 1.0),
    }


async def switch_page(session_id: str, index: int):
    """Switch active page to tab at index (0-based)."""
    session = _sessions.get(session_id)
    if not session or session.get("closing"):
        return None
    pages = session["context"].pages
    if 0 <= index < len(pages):
        session["page"] = pages[index]
        await pages[index].bring_to_front()
        return session["page"]
    return None


async def new_page(session_id: str, url: str | None = None):
    """Create a new tab in the session."""
    session = _sessions.get(session_id)
    if not session or session.get("closing"):
        return None
    page = await session["context"].new_page()
    session["page"] = page
    if url:
        await page.goto(url, wait_until="domcontentloaded", timeout=Config.DEFAULT_TIMEOUT)
    return page


async def close_page(session_id: str, index: int) -> bool:
    """Close a tab by index.

    When the last tab is closed, a new ``about:blank`` page is opened
    automatically so the session always has a valid active page.
    """
    session = _sessions.get(session_id)
    if not session or session.get("closing"):
        return False
    pages = session["context"].pages
    if 0 <= index < len(pages):
        target = pages[index]
        await target.close()
        remaining = session["context"].pages
        if remaining:
            session["page"] = remaining[-1]
        else:
            # Last tab was closed — open about:blank to keep session valid
            blank = await session["context"].new_page()
            session["page"] = blank
        return True
    return False


async def save_state(session_id: str, profile_name: str | None = None) -> dict:
    """Save browser state (cookies + localStorage) to profile dir."""
    session = _sessions.get(session_id)
    if not session or session.get("closing"):
        return {"success": False, "error": f"Session {session_id} not found"}

    name = profile_name or session.get("profile") or session_id
    err = validate_profile_name(name)
    if err:
        return {"success": False, "error": err}
    profile_dir = safe_profile_path(Config.PROFILE_DIR, name)
    if profile_dir is None:
        return {"success": False, "error": f"Invalid profile path: {name}"}
    profile_dir.mkdir(parents=True, exist_ok=True)

    context = session["context"]
    storage_path = profile_dir / "storage.json"
    state = await context.storage_state()
    storage_path.write_text(json.dumps(state, indent=2))

    return {"success": True, "profile": name, "path": str(storage_path)}


async def close(session_id: str) -> dict:
    """Close a browser session and clean up using tier-specific teardown.

    Resources are closed *before* removing the session entry so that a
    teardown failure doesn't orphan browser processes.  The session is
    marked ``closing=True`` first to prevent new operations from
    starting while teardown is in progress.
    """
    session = _sessions.get(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}

    # Prevent new operations from starting on this session
    session["closing"] = True

    # Teardown resources first — keep session entry until success
    try:
        tier_impl: BrowserTier = session.get("tier_impl")
        if tier_impl:
            await tier_impl.teardown(session["pw"], session["browser"])
        else:
            # Fallback for sessions without tier_impl reference
            try:
                await session["browser"].close()
            except Exception:
                pass
            try:
                await session["pw"].stop()
            except Exception:
                pass
    except Exception as exc:
        # Teardown failed — leave session in registry for retry/GC
        session["closing"] = False
        return {"success": False, "error": f"Teardown failed: {exc}"}

    # Resources released successfully — now remove session entry
    _sessions.pop(session_id, None)

    sf = _session_file(session_id)
    if sf.exists():
        sf.unlink()

    return {"success": True}


async def sweep_idle_sessions() -> list[str]:
    """Close sessions that have been idle longer than SESSION_IDLE_TTL.

    Returns list of session IDs that were reaped.
    Skips sessions that are already closing.
    """
    now = time.monotonic()
    ttl = Config.SESSION_IDLE_TTL
    reaped: list[str] = []

    # Snapshot the keys to avoid mutating dict during iteration
    for sid in list(_sessions.keys()):
        session = _sessions.get(sid)
        if session is None or session.get("closing"):
            continue
        idle = now - session.get("last_activity", now)
        if idle > ttl:
            try:
                await close(sid)
                reaped.append(sid)
            except Exception:
                pass  # close() handles its own error reporting

    return reaped


async def list_sessions() -> list[dict]:
    """List active sessions."""
    result = []
    for sid, session in _sessions.items():
        page = session["page"]
        result.append({
            "session_id": sid,
            "tier": session["tier"],
            "profile": session.get("profile"),
            "url": page.url,
        })
    return result


async def detect_available_tiers() -> list[int]:
    """Probe which tiers are available on the current system."""
    available = []
    for tier_num, impl in sorted(TIERS.items()):
        if await impl.detect():
            available.append(tier_num)
    return available


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    async def main():
        request = json.loads(sys.stdin.read())
        action = request.get("action", "launch")

        if action == "launch":
            result = await launch(
                tier=request.get("tier", 1),
                profile=request.get("profile"),
                viewport=request.get("viewport"),
                url=request.get("url"),
            )
        elif action == "close":
            result = await close(request["session_id"])
        elif action == "save_state":
            result = await save_state(
                request["session_id"],
                request.get("profile"),
            )
        elif action == "list":
            result = {"sessions": await list_sessions()}
        elif action == "detect_tiers":
            tiers = await detect_available_tiers()
            result = {"available_tiers": tiers}
        else:
            result = {"success": False, "error": f"Unknown action: {action}"}

        print(json.dumps(result))

    asyncio.run(main())
