from __future__ import annotations

import uuid
from datetime import datetime, timezone

from muxdesk.session_manager import SessionManager
from muxdesk.team.agent_layer import TmuxAgentLayerAdapter
from muxdesk.team.graph import graph_state, load_graph
from muxdesk.team.models import NodeStatus, TeamSession
from muxdesk.team.orchestrator import Orchestrator
from muxdesk.team.team_registry import TeamRegistry


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TeamManager:
    """Top-level coordinator for the team harness: create team (name = semantic_name + date + uuid4) + load graph + start orchestrator."""

    def __init__(self, manager: SessionManager, registry: TeamRegistry) -> None:
        self._manager = manager
        self._registry = registry
        self._adapter = TmuxAgentLayerAdapter(manager, registry)
        self._orchestrators: dict[str, Orchestrator] = {}

    def create_team(self, semantic_name: str, graph_def: dict, *, model: str | None = None, auto_start: bool = True) -> dict:
        team_id = uuid.uuid4().hex
        name = f"{semantic_name}-{_today()}-{team_id[:8]}"
        self._registry.create_team(TeamSession(id=team_id, semantic_name=name, created_at=_now(), metadata={"model": model}))
        load_graph(self._registry, team_id, graph_def)
        if auto_start:
            self.start(team_id, model=model)
        return {"team_id": team_id, "name": name, "graph": graph_state(self._registry, team_id)}

    def start(self, team_id: str, *, model: str | None = None) -> None:
        orch = self._orchestrators.get(team_id)
        if orch is None:
            orch = Orchestrator(self._registry, self._adapter, model=model)
            self._orchestrators[team_id] = orch
        orch.start(team_id)  # orch.start checks thread alive internally to prevent duplicate starts

    def retry_node(self, team_id: str, node_id: str) -> bool:
        """Manual retry: reset node to pending (clear attempt/layer/output) and restart orchestrator to advance."""
        node = self._registry.get_node(node_id)
        if not node:
            return False
        if node.agent_layer_id:
            layer = self._registry.get_layer(node.agent_layer_id)
            if layer and layer.backend_ref:
                try:
                    self._manager.delete(layer.backend_ref)
                except Exception:  # noqa: BLE001
                    pass
        node.status = NodeStatus.PENDING
        node.attempt_count = 0
        node.last_error = None
        node.failure_reason = None
        node.agent_layer_id = None
        node.logs_ref = {}
        node.outputs = {}
        self._registry.upsert_node(node)
        team = self._registry.get_team(team_id)
        self.start(team_id, model=(team.metadata or {}).get("model") if team else None)
        return True

    def mark_node_failed(self, team_id: str, node_id: str) -> bool:
        """Manual abandon: mark node as failed and cascade downstream nodes to skipped (only cascades on failed, not other statuses)."""
        node = self._registry.get_node(node_id)
        if not node:
            return False
        node.status = NodeStatus.FAILED
        self._registry.upsert_node(node)
        self._cascade_skip(team_id, node_id)
        return True

    def _cascade_skip(self, team_id: str, failed_node_id: str) -> None:
        edges = self._registry.list_edges(team_id)
        nodes = {n.id: n for n in self._registry.list_nodes(team_id)}
        stack, seen = [failed_node_id], set()
        while stack:
            cur = stack.pop()
            for e in edges:
                if e.from_node == cur and e.to_node not in seen:
                    seen.add(e.to_node)
                    dn = nodes.get(e.to_node)
                    if dn and dn.status in (NodeStatus.PENDING, NodeStatus.WAITING_INPUT):
                        dn.status = NodeStatus.SKIPPED
                        self._registry.upsert_node(dn)
                    stack.append(e.to_node)

    def graph_state(self, team_id: str) -> dict:
        return graph_state(self._registry, team_id)

    def list_teams(self) -> list[dict]:
        return [{"team_id": t.id, "name": t.semantic_name, "created_at": t.created_at} for t in self._registry.list_teams()]

    def delete_team(self, team_id: str) -> None:
        """Fully delete team: kill and remove all layer sessions, stop orchestrator, cascade-delete registry entries."""
        for layer in self._registry.list_layers(team_id):
            if layer.backend_ref:
                try:
                    self._manager.delete(layer.backend_ref)
                except Exception:  # noqa: BLE001 — best-effort cleanup; individual failures do not block the rest
                    pass
        self._orchestrators.pop(team_id, None)
        self._registry.delete_team(team_id)

    def layers(self, team_id: str) -> list[dict]:
        """Return subagent layers for the team (each = an independent tmux cc session) with corresponding node statuses, for sidebar tree indented display."""
        nodes = {n.id: n for n in self._registry.list_nodes(team_id)}
        out = []
        for layer in self._registry.list_layers(team_id):
            node = nodes.get(layer.node_id) if layer.node_id else None
            label = (node.contract_json or {}).get("name") or layer.node_id if node else layer.node_id
            out.append(
                {
                    "layer_id": layer.id,
                    "node_id": layer.node_id,
                    "app_session_id": layer.backend_ref,  # = cc session id; used when clicking to view details
                    "kind": layer.kind.value,
                    "status": layer.status.value,
                    "node_status": node.status.value if node else None,
                    "label": label,
                }
            )
        return out

    def node_log(self, team_id: str, node_id: str, after_seq: int = 0) -> list[dict]:
        node = self._registry.get_node(node_id)
        if not node or not node.agent_layer_id:
            return []
        layer = self._registry.get_layer(node.agent_layer_id)
        return self._adapter.history(layer, after_seq=after_seq) if layer else []

    def signal_node_done(self, team_id: str, node_id: str) -> None:
        """Webhook received subagent Stop hook -> forward to the corresponding orchestrator to record the turn-end signal (push relay)."""
        orch = self._orchestrators.get(team_id)
        if orch:
            orch.signal_done(team_id, node_id)

    def submit_node_input(self, team_id: str, node_id: str, text: str) -> bool:
        node = self._registry.get_node(node_id)
        if not node or not node.agent_layer_id:
            return False
        layer = self._registry.get_layer(node.agent_layer_id)
        return self._adapter.inject(layer, text) if layer else False

    def messages(self, team_id: str, after_seq: int = 0) -> list[dict]:
        return [
            {"id": m.id, "from": m.from_layer, "to": m.to_layer, "role": m.role.value, "payload": m.payload, "at": m.created_at}
            for m in self._registry.list_messages(team_id, after_seq=after_seq)
        ]
