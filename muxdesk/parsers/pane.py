"""Best-effort conversation reconstruction from tmux ``capture-pane`` text.

Fallback for when the jsonl transcript is unavailable or unparseable. Coarse by
design: scans Claude TUI sentinels (assistant blocks start with ``●``, user input
lines with ``>`` / ``❯``) and emits degraded ``user_message`` / ``assistant_message``
events. It does not attempt tool_use / thinking fidelity. Every emitted event
carries ``degraded: True`` so consumers can flag "reconstructed from screen".

PaneParser is intentionally NOT a :class:`~muxdesk.parsers.base.Parser`: that
interface is record-level (one jsonl line in), whereas a pane scrape consumes a
whole screen. :class:`~muxdesk.parsers.chain.ParserChain` orchestrates the two.
"""
from __future__ import annotations

from collections.abc import Iterator

_USER_PREFIXES = ("> ", "❯ ")
_ASSISTANT_PREFIX = "● "
# tool output (⎿) and box-drawing borders are noise for a coarse rebuild
_SKIP_PREFIXES = ("⎿", "│", "╭", "╰", "├", "└")


class PaneParser:
    """Reconstruct coarse, degraded conversation events from pane text."""

    def parse_pane(self, text: str) -> Iterator[dict]:
        lines = text.splitlines()
        i, n = 0, len(lines)
        while i < n:
            stripped = lines[i].lstrip()
            if stripped.startswith(_USER_PREFIXES):
                msg = stripped[2:].strip()
                if msg:
                    yield self._event("user_message", msg)
                i += 1
            elif stripped.startswith(_ASSISTANT_PREFIX):
                block = [stripped[2:].strip()]
                i += 1
                # gather indented continuation lines until the next sentinel
                while i < n:
                    nxt = lines[i]
                    ns = nxt.lstrip()
                    if not nxt.strip():
                        i += 1
                        continue
                    if (
                        ns.startswith(_USER_PREFIXES)
                        or ns.startswith(_ASSISTANT_PREFIX)
                        or ns.startswith(_SKIP_PREFIXES)
                    ):
                        break
                    block.append(nxt.strip())
                    i += 1
                msg = "\n".join(b for b in block if b).strip()
                if msg:
                    yield self._event("assistant_message", msg)
            else:
                i += 1

    @staticmethod
    def _event(event_type: str, text: str) -> dict:
        return {"event_type": event_type, "payload": {"text": text, "degraded": True}}
