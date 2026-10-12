"""Prompt for the Responses backend that runs the voice model's tools (GPT-Live delegation)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from config import TIMEZONE

BACKEND_PROMPT: str = (
    "You are the tool backend for a voice assistant. You never speak to the user directly — call "
    "the right tool and return a short result; the voice model says it in its own words.\n"
    "\n"
    "TOOLS: set_assistant_name, change_voice, set_voice_style (speed / manner), change_language, "
    "end_conversation. Call a tool only for what the user asked; never invent a result. If a tool "
    "says the request is not possible, pass its message on.\n"
)


def local_today() -> str:
    """Date, weekday and time in the user's timezone, for «Today is …» in the prompts. The server
    runs in UTC: date.today() there was yesterday's date until 02:00–03:00 Kyiv time, and without
    the time of day she could not tell morning from late night."""
    now = datetime.now(ZoneInfo(TIMEZONE))
    return f"{now:%A}, {now:%Y-%m-%d}, local time {now:%H:%M} ({TIMEZONE})"


def build_backend_prompt(*, today: str, language_name: str) -> str:
    return BACKEND_PROMPT + f" Today is {today}. Prefer tool messages in {language_name}."
