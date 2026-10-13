"""The web page's «Realtime» engine, beside GPT-Live.

The browser talks to OpenAI Realtime over WebRTC, as it does to GPT-Live. Realtime has no delegation:
it calls the session tools itself, and this sideband runs them. It also keeps the transcript in the
conversation log, so a new call (or a switch to GPT-Live) remembers everything.

Realtime treats any sound as a turn, so it never answers on its own (create_response false): the
page judges each turn — a cough, a lone «угу», the TV or people talking among themselves get no
reply — and asks for an answer itself. For the same reason a sound doesn't cut her off; the page's
barge-in does that.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

from openai import AsyncOpenAI

from config import (
    OPENAI_REALTIME_EFFORT,
    OPENAI_REALTIME_MODEL,
    OPENAI_REALTIME_TRANSCRIBE,
    REALTIME_EAGERNESS,
    REALTIME_MAX_CONTEXT_TOKENS,
    REALTIME_MAX_OUTPUT_TOKENS,
    REALTIME_SILENCE_MS,
    REALTIME_TURN_DETECTION,
)
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.live_schemas import LIVE_BACKEND_TOOLS
from voice.conversation import ConversationLog, remember_tool_result

logger = logging.getLogger(__name__)

# The voice is picked on the page in this engine (Realtime's presets differ from Live's).
REALTIME_TOOLS: list[dict] = [t for t in LIVE_BACKEND_TOOLS if t["name"] != "change_voice"]

# Speed is a real parameter here (Live has none): the page's «Темп» maps onto it.
_SPEED_VALUES = {"slow": 0.9, "normal": 1.0, "fast": 1.12}
_HISTORY_CHARS = 8000
_EAGERNESS = ("low", "medium", "high", "auto")
_EFFORTS = ("minimal", "low", "medium", "high")


def history_text(log: ConversationLog, max_chars: int = _HISTORY_CHARS) -> str:
    """The conversation so far as plain lines, newest kept (for the Realtime instructions)."""
    lines: list[str] = []
    for item in log.live_input():
        text = "".join(part.get("text", "") for part in item.get("content") or []).strip()
        if not text:
            continue
        role = item.get("role")
        lines.append(("User: " if role == "user" else "You: " if role == "assistant" else "Note: ") + text)
    return "\n".join(lines)[-max_chars:]


def _turn_detection() -> dict:
    if REALTIME_TURN_DETECTION == "server":
        return {
            "type": "server_vad",
            "silence_duration_ms": REALTIME_SILENCE_MS,
            "create_response": False,
            "interrupt_response": False,
        }
    # The end of a turn judged by meaning, like Live: no cut-off at a pause mid-thought.
    eagerness = REALTIME_EAGERNESS if REALTIME_EAGERNESS in _EAGERNESS else "auto"
    return {"type": "semantic_vad", "eagerness": eagerness, "create_response": False, "interrupt_response": False}


def realtime_session(*, instructions: str, language: str, voice: str, speed: str = "normal") -> dict:
    session: dict[str, Any] = {
        "type": "realtime",
        "model": OPENAI_REALTIME_MODEL,
        "instructions": instructions,
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                # A phone or laptop mic close to the speaker: keeps the room out of her ears.
                "noise_reduction": {"type": "near_field"},
                "transcription": {"model": OPENAI_REALTIME_TRANSCRIBE, "language": language},
                "turn_detection": _turn_detection(),
            },
            "output": {"voice": voice, "speed": _SPEED_VALUES.get(speed, 1.0)},
        },
        "tools": REALTIME_TOOLS,
        "tool_choice": "auto",
        "max_output_tokens": REALTIME_MAX_OUTPUT_TOKENS,
        "truncation": {
            "type": "retention_ratio",
            "retention_ratio": 0.75,
            "token_limits": {"post_instructions": REALTIME_MAX_CONTEXT_TOKENS},
        },
    }
    # Reasoning models only (gpt-realtime-2.x); the lowest effort keeps the pause before she speaks short.
    if OPENAI_REALTIME_EFFORT in _EFFORTS and "realtime-2" in OPENAI_REALTIME_MODEL:
        session["reasoning"] = {"effort": OPENAI_REALTIME_EFFORT}
    return session


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


class RealtimeBridge:
    """Sideband on a Realtime WebRTC call: runs its tool calls and keeps the transcript. Same surface
    as SidebandToolBridge where the rest of the server touches it."""

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        call_id: str,
        executor: ToolExecutor,
        conversation: ConversationLog,
        on_closed: Callable[[RealtimeBridge], None] | None = None,
        max_duration_s: float | None = None,
    ) -> None:
        self._client = client
        self.session_id = call_id
        self._executor = executor
        self._conversation = conversation
        self._on_closed = on_closed
        self._max_duration_s = max_duration_s
        self._connection: Any = None
        self._closed = asyncio.Event()
        self._tool_tasks: set[asyncio.Task] = set()
        self._user_turns: list[str] = []
        self._assistant_turns: list[str] = []
        # The page's «is this addressed to you?» checks (out of band): not part of the conversation.
        self._side_responses: set[str] = set()
        # item id → the user's words: a turn the page deletes (not for her) leaves the history too.
        self._heard: dict[str, str] = {}
        self.end_requested = False
        self.restart_for_voice = False  # set by change_voice on Live calls; nothing to restart here

    @property
    def busy(self) -> bool:
        return any(not task.done() for task in self._tool_tasks)

    @property
    def is_open(self) -> bool:
        return self._connection is not None and not self._closed.is_set()

    async def run(self) -> None:
        expiry: asyncio.Task | None = None
        try:
            async with self._client.realtime.connect(call_id=self.session_id) as connection:
                self._connection = connection
                logger.info("realtime.sideband.connected call_id=%s", self.session_id)
                if self._max_duration_s:
                    expiry = asyncio.create_task(self._expire_after(self._max_duration_s))
                async for event in connection:
                    await self._handle_event(event)
        except Exception:
            logger.exception("realtime.sideband.failed call_id=%s", self.session_id)
        finally:
            self._closed.set()
            self._connection = None
            if expiry is not None:
                expiry.cancel()
            for task in list(self._tool_tasks):
                task.cancel()
            logger.info("realtime.sideband.closed call_id=%s", self.session_id)
            if self._on_closed:
                self._on_closed(self)

    async def _expire_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        logger.info("realtime.session.expired call_id=%s", self.session_id)
        await self.close()

    async def close(self) -> None:
        if self.is_open:
            try:
                await self._client.realtime.calls.hangup(self.session_id)
            except Exception:
                logger.debug("realtime hangup failed", exc_info=True)
            try:
                await self._connection.close()
            except Exception:
                logger.debug("realtime sideband close failed", exc_info=True)

    async def say(self, text: str) -> None:
        """Have the assistant tell the user something."""
        if not self.is_open:
            return
        await self._send_system(f"Tell the user briefly: {text}")
        await self._connection.send({"type": "response.create"})

    async def append_instruction(self, text: str, **_: Any) -> None:
        """Context for the model without a reply (a new speed or manner)."""
        if self.is_open and text:
            await self._send_system(text)

    async def set_speed(self, speed: str) -> None:
        """«Говори повільніше» by voice: Realtime has a real speed, applied from her next reply."""
        if self.is_open:
            await self._connection.send({
                "type": "session.update",
                "session": {"type": "realtime", "audio": {"output": {"speed": _SPEED_VALUES.get(speed, 1.0)}}},
            })

    async def _send_system(self, text: str) -> None:
        await self._connection.send({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "system", "content": [{"type": "input_text", "text": text}]},
        })

    async def _handle_event(self, event: Any) -> None:
        etype = _attr(event, "type")
        if etype == "response.created":
            response = _attr(event, "response")
            if (_attr(response, "metadata") or {}).get("purpose"):
                self._side_responses.add(_attr(response, "id"))
            return
        if _attr(event, "response_id") in self._side_responses:
            return
        if etype == "conversation.item.input_audio_transcription.completed":
            text = (_attr(event, "transcript") or "").strip()
            if text:
                self._conversation.add("user", text)
                self._conversation.end_turn()
                self._user_turns = (self._user_turns + [text])[-20:]
                self._heard[_attr(event, "item_id")] = text
                while len(self._heard) > 50:
                    self._heard.pop(next(iter(self._heard)))
            return
        if etype == "conversation.item.deleted":
            # The page judged it not for her (talk nearby, the TV): it must not come back with the
            # history of the next call, nor cost tokens there.
            text = self._heard.pop(_attr(event, "item_id"), None)
            if text:
                self._conversation.forget("user", text)
                if text in self._user_turns:
                    self._user_turns.remove(text)
            return
        if etype == "response.output_audio_transcript.done":
            text = (_attr(event, "transcript") or "").strip()
            if text:
                self._conversation.add("assistant", text)
                self._conversation.end_turn()
                self._assistant_turns = (self._assistant_turns + [text])[-5:]
            return
        if etype == "error":
            logger.error("realtime.error call_id=%s detail=%s", self.session_id, _attr(event, "error") or event)
            return
        if etype != "response.done":
            return
        response = _attr(event, "response")
        if _attr(response, "id") in self._side_responses:
            self._side_responses.discard(_attr(response, "id"))
            return
        calls = [item for item in (_attr(response, "output") or []) if _attr(item, "type") == "function_call"]
        if calls:
            task = asyncio.create_task(self._run_tools(calls))
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)

    async def _run_tools(self, calls: list[Any]) -> None:
        ctx = ToolExecutionContext(
            session_id=self.session_id,
            delegation_id=None,
            user_utterances=list(self._user_turns) or None,
            assistant_utterances=self._assistant_turns[-3:] or None,
        )
        outputs = await asyncio.gather(*(self._run_tool(c, ctx) for c in calls))
        if not self.is_open:
            return
        for call, output in zip(calls, outputs, strict=True):
            await self._connection.send({
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": _attr(call, "call_id"), "output": output},
            })
        await self._connection.send({"type": "response.create", "event_id": f"continue_{uuid.uuid4().hex[:8]}"})

    async def _run_tool(self, call: Any, ctx: ToolExecutionContext) -> str:
        name = _attr(call, "name")
        try:
            result = await self._executor.execute(name, _attr(call, "arguments"), ctx)
        except Exception:
            logger.exception("realtime.tool.failed tool_name=%s", name)
            return '{"ok": false, "status": "error", "message": "Не вдалося виконати."}'
        logger.info("realtime.tool.result call_id=%s tool_name=%s status=%s", self.session_id, name, result.status)
        if name == "end_conversation" and result.ok:
            self.end_requested = True
        remember_tool_result(self._conversation, name, result.message)
        return result.to_json()
