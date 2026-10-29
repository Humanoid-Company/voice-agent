"""synthesize() against a fake WebSocket: audio is streamed, a silent «done» falls back to the next request
format, and when every format stays silent the replies are reported."""
import asyncio
import base64
import json

import pytest

from server.config import Config
from server.tts import RespeecherTTS, TTSError

PCM = base64.b64encode(b"\x01\x00" * 4).decode()
CHUNK, DONE = {"type": "chunk", "data": PCM}, {"type": "done"}


class FakeWS:
    """The provider: `script(messages sent for this context so far)` returns the replies to give now."""

    class state:  # noqa: N801
        name = "OPEN"

    def __init__(self, script):
        self.script, self.sent, self._q, self._by_ctx = script, [], asyncio.Queue(), {}

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        ctx_msgs = self._by_ctx.setdefault(msg["context_id"], [])
        ctx_msgs.append(msg)
        for r in self.script(ctx_msgs) or []:
            self._q.put_nowait(json.dumps({**r, "context_id": msg["context_id"]}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._q.get()

    async def close(self): ...


def run(script):
    ws = FakeWS(script)

    class T(RespeecherTTS):
        async def _open(self):
            return ws

    tts = T(Config(respeecher_api_key="k"), "v")

    async def go():
        try:
            return [c async for c in tts.synthesize("Привіт.")]
        finally:
            await tts.close()

    return asyncio.run(go()), ws


def test_audio_is_streamed_from_a_single_request():
    out, ws = run(lambda msgs: [CHUNK, CHUNK, DONE])
    assert out == [b"\x01\x00" * 4] * 2
    assert len(ws.sent) == 1 and ws.sent[0]["continue"] is False


def test_silent_done_falls_back_to_the_streaming_form():
    # a provider that only speaks once it gets the closing empty chunk
    out, ws = run(lambda msgs: [DONE] if len(msgs) == 1 and msgs[0]["continue"] is False else ([CHUNK, DONE] if len(msgs) == 2 else []))
    assert out == [b"\x01\x00" * 4]
    assert len(ws.sent) == 3  # one failed request, then the two-message form


def test_every_format_silent_is_an_error_that_shows_the_replies():
    with pytest.raises(TTSError) as e:
        run(lambda msgs: [{"type": "flush_done", "note": "x"}, DONE] if msgs[-1]["continue"] is False else [])
    assert "no audio" in str(e.value) and "flush_done" in str(e.value) and "2 request formats" in str(e.value)


def test_the_working_format_is_remembered_so_the_failed_one_is_not_retried():
    tts_ws = {}

    def script(msgs):  # the one-message form is answered with a silent done, the two-message form works
        if len(msgs) == 1 and msgs[0]["continue"] is False:
            return [DONE]
        return [CHUNK, DONE] if len(msgs) == 2 else []

    ws = FakeWS(script)

    class T(RespeecherTTS):
        async def _open(self):
            return ws

    tts = T(Config(respeecher_api_key="k"), "v")

    async def go():
        try:
            await _collect(tts), await _collect(tts)
        finally:
            await tts.close()

    async def _collect(t):
        return [c async for c in t.synthesize("Привіт.")]

    asyncio.run(go())
    # first sentence: 1 failed request + 2 messages; second sentence: only the 2 messages
    assert len(ws.sent) == 1 + 2 + 2
    del tts_ws


# ---------------------------------------------------------------------- one generation for the whole answer


def test_respeecher_one_generation_for_several_sentences_and_a_closing_chunk():
    def script(msgs):
        last = msgs[-1]
        if not last["continue"]:
            return [CHUNK, DONE]
        return [CHUNK] if len(msgs) == 1 else []

    ws = FakeWS(script)

    class T(RespeecherTTS):
        async def _open(self):
            return ws

    async def go():
        tts = T(Config(respeecher_api_key="k"), "v")
        st = tts.open_stream()
        got = []

        async def read():
            async for c in st:
                got.append(c)

        reader = asyncio.create_task(read())
        await st.write("Перше.")
        await st.write("Друге.")
        await st.finish()
        await reader
        await tts.close()
        return got

    got = asyncio.run(go())
    assert len(got) == 2  # the first chunk came before the second sentence was even written
    assert [(m["transcript"], m["continue"]) for m in ws.sent] == [("Перше. ", True), ("Друге. ", True), ("", False)]
    assert len({m["context_id"] for m in ws.sent}) == 1  # one generation


def test_cancelling_a_stream_cancels_the_whole_generation_at_the_provider():
    ws = FakeWS(lambda msgs: [])

    class T(RespeecherTTS):
        async def _open(self):
            return ws

    async def go():
        tts = T(Config(respeecher_api_key="k"), "v")
        await tts.connect()
        st = tts.open_stream()
        await st.write("Довга відповідь.")
        await st.cancel()
        await tts.close()

    asyncio.run(go())
    assert ws.sent[-1] == {"context_id": ws.sent[0]["context_id"], "cancel": True}


def test_soniox_text_goes_in_several_messages_and_text_end_comes_last():
    from server.tts import SonioxTTS

    sent = []

    class WS:
        class state:  # noqa: N801
            name = "OPEN"

        def __init__(self):
            self.q = asyncio.Queue()

        async def send(self, raw):
            m = json.loads(raw)
            sent.append(m)
            if m.get("text_end"):
                self.q.put_nowait(json.dumps({"stream_id": m["stream_id"], "audio": PCM, "audio_end": True}))
                self.q.put_nowait(json.dumps({"stream_id": m["stream_id"], "terminated": True}))

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.q.get()

        async def close(self): ...

    ws = WS()

    class T(SonioxTTS):
        async def _open(self):
            return ws

    async def go():
        tts = T(Config(soniox_api_key="k"), "Taras")
        st = tts.open_stream()
        await st.write("[warm] Привіт.")
        await st.write("Як справи?")
        await st.finish()
        out = [c async for c in st]
        await tts.close()
        return out

    assert len(asyncio.run(go())) == 1
    configs = [m for m in sent if "model" in m]
    assert len(configs) == 1 and configs[0]["voice"] == "Taras"  # configured once, for the whole answer
    texts = [(m["text"], m["text_end"]) for m in sent if "text" in m]
    assert texts == [("[warm] Привіт. ", False), ("Як справи? ", False), ("", True)]
