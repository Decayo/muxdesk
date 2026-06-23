from __future__ import annotations

import uuid

from muxdesk.session_manager import SessionManager
from muxdesk.team.models import AgentLayer, LayerKind, LayerStatus
from muxdesk.team.team_registry import TeamRegistry


class TmuxAgentLayerAdapter:
    """Materialize AgentLayer as an independent tmux claude session -- directly reuses SessionManager
    (which already includes --session-id precise binding, STARTING/READY lifecycle, ready injection gate, load-buffer->paste->Enter sequence)."""

    def __init__(self, manager: SessionManager, registry: TeamRegistry) -> None:
        self._manager = manager
        self._registry = registry

    def spawn(
        self,
        team_id: str,
        node_id: str | None,
        *,
        model: str | None = None,
        title: str | None = None,
        extra_settings: dict | None = None,
    ) -> AgentLayer:
        """Create a new tmux claude session (with session-id + model + optional Stop hook settings) -> build AgentLayer and persist to registry."""
        rec = self._manager.create_session(
            model=model, title=title or f"layer-{node_id or 'root'}", extra_settings=extra_settings
        )
        layer = AgentLayer(
            id=uuid.uuid4().hex,
            team_id=team_id,
            kind=LayerKind.TMUX,
            backend_ref=rec["app_session_id"],
            node_id=node_id,
            status=LayerStatus.SPAWNING,
            log_channels={"tmux_session": rec.get("tmux_session"), "app_session_id": rec["app_session_id"]},
        )
        self._registry.upsert_layer(layer)
        return layer

    def inject(self, layer: AgentLayer, text: str) -> bool:
        """Inject a message into this layer's claude (manager includes ready gate + enter sequence)."""
        return self._manager.submit_user_message(layer.backend_ref, text)

    def is_ready(self, layer: AgentLayer) -> bool:
        rec = self._manager.get(layer.backend_ref)
        return bool(rec) and rec.get("state") == "READY"

    def cc_state(self, layer: AgentLayer) -> str:
        rec = self._manager.get(layer.backend_ref)
        return rec.get("state", "UNKNOWN") if rec else "UNKNOWN"

    def history(self, layer: AgentLayer, after_seq: int = 0) -> list[dict]:
        """Get semantic events for this layer (for orchestrator polling node output / frontend log view)."""
        return self._manager.history(layer.backend_ref, after_seq=after_seq)

    def latest_assistant_text(self, layer: AgentLayer, after_seq: int = 0) -> tuple[str | None, int]:
        """Get the last assistant_message text and its seq after after_seq (used for polling node output)."""
        text, seq = None, after_seq
        for e in self.history(layer, after_seq=after_seq):
            if e.get("event_type") == "assistant_message":
                text = e["payload"].get("text", "")
            seq = max(seq, e.get("seq", seq))
        return text, seq

    def update_status(self, layer: AgentLayer, status: LayerStatus) -> None:
        layer.status = status
        self._registry.upsert_layer(layer)

    def kill(self, layer: AgentLayer) -> None:
        """Kill the tmux session but keep claude_session_id so it can be resumed later."""
        self._manager.archive(layer.backend_ref)
        self.update_status(layer, LayerStatus.COMPLETED)
