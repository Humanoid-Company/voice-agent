"""The assistant's name and language, what the model is told about the time, and voice-name matching."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from difflib import get_close_matches
from zoneinfo import ZoneInfo

# code -> (name in Ukrainian, speech recognition locale, names the user may say). Short codes match exactly,
# the names as a part of the phrase («говори англійською»).
LANGUAGES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "uk": ("українська", "uk-UA", ("ukrainian", "українськ", "украинск")),
    "en": ("англійська", "en-US", ("english", "англійськ", "английск")),
    "ru": ("російська", "ru-RU", ("russian", "російськ", "русск", "по-русски")),
    "pl": ("польська", "pl-PL", ("polish", "польськ", "польск")),
    "de": ("німецька", "de-DE", ("german", "deutsch", "німецьк", "немецк")),
    "fr": ("французька", "fr-FR", ("french", "français", "французьк", "французск")),
    "es": ("іспанська", "es-ES", ("spanish", "español", "іспанськ", "испанск")),
    "it": ("італійська", "it-IT", ("italian", "italiano", "італійськ", "итальянск")),
}
# What each TTS provider can speak: Respeecher has a model per language (Ukrainian and English), Soniox one model.
SUPPORTED: dict[str, set[str]] = {"respeecher": {"uk", "en"}, "soniox": set(LANGUAGES)}

DEFAULT_NAME = "Єва"
_NAME_RE = re.compile(r"^[A-Za-zА-Яа-яІіЇїЄєҐґ'’ʼ -]{2,24}$")
_WEEKDAYS = ("понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя")
_MONTHS = (
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
)  # fmt: skip


@dataclass
class Persona:
    name: str = DEFAULT_NAME
    language: str = "uk"
    character: str = "eva"  # server/characters.py: who speaks and how
    emotion: str = "emotional"  # calm / normal / emotional

    @property
    def language_name(self) -> str:
        return LANGUAGES[self.language][0]

    @property
    def recognition(self) -> str:
        return LANGUAGES[self.language][1]


def find_language(query: str) -> str | None:
    q = (query or "").strip().lower()
    if q in LANGUAGES:
        return q
    return next((code for code, (_, _, names) in LANGUAGES.items() if any(n in q for n in names)), None)


ABOUT_MAX_CHARS = 4000


def sanitize_about(raw: object) -> str:
    """The knowledge the owner wrote about the agent and the place: text only, bounded. It is data for the model."""
    return raw.strip()[:ABOUT_MAX_CHARS] if isinstance(raw, str) else ""


def sanitize_name(raw: str) -> str | None:
    name = re.sub(r"\s+", " ", (raw or "").strip())
    return name if _NAME_RE.match(name) else None


def now_text(tz: str) -> str:
    """«понеділок, 27 жовтня 2026, 14:35, день» in the configured time zone."""
    try:
        now = datetime.now(ZoneInfo(tz))
    except Exception:
        now = datetime.now()
    hour = now.hour
    part = "ранок" if 5 <= hour < 11 else "день" if 11 <= hour < 17 else "вечір" if 17 <= hour < 22 else "ніч"
    return f"{_WEEKDAYS[now.weekday()]}, {now.day} {_MONTHS[now.month - 1]} {now.year}, {now:%H:%M}, {part}"


def find_voice(voices: list[dict], query: str) -> dict | None:
    """The voice the user named: by id or by the name part of the label («Ксенія» for «Ксенія: розмова (female)»)."""
    q = re.sub(r"\s+", " ", (query or "").strip().lower())
    if not q:
        return None
    names = [(v, re.split(r"\s*[(:]", v["label"], maxsplit=1)[0].strip().lower()) for v in voices]
    for test in (lambda v, n: q in (v["id"].lower(), n), lambda v, n: n.startswith(q), lambda v, n: q in n):
        hit = next((v for v, n in names if test(v, n)), None)
        if hit:
            return hit
    close = get_close_matches(q, [n for _, n in names], n=1, cutoff=0.7)
    return next((v for v, n in names if close and n == close[0]), None)
