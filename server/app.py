"""FastAPI: the web page, a config endpoint and one WebSocket per call."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .llm import OpenAILLM
from .session import VoiceSession
from .tts import TTSError, make_tts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

cfg = Config()
app = FastAPI(title="diff-voice-agent")
if cfg.frontend_origins:
    app.add_middleware(CORSMiddleware, allow_origins=cfg.frontend_origins, allow_methods=["*"], allow_headers=["*"])

WEB = Path(__file__).resolve().parent.parent / "web"


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/api/config")
async def api_config() -> dict:
    missing = [] if cfg.openai_api_key else ["OPENAI_API_KEY"]
    return {
        "providers": cfg.tts_providers(),
        "needs_code": bool(cfg.access_code),
        "missing": missing + ([] if cfg.tts_providers() else ["RESPEECHER_API_KEY or SONIOX_API_KEY"]),
    }


@app.websocket("/ws")
async def ws_call(ws: WebSocket) -> None:
    if cfg.access_code and ws.query_params.get("code") != cfg.access_code:
        await ws.close(code=4401)
        return
    await ws.accept()
    session: VoiceSession | None = None
    try:
        # The first message picks the voice: {"type": "start", "provider": "...", "voice": "..."}
        first = json.loads(await ws.receive_text())
        try:
            tts = make_tts(first.get("provider", ""), first.get("voice", ""), cfg)
        except TTSError as e:
            await ws.send_json({"type": "error", "message": str(e)})
            return
        session = VoiceSession(
            llm=OpenAILLM(cfg),
            tts=tts,
            send_json=ws.send_json,
            send_bytes=ws.send_bytes,
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
            if msg.get("text"):
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
