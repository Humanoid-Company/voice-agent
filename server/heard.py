"""What the user actually heard of an answer that was cut off.

The server finishes generating a long answer long before the browser has played it, so «what was said» (the sentences
sent) is more than «what was heard». The page reports how many seconds of the answer had played when it stopped; with the
duration of each sentence that gives the place where the answer was cut, and the agent's memory is trimmed to it."""
from __future__ import annotations

_TOLERANCE_S = 0.15  # a sentence that had all but a breath played counts as heard


def heard_part(audio: list[tuple[str, float]], played_s: float) -> str | None:
    """The text of the answer up to `played_s` seconds of its audio, ending in «…» where it was cut; None when the whole
    answer had played (nothing to trim). `audio` is (sentence, seconds of audio) in the order spoken. A sentence cut in
    the middle is kept as far as its words had been said (by the share of its time that played)."""
    total = sum(d for _, d in audio)
    if not audio or played_s >= total - _TOLERANCE_S:
        return None
    kept: list[str] = []
    t = 0.0
    for sentence, dur in audio:
        if played_s >= t + dur - _TOLERANCE_S:
            kept.append(sentence)
            t += dur
            continue
        share = (played_s - t) / dur if dur > 0 else 0.0
        words = sentence.split()
        n = min(len(words), max(0, round(share * len(words))))
        if n:
            kept.append(" ".join(words[:n]))
        break
    return (" ".join(kept) + " …") if kept else ""
