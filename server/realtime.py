"""The ears and the brain on OpenAI Realtime: it hears the microphone itself (no speech recognizer in between) and
answers with TEXT only, which the session splits into sentences and has Respeecher / Soniox speak.

It offers the same `stream(history, ctx)` as `OpenAILLM`, so the session's turn code is shared. What differs: the
audio is fed in with `feed()`, the model's server-side VAD says when the user started and stopped
(`on_speech_started` / `on_speech_stopped`), the transcript of what was said arrives later (`on_user_text`), and
the Realtime conversation keeps its own context, so `history` is only used to seed it.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from openai import AsyncOpenAI

from . import web_search as ws
from .config import Config
from .llm import (
    _CUES,
    MAX_SEARCHES_PER_TURN,
    MAX_TOOL_ROUNDS,
    SEARCH_LIMIT_NOTE,
    SEGMENT_END,
    WEB_SEARCH_TOOL,
    SearchFn,
    TurnContext,
    run_tool_call,
)

log = logging.getLogger("realtime")

_SEED_HISTORY = 30
# No event of the answer for this long: the connection is alive but the model is not answering. Better an error the
# user sees than a call that stays «thinking» for ever.
_RESPONSE_TIMEOUT_S = 30.0
_CANCELLED_NOTE = "Скасовано: людина перебила, результат не потрібен."

Handler = Callable[..., Awaitable[None]]


class _SdkConnection:
    """The OpenAI SDK's websocket, reduced to what is used here: send a dict, iterate dicts, close."""

    def __init__(self, manager: Any, conn: Any) -> None:
        self._manager, self._conn = manager, conn

    async def send(self, event: dict) -> None:
        await self._conn.send(event)

    async def __aiter__(self) -> AsyncIterator[dict]:
        async for ev in self._conn:
            yield ev if isinstance(ev, dict) else ev.model_dump()

    async def close(self) -> None:
        await self._manager.__aexit__(None, None, None)


async def sdk_connect(cfg: Config) -> _SdkConnection:
    client = AsyncOpenAI(api_key=cfg.openai_api_key)
    manager = client.realtime.connect(model=cfg.realtime_model)
    return _SdkConnection(manager, await manager.__aenter__())


def flat_tool(tool: dict) -> dict:
    """The chat-completions tool shape {"type": "function", "function": {...}} → Realtime's flat one."""
    f = tool["function"]
    return {
        "type": "function",
        "name": f["name"],
        "description": f.get("description", ""),
        "parameters": f.get("parameters") or {"type": "object", "properties": {}},
    }


def _text_item(role: str, text: str) -> dict:
    kind = "input_text" if role == "user" else "output_text"
    return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}


class RealtimeLLM:
    def __init__(
        self,
        cfg: Config,
        *,
        connect: Callable[[], Awaitable[Any]] | None = None,
        search: SearchFn | None = None,
    ) -> None:
        self._cfg = cfg
        mode = cfg.realtime_output
        self._audio_out = mode == "audio" or (mode != "text" and cfg.realtime_model.startswith("gpt-live"))
        self._connect = connect or (lambda: sdk_connect(cfg))
        self.searchable = bool(search or cfg.web_search_api_key)

        async def default_search(query: str, recency_days: int | None, topic: str | None = None) -> str:
            return await ws.web_search(
                query, api_key=cfg.web_search_api_key, recency_days=recency_days, topic=topic,
                timeout_s=cfg.web_search_timeout_s,
            )

        self._search: SearchFn = search or default_search

        self.on_speech_started: Handler | None = None
        self.on_speech_stopped: Handler | None = None  # (item_id)
        self.on_user_text: Handler | None = None  # (item_id, text)
        self.on_user_item: Handler | None = None  # (item_id): the user's audio is in the conversation, an answer can be asked for
        self.on_error: Handler | None = None  # (message)

        self._conn: Any = None
        self._reader: asyncio.Task | None = None
        self._instructions = ""
        self._tools_key = ""
        self._turn_q: asyncio.Queue[dict] | None = None
        self._awaiting_created = False
        self._response_id = ""
        self._items: list[str] = []  # assistant message ids of the current answer
        self._response_open = False
        self._cuts = 0
        self._mark: Callable[[str], None] | None = None
        self._first_delta = False

    # ------------------------------------------------------------------ connection

    async def open(self, *, language: str = "uk", history: list[dict] | None = None) -> None:
        self._conn = await self._connect()
        self._reader = asyncio.create_task(self._read())
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "output_modalities": ["audio"] if self._audio_out else ["text"],
                    "tool_choice": "auto",
                    "audio": {"input": self._audio_input(language)},
                },
            }
        )
        for item in (history or [])[-_SEED_HISTORY:]:
            text = str(item.get("content") or "").strip()
            role = item.get("role")
            if text and role in ("user", "assistant"):
                await self._send({"type": "conversation.item.create", "item": _text_item(role, text)})

    def _audio_input(self, language: str) -> dict:
        return {
            "format": {"type": "audio/pcm", "rate": 24000},
            "transcription": {"model": self._cfg.realtime_transcribe_model, "language": language},
            "turn_detection": {
                "type": "server_vad",
                "threshold": self._cfg.realtime_vad_threshold,
                "prefix_padding_ms": 300,
                "silence_duration_ms": self._cfg.realtime_silence_ms,
                # We decide when to answer (not while the agent is busy) and when to stop: both are the session's.
                "create_response": False,
                "interrupt_response": False,
            },
        }

    async def set_language(self, language: str) -> None:
        if self._conn is not None:
            await self._send(
                {"type": "session.update", "session": {"type": "realtime", "audio": {"input": self._audio_input(language)}}}
            )

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None

    async def _send(self, event: dict) -> None:
        if self._conn is not None:
            await self._conn.send(event)

    async def feed(self, pcm: bytes) -> None:
        """PCM16, 24 kHz, mono from the page."""
        if self._conn is not None and pcm:
            await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})

    # ------------------------------------------------------------------ events

    async def _read(self) -> None:
        try:
            async for ev in self._conn:
                await self._dispatch(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("realtime connection lost")
            await self._call(self.on_error, f"Зв'язок із Realtime обірвано: {type(e).__name__}")
            return
        await self._call(self.on_error, "Зв'язок із Realtime закрито.")

    async def _call(self, handler: Handler | None, *args: Any) -> None:
        if handler is None:
            return
        try:
            await handler(*args)
        except Exception:
            log.exception("realtime handler failed")

    async def _dispatch(self, ev: dict) -> None:
        kind = ev.get("type", "")
        if kind == "input_audio_buffer.speech_started":
            await self._call(self.on_speech_started)
        elif kind == "input_audio_buffer.speech_stopped":
            await self._call(self.on_speech_stopped, ev.get("item_id", ""))
        elif kind in ("conversation.item.added", "conversation.item.created"):
            item = ev.get("item") or {}
            if item.get("role") == "user":
                await self._call(self.on_user_item, item.get("id", ""))
        elif kind == "conversation.item.input_audio_transcription.completed":
            await self._call(self.on_user_text, ev.get("item_id", ""), str(ev.get("transcript") or "").strip())
        elif kind == "error":
            err = ev.get("error") or {}
            log.warning("realtime error: %s %s", err.get("code"), err.get("message"))
            if self._turn_q is not None and err.get("code") not in ("response_cancel_not_active", "item_delete_invalid_item_id"):
                self._turn_q.put_nowait({"type": "error", "message": str(err.get("message") or "помилка Realtime")})
        elif kind == "response.created":
            if self._awaiting_created and self._turn_q is not None:
                self._response_id = (ev.get("response") or {}).get("id", "")
                self._awaiting_created = False
                self._response_open = True
                if self._mark:
                    self._mark("response_created")
        elif kind == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") == "message" and ev.get("response_id", self._response_id) == self._response_id:
                self._items.append(item.get("id", ""))
        elif kind in ("response.output_text.delta", "response.output_audio_transcript.delta", "response.done"):
            if kind == "response.output_audio_transcript.delta":
                # gpt-live: its own voice is dropped (the audio deltas match nothing here), the words it says are kept.
                if not self._audio_out:
                    return
                ev = {**ev, "type": "response.output_text.delta"}
            rid = ev.get("response_id") or (ev.get("response") or {}).get("id")
            if rid == self._response_id and self._turn_q is not None:
                if kind == "response.done":
                    self._response_open = False
                elif self._mark and not self._first_delta:
                    self._first_delta = True
                    self._mark("first_delta")
                self._turn_q.put_nowait(ev)

    # ------------------------------------------------------------------ one answer

    async def stream(self, history: list[dict], ctx: TurnContext) -> AsyncIterator[str]:
        """The answer to what was just said (it is already in the Realtime conversation, as audio)."""
        tools = [flat_tool(t) for t in ctx.extra_tools]
        if self.searchable:
            tools.insert(0, flat_tool(WEB_SEARCH_TOOL))
        key = json.dumps(tools, sort_keys=True)
        update: dict = {"type": "realtime"}
        if ctx.system_prompt != self._instructions:
            update["instructions"] = ctx.system_prompt
        if key != self._tools_key:
            update["tools"] = tools
        if len(update) > 1:
            await self._send({"type": "session.update", "session": update})
            self._instructions, self._tools_key = ctx.system_prompt, key

        self._turn_q = queue = asyncio.Queue()
        self._mark, self._first_delta = ctx.on_mark, False
        self._items = []
        searches = rounds = 0
        unanswered: list[str] = []  # calls of the model whose output has not been sent yet
        try:
            while True:
                self._response_id, self._awaiting_created = "", True
                create: dict = {"type": "response.create"}
                if rounds >= MAX_TOOL_ROUNDS:
                    create["response"] = {"tool_choice": "none"}  # it kept calling tools: now it has to answer
                await self._send(create)
                if ctx.on_mark:
                    ctx.on_mark("create_sent")
                text, calls, done = "", [], None
                while done is None:
                    try:
                        ev = await asyncio.wait_for(queue.get(), _RESPONSE_TIMEOUT_S)
                    except TimeoutError:
                        raise RuntimeError("Realtime не відповів вчасно. Спробуйте ще раз.") from None
                    kind = ev["type"]
                    if kind == "error":
                        raise RuntimeError(ev["message"])
                    if kind == "response.output_text.delta":
                        text += ev.get("delta", "")
                        yield ev.get("delta", "")
                    else:
                        done = ev["response"]
                for item in done.get("output") or []:
                    if item.get("type") == "function_call":
                        calls.append(item)
                self._report_usage(done.get("usage") or {}, ctx)
                if not calls:
                    return
                rounds += 1
                unanswered = [c.get("call_id", "") for c in calls]
                if not text.strip() and any(c.get("name") == "web_search" for c in calls):
                    yield random.choice(_CUES) + " "
                yield SEGMENT_END  # what was said before the tool is spoken while the tool runs
                for call in calls:
                    if call.get("name") == "web_search" and searches >= MAX_SEARCHES_PER_TURN:
                        output = SEARCH_LIMIT_NOTE  # asked for after the limit: not run, not counted
                    else:
                        if call.get("name") == "web_search":
                            searches += 1
                            if ctx.on_search:
                                ctx.on_search()
                        output = await run_tool_call(call.get("name", ""), call.get("arguments") or "", ctx, self._search)
                    await self._send_output(call.get("call_id", ""), output)
                    unanswered.remove(call.get("call_id", ""))
        finally:
            if self._response_open:  # a cancelled turn must stop the generation (and the bill)
                self._response_open = False
                with contextlib.suppress(Exception):
                    await self._send({"type": "response.cancel"})
            for call_id in unanswered:  # cut off while a tool ran: a call left without output confuses the next answer
                with contextlib.suppress(Exception):
                    await self._send_output(call_id, _CANCELLED_NOTE)
            self._turn_q, self._mark = None, None

    async def _send_output(self, call_id: str, output: str) -> None:
        await self._send(
            {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": output}}
        )

    def _report_usage(self, usage: dict, ctx: TurnContext) -> None:
        details = usage.get("input_token_details") or {}
        total_in = int(usage.get("input_tokens") or 0)
        audio_in = int(details.get("audio_tokens") or 0)
        if ctx.on_usage:
            ctx.on_usage(max(0, total_in - audio_in), int(usage.get("output_tokens") or 0))
        if ctx.on_audio and audio_in:
            ctx.on_audio(audio_in)

    async def replace_spoken(self, heard: str) -> None:
        """After a cut-off: the model wrote the whole answer faster than it was spoken. Keep in its conversation only
        what the user actually got to hear."""
        items, self._items = self._items, []
        if self._conn is None or not items:
            return
        for item_id in items:
            if item_id:
                await self._send({"type": "conversation.item.delete", "item_id": item_id})
        if heard.strip():
            # With an id of ours, so that it can be replaced once more when the page says how much had really played.
            self._cuts += 1
            item = {**_text_item("assistant", heard.strip()), "id": f"heard_{self._cuts}"}
            await self._send({"type": "conversation.item.create", "item": item})
            self._items = [item["id"]]

    async def drop_item(self, item_id: str) -> None:
        """Take a phrase out of the conversation (it turned out to be the agent's own echo)."""
        if item_id:
            await self._send({"type": "conversation.item.delete", "item_id": item_id})
