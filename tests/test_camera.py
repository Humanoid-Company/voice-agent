"""Camera on the web: the page sends frames, look_at_camera hands the latest one to the tool model."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import server.app as web
import server.web_users as web_users
from server.live_bridge import SidebandToolBridge
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.live_schemas import LIVE_BACKEND_TOOLS
from tools.results import ToolResult

JPEG = b"\xff\xd8\xff\xe0" + b"frame" * 100


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    return TestClient(web.app)


def _look(user, question="що ти бачиш?"):
    return asyncio.run(
        user.executor.execute("look_at_camera", {"question": question}, ToolExecutionContext(session_id="s"))
    )


def test_result_without_image_stays_plain_json():
    result = ToolResult(ok=True, status="ok", message="Готово.")
    assert result.to_output() == result.to_json()


def test_result_with_image_carries_it_for_the_model():
    output = ToolResult(ok=True, status="ok", message="Кадр.", image_jpeg=JPEG).to_output()
    text, image = output
    assert text["type"] == "input_text" and "Кадр." in text["text"]
    assert image["type"] == "input_image" and image["image_url"].startswith("data:image/jpeg;base64,/9j/")
    assert "image_jpeg" not in ToolResult(ok=True, status="ok", message="x", image_jpeg=JPEG).to_dict()


def test_frame_endpoint_keeps_the_latest_frame_and_forgets_it_when_off(client):
    headers = {"X-Client-Id": "cam-browser-0123456789ab"}
    user = web.users.get(headers["X-Client-Id"])
    assert client.post("/api/frame", headers={**headers, "Content-Type": "image/jpeg"}, content=JPEG).status_code == 200
    assert user.camera_on and user.camera_frame == JPEG
    assert client.delete("/api/frame", headers=headers).status_code == 200
    assert not user.camera_on and user.camera_frame is None


def test_frame_endpoint_takes_only_jpeg_of_sane_size(client):
    headers = {"X-Client-Id": "cam2-browser-0123456789a", "Content-Type": "image/jpeg"}
    assert client.post("/api/frame", headers=headers, content=b"<html>").status_code == 415
    big = b"\xff\xd8\xff" + b"0" * web.MAX_FRAME_BYTES
    assert client.post("/api/frame", headers=headers, content=big).status_code == 413
    assert web.users.get(headers["X-Client-Id"]).camera_frame is None


def test_look_with_the_camera_off_says_so():
    result = _look(web.users.get("cam3-browser-0123456789a"))
    assert not result.ok and result.status == "camera_off" and result.image_jpeg is None
    assert "Камера" in result.message


def test_look_returns_the_fresh_frame_with_the_question():
    user = web.users.get("cam4-browser-0123456789a")
    user.set_camera_frame(JPEG)
    result = _look(user, "що в мене в руці?")
    assert result.ok and result.image_jpeg == JPEG and "що в мене в руці?" in result.message


def test_look_waits_for_the_next_frame_after_a_pause(monkeypatch):
    monkeypatch.setattr(web_users, "FRAME_WAIT_S", 2.0)
    user = web.users.get("cam5-browser-0123456789a")
    user.set_camera_frame(b"\xff\xd8\xffold")
    user.camera_frame_at = time.time() - 60  # sent before the pause
    threading.Timer(0.3, user.set_camera_frame, args=(JPEG,)).start()
    result = _look(user)
    assert result.ok and result.image_jpeg == JPEG


def test_look_takes_the_frame_snapped_for_it_not_an_older_one(monkeypatch):
    """Prod: the object was held up as they asked, and she described the frame from before."""
    monkeypatch.setattr(web_users, "FRAME_WAIT_S", 2.0)
    user = web.users.get("cam8-browser-0123456789a")
    user.set_camera_frame(b"\xff\xd8\xffbefore")
    user.camera_frame_at = time.time() - 2  # the last periodic frame: recent, but before the look
    threading.Timer(0.4, user.set_camera_frame, args=(JPEG,)).start()  # the page snaps one for the look
    result = _look(user)
    assert result.ok and result.image_jpeg == JPEG


def test_look_takes_the_frame_snapped_when_the_request_went_to_the_backend(monkeypatch):
    """The page snaps as the delegation starts: by the time the backend decides to look, that frame —
    taken after the question — is on the server, and the tool returns it without waiting."""
    monkeypatch.setattr(web_users, "FRAME_WAIT_S", 2.0)
    user = web.users.get("cam10-browser-0123456789")
    work_started = time.time() - 1.5
    user.set_camera_frame(JPEG)
    user.camera_frame_at = work_started + 0.4  # uploaded 1.1 s before the call, after the work began
    started = time.monotonic()
    result = asyncio.run(user.executor.execute(
        "look_at_camera", {"question": "що ти бачиш?"},
        ToolExecutionContext(session_id="s", extras={"work_started_at": work_started}),
    ))
    assert result.ok and result.image_jpeg == JPEG and time.monotonic() - started < 0.3


def test_bridge_tells_the_tool_when_the_work_started():
    seen = {}

    def look(args, ctx):
        seen.update(ctx.extras)
        return ToolResult(ok=True, status="ok", message="Кадр.")

    bridge = SidebandToolBridge(client=SimpleNamespace(), session_id="s1", executor=ToolExecutor({"look_at_camera": look}))
    bridge._connection = SimpleNamespace(response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()))

    async def run():
        await bridge._handle_event({"type": "response.event", "delegation_id": "d", "event": {"type": "response.created", "response": {"id": "r"}}})
        await bridge._handle_event({"type": "response.event", "delegation_id": "d", "event": {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "c1", "name": "look_at_camera", "arguments": "{}"},
        }})
        await asyncio.gather(*bridge._tool_tasks)

    before = time.time()
    asyncio.run(run())
    assert before - 1 < seen["work_started_at"] <= time.time()


def test_look_falls_back_to_a_recent_frame_when_none_is_snapped(monkeypatch):
    monkeypatch.setattr(web_users, "FRAME_WAIT_S", 0.3)
    user = web.users.get("cam9-browser-0123456789a")
    user.set_camera_frame(JPEG)
    user.camera_frame_at = time.time() - 3  # an old cached page sends only periodic frames
    result = _look(user)
    assert result.ok and result.image_jpeg == JPEG


def test_look_gives_up_when_frames_stopped(monkeypatch):
    monkeypatch.setattr(web_users, "FRAME_WAIT_S", 0.3)
    user = web.users.get("cam6-browser-0123456789a")
    user.set_camera_frame(JPEG)
    user.camera_frame_at = time.time() - 60  # the tab was closed without turning it off
    result = _look(user)
    assert not result.ok and result.status == "no_frame" and result.image_jpeg is None


def test_web_session_gets_the_camera_tool_and_notes(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    sent: list[dict] = []

    async def fake_create(*, session, transport):
        sent.append(session)
        return SimpleNamespace(session=SimpleNamespace(id="sess-cam"), transport=SimpleNamespace(sdp="answer"))

    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", lambda self: asyncio.sleep(0))
    headers = {"X-Client-Id": "cam7-browser-0123456789a"}
    assert TestClient(web.app).post("/api/session", headers=headers, json={"sdp": "o"}).status_code == 200
    session = sent[0]
    responses = session["delegation"]["responses"]
    assert "look_at_camera" in [t["name"] for t in responses["tools"]]
    assert "CAMERA:" in responses["instructions"]
    assert "You cannot see by yourself" in session["instructions"]
    # Prod: «А це шо?», «Це шо таке?» while holding a speaker up got «не знаю, про що ти питаєш».
    assert "а це шо?" in session["instructions"] and "never answer «не знаю" in session["instructions"]
    # Prod: «що ти бачиш» answered without a look — «стіл, ноутбук, чашка» — then «ту саму картину».
    assert "always needs a new look" in session["instructions"] and "never blame the user" in session["instructions"]
    assert "One look per request" in responses["instructions"]
    web.users.get(headers["X-Client-Id"]).bridges.clear()


def test_desktop_tools_have_no_camera():
    assert "look_at_camera" not in [t["name"] for t in LIVE_BACKEND_TOOLS]


def test_bridge_sends_the_frame_as_an_image_output():
    executor = ToolExecutor({
        "look_at_camera": lambda args, ctx: ToolResult(ok=True, status="ok", message="Кадр.", image_jpeg=JPEG),
    })
    bridge = SidebandToolBridge(client=SimpleNamespace(), session_id="s1", executor=executor)
    conn = SimpleNamespace(response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()))
    bridge._connection = conn

    async def run():
        await bridge._handle_event({"type": "response.event", "delegation_id": "d", "event": {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "c1", "name": "look_at_camera", "arguments": "{}"},
        }})
        await asyncio.gather(*bridge._tool_tasks)

    asyncio.run(run())
    item = conn.response.item.create.await_args.kwargs["item"]
    assert item["type"] == "function_call_output" and item["output"][1]["type"] == "input_image"
