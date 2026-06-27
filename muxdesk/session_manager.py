from __future__ import annotations

import json
import re
import shlex
import threading
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

from muxdesk.settings import Settings
from muxdesk.artifact_detector import ArtifactDetector
from muxdesk.event_bus import EventBus
from muxdesk.jsonl_tailer import JsonlTailer
from muxdesk.pty_bridge import PtyBridge
from muxdesk.session_registry import SessionRegistry
from muxdesk.single_writer_actor import SingleWriterActor
from muxdesk.state_machine import SessionState, SessionStateMachine
from muxdesk.tmux_driver import TmuxDriver
from muxdesk.transcript_parser import parse_record


@dataclass
class _SessionRuntime:
    writer: SingleWriterActor
    state: SessionStateMachine
    artifacts: ArtifactDetector
    tailer: JsonlTailer | None = None


def _cwd_slug(abs_path: str) -> str:
    """Claude's projects directory naming: all non-alphanumeric characters in the absolute path are replaced with '-'."""
    return re.sub(r"[^a-zA-Z0-9]", "-", abs_path)


# Claude TUI screen sentinels (readiness probe matches via capture-pane)
_READY_RE = re.compile(r"accept edits on|shift\+tab to cycle|\? for shortcuts|bypass permissions")
_TRUST_RE = re.compile(r"trust this folder|Is this a project you created|Do you trust")
_FATAL_RE = re.compile(
    r"\b403\b|Forbidden|organization (?:disabled|is not)|model[^\n]{0,30}(?:not found|not have access)",
    re.IGNORECASE,
)
# Blocked screen (trust / login / 401) -- detected even while running; login screen extracts oauth URL for frontend to copy
_BLOCKED_SCREEN_RE = re.compile(
    r"Paste code here if prompted|Browser didn't open|Please run /login|"
    r"Invalid authentication credentials|Yes, I trust this folder|Do you trust|trust this folder\?"
)
_LOGIN_URL_RE = re.compile(r"https://[^\s]*claude\.com[^\s]*")
# Rate limit / overload (probe detects -> retry / --resume revival; includes orchestrators themselves)
_RATE_LIMIT_RE = re.compile(
    r"temporarily limiting|Rate limited|rate.?limit|Overloaded|API Error|\b529\b|too many requests", re.I
)
# Working (streaming/thinking/tool) -- probe silently skips to avoid interrupting active delivery
_WORKING_RE = re.compile(r"esc to interrupt|Channelling|Thinking|Running[…\.]|✶|✻|- · - tokens")
# Narrow "truly running" check (used by readiness probe to detect idle): spinner with live timer "(Ns" / Running... / esc to interrupt.
# Intentionally excludes bare ✻ -- completed residual "✻ Verb for Ns" also contains ✻, which would make probe falsely detect running, never set READY -> startup timeout ERROR.
_ACTIVE_TURN_RE = re.compile(r"esc to interrupt|Running…|\(\d+s")
# User self-interrupt -- probe silently skips (that's the user's intent)
_INTERRUPT_RE = re.compile(r"Interrupted by user|⎿ Interrupted|Interrupted ·")


class SessionManager:
    """Top-level coordinator: everything is a tmux claude session.

    Create session -> bind transcript jsonl -> start tailer -> parse into semantic events ->
    state_machine / artifact_detector / single_writer -> push to frontend via event_bus.
    """

    def __init__(self, settings: Settings, event_bus: EventBus, registry: SessionRegistry) -> None:
        self._settings = settings
        self._bus = event_bus
        self._registry = registry
        self._tmux_bin = "tmux"
        self._driver = TmuxDriver(self._tmux_bin)
        self._runtimes: dict[str, _SessionRuntime] = {}

    # --- create / resume ---

    def create_session(
        self,
        *,
        workspace_path: str | None = None,
        model: str | None = None,
        title: str | None = None,
        provider: str | None = None,
        runtime_command: str | None = None,
        parser: str | None = None,
        claude_projects_dir: str | None = None,
        extra_settings: dict | None = None,
        system_prompt: str | None = None,
        add_dirs: list[str] | None = None,
        permission_mode: str | None = None,
    ) -> dict:
        workspace = str(Path(workspace_path or self._settings.cc_workspace_path).expanduser().resolve())
        app_session_id = uuid.uuid4().hex
        tmux_session = f"{self._settings.cc_tmux_session_prefix}-{app_session_id[:8]}"
        model = model or self._settings.cc_default_model or None
        title = title or f"session {app_session_id[:8]}"
        runtime_command = runtime_command or self._settings.cc_claude_command
        provider = provider or ("claude" if runtime_command == self._settings.cc_claude_command else runtime_command)
        parser = parser or "claude-code"
        claude_projects_dir = str(Path(claude_projects_dir or self._settings.cc_claude_projects_dir).expanduser())

        # Pre-assign claude session id (= jsonl filename), binding hits exactly by filename, avoiding multi-session mtime races
        claude_session_id = str(uuid.uuid4())
        command = self._build_claude_command(
            runtime_command=runtime_command,
            model=model,
            title=title,
            resume_id=None,
            session_id=claude_session_id,
            extra_settings=extra_settings,
            system_prompt=system_prompt,
            add_dirs=add_dirs,
            permission_mode=permission_mode,
        )
        self._driver.new_session(tmux_session, workspace, command)
        pane = self._driver.first_pane(tmux_session)

        record = self._registry.create(
            {
                "app_session_id": app_session_id,
                "tmux_session": tmux_session,
                "pane_id": pane.pane_id if pane else None,
                "pane_pid": pane.pane_pid if pane else None,
                "workspace_path": workspace,
                "transcript_path": None,
                "transcript_inode": None,
                "claude_session_id": claude_session_id,
                "claude_projects_dir": claude_projects_dir,
                "provider": provider,
                "runtime_command": runtime_command,
                "parser": parser,
                "model": model,
                "title": title,
                "add_dirs": add_dirs or [],
                "mode": "AUTO",
                "state": SessionState.STARTING.value,
                "status": "active",
            }
        )
        self._runtimes[app_session_id] = _SessionRuntime(
            writer=SingleWriterActor(),
            state=SessionStateMachine(),
            artifacts=ArtifactDetector(workspace),
        )
        self._start_binding(app_session_id, workspace, claude_session_id, claude_projects_dir)
        self._start_readiness_probe(app_session_id, tmux_session)
        self._bus.publish(app_session_id, "session_init", {"app_session_id": app_session_id, "state": "STARTING"})
        return record

    def resume_session(self, app_session_id: str) -> dict | None:
        record = self._registry.get(app_session_id)
        if not record or not record.get("claude_session_id"):
            return None
        if self.is_alive(app_session_id):
            return record
        workspace = record["workspace_path"]
        tmux_session = f"{self._settings.cc_tmux_session_prefix}-{app_session_id[:8]}"
        runtime_command = record.get("runtime_command") or self._settings.cc_claude_command
        claude_projects_dir = record.get("claude_projects_dir") or self._settings.cc_claude_projects_dir
        command = self._build_claude_command(
            runtime_command=runtime_command,
            model=record.get("model"),
            title=record.get("title"),
            resume_id=record["claude_session_id"],
            add_dirs=record.get("add_dirs"),
        )
        self._driver.new_session(tmux_session, workspace, command)
        pane = self._driver.first_pane(tmux_session)
        self._registry.update(
            app_session_id,
            status="active",
            tmux_session=tmux_session,
            pane_id=pane.pane_id if pane else None,
            pane_pid=pane.pane_pid if pane else None,
            state=SessionState.STARTING.value,
        )
        self._runtimes[app_session_id] = _SessionRuntime(
            writer=SingleWriterActor(),
            state=SessionStateMachine(),
            artifacts=ArtifactDetector(workspace),
        )
        self._start_binding(app_session_id, workspace, record["claude_session_id"], claude_projects_dir)
        self._start_readiness_probe(app_session_id, tmux_session)
        self._bus.publish(app_session_id, "state_change", {"mode": "AUTO", "state": "STARTING"})
        return self._registry.get(app_session_id)

    def probe_recover(self, app_session_id: str) -> dict:
        """Detect session stuck on rate limit / dead and attempt recovery. Includes orchestrators (lead is also a regular session).

        - Process dead (rate limit threw error and exited / crash) -> `--resume` revival (session jsonl is persistent).
        - Alive but screen shows rate limit -> claude has its own backoff; gently nudge with Enter to retry the current turn.
        """
        record = self._registry.get(app_session_id)
        if not record:
            return {"app_session_id": app_session_id, "status": "not_found", "action": None}
        if not self.is_alive(app_session_id):
            resumed = self.resume_session(app_session_id)
            return {
                "app_session_id": app_session_id,
                "status": "dead",
                "action": "resumed" if resumed else "resume_failed",
            }
        pane_id = record.get("pane_id")
        screen = self._driver.capture_pane(pane_id, lines=40) if pane_id else ""
        if _RATE_LIMIT_RE.search(screen):
            if pane_id:
                self._driver.send_key(pane_id, "Enter")
            return {"app_session_id": app_session_id, "status": "rate_limited", "action": "retry_enter"}
        if _FATAL_RE.search(screen):
            return {"app_session_id": app_session_id, "status": "fatal", "action": None}
        return {"app_session_id": app_session_id, "status": "ok", "action": None}

    def start_probe_loop(self, interval: float = 10.0) -> None:
        """Background loop: silently ping active sessions every interval seconds. Dead -> --resume revival (including orchestrators);
        working / user interrupt -> silently skip (don't interrupt delivery, respect user intent); rate-limit stuck -> conservative nudge."""
        if getattr(self, "_probe_started", False):
            return
        self._probe_started = True
        self._rate_stuck: dict[str, int] = {}

        def loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    sessions = self._registry.list(status="active")
                except Exception:  # noqa: BLE001
                    continue
                for record in sessions:
                    self._probe_tick(record)

        threading.Thread(target=loop, daemon=True, name="cc-probe").start()

    def _probe_tick(self, record: dict) -> None:
        sid = record.get("app_session_id")
        if not sid:
            return
        try:
            # Dead (crash exit / crash / rate-limit throw error termination) -> --resume revival (including orchestrators).
            # Dead = is_alive False; naturally doesn't touch working sessions (working ones are always alive), consistent with "don't interrupt delivery".
            if not self.is_alive(sid):
                self.resume_session(sid)
                self._rate_stuck.pop(sid, None)
                return
            pane_id = record.get("pane_id")
            screen = self._driver.capture_pane(pane_id, lines=30) if pane_id else ""
            # Working / user interrupt -> silently skip
            if _WORKING_RE.search(screen) or _INTERRUPT_RE.search(screen):
                self._rate_stuck.pop(sid, None)
                return
            # Rate-limited and not working: claude has its own backoff; only nudge Enter after 2 consecutive stuck rounds (avoid interrupting retries)
            if _RATE_LIMIT_RE.search(screen):
                self._rate_stuck[sid] = self._rate_stuck.get(sid, 0) + 1
                if self._rate_stuck[sid] >= 2 and pane_id:
                    self._driver.send_key(pane_id, "Enter")
                    self._rate_stuck[sid] = 0
            else:
                self._rate_stuck.pop(sid, None)
        except Exception:  # noqa: BLE001 — probe is best-effort; individual failures don't affect other sessions
            return

    def pane_pid(self, pane_id: str) -> int | None:
        """Look up the pane_pid for a global tmux pane (#{pane_id}); used for precise team member -> sessionId matching."""
        return self._driver.pane_pid(pane_id)

    def capture_screen(self, app_session_id: str, lines: int = 40) -> str:
        """Capture the last N lines of plain text from the session pane (used for claude TUI interactive menu detection)."""
        rec = self._registry.get(app_session_id)
        pane = rec.get("pane_id") if rec else None
        return self._driver.capture_pane(pane, lines=lines) if pane else ""

    def send_keys(self, app_session_id: str, *keys: str) -> bool:
        """Send control keys to the session pane (menu navigation: Up/Down/Enter/Escape)."""
        rec = self._registry.get(app_session_id)
        pane = rec.get("pane_id") if rec else None
        if not pane:
            return False
        self._driver.send_key(pane, *keys)
        return True

    def type_text(self, app_session_id: str, text: str) -> bool:
        """Type literal text character by character into the session pane (for TUI menu 'Type something' custom answers)."""
        rec = self._registry.get(app_session_id)
        pane = rec.get("pane_id") if rec else None
        if not pane:
            return False
        self._driver.type_literal(pane, text)
        return True

    def rebind_active_sessions(self) -> int:
        """After backend restart, re-attach tailers for alive active sessions that lack a runtime (repopulate conversation).

        Registry is persistent but `_runtimes` is in-memory; after restart, alive sessions' jsonl has no one tailing ->
        conversation tab is blank (terminal direct-pty is unaffected). Here we rebuild runtime + tail from start
        (JsonlTailer from_start replays the entire conversation) + readiness probe backfills state.
        Dead sessions are left alone (handled by probe loop `--resume`). Returns the count of re-attached sessions.
        """
        count = 0
        for record in self._registry.list(status="active"):
            sid = record.get("app_session_id")
            cid = record.get("claude_session_id")
            if not sid or not cid or sid in self._runtimes:
                continue
            if not self.is_alive(sid):
                continue  # Dead sessions handled by probe loop --resume
            workspace = record["workspace_path"]
            tmux_session = record.get("tmux_session") or f"{self._settings.cc_tmux_session_prefix}-{sid[:8]}"
            self._runtimes[sid] = _SessionRuntime(
                writer=SingleWriterActor(),
                state=SessionStateMachine(),
                artifacts=ArtifactDetector(workspace),
            )
            self._registry.update(sid, mode="AUTO")  # Rebuilt runtime defaults to AUTO, clearing any residual MANUAL
            self._start_binding(sid, workspace, cid, record.get("claude_projects_dir") or self._settings.cc_claude_projects_dir)
            self._start_readiness_probe(sid, tmux_session)
            count += 1
        return count

    def _start_binding(self, app_session_id: str, workspace: str, claude_session_id: str, claude_projects_dir: str | None = None) -> None:
        """Background wait for `<claude_session_id>.jsonl` to appear and attach the tailer.
        Since claude is started with --session-id, the jsonl filename is this id, ensuring exact binding with no multi-session mtime races."""
        target = (
            Path(claude_projects_dir or self._settings.cc_claude_projects_dir).expanduser()
            / _cwd_slug(workspace)
            / f"{claude_session_id}.jsonl"
        )

        def worker() -> None:
            while True:
                runtime = self._runtimes.get(app_session_id)
                if runtime is None:
                    return  # Session has been deleted
                if runtime.tailer is not None:
                    return  # Already bound
                if target.exists():
                    try:
                        inode = target.stat().st_ino
                    except OSError:
                        inode = None
                    runtime.tailer = JsonlTailer(
                        str(target),
                        lambda rec: self._handle_line(app_session_id, rec),
                        from_start=True,
                    )
                    runtime.tailer.start()
                    self._registry.update(
                        app_session_id,
                        transcript_path=str(target),
                        transcript_inode=inode,
                    )
                    # Binding only attaches the tailer; READY is determined by readiness probe (capture-pane), state is not changed here
                    self._bus.publish(app_session_id, "transcript_bound", {"bound": True})
                    return
                time.sleep(0.5)

        threading.Thread(target=worker, name="cc-bind", daemon=True).start()

    def _set_state(self, app_session_id: str, state: SessionState, payload: dict) -> None:
        runtime = self._runtimes.get(app_session_id)
        if runtime is None:
            return
        change = runtime.state.force(state)
        if change is not None:
            self._registry.update(app_session_id, state=change.value)
            self._bus.publish(
                app_session_id,
                "state_change",
                {"state": change.value, "mode": runtime.writer.mode, **payload},
            )

    def _start_readiness_probe(self, app_session_id: str, tmux_session: str) -> None:
        """Background continuous lifecycle monitoring via capture-pane: STARTING->READY, and even after READY,
        detects runtime login / 401 / trust issues (transitions to BLOCKED_INTERACTIVE and extracts login URL for frontend to copy)."""

        def worker() -> None:
            starting_deadline = time.time() + 90
            while True:
                runtime = self._runtimes.get(app_session_id)
                if runtime is None:
                    return  # Session deleted
                st = runtime.state.state
                if st in (SessionState.ERROR, SessionState.TERMINATED):
                    return
                if not self.is_alive(app_session_id):
                    self._set_state(app_session_id, SessionState.ERROR, {"reason": "process_exited"})
                    return
                record = self._registry.get(app_session_id)
                pane_id = record.get("pane_id") if record else None
                screen = self._driver.capture_pane(pane_id, lines=40) if pane_id else ""

                if _BLOCKED_SCREEN_RE.search(screen):
                    payload: dict = {"hint": "Login / trust directory required; use the URL below to complete login in a browser, then return to the terminal tab and paste the code"}
                    url = _LOGIN_URL_RE.search(screen)
                    if url:
                        payload["login_url"] = url.group(0)
                    self._set_state(app_session_id, SessionState.BLOCKED_INTERACTIVE, payload)
                elif _FATAL_RE.search(screen):
                    self._set_state(
                        app_session_id,
                        SessionState.ERROR,
                        {"reason": "fatal", "hint": "Check the terminal tab for raw output / run claude doctor"},
                    )
                    return
                elif _READY_RE.search(screen) and not _ACTIVE_TURN_RE.search(screen):
                    # "accept edits on" footer is present even while running tools -> must exclude "truly running",
                    # otherwise probe overwrites RUNNING_TOOL/streaming to READY every 4s (state can't detect working, stop button disappears).
                    # Uses _ACTIVE_TURN_RE (live timer) instead of _WORKING_RE: the latter's bare ✻ matches completed residual -> would get stuck STARTING->ERROR.
                    self._set_state(app_session_id, SessionState.READY, {})
                elif st == SessionState.STARTING and time.time() > starting_deadline:
                    self._set_state(
                        app_session_id,
                        SessionState.ERROR,
                        {"reason": "startup_timeout", "hint": "Check the terminal tab for raw output / run claude doctor"},
                    )
                    return

                # Dense polling during startup / blocked phases; sparse after READY (only to guard against runtime login/401)
                time.sleep(1.0 if st in (SessionState.STARTING, SessionState.BLOCKED_INTERACTIVE) else 4.0)

        threading.Thread(target=worker, name="cc-monitor", daemon=True).start()

    # --- event handling ---

    def _handle_line(self, app_session_id: str, record: dict) -> None:
        runtime = self._runtimes.get(app_session_id)
        if runtime is None:
            return
        for event in parse_record(record):
            etype = event["event_type"]
            payload = event["payload"]

            # Note: the old detection of "non-muxdesk-injected user_message -> MANUAL_TAKEOVER" has been removed.
            # The mode mechanism was stripped (just converse directly), and on rebind replay from_start the writer's
            # injection records are reset, causing even previously web-injected messages to be misidentified as external
            # input -> every restart would lock all sessions into MANUAL, blocking web message submission.

            if etype in ("assistant_message", "assistant_thinking", "tool_start"):
                runtime.writer.on_assistant_activity()

            # assistant lines carry the actual running model; persist to registry (fixes "default" display when model wasn't specified at session creation)
            if etype == "assistant_message" and payload.get("model"):
                record = self._registry.get(app_session_id)
                if record is not None and record.get("model") != payload["model"]:
                    self._registry.update(app_session_id, model=payload["model"])

            if etype == "tool_start":
                runtime.artifacts.on_tool_start(payload)
            elif etype == "tool_end":
                artifact = runtime.artifacts.on_tool_end(payload)
                if artifact:
                    self._bus.publish(app_session_id, "artifact_written", artifact)

            self._bus.publish(app_session_id, etype, payload)

            change = runtime.state.on_event(etype, payload)
            if change is not None:
                self._registry.update(app_session_id, state=change.value)
                self._bus.publish(
                    app_session_id, "state_change", {"mode": runtime.writer.mode, "state": change.value}
                )
        self._registry.touch(app_session_id)

    # --- interaction ---

    def submit_user_message(self, app_session_id: str, text: str) -> bool:
        runtime = self._runtimes.get(app_session_id)
        record = self._registry.get(app_session_id)
        if runtime is None or record is None:
            return False
        # Direct conversation (mode mechanism removed): sending a web message while in MANUAL (previously triggered by terminal typing / external injection)
        # means the user wants muxdesk to take over -> auto-resume to AUTO/READY first, then check the ready gate.
        # Must be before can_accept_input, otherwise MANUAL is always blocked, deadlocking.
        if runtime.state.state == SessionState.MANUAL_TAKEOVER or not runtime.writer.can_inject():
            self.resume_automation(app_session_id)
        if not runtime.state.can_accept_input():
            self._bus.publish(
                app_session_id,
                "error",
                {
                    "message": f"session {runtime.state.state.value}: not ready yet — wait for READY, or go to the terminal tab to complete trust / login",
                    "state": runtime.state.state.value,
                },
            )
            return False
        pane_id = record.get("pane_id")
        if not pane_id:
            return False
        runtime.writer.register_injection(text)
        change = runtime.state.on_submitting()
        if change is not None:
            self._bus.publish(
                app_session_id, "state_change", {"mode": runtime.writer.mode, "state": change.value}
            )
        self._driver.inject_text(pane_id, text)
        return True

    def interrupt(self, app_session_id: str) -> None:
        record = self._registry.get(app_session_id)
        if record and record.get("pane_id"):
            self._driver.send_key(record["pane_id"], "Escape")
        # Escape cancels the turn but doesn't emit a turn-end event -> explicitly converge state back to READY (otherwise frontend stays stuck in busy)
        runtime = self._runtimes.get(app_session_id)
        if runtime is not None:
            change = runtime.state.on_interrupt()
            if change is not None:
                self._registry.update(app_session_id, state=change.value)
                self._bus.publish(
                    app_session_id, "state_change", {"mode": runtime.writer.mode, "state": change.value}
                )

    def takeover(self, app_session_id: str) -> None:
        runtime = self._runtimes.get(app_session_id)
        if runtime is None:
            return
        runtime.writer.set_manual()
        runtime.state.on_manual_takeover()
        self._registry.update(app_session_id, mode="MANUAL", state=runtime.state.state.value)
        self._bus.publish(app_session_id, "state_change", {"mode": "MANUAL", "state": runtime.state.state.value})

    def resume_automation(self, app_session_id: str) -> None:
        runtime = self._runtimes.get(app_session_id)
        if runtime is None:
            return
        runtime.writer.set_auto()
        change = runtime.state.on_resume_automation()
        state_value = change.value if change else runtime.state.state.value
        self._registry.update(app_session_id, mode="AUTO", state=state_value)
        self._bus.publish(app_session_id, "state_change", {"mode": "AUTO", "state": state_value})

    # --- lifecycle ---

    def archive(self, app_session_id: str) -> bool:
        record = self._registry.get(app_session_id)
        if not record:
            return False
        runtime = self._runtimes.pop(app_session_id, None)
        if runtime and runtime.tailer:
            runtime.tailer.stop()
        self._driver.kill_session(record["tmux_session"])
        self._registry.update(app_session_id, status="archived", state=SessionState.UNKNOWN.value)
        self._bus.publish(app_session_id, "state_change", {"state": "UNKNOWN", "status": "archived"})
        return True

    def delete(self, app_session_id: str) -> bool:
        record = self._registry.get(app_session_id)
        runtime = self._runtimes.pop(app_session_id, None)
        if runtime and runtime.tailer:
            runtime.tailer.stop()
        if record:
            self._driver.kill_session(record["tmux_session"])
        self._registry.delete(app_session_id)
        return record is not None

    def is_alive(self, app_session_id: str) -> bool:
        record = self._registry.get(app_session_id)
        if not record or not self._driver.has_session(record["tmux_session"]):
            return False
        return any(not pane.pane_dead for pane in self._driver.list_panes(record["tmux_session"]))

    # --- query / streaming ---

    def get(self, app_session_id: str) -> dict | None:
        return self._registry.get(app_session_id)

    def list_sessions(self, *, status: str | None = None) -> list[dict]:
        return self._registry.list(status=status)

    def subscribe(self, app_session_id: str, after_seq: int = 0) -> AsyncGenerator[dict, None]:
        return self._bus.subscribe(app_session_id, after_seq=after_seq)

    def history(self, app_session_id: str, after_seq: int = 0) -> list[dict]:
        return self._bus.history(app_session_id, after_seq=after_seq)

    def start_terminal(self, app_session_id: str, cols: int = 120, rows: int = 32) -> PtyBridge | None:
        record = self._registry.get(app_session_id)
        if not record:
            return None
        bridge = PtyBridge(self._tmux_bin, record["tmux_session"])
        bridge.start(cols=cols, rows=rows)
        return bridge

    def _build_claude_command(
        self,
        *,
        runtime_command: str | None,
        model: str | None,
        title: str | None,
        resume_id: str | None,
        session_id: str | None = None,
        extra_settings: dict | None = None,
        system_prompt: str | None = None,
        add_dirs: list[str] | None = None,
        permission_mode: str | None = None,
    ) -> str:
        parts = [runtime_command or self._settings.cc_claude_command]
        if resume_id:
            parts += ["--resume", resume_id]
        elif session_id:
            parts += ["--session-id", session_id]
        pm = permission_mode or self._settings.cc_permission_mode
        if pm:
            parts += ["--permission-mode", pm]
        if model:
            parts += ["--model", model]
        # Extra --add-dir: let sessions with other cwds discover this repo's .claude/skills (muxdesk-ask question-asking conventions skill)
        for d in add_dirs or []:
            parts += ["--add-dir", d]
        # Extra settings (team subagent Stop hook / agent team hook, etc.)
        if extra_settings:
            parts += ["--settings", json.dumps(extra_settings, ensure_ascii=False)]
        # Append system prompt (orchestrator/lead forces native agent team)
        if system_prompt:
            parts += ["--append-system-prompt", system_prompt]
        if title:
            parts += ["-n", title]
        return shlex.join(parts)
