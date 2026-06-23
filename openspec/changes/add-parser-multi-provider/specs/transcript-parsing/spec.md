## ADDED Requirements

### Requirement: Provider-based transcript parsing
The system SHALL parse transcripts through a `Parser` interface exposing `parse_record` and `extract_metadata`, so transcript formats are pluggable per provider. The Claude Code jsonl format SHALL be implemented by a `ClaudeParser` conforming to this interface.

#### Scenario: Claude jsonl record parsed via provider
- **WHEN** a Claude Code jsonl record is passed to `ClaudeParser.parse_record`
- **THEN** it yields the same `{event_type, payload}` events as the legacy parser (e.g. `assistant_message`, `tool_start`)

#### Scenario: Unknown record type tolerated
- **WHEN** a record with an unrecognized `type` is parsed
- **THEN** the parser yields a `raw_event` and never raises

### Requirement: Backward-compatible public functions
The module-level `parse_record` and `extract_metadata` SHALL remain importable from both `muxdesk` and `muxdesk.transcript_parser`, delegating to a default `ClaudeParser`, with output identical to the pre-change behavior.

#### Scenario: Existing import keeps working
- **WHEN** a caller runs `from muxdesk import parse_record` and parses a Claude record
- **THEN** the returned events are identical to the previous implementation's output

### Requirement: Antifragile fallback chain
The system SHALL provide a `ParserChain` that selects the highest-priority available transcript source per session and degrades to the next on unavailability or parse failure, never dropping the conversation and never raising to the caller.

#### Scenario: jsonl available
- **WHEN** the session transcript jsonl exists and yields at least one parseable record
- **THEN** `ParserChain` uses `ClaudeParser` and emits no degradation marker

#### Scenario: jsonl missing or unparseable
- **WHEN** the transcript jsonl is absent, empty, or fails to parse
- **THEN** `ParserChain` falls back to `PaneParser` and emits a one-time `parser_degraded` notice

### Requirement: Best-effort pane-scrape fallback
The `PaneParser` SHALL reconstruct coarse conversation events from tmux `capture-pane` text and SHALL mark every emitted event as degraded.

#### Scenario: Conversation rebuilt from screen
- **WHEN** `PaneParser` parses captured pane text containing a user prompt and an assistant reply
- **THEN** it yields `user_message` / `assistant_message` events each carrying `degraded: true` in the payload
