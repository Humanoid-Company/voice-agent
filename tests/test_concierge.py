"""«Консьєрж»: a guest is confirmed only with their own answer to the check question, and only once
(server/checkins.py); a record of someone already let in says so to the backend."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import server.app as web
from server import checkins, knowledge
from server.knowledge_import import parse_file
from tools.executor import ToolExecutionContext

CSV = "ПІБ;Посада;Тварини\nОлег Петренко;бекенд-розробник;кіт Бублик\nОлег Гнатюк;дизайнер;\n".encode()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    return TestClient(web.app)


def confirm(user, said, answer, name="Олег Петренко", verified=True):
    return asyncio.run(user.executor.execute(
        "confirm_guest", {"full_name": name, "verified": verified, "guest_answer": answer},
        ToolExecutionContext(session_id="s", user_utterances=said)))


def test_a_name_and_surname_alone_never_confirm():
    """Prod: «Олег» + «Петренко» and the backend confirmed him with no check question."""
    user = web.users.get("conc-a-0123456789abcdef")
    for answer in ("", "Петренко", "Олег Петренко"):
        res = confirm(user, ["Олег", "Петренко"], answer)
        assert not res.ok and res.status == "needs_check_question"
    assert checkins.store.all() == [] and user.guest is None


def test_an_answer_the_guest_never_said_does_not_confirm():
    user = web.users.get("conc-b-0123456789abcdef")
    assert confirm(user, ["Олег", "Петренко"], "бекенд-розробник").status == "needs_check_question"
    # said in other words / a declined form: fine
    assert confirm(user, ["Олег", "Петренко", "Я бекенд розробник у вас"], "бекенд-розробник").status == "ok"
    assert user.guest["verified"] is True and checkins.store.find("петренко олег")["full_name"] == "Олег Петренко"


def test_a_second_visit_under_the_same_name_is_caught():
    first = web.users.get("conc-c-0123456789abcdef")
    assert confirm(first, ["Петренко", "Кіт Бублик"], "кіт Бублик").status == "ok"
    again = web.users.get("conc-d-0123456789abcdef")  # another browser, later
    res = confirm(again, ["Петренко", "у мене кіт Бублик"], "кіт Бублик")
    assert res.status == "already_checked_in" and "вже відмічений" in res.message
    assert again.guest["duplicate"] is True and again.guest["verified"] is False


def test_not_verified_needs_no_answer():
    user = web.users.get("conc-e-0123456789abcdef")
    assert confirm(user, [], "", verified=False).status == "not_verified" and checkins.store.all() == []


def test_lookup_in_concierge_marks_who_is_in():
    knowledge.store.create_set("Гості", parse_file("g.csv", CSV).items)
    checkins.store.add("Олег Петренко")
    user = web.users.get("conc-f-0123456789abcdef")
    ctx = ToolExecutionContext(session_id="s")
    plain = asyncio.run(user.executor.execute("knowledge_lookup", {"query": "Олег"}, ctx)).message
    assert "ВЖЕ ВІДМІЧЕНО" not in plain  # the assistant mode doesn't care
    user.concierge = True
    marked = asyncio.run(user.executor.execute("knowledge_lookup", {"query": "Олег"}, ctx)).message
    lines = {line.split(" — ")[0]: line for line in marked.splitlines() if line.startswith("[")}
    assert "ВЖЕ ВІДМІЧЕНО о" in lines["[Гості] Олег Петренко"] and "ВЖЕ" not in lines["[Гості] Олег Гнатюк"]


def test_checkins_list_and_reset(client):
    checkins.store.add("Олег Петренко")
    assert [c["full_name"] for c in client.get("/api/concierge/checkins").json()["checkins"]] == ["Олег Петренко"]
    assert client.delete("/api/concierge/checkins").json() == {"ok": True, "removed": 1}
    assert client.get("/api/concierge/checkins").json()["checkins"] == []


def test_the_prompts_forbid_skipping_the_check_and_inventing():
    conc = web._session_config(web.users.get("conc-g-0123456789abcdef"), concierge=True)
    assert "Never invent a place" in conc["instructions"]
    backend = conc["delegation"]["responses"]["instructions"]
    assert "never enough" in backend and "ВЖЕ ВІДМІЧЕНО" in backend and "never invent places" in backend
