"""Per-browser state for the hosted version.

Each browser gets a random client id (kept in its localStorage): its own voice settings,
conversation history and tool executor, so two people testing at once never share a conversation.

State lives in memory, except the conversation: it is saved to the database (server/conversations.py) a
couple of seconds after each change and restored when the browser comes back after a restart, so Єва
remembers it and the page shows it again. Voice settings come from the page with every call.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from config import WEB_SEARCH_MAX_CALLS_PER_TURN
from integrations.web_search import WebSearchRateLimiter
from server import conversations, knowledge
from server.db import StoreError
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.results import ToolResult
from tools.web_search_tool import web_search_tool_result
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
# A conversation is saved this long after it changed: transcript deltas come many a second, one write covers them.
SAVE_DELAY_S = 2.0
# Camera (look_at_camera). A look is what is in front of the camera now: the page snaps a frame as soon
# as the request goes to the backend (and again when this tool is called). Taking the latest periodic
# frame (up to 5 s old) missed what was just held up — prod: «підніс до камери, а вона нічого не
# побачила». A frame that arrived after the work started, or this long before the call, is «now»: the
# one snapped at the start is there before the backend has even decided to look (measured on GPT-Live:
# the tool no longer waits ~0.4 s for the upload)…
FRAME_FOR_LOOK_S = 0.5
# …the tool waits this long for it…
FRAME_WAIT_S = 2.5
# …and without it (an old cached page, a lost upload) takes the page's latest periodic frame if it is
# no older than this.
FRAME_FALLBACK_S = 6.0


@dataclass
class WebUser:
    client_id: str
    executor: ToolExecutor = field(init=False)
    assistant_name: str | None = None
    language: str = "uk"
    voice: str | None = None
    speed: str = "normal"
    style: str = "normal"
    prompt_variant: str = "v5"  # delivery prompt (v5 default = v4 condensed, v4 = v3 + full answers), sent by the page with every call
    # The voice was changed during a call: the page reconnects with the new voice and the same
    # conversation once the current session closes.
    reconnect_pending: bool = False
    # Several paths can act on one «зміни голос …» (browser recogniser, transcript, the model):
    # within this window after a switch the others are the same request, not a new one.
    voice_switched_at: float = 0.0
    voice_switch_note: str = ""  # who switched it — shown on the page next to «Голос змінено»
    # Dialogue history independent of the Live session (and so of the voice), saved to the database.
    conversation: ConversationLog = field(default_factory=lambda: ConversationLog(merge_interleaved=True))
    conversation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    conversation_started_at: float = field(default_factory=time.time)
    _save_timer: threading.Timer | None = field(default=None, repr=False)
    _save_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _write_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # What people nearby said while Єва was paused — handed to her on the next wake.
    background: BackgroundLog = field(default_factory=lambda: BackgroundLog(_summarize_background))
    last_seen: float = field(default_factory=time.time)
    bridges: set[Any] = field(default_factory=set)  # open calls (SidebandToolBridge)
    # The latest camera frame (JPEG), in memory only; camera_on is false once the page turns it off.
    # When the server last closed one of its calls at the session cap (MAX_SESSION_MINUTES): the page
    # opens a new one with the whole conversation if the user was still talking.
    session_expired_at: float = 0.0
    # «Консьєрж»: who the guest was confirmed as (confirm_guest); cleared by «Наступний гість».
    guest: dict | None = None
    camera_on: bool = False
    camera_frame: bytes | None = None
    camera_frame_at: float = 0.0

    def set_camera_frame(self, jpeg: bytes) -> None:
        self.camera_frame, self.camera_frame_at, self.camera_on = jpeg, time.time(), True

    def camera_off(self) -> None:
        self.camera_on, self.camera_frame, self.camera_frame_at = False, None, 0.0

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
        """A pause closes the Live session after 15 s (web/config.js) — but not while a tool still runs in it."""
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
        self.conversation.on_change = self._save_soon

    # ── the conversation in the database (server/conversations.py) ──
    def _save_soon(self) -> None:
        with self._save_lock:
            if self._save_timer is None:
                self._save_timer = threading.Timer(SAVE_DELAY_S, self.save_conversation)
                self._save_timer.daemon = True
                self._save_timer.start()

    def save_conversation(self) -> None:
        """Write it now (a pending save is folded in). A store that is down only loses this write."""
        with self._save_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
        with self._write_lock:  # one write at a time: an older snapshot never lands after a newer one
            try:
                conversations.store.save(
                    self.conversation_id, self.client_id, self.conversation_started_at, self.conversation.to_list()
                )
            except StoreError:
                logger.warning("web.conversation.save_failed client=%s", self.client_id[:8])

    def restore_conversation(self) -> None:
        """After a restart: the browser's latest conversation becomes the current one again."""
        try:
            saved = conversations.store.latest(self.client_id)
        except StoreError:
            return
        if saved and not len(self.conversation):
            self.conversation_id, self.conversation_started_at = saved["id"], saved["started_at"]
            self.conversation.load(saved["turns"])
            logger.info("web.conversation.restored client=%s turns=%s", self.client_id[:8], len(saved["turns"]))

    def new_conversation(self) -> None:
        """«Нова розмова»: the current one stays saved (in «Історія»), a new one starts empty."""
        self.save_conversation()
        self.conversation_id, self.conversation_started_at = uuid.uuid4().hex, time.time()
        self.conversation.clear()
        self.guest = None

    def resume_conversation(self, saved: dict) -> None:
        """«Продовжити»: a saved conversation becomes the current one (the current one is saved first)."""
        self.save_conversation()
        self.conversation_id, self.conversation_started_at = saved["id"], saved["started_at"]
        self.conversation.load(saved["turns"])


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

    # At most a few searches per request (per browser), so one question can't loop the paid API.
    limiter = WebSearchRateLimiter(max_per_turn=WEB_SEARCH_MAX_CALLS_PER_TURN)

    def web_search(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        return web_search_tool_result(
            args, session_id=ctx.session_id, delegation_id=ctx.delegation_id, limiter=limiter
        )

    def look_at_camera(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        called_at = time.time()
        fresh_since = called_at - FRAME_FOR_LOOK_S
        if ctx.extras.get("work_started_at"):
            fresh_since = min(fresh_since, ctx.extras["work_started_at"])
        deadline = time.monotonic() + FRAME_WAIT_S
        while True:  # runs in a worker thread: waiting here never stalls the call
            if not user.camera_on:
                return ToolResult(
                    ok=False,
                    status="camera_off",
                    message="Камера вимкнена. Скажи коротко, що не бачиш, і попроси увімкнути «Камера» на сторінці.",
                )
            frame, frame_at = user.camera_frame, user.camera_frame_at
            if frame and frame_at >= fresh_since:
                break  # snapped for this look
            if time.monotonic() >= deadline:
                if frame and time.time() - frame_at <= FRAME_FALLBACK_S:
                    break
                return ToolResult(
                    ok=False,
                    status="no_frame",
                    message="Камера увімкнена, але кадр не надходить. Попроси перевірити камеру на сторінці.",
                )
            time.sleep(0.1)
        age = time.time() - frame_at
        question = str(args.get("question") or "").strip()[:300]
        logger.info("web.camera.look bytes=%s age_s=%.1f", len(frame), age)
        return ToolResult(
            ok=True,
            status="ok",
            message="Кадр з камери користувача — у зображенні нижче. Опиши, що на ньому"
            + (f", відповідаючи на питання: «{question}»." if question else "."),
            image_jpeg=frame,
        )

    def knowledge_lookup(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        query = str(args.get("query") or "").strip()[:300]
        try:
            found = knowledge.store.lookup(query, str(args.get("set") or ""))
        except knowledge.KnowledgeError as exc:
            return ToolResult(ok=False, status="error", message=f"{exc} Скажи, що зараз не можеш заглянути в записи команди.")
        logger.info("web.knowledge.lookup total=%s shown=%s", found["total"], found["shown"])
        if not found["total"]:
            return ToolResult(
                ok=True, status="empty",
                message="Команда ще не завантажила знань (сторінка «Знання»). Скажи, що про це в тебе записів немає.",
            )
        head = (
            f"Записи команди ({found['shown']} з {found['total']}"
            + (", найближчі до запиту" if found["shown"] < found["total"] else ", усі")
            + "). Відповідай лише з них; чого тут немає — того ти не знаєш.\n"
        )
        return ToolResult(ok=True, status="ok", message=head + found["text"], data={"total": found["total"], "shown": found["shown"]})

    def confirm_guest(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        name = " ".join(str(args.get("full_name") or "").split())[:200]
        verified = bool(args.get("verified"))
        user.guest = {"name": name, "verified": verified, "at": time.time()}
        logger.info("web.concierge.guest verified=%s", verified)
        if verified:
            return ToolResult(ok=True, status="ok", message=f"Гостя підтверджено: {name}. Привітай на ім'я і спитай, як минає день.")
        return ToolResult(
            ok=True, status="not_verified",
            message="Не підтверджено. Ввічливо скажи, що не вдалося підтвердити, і запропонуй покликати когось із команди.",
        )

    executor.register("set_assistant_name", set_name)
    executor.register("change_language", change_language)
    executor.register("change_voice", change_voice)
    executor.register("set_voice_style", set_voice_style)
    executor.register("end_conversation", end_conversation)
    executor.register("web_search", web_search, run_in_thread=True)  # network: off the event loop
    executor.register("look_at_camera", look_at_camera, run_in_thread=True)  # may wait for a frame
    executor.register("knowledge_lookup", knowledge_lookup, run_in_thread=True)  # database
    executor.register("confirm_guest", confirm_guest)
    return executor


class WebUserRegistry:
    def __init__(self) -> None:
        self._users: dict[str, WebUser] = {}
        self._lock = threading.Lock()

    def get(self, client_id: str) -> WebUser:
        with self._lock:
            user = self._users.get(client_id)
            created = user is None
            if user is None:
                self._evict_idle()
                user = WebUser(client_id=client_id)
                self._users[client_id] = user
                logger.info("web.user.created users=%s", len(self._users))
            user.last_seen = time.time()
        if created:
            user.restore_conversation()  # outside the lock: the database may take a moment
        return user

    def save_all(self) -> None:
        """Server shutting down (a deploy): write the conversations still waiting to be saved."""
        with self._lock:
            pending = [u for u in self._users.values() if u._save_timer is not None]
        for user in pending:
            user.save_conversation()

    def _evict_idle(self) -> None:
        now = time.time()
        stale = [cid for cid, u in self._users.items() if now - u.last_seen > _IDLE_EVICT_S and not u.bridges]
        for cid in stale:
            self._forget(cid)
        while len(self._users) >= _MAX_USERS:
            self._forget(min(self._users.values(), key=lambda u: u.last_seen).client_id)

    def _forget(self, client_id: str) -> None:
        user = self._users.pop(client_id)
        if user._save_timer is not None:  # not written yet: now, before it is gone
            user.save_conversation()
