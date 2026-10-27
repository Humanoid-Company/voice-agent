"""One call: the whole listen -> think -> speak loop and barge-in.

    browser (Google speech recognition, free) --text--> LLM (stream) --sentences--> TTS --PCM16--> browser

Listening happens in the browser (Web Speech API): it sends interim and final text, nothing else.
A final result is the end of the user's utterance.

States: listening -> thinking (LLM running, nothing audible yet) -> speaking (audio is being sent or
still playing in the browser) -> listening. The browser tells us when it has played everything
(`played`), because the audio is buffered there for longer than we generate it.

Barge-in: while thinking/speaking, everything the user says is classified (`classify_interjection`).
A stop word or the user clearly taking the turn cancels the LLM and TTS and tells the browser to
drop its audio queue. "Yes-yes", laughter, the assistant's own voice coming back through the mic:
ignored, and that utterance is not answered.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .interrupt_intent import classify_interjection
from .sentences import SentenceSplitter

log = logging.getLogger(__name__)

_ECHO_TAIL_S = 3.0  # right after the assistant stopped, its tail may still come back through the mic


class VoiceSession:
    def __init__(
        self,
        *,
        llm: Any,
        tts: Any,
        send_json: Callable[[dict], Awaitable[None]],
        send_bytes: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self.llm = llm
        self.tts = tts
        self._send_json = send_json
        self._send_bytes = send_bytes

        self.history: list[dict] = []
        self.state = "listening"
        self._turn: asyncio.Task | None = None
        self._lock = asyncio.Lock()

        # the utterance the user is speaking right now
        self._final = ""
        self._partial = ""
        self._during_turn = False  # started while the assistant was busy
        self._skip_reply = False  # user said "stop": do not answer that utterance

        # the current/last assistant turn
        self._spoken: list[str] = []
        self._turn_done = False
        self._awaiting_played = False
        self._recent = ""  # what the assistant said lately, for echo detection
        self._last_audio_at = 0.0

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.tts.connect()
        await self._j({"type": "ready", "sample_rate": self.tts.sample_rate})
        await self._set_state("listening")

    async def close(self) -> None:
        if self._turn and not self._turn.done():
            self._turn.cancel()
        await self.tts.close()

    # ------------------------------------------------------------------ input

    async def on_client(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "heard":
            await self.on_heard(str(msg.get("text", "")), bool(msg.get("final")))
        elif kind == "interrupt":
            await self.interrupt()
        elif kind == "played" and self._awaiting_played:
            self._awaiting_played = False
            self._last_audio_at = time.monotonic()
            await self._set_state("listening")

    async def on_heard(self, text: str, final: bool) -> None:
        """What the browser's speech recognition heard: interim text, or the final text of an utterance."""
        text = text.strip()
        self._final, self._partial = (text, "") if final else ("", text)
        if text:
            await self._j({"type": "user", "text": text, "final": False})
            if self.state in ("thinking", "speaking"):
                self._during_turn = True
                intent = classify_interjection(text, assistant_recent=self._recent)
                if intent in ("stop", "takeover"):
                    log.info("barge-in (%s): %r", intent, text)
                    await self.interrupt()
                    self._skip_reply = intent == "stop"
        if final:
            await self._on_endpoint()

    async def _on_endpoint(self) -> None:
        text = self._final.strip()
        during, skip = self._during_turn, self._skip_reply
        self._final = self._partial = ""
        self._during_turn = self._skip_reply = False
        if not text:
            return
        busy = self.state in ("thinking", "speaking")
        recently = time.monotonic() - self._last_audio_at < _ECHO_TAIL_S
        echo = classify_interjection(text, assistant_recent=self._recent) == "echo"
        if skip or (during and busy) or (echo and (busy or recently)):
            await self._j({"type": "user_drop"})
            return
        await self._j({"type": "user", "text": text, "final": True})
        self.history.append({"role": "user", "content": text})
        async with self._lock:
            self._turn = asyncio.create_task(self._run_turn())

    # ------------------------------------------------------------------ turn

    async def _run_turn(self) -> None:
        self._spoken, self._turn_done = [], False
        await self._set_state("thinking")
        splitter = SentenceSplitter()
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        full: list[str] = []

        async def produce() -> None:
            try:
                async for delta in self.llm.stream(self.history):
                    full.append(delta)
                    self._recent = "".join(full)[-400:]
                    await self._j({"type": "assistant_delta", "text": delta})
                    for s in splitter.feed(delta):
                        sentences.put_nowait(s)
                for s in splitter.flush():
                    sentences.put_nowait(s)
            finally:
                sentences.put_nowait(None)

        producer = asyncio.create_task(produce())
        try:
            while (sentence := await sentences.get()) is not None:
                await self._set_state("speaking")
                async for chunk in self.tts.synthesize(sentence):
                    self._last_audio_at = time.monotonic()
                    await self._send_bytes(chunk)
                self._spoken.append(sentence)
            await producer  # surfaces an LLM error
            self._turn_done = True
            self.history.append({"role": "assistant", "content": "".join(full).strip()})
            if self._spoken:
                self._awaiting_played = True
                await self._j({"type": "turn_end"})
            else:
                await self._set_state("listening")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("turn failed")
            await self._j({"type": "error", "message": f"{type(e).__name__}: {e}"})
            await self._j({"type": "clear"})
            self._awaiting_played = False
            await self._set_state("listening")
        finally:
            producer.cancel()

    async def interrupt(self) -> None:
        async with self._lock:
            task, self._turn = self._turn, None
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if self.state == "listening" and not self._awaiting_played:
                return
            if not self._turn_done and self._spoken:
                # keep only what the user actually got to hear
                self.history.append({"role": "assistant", "content": " ".join(self._spoken) + " …"})
            self._turn_done = True
            self._awaiting_played = False
            await self._j({"type": "clear"})
            await self._set_state("listening")

    # ------------------------------------------------------------------ helpers

    async def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            await self._j({"type": "state", "state": state})

    async def _j(self, msg: dict) -> None:
        with contextlib.suppress(Exception):  # the browser is gone; close() follows
            await self._send_json(msg)
