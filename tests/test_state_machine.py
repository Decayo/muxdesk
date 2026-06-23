"""Session state machine: startup phase set by probe, interactive phase derived from transcript."""
from __future__ import annotations

from muxdesk import SessionState
from muxdesk.state_machine import SessionStateMachine


def test_initial_state_is_starting():
    sm = SessionStateMachine()
    assert sm.state == SessionState.STARTING
    assert sm.can_accept_input() is False


def test_ready_gate():
    sm = SessionStateMachine()
    sm.force(SessionState.READY)
    assert sm.can_accept_input() is True


def test_non_interactive_phase_ignores_transcript_events():
    sm = SessionStateMachine()  # STARTING (non-interactive)
    assert sm.on_event("assistant_message", {}) is None
    assert sm.state == SessionState.STARTING


def test_full_interactive_turn():
    sm = SessionStateMachine()
    sm.force(SessionState.READY)
    assert sm.on_submitting() == SessionState.SUBMITTING
    assert sm.on_event("assistant_message", {}) == SessionState.ASSISTANT_STREAMING
    assert sm.on_event("tool_start", {"tool_use_id": "t1"}) == SessionState.RUNNING_TOOL
    assert sm.on_event("tool_end", {"tool_use_id": "t1"}) == SessionState.ASSISTANT_STREAMING
    assert sm.on_event("system_notice", {"subtype": "turn_duration"}) == SessionState.READY


def test_turn_end_blocked_while_tool_pending():
    sm = SessionStateMachine()
    sm.force(SessionState.READY)
    sm.on_event("tool_start", {"tool_use_id": "t1"})
    # turn_duration arrives but tool still pending → stays RUNNING_TOOL
    assert sm.on_event("system_notice", {"subtype": "turn_duration"}) is None
    assert sm.state == SessionState.RUNNING_TOOL


def test_interrupt_converges_busy_to_ready():
    sm = SessionStateMachine()
    sm.force(SessionState.READY)
    sm.on_submitting()
    assert sm.on_interrupt() == SessionState.READY


def test_interrupt_is_noop_during_startup():
    sm = SessionStateMachine()  # STARTING
    assert sm.on_interrupt() is None
    assert sm.state == SessionState.STARTING


def test_manual_takeover_freezes_then_resumes():
    sm = SessionStateMachine()
    sm.force(SessionState.READY)
    sm.on_manual_takeover()
    assert sm.state == SessionState.MANUAL_TAKEOVER
    assert sm.on_event("assistant_message", {}) is None  # frozen during takeover
    assert sm.on_resume_automation() == SessionState.READY


def test_no_state_change_returns_none():
    sm = SessionStateMachine()
    sm.force(SessionState.READY)
    sm.on_submitting()
    assert sm.on_submitting() is None  # already SUBMITTING


def test_idle_hint_converges_streaming_to_ready():
    sm = SessionStateMachine()
    sm.force(SessionState.READY)
    sm.on_submitting()
    sm.on_event("assistant_message", {})  # ASSISTANT_STREAMING
    assert sm.on_idle_hint() == SessionState.READY
