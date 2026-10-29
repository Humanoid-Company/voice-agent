"""The GPT-Live bridge against a fake Live socket and a fake voice: no network, no keys.

The fake socket plays GPT-Live: it records what the bridge sends and pushes the events a test wants. The fake voice is a
TTS whose generations the test can feed with audio."""
import asyncio
import base64

from server.config import Config
from server.live import LiveBridge, _Chunker, uses_live
from server.persona import Persona
from server.usage import Usage


class FakeConn:
    def __init__(self):
        self.sent: list[dict] = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, event):
        self.sent.append(event)
        if event["type"] == "session.start":
            self.events.put_nowait({"type": "session.started"})
        if event["type"] == "session.close":
            self.events.put_nowait({"type": "session.closed", "usage": {"seconds": 42}})
            self.events.put_nowait(None)

    async def __aiter__(self):
        while True:
            ev = await self.events.get()
            if ev is None:
                return
            yield ev

    async def close(self):
        self.closed = True

    def push(self, **ev):
        self.events.put_nowait(ev)

    def kinds(self):
        return [e["type"] for e in self.sent]


class Stream:
    """One generation at the voice provider: every written text is answered with a chunk of audio."""

    def __init__(self, marker: bytes, auto_end: bool = True):
        self.auto_end = auto_end  # False: the provider is still talking after the text is complete (the test ends it)
        self.written: list[str] = []
        self.q: asyncio.Queue = asyncio.Queue()
        self.finished = self.cancelled = False
        self.marker = marker

    async def write(self, text):
        self.written.append(text)
        self.q.put_nowait(self.marker)

    async def finish(self):
        self.finished = True
        if self.auto_end:
            self.q.put_nowait(None)

    async def cancel(self):
        self.cancelled = True
        self.q.put_nowait(None)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        while (item := await self.q.get()) is not None:
            if isinstance(item, Exception):
                raise item
            yield item


class FakeTTS:
    sample_rate = 24000

    def __init__(self):
        self.streams: list[Stream] = []
        self.auto_end = True

    async def connect(self): ...
    async def close(self): ...

    def open_stream(self):
        self.streams.append(Stream(bytes([65 + len(self.streams)]) * 4, self.auto_end))  # b"AAAA", b"BBBB", ...
        return self.streams[-1]


def make(conn, *, idle_ms=10_000, greet=False, history=None, **cfg):
    out, audio = [], []

    async def sj(m):
        out.append(m)

    async def sb(b):
        audio.append(b)

    async def connect():
        return conn

    b = LiveBridge(
        tts=FakeTTS(),
        send_json=sj,
        send_bytes=sb,
        cfg=Config(realtime_model="gpt-live-1", live_idle_flush_ms=idle_ms, **cfg),
        provider="soniox",
        persona=Persona(),
        connect=connect,
        greet=greet,
        history=history,
    )
    return b, out, audio


async def pump(n=40):
    for _ in range(n):
        await asyncio.sleep(0)


def say(conn, delta, start_ms, end_ms):
    conn.push(type="session.output_transcript.delta", delta=delta, start_ms=start_ms, end_ms=end_ms)


def hear(conn, delta, start_ms=0, end_ms=0):
    conn.push(type="session.input_transcript.delta", delta=delta, start_ms=start_ms, end_ms=end_ms)


# ---------------------------------------------------------------------- the session


def test_the_session_starts_with_the_model_instructions_history_and_a_greeting():
    async def go():
        conn = FakeConn()
        b, out, _ = make(conn, greet=True, history=[{"role": "user", "content": "привіт"}, {"role": "assistant", "content": "добрий день"}])
        await b.start()
        start = conn.sent[0]
        assert start["type"] == "session.start"
        s = start["session"]
        assert s["model"] == "gpt-live-1" and s["audio"]["format"] == {"type": "audio/pcm", "rate": 24000}
        assert "Тебе звати" in s["instructions"] and "Зараз:" in s["instructions"]
        roles = [i["role"] for i in s["input"]]
        assert roles == ["user", "assistant", "developer"]
        assert s["input"][1]["content"][0]["type"] == "output_text" and "Привітайся" in s["input"][2]["content"][0]["text"]
        assert [m for m in out if m["type"] == "ready"][0]["engine"] == "realtime"
        await b.close()
        assert conn.closed and "session.close" in conn.kinds()

    asyncio.run(go())


def test_the_microphone_goes_to_gpt_live_as_it_is_and_not_while_paused():
    async def go():
        conn = FakeConn()
        b, _, _ = make(conn)
        await b.start()
        await b.on_audio(b"\x01\x00" * 960)  # what pcm.js sends: PCM16, 24 kHz, 40 ms
        sent = [e for e in conn.sent if e["type"] == "session.input_audio.append"]
        assert base64.b64decode(sent[0]["audio"]) == b"\x01\x00" * 960
        await b.on_client({"type": "pause"})
        await b.on_audio(b"\x01\x00" * 960)
        assert len([e for e in conn.sent if e["type"] == "session.input_audio.append"]) == 1
        assert "session.input_audio.mute" in conn.kinds()
        await b.on_client({"type": "resume"})
        assert "session.input_audio.unmute" in conn.kinds()
        await b.close()

    asyncio.run(go())


def test_the_path_follows_realtime_model():
    assert uses_live(Config(realtime_model="gpt-live-1")) and uses_live(Config(realtime_model="GPT-Live-2"))
    assert not uses_live(Config(realtime_model="gpt-realtime-2.1"))


# ---------------------------------------------------------------------- utterances


def test_a_pause_on_the_timeline_longer_than_the_gap_starts_a_new_utterance():
    async def go():
        conn = FakeConn()
        b, out, audio = make(conn, live_gap_ms=600)
        await b.start()
        say(conn, "Привіт, ", 0, 400)
        say(conn, "як справи? ", 450, 900)  # 50 ms later on the timeline: the same utterance
        say(conn, "Нова репліка. ", 1700, 2200)  # 800 ms later: a new one
        await pump()
        streams = b.tts.streams
        assert len(streams) == 2
        assert "".join(streams[0].written).startswith("Привіт, як справи?") and streams[0].finished
        assert "Нова" in "".join(streams[1].written)
        assert [m["text"] for m in out if m["type"] == "record"][0] == "Привіт, як справи?"
        await b.close()

    asyncio.run(go())


def test_the_rest_is_sent_and_the_utterance_ends_when_no_text_comes_for_the_idle_time():
    async def go():
        conn = FakeConn()
        b, out, _ = make(conn, idle_ms=40)
        await b.start()
        say(conn, "Привіт, як спр", 0, 500)
        await pump()
        s = b.tts.streams[0]
        assert "".join(s.written) == "Привіт, як " and not s.finished  # the unfinished word waits
        await asyncio.sleep(0.12)
        assert "".join(s.written) == "Привіт, як спр" and s.finished  # idle: the rest goes out, the utterance is over
        say(conn, "Далі. ", 520, 800)  # text after that is a new utterance
        await pump()
        assert len(b.tts.streams) == 2
        await b.close()

    asyncio.run(go())


def test_a_tag_is_never_cut_in_two_and_only_soniox_hears_it():
    c = _Chunker()
    assert [c.feed(d) for d in ("[wa", "rm] При", "віт ")] == ["", "[warm] ", "Привіт "]  # the tag waits for its end

    async def go():
        conn = FakeConn()
        b, out, _ = make(conn)
        await b.start()
        say(conn, "[wa", 0, 100)
        say(conn, "rm] Привіт ", 100, 300)
        await pump()
        assert "".join(b.tts.streams[0].written) == "[warm] Привіт "  # the voice gets the tag
        deltas = [m["text"] for m in out if m["type"] == "assistant_delta"]
        assert "".join(deltas).strip() == "Привіт"  # the page does not
        await b.close()

    asyncio.run(go())


def test_utterances_are_spoken_in_order_even_when_the_second_one_is_ready_first():
    async def go():
        conn = FakeConn()
        b, _, audio = make(conn, live_gap_ms=600)
        await b.start()
        b.tts.auto_end = False  # the provider is slower than the text: A is still being spoken when B is complete
        say(conn, "Перша репліка. ", 0, 500)
        say(conn, "Друга репліка. ", 2000, 2500)
        await pump()
        a, _second = b.tts.streams
        assert audio == [b"AAAA"]  # A's audio only (B's is held back: it is not B's turn yet)
        a.q.put_nowait(None)  # the provider is done with A
        await pump()
        assert audio == [b"AAAA", b"BBBB"]
        await b.close()

    asyncio.run(go())


def test_a_russian_sentence_cuts_the_utterance():
    async def go():
        conn = FakeConn()
        b, out, audio = make(conn)
        await b.start()
        say(conn, "Добре. Что это такое было? Ну вот. ", 0, 800)
        await pump()
        s = b.tts.streams[0]
        assert s.cancelled
        say(conn, "І ще речення. ", 800, 900)  # the rest of it is not said
        await pump()
        assert "".join(s.written).count("І ще") == 0
        await b.close()

    asyncio.run(go())


# ---------------------------------------------------------------------- interruptions


def test_a_short_listeners_sound_is_not_an_interruption_but_words_are():
    async def go():
        conn = FakeConn()
        b, out, audio = make(conn)
        await b.start()
        say(conn, "Це довга розповідь про щось дуже цікаве. ", 0, 3000)
        await pump()
        assert b.state == "speaking"
        hear(conn, "угу")
        await pump()
        assert not [m for m in out if m["type"] == "clear"]
        b._user_at = 0  # a new phrase
        hear(conn, "зачекай, у мене питання")
        await pump()
        clears = [m for m in out if m["type"] == "clear"]
        assert len(clears) == 1 and clears[0]["reason"] == "takeover"
        assert b.tts.streams[0].cancelled and b.state == "listening"
        await b.close()

    asyncio.run(go())


def test_after_an_interruption_late_audio_and_the_rest_of_the_cut_speech_are_dropped():
    async def go():
        conn = FakeConn()
        b, out, audio = make(conn)
        await b.start()
        say(conn, "Це довга розповідь про щось дуже цікаве. ", 0, 3000)
        await pump()
        first = list(audio)
        assert first
        hear(conn, "стоп давай про інше")
        await pump()
        s = b.tts.streams[0]
        assert s.cancelled
        s.q.put_nowait(b"LATE")  # a tail of the cut utterance still arrives from the provider
        await pump()
        assert audio == first
        say(conn, "Хвіст перебитої репліки. ", 2500, 3200)  # starts before the cut-off point: not said
        await pump()
        assert len(b.tts.streams) == 1
        say(conn, "Нова відповідь. ", 3400, 3900)  # after it: a new utterance, spoken
        await pump()
        assert len(b.tts.streams) == 2 and audio[-1] == b"BBBB"
        await b.close()

    asyncio.run(go())


def test_her_own_words_coming_back_through_the_microphone_are_not_an_interruption():
    async def go():
        conn = FakeConn()
        b, out, _ = make(conn)
        await b.start()
        say(conn, "Це довга розповідь про щось дуже цікаве. ", 0, 3000)
        await pump()
        hear(conn, "довга розповідь про щось дуже цікаве")  # the echo of what she just said
        await pump()
        assert not [m for m in out if m["type"] == "clear"]
        await b.close()

    asyncio.run(go())


def test_the_page_can_interrupt_by_button_and_by_a_stop_word():
    async def go():
        conn = FakeConn()
        b, out, _ = make(conn)
        await b.start()
        say(conn, "Це довга розповідь про щось дуже цікаве. ", 0, 3000)
        await pump()
        await b.on_client({"type": "interrupt", "by": "button"})
        assert [m["reason"] for m in out if m["type"] == "clear"] == ["button"]
        say(conn, "Нова розповідь про інше. ", 3500, 4000)
        await pump()
        await b.on_client({"type": "heard", "text": "стоп", "final": False})
        assert [m["reason"] for m in out if m["type"] == "clear"] == ["button", "stop"]
        await b.close()

    asyncio.run(go())


# ---------------------------------------------------------------------- the meter and the timing


def test_the_cost_is_session_minutes_times_the_price_not_tokens():
    cfg = Config(price_live_per_min=0.05, price_soniox_per_1k_chars=0.0)
    u = Usage(live=True, live_seconds=120, llm_in=1_000_000, llm_out=1_000_000, audio_in=1_000_000)
    assert abs(u.cost(cfg) - 0.10) < 1e-9  # the token fields are not priced
    u.chars["soniox"] = 1000
    assert abs(Usage(live=True, live_seconds=60, chars={"respeecher": 60000}).cost(Config(price_live_per_min=0.05)) - (0.05 + 60 * 0.033)) < 1e-6
    assert Usage(live=False, llm_in=1_000_000).cost(cfg) > 0  # the old path is unchanged

    async def go():
        conn = FakeConn()
        b, out, _ = make(conn)
        await b.start()
        conn.push(type="session.usage.updated", usage={"seconds": 90})
        conn.push(type="session.usage.updated", usage={"seconds": 120})  # cumulative: the last figure, not a sum
        await pump()
        await b._send_usage()
        usage = [m for m in out if m["type"] == "usage"][-1]
        assert abs(usage["cost"] - 0.10) < 1e-6
        await b.close()

    asyncio.run(go())


def test_the_three_moments_of_an_utterance_are_reported():
    async def go():
        conn = FakeConn()
        b, out, _ = make(conn)
        await b.start()
        say(conn, "Привіт, як справи? ", 0, 500)
        await pump()
        ms = [m for m in out if m["type"] == "timing"][0]["ms"]
        assert list(ms) == ["first_delta", "first_write", "tts_audio"]
        assert 0 == ms["first_delta"] <= ms["first_write"] <= ms["tts_audio"]
        await b.close()

    asyncio.run(go())


# ---------------------------------------------------------------------- the page's WebSocket picks the path


def test_ws_picks_the_bridge_for_gpt_live_and_the_old_path_otherwise(monkeypatch):
    from fastapi.testclient import TestClient

    import server.app as app_mod

    made = []

    class Stub:
        def __init__(self, **kw):
            made.append((type(self).__name__, kw.get("ears")))

        async def start(self): ...
        async def close(self): ...
        async def on_audio(self, b): ...
        async def on_client(self, m): ...

    class StubBridge(Stub): ...
    class StubSession(Stub): ...

    monkeypatch.setattr(app_mod, "LiveBridge", StubBridge)
    monkeypatch.setattr(app_mod, "VoiceSession", StubSession)
    monkeypatch.setattr(app_mod, "make_tts", lambda *a, **k: FakeTTS())

    def connect(model, engine):
        monkeypatch.setattr(app_mod, "cfg", Config(realtime_model=model, openai_api_key="k"))
        with TestClient(app_mod.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "start", "provider": "soniox", "voice": "Taras", "language": "uk", "engine": engine})
            ws.send_json({"type": "pause"})

    connect("gpt-live-1", "realtime")
    connect("gpt-realtime-2.1", "realtime")
    connect("gpt-live-1", "browser")  # the browser's recognizer does not use GPT-Live
    assert [m[0] for m in made] == ["StubBridge", "StubSession", "StubSession"]
