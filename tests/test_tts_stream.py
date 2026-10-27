"""synthesize() against a fake WebSocket: audio is streamed, and a silent end is reported with the replies."""
import asyncio
import base64
import json

import pytest

from server.config import Config
from server.tts import RespeecherTTS, TTSError


class FakeWS:
    """Answers the generation request with canned replies, like the provider."""

    class state:  # noqa: N801
        name = "OPEN"

    def __init__(self, replies):
        self.replies, self.sent, self._q = replies, [], asyncio.Queue()

    async def send(self, raw):
        self.sent.append(json.loads(raw))
        if len(self.sent) == 2:  # the whole request (text + final empty chunk) is in
            ctx = self.sent[0]["context_id"]
            for r in self.replies:
                self._q.put_nowait(json.dumps({**r, "context_id": ctx}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._q.get()

    async def close(self): ...


def make(replies):
    class T(RespeecherTTS):
        async def _open(self):
            return FakeWS(replies)

    return T(Config(respeecher_api_key="k"), "v")


async def collect(tts):
    try:
        return [c async for c in tts.synthesize("Привіт.")]
    finally:
        await tts.close()


def test_audio_chunks_are_streamed_until_done():
    pcm = base64.b64encode(b"\x01\x00" * 4).decode()
    out = asyncio.run(collect(make([{"type": "chunk", "data": pcm}, {"type": "chunk", "data": pcm}, {"type": "done"}])))
    assert out == [b"\x01\x00" * 4] * 2


def test_end_without_audio_is_an_error_that_shows_the_replies():
    with pytest.raises(TTSError) as e:
        asyncio.run(collect(make([{"type": "flush_done", "note": "x"}, {"type": "done"}])))
    assert "no audio" in str(e.value) and "flush_done" in str(e.value) and "done" in str(e.value)
