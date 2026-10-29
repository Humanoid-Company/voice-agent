"""A real connection to GPT-Live: start a session, let the model greet, print what comes back.

    uv run python scripts/live_smoke.py [--seconds 8] [--model gpt-live-1]

Needs OPENAI_API_KEY (from .env). One short session: it costs a few cents (the per-minute price of the session). It shows the
event types GPT-Live sends, the first transcript fragments with their start_ms / end_ms, the size of the audio it generates
anyway (there is no way to switch it off) and, at the end, the usage that session.closed reports.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

from server.config import Config  # noqa: E402
from server.persona import Persona  # noqa: E402
from server.prompt import build_stable_prompt  # noqa: E402


async def main(seconds: float, model: str) -> int:
    cfg = Config()
    if not cfg.openai_api_key:
        print("OPENAI_API_KEY is not set (put it in .env): nothing was sent.")
        return 2
    instructions = build_stable_prompt(search=False, persona=Persona(), provider="soniox")
    start = {
        "type": "session.start",
        "session": {
            "model": model,
            "instructions": instructions,
            "audio": {"format": {"type": "audio/pcm", "rate": 24000}},
            "input": [
                {
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Людина щойно увімкнула розмову. Привітайся одним-двома реченнями."}],
                }
            ],
        },
    }
    seen: Counter[str] = Counter()
    audio_bytes = 0
    shown = 0
    t0 = time.monotonic()
    first_transcript: float | None = None
    print(f"connecting to {cfg.live_url} as {model} …")
    async with websockets.connect(
        cfg.live_url, additional_headers={"Authorization": f"Bearer {cfg.openai_api_key}"}, max_size=None
    ) as ws:
        await ws.send(json.dumps(start))
        deadline = t0 + seconds
        closing = False
        while True:
            left = deadline - time.monotonic() if not closing else 5.0
            if left <= 0 and not closing:
                await ws.send(json.dumps({"type": "session.close"}))
                closing = True
                deadline = time.monotonic() + 5.0
                continue
            try:
                raw = await asyncio.wait_for(ws.recv(), max(left, 0.1))
            except TimeoutError:
                if closing:
                    break
                continue
            except websockets.ConnectionClosed:
                break
            ev = json.loads(raw)
            kind = ev.get("type", "?")
            seen[kind] += 1
            if kind == "session.output_audio.delta":
                audio_bytes += len(ev.get("delta", "")) * 3 // 4
            elif kind in ("session.output_transcript.delta", "session.input_transcript.delta"):
                if first_transcript is None and kind.startswith("session.output"):
                    first_transcript = time.monotonic() - t0
                if shown < 12:
                    shown += 1
                    print(f"  {kind}: {ev.get('delta')!r} [{ev.get('start_ms')}–{ev.get('end_ms')} ms]")
            elif kind in ("session.started", "session.closed", "session.usage.updated", "error"):
                print(f"  {kind}: {json.dumps(ev, ensure_ascii=False)[:300]}")
            if kind == "session.closed":
                break
    print("\nevent types:", dict(seen))
    print(f"audio generated (not played): {audio_bytes} bytes ≈ {audio_bytes / 2 / 24000:.1f} s")
    if first_transcript is not None:
        print(f"first transcript fragment after {first_transcript * 1000:.0f} ms")
    else:
        print("no output_transcript.delta came: the model did not speak (check the events above)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--model", default=Config().realtime_model if Config().realtime_model.startswith("gpt-live") else "gpt-live-1")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.seconds, args.model)))
