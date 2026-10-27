"""The voices each TTS provider offers, fetched from the provider's API (cached), with env fallbacks."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import Config

log = logging.getLogger(__name__)

_TTL_S = 3600.0
_cache: dict[str, tuple[float, list[dict]]] = {}


def _label(name: str, gender: str = "", extra: str = "") -> str:
    bits = [b for b in (gender, extra) if b]
    return f"{name} ({', '.join(bits)})" if bits else name


async def _respeecher(cfg: Config, client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(
        f"https://api.respeecher.com/v1/{cfg.respeecher_model.strip('/')}/voices",
        headers={"X-API-Key": cfg.respeecher_api_key},
    )
    r.raise_for_status()
    return [
        {"id": v["id"], "label": _label(v.get("full_name") or v["id"], v.get("gender", ""), v.get("accent", ""))}
        for v in r.json()
        if isinstance(v, dict) and v.get("id")
    ]


async def _soniox(cfg: Config, client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(
        "https://api.soniox.com/v1/tts-models", headers={"Authorization": f"Bearer {cfg.soniox_api_key}"}
    )
    r.raise_for_status()
    models = r.json().get("models") or []
    model = next((m for m in models if m.get("id") == cfg.soniox_tts_model), models[0] if models else {})
    return [
        {"id": v["id"], "label": _label(v["id"], v.get("gender", ""))}
        for v in model.get("voices") or []
        if isinstance(v, dict) and v.get("id")
    ]


async def _load(provider: str, cfg: Config, fallback: list[str]) -> list[dict]:
    cached = _cache.get(provider)
    if cached and time.monotonic() - cached[0] < _TTL_S:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            voices = await (_respeecher if provider == "respeecher" else _soniox)(cfg, client)
    except Exception as e:  # network, auth, or a changed response: the page must still open
        log.warning("%s voices list failed (%s), using the configured ones", provider, type(e).__name__)
        voices = []
    if voices:
        _cache[provider] = (time.monotonic(), voices)
        return voices
    return [{"id": v, "label": v} for v in fallback]


async def tts_providers(cfg: Config) -> list[dict]:
    """Providers that have a key, with their voices — what the page offers in its selectors."""
    jobs, meta = [], []
    if cfg.respeecher_api_key:
        jobs.append(_load("respeecher", cfg, cfg.respeecher_voices))
        meta.append(("respeecher", "Respeecher"))
    if cfg.soniox_api_key:
        jobs.append(_load("soniox", cfg, cfg.soniox_voices))
        meta.append(("soniox", "Soniox"))
    voices = await asyncio.gather(*jobs)
    return [{"id": i, "label": label, "voices": v} for (i, label), v in zip(meta, voices, strict=True)]
