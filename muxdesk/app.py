"""muxdesk server — drives interactive Claude Code (tmux) sessions over a web API.

Needs the host's tmux / claude / ~/.claude/projects. Exposes muxdesk REST + WS routes
under /api/muxdesk, using SessionManager directly.

Run standalone:   uvicorn muxdesk.app:app --host 127.0.0.1 --port 8001
Embed elsewhere:  from muxdesk import create_app; app = create_app(settings=...)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from muxdesk.bind import build_checkin, extract_transcript_checkin, validate_contract, would_cycle  # noqa: E402
from muxdesk.event_bus import EventBus, event_to_ws_json  # noqa: E402
from muxdesk.session_manager import SessionManager  # noqa: E402
from muxdesk.session_registry import SessionRegistry  # noqa: E402
from muxdesk.team.team_manager import TeamManager  # noqa: E402
from muxdesk.team.team_registry import TeamRegistry  # noqa: E402
from muxdesk.transcript_parser import parse_record  # noqa: E402
from muxdesk.settings import Settings  # noqa: E402
from muxdesk.preflight import check as preflight_check  # noqa: E402

# Runtime singletons — populated by _configure(): at import for `uvicorn muxdesk.app:app`,
# or by create_app(settings=...) when embedding (e.g. ibkr-trade-journal).
settings: Settings | None = None
bus: EventBus | None = None
registry: SessionRegistry | None = None
manager: SessionManager | None = None
team_registry: TeamRegistry | None = None
team_manager: TeamManager | None = None


def _configure(custom_settings: Settings | None = None) -> None:
    global settings, bus, registry, manager, team_registry, team_manager
    settings = custom_settings or Settings()
    bus = EventBus()
    registry = SessionRegistry(settings.cc_db_path)
    manager = SessionManager(settings, bus, registry)
    team_registry = TeamRegistry("/tmp/muxdesk-team.sqlite3")
    team_manager = TeamManager(manager, team_registry)
    # Background probe loop: silently ping active sessions every 10s; dead ones revived via --resume.
    manager.start_probe_loop(interval=10.0)
    # After restart, re-attach tailers for alive active sessions (repopulates conversation tab).
    manager.rebind_active_sessions()


app = FastAPI(title="muxdesk")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PREFIX = "/api/muxdesk"

_REPO = os.path.dirname(os.path.abspath(__file__))  # Repo root; passed via --add-dir + absolute paths for muxdesk-ask/hook

# Orchestrator (lead) system prompt: force native agent team (teammates as independent tmux sessions),
# instead of degrading to one-shot Task subagents (in practice, claude degrades for simple tasks)
_MUXDESK_ASK = os.path.join(_REPO, "scripts", "muxdesk-ask")
# Shared question-asking rule for all muxdesk sessions: run muxdesk-ask directly via Bash absolute path,
# avoiding both the AskUserQuestion block and the Skill indirection double-misbinding
_ASK_RULE = (
    "【提問規則 — 必讀】要向 user 問選擇題（單選/多選/多題/要他確認方向）時，"
    f"請【直接執行 Bash】：`{_MUXDESK_ASK} '<json>'`（結構化提問，user 在 web 卡片作答，阻塞回傳答案）。"
    "json schema 與 AskUserQuestion 相同：{questions:[{question,header,multiSelect,options:[{label,description}]}]}。"
    "絕對不要用 AskUserQuestion（此環境已停用、PreToolUse 會擋下 → 浪費一輪）。"
    "也不要先呼叫 muxdesk-ask skill 再執行——直接 Bash 跑上面那行即可（skill 只是說明文件，跑它會多一次工具呼叫）。"
)
# Base system prompt for regular sessions (question-asking rules only; lead has additional orchestrator rules)
BASE_SYSTEM_PROMPT = _ASK_RULE
ORCHESTRATOR_SYSTEM_PROMPT = (
    "你是 muxdesk 的 orchestrator（agent team lead）。當任務需要拆成多個獨立的大塊工作時，"
    "務必用 claude code 的 agent team 機制 spawn teammate（每個 teammate 是獨立 tmux session、可被點開互動），"
    "不要用一次性 Task subagent 處理大塊工作。簡單的內部子任務才用 TodoWrite / Task。"
    "每個 teammate 的結論留在自己的 transcript；你負責協調與綜合。"
    + _ASK_RULE
)

# Team event log (persisted from hooks, used for node checkpoint state sync; durable, not dependent on ephemeral ~/.claude/teams filesystem)
_TEAM_EVENTS_LOG = "/tmp/muxdesk-team-events.jsonl"


def _team_hook_settings(base: str = "http://127.0.0.1:8001") -> dict:
    """Build --settings for the lead: teammateMode tmux + team hooks (TaskCreated/TaskCompleted/TeammateIdle report back to muxdesk).

    The hook command POSTs stdin (hook event JSON) as-is to muxdesk, which persists it to the event log.
    """
    url = f"{base}/api/muxdesk/native-teams/hook"
    cmd = (
        f"curl -sS -X POST {url} -H 'Content-Type: application/json' --data-binary @- "
        f"--max-time 2 >/dev/null 2>>/tmp/muxdesk-hook-err.log || true"
    )
    one = [{"hooks": [{"type": "command", "command": cmd}]}]
    return {"teammateMode": "tmux", "hooks": {"TaskCreated": one, "TaskCompleted": one, "TeammateIdle": one}}


_MUXDESK_ASK_HOOK = os.path.join(_REPO, "scripts", "muxdesk-ask-hook")


def _ask_hook_settings() -> dict:
    """PreToolUse hook: intercept AskUserQuestion and redirect to muxdesk-ask (structured questions). Injected into all muxdesk sessions."""
    return {
        "hooks": {
            "PreToolUse": [
                {"matcher": "AskUserQuestion", "hooks": [{"type": "command", "command": f"python3 {_MUXDESK_ASK_HOOK}"}]}
            ]
        }
    }


def _merge_settings(*settings: dict) -> dict:
    """Shallow-merge multiple settings dicts; hook event keys are merged (appended, not overwritten)."""
    out: dict = {}
    for s in settings:
        for k, v in s.items():
            if k == "hooks" and isinstance(v, dict):
                hooks = out.setdefault("hooks", {})
                for ev, arr in v.items():
                    hooks.setdefault(ev, []).extend(arr)
            else:
                out[k] = v
    return out


@app.get("/api/muxdesk/health")
def health() -> dict:
    return {"ok": True, "workspace": settings.cc_workspace_path}


@app.get(f"{PREFIX}/preflight")
def preflight() -> dict:
    """Report runtime dependency status (tmux / claude / login / python) so the web
    UI can warn explicitly when the demo can't actually drive an interactive session."""
    claude_home = str(Path(settings.cc_claude_projects_dir).expanduser().parent)
    return preflight_check(settings.cc_claude_command, claude_home)


# --- claude code native agents (Agent View / Agent Teams): muxdesk as web dashboard reader ---


def _live_agents() -> list[dict]:
    """List all claude code live background sessions (including agent team lead/teammates: pid/cwd/sessionId)."""
    try:
        proc = subprocess.run(["claude", "agents", "--json"], capture_output=True, timeout=10, text=True)
        return json.loads(proc.stdout) if proc.stdout.strip() else []
    except Exception:  # noqa: BLE001 — best-effort; return empty if claude is absent or timed out
        return []


def _proc_ppid(pid: int) -> int | None:
    """Read /proc/<pid>/stat to get PPID (comm may contain spaces/parens; ppid is the 2nd field after the last ')')."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            data = f.read()
    except OSError:
        return None
    rhs = data[data.rfind(")") + 1 :].split()  # rhs[0]=state, rhs[1]=ppid
    return int(rhs[1]) if len(rhs) >= 2 and rhs[1].isdigit() else None


def _is_descendant(child_pid: int, ancestor_pid: int, max_hops: int = 6) -> bool:
    """Check if child_pid equals or is a descendant of ancestor_pid (walks PPID chain; tolerates shell wrappers)."""
    pid = child_pid
    for _ in range(max_hops):
        if pid == ancestor_pid:
            return True
        ppid = _proc_ppid(pid)
        if not ppid or ppid <= 1:
            return False
        pid = ppid
    return False


def _resolve_member_sessions(members: list[dict]) -> list[dict]:
    """Associate each team member with a claude sessionId (used by frontend to switch to that teammate's session on node click).

    Primary path: member.tmuxPaneId -> pane_pid -> matching live agent (pid equals or is an ancestor).
    Fallback: member.cwd is unique among live agents -> use that sessionId.
    No match -> sessionId=None (frontend disables clicking that node to avoid mismatches).
    """
    agents = _live_agents()
    by_pid = {a.get("pid"): a for a in agents if a.get("pid")}
    cwd_groups: dict[str, list[dict]] = {}
    for a in agents:
        cwd_groups.setdefault(a.get("cwd") or "", []).append(a)

    out: list[dict] = []
    for m in members:
        sid = None
        pane = m.get("tmuxPaneId")
        if pane:
            ppid = manager.pane_pid(pane)
            if ppid:
                hit = by_pid.get(ppid) or next(
                    (a for a in agents if _is_descendant(a.get("pid", 0), ppid)), None
                )
                if hit:
                    sid = hit.get("sessionId")
        if sid is None:  # tmuxPaneId match failed -> fall back to cwd only if unique
            grp = cwd_groups.get(m.get("cwd") or "", [])
            if len(grp) == 1:
                sid = grp[0].get("sessionId")
        out.append({**m, "sessionId": sid})
    return out


@app.get(f"{PREFIX}/native-agents")
def native_agents() -> dict:
    """List all claude code live background sessions (including agent team lead/teammates)."""
    return {"items": _live_agents()}


def _parse_jsonl_events(path: Path) -> list[dict]:
    """Read a claude transcript jsonl and parse_record into semantic events (with seq). Shared by native + subagent."""
    events: list[dict] = []
    seq = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            for ev in parse_record(json.loads(line)):
                seq += 1
                events.append({**ev, "seq": seq})
        except Exception:  # noqa: BLE001 — tolerate partial lines / unknown formats
            continue
    return events


def _lead_project_dir(sid: str) -> Path | None:
    """Find the projects/<slug> directory for a lead claude session id (by scanning, to avoid slug-rule pitfalls)."""
    projects = Path(settings.cc_claude_projects_dir).expanduser()
    if not projects.exists():
        return None
    for d in projects.iterdir():
        if (d / f"{sid}.jsonl").exists() or (d / sid).is_dir():
            return d
    return None


@app.get(f"{PREFIX}/native-agents/transcript")
def native_transcript(sid: str, cwd: str) -> dict:
    """Read a native session's jsonl transcript (~/.claude/projects/<slug>/<sid>.jsonl) and parse into events."""
    slug = cwd.replace("/", "-")  # claude projects slug: replace / with -
    path = Path(settings.cc_claude_projects_dir) / slug / f"{sid}.jsonl"
    if not path.exists():
        return {"items": [], "found": False}
    return {"items": _parse_jsonl_events(path), "found": True}


@app.get(f"{PREFIX}/native-agents/subagent-transcript")
def subagent_transcript(sid: str, agent_id: str) -> dict:
    """Read a Task subagent transcript for the lead (projects/<slug>/<sid>/subagents/agent-<agentId>.jsonl)."""
    proj = _lead_project_dir(sid)
    if proj is None:
        return {"items": [], "found": False}
    path = proj / sid / "subagents" / f"agent-{agent_id}.jsonl"
    if not path.exists():
        return {"items": [], "found": False}
    return {"items": _parse_jsonl_events(path), "found": True}


@app.post(f"{PREFIX}/lead")
def create_lead(body: dict = Body(default={})) -> dict:
    """Create an agent team orchestrator (lead): a cc session (in tmux) with a system prompt forcing native team mode.

    EXPERIMENTAL_AGENT_TEAMS is provided by the global ~/.claude/settings.json; teammateMode tmux ensures teammates use split-pane.
    The returned session is the lead; the frontend binds the conversation view to it so the user interacts directly.
    """
    model = body.get("model") or "claude-sonnet-4-6"
    return manager.create_session(
        model=model,
        title="orchestrator",
        system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
        extra_settings=_merge_settings(_team_hook_settings(), _ask_hook_settings()),
        add_dirs=[_REPO],  # Include muxdesk-ask skill (question-asking conventions)
    )


@app.post(f"{PREFIX}/native-teams/hook")
async def native_team_hook(request: Request) -> dict:
    """Receive claude code team hooks (TaskCreated/TaskCompleted/TeammateIdle) and persist to the event log (for node checkpoint sync)."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    rec = {"ts": time.time(), **(payload if isinstance(payload, dict) else {"raw": payload})}
    try:
        with open(_TEAM_EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return {"ok": True}


@app.get(f"{PREFIX}/native-teams/events")
def native_team_events(after: float = 0.0) -> dict:
    """Read the persistent team event log (frontend polls to update node checkpoints)."""
    events: list[dict] = []
    try:
        with open(_TEAM_EVENTS_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if ev.get("ts", 0) > after:
                        events.append(ev)
                except Exception:  # noqa: BLE001
                    continue
    except OSError:
        pass
    return {"items": events}


@app.get(f"{PREFIX}/native-teams")
def native_teams() -> dict:
    """Read ~/.claude/teams/ to list active agent teams (config members + tasks + inboxes mailbox).

    Teams only exist while active (deleted on cleanup/session end). teams/{key}/ and tasks/{key}/ share the same key.
    """
    home = Path.home()
    teams_dir = home / ".claude" / "teams"
    tasks_dir = home / ".claude" / "tasks"
    out: list[dict] = []
    if not teams_dir.exists():
        return {"items": out}
    for tdir in sorted(teams_dir.iterdir()):
        if not tdir.is_dir():
            continue
        key = tdir.name
        members: list[dict] = []
        cfg = tdir / "config.json"
        if cfg.exists():
            try:
                members = json.loads(cfg.read_text(encoding="utf-8")).get("members", [])
            except Exception:  # noqa: BLE001
                members = []
        inboxes: dict[str, list] = {}
        ibox = tdir / "inboxes"
        if ibox.exists():
            for f in ibox.glob("*.json"):
                try:
                    inboxes[f.stem] = json.loads(f.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    continue
        tasks: list[dict] = []
        tdir2 = tasks_dir / key
        if tdir2.exists():
            for tf in sorted(tdir2.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
                try:
                    tasks.append(json.loads(tf.read_text(encoding="utf-8")))
                except Exception:  # noqa: BLE001
                    continue
        out.append(
            {"key": key, "members": _resolve_member_sessions(members), "tasks": tasks, "inboxes": inboxes}
        )
    return {"items": out}


def _lead_done_tool_ids(lead_jsonl: Path) -> set[str]:
    """Collect toolUseIds from the lead's main jsonl that received successful tool_results -> subagent completion status."""
    done: set[str] = set()
    if not lead_jsonl.exists():
        return done
    for line in lead_jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        msg = r.get("message") if isinstance(r, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and not b.get("is_error"):
                    tid = b.get("tool_use_id")
                    if tid:
                        done.add(tid)
    return done


def _subagent_stats(jsonl: Path) -> tuple[int, int]:
    """Return (tool_uses, tokens).

    - tool_uses: count of unique tool_use ids (matches claude TUI's "N tool uses" display).
    - tokens: peak context = max over turns of (input + cache_creation + output). Matches the token count shown
      in claude TUI (e.g. resource-scout = 3937+6228 ~ 10.2k); does not accumulate cache_read (same context re-read each turn would inflate).
    """
    if not jsonl.exists():
        return 0, 0
    tool_ids: set[str] = set()
    peak = 0
    for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        m = r.get("message") if isinstance(r, dict) else None
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    tool_ids.add(b["id"])
        if m.get("role") == "assistant":
            u = m.get("usage") or {}
            if u:
                ctx = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("output_tokens", 0)
                peak = max(peak, ctx)
    return len(tool_ids), peak


@app.get(f"{PREFIX}/native-teams/subagents")
def native_team_subagents(sid: str) -> dict:
    """Read the lead's Task subagents (projects/<slug>/<sid>/subagents/*.meta.json) as nodes.

    For simple tasks, claude uses Task subagents (not native teams, doesn't write to ~/.claude/teams),
    but does write subagents/agent-*.jsonl + .meta.json. Decision 8: both paths must render to avoid a broken node graph.
    """
    proj = _lead_project_dir(sid)
    if proj is None:
        return {"items": [], "found": False}
    sub_dir = proj / sid / "subagents"
    if not sub_dir.is_dir():
        return {"items": [], "found": False}
    done_ids = _lead_done_tool_ids(proj / f"{sid}.jsonl")
    items: list[dict] = []
    for meta in sorted(sub_dir.glob("agent-*.meta.json")):
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        agent_id = meta.name[len("agent-") : -len(".meta.json")]
        tool_uses, tokens = _subagent_stats(sub_dir / f"agent-{agent_id}.jsonl")
        try:
            ts = meta.stat().st_mtime  # Spawn time (frontend uses it to batch: keep only the latest wave)
        except OSError:
            ts = 0.0
        items.append(
            {
                "name": m.get("name") or agent_id,
                "agentType": m.get("agentType") or "general-purpose",
                "description": m.get("description"),
                "agentId": agent_id,
                "toolUseId": m.get("toolUseId"),
                "status": "completed" if m.get("toolUseId") in done_ids else "in_progress",
                "toolUses": tool_uses,
                "tokens": tokens,
                "ts": ts,
            }
        )
    return {"items": items, "found": True, "leadSessionId": sid}


# --- multi-agent team ---


@app.post(f"{PREFIX}/team")
def create_team(body: dict = Body(default={})) -> dict:
    return team_manager.create_team(
        body.get("semantic_name", "team"),
        body.get("graph", {}),
        model=body.get("model"),
        auto_start=body.get("auto_start", True),
    )


@app.get(f"{PREFIX}/team")
def list_teams() -> dict:
    return {"items": team_manager.list_teams()}


@app.get(f"{PREFIX}/team/{{tid}}/graph-state")
def team_graph_state(tid: str) -> dict:
    return team_manager.graph_state(tid)


@app.get(f"{PREFIX}/team/{{tid}}/messages")
def team_messages(tid: str, after_seq: int = 0) -> dict:
    return {"items": team_manager.messages(tid, after_seq=after_seq)}


@app.get(f"{PREFIX}/team/{{tid}}/node/{{nid}}/log")
def team_node_log(tid: str, nid: str, after_seq: int = 0) -> dict:
    return {"items": team_manager.node_log(tid, nid, after_seq=after_seq)}


@app.post(f"{PREFIX}/team/{{tid}}/node/{{nid}}/input")
def team_node_input(tid: str, nid: str, body: dict = Body(default={})) -> dict:
    return {"ok": team_manager.submit_node_input(tid, nid, body.get("text", ""))}


@app.post(f"{PREFIX}/team/{{tid}}/node/{{nid}}/done")
def team_node_done(tid: str, nid: str) -> dict:
    """Stop hook callback from a subagent: marks the node's turn as complete (push-based handoff, replaces polling guesswork)."""
    team_manager.signal_node_done(tid, nid)
    return {"ok": True}


@app.get(f"{PREFIX}/team/{{tid}}/layers")
def team_layers(tid: str) -> dict:
    """Return the team's subagent layers (used for indented sidebar tree display)."""
    return {"items": team_manager.layers(tid)}


@app.delete(f"{PREFIX}/team/{{tid}}")
def delete_team(tid: str) -> dict:
    """Fully delete a team: kill and remove all subagent sessions, stop the orchestrator, and cascade-delete from the registry."""
    team_manager.delete_team(tid)
    return {"ok": True}


@app.post(f"{PREFIX}/team/{{tid}}/node/{{nid}}/retry")
def team_node_retry(tid: str, nid: str) -> dict:
    """Manually retry a node that failed contract validation (reset the node and restart its orchestrator)."""
    return {"ok": team_manager.retry_node(tid, nid)}


@app.post(f"{PREFIX}/team/{{tid}}/node/{{nid}}/mark-failed")
def team_node_mark_failed(tid: str, nid: str) -> dict:
    """Manually abandon a node: mark it failed and cascade-skip all downstream nodes."""
    return {"ok": team_manager.mark_node_failed(tid, nid)}


@app.post(f"{PREFIX}/sessions")
def create_session(body: dict = Body(default={})) -> dict:
    return manager.create_session(
        workspace_path=body.get("workspace_path"),
        model=body.get("model"),
        title=body.get("title"),
        extra_settings=_ask_hook_settings(),  # Regular sessions also intercept AskUserQuestion -> muxdesk-ask
        system_prompt=BASE_SYSTEM_PROMPT,  # Question-asking rules: run muxdesk-ask directly via Bash, skip misbinding indirection
        add_dirs=[_REPO],  # Include muxdesk-ask skill (question-asking conventions, as documentation)
    )


@app.get(f"{PREFIX}/sessions")
def list_sessions(status: str | None = None) -> dict:
    return {"items": manager.list_sessions(status=status)}


# Quick command bar above the conversation (per-project): clicking a button injects text into claude (slash command / natural language).
# Config path via env MUXDESK_HARNESS_CONFIG (default: backend/harness.json); format in harness.example.json. No file -> empty bar (frontend hides it).
_HARNESS_CONFIG = os.environ.get("MUXDESK_HARNESS_CONFIG") or os.path.join(_REPO, "harness.json")


@app.get(f"{PREFIX}/harness")
def harness_config() -> dict:
    try:
        return json.loads(Path(_HARNESS_CONFIG).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"groups": []}


@app.get(f"{PREFIX}/sessions/{{sid}}")
def get_session(sid: str) -> dict | None:
    return manager.get(sid)


@app.post(f"{PREFIX}/sessions/{{sid}}/archive")
def archive_session(sid: str) -> dict | None:
    manager.archive(sid)
    return manager.get(sid)


@app.post(f"{PREFIX}/sessions/{{sid}}/resume")
def resume_session(sid: str) -> dict | None:
    return manager.resume_session(sid)


@app.post(f"{PREFIX}/sessions/{{sid}}/probe")
def probe_session(sid: str) -> dict:
    """Probe and recover a session (rate limit / dead -> retry / --resume revival). Includes orchestrators themselves."""
    return manager.probe_recover(sid)


@app.post(f"{PREFIX}/sessions/{{sid}}/bind")
def bind_session(sid: str, body: dict = Body(default={})) -> dict | None:
    """Bind a session under a parent (tree) with an optional contract. Module 4 · 4b."""
    if not manager.get(sid):
        raise HTTPException(status_code=404, detail="session not found")
    parent = body.get("parent_session_id")
    contract = body.get("contract")
    ok, errors = validate_contract(contract)
    if not ok:
        raise HTTPException(status_code=422, detail={"errors": errors})
    if parent:
        if not manager.get(parent):
            raise HTTPException(status_code=404, detail="parent session not found")
        if would_cycle(lambda i: (manager.get(i) or {}).get("parent_session_id"), sid, parent):
            raise HTTPException(status_code=409, detail="bind would create a cycle")
    fields = {"parent_session_id": parent, "bind_contract": contract}
    if body.get("project"):
        fields["project"] = body["project"]
    registry.update(sid, **fields)
    return manager.get(sid)


@app.post(f"{PREFIX}/sessions/{{sid}}/unbind")
def unbind_session(sid: str) -> dict | None:
    """Detach a session from its parent and clear its contract. Module 4 · 4b."""
    if not manager.get(sid):
        raise HTTPException(status_code=404, detail="session not found")
    registry.update(sid, parent_session_id=None, bind_contract=None)
    return manager.get(sid)


@app.post(f"{PREFIX}/sessions/{{sid}}/relay")
def relay_to_session(sid: str, body: dict = Body(default={})) -> dict:
    """Parent -> child: inject a message into the bound session. Module 4 · 4d."""
    if not manager.get(sid):
        raise HTTPException(status_code=404, detail="session not found")
    text = (body or {}).get("text", "")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422, detail="text is required")
    return {"ok": bool(manager.submit_user_message(sid, text))}


@app.post(f"{PREFIX}/sessions/{{sid}}/checkin")
def session_checkin(sid: str, body: dict = Body(default={})) -> dict:
    """Child -> parent: report a check-in; validate against the contract, push to the parent's bus. Module 4 · 4c."""
    record = manager.get(sid)
    if not record:
        raise HTTPException(status_code=404, detail="session not found")
    parent, payload, result = build_checkin(record, body)
    if parent:
        bus.publish(parent, "child_checkin", payload)
    return {**result, "delivered_to_parent": bool(parent)}


@app.post(f"{PREFIX}/checkin-by-claude")
def checkin_by_claude(body: dict = Body(default={})) -> dict:
    """Stop-hook entry: map the claude session id to its muxdesk session, build a check-in from the
    transcript tail, validate, and push to the parent's bus. No-ops cleanly for unbound sessions. Module 4 · 4c."""
    claude_id = (body or {}).get("session_id")
    if not claude_id:
        raise HTTPException(status_code=422, detail="session_id is required")
    record = registry.get_by_claude_session_id(str(claude_id))
    if not record:
        return {"ok": True, "matched": False, "delivered_to_parent": False}
    checkin_body = extract_transcript_checkin(record.get("transcript_path"))
    parent, payload, result = build_checkin(record, checkin_body)
    if parent:
        bus.publish(parent, "child_checkin", payload)
    return {**result, "matched": True, "delivered_to_parent": bool(parent)}


# --- claude TUI interactive menus (/model, AskUserQuestion, etc.): capture-pane detection -> clickable in conversation ---
# These menus don't write to jsonl (pure TUI), so we can only scrape the screen; best-effort, screen changes may cause missed detections.
_MENU_OPT_RE = re.compile(r"^\s*([❯>›])?\s*(\d+)\.\s+(\S.*?)\s*$")
_MENU_CANCEL_RE = re.compile(r"Esc to cancel", re.IGNORECASE)
_MENU_CB_RE = re.compile(r"^\[([ xX✔✓])\]\s*(.*)$")  # Multi-select checkbox prefix: [ ] / [✔]
_STAGE_BOXES = "☐☒☑✔✓"
_STAGE_RE = re.compile(r"([☐☒☑✔✓])\s*([^\s☐☒☑✔✓←→]+)")


def _parse_stages(lines: list[str]) -> list[dict]:
    """Parse multi-question AskUserQuestion stage row: `← ☒Language ☐Challenges ✔Submit →` -> [{name, done}]."""
    for line in lines:
        if ("Submit" in line or "→" in line) and any(b in line for b in "☐☒☑"):
            stages = [{"name": m.group(2), "done": m.group(1) in "☒☑✔✓"} for m in _STAGE_RE.finditer(line)]
            if len(stages) >= 2:
                return stages
    return []


def _parse_menu(screen: str) -> dict:
    """Detect interactive menus (/model, AskUserQuestion, etc.): find the "Esc to cancel" footer, then collect "N. option" lines upward.

    Supports multi-select (options with `[ ]`/`[✔]` checkbox -> multiSelect + per-item checked) and multi-question (top `☒/☐` stage row).
    Options start from 1; scanning stops at index==1 (top of options). Separator lines `───` / description sub-lines / blanks are skipped.
    """
    lines = screen.splitlines()
    footer_idx = next((i for i in range(len(lines) - 1, -1, -1) if _MENU_CANCEL_RE.search(lines[i])), -1)
    if footer_idx < 0:
        return {"active": False, "options": []}
    options: dict[int, dict] = {}
    current = 0
    top_idx = footer_idx
    desc_buf: list[str] = []  # Indented description sub-lines for an option (appear below it; encountered first when scanning upward -> buffered and attached to that option)
    multi = False
    for i in range(footer_idx - 1, max(-1, footer_idx - 50), -1):
        line = lines[i]
        m = _MENU_OPT_RE.match(line)
        if m:
            idx = int(m.group(2))
            raw = re.sub(r"\s{2,}", " — ", m.group(3)).strip()
            checked = None
            cb = _MENU_CB_RE.match(raw)
            if cb:  # Multi-select option: [ ] unchecked / [✔] checked
                multi = True
                checked = cb.group(1).strip() != ""
                raw = cb.group(2).strip()
            options.setdefault(idx, {
                "index": idx,
                "label": raw,
                "description": " ".join(reversed(desc_buf)).strip(),
                "current": bool(m.group(1)),
                "checked": checked,
            })
            if m.group(1):
                current = idx
            desc_buf = []
            top_idx = i
            if idx == 1:  # Options start from 1; reaching 1 means top of options area
                break
        elif "───" in line or not line.strip():
            desc_buf = []  # Boundary (separator / blank) -> clear buffer
        else:
            desc_buf.append(line.strip())  # Indented description line
    if len(options) < 2:
        return {"active": False, "options": []}
    # Title: first non-empty, non-separator, non-stage line above the options area (claude's "question" line)
    title = ""
    for j in range(top_idx - 1, max(-1, top_idx - 4), -1):
        s = lines[j].strip()
        if s and "───" not in s and not any(b in s for b in _STAGE_BOXES):
            title = s
            break
    if not multi:
        multi = bool(re.search(r"one or more|多選|複選|select.*multiple", screen, re.IGNORECASE))
    opts = [options[k] for k in sorted(options)]
    return {
        "active": True, "title": title, "multiSelect": multi,
        "stages": _parse_stages(lines), "options": opts, "current": current or opts[0]["index"],
    }


@app.get(f"{PREFIX}/sessions/{{sid}}/menu")
def session_menu(sid: str) -> dict:
    """Detect whether this session is waiting for a TUI menu selection (frontend polls -> displays clickable options)."""
    return _parse_menu(manager.capture_screen(sid, lines=40))


@app.post(f"{PREFIX}/sessions/{{sid}}/menu/select")
def session_menu_select(sid: str, body: dict = Body(default={})) -> dict:
    """Select an option: navigate from the current selection (❯) via arrow keys to the target -> Enter (most reliable, avoids digit input being swallowed)."""
    target = int(body.get("index", 0))
    menu = _parse_menu(manager.capture_screen(sid, lines=40))
    if not menu.get("active"):
        return {"ok": False, "reason": "no_menu"}
    current = int(menu.get("current") or menu["options"][0]["index"])
    delta = target - current
    key = "Down" if delta > 0 else "Up"
    for _ in range(abs(delta)):
        manager.send_keys(sid, key)
        time.sleep(0.05)
    manager.send_keys(sid, "Enter")
    return {"ok": True, "selected": target}


@app.post(f"{PREFIX}/sessions/{{sid}}/menu/select-multi")
def session_menu_select_multi(sid: str, body: dict = Body(default={})) -> dict:
    """Multi-select submit (AskUserQuestion multiSelect): navigate to each index + Space to toggle, then Enter.

    indices = the diff set to toggle (frontend computes: only those differing from current checked state); empty set = no change, just Enter to submit.
    """
    indices = sorted({int(x) for x in body.get("indices", []) if str(x).lstrip("-").isdigit()})
    menu = _parse_menu(manager.capture_screen(sid, lines=40))
    if not menu.get("active"):
        return {"ok": False, "reason": "no_menu"}
    current = int(menu.get("current") or menu["options"][0]["index"])
    for target in indices:
        for _ in range(abs(target - current)):
            manager.send_keys(sid, "Down" if target > current else "Up")
            time.sleep(0.04)
        manager.send_keys(sid, "Space")  # Toggle this item (multiSelect)
        time.sleep(0.04)
        current = target
    # Multi-question (has stages) -> Tab to advance to next question (in practice, multi-select submit/advance uses Tab, not Enter); single question -> Enter to submit
    manager.send_keys(sid, "Tab" if menu.get("stages") else "Enter")
    return {"ok": True, "selected": indices}


@app.post(f"{PREFIX}/sessions/{{sid}}/menu/custom")
def session_menu_custom(sid: str, body: dict = Body(default={})) -> dict:
    """Custom answer (AskUserQuestion's 'Type something'): press the option number to enter text mode -> type character by character -> Enter."""
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "reason": "empty"}
    menu = _parse_menu(manager.capture_screen(sid, lines=40))
    if not menu.get("active"):
        return {"ok": False, "reason": "no_menu"}
    ts = next((o for o in menu["options"] if "type something" in o["label"].lower()), None)
    if not ts:
        return {"ok": False, "reason": "no_type_something"}
    manager.send_keys(sid, str(ts["index"]))  # Jump to Type something -> enter text mode
    time.sleep(0.15)
    manager.type_text(sid, text)  # Type custom answer character by character
    time.sleep(0.15)
    manager.send_keys(sid, "Enter")
    return {"ok": True, "text": text}


@app.post(f"{PREFIX}/sessions/{{sid}}/menu/cancel")
def session_menu_cancel(sid: str) -> dict:
    """Cancel the menu (Esc)."""
    manager.send_keys(sid, "Escape")
    return {"ok": True}


_SWITCH_CONFIRM_RE = re.compile(r"Switch model\?", re.IGNORECASE)


def _is_switch_model_confirm(screen: str) -> bool:
    """Detect the claude `/model` switch confirmation dialog (non-standard menu without Esc footer, so _parse_menu can't catch it)."""
    return bool(_SWITCH_CONFIRM_RE.search(screen)) and "Yes" in screen


@app.post(f"{PREFIX}/sessions/{{sid}}/switch-model")
def session_switch_model(sid: str, body: dict = Body(default={})) -> dict:
    """Switch the model for a running session: send `/model X` -> if a "Switch model?" confirmation dialog appears, auto-press Enter to confirm (cursor defaults to Yes).

    `/model` shows a confirmation dialog when the conversation has cache; without pressing, it gets stuck in SUBMITTING. We detect the dialog and confirm on behalf of the user,
    making the switch one-click; if no dialog appears (direct switch), no action is taken.
    """
    model = (body.get("model") or "").strip()
    if not model:
        return {"ok": False, "reason": "no_model"}
    if not manager.submit_user_message(sid, f"/model {model}"):
        return {"ok": False, "reason": "not_ready"}
    deadline = time.time() + 4.0
    while time.time() < deadline:
        if _is_switch_model_confirm(manager.capture_screen(sid, lines=40)):
            time.sleep(0.1)  # Wait for dialog to render stably before pressing
            manager.send_keys(sid, "Enter")  # Cursor defaults to "Yes, switch" -> Enter to confirm
            return {"ok": True, "confirmed": True}
        time.sleep(0.2)
    return {"ok": True, "confirmed": False}  # No confirmation dialog = already switched directly


# --- muxdesk-ask structured questions (replaces AskUserQuestion TUI; hook redirects -> muxdesk-ask writes req -> frontend card -> POST answer) ---

_ASK_DIR = Path("/tmp/muxdesk-ask")


def _pending_ask_for(sid: str) -> dict | None:
    """Find a pending structured question for a session: req.pid must be a descendant of the session's pane_pid (muxdesk-ask runs inside that pane)."""
    rec = registry.get(sid)
    pane = rec.get("pane_id") if rec else None
    if not pane or not _ASK_DIR.exists():
        return None
    pane_pid = manager.pane_pid(pane)
    if not pane_pid:
        return None
    best: dict | None = None
    for req_file in _ASK_DIR.glob("*.req.json"):
        try:
            req = json.loads(req_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        rpid = req.get("pid")
        if isinstance(rpid, int) and _is_descendant(rpid, pane_pid):
            if best is None or req.get("ts", 0) > best.get("ts", 0):  # Multiple matches: take the newest
                best = req
    return best


@app.get(f"{PREFIX}/sessions/{{sid}}/ask")
def session_ask(sid: str) -> dict:
    """Frontend poll: check if this session has a pending structured question (initiated by muxdesk-ask)."""
    req = _pending_ask_for(sid)
    if not req:
        return {"active": False}
    return {"active": True, "reqid": req["reqid"], "questions": req.get("questions", [])}


@app.post(f"{PREFIX}/sessions/{{sid}}/ask")
def session_ask_answer(sid: str, body: dict = Body(default={})) -> dict:
    """Submit answer -> write ans.json, unblocking muxdesk-ask. answers = {questionIndex: value} (single-select str / multi-select list / custom str)."""
    reqid = body.get("reqid")
    if not reqid:
        return {"ok": False, "reason": "no_reqid"}
    _ASK_DIR.mkdir(parents=True, exist_ok=True)
    (_ASK_DIR / f"{reqid}.ans.json").write_text(
        json.dumps({"answers": body.get("answers") or {}}, ensure_ascii=False), encoding="utf-8"
    )
    return {"ok": True}


@app.post(f"{PREFIX}/sessions/{{sid}}/ask/cancel")
def session_ask_cancel(sid: str, body: dict = Body(default={})) -> dict:
    """Cancel the question -> write cancelled flag; muxdesk-ask returns "user cancelled" to claude."""
    reqid = body.get("reqid")
    if not reqid:
        return {"ok": False, "reason": "no_reqid"}
    _ASK_DIR.mkdir(parents=True, exist_ok=True)
    (_ASK_DIR / f"{reqid}.ans.json").write_text(json.dumps({"cancelled": True}), encoding="utf-8")
    return {"ok": True}


# --- Image paste: web can't send clipboard images directly to tmux claude -> save to file, include absolute path in message for claude to Read ---

_IMG_DIR = Path("/tmp/muxdesk-img")
_IMG_EXT = {"png", "jpg", "jpeg", "gif", "webp"}


@app.post(f"{PREFIX}/sessions/{{sid}}/image")
def session_upload_image(sid: str, body: dict = Body(default={})) -> dict:
    """Receive a base64 image (no multipart dependency) -> save to /tmp/muxdesk-img/<uuid>.<ext> -> return absolute path.

    Called when the frontend pastes an image; the returned path is included in the message text for claude to Read.
    """
    data = body.get("data_base64") or ""
    ext = (body.get("ext") or "png").lstrip(".").lower()
    if ext == "jpe":
        ext = "jpg"
    if ext not in _IMG_EXT:
        ext = "png"
    if not data:
        return {"ok": False, "reason": "no_data"}
    try:
        raw = base64.b64decode(data.split(",", 1)[-1])  # Tolerate data URL prefix
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "bad_data"}
    if not raw or len(raw) > 20 * 1024 * 1024:  # 20MB limit to prevent abuse
        return {"ok": False, "reason": "empty_or_too_large"}
    _IMG_DIR.mkdir(parents=True, exist_ok=True)
    path = _IMG_DIR / f"{uuid.uuid4().hex}.{ext}"
    path.write_bytes(raw)
    return {"ok": True, "path": str(path)}


# --- Live preview: claude writes the full assistant block to jsonl only when complete (long wait) -> during streaming,
# scrape the TUI to show in-progress content; once persisted, frontend swaps in clean markdown ---
#
# Note: the new claude TUI has no persistent spinner during "text streaming" (spinner only briefly appears during pure
# thinking), and reply text is placed above the input box on the same layout as idle -> can't use spinner to detect working.
# Instead we use "content change detection": if the reply block differs between two consecutive captures = actively producing;
# spinner is kept as a working signal for the pure thinking phase.

_LIVE_SPINNER_RE = re.compile(r"\(\d+s\b|esc to interrupt|Running…|[✻✽✢✲✶★]\s*\S+…")
# Reply block upper boundary: user echo prompt / tool output (⎿) / box separator / status bar / footer -> stop on encounter (avoid dumping old conversation as silhouette)
_LIVE_BOUNDARY_RE = re.compile(
    r"^\s*[❯>]\s|⎿|^\s*[─━]{3,}|\d+(?:\.\d+)?%\s*·.*tokens|"
    r"accept edits on|shift\+tab to cycle|Press up to edit|Claude in Chrome|engineer-professional"
)
_LIVE_LAST: dict[str, tuple[str, float]] = {}  # sid -> (last reply block, last change time) for content change detection + grace


def _live_block(screen: str) -> str:
    """Extract claude's current reply block: anchor at the bottom input prompt (❯), collect upward to the first boundary. No reply content -> empty."""
    lines = screen.splitlines()
    prompt_i = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].lstrip().startswith("❯")), len(lines))
    # Skip input box boundary separators / blanks above the prompt to locate the end of conversation content
    end = prompt_i
    while end > 0 and (not lines[end - 1].strip() or re.match(r"^\s*[─━]{3,}", lines[end - 1])):
        end -= 1
    block: list[str] = []
    for i in range(end - 1, -1, -1):
        ln = lines[i].rstrip()
        if not ln.strip():
            if block:
                block.append("")  # Preserve blank line structure within the reply
            continue
        if _LIVE_BOUNDARY_RE.search(ln):
            break
        block.append(ln)
        if ln.lstrip().startswith("●"):  # Start of assistant message -> collection complete
            break
    block.reverse()
    return "\n".join(block).strip()


@app.get(f"{PREFIX}/sessions/{{sid}}/live")
def session_live(sid: str) -> dict:
    screen = manager.capture_screen(sid, lines=40)
    text = _live_block(screen)
    prev_text, last_change = _LIVE_LAST.get(sid, ("", 0.0))
    now = time.time()
    spinner = bool(_LIVE_SPINNER_RE.search(screen))
    if text and text != prev_text:
        last_change = now  # Content changed -> update change time
    elif not text and prev_text and (now - last_change) < 2.5:
        # This capture got blank (TUI redraw/scroll instant) but still within grace -> keep previous text to avoid preview flash
        text = prev_text
    _LIVE_LAST[sid] = (text, last_change)
    # working: content changed within last 2.5s (covers inter-token pauses + redraw instants -> smooth, no flicker) or spinner present during pure thinking
    recently_changed = bool(text) and (now - last_change) < 2.5
    return {"working": recently_changed or spinner, "text": text}


@app.get(f"{PREFIX}/image")
def serve_image(path: str) -> FileResponse:
    """Serve pasted images (used for conversation/input thumbnails + full-size preview on click). Restricted to _IMG_DIR to prevent path traversal."""
    p = Path(path).resolve()
    if not str(p).startswith(str(_IMG_DIR.resolve()) + os.sep) or not p.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    return FileResponse(str(p))


@app.delete(f"{PREFIX}/sessions/{{sid}}", status_code=204)
def delete_session(sid: str) -> None:
    manager.delete(sid)


@app.get(f"{PREFIX}/sessions/{{sid}}/events")
def list_events(sid: str, after_seq: int = 0) -> dict:
    return {"items": manager.history(sid, after_seq=after_seq)}


@app.websocket(f"{PREFIX}/sessions/{{sid}}/ws")
async def session_ws(websocket: WebSocket, sid: str) -> None:
    after_seq = int(websocket.query_params.get("after_seq", "0") or 0)
    await websocket.accept()

    async def pump() -> None:
        try:
            async for event in manager.subscribe(sid, after_seq=after_seq):
                await websocket.send_text(event_to_ws_json(event))
        except Exception:
            pass

    sender = asyncio.create_task(pump())
    try:
        while True:
            message = await websocket.receive_json()
            mtype = message.get("type")
            if mtype == "user_message" and message.get("text"):
                await asyncio.to_thread(manager.submit_user_message, sid, message["text"])
            elif mtype == "interrupt":
                await asyncio.to_thread(manager.interrupt, sid)
            elif mtype == "takeover":
                manager.takeover(sid)
            elif mtype == "resume_automation":
                manager.resume_automation(sid)
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()


@app.websocket(f"{PREFIX}/sessions/{{sid}}/terminal")
async def terminal_ws(websocket: WebSocket, sid: str) -> None:
    await websocket.accept()
    bridge = manager.start_terminal(sid)
    if bridge is None:
        await websocket.close(code=4404)
        return

    async def to_client(data: bytes) -> None:
        await websocket.send_bytes(data)

    reader = asyncio.create_task(bridge.read_loop(to_client))
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                bridge.write(data)
                continue
            text = message.get("text")
            if text is None:
                continue
            if text.startswith("{"):
                try:
                    control = json.loads(text)
                except json.JSONDecodeError:
                    control = None
                if isinstance(control, dict) and "resize" in control:
                    resize = control["resize"] or {}
                    bridge.resize(int(resize.get("cols", 120)), int(resize.get("rows", 32)))
                    continue
            bridge.write(text.encode("utf-8"))
    except WebSocketDisconnect:
        pass
    finally:
        reader.cancel()
        bridge.stop()


def create_app(custom_settings: Settings | None = None) -> FastAPI:
    """Configure runtime singletons and return the FastAPI app.

    Standalone: ``uvicorn muxdesk.app:app``.
    Embedded:   ``from muxdesk import create_app; app = create_app(settings=my_settings)``.
    """
    _configure(custom_settings)
    return app


# Default configuration so `uvicorn muxdesk.app:app` works out of the box.
_configure()
