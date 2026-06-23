"""muxdesk runtime settings.

Env vars use the ``MUXDESK_*`` prefix; legacy ``CC_*`` names are still read as a
fallback so existing deployments (e.g. ibkr-trade-journal) migrate without breakage.
Attribute names keep the ``cc_*`` prefix for internal-call stability.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    """Read ``MUXDESK_<name>``, falling back to legacy ``CC_<name>``, then *default*."""
    return os.environ.get(f"MUXDESK_{name}", os.environ.get(f"CC_{name}", default))


@dataclass
class Settings:
    cc_claude_command: str = _env("CLAUDE_COMMAND", "claude")
    cc_permission_mode: str = _env("PERMISSION_MODE", "acceptEdits")
    cc_default_model: str | None = _env("DEFAULT_MODEL") or None
    cc_workspace_path: str = _env("WORKSPACE", os.path.expanduser("~"))
    cc_claude_projects_dir: str = _env(
        "CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects")
    )
    cc_tmux_session_prefix: str = _env("TMUX_PREFIX", "muxdesk")
    cc_db_path: str = _env("DB_PATH", "/tmp/muxdesk-sessions.json")
