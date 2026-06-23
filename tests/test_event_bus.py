"""Per-session in-memory event bus: seq, ring-buffer history, ws serialization."""
from __future__ import annotations

import json

from muxdesk import EventBus, event_to_ws_json


def test_publish_increments_seq_per_session():
    bus = EventBus()
    assert bus.publish("s1", "a")["seq"] == 1
    assert bus.publish("s1", "b")["seq"] == 2
    # a separate session keeps its own counter
    assert bus.publish("s2", "c")["seq"] == 1


def test_publish_event_shape():
    bus = EventBus()
    ev = bus.publish("s1", "assistant_message", {"text": "hi"})
    assert ev["session_id"] == "s1"
    assert ev["event_type"] == "assistant_message"
    assert ev["payload"] == {"text": "hi"}
    assert "ts" in ev


def test_publish_defaults_empty_payload():
    assert EventBus().publish("s1", "x")["payload"] == {}


def test_history_after_seq_filter():
    bus = EventBus()
    bus.publish("s1", "a")
    bus.publish("s1", "b")
    bus.publish("s1", "c")
    assert [e["event_type"] for e in bus.history("s1")] == ["a", "b", "c"]
    assert [e["event_type"] for e in bus.history("s1", after_seq=1)] == ["b", "c"]
    assert bus.history("s1", after_seq=3) == []


def test_history_unknown_session_is_empty():
    assert EventBus().history("nope") == []


def test_ring_buffer_drops_oldest():
    bus = EventBus(history_limit=2)
    for t in ("a", "b", "c"):
        bus.publish("s1", t)
    assert [e["event_type"] for e in bus.history("s1")] == ["b", "c"]


def test_event_to_ws_json_roundtrip_and_unicode():
    ev = {"session_id": "s1", "event_type": "x", "seq": 1, "payload": {"k": "ü"}}
    s = event_to_ws_json(ev)
    assert json.loads(s) == ev
    assert "ü" in s  # ensure_ascii=False keeps non-ASCII readable
