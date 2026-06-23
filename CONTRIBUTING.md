# Contributing

Thanks for your interest! muxdesk is a thin-ish UI over **undocumented Claude Code internals** — read [`docs/01-pitfalls.md`](docs/01-pitfalls.md) before touching the streaming / state / ask code.

muxdesk spans **two repos** that move in lock-step over the `/api/muxdesk` contract:

- [`muxdesk`](https://github.com/Decayo/muxdesk) (this repo) — backend Python package.
- [`muxdesk-web`](https://github.com/Decayo/muxdesk-web) — React/Vite frontend.

## Dev setup

```bash
# backend (this repo) — editable install with the server extra
cd muxdesk && pip install -e ".[server]"
MUXDESK_WORKSPACE=~/some/project python3 -m uvicorn muxdesk.app:app --reload --port 8001

# frontend (companion repo) — proxies /api → :8001
cd ../muxdesk-web && npm install && npm run dev   # → http://127.0.0.1:5274
```

Requirements: `tmux`, `claude` (logged in), Python 3.11+, Node 20+.

## Layout

**Backend** (`muxdesk` package, this repo):

- `muxdesk/app.py` — FastAPI app (`create_app`): REST + WebSocket, session / ask / live / image / harness endpoints.
- `muxdesk/` — `session_manager` (tmux + jsonl tailer + state), `state_machine`, `transcript_parser` (jsonl → events), `tmux_driver`, `event_bus`, `session_registry`, `team/` (agent graph), `settings`.
- `muxdesk/scripts/{muxdesk-ask,muxdesk-ask-hook}` — structured-ask channel (Bash tool + PreToolUse hook).

**Frontend** (`muxdesk-web`):

- `src/components/muxDesk/*` — chat (`MxEventStream`), agent graph (`MxTeamPanel`), ask card, input, terminal, etc.
- `src/{stores,hooks,api}` — zustand stores, stream hook (`useMxDeskStream`), REST client (`api/muxDesk.ts`).

## Checks before a PR

- Backend: `python3 -m py_compile muxdesk/*.py muxdesk/team/*.py` must succeed; `ruff check muxdesk/` if you have ruff.
- Frontend: `cd muxdesk-web && npm run build` (runs `tsc` + vite build).
- **API contract**: any change to `/api/muxdesk` routes or the `/tmp/muxdesk-*` exchange paths must land in **both** repos together — the frontend detects those paths/strings by convention.

## Things that are fragile (be careful)

These rely on Claude Code internals that can change between versions — keep the parsing centralized and add fallbacks rather than spreading assumptions:

- **jsonl schema** (`transcript_parser.py`) — event field names.
- **`~/.claude` layout** — `projects/<cwd-slug>/<uuid>.jsonl`, `teams/`, `subagents/*.meta.json`.
- **`AskUserQuestion` tool name** — the PreToolUse hook matches it by name.
- **tmux TUI shape** — `capture-pane` scraping for live preview / readiness probe / menus. Pin pane size, match on stable sentinels, return empty on parse failure.

## Style

- Match the surrounding code. Comments in the existing file's language. Small, focused changes.
- No new heavy dependencies without discussion — the core package is intentionally zero-dependency (FastAPI lives behind the `server` extra).

## License

By contributing you agree your contributions are MIT-licensed.
