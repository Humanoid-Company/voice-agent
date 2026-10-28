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
    assert "Анатолій" in tolik and "Толік" not in tolik  # a full name, not a diminutive
    assert "never nationality, religion, gender, sexuality, disability or illness" in tolik  # Eva's limits are kept
    assert "Only real distress drops the act" in tolik and "You are a man" in tolik and "я зрозумів" in tolik
    assert "Манера мовлення" not in tolik  # «normal»: no emotion layer
    kishka = build_system_prompt(search=False, persona=Persona(character="kishka"), now="x")
    assert "You are Кішка" in kishka and "robot DOG" in kishka and "she-cat" not in kishka and "you never meow, you bark" in kishka
    assert "No mat at all" in kishka and "You are a woman" in kishka and "я зрозуміла" in kishka
    assert "Humanoid and Company" in kishka  # the show for celebrations stays
    assert "Манера мовлення" in kishka  # the default emotion: emotional
    assert "незмінне" in kishka  # a named character cannot be renamed

def test_the_plain_character_has_no_mask_and_calm_asks_for_calm():
    plain = build_system_prompt(search=False, persona=Persona(character="eva", emotion="calm"), now="x")
    assert "Your character" not in plain and "незмінне" not in plain and "спокійно" in plain
    assert character_prompt(CHARACTERS["eva"]) == ""
    assert set(EMOTIONS) == {"calm", "normal", "emotional"}


NAMED = ("tolik", "kishka", "taras", "lesya", "mariia", "stepan")


SONIOX_UK = {"Taras", "Bohdan", "Yana"}  # the Ukrainian voices Soniox offers (server/voices.py)


def test_every_named_character_has_a_voice_per_provider_and_the_plain_one_has_none():
    defaults = set()
    for key in NAMED:
        voices = CHARACTERS[key].voices
        soniox = [v for v in voices if v.provider == "soniox"]
        respeecher = [v for v in voices if v.provider == "respeecher"]
        assert len(soniox) == 1 and soniox[0].voice in SONIOX_UK, key  # Soniox has three Ukrainian voices: shared between them
        assert len(respeecher) == 3 and len({v.voice for v in respeecher}) == 3, key
        assert {v.voice for v in respeecher} <= RESPEECHER_UK, key
        assert all(v.label for v in voices) and CHARACTERS[key].ukrainian_only and CHARACTERS[key].character, key
        assert CHARACTERS[key].voice_for("soniox") == soniox[0] and CHARACTERS[key].voice_for("respeecher") == respeecher[0], key
        assert CHARACTERS[key].voice_for("nobody") is None
        defaults.add(respeecher[0].voice)
    assert len(defaults) == len(NAMED)  # no two characters start on the same Respeecher voice
    assert CHARACTERS["eva"].voices == () and CHARACTERS["eva"].voice_for("soniox") is None
    assert CHARACTERS["taras"].voice_for("soniox").voice == "Taras"  # his own name

def test_the_characters_are_found_by_whole_words():
    assert list(CHARACTERS) == ["eva", "tolik", "kishka", "taras", "lesya", "mariia", "stepan"]
    assert [c.label for c in CHARACTERS.values()] == ["Єва", "Анатолій", "Кішка", "Тарас", "Леся", "Марія", "Степан"]
    for phrase, key in (("поклич Тараса", "taras"), ("давай лавеласа", "taras"), ("поклич кішку", "kishka"),
                        ("хочу робособаку", "kishka"), ("Анатолія", "tolik"), ("Толіка", "tolik"),  # the old name still works
                        ("давай Лесю", "lesya"), ("поклич Марію", "mariia"), ("Степана", "stepan"),
                        ("давай знову як Єва", "eva")):  # fmt: skip
        assert find_character(phrase) == key, phrase
    for phrase in ("галявина", "тарасівка", "поклич Мілану", "давай бабу Галю", "лесник", "степанида"):  # not inside a word
        assert find_character(phrase) is None, phrase

def test_taras_is_a_charming_lovelace_with_clear_limits_and_a_mans_grammar():
    p = build_system_prompt(search=False, persona=Persona(character="taras", emotion="normal"), now="x")
    assert "ladies' man" in p and "alpha-male" in p and "You are a man" in p and "я зрозумів" in p
    # the swagger has limits: nothing explicit, no pressure, no put-downs, a «no» is respected, serious things drop it
    for limit in ("nothing explicit or sexual", "no pickup tricks", "no put-downs of anyone", "A «no» is met with a smile and a graceful step back",
                  "drop the swagger completely", "sounds like a child", "without assuming their gender"):  # fmt: skip
        assert limit in p, limit
    assert "Typical requests and how you handle them" in p and "How you sound" in p and "незмінне" in p
    assert CHARACTERS["taras"].feminine is False and "лавелас" in CHARACTERS["taras"].description.lower()


def test_taras_gets_heated_about_the_invasion_but_never_about_a_people():
    p = build_system_prompt(search=False, persona=Persona(character="taras", emotion="normal"), now="x")
    assert "you never raise it yourself" in p and "the Russian invasion" in p and "lose your cool" in p  # only when it is brought up
    for limit in ("never slurs", "never hatred or contempt for people because of their nationality", "never dehumanising",
                  "never threats or calls for violence", "cool down in a beat"):  # fmt: skip
        assert limit in p, limit
    assert "a night out" in p and "polite and tolerant" in p  # he likes company and women, politely
    assert "агресі" in CHARACTERS["taras"].description

def test_the_menu_offers_only_voices_the_providers_really_have():
    live = [{"id": "respeecher", "label": "Respeecher", "voices": [{"id": v, "label": v} for v in RESPEECHER_UK]}]
    menu = {c["id"]: c for c in characters_payload(live)}
    assert [c for c in menu] == ["eva", "tolik", "kishka", "taras", "lesya", "mariia", "stepan"]
    assert [v["voice"] for v in menu["tolik"]["voices"]] == ["dmytro-conversation", "ihoreo-conversation", "yevhen-audiobook"]
    assert [v["label"] for v in menu["kishka"]["voices"]] == ["Роксолана", "Ксенія", "Олеся"] and menu["eva"]["voices"] == []
    assert "хуліган" in menu["tolik"]["description"] and "робособака" in menu["kishka"]["description"].lower()
    fewer = [{"id": "respeecher", "label": "R", "voices": [{"id": "dmytro-conversation", "label": "x"}]}]  # a voice went away
    assert [v["voice"] for v in {c["id"]: c for c in characters_payload(fewer)}["tolik"]["voices"]] == ["dmytro-conversation"]
    none = {c["id"]: c for c in characters_payload([])}  # no TTS keys: the characters are still there, without voices
    assert none["tolik"]["voices"] == [] and none["kishka"]["voices"] == []
    soniox_only = [{"id": "soniox", "label": "S", "voices": [{"id": "dmytro-conversation", "label": "x"}]}]  # the right id, the wrong provider
    assert {c["id"]: c for c in characters_payload(soniox_only)}["tolik"]["voices"] == []

# ---------------------------------------------------------------- the session: a character brings its voice on the selected provider


def _voices(monkeypatch, without=()):
    """The voices the fake provider lists: the real ids the characters use."""
    import tests.test_features as tf

    monkeypatch.setitem(tf.VOICES, ("respeecher", "uk"), [{"id": v, "label": v} for v in sorted(RESPEECHER_UK)])
    monkeypatch.setitem(tf.VOICES, ("soniox", "uk"), [{"id": v, "label": v} for v in ("Daniel", "Taras", "Bohdan", "Yana") if v not in without])


def test_a_character_brings_its_voice_on_the_provider_that_speaks_and_the_plain_one_leaves_it(monkeypatch):
    async def go():
        _voices(monkeypatch)
        s, out = make()  # Respeecher, olesia
        await s.start()
        result = await s.run_tool("change_character", {"character": "Анатолія"})
        assert "Тепер ти Анатолій" in result and s.persona.character == "tolik" and s.persona.name == "Анатолій"
        assert "голос" in result and (s.provider, s.voice) == ("respeecher", "dmytro-conversation")
        p = of(out, "persona")[-1]
        assert p["character"] == "tolik" and p["name"] == "Анатолій" and "хуліган" in p["description"]
        assert of(out, "voice")[-1]["voice"] == "dmytro-conversation"
        await s.run_tool("change_character", {"character": "кішку"})
        assert s.persona.name == "Кішка" and (s.provider, s.voice) == ("respeecher", "roksolana-conversation")
        await s.run_tool("change_character", {"character": "Єва"})  # the plain character has no voice: it is left as it is
        assert s.persona.character == "eva" and (s.provider, s.voice) == ("respeecher", "roksolana-conversation")

    asyncio.run(go())


def test_on_soniox_the_same_characters_bring_soniox_voices_and_the_provider_never_changes(monkeypatch):
    async def go():
        _voices(monkeypatch)
        s, out = make(provider="soniox", voice="Daniel")
        await s.start()
        for who, voice in (("Тараса", "Taras"), ("Лесю", "Yana"), ("Степана", "Bohdan"), ("Марію", "Yana"), ("Анатолія", "Bohdan")):
            await s.run_tool("change_character", {"character": who})
            assert (s.provider, s.voice) == ("soniox", voice), who  # one character, the voice follows the provider

    asyncio.run(go())


def test_the_pages_menu_does_it_too_and_a_voice_picked_by_hand_stays_until_the_next_character(monkeypatch):
    async def go():
        _voices(monkeypatch)
        s, out = make()
        await s.start()
        await s.on_client({"type": "style", "character": "stepan", "emotion": "calm"})
        assert s.persona.character == "stepan" and s.persona.emotion == "calm" and s.voice == "ihoreo-media"
        await s.on_client({"type": "style", "character": "<script>", "emotion": "rage"})  # unknown values are ignored
        assert s.persona.character == "stepan" and s.persona.emotion == "calm" and s.voice == "ihoreo-media"
        await s.on_client({"type": "voice", "provider": "respeecher", "voice": "kseniia-conversation"})  # the user's own pick
        assert s.voice == "kseniia-conversation"
        await s.on_client({"type": "style", "character": "stepan", "emotion": "normal"})  # the same character again: nothing is taken back
        assert s.voice == "kseniia-conversation"
        await s.on_client({"type": "style", "character": "taras"})  # a new character: its voice
        assert s.voice == "oleksandr-radio"

    asyncio.run(go())


def test_a_voice_the_provider_does_not_have_or_that_already_speaks_is_left_alone(monkeypatch):
    async def go():
        _voices(monkeypatch, without=("Bohdan",))  # Soniox without «Bohdan»
        s, out = make(provider="soniox", voice="Daniel")
        await s.start()
        made = len(out["made"])
        result = await s.run_tool("change_character", {"character": "Анатолія"})
        assert s.persona.character == "tolik" and s.voice == "Daniel" and "голос" not in result and len(out["made"]) == made
        await s.run_tool("change_character", {"character": "Тараса"})
        assert s.voice == "Taras"
        made = len(out["made"])
        result = await s.run_tool("change_character", {"character": "Лесю"})  # Yana
        again = await s.run_tool("change_character", {"character": "Марію"})  # Yana already speaks: nothing to switch
        assert s.voice == "Yana" and "голос" in result and "голос" not in again and len(out["made"]) == made + 1

    asyncio.run(go())


def test_back_to_the_plain_character_restores_its_name_and_still_keeps_the_voice():
    async def go():
        s, out = make()  # the fake provider lists only olesia and kseniia: no voice of the characters is there, so none is switched
        old_tts = s.tts
        await s.start()
        await s.run_tool("set_assistant_name", {"name": "Лесі"})
        await s.run_tool("change_character", {"character": "tolik"})
        assert s.persona.name == "Анатолій"
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


def test_every_character_gets_eva_s_common_rules_but_only_the_plain_one_is_friendly():
    common = ("Як іде розмова", "Допомагаєш мовою, а не текстом", "Не вгадуй її стать", "Результат роботи інструмента",
              "Ти нічого не читаєш і не гортаєш сама")  # fmt: skip
    for key in CHARACTERS:
        p = build_system_prompt(search=True, persona=Persona(character=key), now="x")
        for rule in common:
            assert rule in p, (key, rule)
        assert ("Твоя манера: тепла й приязна" in p) == (key == "eva"), key  # a named character brings its own manner
        assert "дві-три найсвіжіші події" in p  # news with substance, not a vague review


def test_war_is_not_raised_unasked_and_general_news_means_interesting_world_news():
    for key in CHARACTERS:
        p = build_system_prompt(search=True, persona=Persona(character=key), now="x")
        assert "Війну (бойові дії, обстріли, жертви, руйнування) сама не зачіпай" in p, key
        assert "цікаві світові новини" in p and "пропускай воєнні" in p and "поки людина не спитала саме про війну" in p, key
    # also without a search tool the war rule holds (it is in the common part)
    assert "Війну (бойові дії" in build_system_prompt(search=False, persona=Persona(), now="x")


# ---------------------------------------------------------------- Тарас and his own voice


def _with_taras(monkeypatch):
    import tests.test_features as tf

    monkeypatch.setitem(tf.VOICES, ("soniox", "uk"), [{"id": "Daniel", "label": "Daniel (male)"}, {"id": "Taras", "label": "Taras (male)"}])


def test_lesya_defends_her_opinion_mariia_sees_in_colours_and_stepan_sings_and_jokes():
    def prompt(key):
        return build_system_prompt(search=False, persona=Persona(character=key, emotion="normal"), now="x")

    lesya = prompt("lesya")
    assert "Lesya Ukrainka" in lesya and "you are not her" in lesya and "first woman-robot" in lesya
    assert "defend them with arguments" in lesya and "you argue, you never bully" in lesya and "never contemptuous of people" in lesya
    assert "You are a woman" in lesya and "я зрозуміла" in lesya and "never claim to be a real person" in lesya
    mariia = prompt("mariia")
    assert "Maria Prymachenko" in mariia and "you are not her" in mariia and "colours" in mariia
    assert "never «погано»" in mariia and "never copy a living artist's style by name" in mariia and "You are a woman" in mariia
    stepan = prompt("stepan")
    assert "singing joker" in stepan and "patriot" in stepan and "never hateful" in stepan and "never an insult to any people" in stepan
    assert "You are a man" in stepan and "я зрозумів" in stepan
    assert "never a whole song, never someone's modern lyrics" in stepan  # no lyrics of living authors
    assert "he is not him" not in stepan and "Бандер" not in stepan  # a friendly nod to an entertainer, no politics of a historical figure
    for key in ("lesya", "mariia", "stepan"):
        c = CHARACTERS[key]
        assert c.ukrainian_only and c.character and c.samples and c.situations and "незмінне" in prompt(key)


def test_a_character_tells_its_voice_per_provider_to_the_page():
    both = [
        {"id": "soniox", "label": "S", "voices": [{"id": "Taras", "label": "x"}, {"id": "Bohdan", "label": "x"}, {"id": "Yana", "label": "x"}]},
        {"id": "respeecher", "label": "R", "voices": [{"id": v, "label": v} for v in RESPEECHER_UK]},
    ]
    menu = {c["id"]: c for c in characters_payload(both)}
    assert menu["taras"]["voice_for"] == {"soniox": "Taras", "respeecher": "oleksandr-radio"}
    assert menu["lesya"]["voice_for"] == {"soniox": "Yana", "respeecher": "kseniia-conversation"}
    assert menu["stepan"]["voice_for"] == {"soniox": "Bohdan", "respeecher": "ihoreo-media"}
    assert menu["eva"]["voice_for"] == {}
    only_respeecher = {c["id"]: c for c in characters_payload(both[1:])}
    assert only_respeecher["taras"]["voice_for"] == {"respeecher": "oleksandr-radio"}  # a provider without keys: nothing for it
    one_missing = [{"id": "respeecher", "label": "R", "voices": [{"id": "yevhen-audiobook", "label": "x"}]}]
    assert {c["id"]: c for c in characters_payload(one_missing)}["tolik"]["voice_for"] == {"respeecher": "yevhen-audiobook"}  # the first that is there
    assert {c["id"]: c for c in characters_payload([])}["taras"]["voice_for"] == {}
