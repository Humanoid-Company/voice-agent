"""Per-browser state for the hosted version.

Each browser gets a random client id (kept in its localStorage): its own voice settings,
conversation history and tool executor, so two people testing at once never share a conversation.

State lives in memory only: a server restart (Render redeploy / free-tier sleep) forgets it.
Fine for team testing; use a database for anything longer.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from tools.executor import ToolExecutionContext, ToolExecutor
from tools.results import ToolResult
from voice.background import BackgroundLog, make_openai_summarizer
from voice.conversation import ConversationLog
from voice.options import (
    LANGUAGE_OPTIONS,
    SPEED_OPTIONS,
    STYLE_OPTIONS,
    VOICE_PERSONAS,
    VOICE_REQUEST_RE,
    _sanitize_name,
    asked_for_voice_change,
    delivery_instruction,
    voice_request_target,
)

logger = logging.getLogger(__name__)

_MAX_USERS = 500
_IDLE_EVICT_S = 24 * 3600
_background: set[asyncio.Task] = set()
VOICE_SWITCH_DEDUP_S = 15.0


@dataclass
class WebUser:
    client_id: str
    executor: ToolExecutor = field(init=False)
    assistant_name: str | None = None
    language: str = "uk"
    voice: str | None = None
    speed: str = "normal"
    style: str = "normal"
    prompt_variant: str = "v4"  # delivery prompt (v4 default = v3 + full answers, v3 = v2 + a person, v2, v1 the older one), sent by the page with every call
    # The voice was changed during a call: the page reconnects with the new voice and the same
    # conversation once the current session closes.
    reconnect_pending: bool = False
    # Several paths can act on one «зміни голос …» (browser recogniser, transcript, the model):
    # within this window after a switch the others are the same request, not a new one.
    voice_switched_at: float = 0.0
    voice_switch_note: str = ""  # who switched it — shown on the page next to «Голос змінено»
    # Dialogue history independent of the Live session (and so of the voice).
    conversation: ConversationLog = field(default_factory=ConversationLog)
    # What people nearby said while Єва was paused — handed to her on the next wake.
    background: BackgroundLog = field(default_factory=lambda: BackgroundLog(_summarize_background))
    last_seen: float = field(default_factory=time.time)
    bridges: set[Any] = field(default_factory=set)  # open calls (SidebandToolBridge)

    def switch_voice_by_request(self, utterance: str) -> bool:
        """«Єва, зміни голос на …» heard in the transcript: switch now (the page reconnects)."""
        if time.time() - self.voice_switched_at < VOICE_SWITCH_DEDUP_S:
            return False
        voice = voice_request_target(utterance, self.voice)
        if voice is None or voice == self.voice:
            return False
        # The matched words are logged so a voice switch nobody asked for can be traced.
        match = VOICE_REQUEST_RE.search(utterance)
        heard = utterance[match.start(): match.end() + 30] if match else ""
        logger.info("web.voice.by_request %s → %s heard=%r", self.voice, voice, heard)
        self.voice_switch_note = f"почула: «{heard.strip()}»"
        self.voice = voice
        self.reconnect_pending = True
        self.voice_switched_at = time.time()
        return True

    def must_keep_session(self) -> bool:
        """A pause closes the Live session after 30 s — but not while a tool still runs in it."""
        return any(getattr(bridge, "busy", False) for bridge in self.bridges)

    async def append_instruction(self, text: str) -> None:
        """Tell the running call something without a reply (e.g. the new language)."""
        for bridge in list(self.bridges):
            try:
                await bridge.append_instruction(text)
            except Exception:
                logger.debug("append_instruction failed", exc_info=True)

    async def apply_delivery(self) -> None:
        """Push the current speed/style into the running call."""
        text = delivery_instruction(self.speed, self.style, changed=True)
        for bridge in list(self.bridges):
            try:
                await bridge.append_instruction(text)
            except Exception:
                logger.debug("apply_delivery failed", exc_info=True)

    def __post_init__(self) -> None:
        self.executor = _build_executor(self)


_summarizer = None


def _summarize_background(text: str) -> str:
    """Compress a long pause's overheard talk (lazy: one OpenAI client for the whole server)."""
    global _summarizer
    if _summarizer is None:
        from openai import OpenAI

        from config import OPENAI_API_KEY, OPENAI_LIVE_BACKEND_MODEL

        _summarizer = make_openai_summarizer(OpenAI(api_key=OPENAI_API_KEY), OPENAI_LIVE_BACKEND_MODEL)
    return _summarizer(text)


def _build_executor(user: WebUser) -> ToolExecutor:
    """The session tools (tools/live_schemas.py). A new tool: describe it there, register it here."""
    executor = ToolExecutor()

    def set_name(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        name = _sanitize_name(str(args.get("name") or ""))
        if not name:
            return ToolResult(ok=False, status="needs_more_info", message="Не зрозумів нового імені.")
        user.assistant_name = name
        return ToolResult(ok=True, status="ok", message=f"Тепер мене звати {name}.")

    def change_language(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        language = str(args.get("language") or "").strip().lower()
        if language not in LANGUAGE_OPTIONS:
            return ToolResult(ok=False, status="needs_more_info", message="Доступні мови: uk, ru, en.")
        user.language = language
        lang_name = LANGUAGE_OPTIONS[language]
        # The running call was started with «Speak exclusively in …» the old language: without this
        # she went back to it a few replies later (the desktop did this already).
        task = asyncio.get_running_loop().create_task(
            user.append_instruction(f"From now on speak exclusively in {lang_name}.")
        )
        _background.add(task)
        task.add_done_callback(_background.discard)
        return ToolResult(
            ok=True,
            status="ok",
            message=f"Мову змінено на {lang_name}. Нічого про це не кажи: просто говори далі цією мовою, а "
            "якщо вже почала відповідати — продовжуй, не починай заново.",
        )

    def change_voice(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        voice = str(args.get("voice") or "").strip().lower()
        # Only the voices offered in the page's picker, so the picker can always show the choice.
        if voice not in VOICE_PERSONAS:
            names = ", ".join(f"{p.label} ({p.voice})" for p in VOICE_PERSONAS.values())
            return ToolResult(ok=False, status="needs_more_info", message=f"Такого голосу немає. Доступні: {names}.")
        if voice == user.voice or time.time() - user.voice_switched_at < VOICE_SWITCH_DEDUP_S:
            return ToolResult(ok=True, status="ok", message="Голос уже змінено. Нічого про це не кажи.")
        last = (ctx.user_utterances or [""])[-1][-60:]
        if not asked_for_voice_change(ctx.user_utterances):
            logger.info("web.voice.tool_refused voice=%s (user didn't mention the voice)", voice)
            return ToolResult(
                ok=False,
                status="error",
                message="Користувач не просив змінити голос — не змінюй його, просто продовжуй розмову.",
            )
        logger.info("web.voice.by_tool %s → %s last_user=%r", user.voice, voice, last)
        user.voice_switch_note = f"попросила модель; останнє від вас: «{last}»"
        user.voice = voice
        user.reconnect_pending = True
        user.voice_switched_at = time.time()
        for bridge in list(user.bridges):
            bridge.restart_for_voice = True  # the page reconnects with the new voice and the same history
        return ToolResult(
            ok=True,
            status="ok",
            message="Голос змінено. Нічого про це не кажи — розмова одразу продовжиться новим голосом.",
        )

    def set_voice_style(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        speed = str(args.get("speed") or user.speed).strip().lower()
        style = str(args.get("style") or user.style).strip().lower()
        if speed not in SPEED_OPTIONS or style not in STYLE_OPTIONS:
            return ToolResult(ok=False, status="needs_more_info", message="Темп: slow/normal/fast, стиль: calm/normal/expressive.")
        user.speed, user.style = speed, style
        task = asyncio.get_running_loop().create_task(user.apply_delivery())
        _background.add(task)
        task.add_done_callback(_background.discard)
        return ToolResult(ok=True, status="ok", message="Готово, говорю так з наступного речення.")

    def end_conversation(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        return ToolResult(ok=True, status="ok", message="Попрощайся коротко.")

    executor.register("set_assistant_name", set_name)
    executor.register("change_language", change_language)
    executor.register("change_voice", change_voice)
    executor.register("set_voice_style", set_voice_style)
    executor.register("end_conversation", end_conversation)
    return executor


class WebUserRegistry:
    def __init__(self) -> None:
        self._users: dict[str, WebUser] = {}
        self._lock = threading.Lock()

    def get(self, client_id: str) -> WebUser:
        with self._lock:
            user = self._users.get(client_id)
            if user is None:
                self._evict_idle()
                user = WebUser(client_id=client_id)
                self._users[client_id] = user
                logger.info("web.user.created users=%s", len(self._users))
            user.last_seen = time.time()
            return user

    def _evict_idle(self) -> None:
        now = time.time()
        stale = [cid for cid, u in self._users.items() if now - u.last_seen > _IDLE_EVICT_S and not u.bridges]
        for cid in stale:
            del self._users[cid]
        while len(self._users) >= _MAX_USERS:
            oldest = min(self._users.values(), key=lambda u: u.last_seen)
            del self._users[oldest.client_id]
