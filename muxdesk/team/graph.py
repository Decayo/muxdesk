from __future__ import annotations

from muxdesk.team.models import Edge, Node, NodeType
from muxdesk.team.team_registry import TeamRegistry


def load_graph(registry: TeamRegistry, team_id: str, graph_def: dict) -> None:
    """Load graph_def ({nodes:[...], edges:[...]}) into the registry. Nodes start with status=pending."""
    for nd in graph_def.get("nodes", []):
        registry.upsert_node(
            Node(
                id=nd["id"],
                team_id=team_id,
                node_type=NodeType(nd.get("type", "task")),
                prompt_markdown=nd.get("prompt", ""),
                contract_json=nd.get("contract", {}),
            )
        )
    for ed in graph_def.get("edges", []):
        registry.add_edge(Edge(team_id=team_id, from_node=ed["from"], to_node=ed["to"], condition=ed.get("condition")))


def graph_state(registry: TeamRegistry, team_id: str) -> dict:
    """Return {nodes, edges} for ReactFlow (with live status + corresponding subagent session), for frontend polling."""
    nodes = registry.list_nodes(team_id)
    edges = registry.list_edges(team_id)
    layers = {layer.id: layer for layer in registry.list_layers(team_id)}
    return {
        "nodes": [
            {
                "id": n.id,
                "type": n.node_type.value,
                "status": n.status.value,
                "label": (n.contract_json or {}).get("name") or n.id,
                "layer_id": n.agent_layer_id,
                # Click node -> switch to the corresponding subagent session (= cc app_session_id)
                "app_session_id": layers[n.agent_layer_id].backend_ref
                if n.agent_layer_id and n.agent_layer_id in layers
                else None,
                "has_output": bool(n.outputs),
                "attempt": n.attempt_count,
                "last_error": n.last_error,
            }
            for n in nodes
        ],
        "edges": [{"from": e.from_node, "to": e.to_node, "condition": e.condition} for e in edges],
    }


# Example linear graph (4 nodes: understand -> design -> implement -> review), for MVP acceptance testing
EXAMPLE_LINEAR_GRAPH = {
    "nodes": [
        {
            "id": "understand",
            "type": "task",
            "prompt": "Understand the requirements and output a one-sentence summary.",
            "contract": {
                "name": "understand",
                "output_schema": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
            },
        },
        {"id": "design", "type": "task", "prompt": "Design a solution based on the summary.", "contract": {"name": "design"}},
        {"id": "implement", "type": "task", "prompt": "Implement based on the design.", "contract": {"name": "implement"}},
        {"id": "review", "type": "checker", "prompt": "Check whether the implementation conforms to the design.", "contract": {"name": "review"}},
    ],
    "edges": [
        {"from": "understand", "to": "design"},
        {"from": "design", "to": "implement"},
        {"from": "implement", "to": "review"},
    ],
}
