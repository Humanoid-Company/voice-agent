"""The agent never speaks Russian, by no route: not on request, not as a game, a role, a translation, a quote or one word.

The prompt says so first (server/prompt.py), Russian is not a language that can be switched on (server/persona.py), and
this is the last line: whatever the model writes, a sentence that is Russian is neither said nor shown (server/session.py).
The test is for what the model might slip into an answer; it is deliberately cautious (a Ukrainian sentence must never be
taken for Russian), so it looks only for what Ukrainian does not have: the letters ы э ъ ё, and words that are Russian
only (none that is also a Ukrainian word: not «люди», «день», «просто», «хочу», «знаю», «вас», «наш»...)."""
from __future__ import annotations

import re

# Letters Ukrainian does not have (it has і ї є ґ instead).
_RUSSIAN_LETTERS = re.compile("[ыэъёЫЭЪЁ]")

# Words that are Russian and not Ukrainian, in lowercase. A sentence with two different ones is Russian.
_RUSSIAN_WORDS = frozenset(
    """
    что это как вот если чтобы чтоб который которая которые которое когда потому очень можно нужно спасибо пожалуйста
    хорошо сейчас здравствуйте почему тоже только сегодня сказать говорить меня мне себя быть был была были они она оно
    есть нет ещё где куда откуда зачем какой какая какие какое такой такая такие мой твой свой свои его ему ей им ими
    него их будет будут будешь будем будете хочешь хочет хотим хотите могу можешь может можем привет ладно конечно
    например поэтому однако также вообще совсем сказал сказала сделать сделал сделала знаешь знает знать понимаю
    понимаешь понял поняла дело время год лет человек дни
    """.split()
)

_WORD = re.compile(r"[а-яіїєґёыэъ']+", re.IGNORECASE)

REFUSAL = {
    "uk": "Російською я не говорю. Давай українською.",
    "en": "I don't speak Russian.",
}


def refusal(language: str) -> str:
    """What is said instead when nothing of an answer could be said because all of it was Russian."""
    return REFUSAL.get(language, REFUSAL["uk"])


def is_russian(text: str) -> bool:
    """Is this sentence Russian? True for the letters Ukrainian lacks, or for two different Russian-only words."""
    if _RUSSIAN_LETTERS.search(text):
        return True
    words = {w.lower() for w in _WORD.findall(text)}
    return len(words & _RUSSIAN_WORDS) >= 2


_ASKS = re.compile(r"russian|російськ|русск|по-русски|по-російськи|\bru\b", re.IGNORECASE)


def asks_for_russian(text: str) -> bool:
    """Is a language the user asked for Russian («говори російською», «по-русски», «ru»)?"""
    return bool(_ASKS.search(text or ""))
