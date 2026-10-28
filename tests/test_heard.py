"""After a cut-off the agent remembers its answer only up to where the user stopped hearing it."""
import asyncio

from server.heard import heard_part
from server.session import VoiceSession

AUDIO = [("Перше довге речення тут.", 2.0), ("Друге довге речення тут.", 2.0), ("Третє довге речення тут.", 2.0)]


def test_heard_part_cuts_at_the_place_the_audio_stopped():
    assert heard_part(AUDIO, 0.0) == ""  # nothing had sounded
    assert heard_part(AUDIO, 2.0) == "Перше довге речення тут. …"  # exactly a sentence
    assert heard_part(AUDIO, 1.9) == "Перше довге речення тут. …"  # all but a breath counts as heard
    assert heard_part(AUDIO, 3.0) == "Перше довге речення тут. Друге довге …"  # half of the second: half of its words
    assert heard_part(AUDIO, 5.0) == "Перше довге речення тут. Друге довге речення тут. Третє довге …"
    assert heard_part(AUDIO, 1.0) == "Перше довге …"


def test_nothing_to_trim_when_it_had_all_played_or_there_was_no_audio():
    assert heard_part(AUDIO, 6.0) is None and heard_part(AUDIO, 5.95) is None and heard_part(AUDIO, 99.0) is None
    assert heard_part([], 3.0) is None


# ---------------------------------------------------------------------- the session


class Sentences:
    searchable = False

    def __init__(self, gap=0.0):
        self.gap = gap

    async def stream(self, history, ctx=None):
        for text in ("Перше довге речення тут. ", "Друге довге речення тут. ", "Третє довге речення тут."):
            yield text
            await asyncio.sleep(self.gap)


class TwoSecondTTS:
    sample_rate = 24000

    async def connect(self): ...
    async def close(self): ...

    async def synthesize(self, text):
        for _ in range(2):  # 2 s of audio in all: 1 s is 24000 samples of 2 bytes
            yield b"\x00\x01" * 24000


def make(llm, **kw):
    out = []

    async def sj(m):
        out.append(m)

    async def sb(_b):
        pass

    return VoiceSession(llm=llm, tts=TwoSecondTTS(), send_json=sj, send_bytes=sb, **kw), out


def test_a_long_answer_cut_off_after_it_was_all_generated_is_remembered_up_to_where_it_was_heard():
    async def go():
        s, out = make(Sentences())
        await s.start()
        await s.on_heard("розкажи щось", True)
        await asyncio.gather(s._turn, return_exceptions=True)  # she generated all of it; the browser is still playing
        assert s.history[-1]["content"].endswith("Третє довге речення тут.") and s._awaiting_played
        await s.interrupt(reason="sound")
        assert s.history[-1]["content"].endswith("Третє довге речення тут.")  # not yet known how much had played
        await s.on_client({"type": "played_until", "seconds": 3.0})
        assert s.history[-1] == {"role": "assistant", "content": "Перше довге речення тут. Друге довге …"}
        await s.on_client({"type": "played_until", "seconds": 1.0})  # a second report changes nothing
        assert s.history[-1]["content"] == "Перше довге речення тут. Друге довге …"

    asyncio.run(go())


def test_a_cut_off_while_it_is_still_being_generated_is_trimmed_too():
    async def go():
        s, out = make(Sentences(gap=0.05))
        await s.start()
        await s.on_heard("розкажи щось", True)
        for _ in range(200):  # two sentences are sent, the third is still coming
            if len(s._spoken) >= 2:
                break
            await asyncio.sleep(0.005)
        await s.interrupt(reason="takeover", text="стоп")
        assert s.history[-1]["content"].startswith("Перше довге речення тут. Друге довге речення тут.")  # what was sent
        await s.on_client({"type": "played_until", "seconds": 1.0})
        assert s.history[-1] == {"role": "assistant", "content": "Перше довге …"}  # what was heard

    asyncio.run(go())


def test_when_nothing_had_played_the_answer_is_forgotten():
    async def go():
        s, out = make(Sentences())
        await s.start()
        await s.on_heard("розкажи щось", True)
        await asyncio.gather(s._turn, return_exceptions=True)
        await s.interrupt(reason="sound")
        await s.on_client({"type": "played_until", "seconds": 0.0})
        assert all(h["role"] != "assistant" for h in s.history)

    asyncio.run(go())


def test_a_report_without_a_cut_off_or_with_nonsense_is_ignored():
    async def go():
        s, out = make(Sentences())
        await s.start()
        await s.on_client({"type": "played_until", "seconds": 1.0})  # nothing was cut
        await s.on_heard("розкажи щось", True)
        await asyncio.gather(s._turn, return_exceptions=True)
        await s.interrupt(reason="sound")
        for bad in ("2", None, True, [1]):
            await s.on_client({"type": "played_until", "seconds": bad})
        assert s.history[-1]["content"].endswith("Третє довге речення тут.")  # untouched

    asyncio.run(go())


def test_realtime_is_told_where_the_answer_was_cut_and_can_be_told_again():
    from tests.test_realtime import FakeConn, answer_events, make_llm, said

    async def go():
        conn = FakeConn()
        out = []

        async def sj(m):
            out.append(m)

        async def sb(_b):
            pass

        s = VoiceSession(llm=None, ears=make_llm(conn), tts=TwoSecondTTS(), send_json=sj, send_bytes=sb)
        await s.start()
        conn.on_create = lambda _e: answer_events(
            conn, "r1", "Перше довге речення тут. Друге довге речення тут. Третє довге речення тут.", item="msg_1"
        )
        said(conn, "u1")
        for _ in range(60):
            await asyncio.sleep(0)
        await asyncio.gather(s._turn, return_exceptions=True)
        await s.interrupt(reason="sound")
        await s.on_client({"type": "played_until", "seconds": 3.0})
        created = [e["item"] for e in conn.sent if e["type"] == "conversation.item.create" and e["item"].get("id")]
        assert [e["item_id"] for e in conn.sent if e["type"] == "conversation.item.delete"] == ["msg_1"]
        assert created[-1]["id"] == "heard_1" and created[-1]["content"][0]["text"] == "Перше довге речення тут. Друге довге …"
        await s.close()

    asyncio.run(go())


def test_the_prompt_says_to_go_on_from_where_she_was_cut():
    from server.persona import Persona
    from server.prompt import build_system_prompt

    p = build_system_prompt(search=False, persona=Persona(), now="x")
    assert "продовжуй рівно з того місця, де тебе обірвало" in p and "закінчується на «…»" in p
