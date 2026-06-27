"""End-to-end bind/check-in/guardrail flow through the real FastAPI app (TestClient).

Env is pointed at a temp DB + guardrails dir BEFORE importing muxdesk.app, so the import-time
_configure() uses throwaway state (no touching the real demo DB / probe loop on real sessions).
"""

import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="muxdesk-it-")
os.environ["MUXDESK_DB_PATH"] = os.path.join(_TMP, "sessions.sqlite3")
os.environ["MUXDESK_GUARDRAILS_DIR"] = os.path.join(_TMP, "guardrails")

import pytest  # noqa: E402

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from muxdesk import app as appmod  # noqa: E402  (import triggers _configure with the temp env)

client = TestClient(appmod.app)
_SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}


def _seed():
    reg = appmod.registry
    reg.create({"app_session_id": "P", "tmux_session": "t-P", "workspace_path": "/ws", "created_at": "2026-01-01T00:00:00+00:00"})
    transcript = os.path.join(_TMP, "child.jsonl")
    with open(transcript, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"message": {"role": "assistant", "content": "done ```json\n{\"summary\": \"ok\"}\n```"}}))
    reg.create(
        {
            "app_session_id": "C",
            "tmux_session": "t-C",
            "workspace_path": "/ws",
            "claude_session_id": "cc-C",
            "transcript_path": transcript,
            "created_at": "2026-01-01T00:00:01+00:00",
        }
    )


def test_bind_checkin_guardrail_end_to_end():
    _seed()
    contract = {
        "mission": "watch the repo",
        "deliverables": {"output_schema": _SCHEMA},
        "guardrails": {"blocklist": ["git-push"]},
        "kind": "persistent",
    }

    # bind C under P with a contract
    r = client.post("/api/muxdesk/sessions/C/bind", json={"parent_session_id": "P", "contract": contract})
    assert r.status_code == 200
    assert r.json()["parent_session_id"] == "P"
    # guardrail marker mirrored for the child
    assert os.path.isfile(os.path.join(os.environ["MUXDESK_GUARDRAILS_DIR"], "cc-C.json"))

    # cycle guard: binding P under C is rejected
    assert client.post("/api/muxdesk/sessions/P/bind", json={"parent_session_id": "C"}).status_code == 409

    # the Stop-hook check-in: maps cc-C -> C, validates {summary:ok} vs schema, delivers to P
    r = client.post("/api/muxdesk/checkin-by-claude", json={"session_id": "cc-C"})
    body = r.json()
    assert body["matched"] is True and body["ok"] is True and body["delivered_to_parent"] is True
    assert any(e["event_type"] == "child_checkin" for e in appmod.bus.history("P"))

    # guardrail: a blocked command is denied; an allowed one passes
    assert client.post("/api/muxdesk/guardrail-check-by-claude", json={"session_id": "cc-C", "tool_name": "Bash", "tool_input": {"command": "git push origin"}}).json()["allow"] is False
    assert client.post("/api/muxdesk/guardrail-check-by-claude", json={"session_id": "cc-C", "tool_name": "Bash", "tool_input": {"command": "ls -la"}}).json()["allow"] is True

    # unbind clears parent + the guardrail marker
    r = client.post("/api/muxdesk/sessions/C/unbind")
    assert r.json()["parent_session_id"] is None
    assert not os.path.isfile(os.path.join(os.environ["MUXDESK_GUARDRAILS_DIR"], "cc-C.json"))


def test_checkin_by_claude_unknown_session_is_noop():
    r = client.post("/api/muxdesk/checkin-by-claude", json={"session_id": "does-not-exist"})
    assert r.json() == {"ok": True, "matched": False, "delivered_to_parent": False}
