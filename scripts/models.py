"""Data models for browser-use skill.

Enums and models used across the skill. Ported patterns:
- CompactionSettings from browser-use v0.11.9 MessageCompactionSettings
- 3-level Recoverability from browser-ai CohesiumAI
- 11-state AgentStateName from browser-ai FSM
- BrowsingMode for sensitive site handling
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class BrowsingMode(str, Enum):
    """Controls action pacing and stealth behavior."""
    EXTRACT = "extract"       # default — fast extraction, normal pacing
    SENSITIVE = "sensitive"   # rate-governed, warming schedule, fingerprint lock


class Recoverability(str, Enum):
    """3-level error recoverability (browser-ai pattern)."""
    RECOVERABLE = "recoverable"          # retry same action
    ESCALATABLE = "escalatable"          # escalate tier or strategy
    NON_RECOVERABLE = "non_recoverable"  # abort task


class AgentStateName(str, Enum):
    """FSM states for the agent loop."""
    IDLE = "IDLE"
    LAUNCHING = "LAUNCHING"
    OBSERVING = "OBSERVING"
    PLANNING = "PLANNING"
    ACTING = "ACTING"
    EVALUATING = "EVALUATING"
    ESCALATING = "ESCALATING"
    RECOVERING = "RECOVERING"
    DONE = "DONE"
    ERROR = "ERROR"
    TEARING_DOWN = "TEARING_DOWN"


# ---------------------------------------------------------------------------
# Core models
# ---------------------------------------------------------------------------

class Session(BaseModel):
    id: str
    tier: int = 1
    profile: str | None = None
    url: str | None = None
    title: str | None = None
    pid: int | None = None
    epoch: int = 0
    mode: BrowsingMode = BrowsingMode.EXTRACT

    model_config = {"json_schema_extra": {"examples": [
        {"id": "s-abc123", "tier": 1, "epoch": 0, "mode": "extract"}
    ]}}


class ActionRequest(BaseModel):
    action: str
    params: dict = Field(default_factory=dict)
    session_id: str
    humanize: float | None = None

    model_config = {"json_schema_extra": {"examples": [
        {"action": "click", "params": {"ref": "@e3"}, "session_id": "s-abc123"}
    ]}}


class ActionResult(BaseModel):
    success: bool
    extracted_content: str | None = None
    error: str | None = None
    page_changed: bool = False
    new_url: str | None = None
    new_title: str | None = None
    screenshot: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SnapshotResult(BaseModel):
    success: bool
    tree: str = ""
    refs: dict[str, dict] = Field(default_factory=dict)
    url: str = ""
    title: str = ""
    tab_count: int = 1
    error: str | None = None


class Profile(BaseModel):
    name: str
    domain: str
    tier: int = 1
    mode: BrowsingMode = BrowsingMode.EXTRACT
    created: str
    last_used: str
    has_cookies: bool = False
    has_storage: bool = False
    has_fingerprint: bool = False
    proxy: str | None = None


class RefEntry(BaseModel):
    """A single ref in the ref map."""
    role: str
    name: str | None = None
    selector: str
    nth: int | None = None


class ChallengeInfo(BaseModel):
    """Detected anti-bot challenge."""
    name: str
    min_tier: int
    detail: str = ""


# ---------------------------------------------------------------------------
# Context compaction settings (browser-use v0.11.9 pattern)
# ---------------------------------------------------------------------------

class CompactionSettings(BaseModel):
    """Controls when and how agent history is compacted.

    Ported from browser-use MessageCompactionSettings. Compaction triggers
    when step_count >= step_cadence AND total_chars >= char_threshold.
    """
    step_cadence: int = Field(default=15, ge=3)
    char_threshold: int = Field(default=40_000, ge=5_000)
    keep_last: int = Field(default=5, ge=1)
    summary_max_chars: int = Field(default=2_000, ge=200)

    @model_validator(mode="after")
    def _keep_last_reasonable(self) -> CompactionSettings:
        if self.keep_last >= self.step_cadence:
            self.keep_last = max(1, self.step_cadence - 2)
        return self


# ---------------------------------------------------------------------------
# FSM snapshot (for status/diagnostics)
# ---------------------------------------------------------------------------

class FSMState(BaseModel):
    """Lightweight snapshot of agent FSM state for status reporting."""
    name: AgentStateName
    since_ms: int
    deadline_ms: int | None = None
    epoch: int = 0


# ---------------------------------------------------------------------------
# Loop detection (ported from browser-use ActionLoopDetector pattern)
# ---------------------------------------------------------------------------

class PageFingerprint(BaseModel):
    """Lightweight page identity for stagnation detection."""
    url_hash: str
    interactive_count: int
    tab_count: int
    top_ref_keys: tuple[str, ...] = ()

    model_config = {"frozen": True}

    @classmethod
    def from_snapshot(
        cls,
        url: str,
        refs: dict[str, dict],
        tab_count: int,
    ) -> PageFingerprint:
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        sorted_keys = sorted(refs.keys())[:10]
        ref_keys = tuple(
            f"{refs[k].get('role', '')}:{refs[k].get('name') or ''}:{refs[k].get('nth', '')}"
            for k in sorted_keys
        )
        return cls(
            url_hash=url_hash,
            interactive_count=len(refs),
            tab_count=tab_count,
            top_ref_keys=ref_keys,
        )

    def similarity(self, other: PageFingerprint) -> float:
        """0.0 = completely different, 1.0 = identical."""
        if self.url_hash != other.url_hash:
            return 0.0
        score = 0.5
        if self.tab_count == other.tab_count:
            score += 0.1
        if self.interactive_count == other.interactive_count:
            score += 0.1
        if self.top_ref_keys and other.top_ref_keys:
            overlap = len(set(self.top_ref_keys) & set(other.top_ref_keys))
            max_len = max(len(self.top_ref_keys), len(other.top_ref_keys))
            score += 0.3 * (overlap / max_len) if max_len else 0
        return min(score, 1.0)


def compute_action_hash(action_name: str, params: dict) -> str:
    """Deterministic hash of action name + normalized parameters."""
    stable = {
        k: v for k, v in sorted(params.items())
        if k not in ("session_id", "timestamp")
    }
    raw = f"{action_name}:{json.dumps(stable, sort_keys=True, default=str)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class ActionLoopDetector:
    """Detects repetitive action patterns and stuck agents.

    Rolling window of (action_hash, page_fingerprint) tuples.
    Returns escalating warning strings when the same action+page pair
    appears >= threshold times in the window.
    """

    def __init__(self, window_size: int = 10, threshold: int = 3):
        self._window: deque[tuple[str, PageFingerprint | None]] = deque(maxlen=window_size)
        self._threshold = threshold

    def record(
        self,
        action_name: str,
        params: dict,
        fingerprint: PageFingerprint | None = None,
    ) -> str | None:
        """Record an action. Returns a warning string if loop detected."""
        action_hash = compute_action_hash(action_name, params)
        self._window.append((action_hash, fingerprint))

        count = sum(1 for h, _ in self._window if h == action_hash)
        if count < self._threshold:
            return None

        if fingerprint:
            fp_matches = sum(
                1 for h, fp in self._window
                if h == action_hash and fp and fingerprint.similarity(fp) > 0.8
            )
        else:
            fp_matches = count

        if fp_matches < self._threshold:
            return None

        if count >= self._threshold + 4:
            return (
                f"CRITICAL: Action '{action_name}' repeated {count} times. "
                "You are in an infinite loop. Call done immediately with partial results."
            )
        elif count >= self._threshold + 2:
            return (
                f"STUCK: Action '{action_name}' repeated {count} times. "
                "Current approach is not working. Try: "
                "1) navigate to a different URL, 2) use evaluate to inspect the DOM, "
                "3) call done with partial results."
            )
        else:
            return (
                f"WARNING: Action '{action_name}' repeated {count} times on same page. "
                "Try a different approach — scroll, use a different element, or navigate elsewhere."
            )

    def reset(self) -> None:
        """Clear the window (e.g., after navigation to new domain)."""
        self._window.clear()
