# Design — session-bind check-ins

## Data flow

```
session create ──► claude launched with --settings { hooks.Stop: curl → /checkin-by-claude }
                                                   (static; same for every session)

child turn ends ──► Stop hook fires ──► curl POST /api/muxdesk/checkin-by-claude
                                          body = claude Stop payload { session_id, transcript_path, … }
                                                   │
   registry.get_by_claude_session_id(session_id) ─┤  (claude id → muxdesk record)
                                                   │   no match → {matched:false}  (cleanly ignored)
   extract_transcript_checkin(transcript_path) ───┤  → { summary, output }
   build_checkin(record, body) ───────────────────┤  validate output vs bind_contract.output_schema
                                                   ▼
                              parent? ── no ──► {delivered_to_parent:false}  (unbound: no-op)
                                 │ yes
                                 ▼
                    bus.publish(parent, "child_checkin", payload) ──► parent WS ──► CheckinCard (web)
```

## Key decisions

1. **Inject at creation, not at bind.** Claude Code reads `--settings` (hooks) only at
   launch, so a running child can't gain a hook at bind time, and relaunching on bind
   would disrupt it. The Stop hook is therefore added to *every* session at creation and
   is a no-op until the session has a parent. Cost: one cheap `curl` per turn.

2. **Route by claude session id, not app id.** The Stop-hook `--settings` is built before
   `create_session` generates the `app_session_id`, so the hook can't embed it. Instead the
   hook forwards its stdin (which always carries the *claude* `session_id`) and the backend
   maps it via `SessionRegistry.get_by_claude_session_id`. This keeps the hook **static**
   (identical for all sessions) — simpler and cacheable.

3. **Build the check-in server-side from the transcript tail.** The hook sends no business
   payload; the backend derives `summary` (last assistant text) and `output` (last fenced
   ```json block) from the transcript. This keeps the hook trivial and means the contract's
   `output_schema` is validated against what the child actually wrote.

4. **No-op safety.** Unbound or unknown sessions short-circuit before any bus publish, so
   the always-on hook is safe to ship to every session.

## Alternatives considered

- *Per-session env (`MUXDESK_SESSION_ID`) baked into the hook* — rejected: needs the app id
  at settings-build time (not yet generated) and makes the hook non-static.
- *Inject at bind via session restart* — rejected: disruptive; loses live state.
- *Child computes & posts the structured output itself* — deferred: relies on the child
  following a convention; the transcript-tail extractor is a zero-cooperation default that a
  future `muxdesk-bind` skill can refine.

## Security / cost

- The hook is a localhost `curl` with `--max-time 2` and `|| true` (never blocks the child).
- `MUXDESK_SELF_URL` (default `http://127.0.0.1:8001`) lets a non-default deploy point the
  callback at the right port.
