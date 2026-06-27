from muxdesk.bind import build_checkin, validate_checkin, validate_contract, would_cycle

_SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}


def test_empty_contract_is_allowed():
    assert validate_contract(None) == (True, [])
    assert validate_contract({}) == (True, [])


def test_valid_full_contract():
    contract = {
        "mission": "review PRs",
        "deliverables": {"output_schema": _SCHEMA},
        "checkin": {"cadence": "on_stop", "format": "md"},
        "guardrails": {"blocklist": ["git-push"]},
        "kind": "persistent",
    }
    assert validate_contract(contract) == (True, [])


def test_rejects_bad_kind_cadence_schema_blocklist():
    ok, errors = validate_contract(
        {
            "kind": "weird",
            "checkin": {"cadence": "hourly"},
            "deliverables": {"output_schema": {"type": "not-a-type"}},
            "guardrails": {"blocklist": "git-push"},
        }
    )
    assert ok is False
    assert len(errors) == 4


def test_non_dict_contract_rejected():
    ok, errors = validate_contract("nope")  # type: ignore[arg-type]
    assert ok is False and errors


def test_validate_checkin_against_output_schema():
    contract = {"deliverables": {"output_schema": _SCHEMA}}
    assert validate_checkin(contract, {"summary": "done"}) == (True, [])
    ok, errors = validate_checkin(contract, {"oops": 1})
    assert ok is False and errors


def test_validate_checkin_no_schema_passes():
    assert validate_checkin({}, {"anything": 1}) == (True, [])


def test_build_checkin_valid_with_parent():
    record = {
        "app_session_id": "child",
        "parent_session_id": "parent",
        "bind_contract": {"deliverables": {"output_schema": _SCHEMA}},
    }
    parent, payload, result = build_checkin(record, {"summary": "ok note", "output": {"summary": "done"}})
    assert parent == "parent"
    assert result == {"ok": True, "errors": []}
    assert payload["child_session_id"] == "child"
    assert payload["summary"] == "ok note"
    assert payload["output"] == {"summary": "done"}


def test_build_checkin_invalid_output_reports_errors():
    record = {"app_session_id": "c", "parent_session_id": "p", "bind_contract": {"deliverables": {"output_schema": _SCHEMA}}}
    parent, payload, result = build_checkin(record, {"output": {"nope": 1}})
    assert parent == "p"
    assert result["ok"] is False and result["errors"]
    assert payload["ok"] is False


def test_build_checkin_no_parent_no_contract():
    parent, payload, result = build_checkin({"app_session_id": "lone"}, {"output": {"x": 1}})
    assert parent is None
    assert result == {"ok": True, "errors": []}  # no schema -> passes


def test_would_cycle():
    parents = {"c": "b", "b": "a", "a": None}
    get = parents.get
    assert would_cycle(get, "x", "x") is True  # bind to self
    assert would_cycle(get, "a", "c") is True  # a is an ancestor of c
    assert would_cycle(get, "c", "a") is False  # binding c under a is fine
    assert would_cycle(get, "new", "a") is False  # unrelated parent
