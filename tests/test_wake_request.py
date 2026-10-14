"""A request said with the wake phrase («Єва, скажи, що ти бачиш?») goes to the Responses backend.

The model was muted and never heard it, and GPT-Live never delegates a request handed over as a
commentary: it answered it itself — a made-up room for «що ти бачиш», a made-up dollar rate.

The live check is opt-in (costs a few cents, needs network and OPENAI_API_KEY):
    RUN_LIVE_TESTS=1 pytest -q -k woken_request
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from prompts.live_prompt import woken_request_note
from tests.test_desktop_parity import _session

ROOT = Path(__file__).resolve().parents[1]


def test_page_uses_the_same_note_as_the_desktop():
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    # Join the page's string pieces: "…the " +\n      "backend…" → "…the backend…".
    joined = re.sub(r'"\s*\+\s*"', "", js).replace('" + text + "', "{text}")
    assert woken_request_note("{text}") in joined


def test_note_tells_the_backend_to_use_its_tools():
    note = woken_request_note("що ти бачиш?")
    # «your backend is already working on it» made the backend skip the camera.
    assert "nothing has been looked at or searched yet" in note and "already working" not in note
    assert "Never voice their words" in note


def test_desktop_sends_note_then_request_then_response_create():
    session = _session()
    order = MagicMock()
    conn = session._connection
    order.attach_mock(conn.session.thinking.append, "thinking")
    order.attach_mock(conn.response.item.create, "item")
    order.attach_mock(conn.response.create, "create")
    asyncio.run(session._ask_backend("що ти бачиш?", "Люди поруч говорили про вокзал."))
    assert [c[0] for c in order.mock_calls] == ["thinking", "thinking", "item", "create"]
    assert order.mock_calls[0].kwargs["content"] == "Люди поруч говорили про вокзал."
    assert order.mock_calls[1].kwargs["content"] == woken_request_note("що ти бачиш?")
    item = order.mock_calls[2].kwargs["item"]
    assert item["role"] == "user" and item["content"][0]["text"] == "що ти бачиш?"


@pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to call GPT-Live")
def test_live_woken_request_reaches_the_camera():
    """Real GPT-Live with the web session config: the backend looks through the camera."""
    import base64

    from dotenv import dotenv_values
    from openai import AsyncOpenAI

    import server.app as web
    from server.live_bridge import SidebandToolBridge
    from server.web_users import WebUser
    from voice.delegation import event_attr

    key = dotenv_values(ROOT / ".env").get("OPENAI_API_KEY")  # conftest puts a dummy into the env
    frame = b"\xff\xd8\xff" + b"0" * 2000  # any JPEG-looking bytes: only the call is checked here
    question = "Що ти бачиш у камері?"

    async def run() -> list[str]:
        client = AsyncOpenAI(api_key=key)
        user = WebUser(client_id="live-wake-test-0123456789")
        user.set_camera_frame(frame)
        config = web._session_config(user)
        config["audio"]["format"] = {"type": "audio/pcm", "rate": 24000}
        tools: list[str] = []
        async with client.live.connect() as conn:
            bridge = SidebandToolBridge(client=client, session_id="live-test", executor=user.executor)
            bridge._connection = conn
            await conn.session.start(session=config, event_id="start")

            async def mic():  # Live expects a live microphone stream
                while True:
                    await conn.session.input_audio.append(audio=base64.b64encode(b"\x00\x00" * 480).decode())
                    await asyncio.sleep(0.02)

            async def reader():
                async for event in conn:
                    if event_attr(event, "type") == "session.started":
                        await conn.session.thinking.append(content=woken_request_note(question), delegation_id=None)
                        await conn.response.item.create(
                            item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": question}]}
                        )
                        await conn.response.create()
                    inner = event_attr(event, "event")
                    item = event_attr(inner, "item")
                    if event_attr(inner, "type") == "response.output_item.done" and event_attr(item, "type") == "function_call":
                        tools.append(str(event_attr(item, "name")))
                        user.set_camera_frame(frame)  # the page snaps one for the look
                    await bridge._handle_event(event)

            tasks = [asyncio.create_task(mic()), asyncio.create_task(reader())]
            for _ in range(100):
                if tools:
                    break
                await asyncio.sleep(0.2)
            for task in tasks:
                task.cancel()
        return tools

    assert "look_at_camera" in asyncio.run(run())
