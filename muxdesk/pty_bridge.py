from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import termios
from collections.abc import Awaitable, Callable


class PtyBridge:
    """Start `tmux attach -t <session>` via pty, bidirectionally bridging terminal bytes to the frontend xterm.js.

    Detach (closing WS) only ends this attach client; it does not kill the tmux session.
    Resize sets winsize via TIOCSWINSZ, so tmux and the claude TUI within receive SIGWINCH.
    """

    def __init__(self, tmux_bin: str, tmux_session: str) -> None:
        self._tmux = tmux_bin
        self._session = tmux_session
        self._pid: int | None = None
        self._fd: int | None = None

    def start(self, cols: int = 120, rows: int = 32) -> None:
        pid, fd = pty.fork()
        if pid == 0:  # child: exec to replace with attach client
            try:
                os.execvp(self._tmux, [self._tmux, "attach-session", "-t", self._session])
            except Exception:
                os._exit(1)
        self._pid = pid
        self._fd = fd
        self.resize(cols, rows)
        os.set_blocking(fd, False)

    def resize(self, cols: int, rows: int) -> None:
        if self._fd is None:
            return
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def write(self, data: bytes) -> None:
        if self._fd is not None:
            try:
                os.write(self._fd, data)
            except OSError:
                pass

    async def read_loop(self, on_data: Callable[[bytes], Awaitable[None]]) -> None:
        loop = asyncio.get_running_loop()
        fd = self._fd
        if fd is None:
            return
        chunks: asyncio.Queue[bytes] = asyncio.Queue()

        def _on_readable() -> None:
            try:
                data = os.read(fd, 65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                loop.remove_reader(fd)
                chunks.put_nowait(b"")
                return
            chunks.put_nowait(data if data else b"")
            if not data:
                loop.remove_reader(fd)

        loop.add_reader(fd, _on_readable)
        try:
            while True:
                data = await chunks.get()
                if data == b"":
                    break
                await on_data(data)
        finally:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass

    def stop(self) -> None:
        if self._pid:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except OSError:
                pass
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._pid = None
        self._fd = None
