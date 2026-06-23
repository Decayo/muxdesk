from __future__ import annotations

from enum import Enum


class SessionState(str, Enum):
    # Startup phase (set by session_manager's readiness probe via capture-pane)
    STARTING = "STARTING"  # tmux started claude, still initializing (loading / auth / MCP)
    BLOCKED_INTERACTIVE = "BLOCKED_INTERACTIVE"  # trust prompt / login / OAuth, needs manual resolution in terminal
    READY = "READY"  # handoff complete, safe to accept user input
    ERROR = "ERROR"  # startup or runtime fatal (401 / invalid model / process exit / timeout)
    TERMINATED = "TERMINATED"
    # Interactive phase (derived from transcript events)
    SUBMITTING = "SUBMITTING"
    ASSISTANT_STREAMING = "ASSISTANT_STREAMING"
    WAITING_TOOL_PERMISSION = "WAITING_TOOL_PERMISSION"
    RUNNING_TOOL = "RUNNING_TOOL"
    MANUAL_TAKEOVER = "MANUAL_TAKEOVER"
    UNKNOWN = "UNKNOWN"


# Startup / blocked / error phases: not derived from transcript events (prevents startup-phase raw_events from overriding probe decisions)
_NON_INTERACTIVE = frozenset(
    {
        SessionState.STARTING,
        SessionState.BLOCKED_INTERACTIVE,
        SessionState.ERROR,
        SessionState.TERMINATED,
    }
)


class SessionStateMachine:
    """Session state machine: startup phase set by readiness probe (capture-pane), interactive phase derived from transcript.

    READY replaces the old IDLE as the stable "can accept input" state; returns to READY when a turn ends.
    """

    def __init__(self) -> None:
        self._state = SessionState.STARTING  # Initial state is STARTING (no longer UNKNOWN)
        self._pending_tools: set[str] = set()

    @property
    def state(self) -> SessionState:
        return self._state

    def _set(self, state: SessionState) -> SessionState | None:
        if state != self._state:
            self._state = state
            return state
        return None

    def force(self, state: SessionState) -> SessionState | None:
        """For readiness probe only: directly set startup / blocked / error states."""
        return self._set(state)

    def can_accept_input(self) -> bool:
        """Ready gate: only READY state accepts frontend injection (injecting before ready would be lost or pollute trust/login screens)."""
        return self._state == SessionState.READY

    def on_submitting(self) -> SessionState | None:
        self._pending_tools.clear()
        return self._set(SessionState.SUBMITTING)

    def on_event(self, event_type: str, payload: dict) -> SessionState | None:
        if self._state == SessionState.MANUAL_TAKEOVER:
            return None  # During human takeover, don't auto-change state; wait for explicit resume
        if self._state in _NON_INTERACTIVE:
            return None  # Startup / blocked / error phases are not derived from transcript
        if event_type in ("assistant_message", "assistant_thinking"):
            return self._set(SessionState.ASSISTANT_STREAMING)
        if event_type == "tool_start":
            tool_id = payload.get("tool_use_id")
            if tool_id:
                self._pending_tools.add(tool_id)
            return self._set(SessionState.RUNNING_TOOL)
        if event_type == "tool_end":
            self._pending_tools.discard(payload.get("tool_use_id"))
            return self._set(SessionState.ASSISTANT_STREAMING)
        if event_type == "system_notice":
            # Turn ended (turn_duration fires every turn / stop_hook_summary) -> return to READY
            subtype = payload.get("subtype")
            is_turn_end = subtype in ("turn_duration", "stop_hook_summary") or bool(payload.get("stop_reason"))
            if is_turn_end and not self._pending_tools:
                return self._set(SessionState.READY)
        return None

    def on_interrupt(self) -> SessionState | None:
        """User pressed stop (Escape) -> cancel the current turn: clear pending tools, converge interactive busy states back to READY.

        Interrupts don't emit turn_duration / tool_end, so state won't return to READY from transcript alone; we converge explicitly here,
        otherwise the frontend stays stuck in busy (stop button won't disappear). Startup / blocked / error phases are not affected (Escape may just close a trust dialog).
        """
        self._pending_tools.clear()
        if self._state in (
            SessionState.SUBMITTING,
            SessionState.ASSISTANT_STREAMING,
            SessionState.RUNNING_TOOL,
            SessionState.WAITING_TOOL_PERMISSION,
        ):
            return self._set(SessionState.READY)
        return None

    def on_manual_takeover(self) -> SessionState | None:
        self._pending_tools.clear()
        return self._set(SessionState.MANUAL_TAKEOVER)

    def on_resume_automation(self) -> SessionState | None:
        return self._set(SessionState.READY)

    def on_idle_hint(self) -> SessionState | None:
        """capture-pane fallback: converge to READY when an input-waiting prompt is detected."""
        if self._state in (SessionState.ASSISTANT_STREAMING, SessionState.SUBMITTING) and not self._pending_tools:
            return self._set(SessionState.READY)
        return None
