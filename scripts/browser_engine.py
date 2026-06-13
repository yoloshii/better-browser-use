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
from proxy_planner import plan_proxy, proxy_to_url, geo_mismatch_warning, ports_list
from errors import _scrub_credentials

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


# ---------------------------------------------------------------------------
# WebMCP init script — intercepts tool registrations on Chrome 146+ pages
# Chrome 147 removed provideContext()/clearContext() (spec PR #132, issue #101)
# Chrome 148 removed unregisterTool(); use AbortSignal instead (spec PR #147)
# ---------------------------------------------------------------------------

WEBMCP_INIT_SCRIPT = """
(() => {
    // Initialize WebMCP interception layer
    window.__webmcp = { tools: {}, available: false, declarative: {}, _abortControllers: {} };

    if (typeof navigator.modelContext === 'undefined') return;
    if (typeof navigator.modelContext.registerTool !== 'function') return;

    window.__webmcp.available = true;

    // --- Intercept imperative tool registrations ---
    // Chrome 148+: unregisterTool() removed, use AbortSignal instead
    // We track AbortControllers so we can unregister tools from our side

    const origRegister = navigator.modelContext.registerTool.bind(navigator.modelContext);
    navigator.modelContext.registerTool = function(tool, options) {
        window.__webmcp.tools[tool.name] = {
            name: tool.name,
            description: tool.description || '',
            inputSchema: tool.inputSchema || {},
            annotations: tool.annotations || {},
            readOnlyHint: !!(tool.annotations && tool.annotations.readOnlyHint),
            _hasExecute: typeof tool.execute === 'function',
            _ref: tool,  // keep live reference for execute()
        };
        // Track the AbortController if the page provided a signal
        if (options && options.signal) {
            options.signal.addEventListener('abort', () => {
                delete window.__webmcp.tools[tool.name];
                delete window.__webmcp._abortControllers[tool.name];
            });
        }
        return origRegister(tool, options);
    };

    // Chrome 147 and below: unregisterTool exists — proxy it
    // Chrome 148+: unregisterTool removed — skip gracefully
    if (typeof navigator.modelContext.unregisterTool === 'function') {
        const origUnregister = navigator.modelContext.unregisterTool.bind(navigator.modelContext);
        navigator.modelContext.unregisterTool = function(name) {
            delete window.__webmcp.tools[name];
            delete window.__webmcp._abortControllers[name];
            return origUnregister(name);
        };
    }

    // --- Scan declarative tools (forms with toolname attribute) ---
    // Runs after DOM is ready; re-scanned on webmcp_discover action.
    const scanDeclarativeForms = () => {
        window.__webmcp.declarative = {};
        document.querySelectorAll('form[toolname]').forEach(form => {
            const name = form.getAttribute('toolname');
            const desc = form.getAttribute('tooldescription') || '';
            const autoSubmit = form.hasAttribute('toolautosubmit');
            const schema = { type: 'object', properties: {}, required: [] };

            form.querySelectorAll('input, select, textarea').forEach(el => {
                if (el.type === 'submit' || el.type === 'hidden') return;
                const paramName = el.getAttribute('toolparamtitle') || el.name;
                if (!paramName) return;

                const paramDesc = el.getAttribute('toolparamdescription')
                    || el.labels?.[0]?.textContent?.trim()
                    || el.getAttribute('aria-description') || '';

                let prop = { description: paramDesc };

                if (el.tagName === 'SELECT') {
                    prop.type = 'string';
                    prop.enum = [];
                    prop.oneOf = [];
                    el.querySelectorAll('option').forEach(opt => {
                        if (opt.value) {
                            prop.enum.push(opt.value);
                            prop.oneOf.push({ const: opt.value, title: opt.textContent.trim() });
                        }
                    });
                } else if (el.type === 'checkbox') {
                    prop.type = 'boolean';
                } else if (el.type === 'number' || el.type === 'range') {
                    prop.type = 'number';
                } else if (el.type === 'radio') {
                    // Radio groups share a name — collect all values
                    if (!schema.properties[paramName]) {
                        prop.type = 'string';
                        prop.enum = [];
                    } else {
                        prop = schema.properties[paramName];
                    }
                    if (el.value && !prop.enum.includes(el.value)) {
                        prop.enum.push(el.value);
                    }
                } else {
                    prop.type = 'string';
                }

                schema.properties[paramName] = prop;
                if (el.required && !schema.required.includes(paramName)) {
                    schema.required.push(paramName);
                }
            });

            window.__webmcp.declarative[name] = {
                name: name,
                description: desc,
                inputSchema: schema,
                autoSubmit: autoSubmit,
                _formSelector: form.id ? '#' + CSS.escape(form.id)
                    : 'form[toolname="' + CSS.escape(name) + '"]',
                _type: 'declarative',
            };
        });
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', scanDeclarativeForms);
    } else {
        scanDeclarativeForms();
    }

    // Expose rescan for webmcp_discover
    window.__webmcp.rescanDeclarative = scanDeclarativeForms;

    // --- Expose execute helper ---
    // Spec: ToolExecuteCallback = (input, client) => Promise
    // client.requestUserInteraction(callback) enables human-in-the-loop
    const mockClient = {
        requestUserInteraction: async (cb) => {
            // In agent context, auto-approve (no human in the loop)
            return typeof cb === 'function' ? await cb() : undefined;
        },
    };

    window.__webmcp.executeTool = async (name, args) => {
        // Try imperative first
        const imp = window.__webmcp.tools[name];
        if (imp && imp._ref && typeof imp._ref.execute === 'function') {
            return await imp._ref.execute(args, mockClient);
        }
        // Try declarative (form fill + submit)
        const decl = window.__webmcp.declarative[name];
        if (decl) {
            const form = document.querySelector(decl._formSelector);
            if (!form) return { error: 'Form not found for declarative tool: ' + name };
            // Fill fields
            for (const [key, value] of Object.entries(args || {})) {
                const el = form.querySelector('[name="' + CSS.escape(key) + '"]')
                    || form.querySelector('[toolparamtitle="' + CSS.escape(key) + '"]');
                if (!el) continue;
                if (el.tagName === 'SELECT') {
                    el.value = value;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                } else if (el.type === 'checkbox') {
                    el.checked = !!value;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                } else if (el.type === 'radio') {
                    const radio = form.querySelector(
                        'input[name="' + CSS.escape(key) + '"][value="' + CSS.escape(String(value)) + '"]'
                    );
                    if (radio) { radio.checked = true; radio.dispatchEvent(new Event('change', { bubbles: true })); }
                } else {
                    // Set value via native setter to trigger React/framework state updates
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    )?.set || Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    )?.set;
                    if (nativeSetter) nativeSetter.call(el, String(value));
                    else el.value = String(value);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }
            // Submit
            if (decl.autoSubmit || true) {  // always submit for agent calls
                const submitBtn = form.querySelector('[type="submit"]') || form.querySelector('button:not([type])');
                if (submitBtn) submitBtn.click();
                else form.requestSubmit();
            }
            return { content: [{ type: 'text', text: 'Form submitted for tool: ' + name }] };
        }
        return { error: 'Tool not found: ' + name };
    };
})();
"""


async def _inject_webmcp_script(context: Any) -> None:
    """Inject the WebMCP interceptor init script into a browser context."""
    await context.add_init_script(WEBMCP_INIT_SCRIPT)


def _build_chrome_launch_opts() -> dict[str, Any]:
    """Build launch options for Chrome with WebMCP flag when enabled."""
    opts: dict[str, Any] = {}

    if Config.WEBMCP_ENABLED == "0":
        return opts

    if Config.CHROME_EXECUTABLE:
        opts["executable_path"] = Config.CHROME_EXECUTABLE
    elif Config.CHROME_CHANNEL:
        opts["channel"] = Config.CHROME_CHANNEL

    # Add WebMCP feature flag
    if Config.CHROME_EXECUTABLE or Config.CHROME_CHANNEL or Config.WEBMCP_ENABLED == "1":
        opts.setdefault("args", []).append("--enable-features=WebMCPTesting")

    return opts


async def _block_trackers(context: Any) -> None:
    """Set up route interception to block tracker/analytics/fingerprinter scripts.

    Uses Playwright's context.route() with glob patterns to abort matching requests.
    """
    for pattern in TRACKER_PATTERNS:
        await context.route(pattern, lambda route: route.abort())


# ---------------------------------------------------------------------------
# GeoIP resolution (auto-detect from proxy or fall back to static config)
# ---------------------------------------------------------------------------

def _authenticated_proxy_url() -> str | None:
    """Full proxy URL with inline URL-encoded credentials, or None when no proxy.

    CloakBrowser's geoip helpers need a URL string (not a Playwright dict), and the
    library's own dict path (_extract_proxy_url) drops username/password for non-SOCKS
    HTTP proxies — so we build the authenticated URL ourselves. Derives from the active
    proxy strategy (static | port_pool | backconnect) via the planner.
    """
    return proxy_to_url(plan_proxy(Config))


def _planned_proxy(session_id: str | None = None) -> dict | None:
    """Build the launch proxy dict via the strategy planner, warning on misconfig + geo mismatch.

    Single emission point for proxy launch warnings so each tier surfaces them exactly once:
      - a non-static strategy that yields no proxy (silent direct launch = real-IP exposure),
      - a port_pool with multiple ports before live rotation lands (only the first is used),
      - a PROXY_COUNTRY vs BROWSER_USE_GEO mismatch.
    All advisory — they never block a launch (operator owns scope).
    """
    proxy = plan_proxy(Config, session_id=session_id)
    strategy = str(getattr(Config, "PROXY_STRATEGY", "static") or "static").lower()
    if proxy is None and strategy != "static":
        log.warning(
            "PROXY_STRATEGY=%r set but no proxy could be built from PROXY_* config — launching "
            "WITHOUT a proxy (direct connection; the real IP is exposed). Verify the proxy settings.",
            strategy,
        )
    if strategy == "port_pool" and len(ports_list(Config.PROXY_PORTS)) > 1:
        log.warning(
            "port_pool has multiple ports configured but per-launch rotation is not yet active — "
            "using the first port. Rotation lands with the proxy retry/rotation step."
        )
    warning = geo_mismatch_warning(Config)
    if warning:
        log.warning(warning)
    return proxy


async def _resolve_geo() -> dict[str, Any]:
    """Resolve geo config: static env override > CloakBrowser GeoIP > default.

    Returns {"timezone", "locale", "exit_ip"}. exit_ip is the proxy's exit IP
    (for WebRTC-IP spoofing via --fingerprint-webrtc-ip) or None when no proxy.

    exit_ip resolution is DECOUPLED from tz/locale GeoIP. The exit IP needs only an
    IP-echo call (no GeoLite2 DB / no `cloakbrowser[geoip]`), so it is resolved whenever
    a proxy is active — independent of GeoIP DB availability and of CLOAKBROWSER_GEOIP.
    `CLOAKBROWSER_GEOIP=0` disables tz/locale GeoIP ONLY; it must NOT re-open a WebRTC
    host-IP leak on proxied sessions. tz/locale GeoIP (DB-dependent) runs only when enabled.
    """
    geo_tz = geo_locale = exit_ip = None
    proxy_url = _authenticated_proxy_url()

    if proxy_url:
        # Exit IP for WebRTC — DB-independent IP echo; resolve whenever a proxy is active.
        try:
            from cloakbrowser.geoip import resolve_proxy_exit_ip
            exit_ip = await asyncio.to_thread(resolve_proxy_exit_ip, proxy_url)
        except Exception as exc:
            log.debug("Proxy exit-IP resolution failed (WebRTC-IP not spoofed): %s", _scrub_credentials(str(exc)))

        # tz/locale GeoIP — DB-dependent (raises without cloakbrowser[geoip]); only when enabled.
        if Config.CLOAKBROWSER_GEOIP != "0":
            try:
                from cloakbrowser.geoip import resolve_proxy_geo_with_ip
                geo_tz, geo_locale, ip2 = await asyncio.to_thread(
                    resolve_proxy_geo_with_ip, proxy_url
                )
                if ip2 and not exit_ip:          # reuse DB-path IP if the echo call missed
                    exit_ip = ip2
                if geo_tz and geo_locale:
                    log.info("GeoIP auto-detected: tz=%s locale=%s exit_ip=%s",
                             geo_tz, geo_locale, exit_ip or "n/a")
            except (ImportError, Exception) as exc:
                log.debug("tz/locale GeoIP failed (falling back to static): %s", _scrub_credentials(str(exc)))

    # A proxied session with no exit IP means WebRTC-IP is NOT spoofed → the real host IP
    # can leak via WebRTC. Surface this LOUDLY instead of silently downgrading stealth.
    # (Most common cause: a SOCKS5 proxy without socksio — the exit-IP probe raises
    # httpx.UnsupportedProtocol. HTTP/HTTPS proxies do not need it.)
    if proxy_url and exit_ip is None:
        is_socks = proxy_url.lower().startswith(("socks5://", "socks5h://", "socks4://"))
        hint = ("SOCKS5 exit-IP resolution needs socksio — install `cloakbrowser[geoip]`."
                if is_socks else "Check proxy reachability and the GeoIP timeout.")
        log.warning(
            "Proxy active but exit IP unresolved — WebRTC-IP NOT spoofed; the real host IP "
            "can leak via WebRTC. %s", hint
        )

    # tz/locale precedence: static override > proxy GeoIP > default.
    if Config.GEO:
        base = get_geo_config()
    elif geo_tz and geo_locale:
        base = {"timezone": geo_tz, "locale": geo_locale}
    else:
        base = get_geo_config()

    return {"timezone": base["timezone"], "locale": base["locale"], "exit_ip": exit_ip}


# ---------------------------------------------------------------------------
# CloakBrowser prewarm (called at server startup)
# ---------------------------------------------------------------------------

_cloakbrowser_status: dict[str, Any] = {"available": False, "version": None, "error": None}


async def prewarm_cloakbrowser() -> dict[str, Any]:
    """Pre-download CloakBrowser binary at server startup (non-blocking).

    Returns status dict for /health endpoint.
    Catches SystemExit from unsupported platform checks.
    """
    global _cloakbrowser_status

    if Config.CLOAKBROWSER_ENABLED == "0":
        _cloakbrowser_status = {"available": False, "version": None, "error": "disabled"}
        return _cloakbrowser_status

    try:
        from cloakbrowser import ensure_binary, binary_info

        # Disable auto-update in production unless explicitly enabled
        import os
        if not Config.CLOAKBROWSER_AUTO_UPDATE:
            os.environ.setdefault("CLOAKBROWSER_AUTO_UPDATE", "false")

        await asyncio.to_thread(ensure_binary)
        info = binary_info()
        _cloakbrowser_status = {
            "available": True,
            "version": info.get("version", "unknown"),
            "binary_path": info.get("binary_path", ""),
        }
        log.info("CloakBrowser prewarmed: v%s", _cloakbrowser_status["version"])
    except (ImportError, SystemExit, Exception) as exc:
        _cloakbrowser_status = {"available": False, "version": None, "error": str(exc)}
        log.info("CloakBrowser not available (using Patchright fallback): %s", exc)

    return _cloakbrowser_status


def get_cloakbrowser_status() -> dict[str, Any]:
    """Get CloakBrowser availability status (for /health endpoint)."""
    return _cloakbrowser_status


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
        await asyncio.to_thread(_ensure_playwright_chromium)
        from playwright.async_api import async_playwright

        geo = get_geo_config()
        pw = await async_playwright().start()

        launch_opts: dict[str, Any] = {"headless": Config.HEADLESS}
        launch_opts.update(_build_chrome_launch_opts())
        browser = await pw.chromium.launch(**launch_opts)

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

        # Inject WebMCP interceptor when enabled
        if Config.WEBMCP_ENABLED != "0":
            await _inject_webmcp_script(context)

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
        await asyncio.to_thread(_ensure_patchright)
        from patchright.async_api import async_playwright

        geo = get_geo_config()
        pw = await async_playwright().start()

        launch_opts: dict[str, Any] = {"headless": Config.HEADLESS}
        launch_opts.update(_build_chrome_launch_opts())
        # Chrome 143+ needs --no-sandbox for DNS resolution in WSL2/containers
        launch_opts.setdefault("args", []).extend(["--no-sandbox", "--disable-setuid-sandbox"])
        browser = await pw.chromium.launch(**launch_opts)

        context_opts: dict[str, Any] = {
            "viewport": viewport or Config.DEFAULT_VIEWPORT,
            # No custom user_agent — Patchright's default is stealthier
            "locale": geo["locale"],
            "timezone_id": geo["timezone"],
        }

        proxy = _planned_proxy()
        if proxy:
            context_opts["proxy"] = proxy

        if profile_path:
            storage_path = Path(profile_path) / "storage.json"
            if storage_path.exists():
                context_opts["storage_state"] = str(storage_path)

        context = await browser.new_context(**context_opts)

        # Block trackers/fingerprinters on stealth tiers
        await _block_trackers(context)

        # Skip add_init_script for Patchright — Chrome 143+ bug: add_init_script
        # breaks DNS resolution (ERR_NAME_NOT_RESOLVED on all navigations).
        # WebMCP requires Chrome 146+ anyway, so Patchright sessions won't have it.

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


class Tier2CloakBrowser(BrowserTier):
    """CloakBrowser — C++ patched Chromium with binary-level stealth.

    26 source-level patches: canvas/WebGL/audio fingerprinting, TLS matching
    (ja3n/ja4), navigator hardening, CDP removal.  0.9 reCAPTCHA v3 scores.

    Falls back to Patchright if CloakBrowser is not installed.
    Uses vanilla Playwright to drive CloakBrowser's binary (not the wrapper's
    own launcher) so we keep full control of the lifecycle.
    """

    @property
    def tier_number(self) -> int:
        return 2

    @property
    def name(self) -> str:
        return "cloakbrowser"

    async def detect(self) -> bool:
        if Config.CLOAKBROWSER_ENABLED == "0":
            return False
        try:
            from cloakbrowser import binary_info
            info = binary_info()
            return info.get("installed", False)
        except (ImportError, SystemExit):
            return False

    async def init(
        self,
        profile_path: str | None = None,
        viewport: dict | None = None,
        **kwargs: Any,
    ) -> tuple[Any, Any, Any]:
        # Adopt CloakBrowser's own launch helper rather than hand-rolling
        # pw.chromium.launch(executable_path=...). The helper owns the stealth launch:
        #   - applies cloakbrowser.config.IGNORE_DEFAULT_ARGS (suppresses BOTH
        #     --enable-automation AND --enable-unsafe-swiftshader; hand-rolling dropped the
        #     latter, leaking a SwiftShader software-WebGL fingerprint that contradicts the
        #     binary's GPU spoof), and
        #   - wires the binary geoip/WebRTC path: resolves the proxy exit IP and injects
        #     --fingerprint-webrtc-ip, and sets timezone/locale as binary flags
        #     (--fingerprint-timezone / --lang) instead of weaker CDP context emulation.
        from cloakbrowser import ensure_binary, launch_context_async

        # Pre-ensure the binary OFF the event loop. launch_context_async() calls the sync
        # ensure_binary() internally; doing it here via to_thread guarantees a ~200MB cold
        # download cannot block the loop. Idempotent — returns the cached path on a warm cache.
        await asyncio.to_thread(ensure_binary)

        # Resolve geo with our precedence: static override > GeoIP(proxy) > default.
        geo = await _resolve_geo()

        # Proxy dict via the strategy planner (static | port_pool | backconnect).
        proxy = _planned_proxy()

        # new_context() kwargs forwarded through the helper (storage_state, etc.).
        context_kwargs: dict[str, Any] = {}
        if profile_path:
            storage_path = Path(profile_path) / "storage.json"
            if storage_path.exists():
                context_kwargs["storage_state"] = str(storage_path)

        # WebRTC-IP spoofing: we resolve the proxy exit IP ourselves in _resolve_geo() using
        # AUTHENTICATED credentials and pass --fingerprint-webrtc-ip explicitly. We do NOT use
        # the helper's geoip=True path: its dict-proxy extraction drops HTTP-proxy auth
        # (_extract_proxy_url), so it would probe unauthenticated and never set the flag. With
        # geoip=False the helper does no second (unauthenticated) lookup.
        args = ["--no-sandbox", "--disable-setuid-sandbox"]  # WSL2/containers; deduped vs stealth defaults
        if geo.get("exit_ip"):
            args.append(f"--fingerprint-webrtc-ip={geo['exit_ip']}")

        context = await launch_context_async(
            headless=Config.HEADLESS,
            proxy=proxy,
            args=args,
            viewport=viewport or Config.CLOAK_VIEWPORT,
            # No custom user_agent — CloakBrowser's binary handles UA via seed.
            locale=geo["locale"],
            timezone=geo["timezone"],
            geoip=False,
            **context_kwargs,
        )
        browser = context.browser

        # Block trackers/fingerprinters on stealth tiers.
        await _block_trackers(context)

        # handle is None: launch_context_async() owns the Playwright instance and patches
        # browser.close() to stop it (see teardown).
        return None, browser, context

    async def teardown(self, handle: Any, browser: Any) -> None:
        # launch_context_async() patches browser.close() to also stop the underlying
        # Playwright instance, so closing the browser is the complete teardown.
        # handle is None for this tier (the helper hides the pw handle).
        try:
            await browser.close()
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

        await asyncio.to_thread(_ensure_camoufox)
        from playwright.async_api import async_playwright
        from camoufox import AsyncNewBrowser

        # Camoufox (Firefox) can fail with X11 errors in WSL/headless — unset DISPLAY
        saved_display = os.environ.pop("DISPLAY", None)

        try:
            pw = await async_playwright().start()

            proxy = _planned_proxy()
            camoufox_opts: dict[str, Any] = {
                "headless": Config.HEADLESS,
                "humanize": Config.DEFAULT_HUMANIZE or None,
                "geoip": bool(proxy),
            }
            if proxy:
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


# Tier registry — Tier 2 uses CloakBrowser if available, Patchright fallback
def _select_tier2() -> BrowserTier:
    """Pick the best Tier 2 implementation at import time.

    CloakBrowser (C++ patched Chromium) is preferred when:
    - CLOAKBROWSER_ENABLED != "0"
    - The cloakbrowser package is importable

    Falls back to Patchright silently.  Actual binary availability is
    checked at launch time (detect() + prewarm), not here.
    """
    if Config.CLOAKBROWSER_ENABLED == "0":
        return Tier2Patchright()
    try:
        import cloakbrowser  # noqa: F401
        return Tier2CloakBrowser()
    except ImportError:
        return Tier2Patchright()


TIERS: dict[int, BrowserTier] = {
    1: Tier1Playwright(),
    2: _select_tier2(),
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
# Auto popup dismissal (ported from browser-use PopupsWatchdog)
# ---------------------------------------------------------------------------

def _setup_popup_handler(context, session_data: dict) -> None:
    """Register automatic popup/dialog dismissal on a browser context.

    Behavior (matching upstream browser-use):
    - alert/confirm/beforeunload: accept (OK)
    - prompt: dismiss (Cancel — can't provide input)
    """
    dismissed: list[dict] = []
    session_data["dismissed_popups"] = dismissed

    async def _on_dialog(dialog):
        dtype = dialog.type
        message = dialog.message
        should_accept = dtype in ("alert", "confirm", "beforeunload")
        dismissed.append({
            "type": dtype,
            "message": message[:200],
            "action": "accepted" if should_accept else "dismissed",
        })
        try:
            if should_accept:
                await dialog.accept()
            else:
                await dialog.dismiss()
        except Exception:
            pass

    context.on("dialog", _on_dialog)


# ---------------------------------------------------------------------------
# Download handling
# ---------------------------------------------------------------------------

def _setup_download_handler(context, session_data: dict, session_id: str) -> None:
    """Register automatic download handling for all pages in a context.

    Downloads are auto-saved to a session-scoped temp directory.
    File metadata is tracked in session_data["downloads"].
    """
    import os
    import tempfile

    download_dir = os.path.join(tempfile.gettempdir(), "browser-use-downloads", session_id)
    os.makedirs(download_dir, exist_ok=True)
    session_data["download_dir"] = download_dir
    downloads_list: list[dict] = []
    session_data["downloads"] = downloads_list

    async def _on_download(download):
        try:
            filename = download.suggested_filename
            save_path = os.path.join(download_dir, filename)
            await download.save_as(save_path)
            size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
            downloads_list.append({
                "filename": filename,
                "path": save_path,
                "url": download.url,
                "size": size,
            })
            log.info(f"Download saved: {filename} ({size} bytes)")
        except Exception as exc:
            log.warning(f"Download save failed: {exc}")

    # Register on all current and future pages
    for p in context.pages:
        p.on("download", _on_download)
    context.on("page", lambda new_page: new_page.on("download", _on_download))


# ---------------------------------------------------------------------------
# Console log capture
# ---------------------------------------------------------------------------

def _setup_console_handler(context, session_data: dict) -> None:
    """Capture JS console messages for later retrieval via the console action."""
    console_logs: list[dict] = []
    session_data["console_logs"] = console_logs

    def _on_console(msg):
        console_logs.append({
            "type": msg.type,
            "text": msg.text[:2000],
        })
        # Keep only last 500 messages to prevent memory bloat
        if len(console_logs) > 500:
            del console_logs[:100]

    # Register on all current and future pages
    for p in context.pages:
        p.on("console", _on_console)
    context.on("page", lambda new_page: new_page.on("console", _on_console))


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
        tier: Stealth tier (1=Playwright, 2=CloakBrowser/Patchright, 3=Camoufox).
        profile: Profile name to load (from ~/.browser-use/profiles/<name>/).
        viewport: Override viewport dict.
        url: Navigate to this URL after launch.

    Returns dict: {success, session_id, tier, tier_engine, url, title}
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
        return {"success": False, "error": _scrub_credentials(str(e))}
    except Exception as e:
        return {"success": False, "error": _scrub_credentials(f"Browser launch failed: {e}")}

    page = await context.new_page()

    # Set up auto popup dismissal and download handling
    _event_data: dict = {}
    _setup_popup_handler(context, _event_data)
    _setup_download_handler(context, _event_data, session_id)
    _setup_console_handler(context, _event_data)

    from models import ActionLoopDetector

    _sessions[session_id] = {
        "pw": pw,
        "browser": browser,
        "context": context,
        "page": page,
        "tier": tier,
        "tier_name": tier_impl.name,
        "tier_impl": tier_impl,
        "profile": profile,
        "lock": asyncio.Lock(),
        "created_at": time.monotonic(),
        "last_activity": time.monotonic(),
        "action_count": 0,
        "ref_map": {},
        "humanize": Config.HUMANIZE_ACTIONS,  # Disabled auto-humanize for Tier 2 (timeout issues)
        "humanize_intensity": Config.DEFAULT_HUMANIZE,
        "webmcp_available": None,  # None=unknown, True/False after probe
        "webmcp_tools": {},        # tool name -> {name, description, inputSchema, type}
        "dismissed_popups": _event_data.get("dismissed_popups", []),
        "downloads": _event_data.get("downloads", []),
        "download_dir": _event_data.get("download_dir"),
        "console_logs": _event_data.get("console_logs", []),
        "loop_detector": ActionLoopDetector(),
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
        "tier_engine": tier_impl.name,
    }

    if url:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=Config.DEFAULT_TIMEOUT)
        except Exception as e:
            result["warning"] = f"Navigation issue: {e}"
        try:
            result["url"] = page.url
            result["title"] = await page.title()
        except Exception:
            result["url"] = getattr(page, "url", url)
            result["title"] = ""

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
        "tier_engine": session.get("tier_name", ""),
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

    # Resources released successfully — now clean up auxiliary state
    from snapshot import clear_previous_snapshots
    clear_previous_snapshots(session_id)

    # Clean download temp dir
    download_dir = session.get("download_dir")
    if download_dir:
        import shutil
        shutil.rmtree(download_dir, ignore_errors=True)

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
