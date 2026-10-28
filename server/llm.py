"""Streaming text completion with tools: `web_search` here, the session's own tools (voice, name, language,
end of call) through the turn context. The text deltas feed the sentence splitter and TTS."""
from __future__ import annotations

import json
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import AsyncOpenAI

from . import web_search as ws
from .config import Config

log = logging.getLogger(__name__)

_MAX_HISTORY = 30
MAX_SEARCHES_PER_TURN = 2
MAX_TOOL_ROUNDS = 4  # model -> tools -> model ...: a model that keeps calling tools is cut off
# Said aloud while the search runs, unless the model already said something before calling the tool.
_CUES = ("Хвилинку, дивлюсь.", "Зараз подивлюсь.", "Секунду, шукаю.")
# Yielded by `stream()` where the text so far must be spoken NOW: right before a tool runs. The splitter would
# otherwise hold «Зараз гляну новини.» (no word after it, or shorter than a sentence) until the search is over,
# and it would be said after the result, glued to it.
SEGMENT_END = "\u2029"
# The answer to a search asked for after the limit (a model may ask several at once): not run, not billed.
SEARCH_LIMIT_NOTE = "Ліміт пошуку на це питання вичерпано: відповідай з того, що вже знайдено або знаєш."

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the public web for what changes or is too recent to know: news, exchange rates, weather, "
            "schedules, match scores, recent releases, prices, what a company or a person is doing now, software "
            "versions. Not for history, geography, science, famous people's lives, definitions, how things work, "
            "advice, translation or writing: answer those yourself. The query names exactly what was asked about "
            "(a person: the full name), in the language that finds it best. A question with two parts gets both "
            "queries at once. Search again only if the results lack the answer itself, never for more detail. "
            "recency_days limits results to roughly the last N days (1 for «сьогодні»)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "recency_days": {"type": "integer", "description": "Only the last N days"},
                "topic": {
                    "type": "string",
                    "enum": list(ws.TOPICS),
                    "description": "news for news and current events (with dates); finance for prices and rates; else general",
                },
            },
            "required": ["query"],
        },
    },
}

SearchFn = Callable[[str, int | None, str | None], Awaitable[str]]  # query, recency_days, topic


@dataclass
class TurnContext:
    """Everything one answer needs from the session: its prompt, its tools, and where to report usage."""

    system_prompt: str
    extra_tools: list[dict] = field(default_factory=list)
    run_tool: Callable[[str, dict], Awaitable[str]] | None = None
    on_usage: Callable[[int, int], None] | None = None  # prompt tokens, completion tokens
    on_search: Callable[[], None] | None = None
    on_audio: Callable[[int], None] | None = None  # audio input tokens (Realtime only)


class LLM(Protocol):
    searchable: bool

    def stream(self, history: list[dict], ctx: TurnContext) -> AsyncIterator[str]: ...


class OpenAILLM:
    def __init__(self, cfg: Config, *, client: Any = None, search: SearchFn | None = None) -> None:
        self._client = client or AsyncOpenAI(api_key=cfg.openai_api_key)
        self._model = cfg.openai_model
        self.searchable = bool(search or cfg.web_search_api_key)
        self._timeout = cfg.web_search_timeout_s

        async def default_search(query: str, recency_days: int | None, topic: str | None = None) -> str:
            return await ws.web_search(
                query, api_key=cfg.web_search_api_key, recency_days=recency_days, topic=topic, timeout_s=self._timeout
            )

        self._search: SearchFn = search or default_search

    async def stream(self, history: list[dict], ctx: TurnContext) -> AsyncIterator[str]:
        messages: list[dict] = [{"role": "system", "content": ctx.system_prompt}, *history[-_MAX_HISTORY:]]
        searches = rounds = 0
        while True:
            tools = list(ctx.extra_tools)
            if self.searchable and searches < MAX_SEARCHES_PER_TURN:
                tools.insert(0, WEB_SEARCH_TOOL)
            kwargs: dict = {
                "model": self._model,
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if tools and rounds < MAX_TOOL_ROUNDS:
                kwargs["tools"] = tools
            resp = await self._client.chat.completions.create(**kwargs)
            text, calls = "", {}
            try:
                async for chunk in resp:
                    usage = getattr(chunk, "usage", None)
                    if usage and ctx.on_usage:
                        ctx.on_usage(usage.prompt_tokens or 0, usage.completion_tokens or 0)
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        text += delta.content
                        yield delta.content
                    for tc in delta.tool_calls or []:
                        c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                        c["id"] = tc.id or c["id"]
                        if tc.function:
                            c["name"] += tc.function.name or ""
                            c["args"] += tc.function.arguments or ""
            finally:
                await resp.close()  # a cancelled turn must stop the generation (and the bill)
            if not calls:
                return
            rounds += 1
            if not text.strip() and any(c["name"] == "web_search" for c in calls.values()):
                yield random.choice(_CUES) + " "  # the answer's first words, spoken while we search
            yield SEGMENT_END
            messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"]}}
                        for c in calls.values()
                    ],
                }
            )
            for c in calls.values():
                if c["name"] == "web_search" and searches >= MAX_SEARCHES_PER_TURN:
                    output = SEARCH_LIMIT_NOTE  # several searches asked for at once: only the first ones run
                else:
                    if c["name"] == "web_search":
                        searches += 1
                        if ctx.on_search:
                            ctx.on_search()
                    output = await run_tool_call(c["name"], c["args"], ctx, self._search)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": output})


async def run_tool_call(name: str, raw_args: str, ctx: TurnContext, search: SearchFn) -> str:
    """One tool call as text for the model. A tool that breaks is reported to the model, not raised: one broken
    tool must not end the whole answer."""
    try:
        args = json.loads(raw_args or "{}")
    except ValueError:
        return "Некоректні аргументи інструмента."
    if not isinstance(args, dict):
        return "Некоректні аргументи інструмента."
    try:
        if name == "web_search":
            days, topic = args.get("recency_days"), args.get("topic")
            return await search(
                str(args.get("query") or ""), days if isinstance(days, int) else None, topic if topic in ws.TOPICS else None
            )
        if ctx.run_tool:
            return await ctx.run_tool(name, args)
    except Exception as e:
        log.exception("tool %s failed", name)
        return f"Інструмент не спрацював ({type(e).__name__}). Скажи про це коротко."
    return "Невідомий інструмент."
