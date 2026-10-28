"""The Realtime ears against a fake connection: no network, no keys.

The fake plays the part of OpenAI: it records what the client sends and pushes the server events a test wants."""
import asyncio
import base64
import json

from server.config import Config
from server.llm import SEGMENT_END, TurnContext
from server.realtime import RealtimeLLM, flat_tool
from server.session import VoiceSession
from server.usage import Usage


class FakeConn:
    def __init__(self):
        self.sent: list[dict] = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, event):
        self.sent.append(event)
        if event["type"] == "response.create":
            self.on_create(event)

    on_create = staticmethod(lambda event: None)

    async def __aiter__(self):
        while True:
            ev = await self.events.get()
            if ev is None:
                return
            yield ev

    async def close(self):
        self.closed = True
        self.events.put_nowait(None)

    def push(self, **ev):
        self.events.put_nowait(ev)

    def kinds(self):
        return [e["type"] for e in self.sent]


def make_llm(conn, search=None):
    async def connect():
        return conn

    return RealtimeLLM(Config(), connect=connect, search=search)


def answer_events(conn, rid, text, *, output=None, usage=None, item="item_a"):
    """What the server sends for one text answer."""
    conn.push(type="response.created", response={"id": rid})
    conn.push(type="response.output_item.added", response_id=rid, item={"id": item, "type": "message"})
    for word in text.split():
        conn.push(type="response.output_text.delta", response_id=rid, delta=word + " ")
    conn.push(
        type="response.done",
        response={"id": rid, "status": "completed", "output": output or [], "usage": usage or {}},
    )


async def collect(llm, ctx=None):
    return "".join([d async for d in llm.stream([], ctx or TurnContext(system_prompt="sys"))]).replace(SEGMENT_END, "")


def test_session_is_text_only_with_manual_turns():
    async def go():
        conn = FakeConn()
        llm = make_llm(conn)
        await llm.open(language="uk", history=[{"role": "user", "content": "привіт"}, {"role": "assistant", "content": "добрий день"}])
        sess = conn.sent[0]["session"]
        assert sess["output_modalities"] == ["text"]
        vad = sess["audio"]["input"]["turn_detection"]
        assert vad["create_response"] is False and vad["interrupt_response"] is False
        assert sess["audio"]["input"]["transcription"]["language"] == "uk"
        seeded = [e["item"] for e in conn.sent if e["type"] == "conversation.item.create"]
        assert [i["role"] for i in seeded] == ["user", "assistant"]
        assert seeded[0]["content"][0]["type"] == "input_text" and seeded[1]["content"][0]["type"] == "output_text"
        await llm.close()
        assert conn.closed

    asyncio.run(go())


def test_audio_is_forwarded_as_base64():
    async def go():
        conn = FakeConn()
        llm = make_llm(conn)
        await llm.open()
        await llm.feed(b"\x01\x02\x03\x04")
        ev = conn.sent[-1]
        assert ev["type"] == "input_audio_buffer.append" and base64.b64decode(ev["audio"]) == b"\x01\x02\x03\x04"
        await llm.close()

    asyncio.run(go())


def test_stream_yields_text_and_reports_usage_with_audio_split():
    async def go():
        conn = FakeConn()
        llm = make_llm(conn)
        await llm.open()
        used, audio = [], []
        ctx = TurnContext(system_prompt="sys", on_usage=lambda i, o: used.append((i, o)), on_audio=audio.append)
        conn.on_create = lambda _e: answer_events(
            conn, "r1", "Привіт тобі", usage={"input_tokens": 100, "output_tokens": 7, "input_token_details": {"audio_tokens": 60}}
        )
        assert await collect(llm, ctx) == "Привіт тобі "
        assert used == [(40, 7)] and audio == [60]
        update = [e for e in conn.sent if e["type"] == "session.update"][-1]["session"]
        assert update["instructions"] == "sys"
        await llm.close()

    asyncio.run(go())


def test_instructions_are_resent_only_when_they_change():
    async def go():
        conn = FakeConn()
        llm = make_llm(conn)
        await llm.open()
        conn.on_create = lambda _e: answer_events(conn, f"r{len(conn.sent)}", "ок")
        await collect(llm, TurnContext(system_prompt="a"))
        await collect(llm, TurnContext(system_prompt="a"))
        await collect(llm, TurnContext(system_prompt="b"))
        updates = [e["session"].get("instructions") for e in conn.sent if e["type"] == "session.update"]
        assert updates[1:] == ["a", "b"]
        await llm.close()

    asyncio.run(go())


def test_tool_call_runs_the_tool_and_asks_for_the_next_response():
    async def go():
        conn = FakeConn()
        llm = make_llm(conn)
        await llm.open()
        ran = []

        async def run_tool(name, args):
            ran.append((name, args))
            return "готово"

        rounds = iter(range(10))

        def create(_e):
            n = next(rounds)
            if n == 0:
                answer_events(
                    conn, "r1", "",
                    output=[{"type": "function_call", "name": "set_assistant_name", "call_id": "c1", "arguments": json.dumps({"name": "Леся"})}],
                )
            else:
                answer_events(conn, "r2", "Тепер я Леся")

        conn.on_create = create
        ctx = TurnContext(system_prompt="s", run_tool=run_tool)
        assert await collect(llm, ctx) == "Тепер я Леся "
        assert ran == [("set_assistant_name", {"name": "Леся"})]
        out = [e["item"] for e in conn.sent if e["type"] == "conversation.item.create" and e["item"]["type"] == "function_call_output"]
        assert out == [{"type": "function_call_output", "call_id": "c1", "output": "готово"}]
        assert conn.kinds().count("response.create") == 2
        await llm.close()

    asyncio.run(go())


def test_web_search_says_a_cue_and_is_limited_per_turn():
    async def go():
        conn = FakeConn()
        searched = []

        async def search(query, days, topic=None):
            searched.append(query)
            return "результат"

        llm = make_llm(conn, search=search)
        await llm.open()
        n = {"i": 0}

        def create(_e):
            n["i"] += 1
            call = {"type": "function_call", "name": "web_search", "call_id": f"c{n['i']}", "arguments": json.dumps({"query": f"q{n['i']}"})}
            if n["i"] <= 3:
                answer_events(conn, f"r{n['i']}", "", output=[call])
            else:
                answer_events(conn, "rz", "Ось")

        conn.on_create = create
        text = await collect(llm)
        assert text.endswith("Ось ") and len(text) > len("Ось ")  # a cue before the first search
        assert searched == ["q1", "q2"]  # the third search is refused, not run
        await llm.close()

    asyncio.run(go())


def test_cancelled_stream_cancels_the_response():
    async def go():
        conn = FakeConn()
        llm = make_llm(conn)
        await llm.open()
        conn.on_create = lambda _e: (
            conn.push(type="response.created", response={"id": "r1"}),
            conn.push(type="response.output_text.delta", response_id="r1", delta="Довго "),
        )
        got = []

        async def run():
            async for d in llm.stream([], TurnContext(system_prompt="s")):
                got.append(d)

        task = asyncio.create_task(run())
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert got == ["Довго "] and "response.cancel" in conn.kinds()
        await llm.close()

    asyncio.run(go())


def test_events_of_an_old_response_do_not_leak_into_the_next_one():
    async def go():
        conn = FakeConn()
        llm = make_llm(conn)
        await llm.open()
        conn.push(type="response.created", response={"id": "old"})  # nobody is waiting: ignored
        conn.push(type="response.output_text.delta", response_id="old", delta="чуже ")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        conn.on_create = lambda _e: (
            conn.push(type="response.output_text.delta", response_id="old", delta="і це чуже "),
            answer_events(conn, "new", "своє"),
        )
        assert await collect(llm) == "своє "
        await llm.close()

    asyncio.run(go())


def test_replace_spoken_keeps_only_what_was_heard():
    async def go():
        conn = FakeConn()
        llm = make_llm(conn)
        await llm.open()
        conn.on_create = lambda _e: answer_events(conn, "r1", "Перше. Друге.", item="msg_1")
        await collect(llm)
        await llm.replace_spoken("Перше. …")
        deleted = [e["item_id"] for e in conn.sent if e["type"] == "conversation.item.delete"]
        assert deleted == ["msg_1"]
        last = conn.sent[-1]["item"]
        assert last["role"] == "assistant" and last["content"][0]["text"] == "Перше. …"
        await llm.replace_spoken("")  # nothing left to replace
        await llm.close()

    asyncio.run(go())


def test_events_reach_the_handlers():
    async def go():
        conn = FakeConn()
        llm = make_llm(conn)
        seen = []

        async def stopped(item_id):
            seen.append(("stopped", item_id))

        async def text(item_id, t):
            seen.append(("text", item_id, t))

        llm.on_speech_stopped, llm.on_user_text = stopped, text
        await llm.open()
        conn.push(type="input_audio_buffer.speech_stopped", item_id="i1")
        conn.push(type="conversation.item.input_audio_transcription.completed", item_id="i1", transcript=" Привіт ")
        for _ in range(10):
            await asyncio.sleep(0)
        assert seen == [("stopped", "i1"), ("text", "i1", "Привіт")]
        await llm.close()

    asyncio.run(go())


def test_flat_tool_shape():
    tool = flat_tool({"type": "function", "function": {"name": "x", "description": "d", "parameters": {"type": "object", "properties": {}}}})
    assert tool == {"type": "function", "name": "x", "description": "d", "parameters": {"type": "object", "properties": {}}}


def test_usage_costs_use_realtime_prices():
    cfg = Config()
    plain = Usage(llm_in=1_000_000)
    rt = Usage(llm_in=1_000_000, audio_in=1_000_000, realtime=True)
    assert plain.cost(cfg) == cfg.price_llm_in_per_m
    assert rt.cost(cfg) == cfg.price_rt_text_in_per_m + cfg.price_rt_audio_in_per_m


# ---------------------------------------------------------------------- the session on top of the ears


class FakeTTS:
    sample_rate = 24000

    def __init__(self):
        self.said = []

    async def connect(self): ...
    async def close(self): ...

    async def synthesize(self, text):
        yield b"\x00\x01" * 10
        self.said.append(text)


def make_session(conn):
    out = []

    async def sj(m):
        out.append(m)

    async def sb(_b):
        pass

    s = VoiceSession(llm=None, ears=make_llm(conn), tts=FakeTTS(), send_json=sj, send_bytes=sb)
    return s, out


async def pump(n=30):
    for _ in range(n):
        await asyncio.sleep(0)


def test_session_answers_when_realtime_hears_the_user_stop():
    async def go():
        conn = FakeConn()
        s, out = make_session(conn)
        await s.start()
        assert [m for m in out if m["type"] == "ready"][0]["engine"] == "realtime"
        conn.on_create = lambda _e: answer_events(conn, "r1", "Привіт. Як справи?")
        await s.on_heard("привіт", False)  # the browser's interim words are only shown
        await s.on_heard("привіт", True)  # a final from the browser does not start a turn
        assert s._turn is None
        conn.push(type="input_audio_buffer.speech_stopped", item_id="u1")
        await pump()
        await asyncio.gather(s._turn, return_exceptions=True)
        conn.push(type="conversation.item.input_audio_transcription.completed", item_id="u1", transcript="Привіт")
        await pump()
        assert " ".join(s.tts.said) == "Привіт. Як справи?"
        assert {"type": "user", "text": "Привіт", "final": True} in out
        assert [h["role"] for h in s.history] == ["user", "assistant"] and s.history[0]["content"] == "Привіт"
        assert s.usage.realtime
        await s.close()

    asyncio.run(go())


def test_session_ignores_sound_while_busy_and_after_stop():
    async def go():
        conn = FakeConn()
        s, out = make_session(conn)
        await s.start()
        s.state = "speaking"
        conn.push(type="input_audio_buffer.speech_stopped", item_id="u1")
        await pump()
        assert s._turn is None  # she was talking: the browser's words decide interruptions
        s.state = "listening"
        s._skip_reply = True  # «стоп» was recognized
        conn.push(type="input_audio_buffer.speech_stopped", item_id="u2")
        await pump()
        assert s._turn is None and any(m["type"] == "user_drop" and m["reason"] == "stop" for m in out)
        await s.close()

    asyncio.run(go())


def test_session_drops_a_phrase_that_is_her_own_echo():
    async def go():
        conn = FakeConn()
        s, out = make_session(conn)
        await s.start()
        conn.on_create = lambda _e: answer_events(conn, "r1", "Окей.")
        conn.push(type="input_audio_buffer.speech_stopped", item_id="u1")
        await pump()
        await asyncio.gather(s._turn, return_exceptions=True)
        s._recent = "Це було моє довге речення про погоду"  # what she said meanwhile
        conn.push(type="conversation.item.input_audio_transcription.completed", item_id="u1", transcript="моє довге речення про погоду")
        await pump()
        assert any(m["type"] == "user_drop" and m["reason"] == "echo" for m in out)
        assert "conversation.item.delete" in conn.kinds()
        assert all(h["content"] for h in s.history)
        await s.close()

    asyncio.run(go())


def test_interrupt_tells_realtime_what_was_heard():
    async def go():
        conn = FakeConn()
        s, out = make_session(conn)
        await s.start()
        s.ears._items = ["msg_9"]
        s._spoken, s._turn_done, s.state = ["Перше речення."], False, "speaking"
        await s.interrupt(reason="takeover", text="стоп")
        assert [e["item_id"] for e in conn.sent if e["type"] == "conversation.item.delete"] == ["msg_9"]
        assert conn.sent[-1]["item"]["content"][0]["text"] == "Перше речення. …"
        await s.close()

    asyncio.run(go())


def test_audio_is_not_sent_on_pause():
    async def go():
        conn = FakeConn()
        s, _out = make_session(conn)
        await s.start()
        await s.on_audio(b"\x00\x01")
        assert conn.kinds().count("input_audio_buffer.append") == 1
        await s.on_client({"type": "pause", "background": True})
        await s.on_audio(b"\x00\x01")
        assert conn.kinds().count("input_audio_buffer.append") == 1
        await s.close()

    asyncio.run(go())


def test_the_line_before_a_search_is_spoken_while_the_search_runs():
    """«Секунду.» used to wait in the splitter and sound after the result, glued to it."""

    async def go():
        spoken_first = asyncio.Event()
        order = []

        class SearchingLLM:
            searchable = True

            async def stream(self, history, ctx):
                yield "Секунду."  # shorter than a sentence: the splitter would glue it to what follows
                yield SEGMENT_END
                order.append("search started")
                await asyncio.wait_for(spoken_first.wait(), 2)  # the search can only end if she already spoke
                order.append("search done")
                yield "Ось що знайшла."

        class WatchingTTS(FakeTTS):
            async def synthesize(self, text):
                order.append("said: " + text)
                spoken_first.set()
                async for chunk in super().synthesize(text):
                    yield chunk

        out = []

        async def sj(m):
            out.append(m)

        async def sb(_b):
            pass

        s = VoiceSession(llm=SearchingLLM(), tts=WatchingTTS(), send_json=sj, send_bytes=sb)
        await s.start()
        await s.on_heard("що цікавого сталось", True)
        await asyncio.wait_for(s._turn, 5)
        assert order == ["search started", "said: Секунду.", "search done", "said: Ось що знайшла."], order
        await s.close()

    asyncio.run(go())


def _call(i, name="web_search", args=None):
    return {"type": "function_call", "name": name, "call_id": f"c{i}", "arguments": json.dumps(args or {"query": f"q{i}"})}


def test_after_the_tool_rounds_are_used_up_the_model_must_answer_in_text():
    async def go():
        conn = FakeConn()
        llm = make_llm(conn, search=lambda q, d, t=None: asyncio.sleep(0, "ok"))
        await llm.open()
        n = {"i": 0}

        def create(event):
            n["i"] += 1
            if event.get("response", {}).get("tool_choice") == "none":
                answer_events(conn, f"r{n['i']}", "Ось відповідь")
            else:
                answer_events(conn, f"r{n['i']}", "", output=[_call(n["i"], "noop", {})])

        conn.on_create = create
        assert await collect(llm) == "Ось відповідь "  # it used to end in silence
        creates = [e for e in conn.sent if e["type"] == "response.create"]
        assert len(creates) == 5 and "response" not in creates[0] and creates[-1]["response"] == {"tool_choice": "none"}
        await llm.close()

    asyncio.run(go())


def test_a_search_over_the_limit_is_not_run_and_not_counted():
    async def go():
        conn = FakeConn()
        searched, counted = [], []

        async def search(query, days, topic=None):
            searched.append(query)
            return "результат"

        llm = make_llm(conn, search=search)
        await llm.open()
        n = {"i": 0}

        def create(_e):
            n["i"] += 1
            if n["i"] == 1:
                answer_events(conn, "r1", "", output=[_call(1), _call(2), _call(3)])
            else:
                answer_events(conn, "r2", "Ось")

        conn.on_create = create
        await collect(llm, TurnContext(system_prompt="s", on_search=lambda: counted.append(1)))
        assert searched == ["q1", "q2"] and len(counted) == 2
        outs = {e["item"]["call_id"]: e["item"]["output"] for e in conn.sent if e["type"] == "conversation.item.create" and e["item"]["type"] == "function_call_output"}
        assert "Ліміт пошуку" in outs["c3"] and outs["c1"] == "результат"
        await llm.close()

    asyncio.run(go())


def test_a_tool_that_breaks_is_reported_not_raised():
    async def go():
        conn = FakeConn()
        llm = make_llm(conn)
        await llm.open()
        n = {"i": 0}

        async def boom(name, args):
            raise ValueError("x")

        def create(_e):
            n["i"] += 1
            answer_events(conn, f"r{n['i']}", "" if n["i"] == 1 else "Не вийшло", output=[_call(1, "change_voice", {})] if n["i"] == 1 else [])

        conn.on_create = create
        assert await collect(llm, TurnContext(system_prompt="s", run_tool=boom)) == "Не вийшло "
        out = [e["item"]["output"] for e in conn.sent if e["type"] == "conversation.item.create" and e["item"]["type"] == "function_call_output"]
        assert "не спрацював (ValueError)" in out[0]
        await llm.close()

    asyncio.run(go())


def test_cut_off_while_a_tool_runs_leaves_no_call_without_an_answer():
    async def go():
        conn = FakeConn()
        started = asyncio.Event()

        async def slow(query, days, topic=None):
            started.set()
            await asyncio.sleep(60)

        llm = make_llm(conn, search=slow)
        await llm.open()
        conn.on_create = lambda _e: answer_events(conn, "r1", "", output=[_call(1)])

        async def run():
            async for _ in llm.stream([], TurnContext(system_prompt="s")):
                pass

        task = asyncio.create_task(run())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        outs = [e["item"] for e in conn.sent if e["type"] == "conversation.item.create" and e["item"]["type"] == "function_call_output"]
        assert [o["call_id"] for o in outs] == ["c1"] and "Скасовано" in outs[0]["output"]
        await llm.close()

    asyncio.run(go())


def test_a_silent_model_ends_the_turn_with_an_error(monkeypatch):
    async def go():
        import server.realtime as rt

        monkeypatch.setattr(rt, "_RESPONSE_TIMEOUT_S", 0.05)
        conn = FakeConn()  # never answers
        llm = make_llm(conn)
        await llm.open()
        try:
            await collect(llm)
        except RuntimeError as e:
            assert "не відповів" in str(e)
        else:
            raise AssertionError("no error")
        await llm.close()

    asyncio.run(go())


# ---------------------------------------------------------------------- Realtime's VAD as an interruption


def vad_session(monkeypatch, conn, delay=0.05):
    import server.session as sess

    monkeypatch.setattr(sess, "_VAD_BARGE_S", delay)
    return make_session(conn)


def test_a_voice_that_goes_on_while_she_speaks_interrupts_her(monkeypatch):
    async def go():
        conn = FakeConn()
        s, out = vad_session(monkeypatch, conn)
        await s.start()
        s.state, s._spoken, s._turn_done = "speaking", ["Перше речення."], False
        conn.push(type="input_audio_buffer.speech_started")
        await asyncio.sleep(0.15)  # the voice goes on: no speech_stopped
        assert any(m["type"] == "clear" and m["reason"] == "sound" for m in out) and s.state == "listening"
        await s.close()

    asyncio.run(go())


def test_a_short_sound_or_a_listeners_ugu_does_not(monkeypatch):
    async def go():
        conn = FakeConn()
        s, out = vad_session(monkeypatch, conn)
        await s.start()
        s.state = "speaking"
        conn.push(type="input_audio_buffer.speech_started")
        await asyncio.sleep(0.01)
        conn.push(type="input_audio_buffer.speech_stopped", item_id="u1")  # over before the timer: a cough
        await asyncio.sleep(0.12)
        assert not any(m["type"] == "clear" for m in out) and s.state == "speaking"
        # «угу» heard by the browser's recognizer just before: not an interruption however long it sounded
        await s.on_heard("угу", False)
        conn.push(type="input_audio_buffer.speech_started")
        await asyncio.sleep(0.15)
        assert not any(m["type"] == "clear" for m in out)
        await s.close()

    asyncio.run(go())


def test_going_on_while_she_thinks_cancels_the_answer_and_the_whole_phrase_is_answered(monkeypatch):
    async def go():
        conn = FakeConn()
        s, out = vad_session(monkeypatch, conn)
        await s.start()
        created = []

        def create(event):
            created.append(event)
            if len(created) == 1:
                conn.push(type="response.created", response={"id": "r1"})  # thinking: no text yet
            else:
                answer_events(conn, "r2", "Відповідаю на все.")

        conn.on_create = create
        conn.push(type="input_audio_buffer.speech_stopped", item_id="u1")
        await asyncio.sleep(0.02)
        assert s.state == "thinking"
        conn.push(type="input_audio_buffer.speech_started")  # the user adds to the phrase
        await asyncio.sleep(0.15)
        assert s._turn is None or s._turn.done() or s.state == "listening"
        assert "response.cancel" in conn.kinds()
        conn.push(type="input_audio_buffer.speech_stopped", item_id="u2")  # the phrase ends: one answer to all of it
        await pump(60)
        await asyncio.gather(s._turn, return_exceptions=True)
        assert len(created) == 2 and " ".join(s.tts.said) == "Відповідаю на все."
        await s.close()

    asyncio.run(go())
