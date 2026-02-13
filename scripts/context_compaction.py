"""LLM-powered history summarization for long browsing sessions.

Ported from browser-use v0.11.9 MessageCompactionSettings.

Delegation model: This module doesn't call an LLM directly. Instead,
it prepares the compaction payload (messages to summarize + previous summary)
and returns it as a structured request for the calling agent to process. The LLM summary
is then fed back via inject_summary().

Gating logic:
  Compaction triggers when BOTH conditions are met:
    1. step_count >= settings.step_cadence
    2. total_chars >= settings.char_threshold

  After compaction, the step counter resets. The keep_last messages are
  never compacted (always preserved in full).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from models import CompactionSettings


@dataclass
class CompactionState:
    """Tracks compaction state across the session."""
    settings: CompactionSettings = field(default_factory=CompactionSettings)
    step_count: int = 0
    total_chars: int = 0
    previous_summary: str = ""
    compaction_count: int = 0

    def record_step(self, chars: int) -> None:
        """Record a new agent step (action + observation)."""
        self.step_count += 1
        self.total_chars += chars

    def should_compact(self) -> bool:
        """Check if compaction gates are met."""
        return (
            self.step_count >= self.settings.step_cadence
            and self.total_chars >= self.settings.char_threshold
        )


def prepare_compaction(
    state: CompactionState,
    messages: list[dict],
) -> dict | None:
    """Prepare a compaction request if gates are met.

    Args:
        state: Current compaction state.
        messages: Full message history (list of {role, content} dicts).

    Returns:
        A compaction request dict for the calling agent to process, or None if
        compaction is not needed yet.

        The request contains:
        - messages_to_summarize: the older messages that should be summarized
        - keep_messages: the recent messages to preserve in full
        - previous_summary: the prior compaction summary (for incremental context)
        - summary_max_chars: maximum length for the new summary
    """
    if not state.should_compact():
        return None

    if len(messages) <= state.settings.keep_last:
        return None

    keep_count = state.settings.keep_last
    to_summarize = messages[:-keep_count]
    keep = messages[-keep_count:]

    return {
        "action": "compact_history",
        "messages_to_summarize": to_summarize,
        "keep_messages": keep,
        "previous_summary": state.previous_summary,
        "summary_max_chars": state.settings.summary_max_chars,
        "compaction_number": state.compaction_count + 1,
    }


def inject_summary(
    state: CompactionState,
    summary: str,
    keep_messages: list[dict],
) -> list[dict]:
    """Inject an LLM-generated summary and return the new compacted history.

    Called after the calling agent processes the compaction request and returns a summary.

    Args:
        state: Current compaction state (will be mutated).
        summary: The LLM-generated summary of older messages.
        keep_messages: The recent messages that were preserved.

    Returns:
        New message list: [summary_message] + keep_messages
    """
    state.previous_summary = summary
    state.compaction_count += 1
    state.step_count = 0

    # Recalculate total_chars from kept messages only
    state.total_chars = sum(len(m.get("content", "")) for m in keep_messages)

    summary_message = {
        "role": "system",
        "content": (
            f"[Session summary — compaction #{state.compaction_count}]\n\n"
            f"{summary}"
        ),
    }

    return [summary_message] + keep_messages
