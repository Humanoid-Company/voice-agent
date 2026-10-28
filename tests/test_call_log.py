"""The hidden /logs page: per-call events (server/call_log.py) behind LOGS_TOKEN."""
from __future__ import annotations

from fastapi.testclient import TestClient

import server.app as web
from server.call_log import CallLogStore


def test_transcript_deltas_join_into_lines_and_tools_are_kept():
    store = CallLogStore(max_calls=2)
    log = store.start("s1", "abcd1234", voice="gleam", prompt="v5")
    for d in ("Який ", "курс ", "долара?"):
        log.add("user", text=d)
    log.add("tool_call", name="web_search", call_id="c1", args={"query": "курс долара"})
    log.add("tool_result", name="web_search", call_id="c1", ok=True, status="ok", message="45,08", ms=1800)
    log.add("eva", text="Сорок п'ять")
    log.add("backend_done", key="k", status="completed")  # between two pieces of her line
    log.add("eva", text=" гривень.")
    events = log.to_dict()["events"]
    assert [e["kind"] for e in events] == ["user", "tool_call", "tool_result", "eva", "backend_done"]
    assert events[3]["text"] == "Сорок п'ять гривень."
    assert log.to_dict()["events"][0]["text"] == "Який курс долара?"
    s = log.summary()
    assert s["turns"] == 1 and s["tools"] == 1 and s["errors"] == 0 and s["first"] == "Який курс долара?"
    store.start("s2", "x")
    store.start("s3", "y")
    assert store.get("s1") is None and [c["session_id"] for c in store.latest()] == ["s3", "s2"]


def test_logs_are_open_unless_a_token_is_set(monkeypatch):
    client = TestClient(web.app)
    monkeypatch.setattr(web, "LOGS_TOKEN", "")
    assert client.get("/api/logs").status_code == 200  # unset: open, like everything else
    monkeypatch.setattr(web, "LOGS_TOKEN", "s3cret")
    assert client.get("/api/logs", headers={"X-Logs-Token": "nope"}).status_code == 401
    log = web.calls.start("sess-logs-test", "client12", voice="gleam")
    log.add("user", text="Привіт")
    ok = client.get("/api/logs", headers={"X-Logs-Token": "s3cret"})
    assert ok.status_code == 200 and ok.json()[0]["session_id"] == "sess-logs-test"
    one = client.get("/api/logs/sess-logs-test", headers={"X-Logs-Token": "s3cret"}).json()
    assert one["events"][0]["text"] == "Привіт"
    assert client.get("/api/logs/missing", headers={"X-Logs-Token": "s3cret"}).status_code == 404
