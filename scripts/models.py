"""Data models for browser-use skill.

Enums and models used across the skill. Ported patterns:
- CompactionSettings from browser-use v0.11.9 MessageCompactionSettings
- 3-level Recoverability from browser-ai CohesiumAI
- 11-state AgentStateName from browser-ai FSM
- BrowsingMode for sensitive site handling
"""

from __future__ import annotations

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
