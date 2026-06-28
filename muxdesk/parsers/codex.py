"""Codex CLI rollout transcript parser.

Codex (the OpenAI coding-agent CLI) writes one ``rollout-*.jsonl`` per session
under ``$CODEX_HOME/sessions/YYYY/MM/DD/``. Each line is a record with a
``type`` of ``session_meta`` / ``turn_context`` / ``response_item`` /
``event_msg``. This parser turns those records into the same
``{event_type, payload}`` event dicts the rest of muxdesk (state machine, event
bus, frontend) already consumes for Claude Code, so a codex session renders in
the web UI without frontend changes.

Mapping (kept intentionally parallel to :class:`muxdesk.parsers.claude.ClaudeParser`):

- ``event_msg{type=user_message}``   → ``user_message``
- ``event_msg{type=agent_message}``  → ``assistant_message``
- ``event_msg{type=agent_reasoning}``→ ``assistant_thinking``
- ``event_msg{type=task_started}``   → (turn-start marker; not surfaced)
- ``event_msg{type=task_complete}``  → ``system_notice{subtype=turn_duration}``
  (drives the state machine back to READY — the codex equivalent of Claude's
  turn-end ``stop_reason`` notice)
- ``event_msg{type=turn_aborted}``   → ``system_notice{subtype=interrupted}``
- ``event_msg{type=token_count}``    → ``system_notice{subtype=token_count}``
- ``response_item{type=function_call}``         → ``tool_start``
- ``response_item{type=function_call_output}``  → ``tool_end``
- ``response_item{role=assistant}`` with image content → ``image``

Version-tolerant: unknown record shapes pass through as ``raw_event`` rather
than raising, matching the antifragile contract of the parser chain.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from muxdesk.parsers.base import Parser

# event_msg subtypes that carry no user-facing signal (turn lifecycle bookkeeping
# emitted by the codex TUI itself). task_started/task_complete/turn_aborted are
# handled explicitly below; these are dropped to keep the event stream clean.
_IGNORED_EVENT_MSG_TYPES: set[str] = set()


def _parse_list(value: object) -> list:
    """Coerce a rollout field into a list, tolerating None / non-list shapes."""
    if isinstance(value, list):
        return value
    return []


class CodexParser(Parser):
    """Parser for the Codex CLI rollout jsonl format."""

    def extract_metadata(self, record: dict) -> dict:
        """Pull session-binding metadata from a codex rollout record.

        Only ``session_meta`` carries the id/cwd; other record types return an
        empty dict so callers can cheaply probe every line for the header.
        """
        if record.get("type") != "session_meta":
            return {}
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        return {
            "session_id": payload.get("id") or payload.get("session_id"),
            "cwd": payload.get("cwd"),
            "version": payload.get("cli_version"),
            "git_branch": None,  # codex rollout does not record git branch
        }

    def parse_record(self, record: dict) -> Iterator[dict]:
        """Convert one codex rollout record into 0..N semantic event dicts.

        Never raises and never drops the stream: unknown record shapes pass
        through as ``raw_event``.
        """
        rec_type = record.get("type")
        if rec_type is None:
            return

        if rec_type == "event_msg":
            yield from self._parse_event_msg(record.get("payload") or {})
            return

        if rec_type == "response_item":
            yield from self._parse_response_item(record.get("payload") or {})
            return

        if rec_type in ("session_meta", "turn_context"):
            # session/header metadata — already surfaced via extract_metadata;
            # no per-turn event needed.
            return

        yield {"event_type": "raw_event", "payload": {"type": rec_type}}

    # --- event_msg: TUI-level agent events (display-faithful) ---

    @staticmethod
    def _parse_event_msg(payload: dict) -> Iterator[dict]:
        subtype = payload.get("type")
        if subtype in _IGNORED_EVENT_MSG_TYPES:
            return

        if subtype == "user_message":
            text = payload.get("message")
            if text is None:
                return
            event_payload: dict[str, Any] = {"role": "user", "text": str(text)}
            # codex attaches user-supplied media as file ids (``images``) and/or
            # local file paths (``local_images``); merge both so the frontend
            # can resolve and render uploads.
            media = [*(_parse_list(payload.get("images"))), *(_parse_list(payload.get("local_images")))]
            if media:
                event_payload["images"] = media
            yield {"event_type": "user_message", "payload": event_payload}
            return

        if subtype == "agent_message":
            text = payload.get("message")
            if text is None:
                return
            yield {
                "event_type": "assistant_message",
                "payload": {"role": "assistant", "text": str(text)},
            }
            return

        if subtype == "agent_reasoning":
            text = payload.get("text") or payload.get("message") or ""
            if not text:
                return
            yield {
                "event_type": "assistant_thinking",
                "payload": {"role": "assistant", "text": str(text)},
            }
            return

        if subtype == "task_started":
            # Turn begin — the state machine flips to STREAMING/RUNNING on the
            # first assistant/tool event; no separate start event needed.
            return

        if subtype == "task_complete":
            # Turn end → mirror Claude's turn-end system_notice so the state
            # machine (on_event: subtype in turn_duration/stop_hook_summary OR
            # stop_reason present) converges back to READY.
            yield {
                "event_type": "system_notice",
                "payload": {
                    "subtype": "turn_duration",
                    "stop_reason": "task_complete",
                    "last_agent_message": payload.get("last_agent_message"),
                    "duration_ms": payload.get("duration_ms"),
                },
            }
            return

        if subtype == "turn_aborted":
            yield {
                "event_type": "system_notice",
                "payload": {"subtype": "interrupted", "stop_reason": "turn_aborted"},
            }
            return

        if subtype == "token_count":
            yield {
                "event_type": "system_notice",
                "payload": {"subtype": "token_count", "rate_limits": payload.get("rate_limits")},
            }
            return

        # Unknown event_msg subtype — pass through as raw so it is visible, not silent.
        yield {"event_type": "raw_event", "payload": {"event_msg_type": subtype}}

    # --- response_item: API-level messages and tool calls ---

    @staticmethod
    def _parse_response_item(payload: dict) -> Iterator[dict]:
        item_type = payload.get("type")

        if item_type == "function_call":
            call_id = payload.get("call_id")
            name = payload.get("name") or "tool"
            raw_args = payload.get("arguments")
            # codex serializes tool arguments as a JSON string; parse best-effort
            # so the frontend's existing tool_start rendering (summarize(input))
            # works unchanged. Unparseable → keep the raw string.
            if isinstance(raw_args, str):
                try:
                    parsed_args: Any = json.loads(raw_args)
                except Exception:  # noqa: BLE001 — keep raw on any parse failure
                    parsed_args = raw_args
            else:
                parsed_args = raw_args
            yield {
                "event_type": "tool_start",
                "payload": {
                    "role": "assistant",
                    "tool_use_id": call_id,
                    "tool_name": name,
                    "input": parsed_args,
                },
            }
            return

        if item_type == "function_call_output":
            yield {
                "event_type": "tool_end",
                "payload": {
                    "role": "assistant",
                    "tool_use_id": payload.get("call_id"),
                    "is_error": False,
                    "content": payload.get("output"),
                },
            }
            return

        if item_type == "message":
            role = payload.get("role")
            content = payload.get("content")
            if not isinstance(content, list):
                return
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                # codex image blocks (output_image / image) carry a file id/url
                if block_type in ("image", "output_image", "input_image"):
                    yield {
                        "event_type": "image",
                        "payload": {
                            "role": role or "assistant",
                            "source": block.get("source") or block.get("image_url") or block.get("url"),
                        },
                    }
            return

        if item_type in ("reasoning", "custom_tool_call", "custom_tool_call_output"):
            # Already surfaced via the display-faithful event_msg stream; skip the
            # duplicated API-level copy to avoid double-rendering.
            return

        # Unknown response_item shape — pass through.
        yield {"event_type": "raw_event", "payload": {"response_item_type": item_type}}
