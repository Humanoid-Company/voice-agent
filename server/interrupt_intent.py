"""Does the user actually want to interrupt the assistant? Decided from partial transcripts.

The assistant often talks over laptop speakers, sometimes to a room, without hardware echo
cancellation. While it speaks, the input transcript also catches listeners' backchannels
("угу", "ага"), laughter, side conversations and the assistant's own voice coming back
through the mic. None of those should cut it off. Only a stop word or the user clearly
taking the turn should.
"""
from __future__ import annotations

import re
from typing import Literal

InterruptIntent = Literal["stop", "takeover", "backchannel", "echo", "unclear"]

# Explicit "stop talking" phrases (uk/ru/en) — interrupt immediately, reply with a short ack.
_STOP_PHRASES = (
    "стоп",
    "стій",
    "стой",
    "зачекай",
    "зачекайте",
    "почекай",
    "почекайте",
    "чекай",
    "подожди",
    "погоди",
    "тихо",
    "досить",
    "хватит",
    "не треба",
    "перестань",
    "замовкни",
    "помовч",
    "секунду",
    "секундочку",
    "хвилинку",
    "минутку",
    "stop",
    "wait",
    "hold on",
)
# Openers people use to grab the turn ("слухай, …") — a takeover even before more words land.
_TAKEOVER_OPENERS = ("слухай", "послухай", "стривай", "та ні", "ні ні", "ні-ні", "нет нет", "а ще", "скажи")

# Listener feedback that must not stop the speaker.
_BACKCHANNELS = frozenset(
    {
        "угу", "ага", "ага-ага", "мгм", "ммм", "мм", "м", "хм", "ну", "так", "так-так", "да", "ну да",
        "ок", "окей", "добре", "ясно", "зрозуміло", "ого", "вау", "ух", "ха", "ха-ха", "хаха", "ахах",
        "ага-а", "ееее", "е", "а", "о", "ой", "ох", "оу", "угу-угу", "слухаю", "нічого", "собі", "та", "клас", "супер", "круто", "правда", "справді",
        "серйозно", "точно", "звісно", "yeah", "yes", "ok", "okay", "uh-huh", "mhm", "wow",
    }
)
_WORD_RE = re.compile(r"[\w'’ʼ-]+", re.UNICODE)
# Єва's own listening sounds («угу», «так-так», «ммм») are at most this long.
_MAX_BACKCHANNEL_WORDS = 3
_TAKEOVER_MIN_WORDS = 2
_ECHO_OVERLAP = 0.6
_STEM = 5


def _words(text: str) -> list[str]:
    cleaned = (text or "").lower().replace("’", "'").replace("ʼ", "'")
    return _WORD_RE.findall(cleaned)


def _has_phrase(words: list[str], phrases: tuple[str, ...]) -> bool:
    joined = " " + " ".join(words) + " "
    return any(f" {phrase} " in joined for phrase in phrases)


def _is_echo(words: list[str], assistant_recent: str) -> bool:
    """Most of what the mic 'heard' is what the assistant itself just said."""
    content = [w for w in words if len(w) >= 3]
    if len(content) < 2 or not assistant_recent:
        return False
    # By the first letters: the recognizer renders her words with other endings («речення» / «реченні»).
    spoken = {w[:_STEM] for w in _words(assistant_recent)}
    overlap = sum(1 for w in content if w[:_STEM] in spoken)
    return overlap / len(content) >= _ECHO_OVERLAP


def is_backchannel_utterance(text: str) -> bool:
    """What the assistant is saying is only a listener sound («Угу.», «Так-так.», «Ммм.»), said
    while the user talks — not an answer the user would be interrupting."""
    words = _words(text)
    return 0 < len(words) <= _MAX_BACKCHANNEL_WORDS and all(w in _BACKCHANNELS for w in words)


def classify_interjection(text: str, *, assistant_recent: str = "") -> InterruptIntent:
    """Classify what the user said while the assistant was talking.

    `text` is the accumulated partial transcript of the current candidate utterance;
    `assistant_recent` is what the assistant has been saying (for echo detection).
    """
    words = _words(text)
    if not words:
        return "unclear"
    # Echo first: the assistant saying «скажіть стоп» must not stop itself via the mic.
    if _is_echo(words, assistant_recent):
        return "echo"
    if _has_phrase(words, _STOP_PHRASES):
        return "stop"
    if _has_phrase(words, _TAKEOVER_OPENERS):
        return "takeover"
    if " ".join(words) in _BACKCHANNELS or all(w in _BACKCHANNELS for w in words):
        return "backchannel"
    meaningful = [w for w in words if w not in _BACKCHANNELS]
    if len(meaningful) >= _TAKEOVER_MIN_WORDS:
        return "takeover"
    return "unclear"


_ECHO_RUN = 3  # this many words in a row, in her order, are her voice and not the user's


def strip_echo(text: str, assistant_recent: str) -> str:
    """Cut the stretches of `text` that repeat what the assistant just said, word for word.

    Speech recognition hears the speakers and glues her last words to the user's reply («…дякую що спитав
    А як у тебе справи» + «Нормально, що порадиш»). Whole-utterance echo checks miss that, as the user's own
    words dilute the overlap. A run of three or more words that follows her own word order is her; shorter
    overlaps are left alone, since people do repeat a word or two. Returns `text` itself when nothing matches.
    """
    raw = _WORD_RE.findall((text or "").replace("’", "'").replace("ʼ", "'"))  # as said: the model gets these
    tokens = [w.lower() for w in raw]
    spoken = [w[:_STEM] for w in _words(assistant_recent)]
    if len(tokens) < _ECHO_RUN or len(spoken) < _ECHO_RUN:
        return text
    runs = {tuple(spoken[i : i + _ECHO_RUN]) for i in range(len(spoken) - _ECHO_RUN + 1)}
    stems = [w[:_STEM] for w in tokens]
    echo = [False] * len(tokens)
    for i in range(len(tokens) - _ECHO_RUN + 1):
        if tuple(stems[i : i + _ECHO_RUN]) in runs:
            echo[i : i + _ECHO_RUN] = [True] * _ECHO_RUN
    if not any(echo):
        return text
    # A stray «в» / «а» glued to her words (the recognizer's rendering of her «у») goes with them.
    for i, t in enumerate(tokens):
        if not echo[i] and len(t) <= 2 and ((i > 0 and echo[i - 1]) or (i + 1 < len(tokens) and echo[i + 1])):
            echo[i] = True
    return " ".join(w for w, e in zip(raw, echo, strict=True) if not e)
