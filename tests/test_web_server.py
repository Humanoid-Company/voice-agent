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
    assert set(client.get("/api/config").json()) == {"access_code_required"}


def test_access_code_is_enforced(client, monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "team-secret")
    assert client.get("/api/me", headers={"X-Client-Id": ALICE}).status_code == 401
    ok = client.get("/api/me", headers={"X-Client-Id": ALICE, "X-Access-Code": "team-secret"})
    assert ok.status_code == 200


def test_client_id_is_validated(client):
    assert client.get("/api/me", headers={"X-Client-Id": "short"}).status_code == 400


def test_each_browser_has_its_own_conversation_and_voice(client):
    gina, hal = "gina-browser-0123456789ab", "hal-browser-0123456789abc"
    web.users.get(gina).conversation.add("user", "секрет Джини")
    client.post("/api/voice", json={"voice": "cedar"}, headers={"X-Client-Id": gina})
    assert len(web.users.get(hal).conversation) == 0 and web.users.get(hal).voice != "cedar"
    assert client.get("/api/me", headers={"X-Client-Id": gina}).json()["voice"] == "cedar"


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
    assert {"marin", "cedar", "gleam", "willow", "meridian"} <= set(ids) and data["default"] in ids
    assert any(v["feminine"] for v in data["voices"]) and not all(v["feminine"] for v in data["voices"])
    assert all(v["label"] and v["description"] for v in data["voices"])

    headers = {"X-Client-Id": ALICE}
    assert client.get("/api/me", headers=headers).json()["voice"] is None
    assert client.post("/api/voice", json={"voice": "willow"}, headers=headers).json()["voice"] == "willow"
    assert client.get("/api/me", headers=headers).json()["voice"] == "willow"
    assert client.post("/api/voice", json={"voice": "nope"}, headers=headers).status_code == 400

    config = web._session_config(web.users.get(ALICE))
    assert config["audio"]["output"]["voice"] == "willow"
    assert "voice preset «Віллоу»" in config["instructions"]
    assert "feminine grammatical gender" in config["instructions"]


def test_voice_tool_accepts_only_picker_voices():
    user = web.users.get(BOB)
    ctx = ToolExecutionContext(session_id="s")
    bad = asyncio.run(user.executor.execute("change_voice", {"voice": "echo"}, ctx))
    assert not bad.ok and user.voice is None
    ok = asyncio.run(user.executor.execute("change_voice", {"voice": "gleam"}, ctx))
    assert ok.ok and user.voice == "gleam"


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

    assert target("Єва, зміни голос на чоловічий", "gleam") == "meridian"
    assert target("зміни голос на чоловічий", "meridian") == "ripple"  # already a man: the next one
    assert target("постав жіночий голос", "stone") == "gleam"
    assert target("зміни голос на інший", "gleam") == "bossa"
    assert target("зроби голос спокійніший", "gleam") == "willow"
    assert target("зроби голос спокійніший", "tempo") == "stone"
    assert target("постав голос Босу", "gleam") == "bossa"
    assert target("переключи голос на Кедра", "gleam") == "cedar"
    assert target("що в мене завтра?", "gleam") is None
    assert target("Єва, змини голос на чоловічий", "gleam") == "meridian"  # STT spelling
    assert target("Єва, зміни голос", "gleam") == "bossa"  # bare: just another one
    assert target("Єва, змини голос на Нагадай, як мене звати", "gleam") is None  # cut off: unclear


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


@pytest.mark.parametrize(
    "utterances, expected",
    [
        (None, True),
        (["Розкажи щось"], False),
        (["Як справи", "Розкажи щось цікаве"], False),
        (["Давай іншим голосом"], True),
        (["Хочу Босу"], True),
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


def test_typed_line_joins_the_conversation_and_the_chat_note(client):
    """/chat: a typed message is the user's line (history, tools), and a chat call knows they type."""
    from server.live_bridge import SidebandToolBridge

    user = web.users.get("chat-browser-0123456789ab")
    bridge = SidebandToolBridge(client=None, session_id="s9", executor=user.executor, conversation=user.conversation)
    user.bridges.add(bridge)
    res = client.post("/api/conversation/text", json={"text": "  Зміни голос на чоловічий  "},
                      headers={"X-Client-Id": "chat-browser-0123456789ab"})
    assert res.json() == {"ok": True}
    assert user.conversation.to_list() == [{"role": "user", "text": "Зміни голос на чоловічий"}]
    assert bridge._user_turns[-1] == "Зміни голос на чоловічий"  # change_voice sees it as asked
    assert client.post("/api/conversation/text", json={"text": " "}, headers={"X-Client-Id": "chat-browser-0123456789ab"}).status_code == 422
    assert "Text chat" in web._session_config(user, chat=True)["instructions"]
    assert "Text chat" not in web._session_config(user)["instructions"]


def test_chat_calls_do_not_think_aloud():
    """/chat: no «thinking aloud» lines while the backend works — she read the typed question back."""
    from server.live_bridge import SidebandToolBridge

    async def run():
        voice = SidebandToolBridge(client=None, session_id="v", executor=web.users.get("tv-browser-0123456789abc").executor)
        chat = SidebandToolBridge(client=None, session_id="c", executor=web.users.get("tc-browser-0123456789abc").executor,
                                  think_aloud=False)
        voice._work_started("k")
        chat._work_started("k")
        assert "k" in voice._mumbles and "k" not in chat._mumbles
        voice._mumbles["k"].cancel()

    asyncio.run(run())


def test_concierge_mode_and_confirm_guest(client):
    """«Консьєрж»: her rules and the backend's check protocol only in that mode; confirm_guest marks the guest,
    «Наступний гість» clears it, and the page sees it in /api/me."""
    user = web.users.get("conc-browser-0123456789ab")
    plain, conc = web._session_config(user), web._session_config(user, concierge=True)
    assert "Concierge mode" in conc["instructions"] and "Concierge mode" not in plain["instructions"]
    names = lambda c: [t["name"] for t in c["delegation"]["responses"]["tools"]]  # noqa: E731
    assert "confirm_guest" in names(conc) and "confirm_guest" not in names(plain)
    assert "CONCIERGE" in conc["delegation"]["responses"]["instructions"]
    ok = asyncio.run(user.executor.execute("confirm_guest", {"full_name": "Олег Петренко", "verified": True},
                                           ToolExecutionContext(session_id="s")))
    assert ok.ok and "Олег Петренко" in ok.message
    me = client.get("/api/me", headers={"X-Client-Id": "conc-browser-0123456789ab"}).json()
    assert me["guest"]["name"] == "Олег Петренко" and me["guest"]["verified"] is True
    client.delete("/api/conversation", headers={"X-Client-Id": "conc-browser-0123456789ab"})  # «Наступний гість»
    assert client.get("/api/me", headers={"X-Client-Id": "conc-browser-0123456789ab"}).json()["guest"] is None
