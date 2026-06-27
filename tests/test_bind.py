from muxdesk.bind import (
    build_checkin,
    guardrail_decision,
    should_auto_deliver,
    validate_checkin,
    validate_contract,
    would_cycle,
)

_SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}


def test_guardrail_marker_write_clear(tmp_path):
    from muxdesk.bind import clear_guardrail_marker, guardrail_marker_path, write_guardrail_marker
    import json as _json

    d = str(tmp_path)
    write_guardrail_marker(d, "claude-1", {"guardrails": {"blocklist": ["git-push"]}})
    p = guardrail_marker_path(d, "claude-1")
    assert p.is_file()
    assert _json.loads(p.read_text())["blocklist"] == ["git-push"]
    # rewriting with no blocklist clears it
    write_guardrail_marker(d, "claude-1", {})
    assert not p.is_file()
    # idempotent + tolerates None
    clear_guardrail_marker(d, "claude-1")
    clear_guardrail_marker(d, None)
    write_guardrail_marker(d, None, {"guardrails": {"blocklist": ["x"]}})  # no claude id -> no-op


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


def test_extract_transcript_checkin(tmp_path):
    from muxdesk.bind import extract_transcript_checkin
    import json as _json

    t = tmp_path / "t.jsonl"
    lines = [
        {"message": {"role": "user", "content": "go"}},
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "working…"}]}},
        {"message": {"role": "assistant", "content": "done. ```json\n{\"summary\": \"ok\"}\n```"}},
    ]
    t.write_text("\n".join(_json.dumps(x) for x in lines), encoding="utf-8")
    result = extract_transcript_checkin(str(t))
    assert result["output"] == {"summary": "ok"}
    assert "done." in result["summary"]


def test_extract_transcript_checkin_no_json(tmp_path):
    from muxdesk.bind import extract_transcript_checkin
    import json as _json

    t = tmp_path / "t.jsonl"
    t.write_text(_json.dumps({"message": {"role": "assistant", "content": "just prose"}}), encoding="utf-8")
    assert extract_transcript_checkin(str(t)) == {"summary": "just prose", "output": {}}


def test_extract_transcript_checkin_missing_file():
    from muxdesk.bind import extract_transcript_checkin

    assert extract_transcript_checkin(None) == {"summary": "", "output": {}}
    assert extract_transcript_checkin("/no/such/file.jsonl") == {"summary": "", "output": {}}


def test_checkin_hook_settings_targets_checkin_by_claude():
    from muxdesk.bind import checkin_hook_settings

    stop = checkin_hook_settings("http://127.0.0.1:9999")["hooks"]["Stop"]
    cmd = stop[0]["hooks"][0]["command"]
    assert "http://127.0.0.1:9999/api/muxdesk/checkin-by-claude" in cmd
    assert "--data-binary @-" in cmd  # forwards the hook stdin (carries the claude session_id)


def test_guardrail_decision():
    contract = {"guardrails": {"blocklist": ["git-push", "deploy", "delete"]}}
    # no blocklist -> always allow
    assert guardrail_decision({}, "Bash", {"command": "git push"}) == (True, "")
    assert guardrail_decision(None, "Bash", {"command": "rm -rf /"}) == (True, "")
    # blocked: "git-push" matches "git push" in the command
    allowed, reason = guardrail_decision(contract, "Bash", {"command": "git push origin main"})
    assert allowed is False and "git-push" in reason
    # blocked by another entry
    assert guardrail_decision(contract, "Bash", {"command": "make deploy"})[0] is False
    # allowed: unrelated command
    assert guardrail_decision(contract, "Bash", {"command": "ls -la"}) == (True, "")
    # matches against tool name too
    assert guardrail_decision({"guardrails": {"blocklist": ["WebFetch"]}}, "WebFetch", {"url": "x"})[0] is False


def test_should_auto_deliver_respects_cadence():
    assert should_auto_deliver(None) is True
    assert should_auto_deliver({}) is True
    assert should_auto_deliver({"checkin": {"cadence": "on_stop"}}) is True
    assert should_auto_deliver({"checkin": {"cadence": "every_turn"}}) is True
    assert should_auto_deliver({"checkin": {"cadence": "manual"}}) is False


def test_would_cycle():
    parents = {"c": "b", "b": "a", "a": None}
    get = parents.get
    assert would_cycle(get, "x", "x") is True  # bind to self
    assert would_cycle(get, "a", "c") is True  # a is an ancestor of c
    assert would_cycle(get, "c", "a") is False  # binding c under a is fine
    assert would_cycle(get, "new", "a") is False  # unrelated parent
