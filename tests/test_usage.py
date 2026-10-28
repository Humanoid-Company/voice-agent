"""What calls cost (server/usage.py) and the session cap carrying a live talk on (server/app.py /api/me)."""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

import server.app as web
from server import usage
from server.call_log import CallLog
from server.live_bridge import SidebandToolBridge

ALICE = "alice-usage-0123456789ab"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(web, "LOGS_TOKEN", "secret")
    return TestClient(web.app)


def _log(session: str, minutes: float, *, tokens=(0, 0), tools=0, **meta) -> CallLog:
    log = CallLog(session_id=session, client="abcd1234", started_at=time.time() - minutes * 60, meta=meta)
    for _ in range(tools):
        log.add("tool_call", name="web_search")
    if tokens != (0, 0):
        log.add("backend_done", status="completed", tokens_in=tokens[0], tokens_out=tokens[1])
    log.end()
    return log


def test_call_row_counts_minutes_tokens_and_kind():
    row = usage.call_row(_log("s1", 2, tokens=(6000, 50), tools=1))
    assert round(row["seconds"]) == 120 and row["tokens_in"] == 6000 and row["tools"] == 1 and row["kind"] == "voice"
    assert usage.call_row(_log("s2", 1, mode="concierge"))["kind"] == "concierge"


def test_summary_per_day_and_browser_with_prices():
    usage.store.record(usage.call_row(_log("a", 10, tokens=(1_000_000, 0))))
    usage.store.record(usage.call_row(_log("b", 2)))
    usage.store.record(usage.call_row(_log("b", 4)))  # the same call written again: replaced, not added
    s = usage.store.summary(7)
    day = s["days"][0]
    assert day["calls"] == 2 and day["minutes"] == pytest.approx(14, abs=0.2)
    assert day["live_usd"] == pytest.approx(0.70, abs=0.02) and day["backend_usd"] == pytest.approx(0.10, abs=0.001)
    assert s["total"]["usd"] == pytest.approx(0.80, abs=0.02) and s["clients"][0]["client"] == "abcd1234"


def test_usage_needs_the_logs_token(client):
    assert client.get("/api/usage").status_code == 401
    assert client.get("/api/usage", headers={"X-Logs-Token": "secret"}).json()["total"]["calls"] == 0


def test_a_closed_call_is_recorded_and_a_cap_close_lets_the_page_carry_on(client):
    assert web.MAX_SESSION_S == 10 * 60  # 10 min a session: a forgotten tab costs at most that
    user = web.users.get(ALICE)
    bridge = SidebandToolBridge(client=None, session_id="cap1", executor=user.executor,
                                call_log=_log("cap1", 10, tokens=(5000, 40)))
    bridge.expired = True  # closed by the cap

    async def close():
        web._call_closed(user, bridge)
        await asyncio.sleep(0.2)  # the write runs in a worker thread

    asyncio.run(close())
    assert client.get("/api/me", headers={"X-Client-Id": ALICE}).json()["expired"] is True
    assert usage.store.summary(1)["days"][0]["calls"] == 1
    # A call the page closed itself is not «expired»
    other = web.users.get("bob-usage-0123456789abc")
    web._call_closed(other, SidebandToolBridge(client=None, session_id="x", executor=other.executor))
    assert client.get("/api/me", headers={"X-Client-Id": "bob-usage-0123456789abc"}).json()["expired"] is False
