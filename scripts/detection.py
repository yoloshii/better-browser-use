"""Site protection detection for optimal tier selection.

Detects anti-bot protection (Cloudflare, DataDome, Akamai, PerimeterX)
from URL patterns, response headers, and HTML content. Recommends
minimum stealth tier and proxy requirements.

Extracted from ultimate-scraper detection/mode_detector.py — self-contained,
no external dependencies except httpx (lazy-imported in probe() only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse


@dataclass
class SiteProfile:
    """Profile describing site protection and recommended approach."""

    url: str
    domain: str = ""

    # Anti-bot detection
    antibot: Optional[str] = None
    antibot_confidence: float = 0.0

    # JA4T (transport-layer fingerprinting)
    uses_ja4t: bool = False
    ja4t_confidence: float = 0.0

    # Content characteristics
    has_static_data: bool = False
    requires_js: bool = False

    # Recommendations
    recommended_tier: int = 1
    needs_proxy: bool = False
    needs_sticky: bool = False

    # Additional info
    detected_framework: Optional[str] = None
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Known site profiles (domain pattern → protection config)
# ---------------------------------------------------------------------------

SITE_PROFILES: dict[str, dict] = {
    # E-commerce (heavy anti-bot with JA4T)
    "amazon.": {"antibot": "akamai", "tier": 3, "proxy": True, "sticky": True, "ja4t": True},
    "ebay.": {"antibot": "akamai", "tier": 3, "proxy": True, "ja4t": True},
    "walmart.": {"antibot": "perimeterx", "tier": 3, "proxy": True, "ja4t": True},
    "target.": {"antibot": "akamai", "tier": 3, "proxy": True, "ja4t": True},
    "bestbuy.": {"antibot": "akamai", "tier": 3, "proxy": True, "ja4t": True},

    # Social media
    "linkedin.": {"antibot": "datadome", "tier": 3, "proxy": True, "sticky": True, "ja4t": True},
    "twitter.": {"antibot": "cloudflare", "tier": 2, "proxy": True},
    "x.com": {"antibot": "cloudflare", "tier": 2, "proxy": True},
    "facebook.": {"antibot": "custom", "tier": 3, "proxy": True, "ja4t": True},
    "instagram.": {"antibot": "custom", "tier": 3, "proxy": True, "ja4t": True},

    # Tech/Reviews
    "g2.com": {"antibot": "datadome", "tier": 3, "proxy": True, "ja4t": True},
    "trustpilot.": {"antibot": "cloudflare", "tier": 2, "proxy": True},
    "glassdoor.": {"antibot": "cloudflare", "tier": 2, "proxy": True},

    # Travel
    "booking.com": {"antibot": "perimeterx", "tier": 3, "proxy": True, "ja4t": True},
    "airbnb.": {"antibot": "akamai", "tier": 3, "proxy": True, "ja4t": True},
    "expedia.": {"antibot": "akamai", "tier": 3, "proxy": True, "ja4t": True},

    # Real estate
    "zillow.": {"antibot": "perimeterx", "tier": 3, "proxy": True, "ja4t": True},
    "redfin.": {"antibot": "cloudflare", "tier": 2, "proxy": True},
    "realtor.": {"antibot": "akamai", "tier": 3, "proxy": True},

    # Job boards
    "indeed.": {"antibot": "cloudflare", "tier": 2, "proxy": True},
    "monster.": {"antibot": "cloudflare", "tier": 2, "proxy": True},

    # News (often paywalled)
    "nytimes.": {"antibot": "cloudflare", "tier": 2, "paywall": True},
    "wsj.": {"antibot": "akamai", "tier": 2, "paywall": True},
    "bloomberg.": {"antibot": "cloudflare", "tier": 2, "paywall": True},

    # Google services
    "google.": {"antibot": "custom", "tier": 2, "proxy": True, "ja4t_suspected": True},
    "youtube.": {"antibot": "custom", "tier": 2, "proxy": True, "ja4t_suspected": True},

    # Financial (heavy security)
    "paypal.": {"antibot": "custom", "tier": 3, "proxy": True, "sticky": True, "ja4t": True},
    "chase.": {"antibot": "akamai", "tier": 3, "proxy": True, "sticky": True, "ja4t": True},
    "bankofamerica.": {"antibot": "akamai", "tier": 3, "proxy": True, "sticky": True, "ja4t": True},

    # Default
    "_default": {"antibot": None, "tier": 1, "proxy": False, "ja4t": False},
}

# Sites with confirmed JA4T (transport-layer fingerprinting)
JA4T_SITES: dict[str, dict] = {
    "linkedin.": {"ja4t": True, "confidence": 0.95},
    "amazon.": {"ja4t": True, "confidence": 0.90},
    "google.": {"ja4t_suspected": True, "confidence": 0.70},
    "facebook.": {"ja4t": True, "confidence": 0.85},
    "booking.com": {"ja4t": True, "confidence": 0.90},
    "zillow.": {"ja4t": True, "confidence": 0.85},
    "walmart.": {"ja4t": True, "confidence": 0.85},
}

# Anti-bot detection patterns in response headers
ANTIBOT_HEADERS: dict[str, str] = {
    "cf-ray": "cloudflare",
    "cf-cache-status": "cloudflare",
    "x-datadome": "datadome",
    "x-datadome-cid": "datadome",
    "x-akamai-transformed": "akamai",
    "akamai-grn": "akamai",
    "x-px-": "perimeterx",
}

# Anti-bot detection patterns in HTML content
ANTIBOT_HTML_PATTERNS: dict[str, list[str]] = {
    "cloudflare": [
        r"cf-browser-verification",
        r"cdn-cgi/challenge-platform",
        r"__cf_chl_",
        r"Cloudflare Ray ID",
        r"Just a moment\.\.\.",
    ],
    "cloudflare_uam": [
        r"Checking your browser before accessing",
        r"This process is automatic",
        r"Please Wait\.\.\. \| Cloudflare",
    ],
    "datadome": [
        r"datadome\.co",
        r"dd\.js",
        r"window\.ddjskey",
    ],
    "akamai": [
        r"_abck",
        r"bm_sz",
        r"ak_bmsc",
    ],
    "perimeterx": [
        r"_px3",
        r"_pxff_",
        r"px-captcha",
    ],
}


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class ModeDetector:
    """Detect site protection characteristics and recommend stealth tier."""

    async def detect(
        self,
        url: str,
        html: Optional[str] = None,
        headers: Optional[dict] = None,
    ) -> SiteProfile:
        """Detect site profile from URL, optional HTML, and optional headers."""
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        profile = SiteProfile(url=url, domain=domain)

        # Check known sites
        for pattern, config in SITE_PROFILES.items():
            if pattern == "_default":
                continue
            if pattern in domain:
                profile.antibot = config.get("antibot")
                profile.recommended_tier = config.get("tier", 1)
                profile.needs_proxy = config.get("proxy", False)
                profile.needs_sticky = config.get("sticky", False)
                profile.antibot_confidence = 0.9
                profile.metadata["matched_pattern"] = pattern

                if config.get("ja4t"):
                    profile.uses_ja4t = True
                    profile.ja4t_confidence = 0.9
                elif config.get("ja4t_suspected"):
                    profile.uses_ja4t = True
                    profile.ja4t_confidence = 0.6
                break

        # JA4T-specific detection
        for pattern, ja4t_config in JA4T_SITES.items():
            if pattern in domain:
                if ja4t_config.get("ja4t") or ja4t_config.get("ja4t_suspected"):
                    profile.uses_ja4t = True
                    profile.ja4t_confidence = max(
                        profile.ja4t_confidence,
                        ja4t_config.get("confidence", 0.7),
                    )
                break

        # Header-based detection (if no known pattern matched)
        if not profile.antibot and headers:
            for header, antibot in ANTIBOT_HEADERS.items():
                if any(header.lower() in h.lower() for h in headers.keys()):
                    profile.antibot = antibot
                    profile.antibot_confidence = 0.7
                    profile.metadata["detected_via"] = "headers"
                    break

        # HTML-based detection
        if html and not profile.antibot:
            for antibot, patterns in ANTIBOT_HTML_PATTERNS.items():
                for pattern in patterns:
                    if re.search(pattern, html, re.IGNORECASE):
                        profile.antibot = antibot
                        profile.antibot_confidence = 0.8
                        profile.metadata["detected_via"] = "html"
                        profile.metadata["detected_pattern"] = pattern
                        break
                if profile.antibot:
                    break

        # Static data detection
        if html:
            profile.has_static_data = _has_static_data(html)
            profile.detected_framework = _detect_framework(html)

        # Tier recommendation based on anti-bot type
        if profile.antibot:
            if profile.antibot in ("akamai", "datadome", "perimeterx", "cloudflare_uam"):
                profile.recommended_tier = 3
                profile.needs_proxy = True
            elif profile.antibot == "cloudflare":
                profile.recommended_tier = 2
                profile.needs_proxy = True
            else:
                profile.recommended_tier = 2

        # JA4T needs at least tier 2
        if profile.uses_ja4t and profile.ja4t_confidence > 0.5:
            profile.recommended_tier = max(profile.recommended_tier, 2)
            profile.needs_proxy = True

        # Default fallback
        if not profile.antibot:
            default = SITE_PROFILES["_default"]
            profile.recommended_tier = default["tier"]
            profile.needs_proxy = default["proxy"]

        return profile

    async def probe(self, url: str, timeout: int = 10) -> SiteProfile:
        """Probe URL with HEAD/GET to detect anti-bot without full browser.

        Requires httpx (lazy-imported, not a hard dependency).
        """
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                response = await client.head(url, timeout=timeout, follow_redirects=True)
                headers = dict(response.headers)

                if len(headers) < 5:
                    response = await client.get(url, timeout=timeout, follow_redirects=True)
                    headers = dict(response.headers)
                    html = response.text
                else:
                    html = None

                return await self.detect(url, html=html, headers=headers)

        except Exception as e:
            profile = SiteProfile(url=url)
            profile.metadata["probe_error"] = str(e)
            return profile


# ---------------------------------------------------------------------------
# Convenience functions for browser-use integration
# ---------------------------------------------------------------------------

async def detect_protection(url: str, html: Optional[str] = None, headers: Optional[dict] = None) -> SiteProfile:
    """Detect site protection — convenience wrapper around ModeDetector."""
    return await ModeDetector().detect(url, html=html, headers=headers)


async def is_blocked(page) -> Optional[str]:
    """Quick check if current page shows a block/challenge page.

    Returns the protection type string if blocked, None if OK.
    Lightweight — just checks page title and a small HTML sample.
    """
    try:
        title = (await page.title()).lower()
        url = page.url.lower()

        # Cloudflare challenge
        if "just a moment" in title or "attention required" in title:
            return "cloudflare"

        # DataDome
        if "datadome" in title:
            return "datadome"

        # PerimeterX
        if "access denied" in title or "px-captcha" in url:
            return "perimeterx"

        # Generic block indicators
        if any(s in title for s in ("access denied", "403 forbidden", "blocked")):
            return "generic"

        # Check for captcha in visible content (small sample)
        content = await page.evaluate(
            "document.body ? document.body.innerText.substring(0, 500) : ''"
        )
        content_lower = content.lower()
        if "captcha" in content_lower or "verify you are human" in content_lower:
            return "captcha"

    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_static_data(html: str) -> bool:
    """Check if HTML contains extractable static data."""
    indicators = [
        "__NEXT_DATA__",
        "__NUXT__",
        "application/ld+json",
        "__APOLLO_STATE__",
        "__INITIAL_STATE__",
        "__PRELOADED_STATE__",
    ]
    return any(indicator in html for indicator in indicators)


def _detect_framework(html: str) -> Optional[str]:
    """Detect frontend framework from HTML."""
    if "__NEXT_DATA__" in html:
        return "nextjs"
    if "__NUXT__" in html:
        return "nuxt"
    if "__remixContext" in html:
        return "remix"
    if "__GATSBY" in html:
        return "gatsby"
    if "ng-version" in html:
        return "angular"
    if "data-reactroot" in html or "data-react-" in html:
        return "react"
    return None
