"""HTTP backend for the hosted web version (deploy on Render).

    uvicorn server.app:app --host 0.0.0.0 --port 8000

The browser (web/, deployed on Vercel) talks to OpenAI GPT-Live directly over WebRTC for
audio; this server only:
  - creates the Live session from the browser's SDP offer (the OpenAI key never leaves it),
  - attaches a sideband to that session and runs the tools (voice, name, language…).

Env: OPENAI_API_KEY, PUBLIC_BACKEND_URL, FRONTEND_ORIGINS, optional ACCESS_CODE. See DEPLOY.md.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
import urllib.parse
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from pydantic import BaseModel

from config import (
    OPENAI_API_KEY,
    OPENAI_LIVE_BACKEND_EFFORT,
    OPENAI_LIVE_BACKEND_MODEL,
    OPENAI_LIVE_MODEL,
    OPENAI_LIVE_PARALLEL_TOOLS,
    OPENAI_LIVE_VOICE,
    WEB_SEARCH_API_KEY,
)
from prompts.backend_prompt import build_backend_prompt, local_today
from prompts.live_prompt import build_live_prompt
from server import conversations, knowledge, usage
from server.call_log import CallLogStore
from server.db import StoreError
from server.knowledge_import import KnowledgeImportError, make_openai_extract, parse_file
from server.live_bridge import SidebandToolBridge
from server.web_users import WebUser, WebUserRegistry
from tools.live_schemas import CONFIRM_GUEST_TOOL, KNOWLEDGE_LOOKUP_TOOL, LIVE_BACKEND_TOOLS, LOOK_AT_CAMERA_TOOL
from voice.background import wake_commentary
from voice.delegation import responses_delegation
from voice.options import (
    LANGUAGE_OPTIONS,
    SPEED_OPTIONS,
    STYLE_OPTIONS,
    VOICE_PERSONAS,
    delivery_instruction,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s")
for noisy in ("httpx", "httpcore", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("server")

PUBLIC_BACKEND_URL = os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000").rstrip("/")
FRONTEND_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("FRONTEND_ORIGINS", "*").split(",") if o.strip()]
ACCESS_CODE = os.getenv("ACCESS_CODE", "").strip()
# The hidden /logs page reads whole conversations: only with this token (unset = logs can't be read).
LOGS_TOKEN = os.getenv("LOGS_TOKEN", "").strip()
# Cap on one Live session (cost guard: a tab left open in a noisy room kept a call going). 10 min, not 30: a
# real talk goes on — the page opens a new session with the whole conversation (/api/me «expired»).
MAX_SESSION_S = float(os.getenv("MAX_SESSION_MINUTES", "10")) * 60
# The page asks within this long after the cap closed a call whether to carry on.
EXPIRED_FRESH_S = 60.0

_WEB_NOTE = "\nWeb demo: you run in a browser tab for the team to try."
# GPT-Live takes no images: the backend looks through the camera (look_at_camera) and tells her. This
# overrides «answer everything else yourself» in the delegation rules.
_CAMERA_NOTE = (
    "\nCamera: the page can show the backend the user's camera. You cannot see by yourself, so anything "
    "about what is visible — «подивись на мене», «що в мене в руці», «що тут написано», «як я "
    "виглядаю», «що ти бачиш» — always goes to the backend at once (it looks and tells you), even if "
    "you think the camera is off. People show things without naming them: a short question that "
    "points at something — «а це шо?», «це що таке?», «що це?», «а так?», «а тепер?», «шо тєпєрь "
    "показує», «глянь» — means they hold it up to the camera. Send it to the backend at once; never "
    "answer «не знаю, про що ти» or ask what they mean. Every new look is a new frame: what they show "
    "changes. Say what it saw as if you looked yourself («Бачу…»), in your own words; never guess or "
    "describe anything it did not report. You have never seen anything by yourself: what is visible "
    "NOW always needs a new look, even right after one («а зараз?», «що ти бачиш зараз»), never «the "
    "same picture» from memory, and never a room, person or object you imagine. What they showed "
    "before you may recall from earlier looks. One look per request: do not look again on your own. "
    "If you said something a look did not confirm, it was your mistake — say so, never blame the user."
)
# v5's camera rules, condensed the same way.
_CAMERA_NOTE_V5 = (
    "\nCamera: you cannot see by yourself. Anything about what is visible — «що ти бачиш», «подивись на "
    "мене», «що в мене в руці», «що тут написано», or a short question pointing at something («а це "
    "шо?», «а тепер?», «глянь») — goes to the backend at once, every time, even if you think the camera "
    "is off; never ask what they mean. Each look is a new frame: never a scene from memory or "
    "imagination. Tell what it saw as if you looked («Бачу…»), only what it reported; one look per "
    "request. A description no look confirmed is your mistake, never the user's."
)
# /chat: the user types and hears her. Their messages go to the backend (the page sends them as the user's
# message, like a request said with the wake phrase), and she says its answers.
_CHAT_NOTE = (
    "\nText chat: right now the user can't talk — they type to you in a chat and hear you. Each message "
    "goes to the backend and its answer comes to you: say it in your own words, as speech, briefly (a long "
    "list or a link — the gist). You hear nothing from them; never ask them to say something aloud or "
    "wonder why they are quiet — they read and type."
)
# Delivery prompts the page may pick; v5 is the default. v1 and v2 are desktop-only (a page asking for them gets v5).
WEB_PROMPT_VARIANTS = ("v3", "v4", "v5")
# A camera frame (JPEG from the page, 768 px): far below this; anything bigger is not ours.
MAX_FRAME_BYTES = 600_000
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9-]{16,64}$")
_background_tasks: set[asyncio.Task] = set()

# Free Render sleeps after ~15 min without inbound requests, and a restart forgets everyone's
# conversation. While running, ping our own public URL (through Render's proxy, so it counts as
# inbound traffic). It can't wake a sleeping server — the first visitor does that. 0 = off.
# Note: one always-on free service uses ~744 of the 750 free instance hours a month.
KEEP_AWAKE_S = float(os.getenv("KEEP_AWAKE_MINUTES", "10")) * 60


async def _keep_awake() -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        while True:
            await asyncio.sleep(KEEP_AWAKE_S)
            try:
                await http.get(f"{PUBLIC_BACKEND_URL}/healthz")
            except httpx.HTTPError:
                logger.warning("keep-awake ping failed")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    task = None
    if KEEP_AWAKE_S > 0 and PUBLIC_BACKEND_URL.startswith("https://"):
        task = asyncio.create_task(_keep_awake())
        logger.info("keep-awake every %ss → %s/healthz", int(KEEP_AWAKE_S), PUBLIC_BACKEND_URL)
    if not WEB_SEARCH_API_KEY:
        logger.warning("WEB_SEARCH_API_KEY is not set: web search answers «unavailable»")
    if not knowledge.store.persistent:
        logger.warning("DATABASE_URL is not set: «Знання» lives in a SQLite file that the next deploy wipes")
    yield
    if task is not None:
        task.cancel()
    users.save_all()  # a deploy stops the server: conversations not yet written are written now


app = FastAPI(title="Voice agent web backend", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],  # DELETE: «Нова розмова», background listening off, a knowledge set
    allow_headers=["*"],
)

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
users = WebUserRegistry()
calls = CallLogStore()


def _user(client_id: str | None, access_code: str | None) -> WebUser:
    if ACCESS_CODE and (access_code or "") != ACCESS_CODE:
        raise HTTPException(status_code=401, detail="access_code_required")
    if not client_id or not _CLIENT_ID_RE.match(client_id):
        raise HTTPException(status_code=400, detail="bad_client_id")
    return users.get(client_id)


def _user_voice(user: WebUser) -> str:
    return user.voice or OPENAI_LIVE_VOICE


def _knowledge_sets() -> list[dict]:
    """The enabled knowledge sets for a new call's prompts; a store that is down just leaves them out."""
    try:
        return knowledge.store.enabled_records()[0]
    except knowledge.KnowledgeError:
        return []


def _session_config(
    user: WebUser, knowledge_sets: list[dict] | None = None, *, chat: bool = False, concierge: bool = False
) -> dict:
    """knowledge_sets: what the team uploaded (_knowledge_sets) — she is told about it, so questions about
    those people go to the backend; the lookup tool is there either way."""
    language = LANGUAGE_OPTIONS.get(user.language, LANGUAGE_OPTIONS["uk"])
    today = local_today()
    voice = _user_voice(user)
    config = {
        "model": OPENAI_LIVE_MODEL,
        "instructions": build_live_prompt(
            language_name=language,
            assistant_name=user.assistant_name,
            today=today,
            voice=voice,
            delivery=delivery_instruction(user.speed, user.style),
            variant=user.prompt_variant,
        )
        + _WEB_NOTE
        + (_CAMERA_NOTE_V5 if user.prompt_variant == "v5" else _CAMERA_NOTE)
        + knowledge.live_note(knowledge_sets or [])
        + (_CHAT_NOTE if chat else "")
        + (knowledge.CONCIERGE_NOTE if concierge else ""),
        "audio": {"output": {"voice": voice}},
        "delegation": responses_delegation(
            model=OPENAI_LIVE_BACKEND_MODEL,
            instructions=build_backend_prompt(today=today, language_name=language, camera=True)
            + knowledge.BACKEND_NOTE
            + (knowledge.CONCIERGE_BACKEND_NOTE if concierge else ""),
            tools=[*LIVE_BACKEND_TOOLS, LOOK_AT_CAMERA_TOOL, KNOWLEDGE_LOOKUP_TOOL,
                   *([CONFIRM_GUEST_TOOL] if concierge else [])],
            parallel_tools=OPENAI_LIVE_PARALLEL_TOOLS,
            effort=OPENAI_LIVE_BACKEND_EFFORT,
        ),
    }
    # A new voice or a wake after a long pause is a new Live session: it starts from the
    # conversation so far, so nothing said before is forgotten.
    history = user.conversation.live_input()
    if history:
        config["input"] = history
    return config


class SessionRequest(BaseModel):
    sdp: str
    # The page's picker choices: server state is in memory and is lost on a Render restart.
    voice: str | None = None
    speed: str | None = None
    style: str | None = None
    # What the user asked right after the wake phrase: goes into the new call's history as their
    # own message (a quoted commentary made GPT-Live read it back).
    user_text: str | None = None
    # A voice switch opens the new call beside the current one: the page closes the old call
    # itself once the new one has taken over (the old one goes on talking meanwhile).
    keep_old: bool = False
    # Delivery prompt: v4 (default) or v3.
    prompt: str | None = None
    # Opened by /chat: the user types, she answers aloud (_CHAT_NOTE).
    chat: bool = False
    # The page's top switch: "concierge" — she welcomes people and checks them against «Знання».
    mode: str | None = None


# A kept old call still open this long after its successor opened is closed anyway (page gone).
KEEP_OLD_MAX_S = 120.0


async def _close_if_replaced(old: Any, new: Any, delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    if getattr(new, "is_open", False) and getattr(old, "is_open", False):
        logger.info("web.session.replaced_close session_id=%s", getattr(old, "session_id", "?"))
        await old.close()


class VoiceRequest(BaseModel):
    voice: str | None = None
    speed: str | None = None
    style: str | None = None


@app.get("/healthz")
def healthz() -> dict:
    # Render sets these: shows which commit/branch is actually deployed.
    return {
        "ok": True,
        "commit": os.getenv("RENDER_GIT_COMMIT", "")[:7] or None,
        "branch": os.getenv("RENDER_GIT_BRANCH") or None,
        # Without the Tavily key she says «пошук зараз не працює» — check it here, not by asking her.
        "web_search": bool(WEB_SEARCH_API_KEY),
        # «Знання» and the conversations: postgres (Neon) keeps them; sqlite on Render is wiped by every deploy.
        "knowledge": knowledge.store.kind,
        "conversations": conversations.store.kind,
    }


@app.get("/api/config")
def public_config() -> dict:
    """What the page needs to know before anything else."""
    return {"access_code_required": bool(ACCESS_CODE)}


@app.post("/api/session")
async def create_session(
    body: SessionRequest,
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """Browser SDP offer in → SDP answer out; tools run here over a sideband."""
    user = await asyncio.to_thread(_user, x_client_id, x_access_code)  # may restore the conversation from the database
    if body.voice and body.voice.strip().lower() in VOICE_PERSONAS:
        user.voice = body.voice.strip().lower()
    if body.speed in SPEED_OPTIONS:
        user.speed = body.speed
    if body.style in STYLE_OPTIONS:
        user.style = body.style
    user.prompt_variant = body.prompt if body.prompt in WEB_PROMPT_VARIANTS else "v5"
    user.reconnect_pending = False
    if body.user_text and body.user_text.strip():
        user.conversation.add("user", body.user_text.strip()[:500])
        user.conversation.end_turn()
    try:
        result = await openai_client.live.create(
            session=_session_config(user, await asyncio.to_thread(_knowledge_sets), chat=body.chat,
                                    concierge=body.mode == "concierge"),
            transport={"type": "webrtc", "sdp": body.sdp},
        )
    except Exception as exc:
        logger.exception("web.session.create_failed")
        raise HTTPException(status_code=502, detail=f"live_create_failed: {type(exc).__name__}") from exc
    session_id = result.session.id
    # One call per browser: an older session (another tab, a page reload) would keep billing
    # and run tools in parallel. A voice switch keeps it for a moment.
    old_bridges = list(user.bridges)
    if not body.keep_old:
        for old in old_bridges:
            try:
                await old.close()
            except Exception:
                logger.debug("closing old session failed", exc_info=True)
    call_log = calls.start(
        session_id, user.client_id[:8], voice=_user_voice(user), prompt=user.prompt_variant,
        model=OPENAI_LIVE_MODEL, backend=OPENAI_LIVE_BACKEND_MODEL, effort=OPENAI_LIVE_BACKEND_EFFORT or "default",
        history=len(user.conversation.live_input()), speed=user.speed, style=user.style,
        **({"via": "chat"} if body.chat else {}),
        **({"mode": "concierge"} if body.mode == "concierge" else {}),
    )
    if body.user_text and body.user_text.strip():
        call_log.add("user", text=body.user_text.strip()[:500], via="wake")
    bridge = SidebandToolBridge(
        client=openai_client,
        session_id=session_id,
        executor=user.executor,
        on_closed=lambda b: _call_closed(user, b),
        max_duration_s=MAX_SESSION_S,
        conversation=user.conversation,
        on_voice_request=user.switch_voice_by_request,
        call_log=call_log,
        think_aloud=not body.chat,
    )
    user.bridges.add(bridge)
    task = asyncio.create_task(bridge.run())
    _background_tasks.add(task)  # keep a reference so the task isn't garbage-collected
    task.add_done_callback(_background_tasks.discard)
    if body.keep_old:
        for old in old_bridges:
            closer = asyncio.create_task(_close_if_replaced(old, bridge, KEEP_OLD_MAX_S))
            _background_tasks.add(closer)
            closer.add_done_callback(_background_tasks.discard)
    logger.info("web.session.created session_id=%s keep_old=%s", session_id, body.keep_old)
    return {"sdp": result.transport.sdp, "session_id": session_id}


def _call_closed(user: WebUser, bridge: SidebandToolBridge) -> None:
    """A call ended: off the user's open calls; a cap close noted for the page; its minutes and tokens saved."""
    user.bridges.discard(bridge)
    if bridge.expired:
        user.session_expired_at = time.time()
    if bridge.call_log is not None:
        row = usage.call_row(bridge.call_log)
        task = asyncio.get_running_loop().run_in_executor(None, _record_usage, row)
        _background_tasks.add(task)  # type: ignore[arg-type]
        task.add_done_callback(_background_tasks.discard)  # type: ignore[arg-type]


def _record_usage(row: dict) -> None:
    try:
        usage.store.record(row)
    except StoreError:
        logger.warning("usage.record_failed session=%s", row["session_id"][-8:])


@app.get("/api/me")
def me(
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    user = _user(x_client_id, x_access_code)
    return {
        "assistant_name": user.assistant_name,
        "voice": user.voice,  # None until chosen; the page keeps its own choice then
        "speed": user.speed,
        "style": user.style,
        "reconnect": user.reconnect_pending,  # voice changed by voice command: page reconnects
        "voice_note": user.voice_switch_note,  # why it changed (diagnostics on the page)
        "keep_session": user.must_keep_session(),  # paused: don't close the call yet
        "history_turns": len(user.conversation),
        "guest": user.guest,  # «Консьєрж»: who the guest was confirmed as
        # The server's cap closed the call a moment ago and no other is open: carry on in a new session.
        "expired": not user.bridges and time.time() - user.session_expired_at < EXPIRED_FRESH_S,
    }


@app.get("/api/voices")
def voices() -> dict:
    """Voices for the page's picker; the chosen one applies from the next call."""
    return {
        "default": OPENAI_LIVE_VOICE,
        "voices": [
            {"id": p.voice, "label": p.label, "description": p.description, "feminine": p.feminine}
            for p in VOICE_PERSONAS.values()
        ],
    }


@app.post("/api/voice")
def set_voice(
    body: VoiceRequest,
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """Voice (applies via reconnect) and speed/style. Both are applied by the page: it reconnects
    for a voice, and sends the returned speed/style instruction once Єва is quiet — the page hears
    her audio, while an instruction that lands mid-answer derails it."""
    user = _user(x_client_id, x_access_code)
    if body.voice is not None:
        voice = body.voice.strip().lower()
        if voice not in VOICE_PERSONAS:
            raise HTTPException(status_code=400, detail="unknown_voice")
        user.voice = voice
    delivery_changed = False
    for name, options in (("speed", SPEED_OPTIONS), ("style", STYLE_OPTIONS)):
        value = getattr(body, name)
        if value is None:
            continue
        if value not in options:
            raise HTTPException(status_code=400, detail=f"unknown_{name}")
        delivery_changed |= value != getattr(user, name)
        setattr(user, name, value)
    instruction = delivery_instruction(user.speed, user.style, changed=True) if delivery_changed else ""
    return {"voice": user.voice, "speed": user.speed, "style": user.style, "instruction": instruction}


class VoiceRequestBody(BaseModel):
    text: str


@app.post("/api/voice-request")
def voice_request(
    body: VoiceRequestBody,
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """«Єва, зміни голос на …» heard by the browser's recogniser — faster than the Live transcript.
    The page restarts the call in the new voice when switched is true."""
    user = _user(x_client_id, x_access_code)
    switched = user.switch_voice_by_request(body.text[:500])
    if switched:
        user.reconnect_pending = False  # the page reconnects itself
    return {"switched": switched, "voice": user.voice}


class OverheardBody(BaseModel):
    text: str


@app.post("/api/overheard")
def overheard(
    body: OverheardBody,
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """A phrase the page's recogniser heard while Єва was paused (not addressed to her)."""
    _user(x_client_id, x_access_code).background.add(body.text[:1000])
    return {"ok": True}


# Numbers only from the page (mic gate levels, barge-in timings, unanswered turns — no words): how the
# call sounds on the testers' own devices, which the live tests can't show (her echo on laptop speakers,
# a quiet voice over her). The last few hundred, in memory; GET /api/diag reads them.
_DIAG_MAX_BYTES = 4000
_diag: deque[dict] = deque(maxlen=200)


def _numbers_only(value: Any, depth: int = 0) -> Any:
    """Keeps numbers, booleans and short keyword strings (states like «muted»); drops any other text."""
    if depth > 3:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 24 and re.fullmatch(r"[\w.:-]*", value) else None
    if isinstance(value, list):
        return [_numbers_only(v, depth + 1) for v in value[:30]]
    if isinstance(value, dict):
        return {str(k)[:24]: _numbers_only(v, depth + 1) for k, v in list(value.items())[:40]}
    return None


@app.post("/api/diag")
async def diag_post(
    request: Request,
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    user = _user(x_client_id, x_access_code)
    raw = await request.body()
    if len(raw) > _DIAG_MAX_BYTES:
        raise HTTPException(status_code=413, detail="too_big")
    try:
        data = _numbers_only(json.loads(raw))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bad_json") from exc
    _diag.append({"at": round(time.time()), "client": user.client_id[:6], "commit": os.getenv("RENDER_GIT_COMMIT", "")[:7], "data": data})
    return {"ok": True}


@app.get("/api/diag")
def diag_get(n: int = 50) -> list[dict]:
    return list(_diag)[-max(1, min(n, 200)):]


def _logs_auth(token: str | None) -> None:
    if not LOGS_TOKEN:
        raise HTTPException(status_code=503, detail="logs_disabled: set LOGS_TOKEN on the server")
    if not hmac.compare_digest((token or "").encode(), LOGS_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="bad_logs_token")


@app.get("/api/logs")
def logs_list(x_logs_token: str | None = Header(default=None), n: int = 100) -> list[dict]:
    """The latest calls, newest first (web/logs.html). In memory: a deploy clears them."""
    _logs_auth(x_logs_token)
    return calls.latest(max(1, min(n, 200)))


@app.get("/api/usage")
def usage_summary(x_logs_token: str | None = Header(default=None), days: int = 14) -> dict:
    """/logs → «Витрати»: minutes, tokens and the estimated $ per day and per browser (server/usage.py)."""
    _logs_auth(x_logs_token)
    try:
        return usage.store.summary(max(1, min(days, 90)))
    except StoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/logs/{session_id}")
def logs_call(session_id: str, x_logs_token: str | None = Header(default=None)) -> dict:
    """One call: every line, note, backend step and tool call with its arguments, result and time."""
    _logs_auth(x_logs_token)
    log = calls.get(session_id)
    if log is None:
        raise HTTPException(status_code=404, detail="unknown_call")
    return log.to_dict()


@app.delete("/api/overheard")
def overheard_clear(
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """Background listening turned off on the page: forget what was heard so far."""
    _user(x_client_id, x_access_code).background.clear()
    return {"ok": True}


@app.post("/api/overheard/digest")
def overheard_digest(
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """On wake: the gist of the pause as a commentary for the page to send; kept in the history."""
    user = _user(x_client_id, x_access_code)
    note = user.background.digest()
    if not note:
        return {"commentary": ""}
    user.conversation.add_note(note)
    logger.info("web.background.digest chars=%s", len(note))
    return {"commentary": wake_commentary(note)}


@app.post("/api/frame")
async def camera_frame(
    request: Request,
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """The page's latest camera frame (raw JPEG body), kept in memory for look_at_camera."""
    user = await asyncio.to_thread(_user, x_client_id, x_access_code)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_FRAME_BYTES:
        raise HTTPException(status_code=413, detail="frame_too_large")  # before reading it in
    body = await request.body()
    if len(body) > MAX_FRAME_BYTES:
        raise HTTPException(status_code=413, detail="frame_too_large")
    if not body.startswith(b"\xff\xd8\xff"):
        raise HTTPException(status_code=415, detail="jpeg_expected")
    user.set_camera_frame(body)
    return {"ok": True}


@app.delete("/api/frame")
def camera_off(
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """Camera turned off on the page: forget the last frame."""
    _user(x_client_id, x_access_code).camera_off()
    return {"ok": True}


@app.delete("/api/conversation")
def clear_conversation(
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """«Нова розмова»: Єва starts from a clean page; the old conversation stays saved («Історія»)."""
    user = _user(x_client_id, x_access_code)
    user.new_conversation()
    user.background.clear()
    return {"ok": True}


# ── the conversations: the current one (shown again after a refresh) and «Історія» (server/conversations.py) ──
def _history(call, *args):
    try:
        return call(*args)
    except StoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/conversation")
def current_conversation(
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """What the page shows on load: the conversation Єва remembers, as it was said."""
    user = _user(x_client_id, x_access_code)
    return {"id": user.conversation_id, "started_at": user.conversation_started_at,
            "turns": conversations.visible(user.conversation.to_list())}


class TypedLine(BaseModel):
    text: str


@app.post("/api/conversation/text")
def typed_line(
    body: TypedLine,
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """A message typed on /chat: into the conversation (history, «Історія», her memory) and the call's log.
    The page itself hands it to the backend through the call."""
    user = _user(x_client_id, x_access_code)
    text = " ".join(body.text.split())[:2000]
    if not text:
        raise HTTPException(status_code=422, detail="empty_text")
    user.conversation.end_turn()
    user.conversation.add("user", text)
    user.conversation.end_turn()
    for bridge in list(user.bridges):
        bridge.user_typed(text)
    return {"ok": True}


@app.get("/api/conversations")
def conversation_list(
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """This browser's saved conversations, newest first (the current one is marked)."""
    user = _user(x_client_id, x_access_code)
    user.save_conversation()  # the current one as it is now
    return {"current": user.conversation_id, "persistent": conversations.store.persistent,
            "conversations": _history(conversations.store.list, user.client_id)}


@app.get("/api/conversations/{conv_id}")
def conversation_get(
    conv_id: str,
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    user = _user(x_client_id, x_access_code)
    if conv_id == user.conversation_id:
        user.save_conversation()
    found = _history(conversations.store.get, user.client_id, conv_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Такої розмови немає.")
    found["turns"] = conversations.visible(found["turns"])
    return found


@app.post("/api/conversations/{conv_id}/resume")
def conversation_resume(
    conv_id: str,
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """«Продовжити»: a saved conversation becomes the current one — Єва remembers it from the next call."""
    user = _user(x_client_id, x_access_code)
    found = _history(conversations.store.get, user.client_id, conv_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Такої розмови немає.")
    user.resume_conversation(found)
    user.background.clear()
    return {"ok": True, "id": conv_id}


@app.delete("/api/conversations/{conv_id}")
def conversation_delete(
    conv_id: str,
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """Delete a saved conversation; the current one is also forgotten by Єва (a new one starts)."""
    user = _user(x_client_id, x_access_code)
    if conv_id == user.conversation_id:
        user.new_conversation()
    if not _history(conversations.store.delete, user.client_id, conv_id):
        raise HTTPException(status_code=404, detail="Такої розмови немає.")
    return {"ok": True}


# ── «Знання»: the team's knowledge sets (server/knowledge.py, web/knowledge.html) ──
# One store for everyone with the access code. Upload is two steps: /parse reads the file and returns the
# records (nothing saved), the page shows them, and POST /api/knowledge saves what the user kept.
_extract = None


def _team(access_code: str | None) -> None:
    if ACCESS_CODE and not hmac.compare_digest((access_code or "").encode(), ACCESS_CODE.encode()):
        raise HTTPException(status_code=401, detail="access_code_required")


def _kb(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except knowledge.KnowledgeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _text_extract():
    global _extract
    if _extract is None:
        from openai import OpenAI

        _extract = make_openai_extract(OpenAI(api_key=OPENAI_API_KEY), OPENAI_LIVE_BACKEND_MODEL)
    return _extract


class KnowledgeItem(BaseModel):
    title: str = ""
    fields: dict[str, Any] = {}


class KnowledgeSetIn(BaseModel):
    name: str
    note: str = ""
    source: str = ""
    items: list[KnowledgeItem]


class KnowledgeSetPatch(BaseModel):
    enabled: bool | None = None
    name: str | None = None
    note: str | None = None


@app.post("/api/knowledge/parse")
async def knowledge_parse(request: Request, x_access_code: str | None = Header(default=None)) -> dict:
    """The file as the raw body, its name in X-File-Name (URL-encoded) → the records it holds."""
    _team(x_access_code)
    filename = urllib.parse.unquote(request.headers.get("x-file-name", ""))[:200]
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > 5_000_000:
        raise HTTPException(status_code=413, detail="Файл завеликий: до 5 МБ.")
    data = await request.body()
    try:
        parsed = await asyncio.to_thread(parse_file, filename, data, lambda text: _text_extract()(text))
    except KnowledgeImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info("knowledge.parsed ext=%s items=%s kind=%s", filename.rsplit(".", 1)[-1], len(parsed.items), parsed.kind)
    return parsed.to_dict()


@app.get("/api/knowledge")
def knowledge_sets(x_access_code: str | None = Header(default=None)) -> dict:
    _team(x_access_code)
    return {"storage": knowledge.store.kind, "persistent": knowledge.store.persistent,
            "sets": _kb(knowledge.store.list_sets)}


@app.post("/api/knowledge")
def knowledge_create(body: KnowledgeSetIn, x_access_code: str | None = Header(default=None)) -> dict:
    _team(x_access_code)
    items = [item.model_dump() for item in body.items]
    return _kb(knowledge.store.create_set, body.name, items, note=body.note, source=body.source)


@app.get("/api/knowledge/{set_id}")
def knowledge_get(set_id: int, x_access_code: str | None = Header(default=None)) -> dict:
    _team(x_access_code)
    found = _kb(knowledge.store.get_set, set_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Такого набору немає.")
    return found


@app.post("/api/knowledge/{set_id}")
def knowledge_update(set_id: int, body: KnowledgeSetPatch, x_access_code: str | None = Header(default=None)) -> dict:
    """Turn a set on or off for Єва («Єва знає»), rename it, change its note."""
    _team(x_access_code)
    found = _kb(knowledge.store.update_set, set_id, enabled=body.enabled, name=body.name, note=body.note)
    if found is None:
        raise HTTPException(status_code=404, detail="Такого набору немає.")
    return found


@app.delete("/api/knowledge/{set_id}")
def knowledge_delete(set_id: int, x_access_code: str | None = Header(default=None)) -> dict:
    _team(x_access_code)
    if not _kb(knowledge.store.delete_set, set_id):
        raise HTTPException(status_code=404, detail="Такого набору немає.")
    return {"ok": True}
