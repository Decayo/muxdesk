from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable


class JsonlTailer:
    """Tail a transcript jsonl, parsing each line into a dict and calling on_line.

    Fault-tolerant:
    - Partial-line tolerance: only splits on newlines; incomplete fragments remain in buffer.
    - On inode change (rotation) or file truncate (size regression), reopens from the beginning.
    Uses an independent daemon thread + poll (os.stat + read), no inotify dependency.
    """

    def __init__(
        self,
        path: str,
        on_line: Callable[[dict], None],
        *,
        poll_interval: float = 0.15,
        from_start: bool = True,
    ) -> None:
        self._path = path
        self._on_line = on_line
        self._poll = poll_interval
        self._from_start = from_start
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cc-jsonl-tailer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run(self) -> None:
        buffer = ""
        inode: int | None = None
        offset = 0
        while not self._stop.is_set():
            try:
                stat = os.stat(self._path)
            except FileNotFoundError:
                self._stop.wait(self._poll)
                continue

            if inode is None:
                inode = stat.st_ino
                offset = 0 if self._from_start else stat.st_size
                buffer = ""
            elif inode != stat.st_ino:  # Rotation -> read new file from start
                inode = stat.st_ino
                offset = 0
                buffer = ""
            elif stat.st_size < offset:  # Truncated -> read from start
                offset = 0
                buffer = ""

            if stat.st_size > offset:
                try:
                    with open(self._path, encoding="utf-8", errors="replace") as fh:
                        fh.seek(offset)
                        chunk = fh.read()
                        offset = fh.tell()
                except FileNotFoundError:
                    self._stop.wait(self._poll)
                    continue
                buffer += chunk
                *lines, buffer = buffer.split("\n")
                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        record = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    try:
                        self._on_line(record)
                    except Exception:
                        # A single-line callback failure should not interrupt the entire tailer
                        pass

            self._stop.wait(self._poll)
