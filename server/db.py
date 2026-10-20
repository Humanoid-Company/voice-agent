"""The backend's database: Postgres (Neon) when DATABASE_URL is set, else a SQLite file — fine locally, but
on Render every deploy wipes it. Each store (server/knowledge.py, server/conversations.py) lists its own
tables in SCHEMA; they are made on first use, and again after a failed query (a reset database).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


# One Postgres connection for the whole server, kept open: opening one to Neon (TLS, auth) took up to a second,
# and a concierge check opened several — she said «Секунду.» and the guest waited in silence. Calls take turns
# on it (the lock); after a quiet spell it is pinged first, since Neon drops idle connections when it sleeps.
_PG_LOCK = threading.RLock()
_PG: dict[str, Any] = {"conn": None, "url": "", "used": 0.0}
_PG_PING_AFTER_S = 45.0


def _pg_connection(url: str) -> Any:
    import psycopg

    conn = _PG["conn"]
    if conn is not None and (conn.closed or _PG["url"] != url):
        conn = None
    elif conn is not None and time.monotonic() - _PG["used"] > _PG_PING_AFTER_S:
        try:
            conn.execute("SELECT 1")
            conn.commit()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conn = None
    if conn is None:
        conn = psycopg.connect(url, connect_timeout=10)
        _PG.update(conn=conn, url=url)
    return conn


def _pg_drop() -> None:
    conn = _PG["conn"]
    _PG["conn"] = None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


class StoreError(Exception):
    """The database can't be reached or refused the request; the message is for the page."""


class SqlStore:
    SCHEMA: ClassVar[dict[str, list[str]]] = {"postgres": [], "sqlite": []}
    # Words for the page: «База знань недоступна», «Історія розмов недоступна»…
    UNAVAILABLE = "База даних недоступна"
    FAILED = "Помилка бази даних"

    def __init__(self, database_url: str = "", sqlite_path: str = "data/eva.db") -> None:
        self.database_url = database_url.strip()
        self.kind = "postgres" if self.database_url else "sqlite"
        self.sqlite_path = sqlite_path
        self._ready = False
        self._lock = threading.Lock()

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        if self.kind == "postgres":
            with _PG_LOCK:
                yield from self._pg_conn()
            return
        try:
            if os.path.dirname(self.sqlite_path):
                os.makedirs(os.path.dirname(self.sqlite_path), exist_ok=True)
            conn = sqlite3.connect(self.sqlite_path, timeout=10)
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception as exc:
            logger.exception("db.connect_failed store=%s kind=sqlite", type(self).__name__)
            raise StoreError(f"{self.UNAVAILABLE} ({type(exc).__name__}).") from exc
        try:
            if not self._ready:
                with self._lock:
                    if not self._ready:
                        for statement in self.SCHEMA[self.kind]:
                            conn.execute(statement)
                        conn.commit()
                        self._ready = True
            yield conn
            conn.commit()
        except StoreError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            self._ready = False  # the tables may be gone (a reset database): made again on the next call
            logger.exception("db.query_failed store=%s kind=%s", type(self).__name__, self.kind)
            raise StoreError(f"{self.FAILED} ({type(exc).__name__}).") from exc
        finally:
            conn.close()

    def _pg_conn(self) -> Iterator[Any]:
        """The shared connection (see _PG): reused, rolled back on a failed query, dropped if it broke."""
        try:
            conn = _pg_connection(self.database_url)
        except Exception as exc:
            logger.exception("db.connect_failed store=%s kind=postgres", type(self).__name__)
            raise StoreError(f"{self.UNAVAILABLE} ({type(exc).__name__}).") from exc
        try:
            if not self._ready:
                for statement in self.SCHEMA["postgres"]:
                    conn.execute(statement)
                conn.commit()
                self._ready = True
            yield conn
            conn.commit()
        except StoreError:
            conn.rollback()
            raise
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                _pg_drop()  # the connection itself broke: a new one next time
            self._ready = False
            logger.exception("db.query_failed store=%s kind=postgres", type(self).__name__)
            raise StoreError(f"{self.FAILED} ({type(exc).__name__}).") from exc
        finally:
            _PG["used"] = time.monotonic()

    def _q(self, sql: str) -> str:
        return sql if self.kind == "postgres" else sql.replace("%s", "?")

    @property
    def persistent(self) -> bool:
        """False for SQLite on Render: a deploy wipes the disk."""
        return self.kind == "postgres" or not os.getenv("RENDER")
