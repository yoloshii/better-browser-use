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
    const r = {type: null, sitekey: null, action: null, cdata: null};
    const url = window.location.href;

    // 1. hCaptcha — check FIRST (hCaptcha elements also have data-sitekey,
    //    which would false-positive as reCAPTCHA if checked later)
    const hc = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
    if (hc) {
        r.type = 'hcaptcha';
        r.sitekey = hc.dataset.sitekey || hc.dataset.hcaptchaSitekey;
        r.url = url; return r;
    }
    if (document.querySelector('script[src*="hcaptcha.com"], iframe[src*="hcaptcha.com"]')) {
        const el = document.querySelector('[data-sitekey]');
        if (el) { r.type = 'hcaptcha'; r.sitekey = el.dataset.sitekey; r.url = url; return r; }
        const f = document.querySelector('iframe[src*="hcaptcha.com"]');
        if (f) {
            const m = f.src.match(/sitekey=([^&]+)/);
            if (m) { r.type = 'hcaptcha'; r.sitekey = m[1]; r.url = url; return r; }
        }
    }

    // 2. Cloudflare Turnstile — check before reCAPTCHA (also uses data-sitekey)
    const cf = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
    if (cf) {
        r.type = 'turnstile';
        r.sitekey = cf.dataset.sitekey || cf.dataset.turnstileSitekey;
        if (cf.dataset.action) r.action = cf.dataset.action;
        if (cf.dataset.cdata) r.cdata = cf.dataset.cdata;
        r.url = url; return r;
    }
    if (document.querySelector('script[src*="challenges.cloudflare.com"]')) {
        // Turnstile script loaded but no widget yet (explicit render mode)
        r.type = 'turnstile_script_only'; r.url = url; return r;
    }
    const cfIframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
    if (cfIframe) {
        const m = cfIframe.src.match(/[?&]k=([^&]+)/);
        if (m) { r.type = 'turnstile'; r.sitekey = m[1]; r.url = url; return r; }
    }

    // 3. reCAPTCHA v3 — invisible, loaded via render= param in script src
    const v3Script = document.querySelector('script[src*="recaptcha"][src*="render="]');
    if (v3Script) {
        const m = v3Script.src.match(/render=([^&]+)/);
        if (m && m[1] !== 'explicit') {
            r.type = 'recaptcha_v3'; r.sitekey = m[1]; r.url = url; return r;
        }
    }

    // 4. reCAPTCHA v2 (checkbox or invisible) — DOM element with data-sitekey
    const rc = document.querySelector('.g-recaptcha[data-sitekey]');
    if (rc) {
        r.sitekey = rc.dataset.sitekey;
        const action = rc.getAttribute('data-action');
        if (action) { r.type = 'recaptcha_v3'; r.action = action; }
        else { r.type = 'recaptcha_v2'; }
        r.url = url; return r;
    }

    // 5. reCAPTCHA v2 via iframe (no DOM element)
    const rcIframe = document.querySelector('iframe[src*="recaptcha"]');
    if (rcIframe) {
        const m = rcIframe.src.match(/[?&]k=([^&]+)/);
        if (m) { r.type = 'recaptcha_v2'; r.sitekey = m[1]; r.url = url; return r; }
    }

    return r;
})()"""


# Token injection JS templates
INJECT_TOKEN_JS = {
    "recaptcha_v2": """(token) => {
        document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {
            el.value = token; el.style.display = 'block';
        });
        // Trigger callback — depth-limited walk through ___grecaptcha_cfg.clients
        if (typeof ___grecaptcha_cfg !== 'undefined' && ___grecaptcha_cfg.clients) {
            const walk = (obj, depth) => {
                if (depth > 4 || !obj) return;
                for (const k in obj) {
                    if (typeof obj[k] === 'function' && k.length < 3) {
                        try { obj[k](token); } catch(e) {}
                    } else if (typeof obj[k] === 'object') {
                        walk(obj[k], depth + 1);
                    }
                }
            };
            for (const cid of Object.keys(___grecaptcha_cfg.clients)) {
                walk(___grecaptcha_cfg.clients[cid], 0);
            }
        }
    }""",
    "recaptcha_v3": """(token) => {
        document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {
            el.value = token;
        });
    }""",
    "hcaptcha": """(token) => {
        const ta = document.querySelector('[name="h-captcha-response"], textarea[name*="hcaptcha"]');
        if (ta) ta.value = token;
        // Set iframe data attribute for cross-frame verification
        document.querySelectorAll('iframe[data-hcaptcha-response]').forEach(f => {
            f.setAttribute('data-hcaptcha-response', token);
        });
        // hCaptcha compat: some sites also check g-recaptcha-response
        const g = document.querySelector('[name="g-recaptcha-response"]');
        if (g) g.value = token;
    }""",
    "turnstile": """(token) => {
        const inp = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile"]');
        if (inp) inp.value = token;
        // Trigger Turnstile success callback via widget element
        const w = document.querySelector('.cf-turnstile');
        if (w && w.dataset.callback && typeof window[w.dataset.callback] === 'function') {
            try { window[w.dataset.callback](token); } catch(e) {}
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
    cdata: Optional[str] = None,
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
    if captcha_type == "turnstile":
        metadata = {}
        if action:
            metadata["action"] = action
        if cdata:
            metadata["cdata"] = cdata
        if metadata:
            task["metadata"] = metadata

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
    cdata: Optional[str] = None,
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
        if action:
            params["action"] = action
        if cdata:
            params["data"] = cdata
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
    cdata = info.get("cdata")

    # Turnstile script_only: widget not rendered yet (explicit render mode).
    # Bounded re-detect: poll up to 3 times, 1s apart.
    if captcha_type == "turnstile_script_only":
        for _ in range(3):
            await asyncio.sleep(1)
            try:
                info = await page.evaluate(EXTRACT_SITEKEY_JS)
            except Exception:
                break
            if info.get("type") and info.get("type") != "turnstile_script_only":
                captcha_type = info.get("type")
                sitekey = info.get("sitekey")
                action = info.get("action")
                cdata = info.get("cdata")
                break
        else:
            return {
                "success": False,
                "error": "Turnstile script loaded but widget never rendered (explicit render). "
                         "The page may require user interaction to trigger turnstile.render().",
            }

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
        token = await _solve_capsolver(captcha_type, sitekey, page_url, action, cdata)
        if token:
            solver_used = "capsolver"

    # Tier 2: 2Captcha (human fallback)
    if not token and Config.TWOCAPTCHA_API_KEY:
        token = await _solve_twocaptcha(captcha_type, sitekey, page_url, action, cdata)
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
