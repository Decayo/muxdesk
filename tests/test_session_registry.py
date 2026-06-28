import sqlite3

from muxdesk.session_registry import SessionRegistry

_BASE = {"app_session_id": "s1", "tmux_session": "tmux-s1", "workspace_path": "/ws"}


def _reg(tmp_path):
    return SessionRegistry(str(tmp_path / "cc.sqlite3"))


def test_create_without_tree_fields_defaults_none(tmp_path):
    reg = _reg(tmp_path)
    reg.create(dict(_BASE))
    row = reg.get("s1")
    assert row["parent_session_id"] is None
    assert row["project"] is None
    assert row["bind_contract"] is None


def test_bind_contract_roundtrips_as_object(tmp_path):
    reg = _reg(tmp_path)
    contract = {"mission": "review PRs", "guardrails": {"blocklist": ["git-push"]}, "kind": "persistent"}
    reg.create({**_BASE, "parent_session_id": "parent1", "project": "muxdesk", "bind_contract": contract})
    row = reg.get("s1")
    assert row["bind_contract"] == contract  # decoded back to a dict
    assert row["parent_session_id"] == "parent1"
    assert row["project"] == "muxdesk"


def test_update_bind_contract(tmp_path):
    reg = _reg(tmp_path)
    reg.create(dict(_BASE))
    reg.update("s1", bind_contract={"mission": "watch"}, project="proj")
    row = reg.get("s1")
    assert row["bind_contract"] == {"mission": "watch"}
    assert row["project"] == "proj"


def test_get_by_claude_session_id(tmp_path):
    reg = _reg(tmp_path)
    reg.create({**_BASE, "claude_session_id": "claude-xyz"})
    assert reg.get_by_claude_session_id("claude-xyz")["app_session_id"] == "s1"
    assert reg.get_by_claude_session_id("missing") is None


def test_list_by_parent_and_project(tmp_path):
    reg = _reg(tmp_path)
    reg.create({**_BASE, "app_session_id": "p", "tmux_session": "t-p", "project": "A"})
    reg.create({**_BASE, "app_session_id": "c1", "tmux_session": "t-c1", "parent_session_id": "p", "project": "A"})
    reg.create({**_BASE, "app_session_id": "c2", "tmux_session": "t-c2", "parent_session_id": "p", "project": "B"})

    children = {r["app_session_id"] for r in reg.list_by_parent("p")}
    assert children == {"c1", "c2"}
    assert [r["app_session_id"] for r in reg.list_by_parent("none")] == []

    proj_a = {r["app_session_id"] for r in reg.list_by_project("A")}
    assert proj_a == {"p", "c1"}


_OLD_SCHEMA = """
CREATE TABLE cc_sessions (
    app_session_id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, pane_id TEXT, pane_pid INTEGER,
    workspace_path TEXT NOT NULL, transcript_path TEXT, transcript_inode INTEGER, claude_session_id TEXT,
    model TEXT, title TEXT, mode TEXT NOT NULL DEFAULT 'AUTO', state TEXT NOT NULL DEFAULT 'UNKNOWN',
    status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, last_event_at TEXT
);
"""


def test_migrates_old_db_adds_columns(tmp_path):
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO cc_sessions (app_session_id, tmux_session, workspace_path, created_at) VALUES (?,?,?,?)",
        ("legacy", "t-legacy", "/ws", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    reg = SessionRegistry(str(db))  # opening should migrate in the missing columns
    legacy = reg.get("legacy")
    assert legacy["parent_session_id"] is None and legacy["project"] is None and legacy["bind_contract"] is None
    # and the migrated DB is fully usable
    reg.update("legacy", project="muxdesk")
    assert reg.get("legacy")["project"] == "muxdesk"
