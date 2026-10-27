"""Streaming text completion. The text deltas feed the sentence splitter and then TTS."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from openai import AsyncOpenAI

from .config import Config
from .prompt import SYSTEM_PROMPT

_MAX_HISTORY = 30


class LLM(Protocol):
    def stream(self, history: list[dict]) -> AsyncIterator[str]: ...


class OpenAILLM:
    def __init__(self, cfg: Config) -> None:
        self._client = AsyncOpenAI(api_key=cfg.openai_api_key)
        self._model = cfg.openai_model

    async def stream(self, history: list[dict]) -> AsyncIterator[str]:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history[-_MAX_HISTORY:]]
        resp = await self._client.chat.completions.create(model=self._model, messages=messages, stream=True)
        try:
            async for chunk in resp:
                if chunk.choices and (delta := chunk.choices[0].delta.content):
                    yield delta
        finally:
            await resp.close()  # a cancelled turn must stop the generation (and the bill)
