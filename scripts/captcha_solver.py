"""CAPTCHA solving integration for browser-use.

Tiered approach:
  1. CapSolver API (fast, AI-based, 1-10s)
  2. 2Captcha API (human fallback, 10-30s, broadest coverage)

Extracts sitekey from the page, calls solver API, injects token back.
Supports: reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any, Optional

from config import Config


# ---------------------------------------------------------------------------
# Sitekey extraction (runs in browser context)
# ---------------------------------------------------------------------------

EXTRACT_SITEKEY_JS = """(() => {
    const result = {type: null, sitekey: null, action: null};

    // reCAPTCHA v2/v3
    const recap = document.querySelector('[data-sitekey]');
    if (recap) {
        result.sitekey = recap.getAttribute('data-sitekey');
        result.type = recap.classList.contains('g-recaptcha') ? 'recaptcha_v2' : 'recaptcha';
        const action = recap.getAttribute('data-action');
        if (action) { result.action = action; result.type = 'recaptcha_v3'; }
        return result;
    }

    // reCAPTCHA v2 iframe
    const recapIframe = document.querySelector('iframe[src*="recaptcha"]');
    if (recapIframe) {
        const m = recapIframe.src.match(/[?&]k=([^&]+)/);
        if (m) { result.sitekey = m[1]; result.type = 'recaptcha_v2'; return result; }
    }

    // hCaptcha
    const hcap = document.querySelector('[data-sitekey]');
    if (hcap && (hcap.classList.contains('h-captcha') || document.querySelector('iframe[src*="hcaptcha"]'))) {
        result.sitekey = hcap.getAttribute('data-sitekey');
        result.type = 'hcaptcha';
        return result;
    }
    const hcapIframe = document.querySelector('iframe[src*="hcaptcha"]');
    if (hcapIframe) {
        const m = hcapIframe.src.match(/sitekey=([^&]+)/);
        if (m) { result.sitekey = m[1]; result.type = 'hcaptcha'; return result; }
    }

    // Cloudflare Turnstile
    const turnstile = document.querySelector('[data-sitekey].cf-turnstile') ||
                      document.querySelector('.cf-turnstile[data-sitekey]') ||
                      document.querySelector('div[data-sitekey]');
    if (turnstile && (document.querySelector('script[src*="turnstile"]') ||
                      document.querySelector('iframe[src*="challenges.cloudflare.com"]'))) {
        result.sitekey = turnstile.getAttribute('data-sitekey');
        result.type = 'turnstile';
        return result;
    }

    // Turnstile via iframe only
    const cfIframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
    if (cfIframe) {
        const m = cfIframe.src.match(/[?&]k=([^&]+)/);
        if (m) { result.sitekey = m[1]; result.type = 'turnstile'; return result; }
    }

    return result;
})()"""


# Token injection JS templates
INJECT_TOKEN_JS = {
    "recaptcha_v2": """(token) => {
        const el = document.getElementById('g-recaptcha-response');
        if (el) { el.value = token; el.style.display = 'none'; }
        // Also try textarea variant
        const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
        if (ta) { ta.value = token; }
        // Trigger callback if registered
        if (typeof ___grecaptcha_cfg !== 'undefined') {
            const clients = ___grecaptcha_cfg.clients;
            if (clients) {
                for (const cid of Object.keys(clients)) {
                    const client = clients[cid];
                    // Walk the client object to find callback
                    const walk = (obj) => {
                        if (!obj || typeof obj !== 'object') return;
                        for (const key of Object.keys(obj)) {
                            if (typeof obj[key] === 'function' && key.length < 3) {
                                try { obj[key](token); } catch(e) {}
                            }
                            if (typeof obj[key] === 'object') walk(obj[key]);
                        }
                    };
                    walk(client);
                }
            }
        }
    }""",
    "recaptcha_v3": """(token) => {
        const el = document.getElementById('g-recaptcha-response');
        if (el) el.value = token;
        const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
        if (ta) ta.value = token;
    }""",
    "hcaptcha": """(token) => {
        const el = document.querySelector('[name="h-captcha-response"]') ||
                   document.querySelector('textarea[name="h-captcha-response"]');
        if (el) el.value = token;
        // Also set g-recaptcha-response (hCaptcha compat mode)
        const g = document.querySelector('[name="g-recaptcha-response"]');
        if (g) g.value = token;
    }""",
    "turnstile": """(token) => {
        const input = document.querySelector('[name="cf-turnstile-response"]') ||
                      document.querySelector('input[name="cf-turnstile-response"]');
        if (input) input.value = token;
        // Turnstile callback
        if (window.turnstile && typeof window.turnstile._callbacks === 'object') {
            for (const cb of Object.values(window.turnstile._callbacks)) {
                if (typeof cb === 'function') try { cb(token); } catch(e) {}
            }
        }
    }""",
}


# ---------------------------------------------------------------------------
# Solver backends
# ---------------------------------------------------------------------------

async def _solve_capsolver(
    captcha_type: str,
    sitekey: str,
    page_url: str,
    action: Optional[str] = None,
) -> Optional[str]:
    """Solve CAPTCHA via CapSolver API. Returns token or None."""
    api_key = Config.CAPSOLVER_API_KEY
    if not api_key:
        return None

    # Map our type names to CapSolver task types
    task_map = {
        "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
        "recaptcha_v3": "ReCaptchaV3TaskProxyLess",
        "hcaptcha": "HCaptchaTaskProxyLess",
        "turnstile": "AntiTurnstileTaskProxyLess",
    }
    task_type = task_map.get(captcha_type)
    if not task_type:
        return None

    try:
        import aiohttp
    except ImportError:
        return None

    task: dict[str, Any] = {
        "type": task_type,
        "websiteURL": page_url,
        "websiteKey": sitekey,
    }
    if captcha_type == "recaptcha_v3":
        task["pageAction"] = action or "verify"
        task["minScore"] = 0.7

    async with aiohttp.ClientSession() as http:
        # Create task
        resp = await http.post(
            "https://api.capsolver.com/createTask",
            json={"clientKey": api_key, "task": task},
            timeout=aiohttp.ClientTimeout(total=15),
        )
        data = await resp.json()
        if data.get("errorId", 0) != 0:
            return None

        task_id = data.get("taskId")
        # Some tasks return solution immediately
        solution = data.get("solution", {})
        token = solution.get("gRecaptchaResponse") or solution.get("token")
        if token:
            return token

        if not task_id:
            return None

        # Poll for result (max 120s)
        for _ in range(60):
            await asyncio.sleep(2)
            resp = await http.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            data = await resp.json()
            status = data.get("status", "")
            if status == "ready":
                sol = data.get("solution", {})
                return sol.get("gRecaptchaResponse") or sol.get("token")
            if status == "failed" or data.get("errorId", 0) != 0:
                return None

    return None


async def _solve_twocaptcha(
    captcha_type: str,
    sitekey: str,
    page_url: str,
    action: Optional[str] = None,
) -> Optional[str]:
    """Solve CAPTCHA via 2Captcha API. Returns token or None."""
    api_key = Config.TWOCAPTCHA_API_KEY
    if not api_key:
        return None

    try:
        import aiohttp
    except ImportError:
        return None

    # Build request params
    params: dict[str, Any] = {
        "key": api_key,
        "json": 1,
    }

    if captcha_type in ("recaptcha_v2", "recaptcha_v3"):
        params["method"] = "userrecaptcha"
        params["googlekey"] = sitekey
        params["pageurl"] = page_url
        if captcha_type == "recaptcha_v3":
            params["version"] = "v3"
            params["action"] = action or "verify"
            params["min_score"] = 0.7
    elif captcha_type == "hcaptcha":
        params["method"] = "hcaptcha"
        params["sitekey"] = sitekey
        params["pageurl"] = page_url
    elif captcha_type == "turnstile":
        params["method"] = "turnstile"
        params["sitekey"] = sitekey
        params["pageurl"] = page_url
    else:
        return None

    async with aiohttp.ClientSession() as http:
        # Submit
        resp = await http.post(
            "https://2captcha.com/in.php",
            data=params,
            timeout=aiohttp.ClientTimeout(total=15),
        )
        data = await resp.json()
        if data.get("status") != 1:
            return None

        request_id = data.get("request")
        if not request_id:
            return None

        # Poll (max 180s)
        await asyncio.sleep(10)  # 2Captcha needs initial wait
        for _ in range(34):
            await asyncio.sleep(5)
            resp = await http.get(
                "https://2captcha.com/res.php",
                params={"key": api_key, "action": "get", "id": request_id, "json": 1},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            data = await resp.json()
            if data.get("status") == 1:
                return data.get("request")
            if data.get("request") != "CAPCHA_NOT_READY":
                return None  # Error

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def solve_captcha(page: Any) -> dict:
    """Detect and solve CAPTCHA on the current page.

    Extracts sitekey, tries CapSolver (fast), falls back to 2Captcha.
    Injects token back into the page on success.

    Returns:
        {success, captcha_type, solver, solve_time_s, error?}
    """
    start = time.monotonic()

    # Extract CAPTCHA parameters from page
    try:
        info = await page.evaluate(EXTRACT_SITEKEY_JS)
    except Exception as e:
        return {"success": False, "error": f"Failed to extract CAPTCHA info: {e}"}

    captcha_type = info.get("type")
    sitekey = info.get("sitekey")
    action = info.get("action")

    if not captcha_type or not sitekey:
        return {
            "success": False,
            "error": "No CAPTCHA detected on page (no sitekey found). "
                     "Page may use a non-standard CAPTCHA or challenge.",
        }

    page_url = page.url
    token = None
    solver_used = None

    # Tier 1: CapSolver (fast, AI)
    if Config.CAPSOLVER_API_KEY:
        token = await _solve_capsolver(captcha_type, sitekey, page_url, action)
        if token:
            solver_used = "capsolver"

    # Tier 2: 2Captcha (human fallback)
    if not token and Config.TWOCAPTCHA_API_KEY:
        token = await _solve_twocaptcha(captcha_type, sitekey, page_url, action)
        if token:
            solver_used = "2captcha"

    if not token:
        configured = []
        if Config.CAPSOLVER_API_KEY:
            configured.append("capsolver")
        if Config.TWOCAPTCHA_API_KEY:
            configured.append("2captcha")
        if not configured:
            return {
                "success": False,
                "error": "No CAPTCHA solver API keys configured. "
                         "Set CAPSOLVER_API_KEY or TWOCAPTCHA_API_KEY.",
            }
        return {
            "success": False,
            "error": f"All solvers failed for {captcha_type} (sitekey: {sitekey[:16]}...). "
                     f"Tried: {', '.join(configured)}",
            "captcha_type": captcha_type,
        }

    # Inject token
    inject_js = INJECT_TOKEN_JS.get(captcha_type)
    if inject_js:
        try:
            await page.evaluate(f"({inject_js})('{token}')")
        except Exception as e:
            return {
                "success": False,
                "error": f"Token obtained but injection failed: {e}",
                "captcha_type": captcha_type,
                "solver": solver_used,
            }

    elapsed = round(time.monotonic() - start, 1)
    return {
        "success": True,
        "captcha_type": captcha_type,
        "solver": solver_used,
        "solve_time_s": elapsed,
        "extracted_content": f"Solved {captcha_type} via {solver_used} in {elapsed}s",
    }
