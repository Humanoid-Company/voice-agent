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
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .config import Config
from .interrupt_intent import classify_interjection, strip_echo
from .llm import TurnContext
from .persona import (
    LANGUAGES,
    SUPPORTED,
    Persona,
    find_language,
    find_voice,
    now_text,
    sanitize_name,
)
from .prompt import build_system_prompt
from .sentences import SentenceSplitter
from .usage import Usage

log = logging.getLogger(__name__)

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
    ) -> None:
        self.llm = llm
        self.tts = tts
        self.cfg = cfg or Config()
        self.provider, self.voice = provider, voice
        self.persona = persona or Persona()
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
        self.usage = Usage()
        self._active_s = 0.0
        self._active_since: float | None = time.monotonic()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.tts.connect()
        await self._j(
            {
                "type": "ready",
                "sample_rate": self.tts.sample_rate,
                "name": self.persona.name,
                "language": self.persona.language,
                "recognition": self.persona.recognition,
            }
        )
        await self._j({"type": "state", "state": "listening"})  # _set_state would skip it: already "listening"

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
            self._end_requested = False  # the user took the floor
            await self.interrupt()
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
        elif kind == "history":  # the page opened another conversation, or started a new one
            await self.interrupt()
            self.history = sanitize_history(msg.get("history"))
            self._recent = ""

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

    # ------------------------------------------------------------------ pause

    async def pause(self, *, background: bool = True) -> None:
        self.background = background
        if self.paused:
            return
        self._end_requested = False
        await self.interrupt()
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

    async def _run_turn(self) -> None:
        self._spoken, self._turn_done = [], False
        await self._set_state("thinking")
        splitter = SentenceSplitter()
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        full: list[str] = []
        ctx = TurnContext(
            system_prompt=build_system_prompt(
                search=bool(getattr(self.llm, "searchable", False)),
                persona=self.persona,
                now=now_text(self.cfg.timezone),
                overheard=self._take_overheard(),
            ),
            extra_tools=TOOLS,
            run_tool=self.run_tool,
            on_usage=self._on_usage,
            on_search=self._on_search,
        )

        async def produce() -> None:
            try:
                async for delta in self.llm.stream(self.history, ctx):
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
                await self._apply_pending()  # a new voice / language: between sentences, never inside one
                await self._set_state("speaking")
                self.usage.chars[self.provider] = self.usage.chars.get(self.provider, 0) + len(sentence)
                async for chunk in self.tts.synthesize(sentence):
                    self._last_audio_at = time.monotonic()
                    await self._send_bytes(chunk)
                self._spoken.append(sentence)
            await producer  # surfaces an LLM error
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
            await self._j({"type": "clear"})
            self._awaiting_played = False
            self._end_requested = False
            await self._set_state("listening")
            await self._send_usage()
        finally:
            producer.cancel()

    async def interrupt(self) -> None:
        async with self._lock:
            task, self._turn = self._turn, None
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if self.state in ("listening", "paused") and not self._awaiting_played:
                return
            if not self._turn_done and self._spoken:
                # keep only what the user actually got to hear
                heard = " ".join(self._spoken) + " …"
                self.history.append({"role": "assistant", "content": heard})
                await self._j({"type": "record", "role": "assistant", "text": heard})
            self._turn_done = True
            self._awaiting_played = False
            await self._j({"type": "clear"})
            await self._set_state("listening")

    # ------------------------------------------------------------------ the model's tools

    async def run_tool(self, name: str, args: dict) -> str:
        if name == "change_voice":
            return await self._tool_change_voice(args)
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
        return f"Голос «{hit['label']}» вмикається. Скажи кілька слів уже ним, коротко підтвердь."

    async def _tool_set_name(self, args: dict) -> str:
        name = sanitize_name(str(args.get("name") or ""))
        if not name:
            return "Таке ім'я не підходить: потрібне одне-два слова з літер, до 24 символів. Попроси інше."
        self.persona.name = name
        await self._j({"type": "persona", "name": name})
        return f"Тепер тебе звати {name}. Коротко підтвердь."

    async def _tool_change_language(self, args: dict) -> str:
        code = find_language(str(args.get("language") or ""))
        if not code:
            return "Такої мови немає. Доступні: " + ", ".join(v[0] for v in LANGUAGES.values()) + "."
        if code not in SUPPORTED.get(self.provider, set()):
            have = ", ".join(LANGUAGES[c][0] for c in sorted(SUPPORTED.get(self.provider, set())))
            return f"Цей голос ({self.provider}) говорить лише: {have}. Скажи, що іншою мовою не вмієш."
        self.persona.language = code
        self._pending = {**(self._pending or {}), "language": code}
        await self._j({"type": "language", "code": code, "recognition": self.persona.recognition})
        return f"Мову змінено: {self.persona.language_name}. Далі говори лише нею, коротко підтвердь нею."

    # ------------------------------------------------------------------ voice change

    async def request_voice(self, provider: str, voice: str) -> None:
        """The page's selectors: applied at once when idle, else between two sentences."""
        self._pending = {**(self._pending or {}), "provider": provider, "voice": voice}
        if self.state in ("listening", "paused") and not self._awaiting_played:
            await self._apply_pending()

    async def _apply_pending(self) -> None:
        async with self._tts_lock:
            pending, self._pending = self._pending, None
            if not pending or self._tts_factory is None:
                return
            provider = str(pending.get("provider") or self.provider)
            language = str(pending.get("language") or self._tts_language)
            voice = str(pending.get("voice") or self.voice)
            if self._voices_for:
                voices = await self._voices_for(provider, language)
                if voices and voice not in {v["id"] for v in voices}:
                    voice = voices[0]["id"]  # e.g. another language model: its own voices
            if (provider, voice, language) == (self.provider, self.voice, self._tts_language):
                return
            try:
                new = self._tts_factory(provider, voice, language)
                await new.connect()
            except Exception as e:
                log.exception("voice change failed")
                await self._j({"type": "error", "message": f"Не вдалося змінити голос: {e}"})
                return
            old, self.tts = self.tts, new
            self.provider, self.voice, self._tts_language = provider, voice, language
            await old.close()
            await self._j(
                {"type": "voice", "provider": provider, "voice": voice, "sample_rate": new.sample_rate, "language": language}
            )

    # ------------------------------------------------------------------ helpers

    def _on_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.usage.llm_in += prompt_tokens
        self.usage.llm_out += completion_tokens

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

    async def _j(self, msg: dict) -> None:
        with contextlib.suppress(Exception):  # the browser is gone; close() follows
            await self._send_json(msg)
