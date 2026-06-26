import subprocess

import pytest

from muxdesk.status import git_status


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
