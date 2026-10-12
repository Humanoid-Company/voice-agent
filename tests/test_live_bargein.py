"""GPT-Live barge-in + non-blocking tool execution regressions."""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from tools.executor import ToolExecutionContext, ToolExecutor
from tools.results import ToolResult
from voice.live_session import LiveVoiceSession
from voice.playback import PlaybackTracker


def _executor_with_handler(name: str, handler, *, run_in_thread: bool) -> ToolExecutor:
    ex = ToolExecutor()
    ex.register(name, handler, run_in_thread=run_in_thread)
    return ex


def test_playback_barge_in_on_input_transcript_delta():
    ex = _executor_with_handler(
        "noop",
        lambda a, c: ToolResult(ok=True, status="ok", message=""),
        run_in_thread=False,
    )
    session = LiveVoiceSession(tool_executor=ex, session_id="sess-barge")
    # Avoid opening PortAudio — seed queue state only.
    session.player._stream = MagicMock()
    session.player._thread = MagicMock()
    session.player.enqueue(b"\x00\x01" * 200)
    assert session.player.is_playing
    assert session.player.queued_bytes > 0

    asyncio.run(
        session._handle_event({"type": "session.input_transcript.delta", "delta": "стоп"})
    )
    assert session.player.queued_bytes == 0


def test_blocking_tool_does_not_freeze_microphone_sender():
    started = threading.Event()
    release = threading.Event()

    def slow_handler(args, context):
        started.set()
        release.wait(timeout=2)
        return ToolResult(ok=True, status="ok", message="done")

    ex = _executor_with_handler("slow_tool", slow_handler, run_in_thread=True)
    assert ex.is_offloaded("slow_tool")

    sent_chunks: list[str] = []

    class FakeInputAudio:
        async def append(self, *, audio: str, event_id=None):
            sent_chunks.append(audio)

    class FakeSession:
        input_audio = FakeInputAudio()

        async def start(self, **kwargs):
            return None

        async def close(self):
            return None

    session = LiveVoiceSession(tool_executor=ex, session_id="sess-mic")
    session._connection = SimpleNamespace(session=FakeSession())
    session._mic_read = lambda: b"\x00\x01" * 64
    session._sleep_requested = False

    async def _run():
        sender = asyncio.create_task(session._send_audio_loop())
        tool_task = asyncio.create_task(
            session._executor.execute(
                "slow_tool", {}, ToolExecutionContext(session_id="sess-mic")
            )
        )
        await asyncio.to_thread(started.wait, 2)
        await asyncio.sleep(0.25)
        assert len(sent_chunks) >= 1
        release.set()
        await tool_task
        session._sleep_requested = True
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)

    asyncio.run(_run())


def test_blocking_tool_does_not_freeze_event_processing():
    release = threading.Event()

    def slow_handler(args, context):
        release.wait(timeout=2)
        return ToolResult(ok=True, status="ok", message="done")

    ex = _executor_with_handler("slow_tool", slow_handler, run_in_thread=True)
    session = LiveVoiceSession(tool_executor=ex, session_id="sess-evt")
    session.player._stream = MagicMock()
    session.player._thread = MagicMock()
    session.player.enqueue(b"\x00\x01" * 80)

    async def _run():
        tool_task = asyncio.create_task(
            session._executor.execute("slow_tool", {}, ToolExecutionContext())
        )
        await asyncio.sleep(0.05)
        await session._handle_event(
            {"type": "session.input_transcript.delta", "delta": "стоп"}
        )
        assert session.player.queued_bytes == 0
        release.set()
        await tool_task

    asyncio.run(_run())


def test_slow_tool_executes_off_event_loop_thread():
    loop_thread_id = {"id": None}
    handler_thread_id = {"id": None}

    def handler(args, context):
        handler_thread_id["id"] = threading.get_ident()
        time.sleep(0.05)
        return ToolResult(ok=True, status="ok", message="done", data={"done": True})

    ex = _executor_with_handler("slow_lookup", handler, run_in_thread=True)
    assert ex.is_offloaded("slow_lookup")

    async def _run():
        loop_thread_id["id"] = threading.get_ident()
        result = await ex.execute("slow_lookup", {}, ToolExecutionContext())
        assert result.ok and result.data.get("done") is True

    asyncio.run(_run())
    assert handler_thread_id["id"] is not None
    assert handler_thread_id["id"] != loop_thread_id["id"]


def test_only_registered_threaded_tools_are_offloaded():
    ex = _executor_with_handler("slow_lookup", lambda a, c: ToolResult(ok=True, status="ok", message="x"), run_in_thread=True)
    ex.register("change_voice", lambda a, c: ToolResult(ok=True, status="ok", message="x"))
    assert ex.is_offloaded("slow_lookup")
    assert not ex.is_offloaded("change_voice")
    assert not ex.is_offloaded("unknown_tool")


def test_mic_send_loop_not_gated_on_playback():
    """Mic must keep uploading during assistant playback (full-duplex).

    Checking player.is_playing for *barge-in* is fine; gating append is not.
    """
    live_src = Path(__file__).resolve().parents[1] / "voice" / "live_session.py"
    text = live_src.read_text(encoding="utf-8")
    send_section = text.split("async def _send_audio_loop")[1].split("async def _handle_event")[0]
    assert "input_audio.append" in send_section
    assert "don't send microphone" not in send_section.lower()
    # Must not skip append when playing:
    assert "if self.player.is_playing:\n                continue" not in send_section
    assert "if self.player.is_playing:\n                return" not in send_section

    tracker = PlaybackTracker(sample_rate=24000)
    tracker._stream = MagicMock()
    tracker._thread = MagicMock()
    tracker.enqueue(b"\x00\x01" * 10)
    assert tracker.is_playing
