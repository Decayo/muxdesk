"""Codex rollout jsonl → semantic events.

Mirrors ``test_transcript_parser.py`` for Claude: each test feeds one record
dict into :class:`~muxdesk.parsers.codex.CodexParser` and asserts the emitted
``{event_type, payload}`` events. Record shapes are lifted verbatim from real
codex rollouts (codex-cli 0.107–0.142).
"""
from __future__ import annotations

from muxdesk.parsers.codex import CodexParser


def _events(record: dict) -> list[dict]:
    return list(CodexParser().parse_record(record))


def test_extract_metadata_only_from_session_meta():
    parser = CodexParser()
    assert parser.extract_metadata({"type": "event_msg"}) == {}
    meta_record = {
        "type": "session_meta",
        "payload": {
            "id": "019cb8aa-a429-7872-8605-27fcfb41009c",
            "cwd": "/home/yurem",
            "cli_version": "0.142.3",
        },
    }
    assert parser.extract_metadata(meta_record) == {
        "session_id": "019cb8aa-a429-7872-8605-27fcfb41009c",
        "cwd": "/home/yurem",
        "version": "0.142.3",
        "git_branch": None,
    }


def test_user_message_event_msg():
    rec = {
        "type": "event_msg",
        "payload": {"type": "user_message", "message": "hello codex", "images": [], "local_images": []},
    }
    [ev] = _events(rec)
    assert ev["event_type"] == "user_message"
    assert ev["payload"]["text"] == "hello codex"
    assert ev["payload"]["role"] == "user"


def test_user_message_surfaces_attached_images():
    rec = {
        "type": "event_msg",
        "payload": {
            "type": "user_message",
            "message": "look at this",
            "images": [{"file_id": "file-abc"}],
            "local_images": ["/tmp/cc-desk-img/x.png"],
        },
    }
    [ev] = _events(rec)
    assert ev["event_type"] == "user_message"
    # Both image fields are surfaced so the frontend can resolve uploads.
    assert ev["payload"]["images"] == [{"file_id": "file-abc"}, "/tmp/cc-desk-img/x.png"]


def test_agent_message_becomes_assistant_message():
    rec = {
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": "sure, here is the plan"},
    }
    [ev] = _events(rec)
    assert ev["event_type"] == "assistant_message"
    assert ev["payload"]["text"] == "sure, here is the plan"
    assert ev["payload"]["role"] == "assistant"


def test_agent_reasoning_becomes_assistant_thinking():
    rec = {
        "type": "event_msg",
        "payload": {"type": "agent_reasoning", "text": "Considering options..."},
    }
    [ev] = _events(rec)
    assert ev["event_type"] == "assistant_thinking"
    assert ev["payload"]["text"] == "Considering options..."


def test_task_started_yields_nothing():
    rec = {
        "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": "t1", "model_context_window": 258400},
    }
    assert _events(rec) == []


def test_task_complete_emits_turn_end_system_notice():
    rec = {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": "t1",
            "last_agent_message": "done",
            "duration_ms": 1234,
        },
    }
    [ev] = _events(rec)
    # Must carry a turn-end marker the state machine recognizes (subtype in
    # turn_duration/stop_hook_summary OR stop_reason present) so a codex turn
    # converges back to READY like Claude's stop_reason notice.
    assert ev["event_type"] == "system_notice"
    payload = ev["payload"]
    assert payload["subtype"] == "turn_duration"
    assert payload["stop_reason"] == "task_complete"


def test_turn_aborted_emits_interrupted_notice():
    rec = {"type": "event_msg", "payload": {"type": "turn_aborted"}}
    [ev] = _events(rec)
    assert ev["event_type"] == "system_notice"
    assert ev["payload"]["subtype"] == "interrupted"


def test_function_call_becomes_tool_start_with_parsed_args():
    rec = {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": '{"cmd":"uname -a","max_output_tokens":2000}',
            "call_id": "call_O1sUYCawzXa4Mo5z4JBofXtf",
        },
    }
    [ev] = _events(rec)
    assert ev["event_type"] == "tool_start"
    assert ev["payload"]["tool_name"] == "exec_command"
    assert ev["payload"]["tool_use_id"] == "call_O1sUYCawzXa4Mo5z4JBofXtf"
    # arguments arrive as a JSON string; the parser parses it so the frontend's
    # existing tool_start rendering (summarize(input)) works unchanged.
    assert ev["payload"]["input"] == {"cmd": "uname -a", "max_output_tokens": 2000}


def test_function_call_keeps_raw_args_when_unparseable():
    rec = {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": "not json{",
            "call_id": "call_x",
        },
    }
    [ev] = _events(rec)
    assert ev["event_type"] == "tool_start"
    assert ev["payload"]["input"] == "not json{"


def test_function_call_output_becomes_tool_end():
    rec = {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": "call_O1sUYCawzXa4Mo5z4JBofXtf",
            "output": "Process exited with code 0",
        },
    }
    [ev] = _events(rec)
    assert ev["event_type"] == "tool_end"
    assert ev["payload"]["tool_use_id"] == "call_O1sUYCawzXa4Mo5z4JBofXtf"
    assert ev["payload"]["content"] == "Process exited with code 0"
    assert ev["payload"]["is_error"] is False


def test_assistant_message_with_image_block_emits_image_event():
    rec = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "output_image", "image_url": "file:///tmp/out.png"},
            ],
        },
    }
    [ev] = _events(rec)
    assert ev["event_type"] == "image"
    assert ev["payload"]["source"] == "file:///tmp/out.png"


def test_reasoning_and_custom_tool_response_items_are_skipped():
    # Already surfaced via the display-faithful event_msg stream; skipping the
    # API-level duplicate avoids double-rendering.
    for item_type in ("reasoning", "custom_tool_call", "custom_tool_call_output"):
        rec = {"type": "response_item", "payload": {"type": item_type}}
        assert _events(rec) == []


def test_session_meta_and_turn_context_yield_nothing():
    for rec_type in ("session_meta", "turn_context"):
        assert _events({"type": rec_type, "payload": {}}) == []


def test_unknown_record_type_passes_through_as_raw_event():
    rec = {"type": "some_future_type", "payload": {"x": 1}}
    [ev] = _events(rec)
    assert ev["event_type"] == "raw_event"
    assert ev["payload"]["type"] == "some_future_type"
