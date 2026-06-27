"""Session-bind contract validation + cycle guard (module 4 · 4b/4c).

A bind_contract describes how a child session reports to a parent:
  { "mission": str, "deliverables": {"output_schema": <JSON Schema>},
    "checkin": {"cadence": "on_stop|every_turn|manual", "format": str},
    "report_to": <parent_session_id>, "guardrails": {"blocklist": [...]},
    "kind": "persistent|ephemeral" }
An ephemeral bind may carry no contract (None / {}).
"""

from __future__ import annotations

import json
import re
from typing import Callable

import jsonschema

from muxdesk.team.contract import _validate

BIND_CADENCES = frozenset({"on_stop", "every_turn", "manual"})
BIND_KINDS = frozenset({"persistent", "ephemeral"})

# Last fenced json/object block in an assistant message -> the structured check-in `output`.
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_SUMMARY_MAX = 500


def validate_contract(contract: dict | None) -> tuple[bool, list[str]]:
    """Structural validation of a bind_contract. None/{} is allowed (ephemeral). Returns (ok, errors)."""
    if not contract:
        return True, []
    if not isinstance(contract, dict):
        return False, ["contract must be an object"]
    errors: list[str] = []

    kind = contract.get("kind")
    if kind is not None and kind not in BIND_KINDS:
        errors.append(f"kind must be one of {sorted(BIND_KINDS)}")

    checkin = contract.get("checkin")
    if isinstance(checkin, dict):
        cadence = checkin.get("cadence")
        if cadence is not None and cadence not in BIND_CADENCES:
            errors.append(f"checkin.cadence must be one of {sorted(BIND_CADENCES)}")

    deliverables = contract.get("deliverables")
    if isinstance(deliverables, dict) and deliverables.get("output_schema") is not None:
        try:
            jsonschema.Draft202012Validator.check_schema(deliverables["output_schema"])
        except jsonschema.SchemaError as exc:
            errors.append(f"deliverables.output_schema is not a valid JSON Schema: {exc.message}")

    guardrails = contract.get("guardrails")
    if isinstance(guardrails, dict) and guardrails.get("blocklist") is not None:
        if not isinstance(guardrails["blocklist"], list):
            errors.append("guardrails.blocklist must be a list")

    return (not errors), errors


def validate_checkin(contract: dict | None, output: dict) -> tuple[bool, list[str]]:
    """Validate a child's check-in output against the contract's deliverables.output_schema."""
    schema = ((contract or {}).get("deliverables") or {}).get("output_schema")
    return _validate(schema, output)


def build_checkin(record: dict, body: dict | None) -> tuple[str | None, dict, dict]:
    """Validate a child's check-in against its own contract.

    Returns (parent_session_id, event_payload, result). The payload is what gets published to the
    parent's event bus; result is what the check-in endpoint returns to the child.
    """
    body = body or {}
    output = body.get("output") or {}
    ok, errors = validate_checkin(record.get("bind_contract"), output)
    payload = {
        "child_session_id": record.get("app_session_id"),
        "summary": body.get("summary"),
        "output": output,
        "ok": ok,
        "errors": errors,
    }
    return record.get("parent_session_id"), payload, {"ok": ok, "errors": errors}


def would_cycle(get_parent: Callable[[str], str | None], sid: str, new_parent: str, max_hops: int = 50) -> bool:
    """True if binding `sid` under `new_parent` would form a cycle (sid is, or is an ancestor of, new_parent)."""
    if new_parent == sid:
        return True
    cur: str | None = new_parent
    for _ in range(max_hops):
        cur = get_parent(cur)
        if cur is None:
            return False
        if cur == sid:
            return True
    return True  # chain too deep -> treat as a cycle defensively


def checkin_hook_settings(base: str) -> dict:
    """Static Stop-hook `--settings`: POST the hook payload (carries the claude session_id) to
    checkin-by-claude. Added to every session at creation; the backend no-ops for unbound sessions."""
    url = f"{base}/api/muxdesk/checkin-by-claude"
    cmd = (
        f"curl -sS -X POST {url} -H 'Content-Type: application/json' --data-binary @- "
        f"--max-time 2 >/dev/null 2>>/tmp/muxdesk-hook-err.log || true"
    )
    return {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}}


def _assistant_text(content: object) -> str:
    """Flatten a message `content` (str or list of blocks) to its text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return ""


def extract_transcript_checkin(transcript_path: str | None) -> dict:
    """Build a check-in body from a transcript tail: {summary, output}.

    summary = the last assistant message text (trimmed); output = the last fenced ```json block in
    that message (the child's structured deliverable), or {} if none. Best-effort, never raises.
    """
    if not transcript_path:
        return {"summary": "", "output": {}}
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return {"summary": "", "output": {}}
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = _assistant_text(message.get("content")).strip()
        if not text:
            continue
        output: dict = {}
        match = _FENCED_JSON_RE.search(text)
        if match:
            try:
                parsed = json.loads(match.group(1))
                if isinstance(parsed, dict):
                    output = parsed
            except json.JSONDecodeError:
                pass
        return {"summary": text[:_SUMMARY_MAX], "output": output}
    return {"summary": "", "output": {}}
