"""Best-effort pane-scrape fallback: coarse, degraded user/assistant events."""
from __future__ import annotations

from muxdesk.parsers import PaneParser


def _events(text: str) -> list[dict]:
    return list(PaneParser().parse_pane(text))


def test_user_and_assistant_rebuilt():
    pane = "> summarise the repo\n● Here is the summary\n  with a second line\n"
    evs = _events(pane)
    assert [e["event_type"] for e in evs] == ["user_message", "assistant_message"]
    assert evs[0]["payload"]["text"] == "summarise the repo"
    assert "second line" in evs[1]["payload"]["text"]


def test_every_event_marked_degraded():
    evs = _events("> hi\n● hello\n")
    assert evs and all(e["payload"]["degraded"] is True for e in evs)


def test_tool_output_and_borders_skipped():
    pane = "● doing work\n⎿ tool output noise\n╭─ box ─╮\n> next question\n"
    evs = _events(pane)
    assert [e["event_type"] for e in evs] == ["assistant_message", "user_message"]
    assert evs[0]["payload"]["text"] == "doing work"  # tool/box noise not merged in


def test_caret_variant_user_prefix():
    evs = _events("❯ hello there\n")
    assert evs[0]["event_type"] == "user_message"
    assert evs[0]["payload"]["text"] == "hello there"


def test_empty_pane_yields_nothing():
    assert _events("") == []
    assert _events("\n\n  \n") == []
