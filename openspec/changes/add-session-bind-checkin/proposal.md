## Why

Module 4's session-bind lets a child session report progress to its parent (`POST
/sessions/{sid}/checkin` → validate against the bind contract → push `child_checkin`
to the parent's event bus). But nothing *produces* those check-ins yet: a bound
child has no automatic trigger to report at the end of each turn.

The plan said "inject a Stop hook into the child at bind time". That isn't feasible
for a **running** session — Claude Code reads `--settings` (hooks) only at launch, so
a hook can't be added to a live session, and re-launching on bind would disrupt it.

## What Changes

Inject the check-in hook at **session creation** (static, every muxdesk session) rather
than at bind time — mirroring the existing `_ask_hook_settings` / `_team_hook_settings`
pattern. The hook is a no-op until the session is actually bound.

- A static **Stop hook** added to every session's `--settings`: an inline `curl` that
  POSTs the hook payload (which carries `session_id` = the *claude* session id) to a new
  endpoint `POST /api/muxdesk/checkin-by-claude`.
- `checkin-by-claude` maps `claude_session_id → app_session_id` (new
  `SessionRegistry.get_by_claude_session_id`), reads the transcript tail to build the
  check-in body (last assistant text = `summary`, last fenced ```json block = `output`),
  then reuses the existing `build_checkin` → validate → `bus.publish(parent, …)`.
- A session with **no parent** short-circuits (the existing `/checkin` already returns
  `delivered_to_parent: false`), so the always-on hook costs one cheap curl per turn and
  only delivers once the session is bound.

Why `checkin-by-claude` instead of baking `app_session_id` into the hook: the Stop-hook
settings are built before `app_session_id` exists (it's generated inside
`create_session`), and a static hook (same for every session) keeps creation simple. The
claude session id is always present in the hook stdin, so the backend can map it.

## Capabilities

### New Capabilities
- `session-bind-checkin`: automatic per-turn child→parent check-ins for bound sessions,
  produced by a creation-time Stop hook and routed via claude-session-id mapping.

## Impact

- **Code**: `session_registry` gains `get_by_claude_session_id`; `bind.py` gains a
  transcript-tail extractor; `app.py` adds `/checkin-by-claude` and a
  `_checkin_hook_settings()` merged into both `create_session` paths.
- **Behavior**: every new session carries one extra Stop hook (cheap curl); unbound
  sessions are unaffected (no parent → no delivery). No change to existing sessions until
  recreated.
- **Dependencies**: none.
- **Frontend**: already renders `child_checkin` (the `CheckinCard`) — no change needed.

## Status / sequencing

Landing incrementally on `feat/session-bind`:
1. ✅ `/checkin` endpoint + `build_checkin` (done).
2. ✅ `get_by_claude_session_id` + transcript-tail extractor + `/checkin-by-claude` (this change).
3. ⏳ `_checkin_hook_settings()` injection into `create_session` (touches the creation
   path for all sessions — landed as its own commit after review of this design).
