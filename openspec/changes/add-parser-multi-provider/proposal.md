## Why

muxdesk reconstructs conversations by parsing transcript jsonl with no SDK — its core differentiator. But `transcript_parser.parse_record` is hard-wired to one format (Claude Code's jsonl) and has no fallback: if the jsonl schema shifts on a Claude Code upgrade, or a transcript is unavailable/corrupt, conversation rebuild silently breaks. The reconstruction layer needs a provider abstraction and an antifragile degradation path.

## What Changes

- Introduce a `muxdesk/parsers/` subpackage:
  - `base.py` — a `Parser` interface (`parse_record` / `extract_metadata`) emitting the existing `{event_type, payload}` event dicts unchanged.
  - `claude.py` — the current `transcript_parser` logic moved into `ClaudeParser` (Claude Code jsonl).
  - `pane.py` — `PaneParser`, a best-effort fallback that rebuilds conversation events from tmux `capture-pane` text when jsonl is unavailable or fails to parse.
  - `chain.py` — `ParserChain`, which tries providers in priority order and degrades (jsonl → pane scrape) without ever interrupting or dropping events.
- `transcript_parser.parse_record` / `extract_metadata` become thin delegations to the default `ClaudeParser`, preserving the public API.
- No non-Claude provider is added — this lands the framework with Claude as the single concrete provider.

## Capabilities

### New Capabilities
- `transcript-parsing`: a pluggable transcript→events parsing layer with a provider abstraction and an antifragile fallback chain (jsonl parser → pane-scrape), replacing the single hard-wired Claude jsonl parser.

### Modified Capabilities
<!-- None. openspec/specs/ is currently empty; transcript-parsing is the first capability. -->

## Impact

- **Code**: new `muxdesk/parsers/` subpackage; `muxdesk/transcript_parser.py` becomes a compatibility shim delegating to `ClaudeParser`; `session_manager` may opt into `ParserChain` for fallback (optional, non-breaking).
- **Public API**: `parse_record` / `extract_metadata` stay importable and behavior-identical — ibkr standalone and `session_manager` depend on them, so **no breaking change**.
- **Dependencies**: none added (pure stdlib, consistent with the zero-dependency core).
- **Frontend / ibkr / vault**: untouched.
