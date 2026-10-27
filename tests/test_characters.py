"""Characters (Толік, Кіт) and the emotion level: the prompt, the voice that fits, the name and the language rules."""
import asyncio

from server.characters import CHARACTERS, EMOTIONS, character_prompt, find_character, first_voice_of, voice_gender
from server.persona import Persona
from server.prompt import build_system_prompt
from tests.test_features import VOICES, Config, FakeLLM, FakeTTS, VoiceSession, make, of, say, types  # noqa: F401

VOICES_G = [
    {"id": "olesia", "label": "Олеся (female, ukrainian)"},
    {"id": "taras", "label": "Тарас (male, ukrainian)"},
    {"id": "plain", "label": "plain"},
]


def test_a_character_is_found_by_the_way_people_call_it():
    assert find_character("поклич Толіка") == "tolik" and find_character("Толік") == "tolik"
    assert find_character("хочу поговорити з котом") == "kit" and find_character("котик") == "kit"
    assert find_character("давай знову як Єва") == "eva" and find_character("tolik") == "tolik"
    assert find_character("Гарольд") is None and find_character("") is None


def test_the_prompt_carries_the_character_its_limits_and_the_emotion():
    tolik = build_system_prompt(search=False, persona=Persona(character="tolik", emotion="normal"), now="x")
    assert "Толік" in tolik and "never\nnationality" not in tolik  # (the limits are one long line, below)
    assert "never nationality, religion, gender, sexuality, disability or illness" in tolik  # Eva's limits are kept
    assert "Only real distress drops the act" in tolik and "You are a man" in tolik and "я зрозумів" in tolik
    assert "Манера мовлення" not in tolik  # «normal»: no emotion layer
    kit = build_system_prompt(search=False, persona=Persona(character="kit"), now="x")
    assert "No mat at all" in kit and "You are a woman" in kit and "Манера мовлення" in kit  # default: emotional
    assert "незмінне" in kit  # a named character cannot be renamed


def test_the_plain_character_has_no_mask_and_calm_asks_for_calm():
    plain = build_system_prompt(search=False, persona=Persona(character="eva", emotion="calm"), now="x")
    assert "Your character" not in plain and "незмінне" not in plain and "спокійно" in plain
    assert character_prompt(CHARACTERS["eva"]) == ""
    assert set(EMOTIONS) == {"calm", "normal", "emotional"}


def test_voice_gender_is_read_from_the_label():
    assert voice_gender(VOICES_G, "olesia") == "female" and voice_gender(VOICES_G, "taras") == "male"
    assert voice_gender(VOICES_G, "plain") == "" and voice_gender(VOICES_G, "nope") == ""
    assert first_voice_of(VOICES_G, "male")["id"] == "taras" and first_voice_of(VOICES_G, "female")["id"] == "olesia"


# ---------------------------------------------------------------- the session

MALE = {"id": "taras", "label": "Тарас (male)"}


def with_male(llm=None, **kw):
    s, out = make(llm, **kw)
    voices_for = s._voices_for

    async def voices(p, lang):
        base = await voices_for(p, lang)
        return base + ([MALE] if p == "respeecher" and lang == "uk" else [])

    s._voices_for = voices
    return s, out


def test_switching_to_tolik_renames_picks_a_male_voice_and_tells_the_page():
    async def go():
        s, out = with_male()
        await s.start()
        result = await s.run_tool("change_character", {"character": "Толіка"})
        assert "Тепер ти Толік" in result
        assert s.persona.character == "tolik" and s.persona.name == "Толік"
        # the voice was a woman's: a man's replaces it, at once (idle)
        assert s.voice == "taras" and out["made"][-1].voice == "taras"
        p = of(out, "persona")[-1]
        assert p["character"] == "tolik" and p["name"] == "Толік" and "хуліган" in p["description"]

    asyncio.run(go())


def test_a_voice_the_user_picked_that_already_fits_stays():
    async def go():
        s, out = with_male(provider="respeecher", voice="taras")
        await s.start()
        await s.run_tool("change_character", {"character": "tolik"})
        assert s.voice == "taras" and not out["made"]  # already a man's voice: nothing replaced

    asyncio.run(go())


def test_back_to_the_plain_character_restores_the_name_and_a_womans_voice():
    async def go():
        s, out = with_male()
        await s.start()
        await s.run_tool("set_assistant_name", {"name": "Лесі"})
        await s.run_tool("change_character", {"character": "tolik"})
        assert s.persona.name == "Толік"
        result = await s.run_tool("change_character", {"character": "Єва"})
        assert "знову просто" in result and s.persona.name == "Лесі" and s.persona.character == "eva"
        assert voice_gender(await s._voices_for("respeecher", "uk"), s.voice) == "female"

    asyncio.run(go())


def test_a_character_name_cannot_be_changed_by_voice():
    async def go():
        s, out = with_male()
        await s.start()
        await s.run_tool("change_character", {"character": "kit"})
        result = await s.run_tool("set_assistant_name", {"name": "Барсик"})
        assert "не можна змінити" in result and s.persona.name == "Кіт"

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
        s, out = with_male()
        await s.start()
        assert "немає" in await s.run_tool("change_character", {"character": "Дарт Вейдер"})
        assert "Ти вже Єва" in await s.run_tool("change_character", {"character": "eva"})

    asyncio.run(go())


def test_the_pages_selectors_change_the_character_and_the_emotion():
    async def go():
        s, out = with_male()
        await s.start()
        await s.on_client({"type": "style", "character": "kit", "emotion": "calm"})
        assert s.persona.character == "kit" and s.persona.emotion == "calm"
        p = of(out, "persona")[-1]
        assert p["character"] == "kit" and p["emotion"] == "calm"
        await s.on_client({"type": "style", "character": "<script>", "emotion": "rage"})  # unknown values are ignored
        assert s.persona.character == "kit" and s.persona.emotion == "calm"

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
        s.persona.character, s.persona.emotion = "kit", "normal"
        await s.start()
        ready = of(out, "ready")[0]
        assert ready["character"] == "kit" and ready["emotion"] == "normal"

    asyncio.run(go())
