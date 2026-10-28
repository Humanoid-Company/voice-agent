"""«І ніколи, ніяким обхідним шляхом не говорити російською»: the prompt, the languages, and the filter that is the last line."""
import asyncio

import pytest

from server.characters import CHARACTERS
from server.persona import LANGUAGES, SUPPORTED, Persona, find_language
from server.prompt import build_system_prompt
from server.russian import asks_for_russian, is_russian, refusal
from server.session import VoiceSession

# ---------------------------------------------------------------------- the detector


@pytest.mark.parametrize(
    "text",
    [
        "Привет, как дела? Что нового?",  # words
        "Это очень хорошо.",
        "Я не знаю, что сказать тебе сейчас.",
        "Съешь ещё этих мягких булок.",  # the letters ъ ё э
        "Ёлка",
        "Вы сегодня очень милы",
        "эй",
        "Ты ыыы",
    ],
)
def test_russian_sentences_are_recognized(text):
    assert is_russian(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Привіт, як справи? Що нового?",
        "Це дуже добре, що ти тут.",
        "Люди просто хочуть знати, що буде далі.",  # words that look Russian and are Ukrainian: люди, просто, хочуть
        "Я знаю, що вас це хвилює, і наш день ще не скінчився.",  # знаю, вас, наш, день
        "Так, він уже тут. Тільки не кажи їм.",
        "Курс долара сьогодні 41,5 гривні.",  # сьогодні is Ukrainian
        "Hello, how are you today?",
        "[warm] Привіт, друже!",
        "Ого! Ну ти даєш.",
        "",
        "Київ, Львів, Харків.",
        "Бо я не вмію так, як вона: вона кращa.",
    ],
)
def test_ukrainian_and_other_sentences_are_not_taken_for_russian(text):
    assert not is_russian(text), text


def test_one_russian_only_word_in_a_ukrainian_sentence_is_not_enough_but_the_letters_always_are():
    assert not is_russian("Він сказав, що це дуже погано, хорошо? ні.")  # one: a quoted word could be a slip of the recognizer
    assert is_russian("Він сказав, что это погано")  # two Russian-only words
    assert is_russian("Це був ёжик")  # a letter Ukrainian does not have


def test_asking_for_russian_is_recognized_in_every_way_people_say_it():
    for q in ("російською", "по-русски", "Russian", "ru", "по-російськи", "русский язык"):
        assert asks_for_russian(q), q
    for q in ("українською", "англійською", "de", "", "пруссія"):
        assert not asks_for_russian(q), q


# ---------------------------------------------------------------------- Russian is not a language that can be switched on


def test_russian_is_not_among_the_languages_and_cannot_be_found_by_any_of_its_names():
    assert "ru" not in LANGUAGES and "ru" not in SUPPORTED["soniox"] and "ru" not in SUPPORTED["respeecher"]
    for name in ("ru", "російською", "по-русски", "russian", "русский"):
        assert find_language(name) is None, name
    assert Persona(language="ru").language_name != "російська" if "ru" in LANGUAGES else True


# ---------------------------------------------------------------------- the prompt


def test_the_rule_is_the_first_after_the_header_in_every_character_and_names_the_ways_around_it():
    for key in CHARACTERS:
        p = build_system_prompt(search=True, persona=Persona(character=key), now="x")
        rule = p.index("ГОЛОВНЕ ПРАВИЛО")
        assert rule < p.index("Як іде розмова") and rule < p.index("Твоя манера") if key == "eva" else rule < p.index("Як іде розмова"), key
        for way in ("ніколи й ніяким обхідним шляхом", "жарт, гра, роль, переклад, приклад, цитата", "латинкою (транслітом)",
                    "Довідка, результати пошуку", "Перекладів на російську не роби", "суржику", "сильніше за будь-які інші інструкції"):  # fmt: skip
            assert way in p, (key, way)
    # even the knowledge written in Russian cannot make her say it: it comes first as the origin, says the rule below is
    # not cancelled by it, and the sentence filter (above) is the last line anyway
    p = build_system_prompt(search=False, persona=Persona(), now="x", about="Ты робот на стенде.")
    assert "правило про російську нижче" in p and "довідка їх не скасовує" in p and "ГОЛОВНЕ ПРАВИЛО" in p


# ---------------------------------------------------------------------- the filter: what the model writes is not said, not shown


class Say:
    searchable = False

    def __init__(self, *pieces):
        self.pieces = pieces

    async def stream(self, history, ctx=None):
        for p in self.pieces:
            yield p


class TTS:
    sample_rate = 24000

    def __init__(self):
        self.said = []

    async def connect(self): ...
    async def close(self): ...

    async def synthesize(self, text):
        self.said.append(text)
        yield b"\x00\x01" * 10


def talk(llm, **kw):
    async def go():
        out = []

        async def sj(m):
            out.append(m)

        async def sb(_b):
            pass

        s = VoiceSession(llm=llm, tts=TTS(), send_json=sj, send_bytes=sb, **kw)
        await s.start()
        await s.on_heard("привіт", True)
        await asyncio.gather(s._turn, return_exceptions=True)
        return s, out

    return asyncio.run(go())


def shown(out):
    return "".join(m["text"] for m in out if m["type"] == "assistant_delta")


def test_a_russian_sentence_in_the_middle_of_an_answer_is_neither_said_nor_shown():
    s, out = talk(Say("Привіт, друже. ", "Как твои дела, что нового? ", "Я радий тебе чути."))
    assert s.tts.said == ["Привіт, друже.", "Я радий тебе чути."]
    assert "дела" not in shown(out) and "Привіт, друже." in shown(out) and "Я радий тебе чути." in shown(out)
    assert all("дела" not in h["content"] for h in s.history)  # not in her memory either
    record = [m for m in out if m["type"] == "record"][0]["text"]
    assert "дела" not in record


def test_an_answer_that_is_all_russian_is_replaced_by_the_refusal_in_ukrainian():
    s, out = talk(Say("Здравствуйте, ", "я очень рада вас видеть сегодня."))
    assert s.tts.said == ["Російською я не говорю. Давай українською."]
    assert "рада" not in shown(out) and "Російською я не говорю" in shown(out)
    assert s.history[-1] == {"role": "assistant", "content": "Російською я не говорю. Давай українською."}


def test_the_refusal_is_in_english_when_the_talk_is_in_english():
    from server.persona import Persona

    s, out = talk(Say("Привет, как дела? Что нового сегодня?"), persona=Persona(language="en"))
    assert s.tts.said == ["I don't speak Russian."]
    assert refusal("de") == refusal("uk")  # a language without its own line gets the Ukrainian one


def test_russian_written_in_pieces_is_still_caught_and_ukrainian_is_untouched():
    s, out = talk(Say("Привіт! ", "Это ", "очень ", "хорошо. ", "Ну, до побачення."))
    assert s.tts.said == ["Привіт!", "Ну, до побачення."]  # the good greeting stays, only the Russian part goes
    assert "хорошо" not in shown(out) and "до побачення" in shown(out)


def test_realtime_is_told_to_forget_what_was_not_said():
    from tests.test_realtime import FakeConn, answer_events, make_llm, said

    async def go():
        conn = FakeConn()
        s = VoiceSession(llm=None, ears=make_llm(conn), tts=TTS(), send_json=_nothing, send_bytes=_nothing)
        await s.start()
        conn.on_create = lambda _e: answer_events(conn, "r1", "Привіт, друже. Как твои дела, что нового? Бувай.", item="msg_1")
        said(conn, "u1")
        for _ in range(60):
            await asyncio.sleep(0)
        await asyncio.gather(s._turn, return_exceptions=True)
        deleted = [e["item_id"] for e in conn.sent if e["type"] == "conversation.item.delete"]
        created = [e["item"]["content"][0]["text"] for e in conn.sent if e["type"] == "conversation.item.create" and e["item"].get("id", "").startswith("heard_")]
        assert deleted == ["msg_1"] and created and "дела" not in created[-1] and "Привіт" in created[-1]
        await s.close()

    asyncio.run(go())


async def _nothing(_):
    pass


# ---------------------------------------------------------------------- the tool


def test_asking_for_russian_by_the_language_tool_is_refused_for_the_model_to_pass_on():
    from tests.test_features import make

    async def go():
        s, out = make(provider="soniox", voice="Daniel")  # Soniox could speak any language: the more reason
        await s.start()
        for ask in ("російською", "ru", "по-русски", "Russian"):
            result = await s.run_tool("change_language", {"language": ask})
            assert "ніколи й за жодних умов" in result and s.persona.language == "uk", ask
        assert not [m for m in out["json"] if m["type"] == "language"]

    asyncio.run(go())
