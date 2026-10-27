"""Cut a streaming LLM answer into sentences, so TTS can start before the answer is complete."""
from __future__ import annotations

import re

_END = re.compile(r"(?<=[.!?…])[\"»”')\]]*\s+|\n+")
# A short first piece ("Так.", "Ну,") sounds choppy as a separate TTS request: glue it to the next.
_MIN_CHARS = 12
# No terminator for this long: cut at the last comma/space instead of making the user wait.
_MAX_CHARS = 160


class SentenceSplitter:
    def __init__(self) -> None:
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []
        while True:
            piece = self._take()
            if piece is None:
                return out
            out.append(piece)

    def flush(self) -> list[str]:
        rest, self._buf = self._buf.strip(), ""
        return [rest] if rest else []

    def _take(self) -> str | None:
        pos = 0
        for m in _END.finditer(self._buf):
            end = m.end()
            if len(self._buf[:end].strip()) >= _MIN_CHARS:
                pos = end
                break
        if not pos and len(self._buf) > _MAX_CHARS:
            cut = max(self._buf.rfind(", ", 0, _MAX_CHARS), self._buf.rfind(" ", 0, _MAX_CHARS))
            pos = cut + 1 if cut > 0 else _MAX_CHARS
        if not pos:
            return None
        piece, self._buf = self._buf[:pos].strip(), self._buf[pos:]
        return piece or self._take()
