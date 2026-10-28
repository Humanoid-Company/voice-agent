"""Emotion tags for Soniox: a short English cue in square brackets in front of the words it colours («[warm] Привіт!»).
Soniox reads them as direction; any other synthesizer would say them aloud, and the page must not show them: so the
text is cleaned before it is displayed, kept, or sent to a voice that does not understand them."""
from __future__ import annotations

import re

# The cues Soniox documents (docs «Emotion & tone»). Others "may be read aloud", so the model is given only these.
ALLOWED = (
    "excited", "warm", "calm", "sad", "nervous", "stern", "sarcastic", "deadpan",
    "laughing", "chuckling", "sighing", "gasping", "whispering", "shouting", "slowly", "rushed",
    "pause", "long pause",
)  # fmt: skip

_TAG = re.compile(r"\[[^\[\]]{1,24}\]")
_MAX_OPEN = 26  # a «[» this close to the end of what has arrived may still become a tag


def strip_tags(text: str) -> str:
    """The same words without the tags (and without the gap a tag leaves)."""
    return re.sub(r"[ \t]{2,}", " ", _TAG.sub("", text)).strip()


class TagStripper:
    """Removes tags from a stream of text deltas: a tag can arrive in pieces («[wa» + «rm] Привіт»), so a «[» that may
    still become one is held back until it closes or turns out to be plain text."""

    def __init__(self) -> None:
        self._held = ""

    def feed(self, delta: str) -> str:
        text = self._held + delta
        self._held = ""
        text = _TAG.sub("", text)
        i = text.rfind("[")
        if i != -1 and "]" not in text[i:] and len(text) - i <= _MAX_OPEN:
            self._held, text = text[i:], text[:i]
        return text

    def flush(self) -> str:
        """The end of the answer: a «[…» that never closed was plain text after all."""
        held, self._held = self._held, ""
        return held
