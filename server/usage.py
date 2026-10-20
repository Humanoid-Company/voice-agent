"""What each call cost: its open minutes (GPT-Live bills every open second) and the backend's tokens, kept in
the database (server/db.py) so a deploy doesn't lose them. /logs → «Витрати» shows them per day (LOGS_TOKEN).

Prices are estimates for the page, not the bill: OpenAI's usage page is the truth. Set them in the env if
they change (LIVE_PRICE_PER_MINUTE, BACKEND_PRICE_IN_PER_M, BACKEND_PRICE_OUT_PER_M).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import TIMEZONE
from server.call_log import CallLog
from server.db import SqlStore

logger = logging.getLogger(__name__)

LIVE_PRICE_PER_MINUTE = float(os.getenv("LIVE_PRICE_PER_MINUTE", "0.05"))
BACKEND_PRICE_IN_PER_M = float(os.getenv("BACKEND_PRICE_IN_PER_M", "0.10"))
BACKEND_PRICE_OUT_PER_M = float(os.getenv("BACKEND_PRICE_OUT_PER_M", "0.50"))

_SCHEMA = {
    "postgres": [
        """CREATE TABLE IF NOT EXISTS call_usage (
            session_id TEXT PRIMARY KEY, client TEXT NOT NULL, started_at DOUBLE PRECISION NOT NULL,
            seconds DOUBLE PRECISION NOT NULL, kind TEXT NOT NULL DEFAULT 'voice',
            tokens_in BIGINT NOT NULL DEFAULT 0, tokens_out BIGINT NOT NULL DEFAULT 0, tools INTEGER NOT NULL DEFAULT 0)""",
        "CREATE INDEX IF NOT EXISTS call_usage_started ON call_usage(started_at)",
    ],
    "sqlite": [
        """CREATE TABLE IF NOT EXISTS call_usage (
            session_id TEXT PRIMARY KEY, client TEXT NOT NULL, started_at REAL NOT NULL,
            seconds REAL NOT NULL, kind TEXT NOT NULL DEFAULT 'voice',
            tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, tools INTEGER NOT NULL DEFAULT 0)""",
        "CREATE INDEX IF NOT EXISTS call_usage_started ON call_usage(started_at)",
    ],
}


def live_usd(seconds: float) -> float:
    return seconds / 60 * LIVE_PRICE_PER_MINUTE


def backend_usd(tokens_in: int, tokens_out: int) -> float:
    return tokens_in / 1e6 * BACKEND_PRICE_IN_PER_M + tokens_out / 1e6 * BACKEND_PRICE_OUT_PER_M


def call_row(log: CallLog) -> dict[str, Any]:
    """One finished call → its usage: open time, the backend's tokens, tool calls, and which page opened it."""
    data = log.to_dict()
    ended = log.ended_at or time.time()
    backend = [e for e in data["events"] if e["kind"] == "backend_done"]
    meta = log.meta
    kind = "chat" if meta.get("via") == "chat" else "concierge" if meta.get("mode") == "concierge" else "voice"
    return {
        "session_id": log.session_id, "client": log.client, "started_at": log.started_at,
        "seconds": max(0.0, ended - log.started_at), "kind": kind,
        "tokens_in": sum(int(e.get("tokens_in") or 0) for e in backend),
        "tokens_out": sum(int(e.get("tokens_out") or 0) for e in backend),
        "tools": sum(1 for e in data["events"] if e["kind"] == "tool_call"),
    }


class UsageStore(SqlStore):
    SCHEMA = _SCHEMA
    UNAVAILABLE = "Облік витрат недоступний"
    FAILED = "Помилка обліку витрат"

    def record(self, row: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                self._q(
                    "INSERT INTO call_usage (session_id, client, started_at, seconds, kind, tokens_in, tokens_out, tools) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (session_id) DO UPDATE SET "
                    "seconds = excluded.seconds, tokens_in = excluded.tokens_in, tokens_out = excluded.tokens_out, "
                    "tools = excluded.tools"
                ),
                (row["session_id"], row["client"], row["started_at"], row["seconds"], row["kind"],
                 row["tokens_in"], row["tokens_out"], row["tools"]),
            )

    def summary(self, days: int = 14) -> dict[str, Any]:
        """Per day (in TIMEZONE) and per browser: calls, minutes, tokens and the estimated $."""
        since = time.time() - days * 86400
        with self._conn() as conn:
            rows = conn.execute(
                self._q("SELECT client, started_at, seconds, kind, tokens_in, tokens_out FROM call_usage "
                        "WHERE started_at >= %s ORDER BY started_at"),
                (since,),
            ).fetchall()
        tz = ZoneInfo(TIMEZONE)
        by_day: dict[str, dict[str, Any]] = {}
        by_client: dict[str, dict[str, Any]] = {}
        for client, started, seconds, kind, t_in, t_out in rows:
            day = datetime.fromtimestamp(float(started), tz).strftime("%Y-%m-%d")
            for key, bucket in ((day, by_day), (client, by_client)):
                b = bucket.setdefault(key, {"calls": 0, "seconds": 0.0, "tokens_in": 0, "tokens_out": 0, "kinds": {}})
                b["calls"] += 1
                b["seconds"] += float(seconds)
                b["tokens_in"] += int(t_in)
                b["tokens_out"] += int(t_out)
                b["kinds"][kind] = b["kinds"].get(kind, 0) + 1

        def priced(key: str, b: dict[str, Any], name: str) -> dict[str, Any]:
            live, backend = live_usd(b["seconds"]), backend_usd(b["tokens_in"], b["tokens_out"])
            return {name: key, "calls": b["calls"], "minutes": round(b["seconds"] / 60, 1),
                    "tokens_in": b["tokens_in"], "tokens_out": b["tokens_out"], "kinds": b["kinds"],
                    "live_usd": round(live, 2), "backend_usd": round(backend, 3), "usd": round(live + backend, 2)}

        days_out = [priced(d, b, "day") for d, b in sorted(by_day.items(), reverse=True)]
        clients = sorted((priced(c, b, "client") for c, b in by_client.items()), key=lambda x: -x["usd"])
        total = {"calls": sum(d["calls"] for d in days_out), "minutes": round(sum(d["minutes"] for d in days_out), 1),
                 "usd": round(sum(d["usd"] for d in days_out), 2)}
        return {"days": days_out, "clients": clients[:20], "total": total,
                "prices": {"live_per_minute": LIVE_PRICE_PER_MINUTE, "backend_in_per_m": BACKEND_PRICE_IN_PER_M,
                           "backend_out_per_m": BACKEND_PRICE_OUT_PER_M}}


# Tests swap it for a temporary one (monkeypatch server.usage.store).
store = UsageStore(os.getenv("DATABASE_URL", ""), os.getenv("USAGE_SQLITE", "data/usage.db"))
