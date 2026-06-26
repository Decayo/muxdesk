import json
import subprocess

import pytest

from muxdesk.status import context_usage, context_window, git_status


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "a.txt").write_text("x", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")


def test_non_git_dir_returns_none_branch(tmp_path):
    assert git_status(str(tmp_path)) == {"branch": None, "dirty": 0}


def test_missing_path_returns_none_branch(tmp_path):
    assert git_status(str(tmp_path / "nope")) == {"branch": None, "dirty": 0}
    assert git_status(None) == {"branch": None, "dirty": 0}


def test_reports_branch(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    status = git_status(str(repo))
    assert status["branch"] == "feature"
    assert status["dirty"] == 0


def test_counts_dirty_files(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("changed", encoding="utf-8")  # modify tracked
    (repo / "b.txt").write_text("new", encoding="utf-8")  # untracked
    status = git_status(str(repo))
    assert status["dirty"] == 2


@pytest.mark.skipif(subprocess.run(["which", "git"], capture_output=True).returncode != 0, reason="git not installed")
def test_smoke_requires_git():
    # guards the suite if a runner lacks git
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0


def test_context_window_maps_1m_suffix():
    assert context_window("claude-opus-4-8[1m]") == 1_000_000
    assert context_window("claude-sonnet-4-6") == 200_000
    assert context_window(None) == 200_000


def _write_transcript(path, turns):
    with open(path, "w", encoding="utf-8") as fh:
        for model, usage in turns:
            fh.write(json.dumps({"message": {"model": model, "usage": usage}}) + "\n")


def test_context_usage_peak_and_pct(tmp_path):
    t = tmp_path / "t.jsonl"
    _write_transcript(
        t,
        [
            ("claude-sonnet-4-6", {"input_tokens": 10, "cache_read_input_tokens": 1000, "output_tokens": 100}),
            ("claude-sonnet-4-6", {"input_tokens": 5, "cache_creation_input_tokens": 40000, "output_tokens": 900}),
        ],
    )
    # peak = max(1110, 40905) = 40905; window 200k -> 20%
    assert context_usage(str(t)) == {"peak": 40905, "window": 200_000, "pct": 20}


def test_context_usage_uses_transcript_model_for_window(tmp_path):
    t = tmp_path / "t.jsonl"
    _write_transcript(t, [("claude-opus-4-8[1m]", {"input_tokens": 500_000, "output_tokens": 0})])
    usage = context_usage(str(t))
    assert usage == {"peak": 500_000, "window": 1_000_000, "pct": 50}


def test_context_usage_missing_or_empty(tmp_path):
    assert context_usage(None) is None
    assert context_usage(str(tmp_path / "nope.jsonl")) is None
    empty = tmp_path / "e.jsonl"
    empty.write_text("not json\n{}\n", encoding="utf-8")
    assert context_usage(str(empty)) is None
