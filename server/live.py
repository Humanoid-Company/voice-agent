"""GPT-Live as the ears and the brain, the TTS (Soniox / Respeecher) as the voice.

GPT-Live is a protocol of its own (wss://api.openai.com/v1/live/sessions, not v1/realtime). It listens to the microphone
continuously and decides itself when to speak: there is no «response.create», no end-of-response event, no item ids on the
transcripts, and no way to switch its own voice off (the session has only `audio.output.voice`), so its audio is generated
and thrown away; the voice the user hears is ours. What we take from it is the transcript of what it says
(`session.output_transcript.delta`: text with start_ms / end_ms on the session timeline).

That text is cut into utterances, each one TTS generation that the words are written to as they arrive:
- a pause on the timeline longer than LIVE_GAP_MS between two fragments starts a new utterance;
- no new fragment for LIVE_IDLE_FLUSH_MS ends the current one (the rest of the text is sent at once);
- the user's words (`session.input_transcript.delta`) over her voice interrupt her: the utterance is cancelled at the
  provider, its late audio is never sent (it carries the id of an utterance that is no longer current), and the page
  is told to clear its player.

The class has the same face as VoiceSession towards server/app.py: start / close / on_audio / on_client. Tools are not here
(GPT-Live does them through delegation): the agent has no internet and no calendar in this mode.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import websockets

from .config import Config
from .interrupt_intent import classify_interjection, strip_echo
from .persona import Persona, now_text
from .prompt import build_stable_prompt, greeting_note, turn_note
from .russian import is_russian
from .sentences import SentenceSplitter
from .tags import strip_tags
from .tts import TTSError
from .usage import Usage

log = logging.getLogger("live")

_HISTORY_ITEMS = 30  # of the conversation so far, given to a new session (it takes at most 128 messages)
_START_TIMEOUT_S = 15.0
_CLOSE_TIMEOUT_S = 2.0
_BARGE_TAIL_S = 0.3  # she counts as sounding this long after her audio stopped
_USER_PHRASE_GAP_S = 1.5  # the user's words this far apart are two phrases
_USER_FINAL_S = 1.2  # this much quiet from the user: what was said is final on the page
_TAG_MAX = 24  # a «[» that is not closed within this many characters is not a tag
_RECENT_CHARS = 400

Handler = Callable[..., Awaitable[None]]


def uses_live(cfg: Config) -> bool:
    """REALTIME_MODEL names a GPT-Live model: it speaks the Live protocol, not v1/realtime."""
    return cfg.realtime_model.lower().startswith("gpt-live")


class _Conn:
    """The WebSocket reduced to what is used here: send a dict, iterate dicts, close."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send(self, event: dict) -> None:
        await self._ws.send(json.dumps(event))

    async def __aiter__(self):
        async for raw in self._ws:
            yield json.loads(raw)

    async def close(self) -> None:
        await self._ws.close()


async def ws_connect(cfg: Config) -> _Conn:
    ws = await websockets.connect(
        cfg.live_url, additional_headers={"Authorization": f"Bearer {cfg.openai_api_key}"}, max_size=None, compression=None
    )
    return _Conn(ws)


class _Chunker:
    """Whole words only: a fragment of the model's text may end in the middle of a word or of a [tag], and neither may reach
    the voice cut in two."""

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, delta: str) -> str:
        self.buf += delta
        limit = len(self.buf)
        open_at = self.buf.rfind("[")
        if open_at != -1 and "]" not in self.buf[open_at:] and len(self.buf) - open_at <= _TAG_MAX:
            limit = open_at  # an open tag: it waits for its end
        cut = max((m.end() for m in re.finditer(r"\s+", self.buf[:limit])), default=0)
        out, self.buf = self.buf[:cut], self.buf[cut:]
        return out

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        return out


@dataclass
class _Utterance:
    id: int
    stream: Any
    t_first_delta: float
    chunker: _Chunker = field(default_factory=_Chunker)
    detect: SentenceSplitter = field(default_factory=SentenceSplitter)  # only to spot Russian
    clean: list[str] = field(default_factory=list)  # what the page shows
    end_ms: int = 0
    ended: bool = False  # no more text will be written to it
    blocked: bool = False  # a sentence of it was Russian: the rest is not said
    reader: asyncio.Task | None = None  # reads the provider's audio into `audio`
    audio: asyncio.Queue = field(default_factory=asyncio.Queue)  # bytes, an Exception, or None at the end
    cancelled: bool = False
    t_first_write: float | None = None
    t_first_audio: float | None = None


class LiveBridge:
    def __init__(
        self,
        *,
        tts: Any,
        send_json: Callable[[dict], Awaitable[None]],
        send_bytes: Callable[[bytes], Awaitable[None]],
        cfg: Config | None = None,
        provider: str = "soniox",
        voice: str = "",
        persona: Persona | None = None,
        tts_factory: Callable[[str, str, str], Any] | None = None,
        history: list[dict] | None = None,
        greet: bool = True,
        about_source: Any = None,
        connect: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.cfg = cfg or Config()
        self.tts = tts
        self.provider, self.voice = provider, voice
        self.persona = persona or Persona()
        self._tts_factory = tts_factory
        self._tts_language = self.persona.language
        self._send_json, self._send_bytes = send_json, send_bytes
        self._about_source = about_source
        self._connect = connect or (lambda: ws_connect(self.cfg))
        self._greet = greet
        self.history: list[dict] = [h for h in (history or []) if isinstance(h, dict)]
        self.realtime = True
        self.state = "listening"
        self.paused = False
        self.usage = Usage(live=True)

        self._conn: Any = None
        self._reader: asyncio.Task | None = None
        self._started = asyncio.Event()
        self._server_seconds = 0.0  # the session's own cumulative count of voice seconds

        self._utt: _Utterance | None = None  # the one text is being written to
        self._utt_seq = 0
        # Utterances are spoken one after another (the provider is slower than the text), by one sender. After an
        # interruption every utterance up to this id is stale: whatever audio of it arrives is dropped, never sent.
        self._stale_upto = 0
        self._open: dict[int, _Utterance] = {}  # not yet fully sent to the page
        self._order: asyncio.Queue[_Utterance] = asyncio.Queue()
        self._sender: asyncio.Task | None = None
        self._idle: asyncio.Task | None = None
        self._cutoff_ms = 0  # what the model had said when she was interrupted: the rest of it is not said
        self._last_end_ms = 0
        self._awaiting_played = False
        self._last_audio_at = 0.0
        self._recent = ""
        self._pending: dict | None = None  # a new voice, taken over at the next utterance

        self._user_buf = ""
        self._user_at = 0.0
        self._user_final: asyncio.Task | None = None

        self._active_s = 0.0
        self._active_since: float | None = time.monotonic()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.tts.connect()
        await self._open_live()
        self._sender = asyncio.create_task(self._send_audio())
        await self._j(
            {
                "type": "ready",
                "engine": "realtime",
                "sample_rate": self.tts.sample_rate,
                "name": self.persona.name,
                "language": self.persona.language,
                "recognition": self.persona.recognition,
                "character": self.persona.character,
                "emotion": self.persona.emotion,
            }
        )
        await self._j({"type": "state", "state": "listening"})

    async def close(self) -> None:
        self._cancel_tasks()
        for utt in list(self._open.values()):
            await self._cancel_utterance(utt)
        if self._sender is not None:
            self._sender.cancel()
            await asyncio.gather(self._sender, return_exceptions=True)
            self._sender = None
        await self._close_live()
        await self.tts.close()

    def _cancel_tasks(self) -> None:
        for task in (self._idle, self._user_final):
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
        self._idle = self._user_final = None

    async def _instructions(self) -> str:
        about = ""
        if self._about_source is not None:
            with contextlib.suppress(Exception):  # the knowledge must never stop a call
                about = await self._about_source.get()
        # Instructions are fixed for the whole session; there is no per-turn note, so the day and the time are as of the start.
        text = build_stable_prompt(search=False, persona=self.persona, about=about, provider=self.provider)
        return f"{text}\n\n{turn_note(now=now_text(self.cfg.timezone))}"

    async def _open_live(self) -> None:
        self._started.clear()
        self._conn = await self._connect()
        self._reader = asyncio.create_task(self._read())
        items: list[dict] = []
        for h in self.history[-_HISTORY_ITEMS:]:
            text = str(h.get("content") or "").strip()
            if text and h.get("role") == "user":
                items.append({"role": "user", "content": [{"type": "input_text", "text": text}]})
            elif text and h.get("role") == "assistant":
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
        if self._greet:
            note = greeting_note(returning=bool(self.history))
            items.append({"role": "developer", "content": [{"type": "input_text", "text": note.strip()}]})
            self._greet = False
        session: dict = {
            "model": self.cfg.realtime_model,
            "instructions": await self._instructions(),
            "audio": {"format": {"type": "audio/pcm", "rate": 24000}},
        }
        if items:
            session["input"] = items
        await self._send({"type": "session.start", "session": session})
        try:
            await asyncio.wait_for(self._started.wait(), _START_TIMEOUT_S)
        except TimeoutError:
            raise RuntimeError("GPT-Live не відповів на session.start") from None

    async def _close_live(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._send({"type": "session.close"})
                await asyncio.sleep(0)
            reader, self._reader = self._reader, None
            if reader is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(reader), _CLOSE_TIMEOUT_S)  # session.closed carries the usage
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None

    async def _send(self, event: dict) -> None:
        if self._conn is not None:
            await self._conn.send(event)

    # ------------------------------------------------------------------ the page

    async def on_audio(self, pcm: bytes) -> None:
        """Microphone sound from the page (through the echo gate): PCM16, 24 kHz, mono: what the session was started with."""
        if self._conn is not None and pcm and not self.paused and self._started.is_set():
            await self._send({"type": "session.input_audio.append", "audio": base64.b64encode(pcm).decode()})

    async def on_client(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "heard":  # the browser's recognizer: only a stop word matters here (the subtitles are GPT-Live's)
            text = str(msg.get("text") or "").strip()
            if text and self._sounding() and classify_interjection(text, assistant_recent=self._recent) in ("stop", "takeover"):
                log.info("barge-in (browser heard): %r", text)
                await self._barge("stop", text)
        elif kind == "interrupt":
            by = str(msg.get("by"))
            text = str(msg.get("text") or "")
            if by == "sound" and classify_interjection(text, assistant_recent=self._recent) in ("backchannel", "echo"):
                return
            await self._barge(by if by in ("button", "sound") else "button", text)
        elif kind == "played":
            self._last_audio_at = time.monotonic()
            if self._awaiting_played and not self._open:
                self._awaiting_played = False
                await self._set_state("listening")
        elif kind == "pause":
            await self.pause()
        elif kind == "resume":
            await self.resume()
        elif kind == "voice":
            await self.request_voice(str(msg.get("provider") or self.provider), str(msg.get("voice") or ""))
        elif kind == "history":  # another conversation, or a new one: a new session seeded with it
            await self._barge("history", "")
            self.history = [h for h in (msg.get("history") or []) if isinstance(h, dict)]
            await self._close_live()
            await self._open_live()
        elif kind == "style":
            log.info("a character or an emotion change is ignored: GPT-Live's instructions are fixed for the session")

    async def pause(self) -> None:
        if self.paused:
            return
        await self._barge("pause", "")
        self.paused = True
        if self._active_since is not None:
            self._active_s += time.monotonic() - self._active_since
            self._active_since = None
        await self._send({"type": "session.input_audio.mute"})
        await self._set_state("paused")
        await self._send_usage()

    async def resume(self) -> None:
        if not self.paused:
            return
        self.paused = False
        self._active_since = time.monotonic()
        await self._send({"type": "session.input_audio.unmute"})
        await self._set_state("listening")

    async def request_voice(self, provider: str, voice: str) -> None:
        self._pending = {"provider": provider, "voice": voice}
        if not self._open:
            await self._apply_pending()

    async def _apply_pending(self) -> None:
        pending, self._pending = self._pending, None
        if not pending or self._tts_factory is None:
            return
        provider, voice = pending["provider"], pending["voice"] or self.voice
        if (provider, voice) == (self.provider, self.voice):
            return
        try:
            new = self._tts_factory(provider, voice, self._tts_language)
            await new.connect()
        except Exception as e:
            log.exception("voice change failed")
            await self._j({"type": "error", "message": f"Не вдалося змінити голос: {e}"})
            return
        old, self.tts = self.tts, new
        self.provider, self.voice = provider, voice
        await old.close()
        await self._j(
            {"type": "voice", "provider": provider, "voice": voice, "sample_rate": new.sample_rate, "language": self._tts_language}
        )

    # ------------------------------------------------------------------ GPT-Live's events

    async def _read(self) -> None:
        try:
            async for ev in self._conn:
                await self._dispatch(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("live connection lost")
            await self._j({"type": "error", "message": f"Зв'язок із GPT-Live обірвано: {type(e).__name__}"})
            await self._barge("error", "")
            return

    async def _dispatch(self, ev: dict) -> None:
        kind = ev.get("type", "")
        if kind == "session.started":
            self._started.set()
        elif kind == "session.output_transcript.delta":
            await self._on_transcript(str(ev.get("delta") or ""), int(ev.get("start_ms") or 0), int(ev.get("end_ms") or 0))
        elif kind == "session.input_transcript.delta":
            await self._on_user_delta(str(ev.get("delta") or ""))
        elif kind == "session.usage.updated":
            seconds = (ev.get("usage") or {}).get("seconds")
            if isinstance(seconds, (int, float)):
                self._server_seconds = float(seconds)  # cumulative: the latest figure, not a sum
        elif kind == "session.closed":
            seconds = ((ev.get("usage") or {}).get("seconds"))
            if isinstance(seconds, (int, float)):
                self._server_seconds = float(seconds)
        elif kind == "session.delegation.created":
            log.warning("GPT-Live delegated a task, but tools are not implemented here: %s", ev.get("delegation"))
        elif kind == "error":
            err = ev.get("error") or {}
            log.warning("live error: %s %s", err.get("code"), err.get("message"))
            await self._j({"type": "error", "message": str(err.get("message") or "помилка GPT-Live")})
        # session.output_audio.delta: its own voice, not played (nothing else is done with it)

    # ------------------------------------------------------------------ what she says: utterances

    async def _on_transcript(self, delta: str, start_ms: int, end_ms: int) -> None:
        if not delta or start_ms < self._cutoff_ms:  # empty, or what she was saying when she was cut off
            return
        self._last_end_ms = max(self._last_end_ms, end_ms)
        utt = self._utt
        if utt is not None and not utt.ended and start_ms - utt.end_ms > self.cfg.live_gap_ms:
            await self._end_utterance(utt)  # a pause on the timeline: a new utterance
        utt = self._utt
        if utt is None or utt.ended:
            utt = await self._begin_utterance()
        utt.end_ms = max(utt.end_ms, end_ms)
        self._cancel_idle()
        await self._feed(utt, delta)
        if not utt.ended:
            self._idle = asyncio.create_task(self._idle_flush(utt))

    async def _begin_utterance(self) -> _Utterance:
        await self._apply_pending()
        self._utt_seq += 1
        utt = _Utterance(id=self._utt_seq, stream=self.tts.open_stream(), t_first_delta=time.monotonic())
        utt.reader = asyncio.create_task(self._collect(utt))
        self._utt = utt
        self._open[utt.id] = utt
        await self._order.put(utt)
        await self._set_state("speaking")
        return utt

    def _cancel_idle(self) -> None:
        if self._idle is not None and not self._idle.done() and self._idle is not asyncio.current_task():
            self._idle.cancel()
        self._idle = None

    async def _idle_flush(self, utt: _Utterance) -> None:
        await asyncio.sleep(self.cfg.live_idle_flush_ms / 1000)
        await self._end_utterance(utt)

    async def _feed(self, utt: _Utterance, delta: str) -> None:
        if utt.blocked:
            return
        for sentence in utt.detect.feed(delta):  # the Russian guard: a whole sentence is judged, as everywhere else
            if is_russian(sentence):
                log.warning("Russian from GPT-Live: the utterance is cut: %r", sentence[:80])
                utt.blocked = True
                await self._cancel_utterance(utt)
                return
        text = utt.chunker.feed(delta)
        if text:
            await self._emit(utt, text)

    async def _emit(self, utt: _Utterance, text: str) -> None:
        clean = strip_tags(text)
        if clean:
            clean += text[len(text.rstrip()):]  # the space that ends a word is kept: the next piece follows it
        voiced = text if self.provider == "soniox" else clean  # only Soniox understands the tags
        if voiced.strip():
            if utt.t_first_write is None:
                utt.t_first_write = time.monotonic()
            self.usage.chars[self.provider] = self.usage.chars.get(self.provider, 0) + len(voiced)
            await utt.stream.write(voiced)
        if clean.strip():
            utt.clean.append(clean)
            self._recent = (self._recent + clean)[-_RECENT_CHARS:]
            await self._j({"type": "assistant_delta", "text": clean})

    async def _end_utterance(self, utt: _Utterance) -> None:
        """No more text for it: the rest of the buffer goes out and the generation is told it is complete."""
        if utt.ended:
            return
        utt.ended = True
        self._cancel_idle()
        if not utt.blocked:
            rest = utt.chunker.flush()
            if rest:
                await self._emit(utt, rest)
            if utt.t_first_write is not None:
                await utt.stream.finish()
            else:
                await self._cancel_utterance(utt)  # nothing was written: no generation to wait for
        text = "".join(utt.clean).strip()
        if text:
            await self._j({"type": "record", "role": "assistant", "text": text})
            self.history.append({"role": "assistant", "content": text})

    async def _collect(self, utt: _Utterance) -> None:
        """The provider's audio of one utterance into its queue (it may be ready long before it is its turn to be sent)."""
        try:
            async for chunk in utt.stream:
                if utt.t_first_audio is None:
                    utt.t_first_audio = time.monotonic()
                    await self._report_timing(utt)
                utt.audio.put_nowait(chunk)
        except asyncio.CancelledError:
            raise
        except TTSError as e:
            utt.audio.put_nowait(e)
        finally:
            utt.audio.put_nowait(None)

    def _stale(self, utt: _Utterance) -> bool:
        return utt.cancelled or utt.id <= self._stale_upto

    async def _send_audio(self) -> None:
        """The one place audio goes to the page: utterances in order, and nothing of a stale one."""
        while True:
            utt = await self._order.get()
            sent = False
            while True:
                chunk = await utt.audio.get()
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    log.warning("the voice failed: %s", chunk)
                    if not self._stale(utt):
                        await self._j({"type": "error", "message": f"Голос: {chunk}"})
                    break
                if self._stale(utt):  # a tail of an utterance that was cut off: never played
                    continue
                sent = True
                self._last_audio_at = time.monotonic()
                await self._send_bytes(chunk)
            self._open.pop(utt.id, None)
            if self._utt is utt:
                self._utt = None
            if self._stale(utt):
                continue
            if sent:
                self._awaiting_played = True
                await self._j({"type": "turn_end"})
                await self._send_usage()
            elif not self._open:
                await self._set_state("listening")

    async def _report_timing(self, utt: _Utterance) -> None:
        """Three moments of an utterance and what lies between them."""
        t0, write, audio = utt.t_first_delta, utt.t_first_write, utt.t_first_audio
        if write is None or audio is None:
            return
        ms = {
            "first_delta": 0,
            "first_write": round((write - t0) * 1000),
            "tts_audio": round((audio - t0) * 1000),
        }
        log.info(
            "live utterance %d: first text → sent to the voice %d ms, → first audio %d ms (%d ms in all)",
            utt.id, ms["first_write"], ms["tts_audio"] - ms["first_write"], ms["tts_audio"],
        )
        await self._j({"type": "timing", "ms": ms})

    async def _cancel_utterance(self, utt: _Utterance) -> None:
        """Stop it at the provider, and let nothing of it reach the page."""
        utt.cancelled = utt.ended = True
        self._cancel_idle()
        with contextlib.suppress(Exception):
            await utt.stream.cancel()  # the provider stops generating the rest
        if utt.reader is not None and utt.reader is not asyncio.current_task():
            utt.reader.cancel()
            await asyncio.gather(utt.reader, return_exceptions=True)
        if self._utt is utt:
            self._utt = None
        if utt.id in self._open and utt.audio.empty():
            utt.audio.put_nowait(None)  # the sender may be waiting for it

    # ------------------------------------------------------------------ what the user says, and interruptions

    def _sounding(self) -> bool:
        """Her voice is audible now: the page is still playing, or it stopped a moment ago."""
        return self.state == "speaking" or time.monotonic() - self._last_audio_at < _BARGE_TAIL_S

    async def _on_user_delta(self, delta: str) -> None:
        if not delta or self.paused:
            return
        now = time.monotonic()
        if now - self._user_at > _USER_PHRASE_GAP_S:
            self._user_buf = ""  # a new phrase
        self._user_at = now
        self._user_buf += delta
        mine = strip_echo(self._user_buf, self._recent) if self._sounding() else self._user_buf
        if self._sounding() and mine.strip():
            intent = classify_interjection(mine, assistant_recent=self._recent)
            if intent not in ("backchannel", "echo") and (
                intent == "stop" or len(re.findall(r"\w+", mine)) >= 2 or len(mine.strip()) >= self.cfg.live_barge_min_chars
            ):
                log.info("barge-in (GPT-Live heard the user over her): %r", mine)
                await self._barge("takeover", mine.strip())
        if mine.strip():
            await self._j({"type": "user", "text": mine.strip(), "final": False})
            if self._user_final is not None and not self._user_final.done():
                self._user_final.cancel()
            self._user_final = asyncio.create_task(self._finish_user_phrase())

    async def _finish_user_phrase(self) -> None:
        await asyncio.sleep(_USER_FINAL_S)
        text = self._user_buf.strip()
        if text:
            self.history.append({"role": "user", "content": text})
            await self._j({"type": "user", "text": text, "final": True})
        self._user_buf = ""

    async def _barge(self, reason: str, text: str) -> None:
        """Cut her off: the utterance is cancelled at the provider, its late audio is dropped, the player is cleared."""
        if not self._open and not self._awaiting_played and self.state != "speaking":
            return
        self._cutoff_ms = self._last_end_ms
        self._stale_upto = self._utt_seq  # whatever audio of the utterances so far is still on its way is dropped
        for utt in list(self._open.values()):
            await self._cancel_utterance(utt)
        self._awaiting_played = False
        await self._j({"type": "clear", "reason": reason, "text": text})
        await self._set_state("paused" if self.paused else "listening")

    # ------------------------------------------------------------------ helpers

    def _active_seconds(self) -> float:
        running = time.monotonic() - self._active_since if self._active_since is not None else 0.0
        return self._active_s + running

    async def _send_usage(self) -> None:
        u = self.usage
        u.live_seconds = self._server_seconds or self._active_seconds()
        await self._j(
            {
                "type": "usage",
                "seconds": int(self._active_seconds()),
                "llm_in": 0,
                "llm_out": 0,
                "searches": 0,
                "tts_chars": sum(u.chars.values()),
                "cost": round(u.cost(self.cfg), 5),
            }
        )

    async def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            await self._j({"type": "state", "state": state})

    async def _j(self, msg: dict) -> None:
        with contextlib.suppress(Exception):  # the browser is gone; close() follows
            await self._send_json(msg)
