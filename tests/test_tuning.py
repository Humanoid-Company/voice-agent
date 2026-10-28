"""The delivery follows the character and the emotion: the voice's own temperature times a factor. The voice itself
is never touched. Against stubs: the factors are a first guess, found by ear."""
import asyncio

from server import app as app_module
from server.characters import TONE_RANGE, tone_factor
from server.config import Config
from server.tts import RespeecherTTS, SonioxTTS
from tests.test_features import FakeTTS, make, of

BASE = {"olesia": 0.8, "kseniia": 0.5}  # the voices' own temperatures, as the provider lists them


class TunableTTS(FakeTTS):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sampling = None

    def set_sampling(self, params):
        self.sampling = dict(params)
        return True


def tuned(**kw):
    s, out = make(tts_cls=TunableTTS, **kw)
    plain = s._voices_for

    async def voices(p, lang):
        return [{**v, "sampling": {"temperature": BASE[v["id"]]} if v["id"] in BASE else {}} for v in await plain(p, lang)]

    s._voices_for = voices
    return s, out


def last(out):
    return of(out, "tuning")[-1]


def test_the_delivery_starts_tuned_from_the_voices_own_temperature():
    async def go():
        s, out = tuned()
        await s.start()
        assert s.tts.sampling == {"temperature": 1.0}  # 0.8 x 1.25 (the plain character, emotional)
        assert last(out) == {"type": "tuning", "active": True, "reason": "", "base": 0.8, "factor": 1.25, "value": 1.0}

    asyncio.run(go())


def test_a_character_and_an_emotion_move_the_temperature_and_never_the_voice():
    async def go():
        s, out = tuned()
        await s.start()
        voice, tts = s.voice, s.tts
        for character, value in (("tolik", 0.9), ("kishka", 1.08), ("taras", 0.85), ("eva", 1.0)):
            await s.on_client({"type": "style", "character": character})
            assert s.tts.sampling == {"temperature": value} and last(out)["value"] == value, character
        await s.on_client({"type": "style", "emotion": "calm"})
        assert s.tts.sampling == {"temperature": 0.72}
        await s.on_client({"type": "style", "emotion": "normal"})
        assert s.tts.sampling == {"temperature": 0.8}
        await s.on_client({"type": "style", "character": "taras", "emotion": "calm"})
        assert s.tts.sampling == {"temperature": 0.64} and last(out)["factor"] == TONE_RANGE[0]  # kept inside the range
        assert s.voice == voice and s.tts is tts and not out["made"] and not of(out, "voice")

    asyncio.run(go())


def test_the_switch_turns_the_tuning_off_and_back_on():
    async def go():
        s, out = tuned()
        await s.start()
        await s.on_client({"type": "style", "tune": False})
        assert s.tts.sampling == {} and last(out)["active"] is False and last(out)["reason"] == "off"
        await s.on_client({"type": "style", "character": "kishka"})  # off: the character does not move it
        assert s.tts.sampling == {}
        await s.on_client({"type": "style", "tune": True})
        assert s.tts.sampling == {"temperature": 1.08} and last(out)["active"] is True

    asyncio.run(go())


def test_a_new_voice_is_tuned_from_its_own_base_and_one_without_a_base_is_left_alone():
    async def go():
        s, out = tuned()
        await s.start()
        await s.on_client({"type": "voice", "provider": "respeecher", "voice": "kseniia"})
        assert s.tts.voice == "kseniia" and s.tts.sampling == {"temperature": 0.625}  # 0.5 x 1.25
        await s.on_client({"type": "voice", "provider": "respeecher", "voice": "olesia"})
        assert s.tts.sampling == {"temperature": 1.0}
        s._voices_for = None  # the provider listed no defaults
        await s._retune()
        assert s.tts.sampling == {} and last(out)["reason"] == "no_base" and last(out)["active"] is False

    asyncio.run(go())


def test_a_provider_without_such_parameters_says_so():
    async def go():
        s, out = make()  # FakeTTS has no set_sampling: like Soniox
        await s.start()
        assert last(out) == {"type": "tuning", "active": False, "reason": "provider"}

    asyncio.run(go())


def test_the_factor_is_kept_in_a_safe_range_and_unknown_names_are_the_plain_character():
    for character in ("eva", "tolik", "kishka", "taras", "nobody"):
        for emotion in ("calm", "normal", "emotional", "rage"):
            assert TONE_RANGE[0] <= tone_factor(character, emotion) <= TONE_RANGE[1]
    assert tone_factor("eva", "normal") == 1.0 and tone_factor("nobody", "normal") == 1.0
    assert tone_factor("kishka", "normal") > 1.0 > tone_factor("tolik", "normal") > tone_factor("taras", "normal")


def test_respeecher_sends_the_tuning_with_every_sentence_and_soniox_has_none():
    r = RespeecherTTS(Config(respeecher_api_key="k"), "olesia")
    one = r._variants()[0]
    assert one("c", "Привіт.")[0]["voice"]["sampling_params"] == {}
    params = {"temperature": 0.7}
    assert r.set_sampling(params) is True
    params["temperature"] = 9  # the tts keeps its own copy
    assert one("c", "Привіт.")[0]["voice"]["sampling_params"] == {"temperature": 0.7}
    assert SonioxTTS(Config(soniox_api_key="k"), "Daniel").set_sampling({"temperature": 1}) is False


def test_the_page_gets_no_sampling_defaults_and_the_character_menu():
    async def go():
        async def fake(cfg):
            return [{"id": "respeecher", "label": "Respeecher", "voices": [
                {"id": "dmytro-conversation", "label": "Дмитро", "sampling": {"temperature": 0.8}}]}]

        original = app_module.tts_providers
        app_module.tts_providers = fake
        try:
            config = await app_module.api_config()
        finally:
            app_module.tts_providers = original
        assert config["providers"][0]["voices"] == [{"id": "dmytro-conversation", "label": "Дмитро"}]  # no defaults to the page
        assert [c["id"] for c in config["characters"]] == ["eva", "tolik", "kishka", "taras", "lesya", "mariia", "stepan"]
        assert [v["voice"] for v in {c["id"]: c for c in config["characters"]}["tolik"]["voices"]] == ["dmytro-conversation"]

    asyncio.run(go())
