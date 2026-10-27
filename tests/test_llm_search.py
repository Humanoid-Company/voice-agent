"""The tool loop of OpenAILLM with a fake OpenAI client and a fake search: no network."""
import asyncio
from types import SimpleNamespace as NS

from server.config import Config
from server.llm import MAX_SEARCHES_PER_TURN, OpenAILLM


def text_chunk(text):
    return NS(choices=[NS(delta=NS(content=text, tool_calls=None))])


def tool_chunk(index, id_="", name="", args=""):
    fn = NS(name=name, arguments=args)
    return NS(choices=[NS(delta=NS(content=None, tool_calls=[NS(index=index, id=id_, function=fn)]))])


class Stream:
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    def __aiter__(self):
        async def gen():
            for c in self.chunks:
                yield c

        return gen()

    async def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, rounds):
        self.rounds, self.calls = list(rounds), []
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **kw):
        self.calls.append(kw)
        return Stream(self.rounds.pop(0))


async def collect(llm, history=None):
    return "".join([d async for d in llm.stream(history or [{"role": "user", "content": "курс долара?"}])])


def make(rounds, searches):
    async def fake_search(query, days):
        searches.append((query, days))
        return "- НБУ (bank.gov.ua): 41,5 грн"

    client = FakeClient(rounds)
    return OpenAILLM(Config(openai_api_key="k"), client=client, search=fake_search), client


def test_tool_call_runs_search_speaks_a_cue_then_answers():
    searches = []
    rounds = [
        [tool_chunk(0, "call1", "web_search", '{"query": "курс дол'), tool_chunk(0, "", "", 'ара", "recency_days": 1}')],
        [text_chunk("Долар коштує сорок один і п'ять.")],
    ]
    llm, client = make(rounds, searches)
    out = asyncio.run(collect(llm))
    assert searches == [("курс долара", 1)]
    assert out.endswith("Долар коштує сорок один і п'ять.")
    assert out.split(".")[0] in ("Хвилинку, дивлюсь", "Зараз подивлюсь", "Секунду, шукаю")  # the cue came first
    second = client.calls[1]["messages"]
    assert second[-2]["tool_calls"][0]["id"] == "call1" and second[-1]["role"] == "tool"
    assert "41,5" in second[-1]["content"]


def test_no_cue_when_model_already_spoke_and_tools_dropped_after_limit():
    searches = []
    call = lambda i: [text_chunk("Зараз перевірю. "), tool_chunk(0, f"c{i}", "web_search", '{"query": "x"}')]  # noqa: E731
    rounds = [call(1), call(2), [text_chunk("Готово.")]]
    llm, client = make(rounds, searches)
    out = asyncio.run(collect(llm))
    assert len(searches) == MAX_SEARCHES_PER_TURN == 2
    assert out.count("Секунду") == 0 and out.endswith("Готово.")
    assert "tools" in client.calls[0] and "tools" in client.calls[1] and "tools" not in client.calls[2]


def test_without_a_key_there_are_no_tools_and_the_prompt_says_so():
    client = FakeClient([[text_chunk("Не знаю актуального.")]])
    llm = OpenAILLM(Config(openai_api_key="k", web_search_api_key=""), client=client)
    asyncio.run(collect(llm))
    assert "tools" not in client.calls[0]
    assert "немає" in client.calls[0]["messages"][0]["content"]
