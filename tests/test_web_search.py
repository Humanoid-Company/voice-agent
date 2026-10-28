"""web_search against Tavily's API with a fake transport: what is sent, what the model gets back."""
import asyncio
import json

import httpx

from server import web_search as ws

REAL_CLIENT = httpx.AsyncClient  # the patch below replaces it for the whole httpx module


def run(monkeypatch, handler, **kw):
    monkeypatch.setattr(ws.httpx, "AsyncClient", lambda **k: REAL_CLIENT(transport=httpx.MockTransport(handler), **k))
    return asyncio.run(ws.web_search(kw.pop("query", "новини"), api_key="k", **kw))


def test_recency_becomes_time_range_and_topic_is_passed_not_the_unknown_days(monkeypatch):
    sent = {}

    def handler(req):
        sent.update(json.loads(req.content))
        return httpx.Response(200, json={"results": []})

    run(monkeypatch, handler, recency_days=1, topic="news")
    assert sent["time_range"] == "day" and sent["topic"] == "news" and "days" not in sent
    sent.clear()
    run(monkeypatch, handler)
    assert "time_range" not in sent and "topic" not in sent  # nothing asked: nothing sent
    sent.clear()
    run(monkeypatch, handler, recency_days=5, topic="weird")
    assert sent["time_range"] == "week" and "topic" not in sent  # an unknown topic is dropped
    assert [ws._time_range(d) for d in (1, 2, 7, 8, 31, 32)] == ["day", "week", "week", "month", "month", "year"]


def test_the_digest_keeps_enough_of_each_snippet_and_the_date(monkeypatch):
    body = {"results": [{"url": "https://www.bbc.com/x", "title": "Заголовок", "published_date": "2026-10-28", "content": "ж" * 800}]}
    out = run(monkeypatch, lambda req: httpx.Response(200, json=body))
    assert out.startswith("- Заголовок (bbc.com, 2026-10-28): ") and out.count("ж") == 500


def test_failures_come_back_as_text_for_the_model(monkeypatch):
    assert "не встиг" in run(monkeypatch, lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))
    assert "недоступний" in run(monkeypatch, lambda req: httpx.Response(500))
    assert "Надійних результатів" in run(monkeypatch, lambda req: httpx.Response(200, json={"results": []}))
    assert run(monkeypatch, lambda req: httpx.Response(200), query="  ") == "Порожній пошуковий запит."
