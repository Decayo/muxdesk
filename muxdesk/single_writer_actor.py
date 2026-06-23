from __future__ import annotations

import threading
from collections import deque


class SingleWriterActor:
    """Single-writer coordination per session.

    Only AUTO mode accepts backend injection; before injecting, a text fingerprint is registered,
    and when the tailer sees user input it compares: match = backend injection; mismatch = human
    typed directly in the tmux pane -> switch to MANUAL.

    Note: slash commands / multi-line directives get expanded by claude into command-message /
    objective etc. multiple user blocks, which don't match the injected text. So after injection
    and before claude's first response, an "echo window" is set: any user events during this
    window are treated as injection echoes, avoiding false human-takeover detection.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode = "AUTO"
        self._pending_fingerprints: deque[str] = deque(maxlen=32)
        self._awaiting_injection_echo = False

    @property
    def mode(self) -> str:
        return self._mode

    def can_inject(self) -> bool:
        return self._mode == "AUTO"

    def register_injection(self, text: str) -> None:
        with self._lock:
            self._pending_fingerprints.append(self._fingerprint(text))
            self._awaiting_injection_echo = True

    def is_own_injection(self, text: str) -> bool:
        fingerprint = self._fingerprint(text)
        with self._lock:
            if fingerprint in self._pending_fingerprints:
                self._pending_fingerprints.remove(fingerprint)
                return True
            # Within the echo window (after injection, before claude responds), user events = command expansion, not human.
            if self._awaiting_injection_echo:
                return True
        return False

    def on_assistant_activity(self) -> None:
        """Claude started responding (assistant / tool) -> close the echo window; subsequent user input may be human takeover."""
        with self._lock:
            self._awaiting_injection_echo = False

    def set_manual(self) -> None:
        with self._lock:
            self._mode = "MANUAL"
            self._pending_fingerprints.clear()
            self._awaiting_injection_echo = False

    def set_auto(self) -> None:
        with self._lock:
            self._mode = "AUTO"
            self._awaiting_injection_echo = False

    @staticmethod
    def _fingerprint(text: str) -> str:
        return " ".join(text.split())[:200]
