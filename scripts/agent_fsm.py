"""Agent state machine — 11 states with typed transitions, deadlines, listeners.

Ported from browser-ai's StateMachine (state-machine.ts + transitions.ts).
Adapted from browser-runtime states to agent-loop states.

States:
  IDLE → LAUNCHING → OBSERVING → PLANNING → ACTING → EVALUATING
  ESCALATING, RECOVERING, DONE, ERROR, TEARING_DOWN

Key differences from browser-ai:
- forceTransition() used for ERROR and TEARING_DOWN (any-state entry)
- Epoch tracking: increments on abort/tier escalation, stale events discarded
- Deadlines loaded from Config.FSM_DEADLINES
"""

from __future__ import annotations

import time
from typing import Callable

from config import Config
from errors import BrowserError, create_error
from models import AgentStateName, FSMState

# Type alias for state change listeners
StateChangeListener = Callable[[FSMState, FSMState], None]

# ---------------------------------------------------------------------------
# Valid transitions (analogous to browser-ai VALID_TRANSITIONS)
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[AgentStateName, list[AgentStateName]] = {
    AgentStateName.IDLE: [
        AgentStateName.LAUNCHING,
    ],
    AgentStateName.LAUNCHING: [
        AgentStateName.OBSERVING,
        AgentStateName.ERROR,
    ],
    AgentStateName.OBSERVING: [
        AgentStateName.PLANNING,
        AgentStateName.ERROR,
    ],
    AgentStateName.PLANNING: [
        AgentStateName.ACTING,
        AgentStateName.DONE,       # agent decides task is complete from plan
        AgentStateName.ERROR,
    ],
    AgentStateName.ACTING: [
        AgentStateName.EVALUATING,
        AgentStateName.ERROR,
    ],
    AgentStateName.EVALUATING: [
        AgentStateName.OBSERVING,   # normal loop: evaluate → re-observe
        AgentStateName.ESCALATING,  # needs tier escalation
        AgentStateName.DONE,        # task complete
        AgentStateName.ERROR,
    ],
    AgentStateName.ESCALATING: [
        AgentStateName.LAUNCHING,   # relaunch at higher tier
        AgentStateName.ERROR,
    ],
    AgentStateName.RECOVERING: [
        AgentStateName.OBSERVING,   # recovered, re-observe
        AgentStateName.ESCALATING,  # recovery failed, escalate
        AgentStateName.ERROR,
    ],
    AgentStateName.DONE: [
        AgentStateName.TEARING_DOWN,
        AgentStateName.IDLE,        # reset for new task
    ],
    AgentStateName.ERROR: [
        AgentStateName.RECOVERING,
        AgentStateName.TEARING_DOWN,
        AgentStateName.IDLE,
    ],
    AgentStateName.TEARING_DOWN: [
        AgentStateName.IDLE,
    ],
}

# States that can be aborted (cancel in-progress work)
ABORTABLE_STATES: set[AgentStateName] = {
    AgentStateName.OBSERVING,
    AgentStateName.PLANNING,
    AgentStateName.ACTING,
    AgentStateName.EVALUATING,
    AgentStateName.ESCALATING,
    AgentStateName.RECOVERING,
}


def is_valid_transition(from_state: AgentStateName, to_state: AgentStateName) -> bool:
    allowed = VALID_TRANSITIONS.get(from_state, [])
    return to_state in allowed


def can_abort(state: AgentStateName) -> bool:
    return state in ABORTABLE_STATES


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class AgentFSM:
    """Agent state machine with typed transitions, deadlines, and epoch tracking.

    Usage:
        fsm = AgentFSM()
        fsm.subscribe(my_listener)
        fsm.to_launching()
        fsm.to_observing()
        ...
    """

    def __init__(self) -> None:
        self._state = FSMState(
            name=AgentStateName.IDLE,
            since_ms=_now_ms(),
            epoch=0,
        )
        self._listeners: list[StateChangeListener] = []

    @property
    def state(self) -> FSMState:
        return self._state

    @property
    def state_name(self) -> AgentStateName:
        return self._state.name

    @property
    def epoch(self) -> int:
        return self._state.epoch

    def subscribe(self, listener: StateChangeListener) -> Callable[[], None]:
        """Register a state change listener. Returns unsubscribe function."""
        self._listeners.append(listener)
        def unsub() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass
        return unsub

    def snapshot(self) -> FSMState:
        """Return a copy of current state for diagnostics."""
        return self._state.model_copy()

    def is_terminal(self) -> bool:
        return self._state.name in (AgentStateName.DONE, AgentStateName.ERROR)

    def is_active(self) -> bool:
        return self._state.name not in (
            AgentStateName.IDLE,
            AgentStateName.DONE,
            AgentStateName.ERROR,
            AgentStateName.TEARING_DOWN,
        )

    def elapsed_ms(self) -> int:
        return _now_ms() - self._state.since_ms

    def is_deadline_exceeded(self) -> bool:
        if self._state.deadline_ms is None:
            return False
        return self.elapsed_ms() > self._state.deadline_ms

    # -- Transitions (validated) ------------------------------------------

    def to_launching(self) -> None:
        self._transition(AgentStateName.LAUNCHING)

    def to_observing(self) -> None:
        self._transition(AgentStateName.OBSERVING)

    def to_planning(self) -> None:
        self._transition(AgentStateName.PLANNING)

    def to_acting(self) -> None:
        self._transition(AgentStateName.ACTING)

    def to_evaluating(self) -> None:
        self._transition(AgentStateName.EVALUATING)

    def to_escalating(self) -> None:
        self._transition(AgentStateName.ESCALATING)

    def to_done(self) -> None:
        self._transition(AgentStateName.DONE)

    # -- Force transitions (any-state entry) ------------------------------

    def to_error(self, error: BrowserError | None = None) -> None:
        """Force transition to ERROR from any state."""
        self._force_transition(AgentStateName.ERROR)

    def to_recovering(self) -> None:
        """Force transition to RECOVERING from any state."""
        self._force_transition(AgentStateName.RECOVERING)

    def to_tearing_down(self) -> None:
        """Force transition to TEARING_DOWN from any state."""
        self._force_transition(AgentStateName.TEARING_DOWN)

    def to_idle(self) -> None:
        """Reset to IDLE. Only valid from DONE, ERROR, or TEARING_DOWN."""
        self._transition(AgentStateName.IDLE)

    # -- Epoch management -------------------------------------------------

    def bump_epoch(self) -> int:
        """Increment epoch (on abort, tier escalation, recovery).

        Returns the new epoch. Any in-flight work with a stale epoch
        should be discarded.
        """
        now = _now_ms()
        prev = self._state
        self._state = FSMState(
            name=prev.name,
            since_ms=prev.since_ms,
            deadline_ms=prev.deadline_ms,
            epoch=prev.epoch + 1,
        )
        self._notify(prev)
        return self._state.epoch

    # -- Internals --------------------------------------------------------

    def _transition(self, to: AgentStateName) -> None:
        prev = self._state
        if not is_valid_transition(prev.name, to):
            raise create_error(
                "INVALID_TRANSITION",
                f"Invalid transition: {prev.name.value} → {to.value}",
                at_state=prev.name,
            )
        self._set_state(to, prev.epoch)
        self._notify(prev)

    def _force_transition(self, to: AgentStateName) -> None:
        prev = self._state
        self._set_state(to, prev.epoch)
        self._notify(prev)

    def _set_state(self, name: AgentStateName, epoch: int) -> None:
        now = _now_ms()
        deadline = Config.FSM_DEADLINES.get(name.value)
        self._state = FSMState(
            name=name,
            since_ms=now,
            deadline_ms=deadline,
            epoch=epoch,
        )

    def _notify(self, prev: FSMState) -> None:
        for listener in self._listeners:
            try:
                listener(self._state, prev)
            except Exception:
                pass  # listener errors must not break FSM


def _now_ms() -> int:
    return int(time.time() * 1000)
