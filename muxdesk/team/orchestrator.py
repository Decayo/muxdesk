from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from muxdesk.team.agent_layer import TmuxAgentLayerAdapter
from muxdesk.team.contract import resolve_node_output
from muxdesk.team.models import Edge, Message, MessageRole, Node, NodeStatus
from muxdesk.team.team_registry import TeamRegistry

_TERMINAL = (NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.SKIPPED)
# Root directory for subagent structured output files (orchestrator reads on the same machine)
_TEAM_OUTPUT_ROOT = Path("/tmp/muxdesk-team")
# Three-tier completion signal: fixed file = ground truth, Stop hook = precise per-node, GRACE = last-resort fallback
_POST_STOP_SECONDS = 4.0  # Short grace period after Stop hook to wait for file flush (seconds, far shorter than GRACE)
_NODE_GRACE_SECONDS = 300.0  # Only falls back to this when hook never arrives (process crash)
_MAX_RETRY = 2  # Max auto-repair retries on contract validation failure; exhausted -> waiting_input for manual resolution


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Orchestrator:
    """Thin state machine: advances a linear graph and routes messages. One background tick loop per team (poll-based).

    Each tick advances only one action (serial, following the single-writer pattern):
      pending and all predecessors succeeded -> spawn layer + wait ready + inject prompt (with predecessor outputs) -> running
      running -> poll layer for latest assistant output + contract validation -> succeeded/failed + write handoff message
    """

    def __init__(
        self,
        registry: TeamRegistry,
        adapter: TmuxAgentLayerAdapter,
        *,
        model: str | None = None,
        max_steps: int = 200,
        tick_interval: float = 1.5,
        team_timeout: float = 900.0,
        webhook_base: str = "http://127.0.0.1:8001",
    ) -> None:
        self._registry = registry
        self._adapter = adapter
        self._model = model
        self._max_steps = max_steps
        self._tick = tick_interval
        self._timeout = team_timeout
        self._webhook_base = webhook_base
        self._threads: dict[str, threading.Thread] = {}
        # Stop hook push signal: webhook thread adds, tick thread reads — requires a lock (check-then-act is non-atomic)
        self._done_signals: dict[str, float] = {}
        self._done_lock = threading.Lock()

    # --- Stop hook push signal (subagent turn-end active notification, replaces polling guesswork) ---

    def signal_done(self, team_id: str, node_id: str) -> None:
        """Webhook received subagent Stop hook -> record the turn-end timestamp for that node."""
        with self._done_lock:
            self._done_signals[f"{team_id}:{node_id}"] = time.monotonic()

    def _stop_ts(self, team_id: str, node_id: str) -> float | None:
        with self._done_lock:
            return self._done_signals.get(f"{team_id}:{node_id}")

    def _clear_done(self, team_id: str, node_id: str) -> None:
        with self._done_lock:
            self._done_signals.pop(f"{team_id}:{node_id}", None)

    def _hook_settings(self, node: Node) -> dict:
        """Inject a dedicated Stop hook into each subagent: on turn end, curl the node-done endpoint (tid/nid hardcoded to avoid reverse-lookup)."""
        url = f"{self._webhook_base}/api/muxdesk/team/{node.team_id}/node/{node.id}/done"
        cmd = f"curl -sS -X POST {url} --max-time 2 --retry 1 --retry-delay 0.2 >/dev/null 2>>/tmp/muxdesk-team-hook.log || true"
        return {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}}

    def start(self, team_id: str) -> None:
        existing = self._threads.get(team_id)
        if existing and existing.is_alive():
            return
        t = threading.Thread(target=self._run, args=(team_id,), name=f"cc-orch-{team_id[:6]}", daemon=True)
        self._threads[team_id] = t
        t.start()

    def _run(self, team_id: str) -> None:
        steps = 0
        deadline = time.time() + self._timeout
        while steps < self._max_steps and time.time() < deadline:
            if not self._registry.get_team(team_id):
                return  # Team has been deleted; stop ticking
            nodes = self._registry.list_nodes(team_id)
            if nodes and all(n.status in _TERMINAL for n in nodes):
                return  # All nodes terminal; team complete
            self._tick_once(team_id, nodes)
            steps += 1
            time.sleep(self._tick)
        # Exceeded max steps or timed out: mark remaining pending/running nodes as failed (don't silently stall)
        for n in self._registry.list_nodes(team_id):
            if n.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                n.status = NodeStatus.FAILED
                self._registry.upsert_node(n)

    @staticmethod
    def _preds(edges: list[Edge], node_id: str) -> list[str]:
        return [e.from_node for e in edges if e.to_node == node_id]

    def _tick_once(self, team_id: str, nodes: list[Node]) -> None:
        edges = self._registry.list_edges(team_id)
        by_id = {n.id: n for n in nodes}
        for node in nodes:
            if node.status == NodeStatus.PENDING:
                preds = self._preds(edges, node.id)
                if all(by_id[p].status == NodeStatus.SUCCEEDED for p in preds if p in by_id):
                    self._start_node(node)
                    return  # Advance one node per tick
            elif node.status == NodeStatus.RUNNING:
                self._poll_node(team_id, node, edges, by_id)
                return

    def _start_node(self, node: Node) -> None:
        self._clear_done(node.team_id, node.id)  # Clear any Stop signal left from a previous spawn (re-entry isolation)
        layer = self._adapter.spawn(
            node.team_id,
            node.id,
            model=self._model,
            title=f"{node.team_id[:6]}-{node.id}",
            extra_settings=self._hook_settings(node),
        )
        node.agent_layer_id = layer.id
        node.logs_ref = {"layer_id": layer.id, "last_seq": 0, "injected": False}
        node.status = NodeStatus.RUNNING
        node.started_at = _now()
        self._registry.upsert_node(node)

    @staticmethod
    def _output_path(node: Node) -> Path:
        return _TEAM_OUTPUT_ROOT / node.team_id / "node_output" / f"{node.id}.json"

    def _read_output_file(self, node: Node) -> str | None:
        try:
            return self._output_path(node).read_text(encoding="utf-8")
        except OSError:
            return None

    def _finish_node(self, team_id: str, node: Node, layer_id: str | None, last_seq: int) -> None:
        node.finished_at = _now()
        node.logs_ref = {**node.logs_ref, "last_seq": last_seq}
        self._registry.upsert_node(node)
        self._registry.add_message(
            Message(
                id=uuid.uuid4().hex,
                team_id=team_id,
                from_layer=layer_id,
                to_layer=None,
                role=MessageRole.AGENT,
                payload={
                    "node": node.id,
                    "output": node.outputs,
                    "ok": node.status == NodeStatus.SUCCEEDED,
                    "source": node.output_source,
                    "reason": node.failure_reason,
                },
                created_at=_now(),
            )
        )

    def _poll_node(self, team_id: str, node: Node, edges: list[Edge], by_id: dict[str, Node]) -> None:
        layer = self._registry.get_layer(node.agent_layer_id) if node.agent_layer_id else None
        if not layer:
            node.status = NodeStatus.FAILED
            node.failure_reason = "layer_missing"
            self._finish_node(team_id, node, None, node.logs_ref.get("last_seq", 0))
            return
        if not node.logs_ref.get("injected"):
            # Wait for layer to be ready before injecting prompt (includes predecessor outputs as input + file-write contract)
            if self._adapter.is_ready(layer):
                _, seq = self._adapter.latest_assistant_text(layer)
                self._adapter.inject(layer, self._build_prompt(node, edges, by_id))
                node.logs_ref = {**node.logs_ref, "injected": True, "last_seq": seq, "inject_at": time.time()}
                self._registry.upsert_node(node)
            return
        # Already injected; now determine completion. Three-tier signal (file = ground truth, Stop hook = precise per-node, GRACE = last resort):
        #   (a) Fixed file valid -> immediately succeeded (fastest path, highest priority)
        #   (b) Stop hook received -> retry reading file within POST_STOP short grace (wait for flush); after grace, fallback / declare failed
        #   (c) GRACE timeout (hook never arrived / process crash fallback) -> fallback
        last_seq = node.logs_ref.get("last_seq", 0)
        file_content = self._read_output_file(node)
        if file_content is not None:
            output, source, _ = resolve_node_output(node, file_content, "")
            if output is not None:
                node.outputs, node.output_source, node.status = output, source, NodeStatus.SUCCEEDED
                self._finish_node(team_id, node, layer.id, last_seq)
                return
            # File exists but invalid: leave for stop/GRACE to decide (subagent may still be rewriting)

        stop_ts = self._stop_ts(team_id, node.id)
        if stop_ts is not None:
            # Stop received: wait for file flush within POST_STOP short grace; only fall through to fallback after it expires
            if time.monotonic() - stop_ts < _POST_STOP_SECONDS:
                return
        elif time.time() - node.logs_ref.get("inject_at", time.time()) < _NODE_GRACE_SECONDS:
            return  # Stop not yet received and within GRACE: keep waiting

        # Decision point: Stop grace expired or GRACE timeout -> fenced json fallback + schema validation
        text, seq = self._adapter.latest_assistant_text(layer, after_seq=last_seq)
        new_seq = max(seq, last_seq)
        output, source, reason = resolve_node_output(node, file_content, text or "")
        schema = (node.contract_json or {}).get("output_schema")
        if output is not None:
            node.outputs, node.output_source, node.status = output, source, NodeStatus.SUCCEEDED
            self._finish_node(team_id, node, layer.id, new_seq)
            return
        if not schema and text:
            # Loose compatibility when no schema: accept plain text (don't force file-write-only constraint)
            node.outputs, node.output_source, node.status = {"text": text}, "transcript_text", NodeStatus.SUCCEEDED
            self._finish_node(team_id, node, layer.id, new_seq)
            return
        # Contract validation failed: auto-retry for "fixable format issues" (reinject into same tmux); exhausted -> waiting_input for manual resolution
        node.last_error = reason
        if node.attempt_count < _MAX_RETRY:
            node.attempt_count += 1
            self._retry_node(team_id, node, layer, new_seq, reason)
            return
        node.failure_reason, node.status = reason, NodeStatus.WAITING_INPUT
        node.logs_ref = {**node.logs_ref, "last_seq": new_seq}
        self._registry.upsert_node(node)
        self._emit(team_id, node, layer.id, "waiting_for_manual_resolution", {"attempt": node.attempt_count, "reason": reason})

    def _retry_prompt(self, node: Node, reason: str | None) -> str:
        out_path = self._output_path(node)
        schema = (node.contract_json or {}).get("output_schema")
        base = f"Your previous output did not conform to the contract (error: {reason}). Please rewrite the final result as JSON to the absolute path:\n{out_path}"
        return f"{base}\nJSON must conform to schema: {json.dumps(schema, ensure_ascii=False)}" if schema else base

    def _retry_node(self, team_id: str, node: Node, layer, last_seq: int, reason: str | None) -> None:
        """Reinject correction prompt into the same tmux session (delete old file first to avoid reading stale output from the previous attempt — critical pitfall)."""
        try:
            self._output_path(node).unlink()
        except OSError:
            pass
        self._clear_done(team_id, node.id)  # Clear old Stop signal; wait for a fresh one after the rewrite
        self._adapter.inject(layer, self._retry_prompt(node, reason))
        node.logs_ref = {**node.logs_ref, "injected": True, "last_seq": last_seq, "inject_at": time.time()}
        self._registry.upsert_node(node)
        self._emit(team_id, node, layer.id, "retry_injected", {"attempt": node.attempt_count, "reason": reason})

    def _emit(self, team_id: str, node: Node, layer_id: str | None, event: str, extra: dict) -> None:
        """Write a process event to the message bus (for timeline display; no separate event log needed)."""
        self._registry.add_message(
            Message(
                id=uuid.uuid4().hex,
                team_id=team_id,
                from_layer=layer_id,
                to_layer=None,
                role=MessageRole.SYSTEM,
                payload={"node": node.id, "event": event, **extra},
                created_at=_now(),
            )
        )

    def _build_prompt(self, node: Node, edges: list[Edge], by_id: dict[str, Node]) -> str:
        ctx = ""
        for p in self._preds(edges, node.id):
            pred = by_id.get(p)
            if pred and pred.outputs:
                ctx += f"\n\n[Output from predecessor node {p}]\n{json.dumps(pred.outputs, ensure_ascii=False)}"
        out_path = self._output_path(node)
        schema = (node.contract_json or {}).get("output_schema")
        schema_hint = (
            f", JSON must conform to schema: {json.dumps(schema, ensure_ascii=False)}"
            if schema
            else " (no fixed schema; you may use {\"result\": \"...\"})"
        )
        contract_req = (
            f"\n\n---\nWhen finished, you MUST use the Write tool to write the final result as JSON to the absolute path:\n{out_path}"
            f"{schema_hint}\nIf you cannot write the file, output exactly one ```json ... ``` block at the end as a fallback."
        )
        return f"{node.prompt_markdown}{ctx}{contract_req}".strip()
