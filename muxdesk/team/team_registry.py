from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from muxdesk.team.models import (
    AgentLayer,
    Edge,
    LayerKind,
    LayerStatus,
    Message,
    MessageRole,
    Node,
    NodeStatus,
    NodeType,
    TeamSession,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cc_teams (
    id TEXT PRIMARY KEY, semantic_name TEXT, created_at TEXT,
    root_layer_id TEXT, graph_id TEXT, metadata TEXT
);
CREATE TABLE IF NOT EXISTS cc_layers (
    id TEXT PRIMARY KEY, team_id TEXT, kind TEXT, backend_ref TEXT,
    node_id TEXT, status TEXT, log_channels TEXT
);
CREATE TABLE IF NOT EXISTS cc_nodes (
    id TEXT PRIMARY KEY, team_id TEXT, node_type TEXT, prompt_markdown TEXT,
    contract_json TEXT, agent_layer_id TEXT, status TEXT, inputs TEXT, outputs TEXT, logs_ref TEXT,
    started_at TEXT, finished_at TEXT, output_source TEXT, failure_reason TEXT,
    attempt_count INTEGER DEFAULT 0, last_error TEXT
);
CREATE TABLE IF NOT EXISTS cc_edges (
    team_id TEXT, from_node TEXT, to_node TEXT, condition TEXT,
    PRIMARY KEY (team_id, from_node, to_node)
);
CREATE TABLE IF NOT EXISTS cc_messages (
    id TEXT PRIMARY KEY, team_id TEXT, from_layer TEXT, to_layer TEXT,
    role TEXT, payload TEXT, created_at TEXT, seq INTEGER
);
"""


def _d(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _l(value: str | None) -> dict:
    return json.loads(value) if value else {}


class TeamRegistry:
    """SQLite persistence for team / layer / node / edge / message (thread-safe); decoupled from muxdesk session_registry."""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Idempotently add new columns (compatible with older sqlite; sqlite has no ADD COLUMN IF NOT EXISTS)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(cc_nodes)")}
        for col in ("started_at", "finished_at", "output_source", "failure_reason", "last_error"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE cc_nodes ADD COLUMN {col} TEXT")
        if "attempt_count" not in cols:
            self._conn.execute("ALTER TABLE cc_nodes ADD COLUMN attempt_count INTEGER DEFAULT 0")

    # --- team ---

    def create_team(self, team: TeamSession) -> TeamSession:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO cc_teams VALUES (?,?,?,?,?,?)",
                (team.id, team.semantic_name, team.created_at, team.root_layer_id, team.graph_id, _d(team.metadata)),
            )
        return team

    def get_team(self, team_id: str) -> TeamSession | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM cc_teams WHERE id=?", (team_id,)).fetchone()
        if not r:
            return None
        return TeamSession(r["id"], r["semantic_name"], r["created_at"], r["root_layer_id"], r["graph_id"], _l(r["metadata"]))

    def update_team(self, team_id: str, **fields: object) -> None:
        allowed = {"semantic_name", "root_layer_id", "graph_id"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        clause = ", ".join(f"{k}=?" for k in sets)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE cc_teams SET {clause} WHERE id=?", [*sets.values(), team_id])

    def list_teams(self) -> list[TeamSession]:
        with self._lock:
            rs = self._conn.execute("SELECT * FROM cc_teams ORDER BY created_at DESC").fetchall()
        return [TeamSession(r["id"], r["semantic_name"], r["created_at"], r["root_layer_id"], r["graph_id"], _l(r["metadata"])) for r in rs]

    def delete_team(self, team_id: str) -> None:
        """Cascade-delete team and its layers / nodes / edges / messages (the cc sessions backing each layer are deleted separately by the manager)."""
        with self._lock, self._conn:
            for tbl in ("cc_messages", "cc_edges", "cc_nodes", "cc_layers"):
                self._conn.execute(f"DELETE FROM {tbl} WHERE team_id=?", (team_id,))
            self._conn.execute("DELETE FROM cc_teams WHERE id=?", (team_id,))

    # --- layer ---

    def upsert_layer(self, layer: AgentLayer) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO cc_layers VALUES (?,?,?,?,?,?,?)",
                (layer.id, layer.team_id, layer.kind.value, layer.backend_ref, layer.node_id, layer.status.value, _d(layer.log_channels)),
            )

    def get_layer(self, layer_id: str) -> AgentLayer | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM cc_layers WHERE id=?", (layer_id,)).fetchone()
        return self._row_layer(r) if r else None

    def list_layers(self, team_id: str) -> list[AgentLayer]:
        with self._lock:
            rs = self._conn.execute("SELECT * FROM cc_layers WHERE team_id=?", (team_id,)).fetchall()
        return [self._row_layer(r) for r in rs]

    @staticmethod
    def _row_layer(r: sqlite3.Row) -> AgentLayer:
        return AgentLayer(r["id"], r["team_id"], LayerKind(r["kind"]), r["backend_ref"], r["node_id"], LayerStatus(r["status"]), _l(r["log_channels"]))

    # --- node ---

    def upsert_node(self, node: Node) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO cc_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    node.id, node.team_id, node.node_type.value, node.prompt_markdown, _d(node.contract_json),
                    node.agent_layer_id, node.status.value, _d(node.inputs), _d(node.outputs), _d(node.logs_ref),
                    node.started_at, node.finished_at, node.output_source, node.failure_reason,
                    node.attempt_count, node.last_error,
                ),
            )

    def get_node(self, node_id: str) -> Node | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM cc_nodes WHERE id=?", (node_id,)).fetchone()
        return self._row_node(r) if r else None

    def list_nodes(self, team_id: str) -> list[Node]:
        with self._lock:
            rs = self._conn.execute("SELECT * FROM cc_nodes WHERE team_id=?", (team_id,)).fetchall()
        return [self._row_node(r) for r in rs]

    @staticmethod
    def _row_node(r: sqlite3.Row) -> Node:
        return Node(
            r["id"], r["team_id"], NodeType(r["node_type"]), r["prompt_markdown"], _l(r["contract_json"]),
            r["agent_layer_id"], NodeStatus(r["status"]), _l(r["inputs"]), _l(r["outputs"]), _l(r["logs_ref"]),
            r["started_at"], r["finished_at"], r["output_source"], r["failure_reason"],
            r["attempt_count"] or 0, r["last_error"],
        )

    # --- edge ---

    def add_edge(self, edge: Edge) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO cc_edges VALUES (?,?,?,?)",
                (edge.team_id, edge.from_node, edge.to_node, edge.condition),
            )

    def list_edges(self, team_id: str) -> list[Edge]:
        with self._lock:
            rs = self._conn.execute("SELECT * FROM cc_edges WHERE team_id=?", (team_id,)).fetchall()
        return [Edge(r["team_id"], r["from_node"], r["to_node"], r["condition"]) for r in rs]

    # --- message (append-only; seq increments as a watermark for the frontend) ---

    def add_message(self, msg: Message) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM cc_messages WHERE team_id=?", (msg.team_id,))
            seq = int(cur.fetchone()[0])
            self._conn.execute(
                "INSERT OR REPLACE INTO cc_messages VALUES (?,?,?,?,?,?,?,?)",
                (msg.id, msg.team_id, msg.from_layer, msg.to_layer, msg.role.value, _d(msg.payload), msg.created_at, seq),
            )
        return seq

    def list_messages(self, team_id: str, after_seq: int = 0) -> list[Message]:
        with self._lock:
            rs = self._conn.execute(
                "SELECT * FROM cc_messages WHERE team_id=? AND seq>? ORDER BY seq", (team_id, after_seq)
            ).fetchall()
        return [
            Message(r["id"], r["team_id"], r["from_layer"], r["to_layer"], MessageRole(r["role"]), _l(r["payload"]), r["created_at"])
            for r in rs
        ]
