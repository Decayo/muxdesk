# Tasks — session-bind check-ins

## 1. Receiving end (validation + delivery)
- [x] `bind.build_checkin(record, body)` — validate output vs `bind_contract.output_schema`, build the `child_checkin` payload.
- [x] `POST /sessions/{sid}/checkin` — validate + publish to parent bus.

## 2. Routing + extraction
- [x] `SessionRegistry.get_by_claude_session_id` — map claude id → record.
- [x] `bind.extract_transcript_checkin(path)` — `{summary, output}` from the transcript tail.
- [x] `POST /checkin-by-claude` — map → extract → build_checkin → publish; no-op for unbound/unknown.

## 3. Producer (Stop hook)
- [x] `bind.checkin_hook_settings(base)` — static Stop-hook `--settings`.
- [x] `MUXDESK_SELF_URL` + merge into both `create_session` paths (lead + regular).

## 4. Frontend (muxdesk-web)
- [x] Render `child_checkin` as a card in the parent conversation (`CheckinCard`).

## 5. Tests
- [x] contract / check-in validation, cycle guard, transcript extraction, hook settings, registry lookup.

## Follow-ups (not in this change)
- [ ] A `muxdesk-bind` skill so the child writes contract-shaped output deliberately (beyond the transcript-tail default).
- [ ] End-to-end integration test (needs `httpx` + a probe-loop-free app fixture).
- [x] Cadence gate: `checkin.cadence: manual` suppresses the auto Stop-hook delivery (on_stop / every_turn deliver per turn; finer every_turn-vs-on_stop timing is a non-goal for a Stop trigger).
