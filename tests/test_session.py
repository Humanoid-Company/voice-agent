"""The turn loop and barge-in against stubs: no network, no keys."""
import asyncio

from server.session import VoiceSession


class FakeLLM:
    def __init__(self, answer="Перше речення тут. Друге речення тут. Третє речення тут."):
        self.answer, self.calls = answer, []

    async def stream(self, history):
        self.calls.append(list(history))
        for word in self.answer.split(" "):
            await asyncio.sleep(0)
            yield word + " "


class FakeTTS:
    sample_rate = 24000

    def __init__(self, delay=0.0):
        self.delay, self.said, self.cancelled = delay, [], 0

    async def connect(self): ...
    async def close(self): ...

    async def synthesize(self, text):
        try:
            for _ in range(3):
                await asyncio.sleep(self.delay)
                yield b"\x00\x01" * 10
            self.said.append(text)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def make(llm=None, tts=None):
    out = {"json": [], "bytes": []}

    async def sj(m):
        out["json"].append(m)

    async def sb(b):
        out["bytes"].append(b)

    s = VoiceSession(llm=llm or FakeLLM(), tts=tts or FakeTTS(), send_json=sj, send_bytes=sb)
    return s, out


def types(out):
    return [m["type"] for m in out["json"]]


async def say(s, text):
    await s.on_heard(text, True)


async def settle(s):
    if s._turn:
        await asyncio.gather(s._turn, return_exceptions=True)


def test_full_turn_then_played_returns_to_listening():
    async def go():
        s, out = make()
        await s.start()
        await say(s, "Привіт, розкажи щось")
        await settle(s)
        assert out["bytes"] and "turn_end" in types(out)
        assert s.state == "speaking"  # audio may still play in the browser
        await s.on_client({"type": "played"})
        assert s.state == "listening"
        assert [m["role"] for m in s.history] == ["user", "assistant"]

    asyncio.run(go())


def test_takeover_cancels_turn_and_answers_new_utterance():
    async def go():
        tts = FakeTTS(delay=0.01)
        llm = FakeLLM()
        s, out = make(llm, tts)
        await s.start()
        await say(s, "Розкажи довгу історію")
        await asyncio.sleep(0.015)  # speaking now
        assert s.state == "speaking"
        await s.on_heard("Ні, краще скажи мені про погоду", False)
        assert "clear" in types(out) and s.state == "listening"
        assert tts.cancelled >= 1
        await s.on_heard("Ні, краще скажи мені про погоду", True)
        await settle(s)
        assert len(llm.calls) == 2
        assert llm.calls[1][-1] == {"role": "user", "content": "Ні, краще скажи мені про погоду"}
        # the interrupted turn is kept in history only as far as it was spoken
        assert any(m["role"] == "assistant" and m["content"].endswith("…") for m in llm.calls[1]) or llm.calls[1][-2]["role"] == "user"

    asyncio.run(go())


def test_backchannel_is_ignored_and_not_answered():
    async def go():
        tts = FakeTTS(delay=0.01)
        llm = FakeLLM()
        s, out = make(llm, tts)
        await s.start()
        await say(s, "Розкажи довгу історію")
        await asyncio.sleep(0.015)
        await s.on_heard("угу", False)
        await s.on_heard("угу", True)
        assert "clear" not in types(out) and "user_drop" in types(out)
        await settle(s)
        assert len(llm.calls) == 1 and tts.cancelled == 0

    asyncio.run(go())


def test_stop_word_interrupts_and_gets_no_reply():
    async def go():
        tts = FakeTTS(delay=0.01)
        llm = FakeLLM()
        s, out = make(llm, tts)
        await s.start()
        await say(s, "Розкажи довгу історію")
        await asyncio.sleep(0.015)
        await s.on_heard("стоп", False)
        await s.on_heard("стоп", True)
        await asyncio.sleep(0.05)
        assert "clear" in types(out) and s.state == "listening"
        assert len(llm.calls) == 1

    asyncio.run(go())


def test_assistants_own_voice_through_mic_is_not_a_turn():
    async def go():
        llm = FakeLLM()
        s, out = make(llm)
        await s.start()
        await say(s, "Розкажи щось")
        await settle(s)
        await s.on_client({"type": "played"})
        # right after playback the tail of her own words comes back through the mic
        await say(s, "Друге речення тут третє речення тут")
        assert len(llm.calls) == 1 and "user_drop" in types(out)

    asyncio.run(go())


def test_llm_error_reports_and_recovers():
    class Boom(FakeLLM):
        async def stream(self, history):
            raise RuntimeError("down")
            yield ""

    async def go():
        s, out = make(Boom())
        await s.start()
        await say(s, "Привіт, як справи")
        await settle(s)
        assert "error" in types(out) and s.state == "listening"

    asyncio.run(go())


def test_start_tells_the_page_it_is_listening():
    async def go():
        s, out = make()
        await s.start()
        assert types(out)[:2] == ["ready", "state"] and out["json"][1]["state"] == "listening"

    asyncio.run(go())


def test_her_echo_glued_to_the_users_words_is_not_sent_to_the_model():
    async def go():
        llm = FakeLLM(answer="У мене все добре, дякую, що спитав. А як у тебе справи?")
        s, out = make(llm)
        await s.start()
        await say(s, "Розкажи як справи")
        await settle(s)
        await s.on_client({"type": "played"})
        await s.on_heard("дякую що спитав А як у тебе справи Нормально що порадиш мені зробити", True)
        await settle(s)
        assert llm.calls[1][-1] == {"role": "user", "content": "Нормально що порадиш мені зробити"}

    asyncio.run(go())
