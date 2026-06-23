"""Backward-compatible shim for the transcript parser.

The pluggable provider framework lives in :mod:`muxdesk.parsers`. This module
preserves the historical free-function API (``parse_record`` / ``extract_metadata``)
that ``session_manager`` and external consumers (e.g. ibkr's standalone server)
import, by delegating to a default :class:`~muxdesk.parsers.claude.ClaudeParser`.
"""
from __future__ import annotations

from collections.abc import Iterator

from muxdesk.parsers.claude import ClaudeParser

_default = ClaudeParser()


def parse_record(record: dict) -> Iterator[dict]:
    """Parse one Claude Code transcript record into semantic events (delegates to ClaudeParser)."""
    return _default.parse_record(record)


def extract_metadata(record: dict) -> dict:
    """Extract session-binding metadata from a transcript record (delegates to ClaudeParser)."""
    return _default.extract_metadata(record)
