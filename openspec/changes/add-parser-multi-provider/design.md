## Context

muxdesk's conversation reconstruction lives entirely in `muxdesk/transcript_parser.py`: `parse_record(record)` turns one Claude Code jsonl line into 0..N `{event_type, payload}` events, and `extract_metadata(record)` pulls session-binding fields. `session_manager` tails the jsonl and feeds each line through `parse_record`; the resulting events drive `state_machine`, `event_bus`, and the frontend.

Two fragilities:
1. **Format lock-in** — the parser assumes Claude Code's exact jsonl shape (record `type`, `message.content` blocks). Another AI CLI, or a Claude Code schema change, has no seam to plug into.
2. **No fallback** — if the jsonl is missing, truncated, or a record fails to parse, that content is lost; there is no secondary source even though the live tmux pane still shows the conversation.

`parse_record` / `extract_metadata` are imported by `session_manager` and by ibkr's standalone server, so their signatures and output are a hard compatibility boundary.

## Goals / Non-Goals

**Goals:**
- A `Parser` interface so transcript→events parsing is pluggable per provider.
- An antifragile `ParserChain` that degrades from the primary jsonl parser to a `capture-pane` scrape rather than dropping a conversation.
- Zero behavior change for existing callers — `parse_record` / `extract_metadata` keep working identically.
- Stay pure-stdlib (no new dependency).

**Non-Goals:**
- Shipping a second concrete provider (non-Claude). This change lands the framework with `ClaudeParser` as the only provider.
- Changing the downstream event contract (`{event_type, payload}`) consumed by `state_machine` / `event_bus` / frontend.
- Frontend, ibkr, or vault changes.

## Decisions

### D1. Parser interface mirrors today's functions
`Parser` exposes `parse_record(record: dict) -> Iterator[dict]` and `extract_metadata(record: dict) -> dict`, emitting the **exact same** event dicts as today. The event schema is already the stable contract with downstream consumers, so matching the existing free functions makes `ClaudeParser` a near-mechanical move and the public shim trivial. Alternative considered — a richer streaming interface (`parse_transcript(path)`) — deferred; record-at-a-time is what `session_manager`'s tailer already drives.

### D2. ParserChain degrades per source, not per record
`ParserChain` holds an ordered list of providers each with an availability check. For a session it uses the highest-priority **available** source: jsonl (`ClaudeParser`) when the transcript exists and parses, else the pane scrape (`PaneParser`). Mixing jsonl and scraped events per-record would produce duplicates and ordering chaos; choosing one source per read keeps the stream coherent. The chain emits a one-time `parser_degraded` marker (under the existing `system_notice` envelope) when it falls back — the fallback is visible, never silent.

### D3. `transcript_parser.py` becomes a compatibility shim
The module keeps `parse_record` / `extract_metadata` as module-level functions delegating to a default `ClaudeParser` instance. This preserves every existing import (`from muxdesk import parse_record`, `from muxdesk.transcript_parser import parse_record`) with no caller edits; top-level `muxdesk` exports are unchanged.

### D4. PaneParser is explicitly best-effort
`PaneParser` reconstructs coarse events (user/assistant message text) from `capture-pane` output, reusing the sentinel patterns the live-preview scraper already relies on. It does not attempt tool_use/thinking fidelity. Pane scrape can only ever be approximate; the goal is "don't lose the conversation," not "perfect parity." Every PaneParser event carries `degraded: true` in its payload.

## Risks / Trade-offs

- **Pane scrape is lossy** → mark every degraded event (`degraded: true` + a one-time `parser_degraded` notice) so consumers can surface "reconstructed from screen, may be incomplete."
- **Refactor regresses Claude parsing** → `ClaudeParser` is a verbatim move of today's logic; the existing `test_transcript_parser.py` suite is repointed at it and must stay green as the regression guard.
- **Chain picks the wrong source** → availability check is conservative: jsonl must both exist and yield ≥1 parseable record before it is chosen; otherwise fall back.
- **Scope creep into multi-provider** → bounded by Non-Goals: no second provider in this change; the interface is the deliverable.

## Open Questions (surfaced during task-1 implementation)

Task 1 (D1/D3 — provider abstraction + `ClaudeParser` + compatibility shim) landed cleanly with zero regression. Tasks 2.x (pane + chain) exposed two interface questions that must be settled before implementing them:

- **PaneParser is session-level, not record-level.** `ClaudeParser.parse_record(record)` consumes one jsonl record; `PaneParser` consumes a whole `capture-pane` screen — there is no per-"record" unit, so the record-level `Parser` interface (D1) fits jsonl but not pane scrape. Options: (a) add a session-level `parse_source(...) -> Iterator[event]` to the interface, with `ClaudeParser` implementing it as tail-jsonl + `parse_record`; (b) keep `PaneParser` outside the `Parser` ABC and let `ParserChain` orchestrate the two shapes.
- **ParserChain's real seat is `session_manager`.** The valuable degradation (jsonl file missing/corrupt → pane scrape) is a per-session source choice needing the jsonl path *and* the tmux pane — both owned by `session_manager`, not the pure parser layer. `ClaudeParser.parse_record` is already version-tolerant (never raises), so a record-level chain adds little. Decide whether `ParserChain` lives in `parsers/` operating on injected sources, or folds into `session_manager`'s read path.

### Resolution (task 2)

- **PaneParser stays out of the `Parser` ABC.** It exposes `parse_pane(text) -> Iterator[event]` (session/screen-level), not `parse_record`. The record-level `Parser` ABC remains jsonl-shaped and clean.
- **ParserChain is source-agnostic.** `reconstruct(jsonl_records=?, pane_text=?)` takes injected sources and picks one per call (jsonl if it yields events, else pane + a one-time `parser_degraded` notice). It has **no dependency on `session_manager`** — it can be unit-tested in isolation and lands as a pure-additive module.
- **session_manager wiring (task 2.3) stays deferred — evaluated, not hand-waved.** Read-path analysis: `parse_record` is called per-record by `JsonlTailer`'s `on_line` callback (`session_manager.py:415`), and the tailer already tolerates a missing transcript (`FileNotFoundError` → wait, `JSONDecodeError` → skip line, callback error → skip). So the *only* place `ParserChain`'s jsonl→pane degradation adds value is a **binding-timeout fallback**: if `<claude_session_id>.jsonl` never appears (`_start_binding`, line 307), scrape `capture_screen` through `ParserChain.reconstruct(pane_text=...)` to surface a degraded conversation. That is a pure-additive branch, but it (a) edits core binding logic on the running standalone, (b) needs a live tmux to integration-test, and (c) only fires when jsonl is *permanently* absent — an edge the tailer already survives without crashing. Runtime risk outweighs the edge-case value, so it waits for a concrete "jsonl went missing and we lost the conversation" need. The framework (pane + chain) is fully usable and unit-tested without it.
