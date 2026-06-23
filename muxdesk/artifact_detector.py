from __future__ import annotations

from pathlib import Path

_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


class ArtifactDetector:
    """Detect claude writing files to the vault: tool_use(Write/Edit) + tool_result(ok) + path is within vault."""

    def __init__(self, workspace_path: str) -> None:
        self._root = Path(workspace_path).resolve()
        self._pending: dict[str, str] = {}  # tool_use_id -> file_path (pending write operations)

    def on_tool_start(self, payload: dict) -> None:
        if payload.get("tool_name") not in _WRITE_TOOLS:
            return
        tool_id = payload.get("tool_use_id")
        file_path = (payload.get("input") or {}).get("file_path")
        if tool_id and file_path:
            self._pending[tool_id] = file_path

    def on_tool_end(self, payload: dict) -> dict | None:
        tool_id = payload.get("tool_use_id")
        if not tool_id or tool_id not in self._pending:
            return None
        file_path = self._pending.pop(tool_id)
        if payload.get("is_error"):
            return None
        rel_path = self._relative_to_vault(file_path)
        if rel_path is None:
            return None
        return {"rel_path": rel_path, "abs_path": file_path}

    def _relative_to_vault(self, file_path: str) -> str | None:
        try:
            resolved = Path(file_path).resolve()
            return str(resolved.relative_to(self._root))
        except (ValueError, OSError):
            return None
