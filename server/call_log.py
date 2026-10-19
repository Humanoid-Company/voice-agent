"""Per-call event log for the hidden /logs page (web/logs.html): what was said, what went to the backend,
which tools ran with which arguments, what they returned and how long it took, and the notes she got.

Kept in memory — the last MAX_CALLS calls; a Render restart (every deploy) clears it. Read only with
LOGS_TOKEN (server/app.py): these are the users' conversations.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

MAX_CALLS = 200
MAX_EVENTS = 3000  # per call: a 30-minute call is a few hundred
TEXT_MAX = 4000
# Transcript deltas arriving this close together are one line.
_MERGE_S = 2.0


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= TEXT_MAX else value[:TEXT_MAX] + "…"
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value[:50]]
    return value


@dataclass
class CallLog:
    session_id: str
    client: str
    started_at: float
    meta: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    ended_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, kind: str, **data: Any) -> None:
        now = time.time()
        with self._lock:
            if len(self.events) >= MAX_EVENTS:
                return
            text = data.get("text")
            if kind in ("user", "eva") and isinstance(text, str):
                # Her line goes on across a tool or backend event logged between its pieces («курс на сь» +
                # «огодні»); a line of the other speaker ends it.
                last = next((e for e in reversed(self.events) if e["kind"] in ("user", "eva")), None)
                if last is not None and last["kind"] == kind and now - last["t_end"] < _MERGE_S:
                    last["text"] = _clip(last["text"] + text)
                    last["t_end"] = now
                    return
            self.events.append({"t": now, "t_end": now, "kind": kind, **_clip(data)})

    def end(self) -> None:
        self.ended_at = self.ended_at or time.time()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            kinds = [e["kind"] for e in self.events]
            first_user = next((e["text"] for e in self.events if e["kind"] == "user" and e.get("text", "").strip()), "")
            # Real failures: a live error, a tool that broke (not «camera off» or «needs more info»), a
            # failed backend step.
            errors = kinds.count("error") + sum(
                1 for e in self.events
                if (e["kind"] == "tool_result" and e.get("status") in ("error", "exception"))
                or (e["kind"] == "backend_done" and e.get("status") not in ("completed", None))
            )
        return {
            "session_id": self.session_id,
            "client": self.client,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "turns": kinds.count("user"),
            "tools": kinds.count("tool_call"),
            "errors": errors,
            "first": first_user.strip()[:120],
            **self.meta,
        }

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            events = [dict(e) for e in self.events]
        return {**self.summary(), "events": events}


class CallLogStore:
    def __init__(self, max_calls: int = MAX_CALLS) -> None:
        self._calls: OrderedDict[str, CallLog] = OrderedDict()
        self._max = max_calls
        self._lock = threading.Lock()

    def start(self, session_id: str, client: str, **meta: Any) -> CallLog:
        log = CallLog(session_id=session_id, client=client, started_at=time.time(), meta=meta)
        with self._lock:
            self._calls[session_id] = log
            while len(self._calls) > self._max:
                self._calls.popitem(last=False)
        return log

    def get(self, session_id: str) -> CallLog | None:
        with self._lock:
            return self._calls.get(session_id)

    def latest(self, n: int = MAX_CALLS) -> list[dict[str, Any]]:
        with self._lock:
            calls = list(self._calls.values())[-n:]
        return [c.summary() for c in reversed(calls)]
