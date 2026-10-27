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
