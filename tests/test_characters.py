"""Characters (Толік, Кішка) and the emotion level: the prompt, their voices, the name and the language rules.
A voice never changes by itself."""
import asyncio

from server.characters import (
    CHARACTERS,
    EMOTIONS,
    character_prompt,
    characters_payload,
    find_character,
    normalize_character_id,
)
from server.persona import Persona
from server.prompt import build_system_prompt
from tests.test_features import FakeLLM, make, of, say

# The Respeecher voices the deployed backend listed on 2026-10-27 (/api/config): the characters' voices must be there.
RESPEECHER_UK = {
    "olesia-conversation", "olesia-media", "olesia-announcement", "oleksandr-radio", "oleksandr-commercial",
    "kseniia-conversation", "roksolana-conversation", "yevhen-commercial", "yevhen-audiobook", "dmytro-conversation",
    "ihoreo-media", "ihoreo-conversation", "mariia-audiobook",
}  # fmt: skip


def test_a_character_is_found_by_the_way_people_call_it():
    assert find_character("поклич Толіка") == "tolik" and find_character("Толік") == "tolik"
    assert find_character("хочу поговорити з кішкою") == "kishka" and find_character("Кішка") == "kishka"
    assert find_character("поклич кота") == "kishka" and find_character("котик") == "kishka"  # the old name still works
    assert find_character("давай знову як Єва") == "eva" and find_character("tolik") == "tolik"
    assert find_character("Гарольд") is None and find_character("") is None


def test_the_cat_is_a_she_cat_now_and_an_old_stored_choice_follows():
    assert "kit" not in CHARACTERS and CHARACTERS["kishka"].label == "Кішка" and CHARACTERS["kishka"].feminine
    assert normalize_character_id("kit") == "kishka" and normalize_character_id("tolik") == "tolik"
    assert normalize_character_id("<script>") == "eva" and normalize_character_id(None) == "eva"


def test_the_prompt_carries_the_character_its_limits_and_the_emotion():
    tolik = build_system_prompt(search=False, persona=Persona(character="tolik", emotion="normal"), now="x")
    assert "Толік" in tolik
    assert "never nationality, religion, gender, sexuality, disability or illness" in tolik  # Eva's limits are kept
    assert "Only real distress drops the act" in tolik and "You are a man" in tolik and "я зрозумів" in tolik
    assert "Манера мовлення" not in tolik  # «normal»: no emotion layer
    kishka = build_system_prompt(search=False, persona=Persona(character="kishka"), now="x")
    assert "You are Кішка" in kishka and "she-cat" in kishka and "я ж кішка" in kishka and "Кіт" not in kishka
    assert "No mat at all" in kishka and "You are a woman" in kishka and "я зрозуміла" in kishka
    assert "Манера мовлення" in kishka  # the default emotion: emotional
    assert "незмінне" in kishka  # a named character cannot be renamed


def test_the_plain_character_has_no_mask_and_calm_asks_for_calm():
    plain = build_system_prompt(search=False, persona=Persona(character="eva", emotion="calm"), now="x")
    assert "Your character" not in plain and "незмінне" not in plain and "спокійно" in plain
    assert character_prompt(CHARACTERS["eva"]) == ""
    assert set(EMOTIONS) == {"calm", "normal", "emotional"}


NAMED = ("tolik", "kishka", "taras", "milana", "halya", "lina", "vitya", "stepan")


def test_every_named_character_has_three_real_voices_and_the_plain_one_has_none():
    for key in NAMED:
        voices = CHARACTERS[key].voices
        assert len(voices) == 3 and len({v.voice for v in voices}) == 3, key
        assert {v.provider for v in voices} == {"respeecher"} and {v.voice for v in voices} <= RESPEECHER_UK, key
        assert all(v.label for v in voices) and CHARACTERS[key].ukrainian_only and CHARACTERS[key].character, key
    assert CHARACTERS["eva"].voices == ()
    assert {v.voice for v in CHARACTERS["tolik"].voices}.isdisjoint({v.voice for v in CHARACTERS["kishka"].voices})


def test_the_ported_and_the_new_characters_are_found_by_name_and_only_by_whole_words():
    for phrase, key in (("поклич Тараса", "taras"), ("хочу Мілану", "milana"), ("давай бабу Галю", "halya"), ("Ліна", "lina"),
                        ("поговори як Вітя", "vitya"), ("Степан Петрович", "stepan"), ("давай лавеласа", "taras"),
                        ("поклич Степана Петровича", "stepan")):  # fmt: skip
        assert find_character(phrase) == key, phrase
    assert find_character("галявина") is None and find_character("віталій") is None  # not inside another word


def test_taras_is_a_charming_lovelace_with_clear_limits_and_a_mans_grammar():
    p = build_system_prompt(search=False, persona=Persona(character="taras", emotion="normal"), now="x")
    assert "ladies' man" in p and "alpha-male" in p and "You are a man" in p and "я зрозумів" in p
    # the swagger has limits: nothing explicit, no pressure, no put-downs, a «no» is respected, serious things drop it
    for limit in ("nothing explicit or sexual", "no pickup tricks", "no put-downs of anyone", "A «no» is met with a smile and a graceful step back",
                  "drop the swagger completely", "sounds like a child", "without assuming their gender"):  # fmt: skip
        assert limit in p, limit
    assert "Typical requests and how you handle them" in p and "How you sound" in p and "незмінне" in p
    assert CHARACTERS["taras"].feminine is False and "лавелас" in CHARACTERS["taras"].description.lower()


def test_the_characters_ported_from_eva_keep_their_limits():
    prompts = {k: character_prompt(CHARACTERS[k]) for k in NAMED}
    assert "You never flirt with someone who sounds like a child" in prompts["milana"]
    assert "nothing explicit or intimate" in prompts["milana"] and "You are a woman" in prompts["milana"]
    assert "never death, self-harm" in prompts["lina"] and "never about the person's looks" in prompts["lina"]
    assert "a folk tip never replaces real medical advice" in prompts["halya"]
    assert "Вітя" in prompts["vitya"] and "never panicking about real danger" in prompts["vitya"] and "You are a man" in prompts["vitya"]
    assert "на «ви»" in prompts["stepan"] or "«ви»" in prompts["stepan"]
    for key in ("milana", "lina", "vitya"):
        assert "Typical requests and how you handle them" in prompts[key], key
    assert all(CHARACTERS[k].feminine for k in ("kishka", "milana", "halya", "lina"))
    assert not any(CHARACTERS[k].feminine for k in ("tolik", "taras", "vitya", "stepan"))


def test_the_menu_offers_only_voices_the_providers_really_have():
    live = [{"id": "respeecher", "label": "Respeecher", "voices": [{"id": v, "label": v} for v in RESPEECHER_UK]}]
    menu = {c["id"]: c for c in characters_payload(live)}
    assert [c for c in menu] == ["eva", "tolik", "kishka", "taras", "milana", "halya", "lina", "vitya", "stepan"]
    assert [v["voice"] for v in menu["tolik"]["voices"]] == ["dmytro-conversation", "ihoreo-conversation", "yevhen-audiobook"]
    assert [v["label"] for v in menu["kishka"]["voices"]] == ["Роксолана", "Ксенія", "Олеся"] and menu["eva"]["voices"] == []
    assert "хуліган" in menu["tolik"]["description"]
    fewer = [{"id": "respeecher", "label": "R", "voices": [{"id": "dmytro-conversation", "label": "x"}]}]  # a voice went away
    assert [v["voice"] for v in {c["id"]: c for c in characters_payload(fewer)}["tolik"]["voices"]] == ["dmytro-conversation"]
    none = {c["id"]: c for c in characters_payload([])}  # no TTS keys: the characters are still there, without voices
    assert none["tolik"]["voices"] == [] and none["kishka"]["voices"] == []
    soniox_only = [{"id": "soniox", "label": "S", "voices": [{"id": "dmytro-conversation", "label": "x"}]}]  # the right id, the wrong provider
    assert {c["id"]: c for c in characters_payload(soniox_only)}["tolik"]["voices"] == []


# ---------------------------------------------------------------- the session: a voice never changes by itself


def test_switching_character_by_voice_never_changes_the_voice():
    async def go():
        s, out = make()
        old_voice, old_tts = s.voice, s.tts
        await s.start()
        result = await s.run_tool("change_character", {"character": "Толіка"})
        assert "Тепер ти Толік" in result and s.persona.character == "tolik" and s.persona.name == "Толік"
        assert s.voice == old_voice and s.tts is old_tts and not out["made"] and not of(out, "voice")
        p = of(out, "persona")[-1]
        assert p["character"] == "tolik" and p["name"] == "Толік" and "хуліган" in p["description"]
        await s.run_tool("change_character", {"character": "кішку"})
        assert s.persona.character == "kishka" and s.persona.name == "Кішка" and s.tts is old_tts and not out["made"]

    asyncio.run(go())


def test_the_page_selecting_a_character_does_not_change_the_voice_either():
    async def go():
        s, out = make()
        old_tts = s.tts
        await s.start()
        await s.on_client({"type": "style", "character": "tolik", "emotion": "calm"})
        assert s.persona.character == "tolik" and s.persona.emotion == "calm"
        assert s.tts is old_tts and not out["made"] and not of(out, "voice")
        await s.on_client({"type": "style", "character": "<script>", "emotion": "rage"})  # unknown values are ignored
        assert s.persona.character == "tolik" and s.persona.emotion == "calm"
        # the voice changes only when it is asked for, as a voice
        await s.on_client({"type": "voice", "provider": "respeecher", "voice": "kseniia"})
        assert s.voice == "kseniia" and of(out, "voice")[-1]["voice"] == "kseniia"

    asyncio.run(go())


def test_back_to_the_plain_character_restores_its_name_and_still_keeps_the_voice():
    async def go():
        s, out = make()
        old_tts = s.tts
        await s.start()
        await s.run_tool("set_assistant_name", {"name": "Лесі"})
        await s.run_tool("change_character", {"character": "tolik"})
        assert s.persona.name == "Толік"
        result = await s.run_tool("change_character", {"character": "Єва"})
        assert "знову просто" in result and s.persona.name == "Лесі" and s.persona.character == "eva"
        assert s.tts is old_tts and not out["made"]

    asyncio.run(go())


def test_a_character_name_cannot_be_changed_by_voice():
    async def go():
        s, out = make()
        await s.start()
        await s.run_tool("change_character", {"character": "kishka"})
        result = await s.run_tool("set_assistant_name", {"name": "Барсик"})
        assert "не можна змінити" in result and s.persona.name == "Кішка"

    asyncio.run(go())


def test_a_ukrainian_only_character_keeps_the_language_and_pulls_it_back():
    async def go():
        s, out = make(provider="soniox", voice="Daniel", language="en")
        await s.start()
        result = await s.run_tool("change_character", {"character": "tolik"})
        assert "українську" in result and s.persona.language == "uk"
        assert of(out, "language")[-1]["code"] == "uk"
        refusal = await s.run_tool("change_language", {"language": "англійською"})
        assert "говорить лише українською" in refusal and s.persona.language == "uk"
        await s.run_tool("change_character", {"character": "Єва"})
        assert "Мову змінено" in await s.run_tool("change_language", {"language": "en"})

    asyncio.run(go())


def test_unknown_character_and_the_same_one_are_handled():
    async def go():
        s, out = make()
        await s.start()
        assert "немає" in await s.run_tool("change_character", {"character": "Дарт Вейдер"})
        assert "Ти вже Єва" in await s.run_tool("change_character", {"character": "eva"})

    asyncio.run(go())


def test_emotion_by_voice_changes_the_next_prompt():
    async def go():
        llm = FakeLLM(tool_calls=[("change_emotion", {"level": "calm"})])
        s, out = make(llm)
        await s.start()
        await say(s, "Говори спокійніше")
        assert s.persona.emotion == "calm" and "Манера змінена" in llm.last_tool_result
        llm.tool_calls = []
        await s.on_client({"type": "played"})
        await say(s, "Ще раз")
        assert "спокійно" in llm.prompts[-1] and "Манера мовлення" in llm.prompts[-1]
        assert "Рівні емоційності" in await s.run_tool("change_emotion", {"level": "rage"})

    asyncio.run(go())


def test_ready_tells_the_page_the_character_and_the_emotion():
    async def go():
        s, out = make()
        s.persona.character, s.persona.emotion = "kishka", "normal"
        await s.start()
        ready = of(out, "ready")[0]
        assert ready["character"] == "kishka" and ready["emotion"] == "normal"

    asyncio.run(go())
