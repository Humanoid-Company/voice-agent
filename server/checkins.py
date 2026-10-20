"""«Консьєрж» check-ins: who has already been let in, kept in the database (server/db.py) so a second person
giving the same name and answers is caught — «you are already registered». Reset per event from the page.

Also the guard on confirm_guest: a guest is confirmed only with their own answer to the check question —
heard in the call and not just their name (on prod the backend once confirmed «Олег» + «Петренко» with no
question at all).
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import TIMEZONE
from server.db import SqlStore

_SCHEMA = {
    "postgres": [
        """CREATE TABLE IF NOT EXISTS guest_checkins (
            name_key TEXT PRIMARY KEY, full_name TEXT NOT NULL, checked_at DOUBLE PRECISION NOT NULL,
            client TEXT NOT NULL DEFAULT '')""",
    ],
    "sqlite": [
        """CREATE TABLE IF NOT EXISTS guest_checkins (
            name_key TEXT PRIMARY KEY, full_name TEXT NOT NULL, checked_at REAL NOT NULL,
            client TEXT NOT NULL DEFAULT '')""",
    ],
}

_WORD = re.compile(r"[\w'’ʼ-]+", re.UNICODE)


def _words(text: str) -> list[str]:
    text = (text or "").lower().replace("ё", "е").replace("’", "'").replace("ʼ", "'")
    return [w for w in _WORD.findall(text) if len(w) >= 3]


def name_key(full_name: str) -> str:
    """«Олег  Петренко» and «петренко олег» are one guest."""
    return " ".join(sorted(_words(full_name)))


def _stem(word: str) -> str:
    return word[:5] if len(word) > 5 else word


def local_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(TIMEZONE)).strftime("%H:%M")


def answer_problem(full_name: str, answer: str, said: list[str]) -> str:
    """Why this answer can't confirm the guest, or "" when it can: it must be theirs (heard in the call)
    and more than their name."""
    words = _words(answer)
    if not words:
        return "no_answer"
    name = {_stem(w) for w in _words(full_name)}
    own = [w for w in words if _stem(w) not in name]
    if not own:
        return "name_only"
    heard = {_stem(w) for line in said for w in _words(line)}
    found = sum(1 for w in own if _stem(w) in heard)
    if found * 2 < len(own):  # most of the answer's own words were never said by the guest
        return "not_heard"
    return ""


# Who is in, kept between checks for this long: every guest asks for it (find_guest, the lookup marks), and
# each trip to Neon was time the guest stood in silence. This server writes every check-in itself.
CACHE_S = 30.0


class CheckinStore(SqlStore):
    SCHEMA = _SCHEMA
    UNAVAILABLE = "Відмітки гостей недоступні"
    FAILED = "Помилка відміток гостей"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cache: list[dict[str, Any]] | None = None
        self._cache_at = 0.0

    def find(self, full_name: str) -> dict[str, Any] | None:
        key = name_key(full_name)
        return next((c for c in self.all() if name_key(c["full_name"]) == key), None)

    def add(self, full_name: str, client: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                self._q("INSERT INTO guest_checkins (name_key, full_name, checked_at, client) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (name_key) DO NOTHING"),
                (name_key(full_name), full_name, time.time(), client),
            )
        self._cache = None

    def all(self) -> list[dict[str, Any]]:
        if self._cache is not None and time.monotonic() - self._cache_at < CACHE_S:
            return list(self._cache)
        with self._conn() as conn:
            rows = conn.execute("SELECT full_name, checked_at FROM guest_checkins ORDER BY checked_at").fetchall()
        self._cache = [{"full_name": r[0], "checked_at": float(r[1]), "time": local_time(float(r[1]))} for r in rows]
        self._cache_at = time.monotonic()
        return list(self._cache)

    def reset(self) -> int:
        with self._conn() as conn:
            removed = int(conn.execute("DELETE FROM guest_checkins").rowcount or 0)
        self._cache = None
        return removed


# Tests swap it for a temporary one (monkeypatch server.checkins.store).
store = CheckinStore(os.getenv("DATABASE_URL", ""), os.getenv("CHECKINS_SQLITE", "data/checkins.db"))
