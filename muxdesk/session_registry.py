from __future__ import annotations

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
    claude_projects_dir TEXT,
    provider          TEXT,
    runtime_command   TEXT,
    parser            TEXT,
    model             TEXT,
    title             TEXT,
    mode              TEXT NOT NULL DEFAULT 'AUTO',
    state             TEXT NOT NULL DEFAULT 'UNKNOWN',
    status            TEXT NOT NULL DEFAULT 'active',
    created_at        TEXT NOT NULL,
    last_event_at     TEXT
);
"""

_FIELDS = (
    "app_session_id", "tmux_session", "pane_id", "pane_pid", "workspace_path",
    "transcript_path", "transcript_inode", "claude_session_id", "claude_projects_dir", "provider", "runtime_command", "parser", "model", "title",
    "mode", "state", "status", "created_at", "last_event_at",
)

_MIGRATIONS = {
    "claude_projects_dir": "ALTER TABLE cc_sessions ADD COLUMN claude_projects_dir TEXT",
    "provider": "ALTER TABLE cc_sessions ADD COLUMN provider TEXT",
    "runtime_command": "ALTER TABLE cc_sessions ADD COLUMN runtime_command TEXT",
    "parser": "ALTER TABLE cc_sessions ADD COLUMN parser TEXT",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionRegistry:
    """SQLite persistence (thread-safe). Session/pane are local process lifecycle state, decoupled from ES.

    status: active (tmux alive) / archived (tmux killed but claude_session_id retained for resume) / dead.
    """

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)
            existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(cc_sessions)")}
            for column, sql in _MIGRATIONS.items():
                if column not in existing:
                    self._conn.execute(sql)

    def create(self, record: dict) -> dict:
        row = {k: record.get(k) for k in _FIELDS}
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
        return row

    def update(self, app_session_id: str, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in _FIELDS and k != "app_session_id"}
        if not fields:
            return
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
        return dict(row) if row else None

    def list(self, *, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM cc_sessions"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        with self._lock:
            cur = self._conn.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    def delete(self, app_session_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM cc_sessions WHERE app_session_id = ?", (app_session_id,)
            )

    def touch(self, app_session_id: str) -> None:
        self.update(app_session_id, last_event_at=_now())
