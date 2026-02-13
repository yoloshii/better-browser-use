"""Sliding-window rate limiter for sensitive site protection.

Enforces Config.SENSITIVE_RATE_LIMITS (actions per minute per domain).
Read-only operations (snapshot, screenshot) are exempt.
"""

from __future__ import annotations

import time
from collections import deque

from config import Config


# Actions that don't count toward rate limits (read-only)
EXEMPT_ACTIONS = frozenset({
    "snapshot", "screenshot", "wait", "done",
    "cookies_get", "tab_switch",
})


class RateLimiter:
    """Per-domain sliding window rate limiter."""

    def __init__(self, limits: dict[str, int] | None = None):
        self._limits = limits or Config.SENSITIVE_RATE_LIMITS
        self._windows: dict[str, deque[float]] = {}

    def _get_limit(self, domain: str) -> int:
        """Get rate limit for a domain (actions per minute)."""
        for pattern, limit in self._limits.items():
            if pattern != "default" and pattern in domain:
                return limit
        return self._limits.get("default", 8)

    def _get_window(self, domain: str) -> deque[float]:
        if domain not in self._windows:
            self._windows[domain] = deque()
        return self._windows[domain]

    def _prune(self, window: deque[float], now: float) -> None:
        """Remove timestamps older than 60 seconds."""
        cutoff = now - 60.0
        while window and window[0] < cutoff:
            window.popleft()

    def check(self, domain: str) -> bool:
        """Check if an action is allowed (True = allowed, False = rate limited)."""
        now = time.monotonic()
        window = self._get_window(domain)
        self._prune(window, now)
        limit = self._get_limit(domain)
        return len(window) < limit

    def record(self, domain: str) -> None:
        """Record an action for rate limiting."""
        now = time.monotonic()
        window = self._get_window(domain)
        self._prune(window, now)
        window.append(now)

    def wait_time(self, domain: str) -> float:
        """Seconds until the next action is allowed. 0 if allowed now."""
        now = time.monotonic()
        window = self._get_window(domain)
        self._prune(window, now)
        limit = self._get_limit(domain)

        if len(window) < limit:
            return 0.0

        # Oldest entry expires at oldest + 60s
        return max(0.0, window[0] + 60.0 - now)


# Module-level singleton
_instance: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Get the global RateLimiter singleton."""
    global _instance
    if _instance is None:
        _instance = RateLimiter()
    return _instance
