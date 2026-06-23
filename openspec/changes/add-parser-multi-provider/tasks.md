## 1. Parser interface + Claude provider

- [x] 1.1 Create `muxdesk/parsers/__init__.py` and `muxdesk/parsers/base.py` defining the `Parser` interface (`parse_record(record) -> Iterator[dict]`, `extract_metadata(record) -> dict`)
- [x] 1.2 Move the current `transcript_parser` logic into `muxdesk/parsers/claude.py` as `ClaudeParser` (verbatim behavior: `_parse_block`, ignored types, sidechain flag, model/usage on assistant text)
- [x] 1.3 Repoint `muxdesk/transcript_parser.py` to a compatibility shim — module-level `parse_record` / `extract_metadata` delegate to a default `ClaudeParser`

## 2. Fallback chain + pane provider

- [ ] 2.1 Implement `muxdesk/parsers/pane.py` `PaneParser`: rebuild coarse user/assistant events from `capture-pane` text (reuse live-preview sentinels), tag every event `degraded: true`
- [ ] 2.2 Implement `muxdesk/parsers/chain.py` `ParserChain`: ordered providers + per-session availability check, degrade jsonl→pane, emit a one-time `parser_degraded` `system_notice`, never raise
- [ ] 2.3 (Optional, non-breaking) let `session_manager` build a `ParserChain` for transcript reads so live sessions get the fallback

## 3. Public API + exports

- [ ] 3.1 Keep top-level `muxdesk` exports unchanged (`parse_record`, `extract_metadata`); optionally export `Parser` / `ClaudeParser` / `ParserChain` for advanced use
- [ ] 3.2 Update `__init__.py` docstring / README API table if new symbols are exported

## 4. Tests + verification

- [ ] 4.1 Repoint `tests/test_transcript_parser.py` at `ClaudeParser` (must stay green — regression guard) and assert the shim output matches the legacy output
- [ ] 4.2 Add `tests/test_parser_chain.py`: jsonl-available uses `ClaudeParser` (no marker); jsonl-missing/unparseable falls back to `PaneParser` with a `parser_degraded` notice
- [ ] 4.3 Add `tests/test_pane_parser.py`: pane text → degraded `user_message` / `assistant_message` events
- [ ] 4.4 `py_compile` + `pytest` green; confirm `from muxdesk import parse_record` is unchanged
