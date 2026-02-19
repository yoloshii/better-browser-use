"""Configuration for browser-use skill."""

import os
import re
from pathlib import Path
from typing import Any

# Load .env file if present (python-dotenv optional — works without it)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

# Disable Playwright's document.fonts.ready wait before screenshots.
# Prevents indefinite hang in headless Chromium on WSL2/CI (playwright#28995).
os.environ.setdefault("PW_TEST_SCREENSHOT_NO_FONTS_READY", "1")


# Strict profile name regex: alphanumeric, dots, dashes, underscores only
PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def validate_profile_name(name: str) -> str | None:
    """Validate and sanitize a profile name. Returns error string or None."""
    if not name:
        return "Profile name cannot be empty"
    if not PROFILE_NAME_RE.match(name):
        return f"Invalid profile name '{name}': only [a-zA-Z0-9._-] allowed"
    if ".." in name or name.startswith("/"):
        return f"Invalid profile name '{name}': path traversal not allowed"
    return None


def safe_profile_path(base_dir: Path, name: str) -> Path | None:
    """Resolve profile path safely, rejecting traversal attempts."""
    err = validate_profile_name(name)
    if err:
        return None
    resolved = (base_dir / name).resolve()
    if not resolved.is_relative_to(base_dir.resolve()):
        return None
    return resolved


class Config:
    # Browser defaults
    DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}
    DEFAULT_TIMEOUT = 30_000  # ms
    HEADLESS = True

    # Stealth tiers
    MAX_TIER = 3
    DOMAIN_TIER_CACHE_PATH = Path.home() / ".browser-use" / "profiles" / "domain_tiers.json"

    # Humanization intensity (0.0 = robot, 2.0 = very human)
    DEFAULT_HUMANIZE = 1.0
    WARM_HUMANIZE = 1.5  # forced minimum for warm_* actions

    # Proxy (optional — bring your own SOCKS5/HTTP proxy)
    PROXY_SERVER = os.getenv("PROXY_SERVER", "")
    PROXY_USERNAME = os.getenv("PROXY_USERNAME", "")
    PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "")

    # CAPTCHA solving (optional — bring your own pay-as-you-go API keys)
    CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "")
    TWOCAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY", "")

    # Persistence paths
    PROFILE_DIR = Path.home() / ".browser-use" / "profiles"
    SESSION_DIR = Path("/tmp/browser-use-sessions")

    # Snapshot limits
    MAX_SNAPSHOT_DEPTH = 10
    MAX_SNAPSHOT_BYTES = 100_000  # hard cap on output size

    # Agent loop limits
    MAX_STEPS = 100
    BUDGET_WARNING_PCT = 0.75

    # Context compaction (see models.CompactionSettings for full schema)
    COMPACTION_STEP_CADENCE = 15       # compact every N steps (if char threshold also met)
    COMPACTION_CHAR_THRESHOLD = 40_000 # minimum total chars before compaction kicks in
    COMPACTION_KEEP_LAST = 5           # always keep last N messages uncompacted
    COMPACTION_SUMMARY_MAX = 2_000     # max chars for the LLM summary

    # Retry
    MAX_CONSECUTIVE_FAILURES = 3

    # FSM deadlines (ms) — states that take longer than this trigger stuck detection
    FSM_DEADLINES: dict[str, int] = {
        "LAUNCHING": 60_000,
        "OBSERVING": 30_000,
        "ACTING": 30_000,
        "RECOVERING": 15_000,
        "TEARING_DOWN": 10_000,
    }

    # Sensitive mode rate limits (actions per minute per platform)
    SENSITIVE_RATE_LIMITS: dict[str, int] = {
        "default": 8,
        "linkedin.com": 4,
        "facebook.com": 5,
        "twitter.com": 6,
        "x.com": 6,
        "instagram.com": 4,
    }
    SENSITIVE_WARM_DELAY_MS = 3_000  # min delay between first actions in sensitive mode

    # Server auth — set BROWSER_USE_TOKEN env var to enable
    AUTH_TOKEN = os.getenv("BROWSER_USE_TOKEN", "")

    # Server bind — default to localhost for security
    DEFAULT_HOST = "127.0.0.1"

    # Session GC
    SESSION_IDLE_TTL = 3600  # seconds before idle session is reaped
    SESSION_SWEEP_INTERVAL = 60  # seconds between GC sweeps
    MAX_SESSIONS = 10

    # Evaluate gating
    EVALUATE_ENABLED = os.getenv("BROWSER_USE_EVALUATE", "1") == "1"

    # Humanization — global default (can be overridden per-launch)
    HUMANIZE_ACTIONS = os.getenv("BROWSER_USE_HUMANIZE", "0") == "1"

    # Geo profile — timezone/locale correlation for stealth
    GEO = os.getenv("BROWSER_USE_GEO", "")

    # WebMCP — structured tool discovery on Chrome 146+ pages
    # "auto" = detect at runtime, "1" = force Chrome channel + flag, "0" = disable
    WEBMCP_ENABLED = os.getenv("BROWSER_USE_WEBMCP", "auto")
    # Chrome channel for WebMCP ("chrome-dev", "chrome-beta", "chrome-canary", "chrome")
    CHROME_CHANNEL = os.getenv("BROWSER_USE_CHROME_CHANNEL", "")
    # Explicit Chrome executable path (overrides CHROME_CHANNEL)
    CHROME_EXECUTABLE = os.getenv("BROWSER_USE_CHROME_PATH", "")

    @classmethod
    def ensure_dirs(cls) -> None:
        cls.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        cls.SESSION_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Geo profiles — timezone/locale correlation for stealth tiers
# ---------------------------------------------------------------------------

GEO_PROFILES: dict[str, dict[str, Any]] = {
    "us": {"timezone": "America/New_York", "locale": "en-US"},
    "us-ny": {"timezone": "America/New_York", "locale": "en-US"},
    "us-la": {"timezone": "America/Los_Angeles", "locale": "en-US"},
    "us-tx": {"timezone": "America/Chicago", "locale": "en-US"},
    "de": {"timezone": "Europe/Berlin", "locale": "de-DE"},
    "uk": {"timezone": "Europe/London", "locale": "en-GB"},
    "fr": {"timezone": "Europe/Paris", "locale": "fr-FR"},
    "jp": {"timezone": "Asia/Tokyo", "locale": "ja-JP"},
    "cn": {"timezone": "Asia/Shanghai", "locale": "zh-CN"},
    "au": {"timezone": "Australia/Sydney", "locale": "en-AU"},
    "br": {"timezone": "America/Sao_Paulo", "locale": "pt-BR"},
    "in": {"timezone": "Asia/Kolkata", "locale": "en-IN"},
}


def get_geo_config() -> dict[str, str]:
    """Get timezone/locale from BROWSER_USE_GEO env var.

    Returns dict with 'timezone' and 'locale' keys.
    Falls back to America/New_York + en-US if not set.
    """
    geo = Config.GEO
    if geo and geo in GEO_PROFILES:
        return GEO_PROFILES[geo]
    return {"timezone": "America/New_York", "locale": "en-US"}
