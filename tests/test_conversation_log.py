"""ConversationLog: the dialogue history that outlives a Live session (and its voice)."""
from __future__ import annotations

from voice.conversation import ConversationLog


def test_deltas_group_into_turns_by_speaker():
    log = ConversationLog()
    for role, delta in [("user", "При"), ("user", "віт"), ("assistant", "Привіт!"), ("user", "Як справи?")]:
        log.add(role, delta)
    assert [(t.role, t.text) for t in log.turns()] == [
        ("user", "Привіт"),
        ("assistant", "Привіт!"),
        ("user", "Як справи?"),
    ]


def test_live_input_keeps_the_newest_turns_within_limits():
    log = ConversationLog()
    for i in range(10):
        log.add("user", f"питання {i}")
        log.add("assistant", f"відповідь {i}")
    items = log.live_input(max_messages=4)
    assert [i["content"][0]["text"] for i in items] == ["питання 8", "відповідь 8", "питання 9", "відповідь 9"]
    assert items[0]["content"][0]["type"] == "input_text"
    assert items[1]["content"][0]["type"] == "output_text"
    assert log.live_input(max_chars=12)[-1]["content"][0]["text"] == "відповідь 9"


def test_end_turn_splits_same_speaker():
    log = ConversationLog()
    log.add("user", "перше")
    log.end_turn()
    log.add("user", "друге")
    assert [t.text for t in log.turns()] == ["перше", "друге"]


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_a_word_said_over_the_user_does_not_cut_their_sentence():
    """Web (merge_interleaved): «О,» over the user's sentence stayed a fragment-by-fragment shred."""
    clock = _Clock()
    log = ConversationLog(merge_interleaved=True, clock=clock)
    for role, delta, dt in [("user", "Мене звати Остап, і я люблю каву без", 0), ("assistant", "О,", 0.3),
                            ("user", " цукру. Зап", 0.3), ("assistant", " добре", 0.2), ("user", "ам'ятай", 0.2),
                            ("assistant", ", запам'ятала.", 0.5), ("user", "Дякую!", 4.0)]:
        clock.t += dt
        log.add(role, delta)
    assert [(t.role, t.text) for t in log.turns()] == [
        ("user", "Мене звати Остап, і я люблю каву без цукру. Запам'ятай"),
        ("assistant", "О, добре, запам'ятала."),
        ("user", "Дякую!"),  # after a pause: a new line
    ]


def test_merged_lines_still_split_on_end_turn_and_glue_sentences():
    clock = _Clock()
    log = ConversationLog(merge_interleaved=True, clock=clock)
    log.add("assistant", "Недочула.")
    log.add("assistant", "О, та добре")
    log.end_turn()
    log.add("assistant", "Наступне")
    assert [t.text for t in log.turns()] == ["Недочула. О, та добре", "Наступне"]


def test_a_whole_answer_ends_the_users_line_even_without_a_pause():
    log = ConversationLog(merge_interleaved=True, clock=_Clock())
    for role, delta in [("user", "Мене звати Остап."), ("assistant", "Приємно, Остапе! Запам'ятала."),
                        ("user", "Зміни голос.")]:
        log.add(role, delta)
    assert [t.text for t in log.turns()] == ["Мене звати Остап.", "Приємно, Остапе! Запам'ятала.", "Зміни голос."]
