"""Where the time of one answer goes: the end of the user's phrase, then each stage up to the first sound in the browser.

Each stage is marked once (the first time it happens in the turn); the report is the milliseconds since the start."""
from __future__ import annotations

import time

# In the order they happen; what a report is built from.
STAGES = (
    "turn_start",  # the session started the answer (after the item wait, the knowledge lookup)
    "create_sent",  # response.create went to Realtime
    "response_created",  # Realtime accepted it
    "first_delta",  # the first text of the answer
    "first_sentence",  # the first sentence is complete and goes to the voice
    "tts_audio",  # the first audio chunk from the voice provider
    "playing",  # the browser started to play (its report, plus the head start it gave itself)
)


class TurnTimer:
    def __init__(self, start: float | None = None) -> None:
        self.start = time.monotonic() if start is None else start
        self.marks: dict[str, float] = {}
        self.reported = False

    def mark(self, stage: str, *, at: float | None = None) -> None:
        self.marks.setdefault(stage, time.monotonic() if at is None else at)

    def report(self) -> dict[str, int]:
        """Milliseconds from the start for each stage that happened."""
        return {s: round((self.marks[s] - self.start) * 1000) for s in STAGES if s in self.marks}

    def line(self) -> str:
        r = self.report()
        parts, prev = [], 0
        for stage, ms in r.items():
            parts.append(f"{stage} {ms} (+{ms - prev})")
            prev = ms
        return "; ".join(parts)
