"""Delivery prompt variants for GPT-Live: v3 (default, v2 + behaving like a person), v2, v1 (older)."""
from __future__ import annotations

from prompts.live_prompt import LIVE_PROMPT, PROMPT_VARIANTS, build_live_prompt

COMMON = dict(language_name="українською", assistant_name=None, today="2026-10-09")


def test_variants():
    assert PROMPT_VARIANTS == ("v1", "v2", "v3", "v4", "v5")
    assert "you are talking, not reading" in build_live_prompt(**COMMON)
    assert "Being a person" in build_live_prompt(**COMMON)
    assert "Answering" in build_live_prompt(**COMMON)  # v4 by default
    assert "you are talking, not reading" not in build_live_prompt(variant="v1", **COMMON)
    assert "Listening like a person" in LIVE_PROMPT


def test_v3_adds_person_to_v2():
    v2 = build_live_prompt(variant="v2", **COMMON)
    v3 = build_live_prompt(variant="v3", **COMMON)
    assert "Being a person" not in v2 and "Being a person" in v3
    assert "you are talking, not reading" in v3  # v2's sound
    assert "end most replies with one short, specific question" in v3  # keeps the conversation going


def test_v4_answers_in_full():
    v3 = build_live_prompt(variant="v3", **COMMON)
    v4 = build_live_prompt(variant="v4", **COMMON)
    assert "Answering" in v4 and "Answering" not in v3
    # what kept her answers thin is gone from v4 only
    assert "Brief by default (one to three sentences)" in v3 and "Brief by default" not in v4
    assert "one good thought beats a full overview" in v3 and "one good thought" not in v4
    assert "end most replies with one short, specific question" not in v4
    assert "Answer first, then keep the talk going" in v4 and "never in place of the content" in v4
    assert "Listening like a person" not in v4 and "let them finish" in v4
    # the rest stays: sound, being a person, hearing, interruptions, delegation
    for part in ("you are talking, not reading", "Being a person", "Interruption policy", "Delegation:", "Pronunciation"):
        assert part in v4


def test_v4_balances_length():
    """Measured on GPT-Live (12 spoken lines, a judge on length): «Оттава. А ти там був?», a list of
    tips after «я втомився», two questions in a row — length right 7/12 → 20/24 with these rules."""
    from prompts.backend_prompt import BACKEND_PROMPT

    v4 = build_live_prompt(language_name="Ukrainian", assistant_name=None, today="2026-10-14", variant="v4")
    assert "«Оттава.», not «Оттава. А ти там був?»" in v4 and "Never two questions in one reply" in v4
    assert "No advice, tips, ideas or plans they did not ask for" in v4
    assert "A quick fact or number: one sentence" in BACKEND_PROMPT and "no unasked advice" in BACKEND_PROMPT


def test_v5_is_v4_condensed():
    """v5: v4's rules said once each (v4 repeated «answer at once» three times, «one question at the
    end» twice) — under half the size, the rules that came from feedback still there."""
    v4 = build_live_prompt(variant="v4", **COMMON)
    v5 = build_live_prompt(variant="v5", voice="gleam", **COMMON)
    assert len(v5) < len(v4) / 2
    for part in ("Let them finish", "never answer a fragment", "«На Rust?»", "«Оттава.»",
                 "never «Хочеш, розкажу…?»", "no advice they did not ask for", "Never two.",
                 "Never dictate code", "rough figure", "always delegate it at once", "drop it and answer what "
                 "they said last", "new request — answer it", "Never assume their gender", "with this voice"):
        assert part.lower() in v5.lower(), part
    assert "(prod)" not in v5
