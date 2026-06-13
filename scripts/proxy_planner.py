"""Proxy session planning — strategy selection + provider username templating.

Pure and I/O-free: produces Playwright proxy dicts (``{"server","username","password"}``)
for a browser launch, plus small diagnostics helpers. No network, no logging, no global
state — so it is fully unit-testable without a live proxy.

Strategies (``PROXY_STRATEGY``):
  - ``static``      : single fixed proxy (``PROXY_SERVER`` + ``PROXY_USERNAME``/``PASSWORD``). Default.
  - ``port_pool``   : rotate across ``PROXY_HOST``:``{PROXY_PORTS[i]}`` per launch (round-robin).
  - ``backconnect`` : residential backconnect endpoint (``PROXY_BACKCONNECT_HOST``:``PORT``) whose
                      exit is shaped via a provider username DSL (geo-targeting + sticky session).

Providers (backconnect only, ``PROXY_PROVIDER``): ``decodo`` | ``generic``. Two providers, one
templating function — deliberately NOT a runtime-registerable plugin system.

Re-authored (MIT) from jo-inc/camofox-browser ``lib/proxy.js`` + ``lib/resources.js``. Minimal
single-operator shape: strategy shaping + provider username templating + geo-targeting.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlparse, urlunparse


# ---------------------------------------------------------------------------
# Username shaping (backconnect providers)
# ---------------------------------------------------------------------------

def _sanitize_backconnect_value(value: Any) -> str:
    """Lowercase + collapse to ``[a-z0-9_]`` for safe proxy-username DSL components.

    Mirror of camofox-browser ``lib/proxy.js:51-60``. Empty/None -> ``""``.
    """
    if not value:
        return ""
    s = str(value).strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def build_session_username(
    provider: str,
    base_username: str,
    *,
    country: str | None = None,
    state: str | None = None,
    city: str | None = None,
    zip_code: str | None = None,
    session_id: str | None = None,
    session_duration_minutes: Any = None,
) -> str:
    """Shape a backconnect username for the given provider.

    decodo  -> ``user-{base}-country-{cc}-state-{st}-city-{c}-zip-{z}-session-{id}-sessionduration-{min}``
    generic -> ``{base}-{session_id}``  (pass-through; only a session suffix)
    """
    if provider == "decodo":
        username = _sanitize_backconnect_value(base_username)
        if not username:
            return ""
        parts = [f"user-{username}"]
        for label, val in (("country", country), ("state", state), ("city", city), ("zip", zip_code)):
            sv = _sanitize_backconnect_value(val)
            if sv:
                parts.append(f"{label}-{sv}")
        sid = _sanitize_backconnect_value(session_id)
        if sid:
            parts.append(f"session-{sid}")
        if session_duration_minutes not in (None, ""):
            try:
                d = max(1, min(1440, int(session_duration_minutes)))
                parts.append(f"sessionduration-{d}")
            except (TypeError, ValueError):
                pass
        return "-".join(parts)

    # generic backconnect — pass through base + optional session suffix
    base = str(base_username or "").strip()
    sid = str(session_id).strip() if session_id else ""
    return f"{base}-{sid}" if sid else base


# ---------------------------------------------------------------------------
# Proxy planning
# ---------------------------------------------------------------------------

def ports_list(raw: Any) -> list[str]:
    """Normalize ``PROXY_PORTS`` (comma/space string or list) to a list of port strings."""
    if not raw:
        return []
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,\s]+", str(raw))
    return [str(p).strip() for p in items if str(p).strip()]


def plan_proxy(config: Any, *, session_id: str | None = None, port_index: int = 0) -> dict | None:
    """Build a Playwright proxy dict for a launch, or ``None`` when no proxy is configured.

    Pure: reads config attributes only; never logs or touches the network. The geo-mismatch
    warning is emitted by the caller (see :func:`geo_mismatch_warning`).
    """
    strategy = str(getattr(config, "PROXY_STRATEGY", "static") or "static").lower()

    if strategy == "static":
        server = getattr(config, "PROXY_SERVER", "")
        if not server:
            return None
        proxy: dict[str, str] = {"server": server}
        if getattr(config, "PROXY_USERNAME", ""):
            proxy["username"] = config.PROXY_USERNAME
        if getattr(config, "PROXY_PASSWORD", ""):
            proxy["password"] = config.PROXY_PASSWORD
        return proxy

    if strategy == "port_pool":
        host = getattr(config, "PROXY_HOST", "")
        ports = ports_list(getattr(config, "PROXY_PORTS", ""))
        if not host or not ports:
            return None
        port = ports[port_index % len(ports)]
        proxy = {"server": f"http://{host}:{port}"}
        if getattr(config, "PROXY_USERNAME", ""):
            proxy["username"] = config.PROXY_USERNAME
        if getattr(config, "PROXY_PASSWORD", ""):
            proxy["password"] = config.PROXY_PASSWORD
        return proxy

    if strategy == "backconnect":
        host = getattr(config, "PROXY_BACKCONNECT_HOST", "")
        port = getattr(config, "PROXY_BACKCONNECT_PORT", "")
        base_user = getattr(config, "PROXY_USERNAME", "")
        pwd = getattr(config, "PROXY_PASSWORD", "")
        if not host or not port or not base_user or not pwd:
            return None
        provider = str(getattr(config, "PROXY_PROVIDER", "decodo") or "decodo").lower()
        username = build_session_username(
            provider,
            base_user,
            country=getattr(config, "PROXY_COUNTRY", ""),
            state=getattr(config, "PROXY_STATE", ""),
            city=getattr(config, "PROXY_CITY", ""),
            zip_code=getattr(config, "PROXY_ZIP", ""),
            session_id=session_id,
            session_duration_minutes=getattr(config, "PROXY_SESSION_DURATION_MINUTES", None),
        )
        return {"server": f"http://{host}:{port}", "username": username, "password": pwd}

    # Unknown strategy -> no proxy. Caller (browser_engine) decides; we stay pure.
    return None


def proxy_to_url(proxy: dict | None) -> str | None:
    """Render a planned proxy dict as an authenticated URL (``http://user:pass@host:port``).

    Credentials are URL-encoded. Returns ``None`` for a falsy proxy. Used for the exit-IP /
    GeoIP probe, which needs a URL string rather than a Playwright dict.
    """
    if not proxy or not proxy.get("server"):
        return None
    server = proxy["server"]
    user = proxy.get("username")
    if not user:
        return server
    parsed = urlparse(server)
    u = quote(user, safe="")
    p = quote(proxy.get("password", ""), safe="") if proxy.get("password") else ""
    netloc = f"{u}:{p}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


# ---------------------------------------------------------------------------
# Geo consistency (protects the WebRTC/GeoIP stealth posture)
# ---------------------------------------------------------------------------

def geo_country_from_locale(locale: str | None) -> str | None:
    """Extract a lowercase 2-letter country from a locale like ``en-US`` -> ``us``. None if absent."""
    if not locale or "-" not in str(locale):
        return None
    cc = str(locale).split("-")[-1].strip().lower()
    return cc or None


def declared_proxy_country(config: Any) -> str | None:
    """The country a backconnect proxy will present (geo-targeting), or ``None``.

    Only the backconnect strategy encodes a country into the exit; static/port_pool do not.
    """
    strategy = str(getattr(config, "PROXY_STRATEGY", "static") or "static").lower()
    if strategy != "backconnect":
        return None
    return _sanitize_backconnect_value(getattr(config, "PROXY_COUNTRY", "")) or None


def geo_mismatch_warning(config: Any) -> str | None:
    """Return a warning string when a backconnect proxy's declared exit country disagrees with
    the statically-configured browser geo (``BROWSER_USE_GEO``), else ``None``.

    A backconnect proxy targeting country X while the browser advertises locale/timezone for
    country Y is a fingerprint mismatch that undermines the WebRTC/GeoIP spoofing. This is
    advisory only — it never blocks a launch (operator owns scope).
    """
    declared = declared_proxy_country(config)
    if not declared:
        return None
    geo_key = str(getattr(config, "GEO", "") or "").strip().lower()
    if not geo_key:
        return None
    # GEO keys look like "us", "us-ny", "de" — take the leading country segment.
    static_country = geo_key.split("-")[0]
    if static_country and static_country != declared:
        return (
            f"Proxy geo mismatch: backconnect proxy targets country '{declared}' but "
            f"BROWSER_USE_GEO='{geo_key}' advertises country '{static_country}'. Browser "
            f"timezone/locale will disagree with the proxy exit country — a fingerprint "
            f"inconsistency that weakens stealth. Align PROXY_COUNTRY with BROWSER_USE_GEO."
        )
    return None


# ---------------------------------------------------------------------------
# Proxy error classification (sanitized — no IPs or credentials)
# ---------------------------------------------------------------------------

def classify_proxy_error(error_message: Any) -> dict:
    """Map a Playwright/launch error message to a clean proxy error code.

    Returns ``{"proxy_error": str|None, "proxy_tls_error": bool}``. Never echoes the raw
    message, IPs, or credentials. Re-authored from camofox-browser ``lib/resources.js:101-111``.
    """
    if not error_message or not isinstance(error_message, str):
        return {"proxy_error": None, "proxy_tls_error": False}
    msg = error_message.upper()
    if "ERR_PROXY_CONNECTION_FAILED" in msg:
        return {"proxy_error": "ERR_PROXY_CONNECTION_FAILED", "proxy_tls_error": False}
    if "ERR_TUNNEL_CONNECTION_FAILED" in msg:
        return {"proxy_error": "ERR_TUNNEL_CONNECTION_FAILED", "proxy_tls_error": False}
    if "ERR_PROXY_AUTH_REQUESTED" in msg or "407" in msg:
        return {"proxy_error": "ERR_PROXY_AUTH_REQUESTED", "proxy_tls_error": False}
    if "ERR_PROXY_CERTIFICATE_INVALID" in msg or ("PROXY" in msg and "SSL" in msg):
        return {"proxy_error": "ERR_PROXY_TLS", "proxy_tls_error": True}
    if "ECONNREFUSED" in msg and "PROXY" in msg:
        return {"proxy_error": "ECONNREFUSED", "proxy_tls_error": False}
    if "ETIMEDOUT" in msg and "PROXY" in msg:
        return {"proxy_error": "ETIMEDOUT", "proxy_tls_error": False}
    return {"proxy_error": None, "proxy_tls_error": False}
