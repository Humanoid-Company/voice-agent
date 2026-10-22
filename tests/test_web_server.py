"""Hosted web backend: per-browser isolation, access code, voices, calls."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server.app as web
from tools.executor import ToolExecutionContext

ALICE = "alice-browser-0123456789"
BOB = "bob-browser-0123456789ab"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    return TestClient(web.app)


def test_health_and_config(client):
    health = client.get("/healthz").json()
    assert health["ok"] is True and isinstance(health["web_search"], bool)
    assert client.get("/api/config").json() == {"login_required": False, "invite_required": False}


def test_with_a_team_code_only_a_login_gets_in(client, monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "team-secret")
    assert client.get("/api/me", headers={"X-Client-Id": ALICE}).status_code == 401
    # The old code alone no longer opens the page: it is the registration code now.
    assert client.get("/api/me", headers={"X-Client-Id": ALICE, "X-Access-Code": "team-secret"}).status_code == 401
    token = client.post("/api/auth/register", json={"email": "a@team.ua", "password": "password1",
                                                    "invite_code": "team-secret"}).json()["token"]
    ok = client.get("/api/me", headers={"X-Client-Id": ALICE, "Authorization": "Bearer " + token})
    assert ok.status_code == 200


def test_client_id_is_validated(client):
    assert client.get("/api/me", headers={"X-Client-Id": "short"}).status_code == 400


def test_each_browser_has_its_own_conversation_and_voice(client):
    gina, hal = "gina-browser-0123456789ab", "hal-browser-0123456789abc"
    web.users.get(gina).conversation.add("user", "секрет Джини")
    client.post("/api/voice", json={"voice": "stone"}, headers={"X-Client-Id": gina})
    assert len(web.users.get(hal).conversation) == 0 and web.users.get(hal).voice != "stone"
    assert client.get("/api/me", headers={"X-Client-Id": gina}).json()["voice"] == "stone"


def test_session_tools_run_per_browser():
    user = web.users.get("ivy-browser-0123456789abc")
    result = asyncio.run(
        user.executor.execute("set_assistant_name", {"name": "Ліна"}, ToolExecutionContext(session_id="s"))
    )
    assert result.ok and user.assistant_name == "Ліна"
    assert web.users.get("jay-browser-0123456789abc").assistant_name is None


class _FakeSideband:
    """Stands in for client.live.sideband.connect(): stays open until session.close()."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()
        self.close_calls = 0
        outer = self

        class _Session:
            async def close(self) -> None:
                outer.close_calls += 1
                outer.closed.set()

        self.session = _Session()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.closed.wait()
        raise StopAsyncIteration


class _FakeLiveClient:
    def __init__(self) -> None:
        self.connections: list[_FakeSideband] = []
        outer = self

        class _Sideband:
            def connect(self, *, session_id: str) -> _FakeSideband:
                conn = _FakeSideband()
                outer.connections.append(conn)
                return conn

        class _Live:
            sideband = _Sideband()

        self.live = _Live()


def test_forgotten_call_is_closed_after_the_time_limit():
    from server.live_bridge import SidebandToolBridge

    fake = _FakeLiveClient()
    closed: list[object] = []
    bridge = SidebandToolBridge(
        client=fake,  # type: ignore[arg-type]
        session_id="sess-1",
        executor=None,  # type: ignore[arg-type]
        on_closed=closed.append,
        max_duration_s=0.05,
    )
    asyncio.run(asyncio.wait_for(bridge.run(), timeout=2))
    assert fake.connections[0].close_calls == 1
    assert closed == [bridge]
    assert not bridge.is_open


def test_new_call_closes_the_previous_one_of_the_same_browser(client, monkeypatch):
    class _Old:
        closed = False

        async def close(self) -> None:
            _Old.closed = True

    class _Created:
        class session:  # noqa: N801
            id = "sess-new"

        class transport:  # noqa: N801
            sdp = "answer"

    async def fake_create(**_kwargs):
        return _Created

    started: list[object] = []
    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", lambda self: asyncio.sleep(0, started.append(self)))
    user = web.users.get(ALICE)
    old = _Old()
    user.bridges.add(old)

    res = client.post("/api/session", json={"sdp": "offer"}, headers={"X-Client-Id": ALICE})
    assert res.status_code == 200
    assert res.json()["sdp"] == "answer"
    assert _Old.closed
    user.bridges.discard(old)
    assert [b.session_id for b in user.bridges] == ["sess-new"]
    user.bridges.clear()


def test_keeps_itself_awake_only_when_deployed(monkeypatch):
    import time

    import httpx

    pinged: list[str] = []

    async def fake_get(self, url, **_kwargs):
        pinged.append(url)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(web, "KEEP_AWAKE_S", 0.05)

    monkeypatch.setattr(web, "PUBLIC_BACKEND_URL", "http://localhost:8000")
    with TestClient(web.app):
        time.sleep(0.2)
    assert pinged == []

    monkeypatch.setattr(web, "PUBLIC_BACKEND_URL", "https://backend.example")
    with TestClient(web.app):
        time.sleep(0.3)
    assert pinged and set(pinged) == {"https://backend.example/healthz"}


def test_voice_picker_lists_live_voices_and_saves_choice(client):
    data = client.get("/api/voices").json()
    ids = [v["id"] for v in data["voices"]]
    assert {"willow", "bossa", "marin", "lina", "meridian", "ripple", "stone", "blunt"} == set(ids)
    assert data["default"] == "willow"  # Єва; OPENAI_LIVE_VOICE=gleam is no character any more
    assert any(v["feminine"] for v in data["voices"]) and not all(v["feminine"] for v in data["voices"])
    assert all(v["label"] and v["description"] for v in data["voices"])

    headers = {"X-Client-Id": ALICE}
    assert client.get("/api/me", headers=headers).json()["voice"] is None
    assert client.post("/api/voice", json={"voice": "willow"}, headers=headers).json()["voice"] == "willow"
    assert client.get("/api/me", headers=headers).json()["voice"] == "willow"
    assert client.post("/api/voice", json={"voice": "nope"}, headers=headers).status_code == 400

    config = web._session_config(web.users.get(ALICE))
    assert config["audio"]["output"]["voice"] == "willow"
    assert "Your character — Єва" in config["instructions"] and "Your name is Єва" in config["instructions"]
    assert "feminine grammatical gender" in config["instructions"]


def test_voice_tool_accepts_only_picker_voices():
    user = web.users.get(BOB)
    ctx = ToolExecutionContext(session_id="s")
    bad = asyncio.run(user.executor.execute("change_voice", {"voice": "echo"}, ctx))
    assert not bad.ok and user.voice is None
    ok = asyncio.run(user.executor.execute("change_voice", {"voice": "bossa"}, ctx))
    assert ok.ok and user.voice == "bossa"


def test_tolik_is_a_character_on_cedar(client):
    """Толік, the courtyard hooligan: Кедр's voice — apart in the picker, only on request, and never at the
    concierge's door."""
    from voice.options import voice_request_target as target

    tolik = next(v for v in client.get("/api/voices").json()["voices"] if v["id"] == "blunt")
    assert tolik["special"] and tolik["sample"] == "cedar" and tolik["label"] == "Толік"
    user = web.users.get("blunt-0123456789abcdef")
    assert client.post("/api/voice", json={"voice": "blunt"}, headers={"X-Client-Id": "blunt-0123456789abcdef"}).status_code == 200
    config = web._session_config(user)
    assert config["audio"]["output"]["voice"] == "cedar"  # the API gets the preset
    assert "You are Толік" in config["instructions"] and "Your name is Толік" in config["instructions"]
    assert "masculine grammatical gender" in config["instructions"]
    # The calmer version: old Ukrainian curses and names, real mat at most once in about half of the replies.
    assert "трясця твоїй матері" in config["instructions"] and "at most once in a reply" in config["instructions"]
    # Prod: a rant, then a calm helpful answer; «Вибач, я справді занадто складно пояснив»; «Да, я здесь».
    assert "never a normal assistant's tone" in config["instructions"] and "Never apologise" in config["instructions"]
    assert "that is the game, not distress" in config["instructions"] and "never a word of Russian" in config["instructions"]
    assert "never death, graves" in config["instructions"]
    # v5's friendly lines are gone for him (they won: «Та нормально, з тобою от балакаю. А в тебе як?»).
    assert "з тобою от балакаю" not in config["instructions"] and "warm and friendly" not in config["instructions"]
    assert "end with one short, specific question" not in config["instructions"]
    assert "VOICE CHARACTER: the voice is Толік" in config["delegation"]["responses"]["instructions"]
    conc = web._session_config(user, concierge=True)
    assert conc["audio"]["output"]["voice"] == "cedar" and "You are Толік" not in conc["instructions"]
    assert "VOICE CHARACTER" not in conc["delegation"]["responses"]["instructions"]
    # Others keep their own character and none of his.
    plain = web._session_config(web.users.get("blunt-plain-0123456789a"))
    assert "з тобою от балакаю" in plain["instructions"]  # Єва, the default: a warm one keeps v5 as it is
    assert "You are Толік" not in plain["instructions"] and "Толік" not in plain["delegation"]["responses"]["instructions"]

    assert target("зміни голос на Толіка", "willow") == "blunt" and target("зміни голос на хуліган", "willow") == "blunt"
    for current in ("willow", "meridian", "ripple", "stone"):  # never by «чоловічий» / «інший»
        assert target("зміни голос на чоловічий", current) != "blunt"
        assert target("зміни голос на інший", current) != "blunt"
    other = web.users.get("blunt-tool-0123456789ab")
    ok = asyncio.run(other.executor.execute("change_voice", {"voice": "blunt"},
                                            ToolExecutionContext(session_id="s", user_utterances=["постав Толіка"])))
    assert ok.ok and other.voice == "blunt"


def test_each_character_answers_to_its_own_name(client):
    """«хай кожен персонаж відкликається на своє імя»: the page wakes and pauses each by its name."""
    voices = {v["id"]: v for v in client.get("/api/voices").json()["voices"]}
    assert voices["willow"]["label"] == "Єва" and "єва" in voices["willow"]["wake"]
    assert {"толік", "толя"} <= set(voices["blunt"]["wake"]) and "мілано" in voices["bossa"]["wake"]
    assert all(v["wake"] for v in voices.values())
    tolik = web.users.get("wake-tolik-0123456789a")
    tolik.voice = "blunt"
    assert "«Толік, скажи»" in web._session_config(tolik)["instructions"]


def test_characters_have_their_tone_and_limits():
    """Мілана flirts lightly, Ліна is gloomy only in manner, Степан Петрович is on «ви»; the gone plain voices
    fall back to Єва."""
    def cfg(voice):
        user = web.users.get(f"char-{voice}-0123456789abc"[:24])
        user.voice = voice
        return web._session_config(user)

    milana, lina, stepan = cfg("bossa"), cfg("lina"), cfg("stone")
    assert "nothing explicit or intimate" in milana["instructions"] and "з тобою от балакаю" in milana["instructions"]
    assert lina["audio"]["output"]["voice"] == "willow" and "never death" in lina["instructions"]
    assert "warm and friendly" not in lina["instructions"]  # her own tone
    assert "«ви»" in stepan["instructions"] and "warm and friendly" not in stepan["instructions"]
    assert "VOICE CHARACTER: the voice is Ліна" in lina["delegation"]["responses"]["instructions"]
    gone = cfg("gleam")  # picked before the characters
    assert gone["audio"]["output"]["voice"] == "willow" and "Your name is Єва" in gone["instructions"]


def test_blunt_prompt_takes_the_users_additions(monkeypatch):
    """voice/blunt_prompt.py: MY_ADDITIONS go to the Live prompt and to the backend; empty — nothing extra."""
    from voice import blunt_prompt

    assert "More from the user" not in blunt_prompt.character()
    monkeypatch.setattr(blunt_prompt, "MY_ADDITIONS", "\nКажи «братан» замість «друже».\n")
    assert blunt_prompt.character().endswith("(these win over the above):\nКажи «братан» замість «друже».")
    assert "Кажи «братан»" in blunt_prompt.backend_note() and blunt_prompt.backend_note().endswith("\n")


def test_voice_change_keeps_the_conversation(client):
    """Conversation → voice change by voice → the new session starts with everything said before."""
    from server.live_bridge import SidebandToolBridge

    user = web.users.get("carol-browser-0123456789")
    bridge = SidebandToolBridge(
        client=None, session_id="s1", executor=user.executor, conversation=user.conversation
    )
    user.bridges.add(bridge)

    async def talk():
        for etype, delta in [
            ("session.input_transcript.delta", "Мене звати Остап, "),
            ("session.input_transcript.delta", "я п'ю каву без цукру."),
            ("session.output_transcript.delta", "Приємно, Остапе! Запам'ятала."),
            ("session.input_transcript.delta", "Зміни голос на чоловічий."),
        ]:
            await bridge._handle_event({"type": etype, "delta": delta})

    asyncio.run(talk())
    result = asyncio.run(
        user.executor.execute("change_voice", {"voice": "meridian"}, ToolExecutionContext(session_id="s1"))
    )
    assert result.ok and user.voice == "meridian"
    assert bridge.restart_for_voice  # the page reconnects with the new voice…
    assert client.get("/api/me", headers={"X-Client-Id": "carol-browser-0123456789"}).json()["reconnect"] is True

    config = web._session_config(user)  # …and the new session gets the whole conversation
    assert config["audio"]["output"]["voice"] == "meridian"
    texts = [(item["role"], item["content"][0]["text"]) for item in config["input"]]
    assert texts == [
        ("user", "Мене звати Остап, я п'ю каву без цукру."),
        ("assistant", "Приємно, Остапе! Запам'ятала."),
        ("user", "Зміни голос на чоловічий."),
    ]
    assert "you are always Єва" in config["instructions"]


def test_speed_and_style_are_saved_and_returned_for_the_page(client):
    """The page sends the returned instruction itself once Єва is quiet; the server only stores."""
    user = web.users.get("dave-browser-0123456789a")
    sent = []

    class FakeBridge:
        async def append_instruction(self, text):
            sent.append(text)

    user.bridges.add(FakeBridge())
    headers = {"X-Client-Id": "dave-browser-0123456789a"}
    body = client.post("/api/voice", json={"speed": "slow", "style": "calm"}, headers=headers).json()
    assert body["speed"] == "slow" and body["style"] == "calm"
    assert "slower" in body["instruction"] and "calm" in body["instruction"] and "not a message" in body["instruction"]
    assert sent == []  # not pushed mid-answer by the server
    assert "slower" in web._session_config(user)["instructions"]  # a new session starts with them
    assert client.post("/api/voice", json={"speed": "slow"}, headers=headers).json()["instruction"] == ""
    assert client.post("/api/voice", json={"speed": "warp"}, headers=headers).status_code == 400
    user.bridges.clear()


def test_new_conversation_clears_history(client):
    headers = {"X-Client-Id": "erin-browser-0123456789a"}
    user = web.users.get("erin-browser-0123456789a")
    user.conversation.add("user", "секрет")
    assert client.delete("/api/conversation", headers=headers).json()["ok"]
    assert len(user.conversation) == 0 and "input" not in web._session_config(user)


def test_voice_change_closes_the_call_without_a_spoken_confirmation():
    """change_voice: no «перемикаю…» — the old session is closed, the new voice carries on."""
    from server.live_bridge import SidebandToolBridge

    user = web.users.get("frank-browser-0123456789")
    sent = []

    class Conn:
        class session:
            @staticmethod
            async def close():
                sent.append("close")

        class response:
            @staticmethod
            async def create(**kw):
                sent.append("response.create")

            class item:
                @staticmethod
                async def create(**kw):
                    sent.append("item.create")

    bridge = SidebandToolBridge(client=None, session_id="s2", executor=user.executor)
    bridge._connection = Conn()
    user.bridges.add(bridge)
    asyncio.run(
        bridge._execute_and_continue(
            name="change_voice", arguments={"voice": "stone"}, call_id="c1", delegation_id=None, key=""
        )
    )
    assert user.voice == "stone" and user.reconnect_pending
    assert sent == ["close"]  # no tool output / response.create → she says nothing in the old voice
    user.bridges.clear()


def test_voice_request_detection_for_the_nudge():
    from voice.options import VOICE_REQUEST_RE as voice_request

    for text in ["Єва, зміни голос на чоловічий", "постав інший голос", "поміняй, будь ласка, голос",
                 "давай голос на жіночий", "Голос на спокійніший"]:
        assert voice_request.search(text), text
    for text in ["голос у тебе гарний", "зміни зустріч на завтра", "зроби нагадування про голосування"]:
        assert not voice_request.search(text), text


def test_voice_request_picks_the_voice():
    from voice.options import voice_request_target as target

    assert target("Єва, зміни голос на чоловічий", "willow") == "meridian"  # Тарас first
    assert target("зміни голос на чоловічий", "meridian") == "ripple"  # already a man: the next one
    assert target("постав жіночий голос", "stone") == "willow"
    assert target("зміни голос на інший", "willow") == "bossa"
    assert target("зроби голос спокійніший", "bossa") == "willow"
    assert target("зроби голос спокійніший", "stone") == "meridian"
    assert target("постав голос Мілани", "willow") == "bossa"
    assert target("переключи голос на Степана", "willow") == "stone"
    assert target("зміни голос на Бабу Галю", "willow") == "marin"
    assert target("зміни голос на Ліну", "willow") == "lina"
    assert target("що в мене завтра?", "willow") is None
    assert target("Єва, змини голос на чоловічий", "willow") == "meridian"  # STT spelling
    assert target("Єва, зміни голос", "willow") == "bossa"  # bare: just another one
    assert target("Єва, змини голос на Нагадай, як мене звати", "willow") is None  # cut off: unclear


def test_bridge_switches_voice_from_the_transcript(monkeypatch):
    """The model said only «Секунду»: the server switches the voice from what the user said."""
    import server.live_bridge as bridge_mod
    from server.live_bridge import SidebandToolBridge

    monkeypatch.setattr(bridge_mod, "_UTTERANCE_PAUSE_S", 0.01)
    monkeypatch.setattr(bridge_mod, "_PAGE_FIRST_S", 0)
    user = web.users.get("gina-browser-0123456789a")
    user.voice = "gleam"
    closed = []

    class Conn:
        class session:
            @staticmethod
            async def close():
                closed.append(True)

    bridge = SidebandToolBridge(
        client=None, session_id="s3", executor=user.executor, on_voice_request=user.switch_voice_by_request
    )
    bridge._connection = Conn()

    async def talk():
        for delta in ["Єва, зміни голос на ", "чоловічий."]:
            await bridge._handle_event({"type": "session.input_transcript.delta", "delta": delta})
        await asyncio.sleep(0.1)

    asyncio.run(talk())
    assert user.voice == "meridian" and user.reconnect_pending and closed == [True]


def test_voice_request_endpoint_and_dedup(client):
    headers = {"X-Client-Id": "hank-browser-0123456789a"}
    user = web.users.get("hank-browser-0123456789a")
    user.voice = "gleam"
    first = client.post("/api/voice-request", json={"text": "Єва, зміни голос на чоловічий"}, headers=headers).json()
    assert first == {"switched": True, "voice": "meridian"}
    # the same request arriving again (Live transcript / the model): no second switch
    again = client.post("/api/voice-request", json={"text": "Єва, зміни голос на чоловічий"}, headers=headers).json()
    assert again == {"switched": False, "voice": "meridian"}
    result = asyncio.run(
        user.executor.execute("change_voice", {"voice": "ripple"}, ToolExecutionContext(session_id="s"))
    )
    assert result.ok and user.voice == "meridian"
    other = client.post("/api/voice-request", json={"text": "котра година?"}, headers=headers).json()
    assert other["switched"] is False


def test_speed_change_waits_until_she_finishes_speaking(monkeypatch):
    """An instruction appended mid-answer derails it: it is sent once she is quiet, latest only."""
    from server import live_bridge
    from server.live_bridge import SidebandToolBridge

    monkeypatch.setattr(live_bridge, "_SPEECH_CHARS_PER_S", 1e6)  # the text's quiet only (audio: test_work_progress)

    sent: list[str] = []

    class Conn:
        class session:
            class instructions:
                @staticmethod
                async def append(content, **kw):
                    sent.append(content)

    async def scenario():
        bridge = SidebandToolBridge(client=None, session_id="s4", executor=None)
        bridge._connection = Conn()
        for word in ["Жила-була ", "маленька "]:
            await bridge._handle_event({"type": "session.output_transcript.delta", "delta": word})
        await bridge.append_instruction("slow", quiet_s=0.3)
        await asyncio.sleep(0.1)
        await bridge.append_instruction("calm", quiet_s=0.3)  # replaces «slow»
        await bridge._handle_event({"type": "session.output_transcript.delta", "delta": "дівчинка."})
        await asyncio.sleep(0.15)
        assert sent == []  # still talking
        await asyncio.sleep(0.5)
        assert sent == ["calm"]

    asyncio.run(scenario())


def test_model_cannot_change_the_voice_nobody_asked_for():
    """Prod: «Розкажи щось» → the model called change_voice on its own and the voice switched."""
    user = web.users.get("voice-guard-0123456789")
    user.voice, user.voice_switched_at = "gleam", 0.0
    unasked = ToolExecutionContext(session_id="s", user_utterances=["Як твої справи", "Розкажи щось"])
    refused = asyncio.run(user.executor.execute("change_voice", {"voice": "bossa"}, unasked))
    assert not refused.ok and user.voice == "gleam" and not user.reconnect_pending
    asked = ToolExecutionContext(session_id="s", user_utterances=["Розкажи щось", "Зроби чоловічий голос"])
    done = asyncio.run(user.executor.execute("change_voice", {"voice": "meridian"}, asked))
    assert done.ok and user.voice == "meridian" and user.reconnect_pending
    assert "попросила модель" in user.voice_switch_note


def test_the_language_changes_only_when_asked():
    """Prod: «Ну так, расскажи, как ИИ работает» → change_language ru by the model; «Українською говори» never
    switched it back, and every next call opened with «speak exclusively in Russian»."""
    user = web.users.get("lang-guard-0123456789a")
    said = lambda *u: ToolExecutionContext(session_id="s", user_utterances=list(u))  # noqa: E731
    refused = asyncio.run(user.executor.execute("change_language", {"language": "ru"}, said("Алло", "расскажи, как ИИ работает")))
    assert not refused.ok and user.language == "uk" and "не змінюй" in refused.message
    assert asyncio.run(user.executor.execute("change_language", {"language": "en"}, said("What's up"))).ok is False
    asked = asyncio.run(user.executor.execute("change_language", {"language": "en"}, said("Давай англійською")))
    assert asked.ok and user.language == "en"
    back = asyncio.run(user.executor.execute("change_language", {"language": "uk"}, said("ok")))  # back home: always
    assert back.ok and user.language == "uk"
    assert asyncio.run(user.executor.execute("change_language", {"language": "ru"}, said("говори по-русски"))).ok


@pytest.mark.parametrize(
    "utterances, expected",
    [
        (None, True),
        (["Розкажи щось"], False),
        (["Як справи", "Розкажи щось цікаве"], False),
        (["Давай іншим голосом"], True),
        (["Хочу Мілану"], True),
        (["Толік, розкажи щось"], False),  # they call each character by name: not a wish to switch
        (["Мілано, як справи?"], False),
        (["Поклич Степана Петровича"], True),
        (["зроби чоловічий"], True),
        (["говори спокійніше"], True),
    ],
)
def test_asked_for_voice_change(utterances, expected):
    from voice.options import asked_for_voice_change

    assert asked_for_voice_change(utterances) is expected



def test_wake_request_goes_into_the_new_call_as_the_users_message(monkeypatch):
    """Prod: «Єва, скажи, яка в мене завтра подія» → she said «Яка в мене завтра подія?». Quoted in a
    commentary GPT-Live sometimes reads it back (1 of 3); as the user's message in the history, 0 of 3."""
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    captured = {}

    async def fake_create(*, session, transport):
        captured["input"] = session.get("input", [])
        return SimpleNamespace(session=SimpleNamespace(id="sess-wake"), transport=SimpleNamespace(sdp="answer"))

    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", lambda self: asyncio.sleep(0))
    user = web.users.get("wake-req-0123456789ab")
    user.conversation.clear()
    res = TestClient(web.app).post(
        "/api/session",
        headers={"X-Client-Id": "wake-req-0123456789ab"},
        json={"sdp": "offer", "user_text": "Єва, скажи, яка в мене завтра подія"},
    )
    assert res.status_code == 200
    last = captured["input"][-1]
    assert last["role"] == "user" and "яка в мене завтра подія" in last["content"][0]["text"]


def test_voice_switch_keeps_the_old_call_until_the_page_swaps(monkeypatch):
    """keep_old: the new call in another voice opens beside the talking one; the page closes the old
    call when it swaps — and the server does it anyway if both are still open much later."""
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    monkeypatch.setattr(web, "KEEP_OLD_MAX_S", 0.05)

    class _Old:
        session_id = "sess-old"
        is_open = True
        closed = False

        async def close(self) -> None:
            _Old.closed = True

    async def fake_create(**_kwargs):
        return SimpleNamespace(session=SimpleNamespace(id="sess-standby"), transport=SimpleNamespace(sdp="answer"))

    async def fake_run(self):
        self._connection = object()  # stays open
        await asyncio.sleep(1)

    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", fake_run)
    client_id = "keep-old-0123456789ab"
    user = web.users.get(client_id)
    user.bridges.clear()
    old = _Old()
    user.bridges.add(old)
    with TestClient(web.app) as client:
        res = client.post("/api/session", json={"sdp": "offer", "keep_old": True}, headers={"X-Client-Id": client_id})
        assert res.status_code == 200
        assert not _Old.closed  # still talking while the new call comes up
        import time

        time.sleep(0.3)
    assert _Old.closed  # the page never swapped: closed by the server
    user.bridges.clear()


def test_prompt_v5_is_the_default_and_v4_v3_only_when_the_page_asks(monkeypatch):
    """v5 (v4 condensed) is the default; v1 and v2 are gone from the web (desktop only): a page that
    still asks for them gets v5."""
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    sent: list[str] = []

    async def fake_create(*, session, transport):
        sent.append(session["instructions"])
        return SimpleNamespace(session=SimpleNamespace(id=f"sess-p{len(sent)}"), transport=SimpleNamespace(sdp="answer"))

    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", lambda self: asyncio.sleep(0))
    headers = {"X-Client-Id": "prompt-var-0123456789ab"}
    client = TestClient(web.app)
    bodies = ({"sdp": "o", "prompt": "v1"}, {"sdp": "o", "prompt": "v2"}, {"sdp": "o", "prompt": "v3"},
              {"sdp": "o", "prompt": "v4"}, {"sdp": "o"}, {"sdp": "o", "prompt": "nonsense"})
    for body in bodies:
        assert client.post("/api/session", headers=headers, json=body).status_code == 200
    v1, v2, v3, v4, v5, fallback = sent
    assert "Being a person" in v3 and "you are talking, not reading" in v3 and "Answering" not in v3
    assert "Answering" in v4 and "Being a person" in v4
    assert "How a turn goes" in v5 and "Answering" not in v5 and v5 == fallback == v1 == v2
    for text in (v3, v4):  # the shared parts stay in both
        assert "Voice changes:" in text and "Interruption policy" in text
        assert "You are a voice assistant first" in text and "зараз прикину" in text
    web.users.get(headers["X-Client-Id"]).bridges.clear()


def test_language_change_reaches_the_running_call():
    """The call was started with «Speak exclusively in українською»: without a note in the call itself
    she went back to Ukrainian a few replies after switching to English."""

    class _Bridge:
        def __init__(self) -> None:
            self.notes: list[str] = []

        async def append_instruction(self, text: str, **_) -> None:
            self.notes.append(text)

    async def scenario():
        user = web.users.get("language-switch-0123456789")
        bridge = _Bridge()
        user.bridges.add(bridge)
        result = await user.executor.execute("change_language", {"language": "en"}, ToolExecutionContext())
        await asyncio.sleep(0)  # the note goes out in the background
        await asyncio.sleep(0)
        user.bridges.clear()
        return user, bridge, result

    user, bridge, result = asyncio.run(scenario())
    assert result.ok and user.language == "en"
    assert bridge.notes == ["From now on speak exclusively in англійською."]
    assert "не починай заново" in result.message  # she goes on, no «Sure… Okay… Sure…»


def test_diag_keeps_numbers_only():
    """/api/diag: the page's call numbers (gate levels, barge-in timings, unanswered turns) — any text
    is dropped, an oversized report refused; GET reads the latest."""
    headers = {"X-Client-Id": "diag-0123456789abcdef"}
    client = TestClient(web.app)
    body = {"gate": {"echo": 0.12, "mic": "speakers"}, "said": "Привіт, Єва, ось мій секрет", "barge": {"muteMs": [800, 1500]}}
    assert client.post("/api/diag", headers=headers, json=body).json() == {"ok": True}
    assert client.post("/api/diag", headers=headers, content=b"x" * 5000).status_code == 413
    last = client.get("/api/diag", params={"n": 1}).json()[-1]
    assert last["data"] == {"gate": {"echo": 0.12, "mic": "speakers"}, "said": None, "barge": {"muteMs": [800, 1500]}}


def test_concierge_mode_and_confirm_guest(client):
    """«Консьєрж»: her rules and the backend's check protocol only in that mode; confirm_guest marks the guest,
    «Наступний гість» clears it, and the page sees it in /api/me."""
    from server import knowledge
    from server.knowledge_import import parse_file

    knowledge.store.create_set("Гості", parse_file("g.csv", "ПІБ;Посада\nОлег Петренко;бекенд-розробник\n".encode()).items)
    user = web.users.get("conc-browser-0123456789ab")
    plain, conc = web._session_config(user), web._session_config(user, concierge=True)
    assert "the concierge at the entrance" in conc["instructions"] and "concierge" not in plain["instructions"]
    names = lambda c: [t["name"] for t in c["delegation"]["responses"]["tools"]]  # noqa: E731
    assert "confirm_guest" in names(conc) and "confirm_guest" not in names(plain)
    assert "CONCIERGE" in conc["delegation"]["responses"]["instructions"]
    ok = asyncio.run(user.executor.execute(
        "confirm_guest", {"full_name": "Олег Петренко", "verified": True, "guest_answer": "бекенд-розробник"},
        ToolExecutionContext(session_id="s", user_utterances=["Олег", "Петренко", "Я бекенд-розробник"])))
    assert ok.ok and "Олег Петренко" in ok.message
    me = client.get("/api/me", headers={"X-Client-Id": "conc-browser-0123456789ab"}).json()
    assert me["guest"]["name"] == "Олег Петренко" and me["guest"]["verified"] is True
    client.delete("/api/conversation", headers={"X-Client-Id": "conc-browser-0123456789ab"})  # «Наступний гість»
    assert client.get("/api/me", headers={"X-Client-Id": "conc-browser-0123456789ab"}).json()["guest"] is None
