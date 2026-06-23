"""Claude Code jsonl transcript parser.

Verbatim port of the original ``muxdesk.transcript_parser`` logic into the
``Parser`` interface. The free functions in ``muxdesk.transcript_parser`` now
delegate to a default instance of this class.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from muxdesk.parsers.base import Parser

# Internal / noise types that don't need to be displayed to the frontend
_IGNORED_TYPES = {"queue-operation", "last-prompt", "attachment"}


class ClaudeParser(Parser):
    """Parser for Claude Code's transcript jsonl format."""

    def extract_metadata(self, record: dict) -> dict:
        """Extract metadata from a transcript line for session binding verification."""
        return {
            "session_id": record.get("sessionId"),
            "cwd": record.get("cwd"),
            "version": record.get("version"),
            "git_branch": record.get("gitBranch"),
        }

    def parse_record(self, record: dict) -> Iterator[dict]:
        """Convert one transcript record into 0..N semantic event dicts ({event_type, payload}).

        Version-tolerant: unknown type/block passes through as raw_event, never dropped or interrupted.
        isSidechain=True marks sidechain events (frontend may deprioritize).
        """
        rec_type = record.get("type")
        if rec_type in _IGNORED_TYPES:
            return
        is_sidechain = bool(record.get("isSidechain"))
        uuid = record.get("uuid")

        if rec_type in ("user", "assistant"):
            message = record.get("message")
            if not isinstance(message, dict):
                return
            role = message.get("role") or rec_type
            content = message.get("content")
            model = message.get("model")  # assistant lines carry the actual running model id (e.g. claude-opus-4-8)
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            for block in blocks:
                if isinstance(block, dict):
                    yield from self._parse_block(role, block, uuid=uuid, is_sidechain=is_sidechain, model=model, usage=usage)
            return

        if rec_type == "system":
            # system events may carry stopReason / hook info, used by state machine and checkpoint hook
            yield {
                "event_type": "system_notice",
                "payload": {
                    "subtype": record.get("subtype"),
                    "stop_reason": record.get("stopReason"),
                    "level": record.get("level"),
                    "hook_count": record.get("hookCount"),
                    "hook_infos": record.get("hookInfos"),
                    "hook_errors": record.get("hookErrors"),
                    "tool_use_id": record.get("toolUseID"),
                    "is_sidechain": is_sidechain,
                    "uuid": uuid,
                },
            }
            return

        yield {
            "event_type": "raw_event",
            "payload": {"type": rec_type, "is_sidechain": is_sidechain, "uuid": uuid},
        }

    @staticmethod
    def _parse_block(
        role: str, block: dict, *, uuid: Any, is_sidechain: bool, model: str | None = None, usage: dict | None = None
    ) -> Iterator[dict]:
        block_type = block.get("type")
        base = {"role": role, "uuid": uuid, "is_sidechain": is_sidechain}

        if block_type == "text":
            text = block.get("text") or ""
            if text:
                payload = {**base, "text": text}
                if role == "assistant" and model:
                    payload["model"] = model
                if role == "assistant" and usage:  # Generated tokens for this turn (frontend displays "N tok")
                    payload["output_tokens"] = usage.get("output_tokens")
                yield {
                    "event_type": "assistant_message" if role == "assistant" else "user_message",
                    "payload": payload,
                }
        elif block_type == "thinking":
            yield {
                "event_type": "assistant_thinking",
                "payload": {**base, "text": block.get("thinking") or ""},
            }
        elif block_type == "image":
            yield {
                "event_type": "image",
                "payload": {**base, "source": block.get("source")},
            }
        elif block_type == "tool_use":
            yield {
                "event_type": "tool_start",
                "payload": {
                    **base,
                    "tool_use_id": block.get("id"),
                    "tool_name": block.get("name"),
                    "input": block.get("input"),
                },
            }
        elif block_type == "tool_result":
            yield {
                "event_type": "tool_end",
                "payload": {
                    **base,
                    "tool_use_id": block.get("tool_use_id"),
                    "is_error": bool(block.get("is_error")),
                    "content": block.get("content"),
                },
            }
        else:
            yield {
                "event_type": "raw_event",
                "payload": {**base, "block_type": block_type},
            }
