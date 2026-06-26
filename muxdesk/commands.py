"""Discover slash-command candidates for the input palette.

Enumerates user-level (`~/.claude`) and project-level (`<workspace>/.claude`) custom
commands (`commands/*.md`) and skills (`skills/*/SKILL.md`). The frontend merges these
with its built-in claude command set. Pure / filesystem-only — no session state.
"""

from __future__ import annotations

from pathlib import Path

_HINT_MAX = 120


def _read_hint(md_path: Path) -> str:
    """Best-effort one-line hint: frontmatter `description:`, else first prose line."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                stripped = line.strip()
                if stripped.lower().startswith("description:"):
                    return stripped.split(":", 1)[1].strip().strip("\"'")[:_HINT_MAX]
            body = text[end + 4 :]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:_HINT_MAX]
    return ""


def discover_commands(workspace_path: str | None, home: Path | None = None) -> list[dict]:
    """Return [{name, hint, source, scope}], project entries overriding user entries by name."""
    home = home or Path.home()
    roots: list[tuple[str, Path]] = [("user", home / ".claude")]
    if workspace_path:
        roots.append(("project", Path(workspace_path) / ".claude"))

    by_name: dict[str, dict] = {}
    for scope, root in roots:
        cmd_dir = root / "commands"
        if cmd_dir.is_dir():
            for f in sorted(cmd_dir.glob("*.md")):
                by_name[f.stem] = {"name": f.stem, "hint": _read_hint(f), "source": "command", "scope": scope}
        skill_dir = root / "skills"
        if skill_dir.is_dir():
            for d in sorted(p for p in skill_dir.iterdir() if p.is_dir()):
                skill_md = d / "SKILL.md"
                if skill_md.is_file():
                    by_name[d.name] = {"name": d.name, "hint": _read_hint(skill_md), "source": "skill", "scope": scope}

    return sorted(by_name.values(), key=lambda i: i["name"])
