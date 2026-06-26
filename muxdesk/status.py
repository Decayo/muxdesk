"""Live session status for the cockpit status bar: git branch/dirty + shell count.

(context % / token peak — which needs the transcript usage history and a model->window map —
is a planned follow-up; this covers the cheap, reliable segments.)
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(cwd: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def git_status(workspace_path: str | None) -> dict:
    """{'branch': str|None, 'dirty': int}. branch is None when the path isn't a git work tree."""
    if not workspace_path or not Path(workspace_path).is_dir():
        return {"branch": None, "dirty": 0}
    branch = _git(workspace_path, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return {"branch": None, "dirty": 0}
    if branch == "HEAD":  # detached HEAD -> short sha
        short = _git(workspace_path, "rev-parse", "--short", "HEAD")
        branch = f"({short})" if short else "(detached)"
    porcelain = _git(workspace_path, "status", "--porcelain")
    dirty = sum(1 for line in porcelain.splitlines() if line.strip()) if porcelain else 0
    return {"branch": branch, "dirty": dirty}
