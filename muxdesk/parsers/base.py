"""Parser interface for transcript → events reconstruction.

A Parser turns one raw transcript record into 0..N semantic event dicts of the
shape ``{"event_type": str, "payload": dict}`` — the stable contract consumed by
``state_machine`` / ``event_bus`` / the frontend. Providers implement this for
different AI CLI transcript formats; ``ParserChain`` composes them with
antifragile fallback.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator


class Parser(ABC):
    """Abstract transcript parser. Implementations must be version-tolerant."""

    @abstractmethod
    def parse_record(self, record: dict) -> Iterator[dict]:
        """Yield 0..N ``{event_type, payload}`` events for one transcript record.

        Must never raise and never drop the stream: unknown shapes pass through
        as a ``raw_event``.
        """
        raise NotImplementedError

    @abstractmethod
    def extract_metadata(self, record: dict) -> dict:
        """Pull session-binding metadata (session_id / cwd / version / git_branch)."""
        raise NotImplementedError
