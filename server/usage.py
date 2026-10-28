"""What a call has cost so far, for the meter on the page. An estimate: prices come from the config."""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config


@dataclass
class Usage:
    llm_in: int = 0
    llm_out: int = 0
    audio_in: int = 0  # Realtime: audio tokens the model heard
    searches: int = 0
    chars: dict[str, int] = field(default_factory=dict)  # TTS characters per provider (the voice can change)
    realtime: bool = False  # llm_in / llm_out were billed by the Realtime model

    def cost(self, cfg: Config) -> float:
        per_1k = {"respeecher": cfg.price_respeecher_per_1k_chars, "soniox": cfg.price_soniox_per_1k_chars}
        text_in = cfg.price_rt_text_in_per_m if self.realtime else cfg.price_llm_in_per_m
        text_out = cfg.price_rt_text_out_per_m if self.realtime else cfg.price_llm_out_per_m
        return (
            self.llm_in * text_in / 1e6
            + self.llm_out * text_out / 1e6
            + self.audio_in * cfg.price_rt_audio_in_per_m / 1e6
            + self.searches * cfg.price_search
            + sum(n * per_1k.get(p, 0.0) / 1000 for p, n in self.chars.items())
        )
