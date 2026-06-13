"""Unit tests for proxy_planner — pure, no live proxy or browser.

Run: python scripts/test_proxy_planner.py   (exit 0 = all pass)
"""

import sys
from types import SimpleNamespace

from proxy_planner import (
    plan_proxy,
    build_session_username,
    _sanitize_backconnect_value,
    ports_list,
    proxy_to_url,
    classify_proxy_error,
    declared_proxy_country,
    geo_country_from_locale,
    geo_mismatch_warning,
)
from errors import _scrub_credentials


def cfg(**kw):
    """A fake Config namespace with all proxy attrs defaulted (overridable per test)."""
    base = dict(
        PROXY_STRATEGY="static", PROXY_PROVIDER="decodo",
        PROXY_SERVER="", PROXY_USERNAME="", PROXY_PASSWORD="",
        PROXY_HOST="", PROXY_PORTS="",
        PROXY_BACKCONNECT_HOST="", PROXY_BACKCONNECT_PORT="",
        PROXY_COUNTRY="", PROXY_STATE="", PROXY_CITY="", PROXY_ZIP="",
        PROXY_SESSION_DURATION_MINUTES="", GEO="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"FAIL: {name}")


# --- sanitize ---------------------------------------------------------------
check("sanitize spaces", _sanitize_backconnect_value("New York") == "new_york")
check("sanitize special", _sanitize_backconnect_value("a@b.c!") == "a_b_c")
check("sanitize collapse+strip", _sanitize_backconnect_value("  __A B__ ") == "a_b")
check("sanitize empty", _sanitize_backconnect_value("") == "")
check("sanitize none", _sanitize_backconnect_value(None) == "")

# --- ports list -------------------------------------------------------------
check("ports csv", ports_list("10, 20 ,30") == ["10", "20", "30"])
check("ports list", ports_list([10, 20]) == ["10", "20"])
check("ports empty", ports_list("") == [])

# --- decodo username DSL ----------------------------------------------------
check("decodo base", build_session_username("decodo", "joe") == "user-joe")
check(
    "decodo geo+session+dur",
    build_session_username("decodo", "joe", country="DE", state="BE", session_id="abc",
                           session_duration_minutes=10)
    == "user-joe-country-de-state-be-session-abc-sessionduration-10",
)
check("decodo dur clamp hi", build_session_username("decodo", "joe", session_duration_minutes=99999).endswith("sessionduration-1440"))
check("decodo dur clamp lo", build_session_username("decodo", "joe", session_duration_minutes=0).endswith("sessionduration-1"))
check("decodo empty base", build_session_username("decodo", "") == "")
check("decodo empty dur str", build_session_username("decodo", "joe", session_duration_minutes="") == "user-joe")

# --- generic username -------------------------------------------------------
check("generic base", build_session_username("generic", "joe") == "joe")
check("generic session", build_session_username("generic", "joe", session_id="s1") == "joe-s1")

# --- plan_proxy: static (default, backward compatible) ----------------------
check("static none when no server", plan_proxy(cfg()) is None)
check("static basic", plan_proxy(cfg(PROXY_SERVER="http://p:8080")) == {"server": "http://p:8080"})
check(
    "static auth",
    plan_proxy(cfg(PROXY_SERVER="http://p:8080", PROXY_USERNAME="u", PROXY_PASSWORD="pw"))
    == {"server": "http://p:8080", "username": "u", "password": "pw"},
)

# --- plan_proxy: port_pool --------------------------------------------------
check("pool none (no host)", plan_proxy(cfg(PROXY_STRATEGY="port_pool", PROXY_PORTS="1,2")) is None)
check("pool none (no ports)", plan_proxy(cfg(PROXY_STRATEGY="port_pool", PROXY_HOST="h")) is None)
check("pool rr index 0", plan_proxy(cfg(PROXY_STRATEGY="port_pool", PROXY_HOST="h", PROXY_PORTS="10,20,30"), port_index=0)["server"] == "http://h:10")
check("pool rr wrap (4%3)", plan_proxy(cfg(PROXY_STRATEGY="port_pool", PROXY_HOST="h", PROXY_PORTS="10,20,30"), port_index=4)["server"] == "http://h:20")

# --- plan_proxy: backconnect ------------------------------------------------
_bc = cfg(PROXY_STRATEGY="backconnect", PROXY_BACKCONNECT_HOST="bh", PROXY_BACKCONNECT_PORT="7000",
          PROXY_USERNAME="joe", PROXY_PASSWORD="pw", PROXY_COUNTRY="de")
check("bc none (missing pw)", plan_proxy(cfg(PROXY_STRATEGY="backconnect", PROXY_BACKCONNECT_HOST="bh", PROXY_BACKCONNECT_PORT="7000", PROXY_USERNAME="joe")) is None)
_bcr = plan_proxy(_bc, session_id="sess1")
check("bc server", _bcr["server"] == "http://bh:7000")
check("bc user dsl", _bcr["username"] == "user-joe-country-de-session-sess1")
check("bc password", _bcr["password"] == "pw")

# --- plan_proxy: unknown strategy -> None -----------------------------------
check("unknown strategy none", plan_proxy(cfg(PROXY_STRATEGY="bogus", PROXY_SERVER="http://p:8080")) is None)

# --- proxy_to_url -----------------------------------------------------------
check("url none", proxy_to_url(None) is None)
check("url noauth", proxy_to_url({"server": "http://p:8080"}) == "http://p:8080")
check("url auth url-encoded", proxy_to_url({"server": "http://p:8080", "username": "u@x", "password": "p:w"}) == "http://u%40x:p%3Aw@p:8080")

# --- classify_proxy_error ---------------------------------------------------
check("cls empty", classify_proxy_error("")["proxy_error"] is None)
check("cls nonstr", classify_proxy_error(None)["proxy_error"] is None)
check("cls conn_failed", classify_proxy_error("net::ERR_PROXY_CONNECTION_FAILED at x")["proxy_error"] == "ERR_PROXY_CONNECTION_FAILED")
check("cls tunnel", classify_proxy_error("ERR_TUNNEL_CONNECTION_FAILED")["proxy_error"] == "ERR_TUNNEL_CONNECTION_FAILED")
check("cls auth407", classify_proxy_error("HTTP 407 proxy auth")["proxy_error"] == "ERR_PROXY_AUTH_REQUESTED")
check("cls tls", classify_proxy_error("proxy ssl handshake failed")["proxy_tls_error"] is True)
check("cls econnrefused", classify_proxy_error("ECONNREFUSED proxy")["proxy_error"] == "ECONNREFUSED")
check("cls etimedout", classify_proxy_error("ETIMEDOUT via proxy")["proxy_error"] == "ETIMEDOUT")
check("cls plain none", classify_proxy_error("some random navigation error")["proxy_error"] is None)

# --- geo helpers ------------------------------------------------------------
check("loc us", geo_country_from_locale("en-US") == "us")
check("loc de", geo_country_from_locale("de-DE") == "de")
check("loc bare none", geo_country_from_locale("en") is None)
check("declared bc", declared_proxy_country(_bc) == "de")
check("declared static none", declared_proxy_country(cfg(PROXY_COUNTRY="de")) is None)

# --- geo mismatch warning ---------------------------------------------------
check("mismatch warns", geo_mismatch_warning(cfg(PROXY_STRATEGY="backconnect", PROXY_COUNTRY="de", GEO="us")) is not None)
check("mismatch same ok", geo_mismatch_warning(cfg(PROXY_STRATEGY="backconnect", PROXY_COUNTRY="us", GEO="us")) is None)
check("mismatch no geo", geo_mismatch_warning(cfg(PROXY_STRATEGY="backconnect", PROXY_COUNTRY="de")) is None)
check("mismatch static skip", geo_mismatch_warning(cfg(PROXY_COUNTRY="de", GEO="us")) is None)
check("mismatch us-ny vs us ok", geo_mismatch_warning(cfg(PROXY_STRATEGY="backconnect", PROXY_COUNTRY="us", GEO="us-ny")) is None)

# --- credential scrubbing (errors.py — secures P0's proxy-error path) -------
check("scrub user:pass", _scrub_credentials("net::ERR http://user:secret@proxy.example:8080 x") == "net::ERR http://***@proxy.example:8080 x")
check("scrub user only", _scrub_credentials("conn socks5://acct123@10.0.0.1:1080") == "conn socks5://***@10.0.0.1:1080")
check("scrub no-creds untouched", _scrub_credentials("ERR_PROXY_CONNECTION_FAILED at http://proxy:8080") == "ERR_PROXY_CONNECTION_FAILED at http://proxy:8080")
check("scrub plain untouched", _scrub_credentials("some random error") == "some random error")


print(f"\n{_PASS} passed, {_FAIL} failed")
sys.exit(1 if _FAIL else 0)
