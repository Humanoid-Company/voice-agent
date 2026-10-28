"""The site's entry: one password for a signed token, and what the token opens."""
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server.auth import make_token, password_ok, valid_token

SECRET = "test-password"


def test_the_password_check_is_exact_and_never_passes_without_a_setting():
    assert password_ok(SECRET, SECRET)
    for bad in ("", "Test-password", SECRET + " ", None, 5, b"test-password"):
        assert not password_ok(SECRET, bad)
    assert not password_ok("", "") and not password_ok("", "anything")


def test_a_token_is_valid_until_it_expires_and_only_for_its_own_password():
    now = 1_000_000.0
    t = make_token(SECRET, days=30, now=now)
    assert valid_token(SECRET, t, now=now + 29 * 86400)
    assert not valid_token(SECRET, t, now=now + 31 * 86400)  # expired
    assert not valid_token("other", t, now=now)  # the password was changed: everybody is signed out
    assert not valid_token("", t, now=now)


def test_a_forged_or_damaged_token_is_refused():
    now = 1_000_000.0
    t = make_token(SECRET, now=now)
    expiry, sig = t.split(".")
    for bad in (
        "", "garbage", ".", f"{expiry}.", f".{sig}", f"{int(expiry) + 99999}.{sig}",  # a longer life, same signature
        f"{expiry}.{sig[:-1]}0", f"abc.{sig}", None, 7, [t],
    ):
        assert not valid_token(SECRET, bad, now=now), bad


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "cfg", replace(app_module.cfg, access_password=SECRET))

    async def quick(_s):
        pass

    monkeypatch.setattr(app_module.asyncio, "sleep", quick)  # a wrong password waits; not in tests
    return TestClient(app_module.app)


def test_a_right_password_gives_a_token_that_opens_the_api(client):
    r = client.post("/api/login", json={"password": SECRET})
    assert r.status_code == 200
    token = r.json()["token"]
    assert valid_token(SECRET, token)
    assert client.get("/api/config", headers={"X-Access-Token": token}).status_code == 200


def test_a_wrong_password_gives_nothing(client):
    for body in ({"password": "wrong"}, {"password": ""}, {}, {"password": None}, {"password": 1}):
        r = client.post("/api/login", json=body)
        assert r.status_code == 401 and "token" not in r.json() and r.json()["detail"] == "Невірний пароль."


def test_the_api_is_closed_without_a_token_but_the_health_check_is_not(client):
    assert client.get("/api/config").status_code == 401
    assert client.get("/api/config", headers={"X-Access-Token": "nope"}).status_code == 401
    assert client.get("/api/preview", params={"provider": "respeecher", "voice": "x"}).status_code == 401
    assert client.get("/api/knowledge").status_code == 401
    assert client.get("/healthz").status_code == 200


def test_the_voice_sample_takes_the_token_from_the_query_because_an_audio_element_cannot_send_headers(client):
    token = make_token(SECRET)
    # past the login: it is the provider (not configured in the test) that has nothing to say, not the login
    r = client.get("/api/preview", params={"provider": "respeecher", "voice": "x", "t": token})
    assert r.status_code != 401


def test_a_call_needs_the_token_in_its_first_message(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "start", "provider": "respeecher", "voice": "x"}))
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["auth"] is True
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "start", "provider": "respeecher", "voice": "x", "token": "forged"}))
        assert ws.receive_json()["auth"] is True
    with client.websocket_connect("/ws") as ws:  # a good token gets past the login: here to «provider is not configured»
        ws.send_text(json.dumps({"type": "start", "provider": "respeecher", "voice": "x", "token": make_token(SECRET)}))
        msg = ws.receive_json()
        assert msg["type"] == "error" and "auth" not in msg
