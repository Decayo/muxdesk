from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from collections import defaultdict, deque
from collections.abc import AsyncGenerator


class EventBus:
    """Per-session in-memory event bus (structure ported from account_copilot/event_bus, without ES).

    - seq increments per session.
    - Ring buffer retains recent events for frontend reconnection with after_seq gap-filling.
    - subscribe: first replays ring buffer events with seq > after_seq, then taps into the live queue; frontend deduplicates by seq watermark.
    """

    def __init__(self, history_limit: int = 2000) -> None:
        self._history_limit = history_limit
        self._lock = threading.Lock()
        self._seq_by_session: dict[str, int] = defaultdict(int)
        self._history: dict[str, deque[dict]] = defaultdict(
            lambda: deque(maxlen=history_limit)
        )
        self._subscribers: dict[str, list[queue.Queue[dict]]] = defaultdict(list)

    def publish(self, session_id: str, event_type: str, payload: dict | None = None) -> dict:
        with self._lock:
            seq = self._seq_by_session[session_id] + 1
            self._seq_by_session[session_id] = seq
            event = {
                "session_id": session_id,
                "event_type": event_type,
                "seq": seq,
                "ts": time.time(),  # server epoch, used by frontend for message timestamps / working time
                "payload": payload or {},
            }
            self._history[session_id].append(event)
            subscribers = list(self._subscribers.get(session_id, []))
        for subscriber in subscribers:
            subscriber.put(event)
        return event

    def history(self, session_id: str, after_seq: int = 0) -> list[dict]:
        with self._lock:
            return [e for e in self._history.get(session_id, ()) if e["seq"] > after_seq]

    async def subscribe(
        self, session_id: str, after_seq: int = 0, heartbeat_seconds: int = 15
    ) -> AsyncGenerator[dict, None]:
        for event in self.history(session_id, after_seq=after_seq):
            yield event

        subscriber: queue.Queue[dict] = queue.Queue()
        with self._lock:
            self._subscribers[session_id].append(subscriber)
        try:
            while True:
                try:
                    event = await asyncio.to_thread(subscriber.get, True, heartbeat_seconds)
                except queue.Empty:
                    event = {
                        "session_id": session_id,
                        "event_type": "heartbeat",
                        "seq": 0,
                        "payload": {},
                    }
                yield event
        finally:
            with self._lock:
                subs = self._subscribers.get(session_id, [])
                if subscriber in subs:
                    subs.remove(subscriber)
                if not subs:
                    self._subscribers.pop(session_id, None)


def event_to_ws_json(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, default=str)
