"""The agent's knowledge in the database, behind a password: the store (SQLite here, Postgres in production through the same
SqlStore as Eva's), the cache calls read, and the API."""
import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server.about_store import AboutKnowledge, AboutStore
from server.auth import make_token
from server.db import StoreError
from server.persona import ABOUT_MAX_CHARS


def store(tmp_path, name="a.db"):
    return AboutStore("", str(tmp_path / name))


def test_the_text_is_kept_replaced_and_survives_a_new_connection(tmp_path):
    s = store(tmp_path)
    assert s.get() == "" and s.kind == "sqlite"
    s.put("Ти робот Тарас.")
    s.put("Ти робот Тарас на стенді в Києві.")  # replaced, not added
    assert s.get() == "Ти робот Тарас на стенді в Києві."
    assert store(tmp_path).get() == "Ти робот Тарас на стенді в Києві."  # a restart of the server


def test_the_tables_are_the_agents_own_so_it_can_share_a_database_with_eva(tmp_path):
    import sqlite3

    store(tmp_path).put("x")
    names = {r[0] for r in sqlite3.connect(tmp_path / "a.db").execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "agent_about" in names and not any(n.startswith("kb_") for n in names)


class Counting:
    def __init__(self, text="v1", fail=False):
        self.text, self.fail, self.reads = text, fail, 0

    def get(self):
        self.reads += 1
        if self.fail:
            raise StoreError("down")
        return self.text

    def put(self, text):
        if self.fail:
            raise StoreError("down")
        self.text = text


def test_calls_read_a_cached_text_that_is_renewed_after_a_while_and_after_a_save():
    async def go():
        db = Counting()
        k = AboutKnowledge(db, ttl_s=0.05)
        assert await k.get() == "v1" and await k.get() == "v1" and db.reads == 1  # one query for many answers
        db.text = "v2"  # another instance saved
        await asyncio.sleep(0.07)
        assert await k.get() == "v2" and db.reads == 2
        await k.save("v3")
        assert await k.get() == "v3" and db.reads == 2  # a save shows at once, without another query

    asyncio.run(go())


def test_a_database_that_is_down_keeps_the_last_text_and_does_not_get_hammered():
    async def go():
        db = Counting()
        k = AboutKnowledge(db, ttl_s=0.05)
        assert await k.get() == "v1"
        db.fail = True
        await asyncio.sleep(0.07)
        assert await k.get() == "v1" and await k.get() == "v1" and db.reads == 2  # one failed try per ttl, not per answer
        with pytest.raises(StoreError):
            await k.save("v9")
        assert await k.get() == "v1"  # the failed save changed nothing

    asyncio.run(go())


# ---------------------------------------------------------------------- the API


@pytest.fixture
def client(tmp_path, monkeypatch):
    st = store(tmp_path, "api.db")
    monkeypatch.setattr(app_module, "about_store", st)
    monkeypatch.setattr(app_module, "about", AboutKnowledge(st))
    monkeypatch.setattr(app_module, "cfg", replace(app_module.cfg, access_password=PASSWORD))

    async def quick(_s):
        pass

    monkeypatch.setattr(app_module.asyncio, "sleep", quick)  # a wrong password waits; not in tests
    return TestClient(app_module.app), st


PASSWORD = "test-password"  # what the fixture sets as ACCESS_PASSWORD
PW = {"X-Access-Token": make_token(PASSWORD)}  # a logged-in page


def test_without_a_login_nothing_is_read_or_written(client):
    c, st = client
    bad = (
        {},
        {"X-Access-Token": "wrong"},
        {"X-Access-Token": ""},
        {"X-Access-Token": make_token("another-password")},  # signed with a different key
        {"X-Access-Token": make_token(PASSWORD, now=0)},  # long expired
        {"X-Access-Password": PASSWORD},  # the raw password is not a token
    )
    for headers in bad:
        assert c.get("/api/knowledge", headers=headers).status_code == 401, headers
        assert c.put("/api/knowledge", json={"text": "зламано"}, headers=headers).status_code == 401, headers
    assert st.get() == ""


def test_with_a_login_the_text_is_saved_and_read_back(client):
    c, st = client
    r = c.put("/api/knowledge", json={"text": "  Ти робот Тарас.  "}, headers=PW)
    assert r.status_code == 200 and r.json()["text"] == "Ти робот Тарас."
    got = c.get("/api/knowledge", headers=PW).json()
    assert got["text"] == "Ти робот Тарас." and got["storage"] == "sqlite" and got["max"] == ABOUT_MAX_CHARS
    assert got["persistent"] is True  # not on Render here
    assert st.get() == "Ти робот Тарас."


def test_the_text_is_bounded_and_text_only(client):
    c, st = client
    assert len(c.put("/api/knowledge", json={"text": "я" * 9000}, headers=PW).json()["text"]) == ABOUT_MAX_CHARS
    assert c.put("/api/knowledge", json={"text": 5}, headers=PW).json()["text"] == ""  # not text: nothing is kept
    assert c.put("/api/knowledge", json={}, headers=PW).json()["text"] == ""


def test_a_blank_password_setting_closes_the_site_for_everyone(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(app_module, "cfg", replace(app_module.cfg, access_password=""))
    assert c.get("/api/knowledge", headers=PW).status_code == 503
    assert c.post("/api/login", json={"password": ""}).status_code == 503
    assert c.get("/api/knowledge", headers={"X-Access-Token": make_token("")}).status_code == 503


def test_a_database_that_is_down_is_a_clear_error_not_a_crash(client, monkeypatch):
    c, st = client

    def boom(*a):
        raise StoreError("База знань недоступна (OperationalError).")

    monkeypatch.setattr(st, "get", boom)
    monkeypatch.setattr(st, "put", boom)
    assert c.get("/api/knowledge", headers=PW).status_code == 503
    r = c.put("/api/knowledge", json={"text": "x"}, headers=PW)
    assert r.status_code == 503 and "недоступна" in r.json()["detail"]


def test_healthz_stays_open_and_says_where_the_knowledge_lives(client):
    c, _ = client
    body = c.get("/healthz").json()  # Render's health check and the keep-awake ping carry no token
    assert body["ok"] is True and body["knowledge"] == "sqlite" and "persistent" in body


def test_without_the_setting_the_knowledge_is_locked_and_there_is_no_default_password(monkeypatch):
    from server.config import Config

    monkeypatch.delenv("ACCESS_PASSWORD", raising=False)
    assert Config().access_password == ""  # not set: closed for everyone, whatever is sent
    monkeypatch.setenv("ACCESS_PASSWORD", "from-the-environment")
    assert Config().access_password == "from-the-environment"
