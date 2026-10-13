"""The web page's Realtime engine: its session, prompt, call, voices and the sideband that runs tools."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

import server.app as web
import server.realtime_engine as engine
from prompts.live_prompt import LIVE_PROMPT, build_realtime_prompt
from server.realtime_engine import REALTIME_TOOLS, RealtimeBridge, history_text, realtime_session
from tools.results import ToolResult
from voice.conversation import ConversationLog

HEADERS = {"X-Client-Id": "realtime-browser-0123456789"}
COMMON = dict(language_name="українською", assistant_name=None, today="2026-10-13")


# ── tools, prompt, session ──


def test_realtime_calls_the_session_tools_itself_but_not_change_voice():
    names = {t["name"] for t in REALTIME_TOOLS}
    assert names == {"set_assistant_name", "set_voice_style", "change_language", "end_conversation"}


def test_realtime_prompt():
    prompt = build_realtime_prompt(voice="cedar", history="User: Привіт\nYou: Привіт!", **COMMON)
    assert "Listening like a person" not in prompt  # no backchannels: Realtime answers after the turn
    assert "Listening like a person" in LIVE_PROMPT
    assert "Being a person" in prompt and "you are talking, not reading" in prompt  # v3 by default
    assert "No preamble before an ordinary answer" in prompt and "never say a sentence you have" in prompt
    assert "«Голос Єви»" in prompt  # the voice is picked on the page
    assert "«Кедр»" in prompt and "я зрозумів" in prompt
    assert prompt.endswith("User: Привіт\nYou: Привіт!")
    assert "Being a person" not in build_realtime_prompt(voice="cedar", variant="v2", **COMMON)
    assert "you are talking, not reading" not in build_realtime_prompt(voice="cedar", variant="v1", **COMMON)
    renamed = build_realtime_prompt(voice="marin", **{**COMMON, "assistant_name": "Ліна"})
    assert "Your name is Ліна" in renamed and "я зрозуміла" in renamed


def test_realtime_session_config(monkeypatch):
    session = realtime_session(instructions="I", language="uk", voice="marin", speed="fast")
    assert session["model"] == "gpt-realtime-2.1"
    assert session["reasoning"] == {"effort": "low"}
    assert session["output_modalities"] == ["audio"]
    assert session["audio"]["output"] == {"voice": "marin", "speed": 1.12}
    assert session["audio"]["input"]["noise_reduction"] == {"type": "near_field"}
    assert session["audio"]["input"]["transcription"]["language"] == "uk"
    vad = session["audio"]["input"]["turn_detection"]
    assert vad == {"type": "semantic_vad", "eagerness": "auto", "create_response": False, "interrupt_response": False}
    assert session["tools"] == REALTIME_TOOLS
    # Tokens: old turns are dropped in steps past 16k (keeps the cache warm), a runaway answer is cut.
    assert session["truncation"] == {
        "type": "retention_ratio", "retention_ratio": 0.75, "token_limits": {"post_instructions": 16000},
    }
    assert session["max_output_tokens"] == 1500
    # An older model has no reasoning; silence-based turns on request.
    monkeypatch.setattr(engine, "OPENAI_REALTIME_MODEL", "gpt-realtime")
    monkeypatch.setattr(engine, "REALTIME_TURN_DETECTION", "server")
    old = realtime_session(instructions="I", language="uk", voice="marin")
    assert "reasoning" not in old
    assert old["audio"]["input"]["turn_detection"]["type"] == "server_vad"
    assert old["audio"]["input"]["turn_detection"]["create_response"] is False


def test_history_text():
    log = ConversationLog()
    log.add("user", "Привіт")
    log.add("assistant", "Привіт! Що робимо?")
    log.add_note("сусіди говорили про ремонт")
    text = history_text(log)
    assert text.startswith("User: Привіт\nYou: Привіт! Що робимо?\nNote: ")
    assert history_text(log, max_chars=10) == text[-10:]


def test_voices_per_engine():
    client = TestClient(web.app)
    live = client.get("/api/voices").json()
    realtime = client.get("/api/voices?engine=realtime").json()
    assert "gleam" in {v["id"] for v in live["voices"]}
    ids = {v["id"] for v in realtime["voices"]}
    assert realtime["default"] == "marin" and "gleam" not in ids and {"marin", "cedar", "ash"} <= ids


def _fake_create(seen: dict):
    async def create(*, sdp, session):
        seen["sdp"], seen["session"] = sdp, session
        headers = {"location": "/v1/realtime/calls/rtc_abc123"}
        return SimpleNamespace(text="answer-sdp", response=SimpleNamespace(headers=headers))

    return create


def test_session_opens_a_realtime_call(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    seen: dict = {}

    async def no_run(self):
        return None

    async def live_create(**kwargs):
        raise AssertionError("a Realtime call must not open a Live session")

    monkeypatch.setattr(web.openai_client.realtime.calls, "create", _fake_create(seen))
    monkeypatch.setattr(web.openai_client.live, "create", live_create)
    monkeypatch.setattr(RealtimeBridge, "run", no_run)
    client = TestClient(web.app)
    user = web.users.get(HEADERS["X-Client-Id"])
    user.voice = "gleam"
    user.conversation.add("user", "Як справи?")
    body = {"sdp": "offer", "engine": "realtime", "voice": "ash", "speed": "slow"}
    res = client.post("/api/session", headers=HEADERS, json=body)
    assert res.status_code == 200
    assert res.json() == {"sdp": "answer-sdp", "session_id": "rtc_abc123"}
    session = seen["session"]
    assert session["audio"]["output"] == {"voice": "ash", "speed": 0.9}
    assert "«Еш»" in session["instructions"] and "Як справи?" in session["instructions"]
    assert "Being a person" in session["instructions"]  # the page sent no prompt: v4, for Realtime the same as v3
    assert user.voice == "gleam"  # Live's own voice choice is untouched
    assert [type(b).__name__ for b in user.bridges] == ["RealtimeBridge"]
    # An unknown voice (a Live one) falls back to Realtime's default.
    client.post("/api/session", headers=HEADERS, json={"sdp": "o", "engine": "realtime", "voice": "gleam"})
    assert seen["session"]["audio"]["output"]["voice"] == "marin"
    user.bridges.clear()
    user.conversation.clear()


# ── sideband ──


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, name, arguments, ctx):
        self.calls.append(name)
        return ToolResult(ok=True, status="ok", message=f"{name} done")


class _FakeConnection:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, event: dict) -> None:
        self.sent.append(event)


def _bridge(log: ConversationLog, executor: _FakeExecutor | None = None) -> tuple[RealtimeBridge, _FakeConnection]:
    bridge = RealtimeBridge(client=None, call_id="rtc_1", executor=executor or _FakeExecutor(), conversation=log)
    conn = _FakeConnection()
    bridge._connection = conn
    return bridge, conn


def test_bridge_runs_tools_and_keeps_the_transcript():
    async def scenario():
        log, executor = ConversationLog(), _FakeExecutor()
        bridge, conn = _bridge(log, executor)
        await bridge._handle_event({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Називай себе Ліна"})
        await bridge._handle_event({"type": "response.output_audio_transcript.done", "transcript": "Добре."})
        await bridge._handle_event({
            "type": "response.done",
            "response": {"output": [
                {"type": "function_call", "name": "set_assistant_name", "arguments": json.dumps({"name": "Ліна"}), "call_id": "c1"},
                {"type": "function_call", "name": "end_conversation", "arguments": "{}", "call_id": "c2"},
            ]},
        })
        await asyncio.gather(*bridge._tool_tasks)
        return bridge, conn, log, executor

    bridge, conn, log, executor = asyncio.run(scenario())
    items = [e["item"] for e in conn.sent if e["type"] == "conversation.item.create"]
    assert [i["call_id"] for i in items] == ["c1", "c2"]
    assert json.loads(items[0]["output"])["message"] == "set_assistant_name done"
    assert conn.sent[-1]["type"] == "response.create"  # she goes on once the results are in
    assert sorted(executor.calls) == ["end_conversation", "set_assistant_name"]
    assert bridge.end_requested
    texts = [t.text for t in log.turns()]
    assert "Називай себе Ліна" in texts and "Добре." in texts


def test_bridge_ignores_answers_without_tools():
    async def scenario():
        bridge, _ = _bridge(ConversationLog())
        await bridge._handle_event({"type": "response.done", "response": {"output": [{"type": "message"}]}})
        return bridge

    assert not asyncio.run(scenario())._tool_tasks


def test_bridge_keeps_the_pages_checks_out_of_the_conversation():
    async def scenario():
        log = ConversationLog()
        bridge, _ = _bridge(log)
        await bridge._handle_event({"type": "response.created", "response": {"id": "r_check", "metadata": {"purpose": "addressed"}}})
        await bridge._handle_event({"type": "response.output_audio_transcript.done", "response_id": "r_check", "transcript": "no"})
        await bridge._handle_event({"type": "response.done", "response": {"id": "r_check", "output": []}})
        await bridge._handle_event({"type": "response.output_audio_transcript.done", "response_id": "r_real", "transcript": "Привіт!"})
        return log, bridge

    log, bridge = asyncio.run(scenario())
    assert [t.text for t in log.turns()] == ["Привіт!"]
    assert not bridge._side_responses


def test_spoken_speed_change_updates_realtime_speed():
    async def scenario():
        user = web.users.get("realtime-speed-0123456789ab")
        bridge, conn = _bridge(user.conversation)
        user.bridges.add(bridge)
        user.speed = "slow"
        await user.apply_delivery()
        user.bridges.clear()
        return conn

    sent = asyncio.run(scenario()).sent
    assert sent[0]["type"] == "conversation.item.create"  # the manner, as an instruction
    assert sent[1] == {"type": "session.update", "session": {"type": "realtime", "audio": {"output": {"speed": 0.9}}}}


def test_a_turn_the_page_deletes_leaves_the_history():
    async def scenario():
        log = ConversationLog()
        bridge, _ = _bridge(log)
        await bridge._handle_event({"type": "conversation.item.input_audio_transcription.completed",
                                    "item_id": "item_1", "transcript": "Привіт, Єво"})
        await bridge._handle_event({"type": "response.output_audio_transcript.done", "transcript": "Привіт!"})
        await bridge._handle_event({"type": "conversation.item.input_audio_transcription.completed",
                                    "item_id": "item_2", "transcript": "Сашко, винеси сміття"})
        await bridge._handle_event({"type": "conversation.item.deleted", "item_id": "item_2"})
        await bridge._handle_event({"type": "conversation.item.deleted", "item_id": "unknown"})
        return log, bridge

    log, bridge = asyncio.run(scenario())
    assert [t.text for t in log.turns()] == ["Привіт, Єво", "Привіт!"]
    assert bridge._user_turns == ["Привіт, Єво"]  # tools don't see it as the user's words either


def test_forget_removes_only_the_latest_matching_turn():
    log = ConversationLog()
    for role, text in (("user", "Так"), ("assistant", "Добре"), ("user", "Так")):
        log.add(role, text)
        log.end_turn()
    log.forget("user", " Так ")
    log.forget("user", "нема такого")
    assert [(t.role, t.text) for t in log.turns()] == [("user", "Так"), ("assistant", "Добре")]
