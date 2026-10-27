"""Streaming text-to-speech: Respeecher and Soniox behind one interface.

Both speak WebSocket and multiplex independent generations over one connection, keyed by an id
(Respeecher `context_id`, Soniox `stream_id`). So the connection is opened once per call and every
sentence is a new generation. Stale audio from a cancelled generation is dropped by its id.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

import websockets

from .config import Config

log = logging.getLogger(__name__)

_CHUNK_TIMEOUT_S = 15.0


class TTSError(RuntimeError):
    pass


class _NoAudio(TTSError):
    """A generation ended without a single audio chunk (not an error reply: the provider said «done»)."""


@dataclass
class _Msg:
    kind: Literal["audio", "end", "error", "info"]
    audio: bytes = b""
    error: str = ""
    raw: str = ""  # the provider's reply with long strings (audio) shortened, for error messages


def _summary(reply: dict) -> str:
    short = {k: (f"<{len(v)} chars>" if isinstance(v, str) and len(v) > 60 else v) for k, v in reply.items()}
    return json.dumps(short, ensure_ascii=False)[:300]


class _MuxTTS:
    """Shared plumbing; subclasses define the URL, the wire messages and how to read a reply."""

    sample_rate: int

    def __init__(self) -> None:
        self._ws: websockets.ClientConnection | None = None
        self._reader: asyncio.Task | None = None
        self._queues: dict[str, asyncio.Queue[_Msg]] = {}
        self._lock = asyncio.Lock()
        self._best = 0  # the request format that last gave audio: tried first, a failed try costs a round trip

    # -- subclass hooks --
    async def _open(self) -> websockets.ClientConnection:
        raise NotImplementedError

    def _variants(self) -> list:
        """Ways to ask for one sentence, tried in order: each is a function (ctx, text) -> messages. A provider
        that ends a generation without audio on one wire format gets the next one."""
        raise NotImplementedError

    def _cancel_message(self, ctx: str) -> dict:
        raise NotImplementedError

    def _route(self, msg: dict) -> tuple[str | None, _Msg | None]:
        """Reply -> (generation id, message); (None, ...) when the reply carries no id."""
        raise NotImplementedError

    # -- public --
    async def connect(self) -> None:
        await self._ensure()

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        tried: list[str] = []
        variants = self._variants()
        for i in sorted(range(len(variants)), key=lambda i: i != self._best):  # the working one first
            build, got_audio = variants[i], False
            try:
                async for chunk in self._generate(build, text):
                    got_audio = True
                    yield chunk
            except _NoAudio as e:
                tried.append(str(e))
                log.warning("%s: %s", type(self).__name__, e)
                continue
            if got_audio:
                self._best = i
                return
        raise TTSError(f"{type(self).__name__} returned no audio. Tried {len(tried)} request formats: {tried}")

    async def _generate(self, build, text: str) -> AsyncIterator[bytes]:
        ws = await self._ensure()
        ctx = uuid.uuid4().hex
        queue: asyncio.Queue[_Msg] = asyncio.Queue()
        self._queues[ctx] = queue
        finished = False
        got_audio = False
        seen: list[str] = []  # the provider's replies, to say what went wrong when no audio comes
        try:
            for m in build(ctx, text):
                await ws.send(json.dumps(m))
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), _CHUNK_TIMEOUT_S)
                except TimeoutError:
                    raise TTSError("tts timeout") from None
                if len(seen) < 6 and (msg.kind != "audio" or not got_audio):
                    seen.append(msg.raw)
                if msg.kind == "error":
                    finished = True
                    raise TTSError(msg.error)
                if msg.kind == "end":
                    finished = True
                    if not got_audio:
                        raise _NoAudio(f"replies {seen}")
                    return
                if msg.audio:
                    got_audio = True
                    yield msg.audio
        finally:
            self._queues.pop(ctx, None)
            if not finished and ws.state.name == "OPEN":
                # Barge-in (or a client that went away): tell the provider to stop generating.
                with contextlib.suppress(Exception):
                    await ws.send(json.dumps(self._cancel_message(ctx)))

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = self._reader = None

    # -- internals --
    async def _ensure(self) -> websockets.ClientConnection:
        async with self._lock:
            if self._ws is None or self._ws.state.name != "OPEN" or (self._reader and self._reader.done()):
                if self._reader:
                    self._reader.cancel()
                self._ws = await self._open()
                self._reader = asyncio.create_task(self._read(self._ws))
            return self._ws

    async def _read(self, ws: websockets.ClientConnection) -> None:
        try:
            async for raw in ws:
                reply = json.loads(raw)
                ctx, msg = self._route(reply)
                if msg is None:
                    log.warning("tts: unrecognized reply %s", _summary(reply))
                    msg = _Msg("info")  # not an error, but shown if the generation ends without audio
                msg.raw = _summary(reply)
                if ctx is None and len(self._queues) == 1:
                    ctx = next(iter(self._queues))
                if ctx in self._queues:
                    self._queues[ctx].put_nowait(msg)
        except websockets.ConnectionClosed:
            pass
        except Exception:
            log.exception("tts reader failed")
        for q in list(self._queues.values()):
            q.put_nowait(_Msg("error", error="tts connection closed"))


class RespeecherTTS(_MuxTTS):
    """wss://api.respeecher.com/v1/public/tts/{ua-rt|en-rt}/tts/websocket

    A generation is: text chunks with `continue: true`, then an empty chunk with `continue: false`.
    Replies: {type: chunk, data: <base64 pcm>}, {type: done}, {type: error, error}.
    """

    def __init__(self, cfg: Config, voice: str) -> None:
        super().__init__()
        self._key = cfg.respeecher_api_key
        self._model = cfg.respeecher_model.strip("/")
        self._voice = voice or cfg.respeecher_voices[0]
        self.sample_rate = cfg.respeecher_sample_rate

    async def _open(self) -> websockets.ClientConnection:
        url = f"wss://api.respeecher.com/v1/{self._model}/tts/websocket?api_key={self._key}"
        return await websockets.connect(url, compression=None, ping_interval=10, max_size=None)

    def _request(self, ctx: str, text: str, cont: bool) -> dict:
        return {
            "transcript": text,
            "continue": cont,
            "context_id": ctx,
            "voice": {"id": self._voice, "sampling_params": {}},
            "output_format": {"encoding": "pcm_s16le", "sample_rate": self.sample_rate},
        }

    def _variants(self) -> list:
        # The whole sentence as the last (and only) chunk; then, if Respeecher says «done» with no audio, the
        # streaming form: the text with `continue: true` and an empty closing chunk.
        return [
            lambda ctx, text: [self._request(ctx, text, False)],
            lambda ctx, text: [self._request(ctx, text, True), self._request(ctx, "", False)],
        ]

    def _cancel_message(self, ctx: str) -> dict:
        return {"context_id": ctx, "cancel": True}

    def _route(self, msg: dict) -> tuple[str | None, _Msg | None]:
        ctx = msg.get("context_id")
        kind = msg.get("type")
        if kind == "chunk":
            return ctx, _Msg("audio", audio=base64.b64decode(msg.get("data", "")))
        if kind == "done":
            return ctx, _Msg("end")
        if kind == "error":
            return ctx, _Msg("error", error=str(msg.get("error", "respeecher error")))
        return ctx, None


class SonioxTTS(_MuxTTS):
    """wss://tts-rt.soniox.com/tts-websocket — config per stream, then text with `text_end`.

    Replies: {audio: <base64>, audio_end}, {terminated: true}, {error_code, error_message}.
    """

    def __init__(self, cfg: Config, voice: str) -> None:
        super().__init__()
        self._cfg = cfg
        self._voice = voice or cfg.soniox_voices[0]
        self.sample_rate = cfg.soniox_tts_sample_rate

    async def _open(self) -> websockets.ClientConnection:
        return await websockets.connect(
            "wss://tts-rt.soniox.com/tts-websocket",
            additional_headers={"Authorization": f"Bearer {self._cfg.soniox_api_key}"},
            max_size=None,
        )

    def _variants(self) -> list:
        return [self._start_messages]

    def _start_messages(self, ctx: str, text: str) -> list[dict]:
        return [
            {
                "stream_id": ctx,
                "model": self._cfg.soniox_tts_model,
                "language": self._cfg.soniox_tts_language,
                "voice": self._voice,
                "audio_format": "pcm_s16le",
                "sample_rate": self.sample_rate,
            },
            {"stream_id": ctx, "text": text, "text_end": True},
        ]

    def _cancel_message(self, ctx: str) -> dict:
        return {"stream_id": ctx, "cancel": True}

    def _route(self, msg: dict) -> tuple[str | None, _Msg | None]:
        ctx = msg.get("stream_id")
        if msg.get("error_code"):
            return ctx, _Msg("error", error=f"{msg.get('error_type')}: {msg.get('error_message')}")
        if msg.get("audio"):  # the last chunk can carry `audio_end: true` together with audio
            return ctx, _Msg("audio", audio=base64.b64decode(msg["audio"]))
        if msg.get("terminated"):  # always follows the last chunk, also after a cancel
            return ctx, _Msg("end")
        return ctx, None


def make_tts(provider: str, voice: str, cfg: Config) -> _MuxTTS:
    if provider == "respeecher" and cfg.respeecher_api_key:
        return RespeecherTTS(cfg, voice)
    if provider == "soniox" and cfg.soniox_api_key:
        return SonioxTTS(cfg, voice)
    raise TTSError(f"TTS provider '{provider}' is not configured")
