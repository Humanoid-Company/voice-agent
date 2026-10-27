"""What a call has cost so far, for the meter on the page. An estimate: prices come from the config."""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config


@dataclass
class Usage:
    llm_in: int = 0
    llm_out: int = 0
    searches: int = 0
    chars: dict[str, int] = field(default_factory=dict)  # TTS characters per provider (the voice can change)

    def cost(self, cfg: Config) -> float:
        per_1k = {"respeecher": cfg.price_respeecher_per_1k_chars, "soniox": cfg.price_soniox_per_1k_chars}
        return (
            self.llm_in * cfg.price_llm_in_per_m / 1e6
            + self.llm_out * cfg.price_llm_out_per_m / 1e6
            + self.searches * cfg.price_search
            + sum(n * per_1k.get(p, 0.0) / 1000 for p, n in self.chars.items())
        )
