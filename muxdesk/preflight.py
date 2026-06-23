"""Runtime dependency preflight for muxdesk.

muxdesk drives a *real* interactive ``claude`` CLI inside tmux, so the host needs
tmux + claude installed and claude already logged in. This module reports which
dependencies are present so the API / frontend can warn explicitly instead of
failing opaquely.

Every check is side-effect free (no ``claude`` invocation) — best-effort only.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def _install_hint(pkg: str) -> str:
    return (
        f"Install {pkg}: `brew install {pkg}` (macOS) / "
        f"`sudo apt install {pkg}` (Debian) / `sudo pacman -S {pkg}` (Arch)."
    )


def check(claude_command: str = "claude", claude_home: str | None = None) -> dict:
    """Return ``{ok, checks: [{name, required, ok, detail, hint}]}``.

    ``ok`` is True iff every *required* check passes. Non-required checks are
    warnings only and never flip ``ok``.
    """
    checks: list[dict] = []

    tmux = shutil.which("tmux")
    checks.append({
        "name": "tmux",
        "required": True,
        "ok": bool(tmux),
        "detail": tmux or "not found in PATH",
        "hint": None if tmux else _install_hint("tmux"),
    })

    claude = shutil.which(claude_command)
    checks.append({
        "name": "claude",
        "required": True,
        "ok": bool(claude),
        "detail": claude or f"`{claude_command}` not found in PATH",
        "hint": None if claude else "Install Claude Code CLI, then run `claude` once to log in.",
    })

    home = Path(claude_home).expanduser() if claude_home else Path.home() / ".claude"
    logged_in = home.exists()
    checks.append({
        "name": "claude login",
        "required": False,
        "ok": logged_in,
        "detail": str(home) if logged_in else f"{home} not found",
        "hint": None if logged_in else "Run `claude` once and complete login / trust the workspace.",
    })

    py_ok = sys.version_info >= (3, 11)
    checks.append({
        "name": "python",
        "required": True,
        "ok": py_ok,
        "detail": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "hint": None if py_ok else "muxdesk needs Python 3.11+.",
    })

    ok = all(c["ok"] for c in checks if c["required"])
    return {"ok": ok, "checks": checks}
