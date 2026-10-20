"""Team knowledge: sets of records (people with their age, hobbies, favourite films…; an event's facts)
uploaded on the page «Знання» (web/knowledge.html) and looked up by Єва with knowledge_lookup.

One store for the whole team (everyone with the access code), not per browser: «Корпоратив 20.10» is
uploaded once and every call knows it.

Postgres (Neon) when DATABASE_URL is set. Without it, a SQLite file (KNOWLEDGE_SQLITE, data/knowledge.db):
fine locally, but on Render every deploy wipes it — /api/knowledge says so and the page shows it.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MAX_ITEMS_PER_SET = 2000
MAX_FIELDS_PER_ITEM = 40
MAX_VALUE_CHARS = 1000
MAX_TITLE_CHARS = 200
MAX_SET_NAME_CHARS = 120
MAX_NOTE_CHARS = 1000
# Everything enabled goes to the backend model at once while it fits (it matches names and answers
# «хто з них любить Тарантіно» better than any search); above this, only the closest records.
LOOKUP_ALL_CHARS = 16_000
LOOKUP_TOP = 40
# The enabled records, kept between lookups: Neon wakes in ~0.5 s after a pause, and a call asks often.
SNAPSHOT_TTL_S = 60.0

_SCHEMA = {
    "postgres": [
        """CREATE TABLE IF NOT EXISTS kb_sets (
            id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '', enabled BOOLEAN NOT NULL DEFAULT TRUE,
            item_count INTEGER NOT NULL DEFAULT 0, created_at DOUBLE PRECISION NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS kb_items (
            id BIGSERIAL PRIMARY KEY, set_id BIGINT NOT NULL REFERENCES kb_sets(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, title TEXT NOT NULL, fields TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS kb_items_set ON kb_items(set_id, position)",
    ],
    "sqlite": [
        """CREATE TABLE IF NOT EXISTS kb_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
            item_count INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS kb_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, set_id INTEGER NOT NULL REFERENCES kb_sets(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, title TEXT NOT NULL, fields TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS kb_items_set ON kb_items(set_id, position)",
    ],
}


class KnowledgeError(Exception):
    """The store can't be reached or refused the request; the message is for the page."""


@dataclass
class Record:
    set_name: str
    title: str
    fields: dict[str, str]

    def line(self) -> str:
        facts = "; ".join(f"{k}: {v}" for k, v in self.fields.items())
        return f"{self.title} — {facts}" if facts else self.title


def clean_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Records as the page sent them → titles and string fields within the limits; empty ones dropped."""
    out: list[dict[str, Any]] = []
    for item in items[:MAX_ITEMS_PER_SET]:
        title = " ".join(str(item.get("title") or "").split())[:MAX_TITLE_CHARS]
        fields: dict[str, str] = {}
        for key, value in list((item.get("fields") or {}).items())[:MAX_FIELDS_PER_ITEM]:
            key = " ".join(str(key).split())[:80]
            value = " ".join(str(value if value is not None else "").split())[:MAX_VALUE_CHARS]
            if key and value:
                fields[key] = value
        if title or fields:
            out.append({"title": title or next(iter(fields.values())), "fields": fields})
    return out


class KnowledgeStore:
    def __init__(self, database_url: str = "", sqlite_path: str = "data/knowledge.db") -> None:
        self.database_url = database_url.strip()
        self.kind = "postgres" if self.database_url else "sqlite"
        self.sqlite_path = sqlite_path
        self._ready = False
        self._lock = threading.Lock()
        self._snapshot: list[Record] | None = None
        self._sets_summary: list[dict[str, Any]] = []
        self._snapshot_at = 0.0

    # ── connection ──
    @contextmanager
    def _conn(self) -> Iterator[Any]:
        try:
            if self.kind == "postgres":
                import psycopg

                conn: Any = psycopg.connect(self.database_url, connect_timeout=10)
            else:
                if os.path.dirname(self.sqlite_path):
                    os.makedirs(os.path.dirname(self.sqlite_path), exist_ok=True)
                conn = sqlite3.connect(self.sqlite_path, timeout=10)
                conn.execute("PRAGMA foreign_keys = ON")
        except Exception as exc:
            logger.exception("knowledge.connect_failed kind=%s", self.kind)
            raise KnowledgeError(f"База знань недоступна ({type(exc).__name__}).") from exc
        try:
            if not self._ready:
                with self._lock:
                    if not self._ready:
                        for statement in _SCHEMA[self.kind]:
                            conn.execute(statement)
                        conn.commit()
                        self._ready = True
            yield conn
            conn.commit()
        except KnowledgeError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            self._ready = False  # the tables may be gone (a reset database): made again on the next call
            logger.exception("knowledge.query_failed kind=%s", self.kind)
            raise KnowledgeError(f"Помилка бази знань ({type(exc).__name__}).") from exc
        finally:
            conn.close()

    def _q(self, sql: str) -> str:
        return sql if self.kind == "postgres" else sql.replace("%s", "?")

    @property
    def persistent(self) -> bool:
        """False for SQLite on Render: a deploy wipes the disk."""
        return self.kind == "postgres" or not os.getenv("RENDER")

    def _changed(self) -> None:
        self._snapshot = None

    # ── sets ──
    def list_sets(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, name, note, source, enabled, item_count, created_at FROM kb_sets ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [self._set_row(r) for r in rows]

    @staticmethod
    def _set_row(r: Any) -> dict[str, Any]:
        return {"id": int(r[0]), "name": r[1], "note": r[2], "source": r[3], "enabled": bool(r[4]),
                "count": int(r[5]), "created_at": float(r[6])}

    def get_set(self, set_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                self._q("SELECT id, name, note, source, enabled, item_count, created_at FROM kb_sets WHERE id = %s"),
                (set_id,),
            ).fetchone()
            if row is None:
                return None
            items = conn.execute(
                self._q("SELECT title, fields FROM kb_items WHERE set_id = %s ORDER BY position"), (set_id,)
            ).fetchall()
        data = self._set_row(row)
        data["items"] = [{"title": t, "fields": json.loads(f)} for t, f in items]
        return data

    def create_set(self, name: str, items: list[dict[str, Any]], *, note: str = "", source: str = "") -> dict[str, Any]:
        name = " ".join(name.split())[:MAX_SET_NAME_CHARS]
        items = clean_items(items)
        if not name:
            raise KnowledgeError("Дайте набору назву.")
        if not items:
            raise KnowledgeError("У наборі немає жодного запису.")
        with self._conn() as conn:
            row = conn.execute(
                self._q("INSERT INTO kb_sets (name, note, source, enabled, item_count, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id"),
                (name, note.strip()[:MAX_NOTE_CHARS], source.strip()[:200], True, len(items), time.time()),
            ).fetchone()
            set_id = int(row[0])
            rows = [(set_id, i, it["title"], json.dumps(it["fields"], ensure_ascii=False)) for i, it in enumerate(items)]
            sql = self._q("INSERT INTO kb_items (set_id, position, title, fields) VALUES (%s, %s, %s, %s)")
            if self.kind == "postgres":
                with conn.cursor() as cur:
                    cur.executemany(sql, rows)
            else:
                conn.executemany(sql, rows)
        self._changed()
        logger.info("knowledge.set_created id=%s items=%s kind=%s", set_id, len(items), self.kind)
        created = self.get_set(set_id)
        assert created is not None
        return created

    def update_set(self, set_id: int, *, enabled: bool | None = None, name: str | None = None,
                   note: str | None = None) -> dict[str, Any] | None:
        changes: list[tuple[str, Any]] = []
        if enabled is not None:
            changes.append(("enabled", bool(enabled)))
        if name is not None and " ".join(name.split()):
            changes.append(("name", " ".join(name.split())[:MAX_SET_NAME_CHARS]))
        if note is not None:
            changes.append(("note", note.strip()[:MAX_NOTE_CHARS]))
        if changes:
            with self._conn() as conn:
                conn.execute(
                    self._q("UPDATE kb_sets SET " + ", ".join(f"{col} = %s" for col, _ in changes) + " WHERE id = %s"),
                    (*[v for _, v in changes], set_id),
                )
            self._changed()
        return self.get_set(set_id)

    def delete_set(self, set_id: int) -> bool:
        with self._conn() as conn:
            conn.execute(self._q("DELETE FROM kb_items WHERE set_id = %s"), (set_id,))
            gone = conn.execute(self._q("DELETE FROM kb_sets WHERE id = %s"), (set_id,)).rowcount
        self._changed()
        return bool(gone)

    # ── what Єва knows ──
    def enabled_records(self) -> tuple[list[dict[str, Any]], list[Record]]:
        """The enabled sets (name, note, count, a few titles) and all their records, cached."""
        if self._snapshot is not None and time.monotonic() - self._snapshot_at < SNAPSHOT_TTL_S:
            return self._sets_summary, self._snapshot
        with self._conn() as conn:
            sets = conn.execute(
                self._q("SELECT id, name, note, item_count FROM kb_sets WHERE enabled = %s ORDER BY created_at, id"),
                (True,),
            ).fetchall()
            rows = conn.execute(
                self._q("SELECT s.id, i.title, i.fields FROM kb_items i JOIN kb_sets s ON s.id = i.set_id "
                        "WHERE s.enabled = %s ORDER BY s.created_at, s.id, i.position"),
                (True,),
            ).fetchall()
        names = {int(s[0]): s[1] for s in sets}
        records = [Record(names[int(sid)], title, json.loads(fields)) for sid, title, fields in rows if int(sid) in names]
        summary = [
            {"name": s[1], "note": s[2], "count": int(s[3]),
             "titles": [r.title for r in records if r.set_name == s[1]][:25]}
            for s in sets
        ]
        self._sets_summary, self._snapshot, self._snapshot_at = summary, records, time.monotonic()
        return summary, records

    def lookup(self, query: str, set_name: str = "") -> dict[str, Any]:
        """Records for the backend model: everything enabled while it fits, else the closest ones."""
        sets, records = self.enabled_records()
        if set_name.strip():
            wanted = _best_set(set_name, [s["name"] for s in sets])
            if wanted:
                records = [r for r in records if r.set_name == wanted]
        total = len(records)
        text = "\n".join(f"[{r.set_name}] {r.line()}" for r in records)
        if len(text) <= LOOKUP_ALL_CHARS:
            return {"sets": sets, "total": total, "shown": total, "text": text}
        scored = sorted(records, key=lambda r: -_score(query, r))[:LOOKUP_TOP]
        text = "\n".join(f"[{r.set_name}] {r.line()}" for r in scored)
        return {"sets": sets, "total": total, "shown": len(scored), "text": text[:LOOKUP_ALL_CHARS]}


# ── loose matching: names come from speech recognition, declined («Олегові») or misspelt («Алєг») ──
_WORD = re.compile(r"[\w'’ʼ-]+", re.UNICODE)


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е").replace("’", "'").replace("ʼ", "'")


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall(_norm(text)) if len(w) >= 3]


def _close(a: str, b: str) -> bool:
    if a[:4] == b[:4] and min(len(a), len(b)) >= 4:  # same stem: Олег / Олегові / Олега
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def _score(query: str, record: Record) -> float:
    q = _words(query)
    if not q:
        return 0.0
    title = _words(record.title)
    rest = _words(" ".join(f"{k} {v}" for k, v in record.fields.items()))
    score = 0.0
    for w in q:
        if any(_close(w, t) for t in title):
            score += 3
        elif any(_close(w, t) for t in rest):
            score += 1
    return score


def _best_set(name: str, names: list[str]) -> str | None:
    n = _norm(name)
    for candidate in names:
        if n in _norm(candidate) or _norm(candidate) in n:
            return candidate
    best = difflib.get_close_matches(n, [_norm(c) for c in names], n=1, cutoff=0.5)
    return next((c for c in names if _norm(c) == best[0]), None) if best else None


# ── prompts ──
def live_note(sets: list[dict[str, Any]]) -> str:
    """For GPT-Live: what the team uploaded, so questions about those people go to the backend."""
    if not sets:
        return ""
    parts = []
    for s in sets[:10]:
        names = ", ".join(s["titles"][:15])
        more = "…" if s["count"] > len(s["titles"][:15]) else ""
        parts.append(f"«{s['name']}» ({s['count']}: {names}{more})" + (f" — {s['note'][:150]}" if s["note"] else ""))
    return (
        "\nTeam knowledge: the team gave you records about people and events — " + "; ".join(parts) + ". "
        "Anything about these people or events — who someone is, their age, hobbies, favourite films, who "
        "likes what, who to seat together, a gift or a toast for them, what is planned — goes to the backend "
        "at once (it looks them up); never answer about them from memory, never guess or invent a fact about "
        "them. A name you hear may be declined or misheard («Олегові», «про Марину», «Алєг»): still delegate. "
        "Asked what you know or remember about the team, name these sets. Tell what it found in your own "
        "words, warmly, like someone who knows these people.")


BACKEND_NOTE = (
    "\nTEAM KNOWLEDGE: knowledge_lookup returns the records the team uploaded (people with their age, "
    "hobbies, favourite films and so on; an event's facts). Call it for any question about a person or "
    "event from them, and for «хто з них…», «кому…», «що подарувати…» — the query is the name or words as "
    "heard (names come from speech recognition: declined or misspelt; the tool matches loosely, and you "
    "match the right person yourself). Then answer only from the records: the concrete values, one to "
    "three spoken sentences. For «хто з них…» go through every record and name everyone who fits, by "
    "meaning, not only by the words: a film counts for its director («Кримінальне чтиво» is Tarantino's), "
    "a diet for «не їсть м'ясо», a hobby for «спортивний». If the records lack it, say plainly "
    "that it isn't in what the team gave — never invent. Two people with a close name: ask which one.\n"
)


# The team's store. Tests swap it for a temporary one (monkeypatch server.knowledge.store).
store = KnowledgeStore(os.getenv("DATABASE_URL", ""), os.getenv("KNOWLEDGE_SQLITE", "data/knowledge.db"))
