"""FastAPI: the web page, a config endpoint and one WebSocket per call."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .about_store import AboutKnowledge, AboutStore
from .characters import CHARACTERS, DEFAULT_EMOTION, EMOTIONS, characters_payload, normalize_character_id
from .config import Config
from .db import StoreError
from .llm import OpenAILLM
from .persona import ABOUT_MAX_CHARS, SUPPORTED, Persona, sanitize_about
from .preview import PreviewError, VoicePreviews
from .realtime import RealtimeLLM
from .session import VoiceSession
from .tts import TTSError, make_tts
from .voices import list_voices, tts_providers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

cfg = Config()
about_store = AboutStore(cfg.database_url, cfg.sqlite_path)
about = AboutKnowledge(about_store)  # what every call knows about the agent and the place

# Free Render sleeps after ~15 min without inbound requests. While running, ping our own public URL (through
# Render's proxy, so it counts as inbound traffic). It cannot wake a sleeping server: the first visitor does
# that (the page waits for it). 0 = off. Render sets RENDER_EXTERNAL_URL itself.
KEEP_AWAKE_S = float(os.getenv("KEEP_AWAKE_MINUTES", "10")) * 60
SELF_URL = (os.getenv("PUBLIC_BACKEND_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")


async def keep_awake(url: str, interval_s: float, client: httpx.AsyncClient | None = None) -> None:
    async with client or httpx.AsyncClient(timeout=30) as http:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await http.get(f"{url}/healthz")
            except httpx.HTTPError:
                log.warning("keep-awake ping failed")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    task = None
    if KEEP_AWAKE_S > 0 and SELF_URL.startswith("https://"):
        task = asyncio.create_task(keep_awake(SELF_URL, KEEP_AWAKE_S))
        log.info("keep-awake every %ss -> %s/healthz", int(KEEP_AWAKE_S), SELF_URL)
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="diff-voice-agent", lifespan=lifespan)
if cfg.frontend_origins:
    app.add_middleware(CORSMiddleware, allow_origins=cfg.frontend_origins, allow_methods=["*"], allow_headers=["*"])

WEB = Path(__file__).resolve().parent.parent / "web"


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "knowledge": about_store.kind, "persistent": about_store.persistent}


@app.get("/api/config")
async def api_config() -> dict:
    full = await tts_providers(cfg)
    providers = [  # what the page needs of a voice: its id and its name (the sampling defaults stay on the server)
        {**p, "voices": [{"id": v["id"], "label": v["label"]} for v in p["voices"]]} for p in full
    ]
    missing = [] if cfg.openai_api_key else ["OPENAI_API_KEY"]
    return {
        "providers": providers,
        "characters": characters_payload(providers),
        "missing": missing + ([] if providers else ["RESPEECHER_API_KEY or SONIOX_API_KEY"]),
    }


previews = VoicePreviews()


async def _require_password(given: str | None) -> None:
    """The knowledge is behind one shared password. A wrong one waits a moment, so guessing is slow."""
    if not cfg.access_password:
        raise HTTPException(status_code=403, detail="Зміна знань вимкнена: на сервері не задано ACCESS_PASSWORD.")
    if not hmac.compare_digest((given or "").encode(), cfg.access_password.encode()):
        log.warning("knowledge: wrong password")
        await asyncio.sleep(0.7)
        raise HTTPException(status_code=401, detail="Невірний пароль.")


def _knowledge_payload(text: str) -> dict:
    return {"text": text, "max": ABOUT_MAX_CHARS, "storage": about_store.kind, "persistent": about_store.persistent}


@app.get("/api/knowledge")
async def get_knowledge(x_access_password: str | None = Header(default=None)) -> dict:
    await _require_password(x_access_password)
    try:
        text = await asyncio.to_thread(about_store.get)
    except StoreError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return _knowledge_payload(text)


@app.put("/api/knowledge")
async def put_knowledge(body: dict, x_access_password: str | None = Header(default=None)) -> dict:
    await _require_password(x_access_password)
    text = sanitize_about(body.get("text"))
    try:
        await about.save(text)
    except StoreError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return _knowledge_payload(text)


@app.get("/api/preview")
async def api_preview(provider: str, voice: str, language: str = "uk") -> Response:
    """A sample of a voice for the page's «Прослухати» (WAV; the browser keeps it)."""
    try:
        wav = await previews.get(cfg, provider, voice, language, make_tts=make_tts, list_voices=list_voices)
    except PreviewError as e:
        raise HTTPException(status_code=e.status, detail=e.message) from e
    return Response(wav, media_type="audio/wav", headers={"Cache-Control": "public, max-age=86400"})


@app.websocket("/ws")
async def ws_call(ws: WebSocket) -> None:
    await ws.accept()
    session: VoiceSession | None = None
    try:
        # The first message picks the voice: {"type": "start", "provider": "...", "voice": "...", "language": "uk"}
        first = json.loads(await ws.receive_text())
        provider = str(first.get("provider", ""))
        language = str(first.get("language") or "uk")
        character = normalize_character_id(first.get("character"))
        emotion = first.get("emotion") if first.get("emotion") in EMOTIONS else DEFAULT_EMOTION
        if CHARACTERS[character].ukrainian_only:
            language = "uk"  # its jokes and curses are Ukrainian
        if language not in SUPPORTED.get(provider, set()):
            language = "uk"  # a language this provider cannot speak: the default one
        try:
            tts = make_tts(provider, str(first.get("voice", "")), cfg, language)
        except TTSError as e:
            await ws.send_json({"type": "error", "message": str(e)})
            return
        realtime = first.get("engine") == "realtime"
        session = VoiceSession(
            llm=OpenAILLM(cfg),
            ears=RealtimeLLM(cfg) if realtime else None,
            tts=tts,
            send_json=ws.send_json,
            send_bytes=ws.send_bytes,
            cfg=cfg,
            provider=provider,
            voice=getattr(tts, "voice", ""),
            persona=Persona(
                name=CHARACTERS[character].label if CHARACTERS[character].character else Persona().name,
                language=language,
                character=character,
                emotion=emotion,
            ),
            tts_factory=lambda p, v, lang: make_tts(p, v, cfg, lang),
            voices_for=lambda p, lang: list_voices(cfg, p, lang),
            history=first.get("history"),
            tune=first.get("tune") is not False,
            greet=first.get("greet") is not False,
            about_source=about,
        )
        try:
            await session.start()
        except Exception as e:
            log.exception("session start failed")
            await ws.send_json({"type": "error", "message": f"Не вдалося підключитись: {type(e).__name__}"})
            return
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                await session.on_audio(msg["bytes"])
            elif msg.get("text"):
                await session.on_client(json.loads(msg["text"]))
    except WebSocketDisconnect:
        pass
    finally:
        if session:
            await session.close()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=WEB), name="web")
