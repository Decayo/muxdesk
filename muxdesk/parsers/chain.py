"""Antifragile transcript reconstruction chain.

Prefers the jsonl source (``ClaudeParser``); degrades to a tmux pane scrape
(``PaneParser``) when jsonl is unavailable, empty, or yields nothing — emitting a
one-time ``parser_degraded`` notice so the fallback is visible, never silent. It
never raises to the caller and never drops the conversation.

Source-agnostic by design: the caller injects whatever sources are available
(``jsonl_records`` and/or ``pane_text``), so the chain has no dependency on
``session_manager`` or the filesystem. Wiring it into ``session_manager``'s read
path is deferred (see the openspec design Open Questions).
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator

from muxdesk.parsers.claude import ClaudeParser
from muxdesk.parsers.pane import PaneParser


class ParserChain:
    """Reconstruct events from the best available source, degrading on failure."""

    def __init__(self, jsonl_parser: ClaudeParser | None = None, pane_parser: PaneParser | None = None) -> None:
        self._jsonl = jsonl_parser or ClaudeParser()
        self._pane = pane_parser or PaneParser()

    def reconstruct(
        self,
        *,
        jsonl_records: Iterable[dict] | None = None,
        pane_text: str | None = None,
    ) -> Iterator[dict]:
        """Yield events from jsonl if it produces any, else degrade to the pane scrape.

        Choosing one source per call (not merging per-record) keeps the event
        stream coherent — no duplicates, no ordering chaos.
        """
        if jsonl_records is not None:
            produced = False
            for record in jsonl_records:
                try:
                    events = list(self._jsonl.parse_record(record))
                except Exception:  # noqa: BLE001 — a bad record must not break the stream
                    events = []
                for ev in events:
                    produced = True
                    yield ev
            if produced:
                return  # jsonl was usable; do not fall back

        if pane_text:
            yield {
                "event_type": "system_notice",
                "payload": {
                    "subtype": "parser_degraded",
                    "detail": "jsonl transcript unavailable; reconstructed from tmux pane (best-effort, may be incomplete)",
                },
            }
            yield from self._pane.parse_pane(pane_text)
