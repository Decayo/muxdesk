"""Provider extension hooks in SessionManager.

Verifies the three injection points added for non-Claude runtimes (Codex):
``command_builder``, ``transcript_resolver``, and ``parser_for``. The tmux
driver is stubbed so no real tmux is launched.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import muxdesk.session_manager as sm
from muxdesk import EventBus, SessionManager, SessionRegistry, Settings
from muxdesk.parsers.codex import CodexParser


def _make_manager(**kwargs) -> SessionManager:
    settings = Settings(cc_db_path="/tmp/test-codex-sm.json")
    registry = SessionRegistry("/tmp/test-codex-sm.json")
    manager = SessionManager(settings, EventBus(), registry, **kwargs)
    # Stub tmux so create_session does not launch a real session.
    manager._driver = MagicMock()
    manager._driver.new_session = MagicMock()
    manager._driver.first_pane = MagicMock(return_value=None)
    manager._driver.has_session = MagicMock(return_value=True)
    manager._driver.list_panes = MagicMock(return_value=[])
    return manager


def test_default_command_builder_is_claude_flag_set():
    manager = _make_manager()
    cmd = manager._command_builder(
        runtime_command="claude",
        model="claude-opus-4-8",
        title="t",
        resume_id=None,
        session_id="abc",
        permission_mode="acceptEdits",
        parser="claude-code",
    )
    # Claude Code flags: --session-id, --model, --permission-mode, -n title
    assert "--session-id" in cmd
    assert "--model" in cmd
    assert "claude-opus-4-8" in cmd
    assert "--permission-mode" in cmd


def test_injected_command_builder_is_used_and_receives_parser():
    captured: dict = {}

    def codex_builder(**kw):
        captured.update(kw)
        # Codex has no --session-id; prompt is positional.
        return "codex --skip-git-repo-check -s workspace-write 'go'"

    manager = _make_manager(command_builder=codex_builder)
    record = manager.create_session(
        runtime_command="codex",
        provider="codex",
        parser="codex",
        model="gpt-5.4-codex",
        title="codex session",
    )
    manager._driver.new_session.assert_called_once()
    launched = manager._driver.new_session.call_args[0][2]
    assert launched.startswith("codex ")
    assert "--session-id" not in launched  # codex rejects --session-id
    # The parser id is forwarded so a codex-aware builder can branch on it.
    assert captured["parser"] == "codex"
    assert record["parser"] == "codex"
    # cleanup so the probe loop / binding worker exit promptly
    manager._runtimes.clear()


def test_injected_transcript_resolver_is_polled_for_binding():
    calls = {"n": 0}
    target = Path("/tmp/fake-codex-rollout.jsonl")

    def codex_resolver(workspace, session_id, projects_dir):
        calls["n"] += 1
        # Simulate: appears on the third poll (codex writes rollout after start)
        if calls["n"] >= 3:
            return target
        return None

    manager = _make_manager(transcript_resolver=codex_resolver)
    manager.create_session(runtime_command="codex", provider="codex", parser="codex")
    # Drive the binding worker synchronously by calling the resolver directly:
    # the worker polls _transcript_resolver in a loop until a Path is returned.
    import time

    deadline = time.time() + 5
    resolved = None
    while time.time() < deadline:
        resolved = manager._transcript_resolver("/w", "sid", None)
        if resolved is not None:
            break
    assert resolved == target
    manager._runtimes.clear()


def test_parser_for_codex_uses_codex_parser():
    manager = _make_manager()
    parse = manager._parser_for("codex")
    # CodexParser instance method (bound), not the module-level Claude parser
    assert parse.__func__ is CodexParser.parse_record or parse == CodexParser().parse_record


def test_parser_for_default_is_claude():
    from muxdesk.transcript_parser import parse_record as claude_parse

    manager = _make_manager()
    assert manager._parser_for("claude-code") is claude_parse
    assert manager._parser_for(None) is claude_parse


def test_default_transcript_resolver_returns_path_only_when_file_exists(tmp_path):
    # The default (Claude) resolver returns the pre-assigned filename only if it
    # exists on disk; otherwise None so the binding worker keeps polling.
    manager = _make_manager()
    workspace = "/home/yurem/work"
    session_id = "abc-123"
    projects_dir = str(tmp_path)
    # File does not exist yet -> None
    assert manager._transcript_resolver(workspace, session_id, projects_dir) is None
    # Create the expected slug/filename and resolve again
    slug = sm._cwd_slug(workspace)
    target = tmp_path / slug / f"{session_id}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n")
    assert manager._transcript_resolver(workspace, session_id, projects_dir) == target


def test_codex_ready_prompt_is_not_stuck_behind_stale_trust_text():
    screen = """
> Do you trust the contents of this directory?

› 1. Yes, continue
  2. No, quit

╭────────────────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.142.3)                             │
│                                                        │
│ model:     gpt-5.4-codex medium   /model to change     │
│ directory: /mnt/shares/…/workspace/harness-stock-vault │
╰────────────────────────────────────────────────────────╯

› Use /skills to list available skills

  gpt-5.4-codex medium · /mnt/shares/arch-linux/workspace/harness-stock-vault
"""
    assert sm._screen_is_ready(screen) is True
    assert sm._screen_is_blocked(screen) is False


def test_current_blocked_prompt_still_wins_after_old_codex_ready_text():
    screen = """
› Use /skills to list available skills

  gpt-5.4-codex medium · /mnt/shares/arch-linux/workspace/harness-stock-vault

Please run /login
Paste code here if prompted
"""
    assert sm._screen_is_ready(screen) is True
    assert sm._screen_is_blocked(screen) is True


def test_handle_line_uses_per_session_parser():
    """A codex-parsed rollout record routes through CodexParser, not the default
    Claude parser. We verify by feeding a codex event_msg and checking the
    emitted event reaches the bus with the codex-derived event_type."""
    manager = _make_manager()
    record = manager.create_session(runtime_command="codex", provider="codex", parser="codex")
    sid = record["app_session_id"]
    manager._bus.publish(sid, "session_init", {"state": "STARTING"})

    # Feed a codex agent_message record directly into the handler.
    codex_record = {
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": "hello from codex"},
    }
    manager._handle_line(sid, codex_record)
    history = manager._bus.history(sid)
    # The codex agent_message must arrive as an assistant_message event.
    types = [e.get("event_type") for e in history]
    assert "assistant_message" in types
    manager._runtimes.clear()


def test_codex_session_meta_updates_persisted_resume_id():
    manager = _make_manager()
    record = manager.create_session(runtime_command="codex", provider="codex", parser="codex")
    sid = record["app_session_id"]

    manager._handle_line(
        sid,
        {
            "type": "session_meta",
            "payload": {
                "id": "real-codex-session-id",
                "cwd": record["workspace_path"],
                "cli_version": "0.142.3",
            },
        },
    )

    assert manager.get(sid)["claude_session_id"] == "real-codex-session-id"
    manager._runtimes.clear()


def test_codex_resume_uses_persisted_real_session_id():
    seen: list[dict] = []

    def codex_builder(**kw):
        seen.append(kw)
        return "codex"

    manager = _make_manager(command_builder=codex_builder)
    record = manager.create_session(runtime_command="codex", provider="codex", parser="codex")
    sid = record["app_session_id"]
    manager._handle_line(
        sid,
        {
            "type": "session_meta",
            "payload": {
                "id": "real-codex-session-id",
                "cwd": record["workspace_path"],
                "cli_version": "0.142.3",
            },
        },
    )
    manager._registry.update(sid, transcript_path="/tmp/rollout.jsonl", transcript_inode=123)
    manager._driver.has_session = MagicMock(return_value=False)
    manager._driver.new_session.reset_mock()

    manager.resume_session(sid)

    assert seen[-1]["resume_id"] == "real-codex-session-id"
    manager._runtimes.clear()


def test_empty_codex_resume_reopens_fresh_session_instead_of_dead_resume():
    seen: list[dict] = []

    def codex_builder(**kw):
        seen.append(kw)
        return "codex"

    manager = _make_manager(command_builder=codex_builder)
    record = manager.create_session(runtime_command="codex", provider="codex", parser="codex")
    sid = record["app_session_id"]
    placeholder = record["claude_session_id"]
    manager._driver.has_session = MagicMock(return_value=False)
    manager._driver.new_session.reset_mock()

    resumed = manager.resume_session(sid)

    assert resumed is not None
    assert seen[-1]["resume_id"] is None
    assert seen[-1]["session_id"] != placeholder
    assert resumed["claude_session_id"] == seen[-1]["session_id"]
    assert resumed["transcript_path"] is None
    manager._runtimes.clear()
