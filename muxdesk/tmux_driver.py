from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass


class TmuxError(RuntimeError):
    """tmux command execution failed."""


@dataclass(frozen=True)
class PaneInfo:
    session: str
    window_id: str
    pane_id: str
    pane_pid: int
    pane_dead: bool


class TmuxDriver:
    """Pure subprocess wrapper for tmux operations (no libtmux).

    The injection sequence is intentionally split into three steps: load-buffer -> paste-buffer -> separate send-keys Enter,
    to safely support multi-line, CJK (UTF-8), and special characters, avoiding send-keys treating content as key names.
    """

    def __init__(self, tmux_bin: str = "tmux", default_timeout: float = 10.0) -> None:
        self._tmux = tmux_bin
        self._timeout = default_timeout

    def _run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        proc = subprocess.run(
            [self._tmux, *args],
            input=input_bytes,
            capture_output=True,
            timeout=timeout or self._timeout,
        )
        if check and proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise TmuxError(f"tmux {' '.join(args)} failed ({proc.returncode}): {stderr}")
        return proc

    # --- session lifecycle ---

    def has_session(self, name: str) -> bool:
        return self._run(["has-session", "-t", name], check=False).returncode == 0

    def new_session(self, name: str, cwd: str, command: str) -> None:
        """Create a detached tmux session and run the given command inside it."""
        self._run(["new-session", "-d", "-s", name, "-c", cwd, command])
        # remain-on-exit: keep the dead pane after claude exits, for liveness detection
        self._run(["set-option", "-t", name, "remain-on-exit", "on"], check=False)

    def kill_session(self, name: str) -> None:
        self._run(["kill-session", "-t", name], check=False)

    def list_panes(self, name: str) -> list[PaneInfo]:
        fmt = "#{session_name}\t#{window_id}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}"
        proc = self._run(["list-panes", "-t", name, "-F", fmt], check=False)
        if proc.returncode != 0:
            return []
        panes: list[PaneInfo] = []
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) != 5:
                continue
            session, window_id, pane_id, pane_pid, pane_dead = parts
            panes.append(
                PaneInfo(
                    session=session,
                    window_id=window_id,
                    pane_id=pane_id,
                    pane_pid=int(pane_pid) if pane_pid.isdigit() else 0,
                    pane_dead=pane_dead == "1",
                )
            )
        return panes

    def first_pane(self, name: str) -> PaneInfo | None:
        panes = self.list_panes(name)
        return panes[0] if panes else None

    def pane_pid(self, pane_id: str) -> int | None:
        """Look up the pane_pid for a single pane (global #{pane_id}, e.g. %128); returns None if not found.

        Agent team teammate panes belong to other tmux sessions, so we use the global pane id directly,
        without needing to know the session name (list_panes requires the session)."""
        proc = self._run(["display-message", "-p", "-t", pane_id, "#{pane_pid}"], check=False)
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        return int(out) if out.isdigit() else None

    # --- interaction ---

    def inject_text(self, pane_id: str, text: str) -> None:
        """Safely send text into the pane's input box and submit (multi-line/CJK/special-char safe).

        After bracketed paste of long multi-line content, the TUI needs time to render it as [Pasted text] collapsed;
        pressing Enter too fast gets swallowed, so after sending Enter we capture to detect residual text, and re-send if needed (up to 4 times).
        Short prompts submit on the first try (no residual in capture) -> returns in 1 round.
        """
        buffer_name = f"muxdesk-{uuid.uuid4().hex[:12]}"
        self._run(["load-buffer", "-b", buffer_name, "-"], input_bytes=text.encode("utf-8"))
        # -d: delete buffer after paste; -p: bracketed paste, so TUI knows it's a block paste
        self._run(["paste-buffer", "-d", "-p", "-b", buffer_name, "-t", pane_id])
        for _ in range(4):
            time.sleep(0.3)
            self._run(["send-keys", "-t", pane_id, "Enter"])
            time.sleep(0.25)
            # Input still has [Pasted text] collapsed residual = Enter was swallowed, re-send
            if "Pasted text" not in self.capture_pane(pane_id, lines=8):
                return

    def send_key(self, pane_id: str, *keys: str) -> None:
        """Send control keys (e.g. C-c / Escape / Enter)."""
        self._run(["send-keys", "-t", pane_id, *keys])

    def type_literal(self, pane_id: str, text: str) -> None:
        """Type literal text character by character (send-keys -l, UTF-8 safe), without sending Enter.

        Used for TUI menu 'Type something' custom answers: press the digit to select Type something, then type the answer."""
        self._run(["send-keys", "-t", pane_id, "-l", text])

    def capture_pane(self, pane_id: str, lines: int = 50) -> str:
        """Capture the last N lines of plain text from the pane (fallback for turn-complete detection)."""
        proc = self._run(
            ["capture-pane", "-p", "-t", pane_id, "-S", f"-{lines}"],
            check=False,
        )
        return proc.stdout.decode("utf-8", errors="replace")
