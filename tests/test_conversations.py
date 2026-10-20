"""Saved conversations: a refresh shows the conversation again, a restart keeps what Єва remembers, «Нова
розмова» keeps the old one in «Історія» (server/conversations.py, server/web_users.py)."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import server.app as web
from server import conversations, web_users
from server.conversations import ConversationStore
from server.web_users import WebUserRegistry
from voice.conversation import ConversationLog

ANNA = "anna-browser-0123456789ab"
BORYS = "borys-browser-0123456789a"
TEST_PG = os.getenv("KNOWLEDGE_TEST_DATABASE_URL", "")  # the same throwaway Postgres as tests/test_knowledge.py


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path, monkeypatch):
    if request.param == "postgres":
        if not TEST_PG:
            pytest.skip("KNOWLEDGE_TEST_DATABASE_URL not set")
        import psycopg

        with psycopg.connect(TEST_PG) as conn:
            conn.execute("DROP TABLE IF EXISTS conversations")
        s = ConversationStore(TEST_PG)
    else:
        s = ConversationStore("", str(tmp_path / "conv.db"))
    monkeypatch.setattr(conversations, "store", s)
    return s


@pytest.fixture()
def client(store, monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    monkeypatch.setattr(web, "users", WebUserRegistry())
    return TestClient(web.app)


def _talk(user, *lines):
    for role, text in lines:
        user.conversation.add(role, text)
        user.conversation.end_turn()


def test_log_round_trip_keeps_turns_apart():
    log = ConversationLog()
    log.add("user", "Привіт")
    log.end_turn()
    log.add("user", "Ти тут?")
    log.add_note("поруч говорили про відпустку")
    saved = log.to_list()
    again = ConversationLog()
    again.load(saved)
    again.add("assistant", "Так, слухаю")
    assert [t["text"] for t in again.to_list()] == ["Привіт", "Ти тут?", "поруч говорили про відпустку", "Так, слухаю"]


def test_store_save_latest_list_get_delete(store):
    turns = [{"role": "user", "text": "Що любить Олег?"}, {"role": "assistant", "text": "Шахи."},
             {"role": "assistant", "text": "(Результат web_search: …)"}, {"role": "note", "text": "поруч"}]
    store.save("greeting", ANNA, 1.0, [{"role": "assistant", "text": "Привіт!"}])  # kept, not listed
    store.save("c1", ANNA, 100.0, turns)
    store.save("c1", ANNA, 100.0, turns + [{"role": "user", "text": "Дякую"}])  # rewritten as it grows
    store.save("c2", BORYS, 50.0, turns)
    listed = store.list(ANNA)
    assert [c["id"] for c in listed] == ["c1"] and listed[0]["title"] == "Що любить Олег?" and listed[0]["count"] == 3
    assert store.latest(ANNA)["turns"][-1]["text"] == "Дякую"
    assert store.get(BORYS, "c1") is None  # only the browser's own
    assert store.delete(ANNA, "c1") and store.latest(ANNA)["id"] == "greeting"


def test_saved_after_a_change_and_restored_after_a_restart(store, monkeypatch):
    monkeypatch.setattr(web_users, "SAVE_DELAY_S", 0.05)
    before = WebUserRegistry()
    user = before.get(ANNA)
    _talk(user, ("user", "Мене звати Остап"), ("assistant", "Привіт, Остапе!"))
    user.save_conversation()
    after = WebUserRegistry()  # a deploy: memory is gone
    again = after.get(ANNA)
    assert again.conversation_id == user.conversation_id
    assert "Остап" in again.conversation.live_input()[0]["content"][0]["text"]  # Єва remembers
    assert len(after.get(BORYS).conversation) == 0


def test_debounced_save_and_save_all(store, monkeypatch):
    monkeypatch.setattr(web_users, "SAVE_DELAY_S", 30)
    registry = WebUserRegistry()
    user = registry.get(ANNA)
    _talk(user, ("user", "Тест"))
    assert store.latest(ANNA) is None  # waiting for the debounce
    registry.save_all()  # the server stops
    assert store.latest(ANNA)["turns"] == [{"role": "user", "text": "Тест"}]


def test_page_sees_its_conversation_after_a_refresh(client):
    user = web.users.get(ANNA)
    _talk(user, ("user", "Що любить Олег?"), ("assistant", "Риболовлю."), ("assistant", "(Результат web_search: x)"))
    current = client.get("/api/conversation", headers={"X-Client-Id": ANNA}).json()
    assert current["turns"] == [{"role": "user", "text": "Що любить Олег?"}, {"role": "assistant", "text": "Риболовлю."}]
    assert client.get("/api/conversation", headers={"X-Client-Id": BORYS}).json()["turns"] == []


def test_new_conversation_keeps_the_old_one_in_history(client):
    user = web.users.get(ANNA)
    _talk(user, ("user", "Перша розмова"), ("assistant", "Так"))
    first = user.conversation_id
    assert client.delete("/api/conversation", headers={"X-Client-Id": ANNA}).json() == {"ok": True}
    assert len(user.conversation) == 0 and user.conversation_id != first
    _talk(user, ("user", "Друга розмова"))
    listing = client.get("/api/conversations", headers={"X-Client-Id": ANNA}).json()
    assert [c["title"] for c in listing["conversations"]] == ["Друга розмова", "Перша розмова"]
    assert listing["current"] == user.conversation_id
    old = client.get(f"/api/conversations/{first}", headers={"X-Client-Id": ANNA}).json()
    assert old["turns"][0]["text"] == "Перша розмова"
    # Продовжити: the first is current again, the second stays saved
    assert client.post(f"/api/conversations/{first}/resume", headers={"X-Client-Id": ANNA}).json()["ok"]
    assert user.conversation_id == first and user.conversation.to_list()[0]["text"] == "Перша розмова"
    assert len(client.get("/api/conversations", headers={"X-Client-Id": ANNA}).json()["conversations"]) == 2


def test_another_browser_cannot_read_or_delete_it(client):
    user = web.users.get(ANNA)
    _talk(user, ("user", "Секрет Анни"))
    user.save_conversation()
    conv = user.conversation_id
    assert client.get(f"/api/conversations/{conv}", headers={"X-Client-Id": BORYS}).status_code == 404
    assert client.delete(f"/api/conversations/{conv}", headers={"X-Client-Id": BORYS}).status_code == 404
    assert client.delete(f"/api/conversations/{conv}", headers={"X-Client-Id": ANNA}).json() == {"ok": True}
    assert len(user.conversation) == 0  # the current one deleted: Єва forgets it too


def test_a_new_conversation_stays_current_after_a_restart(store):
    """«Нова розмова», then only her greeting, then a deploy: the new one comes back, not the old one."""
    registry = WebUserRegistry()
    user = registry.get(ANNA)
    _talk(user, ("user", "Стара тема"))
    user.new_conversation()
    _talk(user, ("assistant", "Привіт, я слухаю."))
    user.save_conversation()
    again = WebUserRegistry().get(ANNA)
    assert again.conversation_id == user.conversation_id
    assert [t["text"] for t in again.conversation.to_list()] == ["Привіт, я слухаю."]
    assert [c["title"] for c in store.list(ANNA)] == ["Стара тема"]


def test_sound_tags_are_not_shown():
    turns = [{"role": "assistant", "text": "[breath] М'ясо не їдять Марина [hum] і Юлія."}, {"role": "assistant", "text": "[hum]"}]
    assert conversations.visible(turns) == [{"role": "assistant", "text": "М'ясо не їдять Марина і Юлія."}]
