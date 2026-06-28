from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cc_sessions (
    app_session_id    TEXT PRIMARY KEY,
    tmux_session      TEXT NOT NULL,
    pane_id           TEXT,
    pane_pid          INTEGER,
    workspace_path    TEXT NOT NULL,
    transcript_path   TEXT,
    transcript_inode  INTEGER,
    claude_session_id TEXT,
    model             TEXT,
    title             TEXT,
    mode              TEXT NOT NULL DEFAULT 'AUTO',
    state             TEXT NOT NULL DEFAULT 'UNKNOWN',
    status            TEXT NOT NULL DEFAULT 'active',
    created_at        TEXT NOT NULL,
    last_event_at     TEXT,
    parent_session_id TEXT,
    project           TEXT,
    bind_contract     TEXT
);
"""

_FIELDS = (
    "app_session_id", "tmux_session", "pane_id", "pane_pid", "workspace_path",
    "transcript_path", "transcript_inode", "claude_session_id", "model", "title",
    "mode", "state", "status", "created_at", "last_event_at",
    # session-tree (module 4): parent link, by-project grouping, bind contract (JSON)
    "parent_session_id", "project", "bind_contract",
)

# Columns stored as JSON text but exposed as parsed objects on the dict API.
_JSON_FIELDS = ("bind_contract",)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode(record: dict) -> dict:
    """Pull the known fields out of a record, JSON-encoding object-valued JSON columns."""
    row = {k: record.get(k) for k in _FIELDS}
    for k in _JSON_FIELDS:
        if isinstance(row.get(k), (dict, list)):
            row[k] = json.dumps(row[k], ensure_ascii=False)
    return row


def _decode(row: sqlite3.Row) -> dict:
    """Row -> dict, JSON-decoding JSON columns (best-effort: leave the raw string if it won't parse)."""
    out = dict(row)
    for k in _JSON_FIELDS:
        val = out.get(k)
        if isinstance(val, str) and val:
            try:
                out[k] = json.loads(val)
            except json.JSONDecodeError:
                pass
    return out


class SessionRegistry:
    """SQLite persistence (thread-safe). Session/pane are local process lifecycle state, decoupled from ES.

    status: active (tmux alive) / archived (tmux killed but claude_session_id retained for resume) / dead.
    Module 4 adds parent_session_id (tree), project (by-project view), bind_contract (JSON I/O contract).
    """

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add any columns missing from an older DB (CREATE IF NOT EXISTS won't alter an existing table)."""
        with self._lock, self._conn:
            existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(cc_sessions)")}
            for column in _FIELDS:
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE cc_sessions ADD COLUMN {column} TEXT")

    def create(self, record: dict) -> dict:
        row = _encode(record)
        row["created_at"] = row.get("created_at") or _now()
        row["mode"] = row.get("mode") or "AUTO"
        row["state"] = row.get("state") or "UNKNOWN"
        row["status"] = row.get("status") or "active"
        columns = ", ".join(_FIELDS)
        placeholders = ", ".join("?" for _ in _FIELDS)
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT OR REPLACE INTO cc_sessions ({columns}) VALUES ({placeholders})",
                [row[k] for k in _FIELDS],
            )
        return _decode_dict(row)

    def update(self, app_session_id: str, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in _FIELDS and k != "app_session_id"}
        if not fields:
            return
        for k in _JSON_FIELDS:
            if isinstance(fields.get(k), (dict, list)):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE cc_sessions SET {assignments} WHERE app_session_id = ?",
                [*fields.values(), app_session_id],
            )

    def get(self, app_session_id: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM cc_sessions WHERE app_session_id = ?", (app_session_id,)
            )
            row = cur.fetchone()
        return _decode(row) if row else None

    def get_by_claude_session_id(self, claude_session_id: str) -> dict | None:
        """Resolve a session by its claude session id (used to route Stop-hook check-ins). Newest match."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM cc_sessions WHERE claude_session_id = ? ORDER BY created_at DESC LIMIT 1",
                (claude_session_id,),
            )
            row = cur.fetchone()
        return _decode(row) if row else None

    def list(self, *, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM cc_sessions"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        with self._lock:
            cur = self._conn.execute(query, params)
            return [_decode(r) for r in cur.fetchall()]

    def list_by_parent(self, parent_session_id: str) -> list[dict]:
        """Direct children of a session (one tree level), newest first."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM cc_sessions WHERE parent_session_id = ? ORDER BY created_at DESC",
                (parent_session_id,),
            )
            return [_decode(r) for r in cur.fetchall()]

    def list_by_project(self, project: str) -> list[dict]:
        """All sessions tagged with a project (by-project view), newest first."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM cc_sessions WHERE project = ? ORDER BY created_at DESC",
                (project,),
            )
            return [_decode(r) for r in cur.fetchall()]

    def delete(self, app_session_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM cc_sessions WHERE app_session_id = ?", (app_session_id,)
            )

    def touch(self, app_session_id: str) -> None:
        self.update(app_session_id, last_event_at=_now())


def _decode_dict(row: dict) -> dict:
    """Decode JSON columns of an in-memory row dict (mirror of _decode for create()'s return)."""
    out = dict(row)
    for k in _JSON_FIELDS:
        val = out.get(k)
        if isinstance(val, str) and val:
            try:
                out[k] = json.loads(val)
            except json.JSONDecodeError:
                pass
    return out
