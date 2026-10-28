"""The tool loop of OpenAILLM with a fake OpenAI client and fake tools: no network."""
import asyncio
from types import SimpleNamespace as NS

from server.config import Config
from server.llm import MAX_SEARCHES_PER_TURN, SEGMENT_END, OpenAILLM, TurnContext


def text_chunk(text):
    return NS(choices=[NS(delta=NS(content=text, tool_calls=None))])


def tool_chunk(index, id_="", name="", args=""):
    fn = NS(name=name, arguments=args)
    return NS(choices=[NS(delta=NS(content=None, tool_calls=[NS(index=index, id=id_, function=fn)]))])


def usage_chunk(prompt, completion):
    return NS(choices=[], usage=NS(prompt_tokens=prompt, completion_tokens=completion))


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


async def collect(llm, ctx=None, history=None):
    ctx = ctx or TurnContext(system_prompt="sys")
    deltas = [d async for d in llm.stream(history or [{"role": "user", "content": "курс долара?"}], ctx)]
    return "".join(deltas).replace(SEGMENT_END, "")


def test_what_was_said_before_a_tool_is_cut_off_from_what_comes_after():
    async def search(query, days):
        return "курс 41"

    client = FakeClient([[text_chunk("Зараз гляну."), tool_chunk(0, "c1", "web_search", '{"query": "курс"}')], [text_chunk("Курс 41.")]])
    llm = OpenAILLM(Config(openai_api_key="k"), client=client, search=search)

    async def run():
        return [d async for d in llm.stream([{"role": "user", "content": "курс?"}], TurnContext(system_prompt="s"))]

    assert asyncio.run(run()) == ["Зараз гляну.", SEGMENT_END, "Курс 41."]


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


def test_no_cue_when_model_already_spoke_and_search_dropped_after_limit():
    searches = []
    call = lambda i: [text_chunk("Зараз перевірю. "), tool_chunk(0, f"c{i}", "web_search", '{"query": "x"}')]  # noqa: E731
    rounds = [call(1), call(2), [text_chunk("Готово.")]]
    llm, client = make(rounds, searches)
    out = asyncio.run(collect(llm))
    assert len(searches) == MAX_SEARCHES_PER_TURN == 2
    assert out.count("Секунду") == 0 and out.endswith("Готово.")

    def names(call):
        return [t["function"]["name"] for t in call.get("tools", [])]

    assert names(client.calls[0]) == names(client.calls[1]) == ["web_search"] and names(client.calls[2]) == []


def test_without_a_key_there_are_no_search_tool():
    client = FakeClient([[text_chunk("Не знаю актуального.")]])
    llm = OpenAILLM(Config(openai_api_key="k", web_search_api_key=""), client=client)
    assert llm.searchable is False
    asyncio.run(collect(llm))
    assert "tools" not in client.calls[0]


def test_session_tools_are_offered_and_run_without_a_cue():
    ran = []

    async def run_tool(name, args):
        ran.append((name, args))
        return "Голос змінено."

    tool = {"type": "function", "function": {"name": "change_voice", "parameters": {"type": "object", "properties": {}}}}
    ctx = TurnContext(system_prompt="sys", extra_tools=[tool], run_tool=run_tool)
    client = FakeClient([[tool_chunk(0, "c1", "change_voice", '{"voice": "Ксенія"}')], [text_chunk("Готово.")]])
    llm = OpenAILLM(Config(openai_api_key="k"), client=client)
    out = asyncio.run(collect(llm, ctx))
    assert ran == [("change_voice", {"voice": "Ксенія"})]
    assert out == "Готово."  # no «Хвилинку, дивлюсь» for a tool that is not a search
    assert [t["function"]["name"] for t in client.calls[0]["tools"]] == ["change_voice"]


def test_usage_is_reported_and_a_runaway_tool_loop_is_cut_off():
    seen = []
    loop = [[tool_chunk(0, f"c{i}", "noop", "{}"), usage_chunk(10, 2)] for i in range(4)] + [[text_chunk("Все."), usage_chunk(10, 2)]]

    async def run_tool(name, args):
        return "ok"

    tool = {"type": "function", "function": {"name": "noop", "parameters": {"type": "object", "properties": {}}}}
    ctx = TurnContext(system_prompt="s", extra_tools=[tool], run_tool=run_tool, on_usage=lambda i, o: seen.append((i, o)))
    client = FakeClient(loop)
    out = asyncio.run(collect(OpenAILLM(Config(openai_api_key="k"), client=client), ctx))
    assert out == "Все." and seen == [(10, 2)] * 5
    assert "tools" in client.calls[3] and "tools" not in client.calls[4]  # after 4 rounds: no tools, it has to answer
