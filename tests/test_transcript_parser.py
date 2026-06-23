"""Transcript jsonl → semantic events. The core of muxdesk's no-SDK reconstruction."""
from __future__ import annotations

from muxdesk import extract_metadata, parse_record


def _events(record: dict) -> list[dict]:
    return list(parse_record(record))


def test_extract_metadata():
    rec = {"sessionId": "s1", "cwd": "/w", "version": "1.2", "gitBranch": "main"}
    assert extract_metadata(rec) == {
        "session_id": "s1",
        "cwd": "/w",
        "version": "1.2",
        "git_branch": "main",
    }


def test_ignored_types_yield_nothing():
    for t in ("queue-operation", "last-prompt", "attachment"):
        assert _events({"type": t}) == []


def test_assistant_text_with_model_and_usage():
    rec = {
        "type": "assistant",
        "uuid": "u1",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-8",
            "usage": {"output_tokens": 42},
            "content": [{"type": "text", "text": "hello"}],
        },
    }
    [ev] = _events(rec)
    assert ev["event_type"] == "assistant_message"
    assert ev["payload"]["text"] == "hello"
    assert ev["payload"]["model"] == "claude-opus-4-8"
    assert ev["payload"]["output_tokens"] == 42


def test_user_text_string_content():
    rec = {"type": "user", "uuid": "u2", "message": {"role": "user", "content": "hi"}}
    [ev] = _events(rec)
    assert ev["event_type"] == "user_message"
    assert ev["payload"]["text"] == "hi"


def test_thinking_block():
    rec = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}]}}
    [ev] = _events(rec)
    assert ev["event_type"] == "assistant_thinking"
    assert ev["payload"]["text"] == "hmm"


def test_tool_use_then_result():
    use = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
        ]},
    }
    [ev] = _events(use)
    assert ev["event_type"] == "tool_start"
    assert ev["payload"]["tool_use_id"] == "t1"
    assert ev["payload"]["tool_name"] == "Bash"
    assert ev["payload"]["input"] == {"command": "ls"}

    res = {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": False, "content": "out"}
        ]},
    }
    [ev] = _events(res)
    assert ev["event_type"] == "tool_end"
    assert ev["payload"]["tool_use_id"] == "t1"
    assert ev["payload"]["is_error"] is False
    assert ev["payload"]["content"] == "out"


def test_system_notice():
    rec = {"type": "system", "subtype": "turn_duration", "stopReason": None, "uuid": "s"}
    [ev] = _events(rec)
    assert ev["event_type"] == "system_notice"
    assert ev["payload"]["subtype"] == "turn_duration"


def test_unknown_record_type_passes_through_as_raw():
    [ev] = _events({"type": "future-thing", "uuid": "x"})
    assert ev["event_type"] == "raw_event"
    assert ev["payload"]["type"] == "future-thing"


def test_unknown_block_type_raw():
    rec = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "mystery"}]}}
    [ev] = _events(rec)
    assert ev["event_type"] == "raw_event"
    assert ev["payload"]["block_type"] == "mystery"


def test_empty_text_block_skipped():
    rec = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": ""}]}}
    assert _events(rec) == []


def test_malformed_message_does_not_crash():
    # message not a dict → no events, no exception (version tolerance)
    assert _events({"type": "assistant", "message": None}) == []


def test_sidechain_flag_propagates():
    rec = {
        "type": "assistant",
        "isSidechain": True,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
    }
    [ev] = _events(rec)
    assert ev["payload"]["is_sidechain"] is True


def test_multiple_blocks_yield_multiple_events():
    rec = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "plan"},
            {"type": "text", "text": "answer"},
        ]},
    }
    evs = _events(rec)
    assert [e["event_type"] for e in evs] == ["assistant_thinking", "assistant_message"]
