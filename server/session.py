"""One call: the whole listen -> think -> speak loop, barge-in, pause, and the voice-driven settings.

    browser (Google speech recognition, free) --text--> LLM (stream) --sentences--> TTS --PCM16--> browser

Listening happens in the browser (Web Speech API): it sends interim and final text, nothing else.
A final result is the end of the user's utterance.

States: listening -> thinking (LLM running, nothing audible yet) -> speaking (audio is being sent or
still playing in the browser) -> listening; paused (nobody is addressed: what is said nearby is only kept).
The browser tells us when it has played everything (`played`), because the audio is buffered there for
longer than we generate it.

Barge-in: while thinking/speaking, everything the user says is classified (`classify_interjection`).
A stop word or the user clearly taking the turn cancels the LLM and TTS and tells the browser to
drop its audio queue. "Yes-yes", laughter, the assistant's own voice coming back through the mic:
ignored, and that utterance is not answered.

The model can change the voice, the assistant's name and the language, or end the call (tools). A new voice or
language takes effect between two sentences, never inside one.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .characters import CHARACTERS, EMOTIONS, tone_factor
from .config import Config
from .heard import heard_part
from .interrupt_intent import classify_interjection, strip_echo
from .llm import SEGMENT_END, TurnContext
from .persona import (
    LANGUAGES,
    SUPPORTED,
    Persona,
    find_language,
    find_voice,
    now_text,
    sanitize_about,
    sanitize_name,
)
from .prompt import build_stable_prompt, greeting_note, turn_note
from .russian import asks_for_russian, is_russian, refusal
from .sentences import SentenceSplitter
from .tags import strip_tags
from .timing import TurnTimer
from .tts import TTSError
from .usage import Usage

log = logging.getLogger(__name__)

# Realtime heard the user go on speaking this long while she was busy: that is an interruption, whatever the browser's
# recognizer managed to hear (it is fed silence through a long answer and restarts, so it is often late or deaf then). Her
# own echo does not count: the microphone Realtime gets is shut while she is audible, and only a louder voice opens it.
_VAD_BARGE_S = 0.6
# An answer is asked for when the user's phrase is IN the Realtime conversation (its item exists), not when the VAD says the
# phrase ended: «speech_stopped» comes first, the item is made a moment later, and an answer asked for in between is made
# without the last phrase, that is to the one before it («відповідає на крок позаду»). If no item event comes, ask anyway.
_ITEM_WAIT_S = 1.0
_ECHO_TAIL_S = 3.0  # right after the assistant stopped, its tail may still come back through the mic
_ECHO_STRIP_S = 8.0  # her last words may be glued to the user's reply for this long after she stopped
_OVERHEARD_MAX = 20  # what is kept from a pause
_OVERHEARD_CHARS = 300


_HISTORY_TURNS = 30  # what the model sees of the conversation
_HISTORY_CHARS = 2000


def sanitize_history(items: object) -> list[dict]:
    """A conversation the page sends (a resumed one): only user/assistant text, bounded. It is data, not instructions."""
    if not isinstance(items, list):
        return []
    clean = [
        {"role": it["role"], "content": it["content"].strip()[:_HISTORY_CHARS]}
        for it in items
        if isinstance(it, dict) and it.get("role") in ("user", "assistant")
        and isinstance(it.get("content"), str) and it["content"].strip()
    ]  # fmt: skip
    return clean[-_HISTORY_TURNS:]


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    params = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


TOOLS = [
    _fn(
        "change_voice",
        "Change the assistant's voice when the user asks for another one by name («змін голос на Ксенію»). "
        "`voice` is the name as the user said it. `provider` only when they name the service.",
        {
            "voice": {"type": "string", "description": "Voice name"},
            "provider": {"type": "string", "enum": ["respeecher", "soniox"]},
        },
        ["voice"],
    ),
    _fn(
        "set_assistant_name",
        "Set the name the assistant answers to, when the user says what to call her («тепер тебе звати Лесі»).",
        {"name": {"type": "string", "description": "The new name, one or two words"}},
        ["name"],
    ),
    _fn(
        "change_language",
        "Change the language the assistant speaks and listens in, only when the user asks for it («говори англійською»).",
        {"language": {"type": "string", "description": "Language name or code: " + ", ".join(LANGUAGES)}},
        ["language"],
    ),
    _fn(
        "change_emotion",
        "Make the delivery calmer or more emotional when the user asks for it («говори емоційніше», «заспокойся», "
        "«говори рівніше»).",
        {"level": {"type": "string", "enum": list(EMOTIONS), "description": "calm, normal or emotional"}},
        ["level"],
    ),
    _fn(
        "end_conversation",
        "End the call when the user says goodbye or asks to finish. Say a short goodbye first.",
        {},
    ),
]


class VoiceSession:
    def __init__(
        self,
        *,
        llm: Any,
        tts: Any,
        send_json: Callable[[dict], Awaitable[None]],
        send_bytes: Callable[[bytes], Awaitable[None]],
        cfg: Config | None = None,
        provider: str = "respeecher",
        voice: str = "",
        persona: Persona | None = None,
        tts_factory: Callable[[str, str, str], Any] | None = None,
        voices_for: Callable[[str, str], Awaitable[list[dict]]] | None = None,
        history: list[dict] | None = None,
        tune: bool = True,
        ears: Any = None,
        greet: bool = False,
        about: str = "",
        about_source: Any = None,
    ) -> None:
        self.llm = ears or llm
        self._greet = greet  # speak first when the call starts
        self.about = sanitize_about(about)  # what the owner wrote about the agent and the place: in every answer's prompt
        self._about_source = about_source  # ...kept in the database for every call and device (about_store.py)
        # Realtime ears: the model hears the microphone itself and tells when the user stopped (see realtime.py);
        # the browser's recognizer then only supplies interim words for barge-in and the on-screen text.
        self.ears = ears
        self.realtime = ears is not None
        self._rt_users: dict[str, dict] = {}  # Realtime audio item id -> the history entry waiting for its transcript
        self._last_interim = ""
        self._last_interim_at = 0.0
        # What was sent to the browser to speak this turn: (sentence, seconds of its audio). When she is cut off the page
        # says how many seconds had really played, and her memory is trimmed to that place (see heard.py).
        self._turn_audio: list[tuple[str, float]] = []
        self._cut: dict | None = None
        self._vad_speaking = False  # Realtime's VAD hears the user right now
        self._t_stop = 0.0  # when Realtime's VAD last heard a phrase end: the start of the answer's timing
        self._timer: TurnTimer | None = None
        self._rates: dict[str, float] = {}  # per provider: seconds of speech per character, learned from finished answers
        self._stream_broken = False  # the voice gave no audio for one generation per answer: sentence by sentence from now on
        self._rt_waiting: dict[str, asyncio.Task] = {}  # phrases to answer, until their item exists (fallback timers)
        # Phrases that ended while she was busy: item id -> the words (None until the transcript comes). Not lost: what is
        # more than a «угу» is answered as soon as she is free (_answer_late).
        self._late: dict[str, str | None] = {}
        self._barge_task: asyncio.Task | None = None
        self.tts = tts
        self.cfg = cfg or Config()
        self.provider, self.voice = provider, voice
        self.persona = persona or Persona()
        self._default_name = self.persona.name  # the plain character's name: kept while another character speaks
        self._tune = tune  # the delivery follows the character and the emotion (the page's switch)
        self._tts_language = self.persona.language  # what the current TTS speaks: changes between sentences
        self._tts_factory = tts_factory
        self._voices_for = voices_for
        self._send_json = send_json
        self._send_bytes = send_bytes

        self.history: list[dict] = sanitize_history(history)  # a resumed conversation starts with its past
        self.state = "listening"
        self._turn: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._tts_lock = asyncio.Lock()
        self._pending: dict | None = None  # voice / provider / language to apply between two sentences
        # A new voice is connected in the background as soon as it is asked for, so that the swap between two
        # sentences is instant and the answer goes on without a pause.
        self._prepared: tuple[tuple[str, str, str], Any] | None = None
        self._prewarm_task: asyncio.Task | None = None
        self._prewarm_key: tuple[str, str, str] | None = None
        self._bg: set[asyncio.Task] = set()

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
        self._end_requested = False  # the model ended the call: say goodbye, then close

        # pause: nobody is addressed; what is said nearby is kept (if the user allows) for later
        self.paused = False
        self.background = True
        self.overheard: list[str] = []

        # the meter
        self.usage = Usage(realtime=self.realtime)
        self._active_s = 0.0
        self._active_since: float | None = time.monotonic()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.tts.connect()
        if self.ears:
            await self._open_ears()
        await self._j(
            {
                "type": "ready",
                "engine": "realtime" if self.realtime else "browser",
                "sample_rate": self.tts.sample_rate,
                "name": self.persona.name,
                "language": self.persona.language,
                "recognition": self.persona.recognition,
                "character": self.persona.character,
                "emotion": self.persona.emotion,
            }
        )
        await self._j({"type": "state", "state": "listening"})  # _set_state would skip it: already "listening"
        await self._retune()
        if self._greet:
            async with self._lock:
                self._turn = asyncio.create_task(self._run_turn(greeting=True))

    async def close(self) -> None:
        if self._turn and not self._turn.done():
            self._turn.cancel()
        self._cancel_barge()
        self._cancel_waiting()
        for task in list(self._bg):
            task.cancel()
        if self._prepared:
            await self._prepared[1].close()
            self._prepared = None
        if self.ears:
            await self.ears.close()
        await self.tts.close()

    # ------------------------------------------------------------------ input

    async def on_client(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "heard":
            await self.on_heard(str(msg.get("text", "")), bool(msg.get("final")))
        elif kind == "interrupt":
            self._end_requested = False  # the user took the floor
            by = str(msg.get("by"))
            if by == "sound" and classify_interjection(str(msg.get("text") or ""), assistant_recent=self._recent) in ("backchannel", "echo"):
                return  # a listener's «угу» or her own echo, not the user taking the floor, however long it sounded
            await self.interrupt(reason=by if by in ("button", "sound") else "button")
        elif kind == "played_until":  # the page stopped her audio: how much of it had really played
            await self._trim_to_heard(msg.get("seconds"))
        elif kind == "play_start" and self._timer and not self._timer.reported:  # the browser began to play the answer
            lead = msg.get("lead_ms")
            self._timer.mark("playing", at=time.monotonic() + (lead / 1000 if isinstance(lead, (int, float)) and 0 <= lead < 2000 else 0))
            await self._report_timing()
        elif kind == "played" and self._awaiting_played:
            self._awaiting_played = False
            self._last_audio_at = time.monotonic()
            await self._set_state("listening")
            if self._end_requested:
                await self._j({"type": "end"})
        elif kind == "pause":
            await self.pause(background=bool(msg.get("background", True)))
        elif kind == "resume":
            await self.resume()
        elif kind == "voice":
            await self.request_voice(str(msg.get("provider") or self.provider), str(msg.get("voice") or ""))
        elif kind == "style":  # the page's selectors: the character and how emotional the delivery is
            if "tune" in msg and bool(msg["tune"]) != self._tune:
                self._tune = bool(msg["tune"])
                await self._retune()
            if msg.get("character") in CHARACTERS:
                await self.set_character(str(msg["character"]))
            if msg.get("emotion") in EMOTIONS:
                await self.set_emotion(str(msg["emotion"]))
        elif kind == "history":  # the page opened another conversation, or started a new one
            await self.interrupt(reason="history")
            self.history = sanitize_history(msg.get("history"))
            self._recent = ""
            if self.ears:
                await self._open_ears()  # a new Realtime conversation, seeded with this one

    async def on_heard(self, text: str, final: bool) -> None:
        """What the browser's speech recognition heard: interim text, or the final text of an utterance."""
        if self.paused:
            text = text.strip()
            if final and text and self.background:
                self._overhear(text)
                await self._j({"type": "overheard", "count": len(self.overheard)})
            return
        raw = text.strip()
        text = self._without_echo(raw)
        self._final, self._partial = (text, "") if final else ("", text)
        if final and not text:  # nothing but her own voice
            await self._j({"type": "user_drop", "reason": "echo", "text": raw})
            return
        if text:
            self._last_interim = text
            self._last_interim_at = time.monotonic()
            await self._j({"type": "user", "text": text, "final": False})
            if self.state in ("thinking", "speaking"):
                self._during_turn = True
                intent = classify_interjection(text, assistant_recent=self._recent)
                if intent in ("stop", "takeover"):
                    log.info("barge-in (%s): %r", intent, text)
                    await self.interrupt(reason=intent, text=text)
                    self._skip_reply = intent == "stop"
        if final and self.realtime:
            self._final = ""  # the end of the phrase and its text come from Realtime (_on_speech_stopped)
        elif final:
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
        intent = classify_interjection(text, assistant_recent=self._recent)
        if skip or (during and busy) or (intent == "echo" and (busy or recently)):
            # The page shows why a phrase was not answered, so the user can tell a deaf agent from a quiet voice.
            reason = "stop" if skip else "echo" if intent == "echo" else "backchannel" if intent == "backchannel" else "not_interruption"
            await self._j({"type": "user_drop", "reason": reason, "text": text})
            return
        await self._j({"type": "user", "text": text, "final": True})
        self.history.append({"role": "user", "content": text})
        async with self._lock:
            self._turn = asyncio.create_task(self._run_turn())

    # ------------------------------------------------------------------ Realtime ears

    async def _open_ears(self) -> None:
        await self.ears.close()
        self._rt_users.clear()
        self._late.clear()
        self._cancel_waiting()
        self.ears.on_speech_started = self._on_speech_started
        self.ears.on_speech_stopped = self._on_speech_stopped
        self.ears.on_user_item = self._on_user_item
        self.ears.on_user_text = self._on_user_text
        self.ears.on_error = self._on_ears_error
        await self.ears.open(language=self.persona.language, history=[h for h in self.history if h.get("content")])

    async def on_audio(self, pcm: bytes) -> None:
        """Microphone sound from the page (through the echo gate): PCM16, 24 kHz, mono."""
        if self.ears and not self.paused:
            await self.ears.feed(pcm)

    async def _on_speech_started(self) -> None:
        """Realtime hears the user begin. While she is busy a voice that goes on is an interruption (a short sound,
        «угу» or a cough, is over before the timer ends); the phrase is answered when it ends (_on_speech_stopped)."""
        self._vad_speaking = True
        if self.paused or self.state not in ("thinking", "speaking"):
            return
        self._cancel_barge()
        self._barge_task = asyncio.create_task(self._barge_after(_VAD_BARGE_S))

    def _cancel_barge(self) -> None:
        if self._barge_task and not self._barge_task.done() and self._barge_task is not asyncio.current_task():
            self._barge_task.cancel()
        self._barge_task = None

    async def _barge_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if not self._vad_speaking or self.paused or self.state not in ("thinking", "speaking"):
            return
        text = self._last_interim if time.monotonic() - self._last_interim_at < 4.0 else ""
        if text and classify_interjection(text, assistant_recent=self._recent) in ("backchannel", "echo"):
            return  # a listener's «угу» or her own echo, however long it sounded
        log.info("barge-in (Realtime heard the user go on): %r", text)
        self._barge_task = None
        await self.interrupt(reason="sound", text=text)

    async def _on_speech_stopped(self, item_id: str) -> None:
        """Realtime heard the user finish a phrase: answer it, unless she was busy (then the words the browser
        recognized decided whether it was an interruption) or the user said «стоп»."""
        self._vad_speaking = False
        self._t_stop = time.monotonic()
        self._cancel_barge()
        if self.paused:
            return
        skip = self._skip_reply
        self._during_turn = self._skip_reply = False
        if skip:
            await self._j({"type": "user_drop", "reason": "stop", "text": self._last_interim})
            return
        if self.state in ("thinking", "speaking") or (self._turn and not self._turn.done()):
            # She was busy and this was short: not an interruption (that takes _VAD_BARGE_S of voice). Not dropped: its
            # words decide (_on_user_text), and what is more than a «угу» is answered when she is free.
            self._late[item_id] = None
            return
        if item_id not in self._rt_waiting:  # answered when its item exists (_on_user_item), or after _ITEM_WAIT_S anyway
            self._rt_waiting[item_id] = asyncio.create_task(self._item_fallback(item_id))

    async def _item_fallback(self, item_id: str) -> None:
        await asyncio.sleep(_ITEM_WAIT_S)
        await self._start_rt_turn(item_id)

    async def _on_user_item(self, item_id: str) -> None:
        await self._start_rt_turn(item_id)

    def _cancel_waiting(self) -> None:
        for task in self._rt_waiting.values():
            if task is not asyncio.current_task():
                task.cancel()
        self._rt_waiting.clear()

    async def _start_rt_turn(self, item_id: str) -> None:
        task = self._rt_waiting.pop(item_id, None)
        if task is None:  # already answered (the item event and the fallback both come), or no longer wanted
            return
        if task is not asyncio.current_task():
            task.cancel()
        if self.paused:
            return
        if self.state in ("thinking", "speaking") or (self._turn and not self._turn.done()):
            self._late[item_id] = None  # the item came while she was busy: kept, not lost
            return
        entry = {"role": "user", "content": ""}  # the transcript arrives later (_on_user_text)
        self._rt_users[item_id] = entry
        self.history.append(entry)
        async with self._lock:
            self._turn = asyncio.create_task(self._run_turn())

    async def _on_user_text(self, item_id: str, text: str) -> None:
        if item_id in self._late:
            await self._judge_late(item_id, text)
            return
        entry = self._rt_users.pop(item_id, None)
        if entry is None:  # a phrase we did not answer
            return
        if not text.strip():
            # Nothing was recognized (short or quiet words often come back empty). The model heard the audio itself and is
            # already answering it: an empty transcript is no reason to drop the answer (it used to be taken for echo).
            with contextlib.suppress(ValueError):
                self.history.remove(entry)
            return
        busy = self.state in ("thinking", "speaking")
        recently = time.monotonic() - self._last_audio_at < _ECHO_TAIL_S
        clean = self._without_echo(text)
        echo = not clean or (classify_interjection(clean, assistant_recent=self._recent) == "echo" and (busy or recently))
        if echo:
            with contextlib.suppress(ValueError):
                self.history.remove(entry)
            await self.interrupt(reason="echo")
            await self.ears.drop_item(item_id)
            await self._j({"type": "user_drop", "reason": "echo", "text": text})
            return
        entry["content"] = clean
        await self._j({"type": "user", "text": clean, "final": True})

    async def _judge_late(self, item_id: str, text: str) -> None:
        clean = self._without_echo(text) if text.strip() else ""
        if not clean or classify_interjection(clean, assistant_recent=self._recent) in ("backchannel", "echo"):
            self._late.pop(item_id, None)
            await self._j({"type": "user_drop", "reason": "busy", "text": text})
            return
        self._late[item_id] = clean
        await self._answer_late()

    async def _answer_late(self) -> None:
        """Answer the phrases said while she was busy, all in one turn, once she is free."""
        if self.paused or self.state != "listening" or (self._turn and not self._turn.done()):
            return  # called again when she is free (_set_state)
        ready = [(i, t) for i, t in self._late.items() if t]
        if not ready:
            return
        for i, _ in ready:
            del self._late[i]
        for _, t in ready:
            await self._j({"type": "user", "text": t, "final": True})
            self.history.append({"role": "user", "content": t})
        async with self._lock:
            self._turn = asyncio.create_task(self._run_turn())

    async def _on_ears_error(self, message: str) -> None:
        await self._j({"type": "error", "message": message})
        await self.interrupt(reason="error")

    # ------------------------------------------------------------------ pause

    async def pause(self, *, background: bool = True) -> None:
        self.background = background
        if self.paused:
            return
        self._end_requested = False
        await self.interrupt(reason="pause")
        self.paused = True
        self._final = self._partial = ""
        self._during_turn = self._skip_reply = False
        if self._active_since is not None:
            self._active_s += time.monotonic() - self._active_since
            self._active_since = None
        await self._set_state("paused")
        await self._send_usage()

    async def resume(self) -> None:
        if not self.paused:
            return
        self.paused = False
        self._active_since = time.monotonic()
        await self._set_state("listening")

    def _overhear(self, text: str) -> None:
        self.overheard.append(text[:_OVERHEARD_CHARS])
        del self.overheard[:-_OVERHEARD_MAX]

    def _take_overheard(self) -> list[str]:
        """What was heard during the pause: told to the model once, with the first answer after it."""
        heard, self.overheard = self.overheard, []
        return heard

    # ------------------------------------------------------------------ turn

    async def _run_turn(self, *, greeting: bool = False) -> None:
        self._spoken, self._turn_done, self._turn_audio = [], False, []
        await self._set_state("thinking")
        fresh = self.realtime and not greeting and time.monotonic() - self._t_stop < 5.0
        self._timer = timer = TurnTimer(self._t_stop if fresh else None)
        timer.mark("turn_start")
        splitter = SentenceSplitter()
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        full: list[str] = []
        ctx = TurnContext(
            system_prompt=build_stable_prompt(
                search=bool(getattr(self.llm, "searchable", False)),
                persona=self.persona,
                about=await self._about(),
                provider=self.provider,
            ),
            note=turn_note(
                now=now_text(self.cfg.timezone),
                overheard=self._take_overheard(),
                greeting=greeting_note(returning=bool(self.history)) if greeting else "",
            ),
            extra_tools=TOOLS,
            run_tool=self.run_tool,
            on_usage=self._on_usage,
            on_search=self._on_search,
            on_audio=self._on_audio,
            on_mark=timer.mark,
        )

        kept_back: list[str] = []  # sentences the model wrote in Russian: never said, never shown
        said: list[str] = []

        async def produce() -> None:
            # The voice gets the text as the model wrote it (a Soniox voice acts on [warm]-style tags); the page, the
            # history and the echo check get it without the tags. Sentence by sentence: a Russian sentence (server/russian.py)
            # is neither said nor shown, whatever the model was told or asked.
            async def release(sentence: str) -> None:
                # The splitter glues short sentences together: each part is judged alone, so a Russian one takes only itself.
                parts = [p for p in re.split(r"(?<=[.!?…])\s+", sentence) if p.strip()]
                good = [p for p in parts if not is_russian(p)]
                if len(good) < len(parts):
                    log.warning("Russian was kept back: %r", sentence[:80])
                    kept_back.extend(p for p in parts if is_russian(p))
                if not good:
                    return
                sentence = " ".join(good)
                said.append(sentence)
                timer.mark("first_sentence")
                sentences.put_nowait(sentence)
                clean = strip_tags(sentence)
                if clean:
                    full.append(clean + " ")
                    self._recent = "".join(full)[-400:]
                    await self._j({"type": "assistant_delta", "text": clean + " "})

            try:
                async for delta in self.llm.stream(self.history, ctx):
                    if delta == SEGMENT_END:  # a tool is about to run: say what we have now, not after it
                        for s in splitter.flush():
                            await release(s)
                        continue
                    for s in splitter.feed(delta):
                        await release(s)
                for s in splitter.flush():
                    await release(s)
                if kept_back and not said:  # all of it was Russian: say why there is nothing else
                    await release(refusal(self.persona.language))
            finally:
                sentences.put_nowait(None)

        producer = asyncio.create_task(produce())
        try:
            await self._speak(sentences, timer)
            await producer  # surfaces an LLM error
            if kept_back and self.ears:  # the model's own memory must not keep what was not said
                await self.ears.replace_spoken(" ".join(said))
            await self._apply_pending()
            self._turn_done = True
            answer = "".join(full).strip()
            self.history.append({"role": "assistant", "content": answer})
            await self._j({"type": "record", "role": "assistant", "text": answer})
            await self._send_usage()
            if self._spoken:
                self._awaiting_played = True
                await self._j({"type": "turn_end"})
            else:
                await self._set_state("listening")
                if self._end_requested:
                    await self._j({"type": "end"})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("turn failed")
            await self._j({"type": "error", "message": f"{type(e).__name__}: {e}"})
            await self._j({"type": "clear", "reason": "error"})
            self._awaiting_played = False
            self._end_requested = False
            await self._set_state("listening")
            await self._send_usage()
        finally:
            producer.cancel()

    def _seconds_per_char(self) -> float:
        return self._rates.get(self.provider, self.cfg.tts_seconds_per_char)

    def _sync_progress(self, written: list[str], received_s: float) -> None:
        """One generation does not say where a sentence ends, so each sentence's time is estimated from its length. A
        sentence counts as spoken once the audio received has reached its estimated start: the model writes much faster
        than the voice speaks, and the sentences that are only written must not be taken for heard."""
        rate = self._seconds_per_char()
        self._turn_audio = [(t, len(t) * rate) for t in written]
        spoken, start = [], 0.0
        for text, dur in self._turn_audio:
            if start >= received_s:
                break
            spoken.append(text)
            start += dur
        self._spoken = spoken

    def _finish_progress(self, written: list[str], seconds: float) -> None:
        """The answer was spoken to the end: everything counts as spoken, the estimates are scaled to the audio that really
        came, and the seconds-per-character figure learns from it (a moving average)."""
        chars = sum(len(t) for t in written)
        self._spoken = list(written)
        est = chars * self._seconds_per_char()
        scale = seconds / est if est > 0 and seconds > 0 else 1.0
        self._turn_audio = [(t, len(t) * self._seconds_per_char() * scale) for t in written]
        if chars >= 20 and seconds >= 0.5:
            self._rates[self.provider] = 0.7 * self._seconds_per_char() + 0.3 * (seconds / chars)

    async def _speak(self, sentences: asyncio.Queue, timer: TurnTimer) -> None:
        """Voice the sentences of the answer as they come: one TTS generation for all of them when the voice supports it,
        otherwise (or when it gave no audio that way) one generation per sentence."""
        first: list[str] = []
        if self.cfg.tts_one_generation and not self._stream_broken and hasattr(self.tts, "open_stream"):
            first = await self._speak_stream(sentences, timer) or []
            if not first:
                return
        await self._speak_sentences(sentences, timer, first)

    async def _speak_stream(self, sentences: asyncio.Queue, timer: TurnTimer) -> list[str] | None:
        """One generation for the whole answer: each sentence is added to it the moment it is complete. The voice and the
        language are changed only before the answer (never inside one generation). Returns None when all is said; the
        sentences written so far when the voice gave no audio this way (then they are said one by one: see _speak)."""
        stream = reader = None
        seconds = 0.0
        written: list[str] = []  # the sentences as the voice got them (with tags)
        plain_written: list[str] = []
        drained = False  # the end of the answer (None) was taken from the queue

        async def pump(st) -> None:
            nonlocal seconds
            async for chunk in st:
                timer.mark("tts_audio")
                self._last_audio_at = time.monotonic()
                await self._send_bytes(chunk)
                seconds += len(chunk) / 2 / self.tts.sample_rate
                self._sync_progress(plain_written, seconds)

        try:
            while (sentence := await sentences.get()) is not None:
                if stream is None:
                    await self._apply_pending()
                    await self._set_state("speaking")
                    stream = self.tts.open_stream()
                    reader = asyncio.create_task(pump(stream))
                elif reader.done():
                    reader.result()  # the voice failed: raises
                plain = strip_tags(sentence)
                voiced = sentence if self.provider == "soniox" else plain  # only Soniox understands the tags
                if not voiced:
                    continue
                self.usage.chars[self.provider] = self.usage.chars.get(self.provider, 0) + len(voiced)
                await stream.write(voiced)
                written.append(sentence)
                if plain:
                    plain_written.append(plain)
                    self._sync_progress(plain_written, seconds)
            drained = True
            if stream is not None and written:
                await stream.finish()
                await reader
                self._finish_progress(plain_written, seconds)
            return None
        except TTSError as e:
            if seconds > 0 or not written:
                raise
            # No audio at all: this provider does not do it this way. The answer is not lost: said sentence by sentence.
            log.warning("one TTS generation per answer gave no audio (%s): back to one per sentence", e)
            self._stream_broken = True
            for sentence in written:
                voiced = sentence if self.provider == "soniox" else strip_tags(sentence)
                self.usage.chars[self.provider] -= len(voiced)
            self._spoken, self._turn_audio = [], []
            if drained:
                sentences.put_nowait(None)  # the sentence-by-sentence voicing waits for the end of the answer again
            return written
        finally:
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if stream is not None and not stream.finished:
                await stream.cancel()  # a barge-in: the provider stops generating the rest of the answer

    async def _speak_sentences(self, sentences: asyncio.Queue, timer: TurnTimer, first: list[str]) -> None:
        todo = list(first)
        while True:
            if todo:
                sentence = todo.pop(0)
            elif (sentence := await sentences.get()) is None:
                return
            await self._apply_pending()  # a new voice / language: between sentences, never inside one
            await self._set_state("speaking")
            plain = strip_tags(sentence)
            voiced = sentence if self.provider == "soniox" else plain  # only Soniox understands the tags
            if not voiced:
                continue
            self.usage.chars[self.provider] = self.usage.chars.get(self.provider, 0) + len(voiced)
            seconds = 0.0
            async for chunk in self.tts.synthesize(voiced):
                timer.mark("tts_audio")
                self._last_audio_at = time.monotonic()
                await self._send_bytes(chunk)
                seconds += len(chunk) / 2 / self.tts.sample_rate
            if plain:
                self._spoken.append(plain)
                self._turn_audio.append((plain, seconds))

    async def _report_timing(self) -> None:
        timer = self._timer
        if timer is None or timer.reported or "tts_audio" not in timer.marks:
            return
        timer.reported = True
        log.info("turn timing (ms from the end of the phrase): %s", timer.line())
        await self._j({"type": "timing", "ms": timer.report()})

    async def interrupt(self, *, reason: str = "", text: str = "") -> None:
        """Cut the answer off. `reason` (stop / takeover: the user's voice, with their words in `text`; button;
        sound; pause; history) goes to the page, which keeps what the user tried in the conversation record."""
        self._late.clear()  # the user is speaking over her: the phrase that follows is answered with all that was said
        async with self._lock:
            task, self._turn = self._turn, None
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if self.state in ("listening", "paused") and not self._awaiting_played:
                return
            entry = None  # the history entry that says what she said: trimmed when the page reports what had played
            if not self._turn_done and self._spoken:
                # keep only what the user actually got to hear
                heard = " ".join(self._spoken) + " …"
                entry = {"role": "assistant", "content": heard}
                self.history.append(entry)
                await self._j({"type": "record", "role": "assistant", "text": heard})
            elif self._awaiting_played and self.history and self.history[-1].get("role") == "assistant":
                entry = self.history[-1]  # all of it was generated and sent, but not all of it had played
            self._cut = {"audio": list(self._turn_audio), "entry": entry} if entry is not None and self._turn_audio else None
            if self.ears and not self._turn_done:  # the model wrote the whole answer: keep what was heard
                await self.ears.replace_spoken(" ".join(self._spoken) + " …" if self._spoken else "")
            self._turn_done = True
            self._awaiting_played = False
            await self._j({"type": "clear", "reason": reason, "text": text})
            await self._set_state("listening")

    async def _about(self) -> str:
        """The knowledge for this answer: the database's (renewed every half minute and on every save) or, without one, the
        text the session was given."""
        if self._about_source is None:
            return self.about
        try:
            return await self._about_source.get()
        except Exception:  # the knowledge must never stop an answer
            log.exception("knowledge unavailable")
            return self.about

    async def _trim_to_heard(self, seconds: object) -> None:
        """After a cut-off: her memory of the answer ends where the user stopped hearing it, so that «продовжуй» goes on
        from that place and not from the end of an answer she had only generated."""
        cut, self._cut = self._cut, None
        if not cut or isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return
        heard = heard_part(cut["audio"], max(0.0, float(seconds)))
        if heard is None:  # it had all played
            return
        if heard:
            cut["entry"]["content"] = heard
        else:
            with contextlib.suppress(ValueError):
                self.history.remove(cut["entry"])
        if self.ears:
            await self.ears.replace_spoken(heard)
        log.info("cut off after %.1f s of audio: she remembers %r", float(seconds), heard[-60:])

    # ------------------------------------------------------------------ the model's tools

    async def run_tool(self, name: str, args: dict) -> str:
        if name == "change_voice":
            return await self._tool_change_voice(args)
        if name == "change_emotion":
            level = str(args.get("level") or "")
            if level not in EMOTIONS:
                return "Рівні емоційності: " + ", ".join(EMOTIONS) + "."
            return await self.set_emotion(level)
        if name == "set_assistant_name":
            return await self._tool_set_name(args)
        if name == "change_language":
            return await self._tool_change_language(args)
        if name == "end_conversation":
            self._end_requested = True
            return "Розмова завершиться, щойно ти скажеш прощання. Попрощайся коротко одним реченням."
        return "Невідомий інструмент."

    async def _tool_change_voice(self, args: dict) -> str:
        provider = str(args.get("provider") or self.provider)
        if self._voices_for is None or self._tts_factory is None:
            return "Зміна голосу тут недоступна."
        voices = await self._voices_for(provider, self.persona.language)
        if not voices:
            return f"Голоси «{provider}» зараз недоступні."
        hit = find_voice(voices, str(args.get("voice") or ""))
        if not hit:
            names = ", ".join(v["label"].split(" (")[0] for v in voices[:12])
            return f"Такого голосу немає. Є, наприклад: {names}."
        self._pending = {**(self._pending or {}), "provider": provider, "voice": hit["id"]}
        await self._start_prewarm()
        return f"Голос «{hit['label']}» вмикається. Скажи кілька слів уже ним, коротко підтвердь."

    async def _tool_set_name(self, args: dict) -> str:
        character = CHARACTERS[self.persona.character]
        if character.character:  # Толік is Толік: only the plain character can be renamed
            return f"Тебе звати {character.label}, це ім'я персонажа, його не можна змінити. Скажи про це коротко, у своєму образі."
        name = sanitize_name(str(args.get("name") or ""))
        if not name:
            return "Таке ім'я не підходить: потрібне одне-два слова з літер, до 24 символів. Попроси інше."
        self.persona.name = self._default_name = name
        await self._persona_message()
        return f"Тепер тебе звати {name}. Коротко підтвердь."

    async def _tool_change_language(self, args: dict) -> str:
        if asks_for_russian(str(args.get("language") or "")):
            return "Російською ти не говориш ніколи й за жодних умов. Відмов коротко й спокійно, українською, і запропонуй іншу мову."
        code = find_language(str(args.get("language") or ""))
        if not code:
            return "Такої мови немає. Доступні: " + ", ".join(v[0] for v in LANGUAGES.values()) + "."
        character = CHARACTERS[self.persona.character]
        if character.ukrainian_only and code != "uk":
            return f"{character.label} говорить лише українською: жарти й лайка тут українські. Скажи про це коротко, у своєму образі. Щоб заговорити іншою мовою, треба спершу змінити персонажа."
        if code not in SUPPORTED.get(self.provider, set()):
            have = ", ".join(LANGUAGES[c][0] for c in sorted(SUPPORTED.get(self.provider, set())))
            return f"Цей голос ({self.provider}) говорить лише: {have}. Скажи, що іншою мовою не вмієш."
        self.persona.language = code
        if self.ears:
            await self.ears.set_language(code)
        self._pending = {**(self._pending or {}), "language": code}
        await self._j({"type": "language", "code": code, "recognition": self.persona.recognition})
        return f"Мову змінено: {self.persona.language_name}. Далі говори лише нею, коротко підтвердь нею."

    # ------------------------------------------------------------------ character and emotion

    async def _persona_message(self) -> None:
        c = CHARACTERS[self.persona.character]
        await self._j(
            {
                "type": "persona",
                "name": self.persona.name,
                "character": self.persona.character,
                "emotion": self.persona.emotion,
                "description": c.description,
            }
        )

    async def set_character(self, key: str) -> str:
        """Another character speaks from the next answer. Returns what the model is told about it."""
        c = CHARACTERS[key]
        if key == self.persona.character:
            return f"Ти вже {c.label}."
        self.persona.character = key
        self.persona.name = c.label if c.character else self._default_name
        extra = ""
        if c.ukrainian_only and self.persona.language != "uk":
            # its jokes and curses are Ukrainian: the language goes back to Ukrainian, with the voice and the recognizer
            self.persona.language = "uk"
            self._pending = {**(self._pending or {}), "language": "uk"}
            await self._j({"type": "language", "code": "uk", "recognition": self.persona.recognition})
            extra = " Мова повернулась на українську: цей персонаж говорить лише нею."
        # One character, whichever provider speaks: it brings the voice it has for the provider selected now (the provider
        # itself is not changed). A voice the user picks by hand stays until the next character.
        extra += await self._bind_voice(c)
        await self._persona_message()
        await self._retune()
        if not c.character:
            return f"Тепер ти знову просто {c.label}: звичайна приязна помічниця.{extra}"
        return f"Тепер ти {c.label}. Відповідай у цьому образі з наступної фрази, коротко представся ним.{extra}"

    async def _bind_voice(self, c) -> str:
        """The character's own voice on the provider that speaks now (if the provider has it and it is not already the one)."""
        own = c.voice_for(self.provider)
        if own is None or own.voice == self.voice or self._voices_for is None:
            return ""
        voices = await self._voices_for(self.provider, self.persona.language)
        if not any(v["id"] == own.voice for v in voices):
            return ""
        await self.request_voice(self.provider, own.voice)
        return f" Голос теж змінюється на «{own.label}»: це голос цього образу."

    async def set_emotion(self, level: str) -> str:
        self.persona.emotion = level
        await self._persona_message()
        await self._retune()
        return f"Манера змінена: {EMOTIONS[level][0].lower()}. Коротко підтвердь і говори так далі."

    async def _base_temperature(self) -> float | None:
        """The current voice's own temperature, as the provider lists it: the starting point of the tuning."""
        if self._voices_for is None:
            return None
        voices = await self._voices_for(self.provider, self._tts_language)
        entry = next((v for v in voices if v["id"] == self.voice), None)
        t = ((entry or {}).get("sampling") or {}).get("temperature")
        return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 else None

    async def _retune(self) -> None:
        """The delivery follows the character and the emotion: the voice's own temperature times a factor. The voice
        itself is never touched. Reports what was done (or why nothing was) to the page, which shows it, because the
        right factors are found by ear."""
        setter = getattr(self.tts, "set_sampling", None)
        report: dict = {"type": "tuning", "active": False, "reason": ""}
        if not (callable(setter) and setter({})):  # this also resets any earlier tuning
            report["reason"] = "provider"  # the synthesizer has no such parameters (Soniox): the manner is in the text
        elif not self._tune:
            report["reason"] = "off"
        else:
            base = await self._base_temperature()
            if base is None:
                report["reason"] = "no_base"  # the provider did not list this voice's defaults
            else:
                factor = tone_factor(self.persona.character, self.persona.emotion)
                value = round(base * factor, 3)
                setter({"temperature": value})
                report.update(active=True, base=base, factor=factor, value=value)
        await self._j(report)

    # ------------------------------------------------------------------ voice change

    async def request_voice(self, provider: str, voice: str) -> None:
        """The page's «Застосувати»: at once when idle; during an answer the new voice is connected in the background
        and takes over at the next sentence, the answer goes on in it."""
        self._pending = {**(self._pending or {}), "provider": provider, "voice": voice}
        if self.state in ("listening", "paused") and not self._awaiting_played:
            await self._apply_pending()
        else:
            await self._start_prewarm()

    async def _resolve_pending(self, pending: dict) -> tuple[str, str, str]:
        provider = str(pending.get("provider") or self.provider)
        language = str(pending.get("language") or self._tts_language)
        voice = str(pending.get("voice") or self.voice)
        if self._voices_for:
            voices = await self._voices_for(provider, language)
            if voices and voice not in {v["id"] for v in voices}:
                voice = voices[0]["id"]  # e.g. another language model: its own voices
        return provider, voice, language

    async def _start_prewarm(self) -> None:
        if self._tts_factory is None or not self._pending:
            return
        key = await self._resolve_pending(self._pending)
        if key == (self.provider, self.voice, self._tts_language) or key == self._prewarm_key:
            return
        if self._prewarm_task and not self._prewarm_task.done():
            self._prewarm_task.cancel()  # the user picked another voice meanwhile
        self._prewarm_key = key
        self._prewarm_task = self._spawn(self._prewarm(key))

    async def _prewarm(self, key: tuple[str, str, str]) -> None:
        tts = None
        try:
            tts = self._tts_factory(*key)  # type: ignore[misc]
            await tts.connect()
        except asyncio.CancelledError:
            if tts is not None:
                await tts.close()
            raise
        except Exception:
            log.warning("voice prewarm failed: the swap will try again", exc_info=True)
            return
        if self._prepared:
            await self._prepared[1].close()
        self._prepared = (key, tts)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    async def _apply_pending(self) -> None:
        async with self._tts_lock:
            pending, self._pending = self._pending, None
            if not pending or self._tts_factory is None:
                return
            key = await self._resolve_pending(pending)
            if key == (self.provider, self.voice, self._tts_language):
                return
            if self._prewarm_task and not self._prewarm_task.done() and self._prewarm_key == key:
                await asyncio.shield(self._prewarm_task)  # nearly there: waiting is shorter than starting again
            new = None
            if self._prepared and self._prepared[0] == key:
                new, self._prepared = self._prepared[1], None
            elif self._prepared:  # a voice nobody wants any more
                await self._prepared[1].close()
                self._prepared = None
            try:
                if new is None:
                    new = self._tts_factory(*key)
                    await new.connect()
            except Exception as e:
                log.exception("voice change failed")
                await self._j({"type": "error", "message": f"Не вдалося змінити голос: {e}"})
                return
            provider, voice, language = key
            old, self.tts = self.tts, new
            self.provider, self.voice, self._tts_language = provider, voice, language
            self._prewarm_key = None
            await old.close()
            await self._retune()  # the new voice has its own defaults to start from
            await self._j(
                {"type": "voice", "provider": provider, "voice": voice, "sample_rate": new.sample_rate, "language": language}
            )

    # ------------------------------------------------------------------ helpers

    def _on_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.usage.llm_in += prompt_tokens
        self.usage.llm_out += completion_tokens

    def _on_audio(self, tokens: int) -> None:
        self.usage.audio_in += tokens

    def _on_search(self) -> None:
        self.usage.searches += 1

    def _active_seconds(self) -> float:
        running = time.monotonic() - self._active_since if self._active_since is not None else 0.0
        return self._active_s + running

    async def _send_usage(self) -> None:
        u = self.usage
        await self._j(
            {
                "type": "usage",
                "seconds": int(self._active_seconds()),
                "llm_in": u.llm_in,
                "llm_out": u.llm_out,
                "searches": u.searches,
                "tts_chars": sum(u.chars.values()),
                "cost": round(u.cost(self.cfg), 5),
            }
        )

    def _without_echo(self, text: str) -> str:
        busy = self.state in ("thinking", "speaking")
        if text and (busy or time.monotonic() - self._last_audio_at < _ECHO_STRIP_S):
            return strip_echo(text, self._recent)
        return text

    async def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            await self._j({"type": "state", "state": state})
            if state == "listening" and any(self._late.values()):
                asyncio.create_task(self._answer_late_soon())

    async def _answer_late_soon(self) -> None:
        for _ in range(20):  # the turn that just ended is finishing
            if not (self._turn and not self._turn.done()):
                break
            await asyncio.sleep(0.05)
        await self._answer_late()

    async def _j(self, msg: dict) -> None:
        with contextlib.suppress(Exception):  # the browser is gone; close() follows
            await self._send_json(msg)
