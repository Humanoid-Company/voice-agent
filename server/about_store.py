"""What the agent knows about itself and the place (who it is, where, what event): one text for the whole backend, kept in the
database (server/db.py: Postgres on Neon when DATABASE_URL is set, else a SQLite file that a deploy on Render wipes), so that
every device and every call has it. Edited on the page behind a password; read by every call."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import ClassVar

from .db import SqlStore, StoreError

log = logging.getLogger(__name__)

_ID = 1  # one record
_TTL_S = 30.0  # how long a call may use the text it has before asking the database again (another instance may have changed it)


class AboutStore(SqlStore):
    SCHEMA: ClassVar[dict[str, list[str]]] = {
        "postgres": [
            "CREATE TABLE IF NOT EXISTS agent_about (id INTEGER PRIMARY KEY, text TEXT NOT NULL DEFAULT '', "
            "updated_at DOUBLE PRECISION NOT NULL)"
        ],
        "sqlite": [
            "CREATE TABLE IF NOT EXISTS agent_about (id INTEGER PRIMARY KEY, text TEXT NOT NULL DEFAULT '', "
            "updated_at REAL NOT NULL)"
        ],
    }
    UNAVAILABLE = "База знань недоступна"
    FAILED = "Помилка бази знань"

    def get(self) -> str:
        with self._conn() as conn:
            row = conn.execute(self._q("SELECT text FROM agent_about WHERE id = %s"), (_ID,)).fetchone()
        return str(row[0]) if row else ""

    def put(self, text: str) -> None:
        with self._conn() as conn:
            conn.execute(
                self._q(
                    "INSERT INTO agent_about (id, text, updated_at) VALUES (%s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text, updated_at = EXCLUDED.updated_at"
                ),
                (_ID, text, time.time()),
            )


class AboutKnowledge:
    """The text as the calls see it: kept in memory, renewed from the database every half minute and whenever it is saved.
    A database that cannot be reached leaves the last text in place (the agent still answers, only without a change)."""

    def __init__(self, store: AboutStore, ttl_s: float = _TTL_S) -> None:
        self.store, self._ttl = store, ttl_s
        self._text = ""
        self._at = -1e9
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        if time.monotonic() - self._at > self._ttl:
            async with self._lock:
                if time.monotonic() - self._at > self._ttl:
                    try:
                        self._text = await asyncio.to_thread(self.store.get)
                    except StoreError as e:
                        log.warning("knowledge not renewed: %s", e)
                    self._at = time.monotonic()  # also after a failure: not a query per answer while it is down
        return self._text

    async def save(self, text: str) -> None:
        """Raises StoreError when the database refuses: the page says so and nothing is changed."""
        await asyncio.to_thread(self.store.put, text)
        self._text, self._at = text, time.monotonic()
