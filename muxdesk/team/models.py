from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LayerKind(str, Enum):
    ROOT = "root"  # Team's root layer, reuses existing muxdesk single session
    TMUX = "tmux"  # Independent tmux claude subagent (M1 primary)
    CLAUDE_SUBAGENT = "claude_subagent"  # Host claude's native Task subagent (M2)


class LayerStatus(str, Enum):
    SPAWNING = "spawning"
    READY = "ready"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    ERROR = "error"


class NodeType(str, Enum):
    TASK = "task"
    CHECKER = "checker"
    AGENT = "agent"


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class MessageRole(str, Enum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    PLAN = "plan"
    LOG = "log"
    ARTIFACT = "artifact"


@dataclass
class TeamSession:
    """One harness launch = one team (naming = semantic name + date + uuid4)."""

    id: str
    semantic_name: str
    created_at: str
    root_layer_id: str | None = None
    graph_id: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentLayer:
    """Execution container for a subagent. M1 kind=tmux (independent tmux claude session); root reuses an existing session."""

    id: str
    team_id: str
    kind: LayerKind
    backend_ref: str | None = None  # tmux session name / existing cc app_session_id (root)
    node_id: str | None = None
    status: LayerStatus = LayerStatus.SPAWNING
    log_channels: dict = field(default_factory=dict)  # {transcript_path, tmux_session, pane_id}


@dataclass
class Node:
    """Task / checkpoint / agent = markdown prompt + JSON Schema contract + state."""

    id: str
    team_id: str
    node_type: NodeType
    prompt_markdown: str
    contract_json: dict  # {name, description, input_schema, output_schema}
    agent_layer_id: str | None = None
    status: NodeStatus = NodeStatus.PENDING
    inputs: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    logs_ref: dict = field(default_factory=dict)  # {transcript_path, offset}
    # Completion contract metadata (for snapshot facts, frontend projects as header)
    started_at: str | None = None
    finished_at: str | None = None
    output_source: str | None = None  # file / fallback_transcript / transcript_text
    failure_reason: str | None = None  # missing_output_file / invalid_json_file / invalid_fallback_json
    # Contract validation failure auto-repair (6.3): retry count + latest schema error summary
    attempt_count: int = 0
    last_error: str | None = None


@dataclass
class Edge:
    team_id: str
    from_node: str
    to_node: str
    condition: str | None = None  # rule expression (e.g. "outputs.ok == true")


@dataclass
class Message:
    """Atomic unit of inter-subagent communication (routed through orchestrator)."""

    id: str
    team_id: str
    from_layer: str | None
    to_layer: str | None  # None = broadcast / blackboard
    role: MessageRole
    payload: dict
    created_at: str
