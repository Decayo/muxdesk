# muxdesk

A **terminal-native cockpit for _interactive_ AI CLI sessions running in tmux** — drive a real, interactive `claude` (full TTY, native agent-team, slash commands) from your browser, with a clean chat UI, structured question cards, a live agent graph, and streaming preview.

> Unlike headless wrappers (`claude --print` / `--output-format stream-json`), muxdesk keeps the **real interactive TTY** in tmux — so Claude Code's native agent-team split-pane, `/model`, and other interactive features keep working. The conversation is reconstructed by tailing Claude Code's jsonl transcripts; live streaming is recovered by scraping the tmux pane.

**muxdesk is an _interface_, not a framework.** It gives you a tmux-backed session platform — create a session, inject a system prompt + settings, read the transcript back as structured events, ask the user structured questions, wire an agent graph — and stays out of your business logic. Any "orchestrator" is just a `system_prompt` you pass in.

This repo is the **backend Python package** (`muxdesk`). The web UI lives in the companion repo [`muxdesk-web`](https://github.com/Decayo/muxdesk-web).

## Why

There are tools that orchestrate Claude in tmux, and tools that tail jsonl transcripts read-only — but (as of mid-2026) nothing combines **interactive TTY + jsonl reconstruction + structured-ask interception + a live agent-team graph** into one cockpit. This fills that gap.

## Features

- **Chat UI from jsonl** — markdown, mermaid diagrams, tables, pasted images; reconstructed from `~/.claude/projects/.../<uuid>.jsonl`.
- **Live streaming preview** — jsonl is written per-complete-message (no token stream), so the in-progress reply is scraped from the tmux pane (`capture-pane`) and swapped for clean markdown when it lands.
- **Structured Ask cards** — Claude Code's interactive `AskUserQuestion` is a TUI menu (unusable from a browser). A `PreToolUse` hook denies it and redirects Claude to a `muxdesk-ask` bash tool that emits **structured JSON**; the web renders clickable cards (single/multi-select, multi-question, custom answer) and writes the answer back.
- **Agent-team node graph** — native agent teams / Task subagents rendered as a live React-Flow DAG (orchestrator → teammates/subagents); click a node to open its transcript.
- **Session lifecycle** — readiness probe (trust/login/rate-limit detection), interrupt (Esc), model switch with auto-confirm, image paste (→ Claude `Read`s the file), font zoom.
- **Interactive terminal** — a real terminal tab (xterm.js ⇄ pty `tmux attach`). **Terminal-agnostic:** it does not depend on the host terminal emulator — tmux is the abstraction and the browser renders via xterm.js.

## Install

```bash
pip install muxdesk            # core building blocks only (pure-stdlib, zero deps)
pip install "muxdesk[server]"  # + the generic cockpit server (FastAPI/uvicorn)
```

The core (parser / session manager / event bus / registry) has **zero Python dependencies** — it only needs a `tmux` and `claude` binary at runtime. FastAPI is pulled in lazily, only when you call `create_app()`.

Requirements: `tmux`, `claude` (Claude Code CLI, logged in), Python 3.11+. The web UI additionally needs Node 20+.

## Quickstart

### 1. One-liner — the generic cockpit server

```bash
pip install "muxdesk[server]"
MUXDESK_WORKSPACE=/path/to/your/project \
    python3 -m uvicorn muxdesk.app:app --host 127.0.0.1 --port 8001
```

Then run the [`muxdesk-web`](https://github.com/Decayo/muxdesk-web) frontend (Vite :5274, proxies `/api` → :8001) and open `http://127.0.0.1:5274`.

### 2. Embed — drive sessions yourself, inject your own prompts

`create_app()` returns a stock FastAPI app you can mount or extend. Or skip the
server entirely and use the building blocks directly — muxdesk never knows what
your prompt is about:

```python
from muxdesk import SessionManager, EventBus, SessionRegistry, Settings

settings = Settings(cc_workspace_path="/path/to/project")
mgr = SessionManager(settings, EventBus(), SessionRegistry(settings.cc_db_path))

rec = mgr.create_session(
    system_prompt="You are a release-notes assistant.",  # your business logic = a prompt
    permission_mode="acceptEdits",
)
sid = rec["app_session_id"]
mgr.submit_user_message(sid, "Summarise the last 10 commits.")

for event in mgr.history(sid):       # semantic events rebuilt from the jsonl transcript
    print(event["type"], event.get("text", ""))
```

## Python API

Everything below is exported from the top-level `muxdesk` package.

| Symbol | Kind | Purpose |
|--------|------|---------|
| `Settings` | dataclass | Runtime config; env via `MUXDESK_*` (legacy `CC_*` fallback). |
| `SessionManager` | class | The interface. Create/drive tmux claude sessions, rebuild transcripts. |
| `SessionRegistry` | class | JSON-file persistence of session records. |
| `EventBus` | class | Pub/sub of semantic events per session. |
| `event_to_ws_json` | func | Serialize an event for a WebSocket frame. |
| `parse_record` | func | One raw jsonl record → iterator of semantic events. |
| `extract_metadata` | func | Pull model/usage/etc. metadata from a jsonl record. |
| `SessionState` | enum | `STARTING / READY / WORKING / …` lifecycle states. |
| `create_app` | func | Build the stock cockpit FastAPI app (needs `[server]`). |

`SessionManager` (constructed as `SessionManager(settings, event_bus, registry)`):

```python
create_session(*, workspace_path=None, model=None, title=None,
               extra_settings=None, system_prompt=None,
               add_dirs=None, permission_mode=None) -> dict   # returns a record; id = rec["app_session_id"]
submit_user_message(sid, text) -> bool
history(sid, after_seq=0) -> list[dict]                       # rebuilt events
subscribe(sid, after_seq=0) -> AsyncGenerator[dict, None]     # live event stream
list_sessions(*, status=None) -> list[dict]
get(sid) -> dict | None
interrupt(sid) / takeover(sid) / resume_automation(sid) -> None
archive(sid) / delete(sid) -> bool
resume_session(sid) -> dict | None
probe_recover(sid) -> dict                                    # rate-limit/dead recovery
start_probe_loop(interval=10.0) -> None                       # background liveness loop
rebind_active_sessions() -> int                               # re-attach tailers after restart
start_terminal(sid, cols=120, rows=32) -> PtyBridge | None
capture_screen(sid, lines=40) -> str                          # TUI menu scraping
send_keys(sid, *keys) -> bool / type_text(sid, text) -> bool  # TUI interaction
```

## Architecture

```
┌──────────────────────────┐   /api    ┌───────────────────────────────┐
│  muxdesk-web (React/Vite) │  ─proxy─► │  muxdesk (FastAPI, this repo)  │
│  Mx* components            │  :8001    │  create_app()                  │
│  chat · graph · ask cards  │           │  SessionManager / EventBus     │
└──────────────────────────┘           └──────────────┬────────────────┘
                                                       │ tmux send-keys / capture-pane
                                                       ▼
                                        claude (interactive, in tmux)
                                        ~/.claude/projects/<cwd>/<uuid>.jsonl  ← conversation (tailed)
                                        ~/.claude/teams · subagents/*.meta.json ← agent graph
                                        /tmp/muxdesk-ask   ← structured-ask req/ans exchange
                                        /tmp/muxdesk-img   ← pasted images
```

- **Package layout** (`muxdesk/`): `app.py` (FastAPI, `create_app`), `session_manager.py`, `state_machine.py`, `transcript_parser.py`, `tmux_driver.py`, `event_bus.py`, `session_registry.py`, `pty_bridge.py`, `team/` (agent graph), `scripts/{muxdesk-ask,muxdesk-ask-hook}` (structured-ask channel).
- **Contract with the frontend**: REST/WS under `/api/muxdesk`. The two `/tmp/muxdesk-*` paths are the only out-of-band coupling (backend writes, frontend detects).

## Configuration

Env vars (backend), `MUXDESK_*` prefix with legacy `CC_*` fallback:

| Env | Default | Meaning |
|-----|---------|---------|
| `MUXDESK_WORKSPACE` | `~` | Default workspace path for new sessions. |
| `MUXDESK_CLAUDE_COMMAND` | `claude` | The CLI to drive. |
| `MUXDESK_DEFAULT_MODEL` | — | Default model for new sessions. |
| `MUXDESK_PERMISSION_MODE` | `acceptEdits` | Claude permission mode. |
| `MUXDESK_CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Where transcripts live. |
| `MUXDESK_TMUX_PREFIX` | `muxdesk` | tmux session name prefix. |
| `MUXDESK_DB_PATH` | `/tmp/muxdesk-sessions.json` | Session registry file. |

## Development (two-repo layout)

```
workspace/muxdesk-dev/        ← dev container (not a repo)
├── muxdesk/                  ← this repo: pip package (backend)
└── muxdesk-web/              ← companion repo: React frontend
```

```bash
# backend (editable, with server extra)
cd muxdesk && pip install -e ".[server]"
MUXDESK_WORKSPACE=~/some/project python3 -m uvicorn muxdesk.app:app --port 8001

# frontend (proxies /api → :8001)
cd ../muxdesk-web && npm install && npm run dev
```

The frontend depends on the backend's `/api/muxdesk` contract; keep API changes in
lock-step across the two repos. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Status & caveats

Extracted from a private monorepo; works, but it leans on **Claude Code's undocumented internals** (jsonl schema, `~/.claude` layout, the `AskUserQuestion` tool name, the tmux TUI shape). These can break on Claude Code upgrades. See [`docs/01-pitfalls.md`](docs/01-pitfalls.md) for the hard-won lessons.

## License

MIT © Decayo
