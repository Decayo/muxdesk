"""muxdesk -- terminal-native AI agent cockpit.

Run AI CLIs (Claude Code, …) in real tmux TTYs, rebuild conversations from
transcript jsonl (no SDK), and drive them from a web UI.

muxdesk is an *interface*, not a framework. It gives you a tmux-backed session
platform — create a session, inject a system prompt + settings, read the
transcript back as structured events, ask the user structured questions, wire an
agent graph — and stays out of your business logic. Any "orchestrator" is just a
``system_prompt`` you pass in; muxdesk never knows what your prompt is about.

Two ways to use it:

    # 1. One-liner: a full generic cockpit app (needs the `server` extra)
    from muxdesk import create_app
    app = create_app()                       # or: uvicorn muxdesk.app:app

    # 2. Assemble: drive sessions yourself, inject your own prompts (no FastAPI)
    from muxdesk import SessionManager, EventBus, SessionRegistry, Settings
    mgr = SessionManager(Settings(), EventBus(), SessionRegistry("/tmp/x.json"))
    rec = mgr.create_session(system_prompt="you are …",
                             permission_mode="acceptEdits")
    mgr.submit_user_message(rec["id"], "hello")

The core building blocks are pure-stdlib (only a `tmux`/`claude` binary at
runtime); FastAPI is an optional dependency pulled in lazily by ``create_app``.
See REQUIREMENTS.md for the capability spec.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .event_bus import EventBus, event_to_ws_json
from .session_manager import SessionManager
from .session_registry import SessionRegistry
from .settings import Settings
from .state_machine import SessionState
from .transcript_parser import extract_metadata, parse_record

if TYPE_CHECKING:  # avoid importing FastAPI for type-only consumers
    from fastapi import FastAPI

__version__ = "0.1.0"


def create_app(custom_settings: "Settings | None" = None) -> "FastAPI":
    """Build the generic cockpit FastAPI app.

    Thin re-export of :func:`muxdesk.app.create_app`, imported lazily so that
    consumers using only the core building blocks (parser / session manager)
    never pay the FastAPI import cost. Requires the ``server`` extra.
    """
    from .app import create_app as _create_app

    return _create_app(custom_settings)


__all__ = [
    "__version__",
    "create_app",
    "Settings",
    "SessionManager",
    "SessionRegistry",
    "SessionState",
    "EventBus",
    "event_to_ws_json",
    "parse_record",
    "extract_metadata",
]
