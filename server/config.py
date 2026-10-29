"""Settings from the environment (.env is loaded once, on import)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _list(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))

    # The Realtime "ears": it hears the microphone and answers with text (the voice is Respeecher / Soniox).
    realtime_model: str = field(default_factory=lambda: os.getenv("REALTIME_MODEL", "gpt-realtime-2.1"))
    realtime_transcribe_model: str = field(default_factory=lambda: os.getenv("REALTIME_TRANSCRIBE_MODEL", "gpt-4o-transcribe"))
    realtime_silence_ms: int = field(default_factory=lambda: _int("REALTIME_SILENCE_MS", 700))  # quiet that ends a phrase
    # How the end of a phrase is found: "server" (quiet for REALTIME_SILENCE_MS) or "semantic" (the model judges whether the
    # person is done; REALTIME_EAGERNESS low|medium|high|auto says how soon it may decide: high = soonest).
    realtime_turn: str = field(default_factory=lambda: os.getenv("REALTIME_TURN", "server").strip().lower())
    realtime_eagerness: str = field(default_factory=lambda: os.getenv("REALTIME_EAGERNESS", "high").strip().lower())
    # The input noise reduction of Realtime, before its VAD and the transcriber: "far_field" (a laptop or a stand's
    # microphone), "near_field" (a headset) or "off".
    realtime_noise_reduction: str = field(default_factory=lambda: os.getenv("REALTIME_NOISE_REDUCTION", "far_field").strip().lower())
    realtime_vad_threshold: float = field(default_factory=lambda: _float("REALTIME_VAD_THRESHOLD", 0.5))

    # One TTS generation per answer (text is added to it as the model writes) instead of one per sentence. "0" goes back.
    tts_one_generation: bool = field(default_factory=lambda: os.getenv("TTS_ONE_GENERATION", "1") != "0")

    # How long the voice takes per character of text, in seconds: the first guess for where a cut-off answer was
    # (session.py). With one generation per answer the provider does not say where a sentence ends; the value is then
    # refined by a moving average over the answers that were spoken to the end.
    tts_seconds_per_char: float = field(default_factory=lambda: _float("TTS_SECONDS_PER_CHAR", 0.065))

    # GPT-Live (REALTIME_MODEL=gpt-live-*, server/live.py): a primary WebSocket of its own, not v1/realtime.
    live_url: str = field(default_factory=lambda: os.getenv("LIVE_URL", "wss://api.openai.com/v1/live/sessions"))
    # Its transcript of what it says is split into utterances for the voice: a pause on the session timeline longer than
    # this starts a new one, and so does a silence of this length in the arrival of the text (the rest is spoken at once).
    live_gap_ms: int = field(default_factory=lambda: _int("LIVE_GAP_MS", 600))
    live_idle_flush_ms: int = field(default_factory=lambda: _int("LIVE_IDLE_FLUSH_MS", 300))
    # The user's words over her voice count as an interruption from this many characters (or from two words).
    live_barge_min_chars: int = field(default_factory=lambda: _int("LIVE_BARGE_MIN_CHARS", 8))
    price_live_per_min: float = field(default_factory=lambda: _float("PRICE_LIVE_PER_MIN", 0.05))

    soniox_api_key: str = field(default_factory=lambda: os.getenv("SONIOX_API_KEY", ""))

    respeecher_api_key: str = field(default_factory=lambda: os.getenv("RESPEECHER_API_KEY", ""))
    respeecher_model: str = field(default_factory=lambda: os.getenv("RESPEECHER_MODEL", "public/tts/ua-rt"))
    respeecher_voices: list[str] = field(default_factory=lambda: _list("RESPEECHER_VOICES", "olesia-conversation"))
    respeecher_sample_rate: int = field(default_factory=lambda: _int("RESPEECHER_SAMPLE_RATE", 24000))

    soniox_tts_model: str = field(default_factory=lambda: os.getenv("SONIOX_TTS_MODEL", "tts-rt-v2"))
    soniox_tts_language: str = field(default_factory=lambda: os.getenv("SONIOX_TTS_LANGUAGE", "uk"))
    soniox_voices: list[str] = field(default_factory=lambda: _list("SONIOX_VOICES", "Taras"))
    soniox_tts_sample_rate: int = field(default_factory=lambda: _int("SONIOX_TTS_SAMPLE_RATE", 24000))

    web_search_api_key: str = field(default_factory=lambda: os.getenv("WEB_SEARCH_API_KEY", ""))
    web_search_timeout_s: float = field(default_factory=lambda: float(_int("WEB_SEARCH_TIMEOUT_S", 9)))

    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Europe/Kyiv"))

    # For the cost estimate, USD. The LLM prices are per 1M tokens and match gpt-4.1-mini: change them with
    # OPENAI_MODEL. Respeecher is $2 per 60k characters (a third-party figure); Soniox's TTS price is unknown, so 0.
    price_llm_in_per_m: float = field(default_factory=lambda: _float("PRICE_LLM_IN_PER_M", 0.40))
    price_llm_out_per_m: float = field(default_factory=lambda: _float("PRICE_LLM_OUT_PER_M", 1.60))
    # The same for the Realtime engine (an estimate: check the current price list of REALTIME_MODEL). Audio input is
    # billed for every second sent, silence included; the phrase transcription (REALTIME_TRANSCRIBE_MODEL) is not counted.
    price_rt_text_in_per_m: float = field(default_factory=lambda: _float("PRICE_RT_TEXT_IN_PER_M", 4.0))
    price_rt_text_out_per_m: float = field(default_factory=lambda: _float("PRICE_RT_TEXT_OUT_PER_M", 16.0))
    price_rt_audio_in_per_m: float = field(default_factory=lambda: _float("PRICE_RT_AUDIO_IN_PER_M", 32.0))
    price_respeecher_per_1k_chars: float = field(default_factory=lambda: _float("PRICE_RESPEECHER_PER_1K_CHARS", 0.033))
    price_soniox_per_1k_chars: float = field(default_factory=lambda: _float("PRICE_SONIOX_PER_1K_CHARS", 0.0))
    price_search: float = field(default_factory=lambda: _float("PRICE_SEARCH", 0.008))

    # The database (Postgres on Neon, like Eva's; empty = a SQLite file that a deploy on Render wipes).
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    sqlite_path: str = field(default_factory=lambda: os.getenv("AGENT_SQLITE", "data/agent.db"))

    frontend_origins: list[str] = field(default_factory=lambda: _list("FRONTEND_ORIGINS", ""))
