"""The voice sample behind «▶ Прослухати»: made once per voice and language, only for voices the provider lists."""
import asyncio
import io
import wave

import pytest

from server.config import Config
from server.preview import PHRASES, PreviewError, VoicePreviews, pcm_to_wav, phrase_language
from server.tts import TTSError

CFG = Config()


class FakeTTS:
    sample_rate = 24000

    def __init__(self, audio=b"\x01\x00" * 480, fail=False):
        self.audio, self.fail, self.said, self.closed = audio, fail, [], False

    async def connect(self): ...

    async def close(self):
        self.closed = True

    async def synthesize(self, text):
        self.said.append(text)
        if self.fail:
            raise RuntimeError("no audio")
        for i in range(0, len(self.audio), 320):
            yield self.audio[i : i + 320]


async def listed(_cfg, _provider, _language):
    return [{"id": "olesia", "label": "Олеся"}]


def run(previews, voice="olesia", provider="respeecher", language="uk", tts=None, make=None):
    made = []

    def make_tts(p, v, cfg, lang):
        made.append((p, v, lang))
        return make(p, v, cfg, lang) if make else tts

    wav = asyncio.run(previews.get(CFG, provider, voice, language, make_tts=make_tts, list_voices=listed))
    return wav, made


def test_the_sample_is_a_playable_wav_of_the_fixed_phrase():
    tts = FakeTTS()
    wav, made = run(VoicePreviews(), tts=tts)
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 24000, 480)
    assert tts.said == [PHRASES["uk"]] and tts.closed and made == [("respeecher", "olesia", "uk")]


def test_a_voice_is_synthesized_once_however_many_clicks():
    previews, tts = VoicePreviews(), FakeTTS()
    run(previews, tts=tts)
    wav, made = run(previews, tts=tts)
    assert len(tts.said) == 1 and made == [] and wav  # the second click came from memory


def test_one_synthesis_even_when_the_clicks_come_at_once():
    previews, tts = VoicePreviews(), FakeTTS()

    async def go():
        async def listing(*a):
            await asyncio.sleep(0.01)
            return await listed(*a)

        return await asyncio.gather(
            *(previews.get(CFG, "respeecher", "olesia", "uk", make_tts=lambda *a: tts, list_voices=listing) for _ in range(5))
        )

    out = asyncio.run(go())
    assert len(tts.said) == 1 and len({bytes(o) for o in out}) == 1


def test_an_unlisted_voice_is_refused_before_any_paid_synthesis():
    tts = FakeTTS()
    with pytest.raises(PreviewError) as e:
        run(VoicePreviews(), voice="not-a-voice", tts=tts)
    assert e.value.status == 404 and tts.said == []


def test_a_provider_without_a_key_and_a_silent_or_broken_voice_are_errors():
    def nokey(*a):
        raise TTSError("TTS provider 'soniox' is not configured")

    with pytest.raises(PreviewError) as e:
        run(VoicePreviews(), make=nokey)
    assert e.value.status == 400
    with pytest.raises(PreviewError) as e:
        run(VoicePreviews(), tts=FakeTTS(audio=b""))
    assert e.value.status == 502
    broken = FakeTTS(fail=True)
    with pytest.raises(PreviewError) as e:
        run(VoicePreviews(), tts=broken)
    assert e.value.status == 502 and broken.closed
    # a failure is not kept: the next try synthesizes again
    previews = VoicePreviews()
    with pytest.raises(PreviewError):
        run(previews, tts=FakeTTS(fail=True))
    assert run(previews, tts=FakeTTS())[0]


def test_the_phrase_follows_the_language_the_provider_speaks():
    assert phrase_language("respeecher", "uk") == "uk" and phrase_language("respeecher", "en") == "en"
    assert phrase_language("respeecher", "de") == "en"  # no German phrase: English
    assert phrase_language("soniox", "uk") == "uk" and phrase_language("soniox", "fr") == "en"


def test_pcm_to_wav_header():
    wav = pcm_to_wav(b"\x00\x00" * 10, 16000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
