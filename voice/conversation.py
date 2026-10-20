"""Dialogue history kept outside any voice session.

A GPT-Live session fixes its voice at startup, so changing the voice — or waking up again after
a long pause — means a new session. The conversation must survive that: this log collects the
transcript of every session (desktop: one per app run; web: one per browser) and seeds the next
session with it through Live's startup `input` history.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

# GPT-Live accepts at most 128 history messages and 8,192 rendered tokens. Cyrillic text runs
# about 3 characters per token (measured 2.97): 20k characters ≈ 6.7k tokens, which leaves room for
# the message framing. A pause now closes the session after 30 s, so this history is what a woken
# Єва remembers — the more of it, the better.
_LIVE_MAX_MESSAGES = 120
_LIVE_MAX_CHARS = 20_000
# A tool result kept in the history (what a tool returned), at most this long.
_TOOL_NOTE_CHARS = 400
_KEEP_TURNS = 400
# merge_interleaved (web): the same rules as the page's bubbles (web/app.js appendTranscript). A speaker's
# line goes on after a pause this short, even if the other side said a word meanwhile («О,» over the
# user's sentence)…
_TURN_RESUME_S = 1.0
# …and a pause this long ends it, as does more than an interjection from the other side (a real answer).
_TURN_PAUSE_S = 3.0
_INTERJECTION_CHARS = 25


# Tools whose results matter for later turns (a new session after a pause or a voice change knows
# only the history). Session tools (voice, name, end…) carry nothing; add the prefixes of new
# tools whose answers she may need later, e.g. ("calendar_", "web_search").
REMEMBERED_TOOL_PREFIXES: tuple[str, ...] = ("web_search",)


def remember_tool_result(log: ConversationLog | None, tool: str, message: str) -> None:
    if log is not None and tool.startswith(REMEMBERED_TOOL_PREFIXES):
        log.add_tool_note(tool, message)


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    text: str


class ConversationLog:
    """Thread-safe transcript fed by streaming transcript deltas."""

    def __init__(self, *, merge_interleaved: bool = False, clock: Callable[[], float] = time.monotonic) -> None:
        self._turns: list[Turn] = []
        self._lock = threading.Lock()
        # Without it every change of speaker starts a new turn: a «О,» said over the user cut their
        # sentence in two («…каву без» / «цукру. Зап» / «ам'ят»…), saved and fed to the next session so.
        self._merge = merge_interleaved
        self._clock = clock
        self._open: dict[str, Turn] = {}  # each speaker's line still being said (merge_interleaved)
        self._last: dict[str, float] = {}  # when each speaker's last fragment came
        self._since: dict[str, int] = {}  # how much the other side said since each speaker's last fragment        # Called after every change, outside the lock (web: saves it to the database, debounced).
        self.on_change: Callable[[], None] | None = None

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def add(self, role: str, delta: str) -> None:
        """Append a transcript fragment; a change of speaker starts a new turn."""
        if role not in ("user", "assistant") or not delta:
            return
        if self._merge:
            self._add_merged(role, delta)
            self._changed()
            return
        with self._lock:
            if self._turns and self._turns[-1].role == role:
                self._turns[-1].text += delta
            elif delta.strip():
                self._turns.append(Turn(role, delta.lstrip()))
                del self._turns[:-_KEEP_TURNS]
        self._changed()

    def _add_merged(self, role: str, delta: str) -> None:
        other = "assistant" if role == "user" else "user"
        now = self._clock()
        with self._lock:
            turn = self._open.get(role)
            last, last_other = self._last.get(role, 0.0), self._last.get(other, 0.0)
            if (turn is None or now - last > _TURN_PAUSE_S or last_other > last + _TURN_PAUSE_S
                    or (last_other > last and now - last > _TURN_RESUME_S)
                    or self._since.get(role, 0) > _INTERJECTION_CHARS):
                if not delta.strip():
                    return
                turn = Turn(role, delta.lstrip())
                self._turns.append(turn)
                self._open[role] = turn
                del self._turns[:-_KEEP_TURNS]
            elif turn.text.rstrip()[-1:] in ".!?…" and turn.text == turn.text.rstrip() and delta[:1].isalnum():
                turn.text += " " + delta  # sentences can arrive glued («недочула.О, та добре»)
            else:
                turn.text += delta
            self._last[role] = now
            self._since[role] = 0
            self._since[other] = self._since.get(other, 0) + len(delta.strip())

    def add_note(self, text: str) -> None:
        """Context she got without anyone saying it to her (overheard during a pause), for later
        sessions. A developer message, not her line: as the last assistant item of a new session's
        history it surfaced in her first words, and measured on GPT-Live it was recalled worse."""
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._open.clear()
            self._turns.append(Turn("note", text))
            del self._turns[:-_KEEP_TURNS]
        self._changed()

    def add_tool_note(self, tool: str, message: str) -> None:
        """What a tool returned, in short: a new session (after a pause or a voice change) knows only
        the history, and «перенеси третю» needs the list she read out, not just her words."""
        message = " ".join((message or "").split())
        if not message:
            return
        if len(message) > _TOOL_NOTE_CHARS:
            message = message[: _TOOL_NOTE_CHARS - 1] + "…"
        with self._lock:
            self._open.clear()
            self._turns.append(Turn("assistant", f"(Результат {tool}: {message})"))
            self._turns.append(Turn("boundary", ""))
            del self._turns[:-_KEEP_TURNS]
        self._changed()

    def end_turn(self) -> None:
        """Close the current turn so the next fragment of the same speaker starts a new one."""
        with self._lock:
            self._open.clear()
            if self._turns and self._turns[-1].role != "boundary":
                self._turns.append(Turn("boundary", ""))

    def turns(self) -> list[Turn]:
        with self._lock:
            return [Turn(t.role, t.text.strip()) for t in self._turns if t.role != "boundary" and t.text.strip()]

    def __len__(self) -> int:
        return len(self.turns())

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()
            self._open.clear()
        self._changed()

    def to_list(self) -> list[dict[str, str]]:
        """The turns as plain data (saved by the web backend, server/conversations.py)."""
        return [{"role": t.role, "text": t.text} for t in self.turns()]

    def load(self, turns: list[dict[str, str]]) -> None:
        """Replace the history with saved turns (to_list); each stays its own turn."""
        restored: list[Turn] = []
        for t in turns[-_KEEP_TURNS:]:
            if t.get("role") in ("user", "assistant", "note") and str(t.get("text") or "").strip():
                restored += [Turn(t["role"], str(t["text"])), Turn("boundary", "")]
        with self._lock:
            self._turns = restored
            self._open.clear()

    def live_input(self, *, max_messages: int = _LIVE_MAX_MESSAGES, max_chars: int = _LIVE_MAX_CHARS) -> list[dict]:
        """The most recent turns as Live startup history (oldest first, within Live's limits)."""
        picked: list[Turn] = []
        used = 0
        for turn in reversed(self.turns()):
            if len(picked) >= max_messages or used + len(turn.text) > max_chars:
                break
            picked.append(turn)
            used += len(turn.text)
        picked.reverse()
        items = []
        for turn in picked:
            if turn.role == "note":
                text = f"Overheard while you were paused (people nearby talking among themselves, not the user): {turn.text}"
                items.append({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]})
                continue
            part = (
                {"type": "input_text", "text": turn.text}
                if turn.role == "user"
                else {"type": "output_text", "text": turn.text}
            )
            items.append({"type": "message", "role": turn.role, "content": [part]})
        return items
