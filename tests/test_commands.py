from muxdesk.commands import discover_commands


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_discovers_user_commands_and_skills(tmp_path):
    home = tmp_path / "home"
    _write(home / ".claude" / "commands" / "deploy.md", "# Deploy\nShip the app to prod\n")
    _write(home / ".claude" / "skills" / "review" / "SKILL.md", "---\nname: review\ndescription: Review a PR\n---\nbody\n")

    items = discover_commands(workspace_path=None, home=home)
    by_name = {i["name"]: i for i in items}

    assert by_name["deploy"] == {"name": "deploy", "hint": "Ship the app to prod", "source": "command", "scope": "user"}
    assert by_name["review"]["source"] == "skill"
    assert by_name["review"]["hint"] == "Review a PR"  # frontmatter description preferred


def test_project_overrides_user_by_name(tmp_path):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    _write(home / ".claude" / "commands" / "build.md", "user build\n")
    _write(ws / ".claude" / "commands" / "build.md", "project build\n")

    items = discover_commands(workspace_path=str(ws), home=home)
    build = next(i for i in items if i["name"] == "build")
    assert build["scope"] == "project"
    assert build["hint"] == "project build"


def test_missing_dirs_yield_empty(tmp_path):
    assert discover_commands(workspace_path=str(tmp_path / "nope"), home=tmp_path / "empty") == []


def test_results_sorted_by_name(tmp_path):
    home = tmp_path / "home"
    for n in ("zeta", "alpha", "mid"):
        _write(home / ".claude" / "commands" / f"{n}.md", f"{n} cmd\n")
    names = [i["name"] for i in discover_commands(workspace_path=None, home=home)]
    assert names == ["alpha", "mid", "zeta"]
