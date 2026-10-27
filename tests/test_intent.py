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


from server.interrupt_intent import strip_echo  # noqa: E402

SAID2 = "У мене все добре, дякую, що спитав. А як у тебе справи?"


def test_her_words_glued_to_the_users_reply_are_cut_out():
    # a real case: the recognizer heard her last words through the speakers, then the user answered
    heard = "В мене все добре дякую що спитав А як у тебе справи Нормально Що порадиш мені сьогодні зробити"
    assert strip_echo(heard, SAID2) == "Нормально Що порадиш мені сьогодні зробити"


def test_pure_echo_leaves_nothing():
    assert strip_echo("дякую що спитав а як у тебе справи", SAID2) == ""


def test_short_overlap_and_unrelated_text_are_left_alone():
    assert strip_echo("а як справи у футболу", SAID2) == "а як справи у футболу"
    assert strip_echo("Розкажи про погоду на завтра", SAID2) == "Розкажи про погоду на завтра"
    assert strip_echo("щось сказати", "") == "щось сказати"
