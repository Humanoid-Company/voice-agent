from server.sentences import SentenceSplitter


def run(deltas):
    sp, out = SentenceSplitter(), []
    for d in deltas:
        out += sp.feed(d)
    return out + sp.flush()


def test_splits_on_terminators_across_deltas():
    assert run(["Привіт, як спра", "ви? У мене все ", "добре. Дякую!"]) == ["Привіт, як справи?", "У мене все добре.", "Дякую!"]


def test_short_first_piece_is_glued_to_next():
    assert run(["Так. Звісно, зараз розповім."]) == ["Так. Звісно, зараз розповім."]


def test_long_text_without_terminator_is_cut():
    out = run(["слово, " * 40])
    assert len(out) >= 2 and all(len(s) <= 170 for s in out)


def test_first_piece_may_end_at_a_comma_but_later_ones_wait_for_a_terminator():
    text = "Звісно, зараз я розповім вам про погоду, яка сьогодні сонячна. Завтра, мабуть, буде дощ і вітер."
    out = run(list(text))  # tokens arrive a few characters at a time, so the comma is seen before the full stop
    assert out[0] == "Звісно, зараз я розповім вам про погоду,"
    assert out[1:] == ["яка сьогодні сонячна.", "Завтра, мабуть, буде дощ і вітер."]


def test_flush_returns_tail_and_empty_is_nothing():
    assert run(["Без крапки"]) == ["Без крапки"]
    assert run([""]) == []
