from __future__ import annotations

import json
import re

import jsonschema

from muxdesk.team.models import Node

# contract_json follows OpenAI tool-spec style:
#   { "name", "description", "input_schema": <JSON Schema>, "output_schema": <JSON Schema> }

# Fallback rescue channel: extract the last fenced json/object block from the transcript
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _validate(schema: dict | None, data: dict) -> tuple[bool, list[str]]:
    if not schema:
        return True, []  # No schema = no validation (pass)
    validator = jsonschema.Draft202012Validator(schema)
    errors = [e.message for e in validator.iter_errors(data)]
    return (not errors), errors


def validate_output(node: Node, output: dict) -> tuple[bool, list[str]]:
    """Validate output against the node contract's output_schema. Returns (ok, errors)."""
    return _validate((node.contract_json or {}).get("output_schema"), output)


def validate_input(node: Node, inputs: dict) -> tuple[bool, list[str]]:
    """Validate inputs against the node contract's input_schema. Returns (ok, errors)."""
    return _validate((node.contract_json or {}).get("input_schema"), inputs)


def _parse_object(raw: str, bad_reason: str) -> tuple[dict | None, str | None]:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None, bad_reason
    if not isinstance(data, dict):
        return None, bad_reason
    return data, None


def resolve_node_output(
    node: Node, file_content: str | None, transcript_text: str | None
) -> tuple[dict | None, str | None, str | None]:
    """Determine the final node output: fixed-file primary channel + fenced json fallback, both validated against output_schema.

    Returns (output|None, source|None, failure_reason|None):
      - Fixed file parses as object and passes schema -> (data, "file", None)
      - Otherwise, last fenced json object in transcript passes schema -> (data, "fallback_transcript", None)
      - Neither works -> (None, None, reason), reason in {missing_output_file, invalid_json_file, invalid_fallback_json}
    """
    schema = (node.contract_json or {}).get("output_schema")
    # 1. Primary channel: fixed file (node_output/<id>.json written by the subagent)
    if file_content is not None:
        data, reason = _parse_object(file_content, "invalid_json_file")
        if data is None:
            return None, None, reason
        ok, _ = _validate(schema, data)
        return (data, "file", None) if ok else (None, None, "invalid_json_file")
    # 2. Rescue channel: last fenced json block in the transcript
    blocks = _FENCED_JSON_RE.findall(transcript_text or "")
    if blocks:
        data, reason = _parse_object(blocks[-1], "invalid_fallback_json")
        if data is None:
            return None, None, reason
        ok, _ = _validate(schema, data)
        return (data, "fallback_transcript", None) if ok else (None, None, "invalid_fallback_json")
    # 3. Neither available
    return None, None, "missing_output_file"
