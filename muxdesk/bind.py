"""Session-bind contract validation + cycle guard (module 4 · 4b/4c).

A bind_contract describes how a child session reports to a parent:
  { "mission": str, "deliverables": {"output_schema": <JSON Schema>},
    "checkin": {"cadence": "on_stop|every_turn|manual", "format": str},
    "report_to": <parent_session_id>, "guardrails": {"blocklist": [...]},
    "kind": "persistent|ephemeral" }
An ephemeral bind may carry no contract (None / {}).
"""

from __future__ import annotations

from typing import Callable

import jsonschema

from muxdesk.team.contract import _validate

BIND_CADENCES = frozenset({"on_stop", "every_turn", "manual"})
BIND_KINDS = frozenset({"persistent", "ephemeral"})


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
