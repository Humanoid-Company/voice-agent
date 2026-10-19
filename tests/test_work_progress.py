"""Delegated work (a slow tool) runs while she is silent: after a few quiet seconds the sideband
has her say she is on it, and stops once the answer comes."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from prompts.live_prompt import work_progress_note
from server import live_bridge
from server.live_bridge import SidebandToolBridge
from tools.executor import ToolExecutor
from tools.results import ToolResult


def _event(inner: dict) -> dict:
    return {"type": "response.event", "delegation_id": "dlg-1", "event": inner}


def _call(call_id: str) -> dict:
    return _event({
        "type": "response.output_item.done",
        "item": {"type": "function_call", "call_id": call_id, "name": "slow_lookup", "arguments": "{}"},
    })


def _bridge(monkeypatch, *, after=0.05, quiet=0.0, every=0.1) -> tuple[SidebandToolBridge, SimpleNamespace]:
    monkeypatch.setattr(live_bridge, "_MUMBLE_THINK_FIRST_S", after)
    monkeypatch.setattr(live_bridge, "_MUMBLE_EVERY_S", every)
    monkeypatch.setattr(live_bridge, "_MUMBLE_QUIET_S", quiet)
    monkeypatch.setattr(live_bridge, "_MUMBLE_INPUT_GRACE_S", 0.0)
    executor = ToolExecutor({"slow_lookup": lambda args, ctx: ToolResult(ok=True, status="ok", message="ok")})
    bridge = SidebandToolBridge(client=SimpleNamespace(), session_id="s1", executor=executor)
    conn = SimpleNamespace(
        response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()),
        session=SimpleNamespace(commentary=SimpleNamespace(append=AsyncMock())),
    )
    bridge._connection = conn
    return bridge, conn


def _said(conn: SimpleNamespace) -> list[str]:
    return [c.kwargs["content"] for c in conn.session.commentary.append.await_args_list]


def test_note_without_sample_lines():
    note = work_progress_note()
    assert "the task for their last request is still running" in note and "Not a result" in note
    assert "«" not in note  # sample lines were said word for word
    assert "nobody is talking" not in note and "go quiet" not in note  # a choice → a literal «null»


def test_she_keeps_thinking_aloud_while_the_work_runs(monkeypatch):
    """The team: she should talk the whole time she works, not one line after 8 s of silence."""
    bridge, conn = _bridge(monkeypatch)

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await asyncio.sleep(1.2)  # it checks every 0.25 s
        said = _said(conn)
        assert len(said) >= 3 and all("working out the answer" in line for line in said)
        assert "tiny sound" not in said[0] and "tiny sound" in said[1]  # «угу…» / «хм…»
        assert "half to yourself" in said[2]  # then a few half-said words, in turn

    asyncio.run(run())


def test_nothing_for_a_quick_answer(monkeypatch):
    bridge, conn = _bridge(monkeypatch, after=0.3)

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await bridge._handle_event(_event({"type": "response.completed", "response": {"id": "r1"}}))
        await asyncio.sleep(0.5)
        assert _said(conn) == [] and not bridge._work and not bridge._mumbles

    asyncio.run(run())


def test_at_most_a_few_lines_for_one_piece_of_work(monkeypatch):
    bridge, conn = _bridge(monkeypatch, every=0.05)
    monkeypatch.setattr(live_bridge, "_MUMBLE_MAX", 4)

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await asyncio.sleep(2.0)
        assert len(_said(conn)) == 4

    asyncio.run(run())


def test_no_progress_after_the_user_speaks_again(monkeypatch):
    bridge, conn = _bridge(monkeypatch, after=0.3)

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await asyncio.sleep(0.05)
        await bridge._handle_event({"type": "session.input_transcript.delta", "delta": "ні, я не про це"})
        await asyncio.sleep(0.6)
        assert _said(conn) == []

    asyncio.run(run())


def test_no_progress_while_she_talks(monkeypatch):
    bridge, conn = _bridge(monkeypatch, quiet=10.0)

    async def run():
        await bridge._handle_event({"type": "session.output_transcript.delta", "delta": "Секунду."})
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await asyncio.sleep(0.4)
        assert _said(conn) == []

    asyncio.run(run())


def test_no_progress_while_her_long_answer_still_plays(monkeypatch):
    """Prod: a full camera description was all text in a moment and played on for many seconds; the
    progress line for another delegation came in «4 s of no text» and cut her off mid-word."""
    bridge, conn = _bridge(monkeypatch, quiet=0.2)
    monkeypatch.setattr(live_bridge, "_SPEECH_CHARS_PER_S", 100.0)

    async def run():
        description = "Бачу людину в розстібнутому білому худі й темній футболці, вона сидить у чорному кріслі. " * 2
        await bridge._handle_event({"type": "session.output_transcript.delta", "delta": description})  # ~1.8 s of audio
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r2"}}))
        await asyncio.sleep(0.8)  # no new text for 0.8 s, but she is still speaking
        assert _said(conn) == []
        await asyncio.sleep(1.5)  # her audio has ended and 0.2 s of quiet passed
        assert len(_said(conn)) >= 1

    asyncio.run(run())


def test_setting_waits_for_her_audio_not_just_her_text(monkeypatch):
    monkeypatch.setattr(live_bridge, "_SPEECH_CHARS_PER_S", 100.0)
    bridge, conn = _bridge(monkeypatch)
    conn.session.instructions = SimpleNamespace(append=AsyncMock())

    async def run():
        await bridge._handle_event({"type": "session.output_transcript.delta", "delta": "Жила-була дівчинка. " * 5})  # ~1 s
        await bridge.append_instruction("speak slower", quiet_s=0.1)
        await asyncio.sleep(0.5)
        assert conn.session.instructions.append.await_count == 0
        await asyncio.sleep(1.0)
        assert conn.session.instructions.append.await_args.kwargs["content"] == "speak slower"

    asyncio.run(run())


def test_she_starts_soon_enough():
    # a quick answer (2–3 s) comes before the first line; a search gets one soon after it starts
    assert live_bridge._MUMBLE_THINK_FIRST_S >= 2.5 and live_bridge._MUMBLE_SEARCH_FIRST_S <= 2.0
    assert live_bridge._MUMBLE_EVERY_S <= 5.0


# ── a web search: she thinks aloud while it runs ─────────────────────────────


def _search_bridge(monkeypatch, *, first=0.05, every=0.1):
    import time as _time

    monkeypatch.setattr(live_bridge, "_MUMBLE_SEARCH_FIRST_S", first)
    monkeypatch.setattr(live_bridge, "_MUMBLE_THINK_FIRST_S", 10.0)  # only the search lines here
    monkeypatch.setattr(live_bridge, "_MUMBLE_EVERY_S", every)
    monkeypatch.setattr(live_bridge, "_MUMBLE_QUIET_S", 0.0)
    monkeypatch.setattr(live_bridge, "_MUMBLE_MAX", 3)

    def slow_search(args, ctx):
        _time.sleep(1.2)
        return ToolResult(ok=True, status="ok", message="Знайшла.")

    executor = ToolExecutor()
    executor.register("web_search", slow_search, run_in_thread=True)
    bridge = SidebandToolBridge(client=SimpleNamespace(), session_id="s1", executor=executor)
    conn = SimpleNamespace(
        response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()),
        session=SimpleNamespace(commentary=SimpleNamespace(append=AsyncMock())),
    )
    bridge._connection = conn
    return bridge, conn


def _search_call():
    return _event({
        "type": "response.output_item.done",
        "item": {"type": "function_call", "call_id": "c1", "name": "web_search", "arguments": '{"query": "курс біткоїна"}'},
    })


def test_she_thinks_aloud_while_a_search_runs(monkeypatch):
    """Feedback: a search left a long silence after her «Секунду». Lines while it runs, about this
    search."""
    bridge, conn = _search_bridge(monkeypatch)

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await bridge._handle_event(_search_call())
        await asyncio.sleep(1.0)
        said = _said(conn)
        assert len(said) == 3 and all("searching the web" in s and "курс біткоїна" in s for s in said)
        assert "tiny sound" not in said[0] and "tiny sound" in said[1]  # «угу…» / «хм…»
        assert "half to yourself" in said[2]  # then a few half-said words, in turn
        await asyncio.gather(*bridge._tool_tasks)

    asyncio.run(run())


def test_no_thinking_aloud_once_the_answer_is_ready_or_the_user_speaks(monkeypatch):
    bridge, conn = _search_bridge(monkeypatch, first=0.3)

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await bridge._handle_event(_search_call())
        await bridge._handle_event({"type": "session.input_transcript.delta", "delta": "а ні, не треба"})
        await asyncio.sleep(0.6)
        assert _said(conn) == []
        await asyncio.gather(*bridge._tool_tasks)

    asyncio.run(run())

    bridge, conn = _search_bridge(monkeypatch, first=0.3)

    async def answered():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await bridge._handle_event(_search_call())
        bridge._work.pop("dlg-1", None)  # the backend's answer is ready: she speaks it now
        await asyncio.sleep(0.6)
        assert _said(conn) == []
        await asyncio.gather(*bridge._tool_tasks)

    asyncio.run(answered())
