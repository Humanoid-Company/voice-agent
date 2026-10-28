"""A short sample of a voice for the «▶ Прослухати» button: the real synthesizer says a fixed phrase, once per voice and
language, and the audio is kept. The phrase is fixed and the voice must be one the provider lists, so the number of
paid syntheses is bounded by voices × languages, however often the endpoint is called."""
from __future__ import annotations

import asyncio
import contextlib
import io
import wave
from collections.abc import Awaitable, Callable
from typing import Any

from .config import Config
from .persona import SUPPORTED
from .tts import TTSError

PHRASES = {
    "uk": "Привіт! Я твій голосовий помічник. Ось так я звучу, коли ми розмовляємо.",
    "en": "Hi! I'm your voice assistant. This is how I sound when we talk.",
}
_MAX_KEPT = 120


class PreviewError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """PCM16 mono → a WAV file, which every browser plays."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return out.getvalue()


def phrase_language(provider: str, language: str) -> str:
    """The sample is Ukrainian or English: the language asked for when it has a phrase and the provider speaks it,
    else English where the provider speaks it, else Ukrainian."""
    can = SUPPORTED.get(provider, set())
    if language in PHRASES and language in can:
        return language
    return "en" if "en" in can and language != "uk" else "uk"


class VoicePreviews:
    def __init__(self) -> None:
        self._kept: dict[tuple[str, str, str], bytes] = {}
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    async def get(
        self,
        cfg: Config,
        provider: str,
        voice: str,
        language: str,
        *,
        make_tts: Callable[..., Any],
        list_voices: Callable[..., Awaitable[list[dict]]],
    ) -> bytes:
        lang = phrase_language(provider, language)
        key = (provider, voice, lang)
        if key in self._kept:
            return self._kept[key]
        async with self._locks.setdefault(key, asyncio.Lock()):  # one synthesis for a voice, however many clicks
            if key in self._kept:
                return self._kept[key]
            voices = await list_voices(cfg, provider, lang)
            if not any(v["id"] == voice for v in voices):
                raise PreviewError(404, "Такого голосу немає.")
            try:
                tts = make_tts(provider, voice, cfg, lang)
            except TTSError as e:
                raise PreviewError(400, str(e)) from e
            pcm = bytearray()
            try:
                await tts.connect()
                async for chunk in tts.synthesize(PHRASES[lang]):
                    pcm += chunk
            except Exception as e:
                raise PreviewError(502, f"Не вдалося озвучити зразок: {type(e).__name__}") from e
            finally:
                with contextlib.suppress(Exception):  # the sample is made or not; a failing close changes nothing
                    await tts.close()
            if not pcm:
                raise PreviewError(502, "Голос не повернув звуку.")
            wav = pcm_to_wav(bytes(pcm), tts.sample_rate)
            if len(self._kept) >= _MAX_KEPT:
                self._kept.pop(next(iter(self._kept)))
            self._kept[key] = wav
            return wav
