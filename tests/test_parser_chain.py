"""Antifragile chain: prefer jsonl, degrade to pane scrape, never raise/drop."""
from __future__ import annotations

from muxdesk.parsers import ParserChain


def _claude_record(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _degraded(evs: list[dict]) -> bool:
    return any(e["payload"].get("subtype") == "parser_degraded" for e in evs)


def test_jsonl_available_uses_claude_no_degrade():
    evs = list(ParserChain().reconstruct(jsonl_records=[_claude_record("hi")], pane_text="> x\n● y"))
    assert [e["event_type"] for e in evs] == ["assistant_message"]
    assert evs[0]["payload"]["text"] == "hi"
    assert not _degraded(evs)  # pane ignored when jsonl is usable


def test_jsonl_empty_falls_back_to_pane():
    evs = list(ParserChain().reconstruct(jsonl_records=[], pane_text="> question\n● answer\n"))
    assert evs[0]["payload"]["subtype"] == "parser_degraded"
    assert [e["event_type"] for e in evs[1:]] == ["user_message", "assistant_message"]


def test_jsonl_none_falls_back_to_pane():
    evs = list(ParserChain().reconstruct(pane_text="● only screen\n"))
    assert _degraded(evs)
    assert evs[1]["payload"]["text"] == "only screen"


def test_records_yielding_no_events_fall_back():
    # ignored-type records produce no events → degrade to pane
    evs = list(ParserChain().reconstruct(jsonl_records=[{"type": "queue-operation"}], pane_text="● fallback\n"))
    assert _degraded(evs)


def test_bad_record_does_not_break_stream():
    # a record that would raise inside parse_record is swallowed; chain degrades
    evs = list(ParserChain().reconstruct(jsonl_records=[None], pane_text="● recovered\n"))
    assert any(e["event_type"] == "assistant_message" and e["payload"].get("degraded") for e in evs)


def test_no_sources_yields_nothing():
    assert list(ParserChain().reconstruct()) == []
