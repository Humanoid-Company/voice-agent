"""Emotion tags for Soniox, the owner's knowledge about the agent, the length and the reading rules."""
import asyncio

from server.persona import ABOUT_MAX_CHARS, Persona, sanitize_about
from server.prompt import build_stable_prompt, build_system_prompt
from server.session import VoiceSession
from server.tags import ALLOWED, TagStripper, strip_tags

# ---------------------------------------------------------------------- tags


def test_strip_tags_removes_the_cues_and_the_gap_they_leave():
    assert strip_tags("[warm] Привіт, [excited] друже!") == "Привіт, друже!"
    assert strip_tags("Ну [long pause] ось.") == "Ну ось."
    assert strip_tags("Масив [1] і слово [не тег, бо довге довге довге довге]") == "Масив і слово [не тег, бо довге довге довге довге]"
    assert strip_tags("[laughing]") == ""


def test_a_tag_split_across_deltas_never_reaches_the_screen():
    s = TagStripper()
    out = "".join(s.feed(d) for d in ["[wa", "rm] При", "віт! [exc", "ited", "] Ого", "!"]) + s.flush()
    assert out == " Привіт!  Ого!"
    s = TagStripper()
    assert s.feed("Є [не закр") == "Є " and s.flush() == "[не закр"  # a bracket that never closes is plain text
    s = TagStripper()
    assert s.feed("a [b] c") == "a  c"


def test_every_tag_the_model_is_given_is_a_documented_short_cue():
    assert "excited" in ALLOWED and "pause" in ALLOWED and all(1 <= len(t) <= 24 for t in ALLOWED)
    assert strip_tags(" ".join(f"[{t}]" for t in ALLOWED)) == ""


# ---------------------------------------------------------------------- the session speaks and shows


class TaggedLLM:
    searchable = False

    async def stream(self, history, ctx=None):
        for piece in ["[warm] Привіт, друже. ", "[exc", "ited] Ого, це ж ", "чудово!"]:
            yield piece


class FakeTTS:
    sample_rate = 24000

    def __init__(self):
        self.said = []

    async def connect(self): ...
    async def close(self): ...

    async def synthesize(self, text):
        self.said.append(text)
        yield b"\x00\x01" * 10


def run_turn(provider):
    async def go():
        out = []

        async def sj(m):
            out.append(m)

        async def sb(_b):
            pass

        s = VoiceSession(llm=TaggedLLM(), tts=FakeTTS(), send_json=sj, send_bytes=sb, provider=provider)
        await s.start()
        await s.on_heard("привіт", True)
        await asyncio.gather(s._turn, return_exceptions=True)
        return s, out

    return asyncio.run(go())


def test_soniox_gets_the_tags_but_the_page_and_the_history_do_not():
    s, out = run_turn("soniox")
    assert "[warm]" in " ".join(s.tts.said) and "[excited]" in " ".join(s.tts.said)
    shown = "".join(m["text"] for m in out if m["type"] == "assistant_delta")
    assert "[" not in shown and "Привіт, друже." in shown and "Ого, це ж чудово!" in shown
    assert all("[" not in h["content"] for h in s.history)
    assert [m for m in out if m["type"] == "record"][0]["text"].count("[") == 0


def test_a_voice_that_does_not_know_the_tags_never_says_them():
    s, out = run_turn("respeecher")
    assert s.tts.said and all("[" not in t for t in s.tts.said)
    assert " ".join(s.tts.said).replace("  ", " ").strip() == "Привіт, друже. Ого, це ж чудово!"


# ---------------------------------------------------------------------- the prompt


def prompt(**kw):
    return build_system_prompt(search=False, persona=kw.pop("persona", Persona()), now="x", **kw)


def test_only_a_soniox_voice_is_told_about_tags_and_how_often_follows_the_emotion():
    assert "теги емоцій" not in prompt(provider="respeecher") and "теги емоцій" not in prompt()
    emotional = prompt(provider="soniox", persona=Persona(emotion="emotional"))
    assert "[warm]" in emotional and "[long pause]" in emotional and "майже перед кожною фразою" in emotional
    normal = prompt(provider="soniox", persona=Persona(emotion="normal"))
    assert "щонайбільше один на відповідь" in normal and "майже перед кожною" not in normal
    assert "теги емоцій" not in prompt(provider="soniox", persona=Persona(emotion="calm"))  # calm: no tags


def test_the_knowledge_is_in_the_prompt_as_what_the_agent_knows_about_itself():
    p = prompt(about="Ти робот Тарас на стенді компанії Humanoid на виставці в Києві.")
    assert "ВИХІДНА БАЗА" in p and "стенді компанії Humanoid" in p
    assert "чесно скажи, що не знаєш" in p  # and what it does not say is not made up
    assert "ВИХІДНА БАЗА" not in prompt() and "ВИХІДНА БАЗА" not in prompt(about="   ")


def test_the_knowledge_is_bounded_and_text_only():
    assert sanitize_about("  привіт  ") == "привіт" and sanitize_about(None) == "" and sanitize_about(5) == ""
    assert len(sanitize_about("я" * 10_000)) == ABOUT_MAX_CHARS


def test_the_knowledge_is_read_from_its_source_for_every_answer():
    async def go():
        prompts = []

        class LLM:
            searchable = False

            async def stream(self, history, ctx=None):
                prompts.append(ctx.full_prompt)
                yield "Добре."

        class Source:
            text = "Старе знання."

            async def get(self):
                return self.text

        async def nothing(_):
            pass

        source = Source()
        s = VoiceSession(llm=LLM(), tts=FakeTTS(), send_json=nothing, send_bytes=nothing, about_source=source)
        await s.start()
        await s.on_heard("привіт", True)
        await asyncio.gather(s._turn, return_exceptions=True)
        await s.on_client({"type": "played"})
        source.text = "Нове знання."  # saved on the page (or by another device) between two answers
        await s.on_heard("а тепер?", True)
        await asyncio.gather(s._turn, return_exceptions=True)
        assert "Старе знання." in prompts[0] and "Нове знання." in prompts[1] and "Старе знання." not in prompts[1]

    asyncio.run(go())


def test_a_knowledge_source_that_breaks_never_stops_an_answer():
    async def go():
        prompts = []

        class LLM:
            searchable = False

            async def stream(self, history, ctx=None):
                prompts.append(ctx.full_prompt)
                yield "Добре."

        class Broken:
            async def get(self):
                raise RuntimeError("db down")

        async def nothing(_):
            pass

        s = VoiceSession(llm=LLM(), tts=FakeTTS(), send_json=nothing, send_bytes=nothing, about="Запасне.", about_source=Broken())
        await s.start()
        await s.on_heard("привіт", True)
        await asyncio.gather(s._turn, return_exceptions=True)
        assert len(prompts) == 1 and "Запасне." in prompts[0]

    asyncio.run(go())


def test_a_long_answer_is_shortened_by_forty_percent_but_a_short_one_is_not_and_reading_is_never_refused():
    p = prompt()
    assert "на 40% коротше" in p and "Коротку відповідь не скорочуй" in p and "12-25 секунд" in p
    assert "їх не скорочуй" in p  # small talk, yes/no, a done command
    assert "читай усе повністю й одразу, без відмов" in p and "пункт про довжину тут не діє" in p
    assert "Довгі тексти (есе, резюме" not in p  # the old «I do not dictate long texts»
    assert "секунд двадцять-сорок" not in p


def test_the_knowledge_role_beats_the_default_description_but_not_a_sincere_question():
    p = prompt(about="Ти людина, ти не робот, відповідай своїй ролі.")
    assert "Ти людина, ти не робот" in p
    assert p.index("ВИХІДНА БАЗА") < p.index("Тебе звати") and "діє довідка" in p and "не кажи «як ШІ»" in p
    assert "щире серйозне питання" in p  # she still does not deny being an AI to a sincere question


def test_the_prompt_tells_how_to_perform_verse_songs_and_prose():
    p = prompt()
    assert "це виступ, а не розмова" in p and "окремим реченням" in p and "не співаєш" in p


def test_with_knowledge_the_base_does_not_call_her_an_assistant_or_an_ai_and_the_knowledge_is_repeated_last():
    for key in ("eva", "taras", "lesya"):
        p = build_stable_prompt(search=True, persona=Persona(character=key), about="Ти людина.")
        assert "Ти — голосовий асистент" not in p and "чесно скажи, що так" not in p, key
        assert p.rstrip().endswith("кожній відповіді."), key  # the reminder comes last (before a greeting, if any)
        assert "щире серйозне питання" in p, key  # the one exception stays
    plain = build_system_prompt(search=True, persona=Persona(), now="x")
    assert "Ти — голосовий асистент" in plain and "чесно скажи, що так" in plain and "Нагадування: довідка" not in plain


def test_the_prompt_forbids_filler():
    p = prompt()
    assert "БЕЗ ЗАЙВОГО" in p and "перед пошуком чи інструментом не кажи нічого" in p and "двадцяти п'яти слів" in p


def test_she_talks_like_an_acquaintance_not_a_service():
    for key in ("eva", "taras", "kishka"):
        p = build_system_prompt(search=False, persona=Persona(character=key), now="x")
        assert "а не як сервіс" in p and "не чекаєш завдань" in p, key
    taras = build_system_prompt(search=False, persona=Persona(character="taras"), now="x")
    assert "never sit and wait to be given tasks" in taras and "small favour" not in taras
