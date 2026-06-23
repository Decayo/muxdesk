"""Settings env resolution: MUXDESK_* prefix with legacy CC_* fallback."""
from __future__ import annotations

from muxdesk import Settings
from muxdesk.settings import _env


def test_env_reads_muxdesk_prefix(monkeypatch):
    monkeypatch.setenv("MUXDESK_FOO", "bar")
    assert _env("FOO") == "bar"


def test_env_falls_back_to_legacy_cc(monkeypatch):
    monkeypatch.delenv("MUXDESK_FOO", raising=False)
    monkeypatch.setenv("CC_FOO", "legacy")
    assert _env("FOO") == "legacy"


def test_env_muxdesk_wins_over_cc(monkeypatch):
    monkeypatch.setenv("MUXDESK_FOO", "new")
    monkeypatch.setenv("CC_FOO", "old")
    assert _env("FOO") == "new"


def test_env_default_when_unset(monkeypatch):
    monkeypatch.delenv("MUXDESK_FOO", raising=False)
    monkeypatch.delenv("CC_FOO", raising=False)
    assert _env("FOO", "fallback") == "fallback"


def test_settings_constructs_with_expected_fields():
    s = Settings()
    assert isinstance(s.cc_claude_command, str) and s.cc_claude_command
    assert isinstance(s.cc_permission_mode, str)
    assert s.cc_db_path  # some path string
