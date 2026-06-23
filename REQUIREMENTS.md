# muxdesk — Requirements

A checkable spec for the project. Each functional requirement (FR) has acceptance
criteria; running the backend + frontend (see README) is meant to exercise every one of them.
This doubles as a release gate: before going public, walk the demo and tick each box.

> Legend: **R** = required for a usable product · **O** = optional / nice-to-have.
> Status: `[x]` implemented · `[~]` partial · `[ ]` not yet.

---

## 1. Purpose & scope

muxdesk is a **web cockpit for _interactive_ Claude Code sessions running in tmux**.
It drives a real, interactive `claude` CLI (full TTY) from the browser — keeping
native agent-team, slash commands and `/model` working — and reconstructs the
conversation by tailing Claude Code's jsonl transcripts plus scraping the tmux pane.

**In scope:** session lifecycle, chat reconstruction, live preview, structured ask,
agent-team graph, interactive terminal, model switching, image paste, configurable
quick-action bar, dependency preflight.

**Non-goals:** hosting/multi-tenant auth, billing, a hosted SaaS, replacing the
Claude Code CLI, or working against a headless (`--print`) backend.

---

## 2. Runtime dependencies

The product distinguishes **running the demo** from **installing as a package**:

| | Demo (clone + run) | Package install |
|---|---|---|
| How | clone both repos → run backend + frontend | `pip install "muxdesk[server]"` + serve built frontend |
| Workspace | a sandbox dir via `MUXDESK_WORKSPACE` (your real `~/.claude/projects` untouched) | your chosen `MUXDESK_WORKSPACE` |
| Audience | try it in a few minutes | run it for real |

**Both** require, on the host: `tmux`, `claude` (Claude Code CLI, logged in),
Python 3.11+, and (for the dev frontend) Node 20+.

- **FR-DEP (R) Dependency preflight.** When a dependency is missing, the user is
  **warned explicitly** rather than hitting an opaque failure.
  - [x] Backend `GET /api/muxdesk/preflight` reports tmux / claude / login / python with `ok`, `detail`, `hint`.
  - [x] Frontend shows a dismissible banner listing missing **required** deps + install hints.
  - [x] Frontend shows a distinct banner when the backend itself is unreachable.
  - [x] Starting the backend logs a preflight summary so missing host deps surface before use.

---

## 3. Functional requirements

### FR1 (R) Session lifecycle & persistence
- [x] Create an interactive `claude` session in tmux (`POST /sessions`); list / get / archive / resume / delete.
- [x] Sessions persist across backend restarts (sqlite `SessionRegistry`); on restart, alive sessions are re-bound and the chat repopulates.
- [x] `archived` sessions keep `claude_session_id` so they can be `--resume`d.
- **Accept:** restart the backend mid-session → the session still appears, chat history is intact, and the terminal reconnects.

### FR2 (R) Chat reconstruction from jsonl
- [x] Tail `~/.claude/projects/<cwd-slug>/<uuid>.jsonl`; parse user / assistant / thinking / tool events.
- [x] Render markdown, mermaid diagrams, tables, and pasted-image thumbnails.
- [x] Unknown / half-written jsonl records are skipped, never crash the parser.
- **Accept:** a reply containing a mermaid block and a table renders correctly; a corrupt line does not blank the chat.

### FR3 (R) Live streaming preview
- [x] Because jsonl is written per-complete-message, the in-progress reply is scraped from the tmux pane (`/live`, capture-pane) and swapped for clean markdown when it lands.
- [x] Show/hide is driven by turn lifecycle (not flaky content/working detection) so it does not flicker.
- **Accept:** sending a prompt shows incremental text within ~1s and does not blink; thinking phases do not hide the preview.

### FR4 (R) Structured Ask (muxdesk-ask)
- [x] A `PreToolUse` hook denies `AskUserQuestion` and redirects Claude to the `muxdesk-ask` bash tool emitting structured JSON.
- [x] Web renders clickable cards: single-select, multi-select, multi-question, custom answer.
- [x] Answer is written back to unblock `muxdesk-ask`; cancel returns a “user cancelled” result.
- **Accept:** ask a single- and a multi-select question from the agent → cards appear, answers flow back, no `AskUserQuestion`/`Skill(muxdesk-ask)` noise leaks into the chat.

### FR5 (R) Agent-team node graph
- [x] Render native agent teams **and** one-shot Task subagents as a live React-Flow DAG (orchestrator → teammates / subagents).
- [x] Click a node to open that teammate/subagent transcript.
- [x] Node shows description, tool-use count, peak tokens, and status; only the latest spawn wave is kept.
- **Accept:** an orchestrator that spawns teammates shows them as nodes; clicking one opens its transcript.

### FR6 (R) Interactive terminal — terminal-agnostic
- [x] Browser xterm.js ⇄ pty bridge (`tmux attach`) ⇄ tmux; bidirectional I/O + resize (SIGWINCH).
- [x] **No dependency on the host terminal emulator** (kitty/alacritty/iTerm/…): tmux is the abstraction, the browser renders via xterm.js. Closing the tab detaches without killing tmux.
- [x] Font is configurable via `VITE_MUXDESK_TERMINAL_FONT`; a Nerd Font is recommended for Claude TUI box-drawing/icons, with a documented fallback chain.
- **Accept:** the terminal tab attaches and renders the live TUI regardless of which terminal the host was launched from; resizing the browser resizes the TUI.

### FR7 (R) Model switch with auto-confirm
- [x] Switch a running session's model (`/model X`); auto-confirm the “Switch model?” dialog.
- **Accept:** switching model on a cached session succeeds in one click without hanging.

### FR8 (O) Native TUI menu bridging
- [x] Detect capture-pane menus (e.g. `/model`) and render clickable options; best-effort (may miss on TUI redraw).
- **Accept:** `/model` shows clickable options that select correctly.

### FR9 (O) Image paste
- [x] Paste an image → base64 upload → stored file → absolute path injected into the message for Claude to `Read`; thumbnail + lightbox in the UI.
- [x] Image serving is path-traversal guarded (confined to the image dir).
- **Accept:** pasting an image lets Claude read it; `/image?path=` cannot escape the image dir.

### FR10 (O) Configurable quick-action bar (harness)
- [x] Data-driven from `harness.json` / `MUXDESK_HARNESS_CONFIG`; each button injects its `cmd`. No config → the bar is hidden.
- [x] The shipped `harness.example.json` is **project-neutral** (no IBKR/trade specifics).
- **Accept:** with no config the bar is hidden; copying the example shows generic buttons that inject prompts.

### FR11 (R) Readiness probe & state machine
- [x] Probe detects trust/login/rate-limit/ERROR and startup readiness; interactive busy state is driven by transcript, not overwritten by the probe.
- [x] `BLOCKED_INTERACTIVE` surfaces a login URL to copy; interrupt (Esc) converges state to READY.
- **Accept:** a session needing login shows the URL; pressing stop clears the busy/stop UI.

### FR12 (R) Configurability
- [x] Env-configurable: `MUXDESK_WORKSPACE`, `MUXDESK_CLAUDE_COMMAND`, `MUXDESK_DEFAULT_MODEL`, `MUXDESK_PERMISSION_MODE`, `MUXDESK_CLAUDE_PROJECTS_DIR`, `MUXDESK_TMUX_PREFIX`, `MUXDESK_DB_PATH`, `MUXDESK_HARNESS_CONFIG`.
- [x] No new external services (no DB server; sqlite + files + in-memory).
- **Accept:** pointing `MUXDESK_WORKSPACE` at a sandbox runs the demo without touching real projects.

---

## 4. Non-functional requirements

- **NFR1 Security.** Image path-traversal guard (FR9). The standalone server has **no auth** — it is meant to sit behind a trusted boundary (e.g. tailnet); this is documented, and `funnel`/public exposure is warned against.
- **NFR2 Resilience.** jsonl / capture-pane parsing tolerates unknown formats and transient blank captures without crashing or flickering (see `docs/01-pitfalls.md`).
- **NFR3 Portability.** Terminal-agnostic (FR6); paths/ports/commands are env-configurable (FR12).
- **NFR4 Known fragility (documented, not a bug).** Relies on Claude Code's **undocumented internals**: jsonl schema, `~/.claude` layout, the `AskUserQuestion` tool name, the tmux TUI shape. These can break on Claude Code upgrades; mitigations are centralized (`transcript_parser.py`, the TUI sentinel regexes) and documented in `docs/01-pitfalls.md`.

---

## 5. Traceability

| Area | Backend | Frontend |
|---|---|---|
| Sessions / persistence | `muxdesk/session_registry.py`, `session_manager.py` | `stores/sessionStore.ts`, `api/muxDesk.ts` |
| Chat / live preview | `transcript_parser.py`, `app.py` `/live` | `components/muxDesk/MxEventStream.tsx`, `hooks/useMxDeskStream.ts` |
| Structured ask | `scripts/muxdesk-ask`, `scripts/muxdesk-ask-hook`, `app.py` `/ask` | `components/muxDesk/AskUserQuestionCard.tsx` |
| Agent graph | `muxdesk/team/*`, `app.py` `/native-teams`, `/team` | `components/muxDesk/MxTeamPanel.tsx` |
| Terminal | `muxdesk/pty_bridge.py`, `app.py` `/terminal` | `hooks/useMxTerminal.ts`, `components/muxDesk/MxTerminal.tsx` |
| Preflight | `muxdesk/preflight.py`, `app.py` `/preflight` | `components/muxDesk/MxPreflightBanner.tsx` |

See `docs/archive/` for historical extraction/packaging notes from when this repo was split out of its originating monorepo.
