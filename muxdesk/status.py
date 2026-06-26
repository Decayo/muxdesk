"""Live session status for the cockpit status bar: git branch/dirty, shell count, context usage."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

_USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
_DEFAULT_WINDOW = 200_000
_EXTENDED_WINDOW = 1_000_000


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


def context_window(model: str | None) -> int:
    """Context window in tokens for a model id; `[1m]` suffix -> 1M, otherwise 200k."""
    if model and "[1m]" in model.lower():
        return _EXTENDED_WINDOW
    return _DEFAULT_WINDOW


def context_usage(transcript_path: str | None, model: str | None = None) -> dict | None:
    """Peak context size from the transcript's per-turn usage -> {peak, window, pct}; None if unavailable.

    Peak = max over assistant turns of (input + cache_read + cache_creation + output) tokens — i.e. the
    largest context the model has held this session. The model is taken from the transcript when present
    (it carries the actual running id), falling back to the registry `model`.
    """
    if not transcript_path or not Path(transcript_path).is_file():
        return None
    peak = 0
    seen_model = model
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                if message.get("model"):
                    seen_model = message["model"]
                usage = message.get("usage")
                if isinstance(usage, dict):
                    total = sum(int(usage.get(k) or 0) for k in _USAGE_KEYS)
                    peak = max(peak, total)
    except OSError:
        return None
    if peak == 0:
        return None
    window = context_window(seen_model)
    return {"peak": peak, "window": window, "pct": round(peak / window * 100)}
