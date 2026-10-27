"""Streaming text completion with a `web_search` tool. The text deltas feed the sentence splitter and TTS."""
from __future__ import annotations

import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from openai import AsyncOpenAI

from . import web_search as ws
from .config import Config
from .prompt import build_system_prompt

_MAX_HISTORY = 30
MAX_SEARCHES_PER_TURN = 2
# Said aloud while the search runs, unless the model already said something before calling the tool.
_CUES = ("Хвилинку, дивлюсь.", "Зараз подивлюсь.", "Секунду, шукаю.")

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the public web for current or externally verifiable information: news, exchange rates, "
                "weather, schedules, match scores, recent releases, prices, anything that may have changed. "
                "Do not use for general knowledge, advice, translation or writing. "
                "recency_days limits results to roughly the last N days (1 for «сьогодні»)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "recency_days": {"type": "integer", "description": "Only the last N days"},
                },
                "required": ["query"],
            },
        },
    }
]

SearchFn = Callable[[str, int | None], Awaitable[str]]


class LLM(Protocol):
    def stream(self, history: list[dict]) -> AsyncIterator[str]: ...


class OpenAILLM:
    def __init__(self, cfg: Config, *, client: Any = None, search: SearchFn | None = None) -> None:
        self._client = client or AsyncOpenAI(api_key=cfg.openai_api_key)
        self._model = cfg.openai_model
        self._searchable = bool(search or cfg.web_search_api_key)
        self._prompt = build_system_prompt(search=self._searchable)
        self._timeout = cfg.web_search_timeout_s

        async def default_search(query: str, recency_days: int | None) -> str:
            return await ws.web_search(
                query, api_key=cfg.web_search_api_key, recency_days=recency_days, timeout_s=self._timeout
            )

        self._search: SearchFn = search or default_search

    async def stream(self, history: list[dict]) -> AsyncIterator[str]:
        messages: list[dict] = [{"role": "system", "content": self._prompt}, *history[-_MAX_HISTORY:]]
        searches = 0
        while True:
            kwargs: dict = {"model": self._model, "messages": messages, "stream": True}
            if self._searchable and searches < MAX_SEARCHES_PER_TURN:
                kwargs["tools"] = _TOOLS
            resp = await self._client.chat.completions.create(**kwargs)
            text, calls = "", {}
            try:
                async for chunk in resp:
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
            if not text.strip():
                yield random.choice(_CUES) + " "  # the answer's first words, spoken while we search
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
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": await self._run_tool(c)})
                searches += 1

    async def _run_tool(self, call: dict) -> str:
        if call["name"] != "web_search":
            return "Невідомий інструмент."
        try:
            args = json.loads(call["args"] or "{}")
        except ValueError:
            return "Некоректні аргументи пошуку."
        days = args.get("recency_days")
        return await self._search(str(args.get("query") or ""), days if isinstance(days, int) else None)
