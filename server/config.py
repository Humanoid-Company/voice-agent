"""Settings from the environment (.env is loaded once, on import)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _list(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))

    soniox_api_key: str = field(default_factory=lambda: os.getenv("SONIOX_API_KEY", ""))

    respeecher_api_key: str = field(default_factory=lambda: os.getenv("RESPEECHER_API_KEY", ""))
    respeecher_model: str = field(default_factory=lambda: os.getenv("RESPEECHER_MODEL", "public/tts/ua-rt"))
    respeecher_voices: list[str] = field(default_factory=lambda: _list("RESPEECHER_VOICES", "olesia-conversation"))
    respeecher_sample_rate: int = field(default_factory=lambda: _int("RESPEECHER_SAMPLE_RATE", 24000))

    soniox_tts_model: str = field(default_factory=lambda: os.getenv("SONIOX_TTS_MODEL", "tts-rt-v2"))
    soniox_tts_language: str = field(default_factory=lambda: os.getenv("SONIOX_TTS_LANGUAGE", "uk"))
    soniox_voices: list[str] = field(default_factory=lambda: _list("SONIOX_VOICES", "Adrian"))
    soniox_tts_sample_rate: int = field(default_factory=lambda: _int("SONIOX_TTS_SAMPLE_RATE", 24000))

    web_search_api_key: str = field(default_factory=lambda: os.getenv("WEB_SEARCH_API_KEY", ""))
    web_search_timeout_s: float = field(default_factory=lambda: float(_int("WEB_SEARCH_TIMEOUT_S", 9)))

    frontend_origins: list[str] = field(default_factory=lambda: _list("FRONTEND_ORIGINS", ""))
