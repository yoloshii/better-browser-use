"""Error types and AI-friendly error transformation for browser-use skill.

Ported patterns:
- BrowserError with 3-level Recoverability from browser-ai CohesiumAI
- Error catalog with agent_action/user_action guidance
- AI-friendly message transforms for Playwright exceptions
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from models import AgentStateName, Recoverability


# ---------------------------------------------------------------------------
# Typed error
# ---------------------------------------------------------------------------

@dataclass
class BrowserError:
    """Structured error with recoverability and actionable guidance.

    Mirrors browser-ai's BrowserAIError interface:
    - code: stable identifier for programmatic matching
    - message: human/agent readable description
    - recoverability: RECOVERABLE → retry, ESCALATABLE → tier/strategy change, NON_RECOVERABLE → abort
    - agent_action: what the agent should do next
    - user_action: what to tell the user if surfaced
    """
    code: str
    message: str
    recoverability: Recoverability = Recoverability.NON_RECOVERABLE
    agent_action: str = ""
    user_action: str = ""
    at_state: AgentStateName | None = None
    cause: Any = None
    details: dict[str, Any] = field(default_factory=dict)
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    @property
    def is_recoverable(self) -> bool:
        return self.recoverability == Recoverability.RECOVERABLE

    @property
    def is_escalatable(self) -> bool:
        return self.recoverability == Recoverability.ESCALATABLE

    def to_agent_message(self) -> str:
        """Format for agent consumption (included in action results)."""
        parts = [self.message]
        if self.agent_action:
            parts.append(f"Suggested: {self.agent_action}")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "recoverability": self.recoverability.value,
            "agent_action": self.agent_action,
            "user_action": self.user_action,
            "at_state": self.at_state.value if self.at_state else None,
            "timestamp_ms": self.timestamp_ms,
        }


# ---------------------------------------------------------------------------
# Error catalog — stable codes with default recoverability + guidance
# ---------------------------------------------------------------------------

_CATALOG: dict[str, dict[str, Any]] = {
    # Timeout
    "TIMEOUT_ACTION": {
        "recoverability": Recoverability.RECOVERABLE,
        "agent_action": "Take a new snapshot to verify element exists, then retry.",
        "user_action": "Page may be slow — the agent will retry.",
    },
    "TIMEOUT_NAVIGATION": {
        "recoverability": Recoverability.RECOVERABLE,
        "agent_action": "Check URL, wait for load, retry navigation.",
        "user_action": "Site may be slow to respond.",
    },
    # Element issues
    "ELEMENT_NOT_VISIBLE": {
        "recoverability": Recoverability.RECOVERABLE,
        "agent_action": "Scroll element into view or dismiss overlays, then retry.",
    },
    "ELEMENT_DETACHED": {
        "recoverability": Recoverability.RECOVERABLE,
        "agent_action": "Take a new snapshot — page content changed.",
    },
    "ELEMENT_NOT_FOUND": {
        "recoverability": Recoverability.RECOVERABLE,
        "agent_action": "Take a new snapshot. Ref may be stale.",
    },
    # Page lifecycle
    "FRAME_DETACHED": {
        "recoverability": Recoverability.RECOVERABLE,
        "agent_action": "Take a new snapshot — iframe navigated away.",
    },
    "CONTEXT_DESTROYED": {
        "recoverability": Recoverability.RECOVERABLE,
        "agent_action": "Page navigated during action. Snapshot the new page.",
    },
    "TARGET_CLOSED": {
        "recoverability": Recoverability.ESCALATABLE,
        "agent_action": "Tab/context closed. Relaunch session or switch tab.",
        "user_action": "Browser tab was closed unexpectedly.",
    },
    # Network
    "NETWORK_ERROR": {
        "recoverability": Recoverability.ESCALATABLE,
        "agent_action": "Check URL. If blocked, escalate stealth tier.",
        "user_action": "Site may be blocking access.",
    },
    # Anti-bot
    "CHALLENGE_DETECTED": {
        "recoverability": Recoverability.ESCALATABLE,
        "agent_action": "Escalate to higher stealth tier.",
        "user_action": "Site has anti-bot protection — escalating stealth.",
    },
    "CAPTCHA_DETECTED": {
        "recoverability": Recoverability.ESCALATABLE,
        "agent_action": "CAPTCHA detected. Escalate tier or wait and retry.",
        "user_action": "Site is showing a CAPTCHA challenge.",
    },
    # Rate limiting
    "RATE_LIMITED": {
        "recoverability": Recoverability.RECOVERABLE,
        "agent_action": "Wait before retrying. Reduce action frequency on this domain.",
        "user_action": "Pausing to avoid rate limiting on this site.",
    },
    # Browser crash
    "BROWSER_CRASHED": {
        "recoverability": Recoverability.NON_RECOVERABLE,
        "agent_action": "Relaunch browser session from scratch.",
        "user_action": "Browser process crashed. Restarting.",
    },
    # FSM
    "INVALID_TRANSITION": {
        "recoverability": Recoverability.NON_RECOVERABLE,
        "agent_action": "Internal error — invalid state transition.",
    },
    "DEADLINE_EXCEEDED": {
        "recoverability": Recoverability.ESCALATABLE,
        "agent_action": "State timed out. Evaluate and recover.",
    },
    # Budget
    "STEP_BUDGET_EXCEEDED": {
        "recoverability": Recoverability.NON_RECOVERABLE,
        "agent_action": "Maximum steps reached. Report progress and stop.",
        "user_action": "Task hit step limit. Review partial results.",
    },
    # Generic
    "UNKNOWN": {
        "recoverability": Recoverability.NON_RECOVERABLE,
        "agent_action": "Take a snapshot to assess state.",
    },
}


def create_error(
    code: str,
    message: str,
    *,
    at_state: AgentStateName | None = None,
    cause: Any = None,
    details: dict[str, Any] | None = None,
    recoverability: Recoverability | None = None,
) -> BrowserError:
    """Create a BrowserError from the catalog, with optional overrides."""
    defaults = _CATALOG.get(code, _CATALOG["UNKNOWN"])
    return BrowserError(
        code=code,
        message=message,
        recoverability=recoverability or defaults["recoverability"],
        agent_action=defaults.get("agent_action", ""),
        user_action=defaults.get("user_action", ""),
        at_state=at_state,
        cause=cause,
        details=details or {},
    )


# ---------------------------------------------------------------------------
# AI-friendly transform (preserving existing behavior, now returning BrowserError)
# ---------------------------------------------------------------------------

def _extract_timeout(msg: str) -> str:
    m = re.search(r"(\d+)ms", msg)
    return m.group(1) if m else "30000"


def _extract_net_error(msg: str) -> str:
    m = re.search(r"net::(ERR_\w+)", msg)
    return m.group(1) if m else "unknown network error"


_PATTERN_MAP: list[tuple[str, str, object]] = [
    (
        "TimeoutError",
        "TIMEOUT_ACTION",
        lambda e: f"Action timed out after {_extract_timeout(str(e))}ms.",
    ),
    (
        "not visible",
        "ELEMENT_NOT_VISIBLE",
        lambda e: "Element is present but not visible (hidden by CSS, behind overlay, or off-screen).",
    ),
    (
        "detached",
        "ELEMENT_DETACHED",
        lambda e: "Element was removed from the DOM (page content changed).",
    ),
    (
        "Target closed",
        "TARGET_CLOSED",
        lambda e: "Browser tab or context was closed.",
    ),
    (
        "net::ERR_",
        "NETWORK_ERROR",
        lambda e: f"Network error: {_extract_net_error(str(e))}.",
    ),
    (
        "frame was detached",
        "FRAME_DETACHED",
        lambda e: "The iframe navigated away during the action.",
    ),
    (
        "Execution context was destroyed",
        "CONTEXT_DESTROYED",
        lambda e: "Page navigated during the action.",
    ),
    (
        "429",
        "RATE_LIMITED",
        lambda e: "Site returned HTTP 429 (Too Many Requests). Slow down.",
    ),
    (
        "captcha",
        "CAPTCHA_DETECTED",
        lambda e: "CAPTCHA detected on the page.",
    ),
]


def classify_error(
    error: Exception,
    at_state: AgentStateName | None = None,
) -> BrowserError:
    """Classify a Playwright/browser exception into a structured BrowserError."""
    msg = str(error)
    for pattern, code, msg_fn in _PATTERN_MAP:
        if pattern.lower() in msg.lower():
            return create_error(code, msg_fn(error), at_state=at_state, cause=error)
    return create_error("UNKNOWN", f"Browser error: {msg}", at_state=at_state, cause=error)


def to_ai_friendly_error(error: Exception) -> str:
    """Legacy wrapper — returns a plain string for backward compatibility."""
    return classify_error(error).to_agent_message()
