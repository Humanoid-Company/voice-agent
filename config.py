"""
Central configuration for the Ukrainian voice assistant.
All tunable constants live here — import from this module, never hard-code.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_ROOT = Path(__file__).resolve().parent

# ── OpenAI ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

if not OPENAI_API_KEY:
    raise OSError(
        "OPENAI_API_KEY is not set.\n"
        "Create a .env file in the project directory with:\n"
        "OPENAI_API_KEY=sk-..."
    )

# ── Process lock ────────────────────────────────────────────────────────────
PID_FILE: Path = _ROOT / "assistant.pid"

# ── Language ──────────────────────────────────────────────────────────────────
LANGUAGE_BCP47: str = "uk-UA"

# ── OpenAI GPT-Live API ───────────────────────────────────────────────────────
OPENAI_LIVE_MODEL: str = os.getenv("OPENAI_LIVE_MODEL", "gpt-live-1")
# Responses delegation backend — configurable; default from current OpenAI Live docs.
OPENAI_LIVE_BACKEND_MODEL: str = os.getenv("OPENAI_LIVE_BACKEND_MODEL", "gpt-6-luna")
# The backend may call several tools at once.
OPENAI_LIVE_PARALLEL_TOOLS: bool = os.getenv("OPENAI_LIVE_PARALLEL_TOOLS", "true").lower() in ("1", "true", "yes")
# Reasoning effort of the backend model (minimal|low|medium|high). Empty = model default.
# Lower = shorter pause before tool calls; check quality on real requests before keeping it.
OPENAI_LIVE_BACKEND_EFFORT: str = os.getenv("OPENAI_LIVE_BACKEND_EFFORT", "").strip().lower()
# Gleam: the team's pick by ear on Ukrainian samples.
OPENAI_LIVE_VOICE: str = os.getenv("OPENAI_LIVE_VOICE", "gleam")
OPENAI_LIVE_AUDIO_RATE: int = int(os.getenv("OPENAI_LIVE_AUDIO_RATE", "24000"))

# ── Live voice UX: local barge-in + busy cues ─────────────────────────────────
# Local WebRTC VAD stops assistant playback before transcripts arrive.
VOICE_LOCAL_BARGE_IN: bool = os.getenv("VOICE_LOCAL_BARGE_IN", "true").lower() in (
    "1",
    "true",
    "yes",
)
# Default 3 frames (~90 ms) — less click/cough false positives; still sub-300 ms confirm.
VOICE_BARGE_IN_ONSET_FRAMES: int = int(os.getenv("VOICE_BARGE_IN_ONSET_FRAMES", "3"))
VOICE_BARGE_IN_COOLDOWN_MS: int = int(os.getenv("VOICE_BARGE_IN_COOLDOWN_MS", "500"))
# After confirmed barge-in, drop server output audio until local silence (ms).
VOICE_BARGE_IN_SUPPRESS_MS: int = int(os.getenv("VOICE_BARGE_IN_SUPPRESS_MS", "400"))
# Two-stage gate: duck first, then confirm only on real intent to interrupt — a stop word
# or the user taking the turn (partial transcript), or sustained speech when ASR lags.
# Backchannels («угу», «ага»), coughs, room chatter and echo restore the volume instead.
# Window a ducked candidate may stay open waiting for that evidence.
VOICE_BARGE_IN_CONFIRM_MS: int = int(os.getenv("VOICE_BARGE_IN_CONFIRM_MS", "1200"))
# Audio-only confirm (no transcript yet): this much continuous speech. Longer than any
# «угу»/«ага»/laugh, shorter than a real sentence.
VOICE_BARGE_IN_MIN_SPEECH_MS: int = int(os.getenv("VOICE_BARGE_IN_MIN_SPEECH_MS", "600"))
# Silence that ends a short candidate; long enough to span pauses between words.
VOICE_BARGE_IN_REJECT_SILENCE_MS: int = int(os.getenv("VOICE_BARGE_IN_REJECT_SILENCE_MS", "300"))
# While the assistant talks, a candidate must be this many times louder than the mic level
# of its own voice from the speakers (no hardware echo cancellation on laptops).
VOICE_BARGE_IN_ECHO_MARGIN: float = float(os.getenv("VOICE_BARGE_IN_ECHO_MARGIN", "2.5"))
# Duck only gently: a false candidate should be barely noticeable.
VOICE_BARGE_IN_DUCK_VOLUME: float = float(os.getenv("VOICE_BARGE_IN_DUCK_VOLUME", "0.5"))
VOICE_BARGE_IN_USE_ENERGY_GATE: bool = os.getenv(
    "VOICE_BARGE_IN_USE_ENERGY_GATE", "true"
).lower() in ("1", "true", "yes")
VOICE_BARGE_IN_ENERGY_MARGIN: float = float(os.getenv("VOICE_BARGE_IN_ENERGY_MARGIN", "2.2"))
# Stricter while assistant speaks (crude echo / room-bleed guard; no hardware AEC).
VOICE_BARGE_IN_ENERGY_MARGIN_PLAYING: float = float(
    os.getenv("VOICE_BARGE_IN_ENERGY_MARGIN_PLAYING", "3.0")
)

# Pause = nobody talks to her, she is not off: keep what is said nearby and hand it to her on wake
# (voice/background.py). false = she remembers nothing from her pauses.
BACKGROUND_LISTENING: bool = os.getenv("BACKGROUND_LISTENING", "true").lower() in ("1", "true", "yes")

# Same timings as the web page (web/config.js). GPT-Live bills every second a session is open, so
# this long with nobody speaking pauses her (0 = never)…
IDLE_PAUSE_SECONDS: float = float(os.getenv("IDLE_PAUSE_SECONDS", "90"))
# …and a pause keeps the session open (muted) this long: «Єва, скажи» within it answers at once,
# later it opens a new session with the whole history.
PAUSE_CLOSE_SECONDS: float = float(os.getenv("PAUSE_CLOSE_SECONDS", "30"))

# Delivery prompt (prompts/live_prompt.py): v3 (default) = v2 + behaves like a person, v2, v1 the older one.
LIVE_PROMPT_VARIANT: str = os.getenv("LIVE_PROMPT_VARIANT", "v3").strip().lower()

# Off by default: the cue is synthesized by a separate TTS voice (TTS_VOICE), so «Угу» came out
# in a different voice than Єва's. GPT-Live now says the short acknowledgement itself.
VOICE_BUSY_CUES_ENABLED: bool = os.getenv("VOICE_BUSY_CUES_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
VOICE_BUSY_CUE_DELAY_MS: int = int(os.getenv("VOICE_BUSY_CUE_DELAY_MS", "900"))
VOICE_BUSY_CUE_SECOND_DELAY_MS: int = int(os.getenv("VOICE_BUSY_CUE_SECOND_DELAY_MS", "3000"))
VOICE_BUSY_CUE_MAX_PER_TURN: int = int(os.getenv("VOICE_BUSY_CUE_MAX_PER_TURN", "2"))

# ── OpenAI TTS ────────────────────────────────────────────────────────────────
TTS_VOICE: str = "nova"
TTS_MODEL: str = "tts-1"

# ── Local time ────────────────────────────────────────────────────────────────
# IANA zone for the date and time in the prompts («сьогодні», «доброго ранку»).
TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Kyiv")

# ── Wake phrase ───────────────────────────────────────────────────────────────
# Wake / pause phrases: «Єва, скажи» / «Дякую, Єва» — see voice/wake_phrases.py.

# ── Raw audio parameters ──────────────────────────────────────────────────────
SAMPLE_RATE: int = 16_000
CHANNELS: int = 1
SAMPLE_WIDTH: int = 2
CHUNK_SIZE: int = 512

# ── speech_recognition tuning ────────────────────────────────────────────────
STT_ENERGY_THRESHOLD: int = 300
STT_PAUSE_THRESHOLD: float = 0.5
STT_NON_SPEAKING_DURATION: float = 0.3
STT_TIMEOUT: float = 8.0
STT_PHRASE_LIMIT: float = 15.0
