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


@pytest.fixture(autouse=True)
def guest_list():
    """The team's list the concierge checks against (the stores are temporary per test, conftest.py)."""
    knowledge.store.create_set("Гості", parse_file("g.csv", CSV).items)


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
    assert "never enough" in backend and "already registered" in backend and "never invent places" in backend
    assert "find_guest" in backend and "who invited them" in backend and "who invited them" in conc["instructions"]
    # Prod: «Так, тепер маленька перевірка… яку вуличну їжу ви любите?» — a question of her own, copied from
    # an example, before the backend said there are eleven Владислави.
    assert "never ask a check question of your own" in conc["instructions"]
    # Prod: «Віктор» (13 on the list) → «Вікторе, вуаля. Ви в списку. Проходьте» with no surname asked.
    assert "a first name alone never is" in conc["instructions"]
    assert "a first name alone never is" in web._session_config(web.users.get("conc-g2-0123456789abcd"), concierge=True)["instructions"]
    assert "яку їжу" not in conc["instructions"] and "питання на мільйон" not in conc["instructions"]


def test_its_own_prompt_no_small_talk_and_only_the_tools_it_needs():
    """v5 made her a friend for a chat («Ну що, як твої справи?»); the concierge has its own prompt."""
    user = web.users.get("conc-h-0123456789abcdef")
    conc = web._session_config(user, concierge=True)
    assert "No small talk" in conc["instructions"] and "«ти»" not in conc["instructions"]
    assert "personal voice assistant" not in conc["instructions"]  # not v5
    tools = {t["name"] for t in conc["delegation"]["responses"]["tools"]}
    assert tools == {"find_guest", "confirm_guest", "knowledge_lookup", "end_conversation", "change_language"}  # no web, no camera
    ok = confirm(user, ["Петренко", "кіт Бублик"], "кіт Бублик")
    assert "Проходьте" in ok.message and "як минає" not in ok.message


def test_the_check_question_is_about_a_random_fact_stable_within_a_visit():
    user = web.users.get("conc-i-0123456789abcdef")
    user.concierge = True
    ctx = ToolExecutionContext(session_id="s")

    def asked():
        message = asyncio.run(user.executor.execute("knowledge_lookup", {"query": "Петренко"}, ctx)).message
        line = next(x for x in message.splitlines() if x.startswith("[Гості] Олег Петренко"))
        return line.rsplit("[перевірка: ", 1)[1].rstrip("]")

    first = asked()
    assert first in ("Посада", "Тварини") and asked() == first  # the same while this guest talks
    picks = set()
    for _ in range(20):  # next guests: «Наступний гість» starts a new conversation
        user.new_conversation()
        picks.add(asked())
    assert picks == {"Посада", "Тварини"}


def test_thinking_aloud_can_be_off():
    """«Так, Тарас у нас є, зараз звірю…» told a stranger the name is on the list before any check."""
    from server.live_bridge import SidebandToolBridge

    async def run():
        user = web.users.get("conc-j-0123456789abcdef")
        voice = SidebandToolBridge(client=None, session_id="v", executor=user.executor)
        host = SidebandToolBridge(client=None, session_id="c", executor=user.executor, think_aloud="off")
        voice._work_started("k")
        host._work_started("k")
        assert "k" in voice._mumbles and "k" not in host._mumbles
        voice._mumbles["k"].cancel()

    asyncio.run(run())


def find(user, name, surname=""):
    return asyncio.run(user.executor.execute("find_guest", {"name": name, "surname": surname},
                                             ToolExecutionContext(session_id="s")))


def test_find_guest_short_forms_surnames_and_people_not_on_the_list():
    knowledge.store.create_set("Ще", parse_file("h.csv", "ПІБ;Хобі\nКатерина Шаповал;книжки\nДмитро Кравець;гольф\n".encode()).items)
    user = web.users.get("conc-k-0123456789abcdef")
    katya = find(user, "Катя")
    assert katya.status == "found" and "Катерина Шаповал" in katya.message and "коротка форма" in katya.message
    assert find(user, "Діма").status == "found" and find(user, "Каті").status == "found"  # a case ending too
    assert find(user, "Олег").status == "ask_surname" and "нікого не називай" in find(user, "Олег").message
    assert find(user, "Алєг", "Гнатюк").data["name"] == "Олег Гнатюк"  # how a recogniser may write Олег
    for stranger in ("Петро", "Тімлід", "Саша"):  # «Петро» is not «Петренко»
        res = find(user, stranger)
        assert res.status == "not_found" and "хто запрошував" in res.message
    # Prod: «Владислав» → «Сеньків», nobody with that surname among the Владислави — not «several people
    # with the name and surname, say your full name» over and over.
    res = find(user, "Олег", "Сеньків")
    assert res.status == "not_found" and "Олег Сеньків" in res.message
    assert find(user, "Олег", "Петренка").data["name"] == "Олег Петренко"  # a case ending on the surname still finds him
    # Prod: «Артем Шаповалов» was let in as Артем Шаповал — «-ов» is another surname, not a case ending.
    knowledge.store.create_set("Ще 2", [{"title": "Артем Шаповал", "fields": {"Хобі": "шахи"}}])
    assert find(user, "Артем", "Шаповалов").status == "not_found"
    assert find(user, "Артем", "Шаповалові").data["name"] == "Артем Шаповал"
    knowledge.store.create_set("Ще 3", [{"title": "Павло Шевчук"}, {"title": "Павло Шевченко"}])
    assert find(user, "Павло", "Шевчука").data["name"] == "Павло Шевчук"  # not «which one?»
    assert find(user, "Павло", "Шевченка").data["name"] == "Павло Шевченко"


def test_find_guest_says_already_registered_without_a_check_question():
    checkins.store.add("Олег Петренко")
    res = find(web.users.get("conc-l-0123456789abcdef"), "Олег", "Петренко")
    assert res.status == "already_checked_in" and "Без перевірочного питання" in res.message


def test_find_guest_names_the_fact_to_ask_about():
    res = find(web.users.get("conc-m-0123456789abcdef"), "Олег", "Петренко")
    assert res.status == "found" and ("про «Посада»" in res.message or "про «Тварини»" in res.message)


def test_nobody_off_the_list_can_be_confirmed():
    res = confirm(web.users.get("conc-n-0123456789abcdef"), ["Петро Іваненко", "я бухгалтер"], "бухгалтер",
                  name="Петро Іваненко")
    assert res.status == "not_on_list" and checkins.store.all() == []


def test_the_concierge_says_one_neutral_line_while_a_check_runs(monkeypatch):
    """With no line at all a slow check sounded like she had hung («Секунду.» … silence … «Ну шо там?»)."""
    from server import live_bridge
    from server.live_bridge import SidebandToolBridge

    monkeypatch.setattr(live_bridge, "_MUMBLE_THINK_FIRST_S", 0.1)
    monkeypatch.setattr(live_bridge, "_MUMBLE_EVERY_S", 0.1)
    said = []

    async def run():
        host = SidebandToolBridge(client=None, session_id="c", executor=web.users.get("conc-o-0123456789abcdef").executor,
                                  think_aloud="neutral")
        host._connection = object()  # «open»

        async def say(text):
            said.append(text)
        host.say = say
        host._quiet_for = lambda now: 99.0
        host._work_started("k")
        await asyncio.sleep(1.0)
        host._work.pop("k", None)

    asyncio.run(run())
    assert len(said) == 1 and "Ще секундочку" in said[0] and "this exact request" not in said[0]


def test_found_guest_is_not_announced_before_the_check():
    """Prod: «О, бачу Марину Коваль» — before any check question."""
    res = find(web.users.get("conc-p-0123456789abcdef"), "Олег", "Петренко")
    assert "Не кажи, що людину знайдено" in res.message
    assert "О, бачу Марину Коваль" in web._session_config(web.users.get("conc-p-0123456789abcdef"), concierge=True)["instructions"]


def test_checkins_are_cached_and_a_new_one_shows_at_once():
    assert checkins.store.all() == []
    checkins.store.add("Олег Петренко")  # written by this server: seen at once
    assert checkins.store.find("Петренко Олег")["full_name"] == "Олег Петренко"
    assert checkins.store.reset() == 1 and checkins.store.find("Олег Петренко") is None


def test_same_surname_and_short_name_asks_the_full_name_not_the_surname_again():
    """«Саша Козак» with Олександр and Олександра Козак on the list: asking the surname again went round in circles."""
    knowledge.store.create_set("Козаки", parse_file("k.csv", "ПІБ;Хобі\nОлександр Козак;біг\nОлександра Козак;йога\n".encode()).items)
    user = web.users.get("conc-q-0123456789abcdef")
    res = find(user, "Саша", "Козак")
    assert res.status == "ask_full_name" and "повне ім'я" in res.message
    assert find(user, "Олександра", "Козак").data["name"] == "Олександра Козак"


def test_without_the_check_question_the_name_on_the_list_lets_them_in():
    """«Перевірочне питання» off on the page: find_guest lets the one person in and marks them, no question."""
    user = web.users.get("conc-n-0123456789abcdef")
    user.check_question = False
    conc = web._session_config(user, concierge=True)
    assert "There is no check question at this door" in conc["instructions"]
    assert "its one check question" not in conc["instructions"]  # the usual steps 3–5 are replaced
    assert "NO check question" in conc["delegation"]["responses"]["instructions"]
    res = find(user, "Олег", "Петренко")
    assert res.status == "ok" and "Олег Петренко" in res.message and "перевірочн" not in res.message.lower()
    assert user.guest["verified"] is True and checkins.store.find("Олег Петренко") is not None
    assert find(user, "Олег", "Петренко").status == "already_checked_in"  # once a day, as with the question
    assert find(user, "Олег").status == "ask_surname"  # two Олеги: the surname still decides
    lookup = asyncio.run(user.executor.execute("knowledge_lookup", {"query": "Гнатюк"}, ToolExecutionContext(session_id="s")))
    assert "[перевірка:" not in lookup.message
    # The model may still call confirm_guest: no answer needed then.
    ok = confirm(user, ["Олег", "Гнатюк"], "", name="Олег Гнатюк")
    assert ok.status == "ok"

    user.check_question = True  # back on: the question again
    on = web._session_config(user, concierge=True)
    assert "There is no check question" not in on["instructions"] and "NO check question" not in on["delegation"]["responses"]["instructions"]
