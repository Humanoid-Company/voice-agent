from server.interrupt_intent import classify_interjection

SAID = "Перше речення тут. Друге речення тут. Погода сьогодні сонячна."


def test_echo_with_other_word_endings_is_still_echo():
    # the recognizer heard her through the speakers and rendered the endings differently
    assert classify_interjection("друге реченні тут погоді сьогодні", assistant_recent=SAID) == "echo"


def test_real_takeover_is_not_echo():
    assert classify_interjection("ні розкажи краще про футбол", assistant_recent=SAID) == "takeover"


def test_stop_and_backchannel_unchanged():
    assert classify_interjection("стоп", assistant_recent=SAID) == "stop"
    assert classify_interjection("угу", assistant_recent=SAID) == "backchannel"
