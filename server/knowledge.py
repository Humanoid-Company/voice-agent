"""Team knowledge: sets of records (people with their age, hobbies, favourite films…; an event's facts)
uploaded on the page «Знання» (web/knowledge.html) and looked up by Єва with knowledge_lookup.

One store for the whole team (everyone with the access code), not per browser: «Корпоратив 20.10» is
uploaded once and every call knows it.

Postgres (Neon) when DATABASE_URL is set (server/db.py). Without it, a SQLite file (KNOWLEDGE_SQLITE,
data/knowledge.db): fine locally, but on Render every deploy wipes it — /api/knowledge says so and the page
shows it.
"""
from __future__ import annotations

import difflib
import functools
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from server.db import SqlStore, StoreError

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
            item_count INTEGER NOT NULL DEFAULT 0, created_at DOUBLE PRECISION NOT NULL,
            owner_id BIGINT, owner_name TEXT NOT NULL DEFAULT '')""",
        """CREATE TABLE IF NOT EXISTS kb_items (
            id BIGSERIAL PRIMARY KEY, set_id BIGINT NOT NULL REFERENCES kb_sets(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, title TEXT NOT NULL, fields TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS kb_items_set ON kb_items(set_id, position)",
    ],
    "sqlite": [
        """CREATE TABLE IF NOT EXISTS kb_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
            item_count INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
            owner_id INTEGER, owner_name TEXT NOT NULL DEFAULT '')""",
        """CREATE TABLE IF NOT EXISTS kb_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, set_id INTEGER NOT NULL REFERENCES kb_sets(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, title TEXT NOT NULL, fields TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS kb_items_set ON kb_items(set_id, position)",
    ],
}


# Kept under its old name: the tool and the endpoints catch it.
KnowledgeError = StoreError


@dataclass
class Record:
    set_name: str
    title: str
    fields: dict[str, str]

    def line(self) -> str:
        facts = "; ".join(f"{k}: {v}" for k, v in self.fields.items())
        return f"{self.title} — {facts}" if facts else self.title

    # Its words, split once and kept (records stay in the snapshot between lookups).
    @functools.cached_property
    def title_words(self) -> frozenset[str]:
        return frozenset(_words(self.title))

    @functools.cached_property
    def fact_words(self) -> frozenset[str]:
        return frozenset(_words(" ".join(f"{k} {v}" for k, v in self.fields.items())))


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


class KnowledgeStore(SqlStore):
    SCHEMA = _SCHEMA
    # Who uploaded a set (an account's id and name): they and admins may delete it. Sets from before have none.
    ADDED_COLUMNS = [
        ("kb_sets", "owner_id", {"postgres": "BIGINT", "sqlite": "INTEGER"}),
        ("kb_sets", "owner_name", {"postgres": "TEXT NOT NULL DEFAULT ''", "sqlite": "TEXT NOT NULL DEFAULT ''"}),
    ]
    _SET_COLS = "id, name, note, source, enabled, item_count, created_at, owner_id, owner_name"
    UNAVAILABLE = "База знань недоступна"
    FAILED = "Помилка бази знань"

    def __init__(self, database_url: str = "", sqlite_path: str = "data/knowledge.db") -> None:
        super().__init__(database_url, sqlite_path)
        self._snapshot: list[Record] | None = None
        self._sets_summary: list[dict[str, Any]] = []
        self._snapshot_at = 0.0

    def _changed(self) -> None:
        self._snapshot = None

    # ── sets ──
    def list_sets(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {self._SET_COLS} FROM kb_sets ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [self._set_row(r) for r in rows]

    @staticmethod
    def _set_row(r: Any) -> dict[str, Any]:
        return {"id": int(r[0]), "name": r[1], "note": r[2], "source": r[3], "enabled": bool(r[4]),
                "count": int(r[5]), "created_at": float(r[6]),
                "owner_id": int(r[7]) if r[7] is not None else None, "owner_name": r[8] or ""}

    def get_set(self, set_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                self._q(f"SELECT {self._SET_COLS} FROM kb_sets WHERE id = %s"),
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

    def create_set(self, name: str, items: list[dict[str, Any]], *, note: str = "", source: str = "",
                   owner_id: int | None = None, owner_name: str = "") -> dict[str, Any]:
        name = " ".join(name.split())[:MAX_SET_NAME_CHARS]
        items = clean_items(items)
        if not name:
            raise KnowledgeError("Дайте набору назву.")
        if not items:
            raise KnowledgeError("У наборі немає жодного запису.")
        with self._conn() as conn:
            row = conn.execute(
                self._q("INSERT INTO kb_sets (name, note, source, enabled, item_count, created_at, owner_id, owner_name) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id"),
                (name, note.strip()[:MAX_NOTE_CHARS], source.strip()[:200], True, len(items), time.time(),
                 owner_id, " ".join(owner_name.split())[:120]),
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
        scored = _closest(query, records, LOOKUP_TOP)
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
    m = difflib.SequenceMatcher(None, a, b)
    # quick_ratio is an upper bound of ratio: most pairs are out before the slow exact one.
    return m.real_quick_ratio() >= 0.75 and m.quick_ratio() >= 0.75 and m.ratio() >= 0.75


def _matches(query_words: list[str], vocabulary: set[str]) -> list[frozenset[str]]:
    """For each query word, the words of the records it matches — each pair compared once, not once per
    record («хобі», «посада» are in every record): 2000 people took 2 s a lookup, compared record by record."""
    return [frozenset(t for t in vocabulary if _close(w, t)) for w in query_words]


def _score_with(matched: list[frozenset[str]], record: Record) -> float:
    score = 0.0
    for m in matched:
        if m & record.title_words:
            score += 3
        elif m & record.fact_words:
            score += 1
    return score


def _score(query: str, record: Record) -> float:
    q = _words(query)
    return _score_with(_matches(q, set(record.title_words | record.fact_words)), record) if q else 0.0


def _closest(query: str, records: list[Record], top: int) -> list[Record]:
    q = _words(query)
    if not q:
        return records[:top]
    vocabulary: set[str] = set()
    for r in records:
        vocabulary |= r.title_words
        vocabulary |= r.fact_words
    matched = _matches(q, vocabulary)
    return sorted(records, key=lambda r: -_score_with(matched, r))[:top]


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
        "likes what, who to seat together, a gift, food or a toast for them, what is planned — goes to the "
        "backend at once (it looks them up); never answer about them from memory, never guess or invent a fact "
        "about them. That holds whenever such a name comes up, also later in the talk: they asked for advice "
        "for a colleague or a friend and then say who it is («його звати Олег», «це Тарас з нашої команди», "
        "«для Марини») — delegate at once and redo the advice for that person; never keep general advice once "
        "you know who it is for. A name you hear may be declined or misheard («Олегові», «про Марину», «Алєг»): "
        "still delegate. "
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


# «Консьєрж» (web, the page's top switch): she has her own prompt (prompts/live_prompt.py
# build_concierge_prompt); the backend checks people against these records with this protocol.
CONCIERGE_BACKEND_NOTE = (
    "\nCONCIERGE: you check people against the team's guest list for a voice concierge; she says your reply. "
    "A name someone gives (with or without a surname): call find_guest(name, surname) — never decide from "
    "knowledge_lookup or from memory; it matches short forms («Катя» — Катерина), case endings and near "
    "spellings itself. Reply as its result says: not on the list — say you can't find this name among the "
    "guests and ask them to repeat it or add the surname; still not there — suggest they contact the person "
    "who invited them or someone from the team, and do not let them in; several with the name — ask the "
    "surname without naming anyone; already registered — say so, no check question; one person — ONE open "
    "check question about the fact it names, which never contains or hints at the answer («Нагадайте, ким "
    "ви у нас працюєте?», «Як звати вашого улюбленця?», «Яке у вас хобі?»). A name with the surname is never "
    "enough: the check question always comes first. Their answer: compare it by meaning with that fact in "
    "the record (other words for the same thing, a part of a list is enough). It matches → confirm_guest("
    "full_name exactly as in the record, verified=true, guest_answer=their own words) and reply as its result "
    "says: a short welcome by first name and «Проходьте, будь ласка» — no question, no small talk. It does not "
    "→ one more check question about another fact from the record; a second miss → confirm_guest(verified="
    "false) and reply that you could not confirm it, politely. Never reveal anything from a record before "
    "confirmation. Questions about the place or the event: knowledge_lookup, answer only from the records "
    "(an event's notes); not there — say you don't know and offer to call someone from the team; never "
    "invent places, directions or times.\n"
)
