"""Voice lists: parsing the providers' replies and the fallback. A mock transport, no network."""
import asyncio

import httpx

from server import voices
from server.config import Config

CFG = Config(respeecher_api_key="r", soniox_api_key="s", soniox_tts_model="tts-rt-v2")


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_respeecher_voices_use_the_ukrainian_model_and_key_header():
    seen = {}

    def handler(req):
        seen["url"], seen["key"] = str(req.url), req.headers.get("x-api-key")
        return httpx.Response(200, json=[{"id": "olesia-conversation", "full_name": "Олеся", "gender": "female", "accent": "ukrainian", "sampling_params": {"temperature": 0.8, "top_p": 0.9}}, {"id": "x", "full_name": "Ікс"}, {"nope": 1}])

    out = asyncio.run(voices._respeecher(CFG, client(handler)))
    assert seen == {"url": "https://api.respeecher.com/v1/public/tts/ua-rt/voices", "key": "r"}
    # each voice keeps its own sampling defaults (the base of the character's tuning); none listed is an empty dict
    assert out == [
        {"id": "olesia-conversation", "label": "Олеся (female, ukrainian)", "sampling": {"temperature": 0.8, "top_p": 0.9}},
        {"id": "x", "label": "Ікс", "sampling": {}},
    ]


def test_soniox_voices_come_from_the_configured_model():
    def handler(req):
        assert req.headers["authorization"] == "Bearer s"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"id": "tts-rt-v1", "voices": [{"id": "Old", "gender": "male"}]},
                    {"id": "tts-rt-v2", "voices": [{"id": "Maya", "gender": "female"}, {"id": "Daniel", "gender": "male"}]},
                ]
            },
        )

    out = asyncio.run(voices._soniox(CFG, client(handler)))
    assert [v["id"] for v in out] == ["Maya", "Daniel"] and out[0]["label"] == "Maya (female)"


def _soniox_catalog(req):
    names = ["Daniel", "Nina", "Taras", "Andrei", "Yana", "Bohdan", "Kirill", "Marek", "Emma", "Vadim"]
    return httpx.Response(200, json={"models": [{"id": "tts-rt-v2", "voices": [{"id": n, "gender": "x"} for n in names]}]})


def test_soniox_offers_only_ukrainian_voices_for_ukrainian():
    out = asyncio.run(voices._soniox(CFG, client(_soniox_catalog), "uk"))
    assert [v["id"] for v in out] == ["Taras", "Bohdan", "Andrei", "Yana", "Nina", "Emma"]  # in the order of SONIOX_UKRAINIAN
    assert "Kirill" not in [v["id"] for v in out] and "Vadim" not in [v["id"] for v in out]  # Russian-named: not offered


def test_soniox_keeps_the_full_list_for_other_languages():
    out = asyncio.run(voices._soniox(CFG, client(_soniox_catalog), "en"))
    assert len(out) == 10


def test_provider_list_falls_back_to_configured_voices_when_the_api_fails(monkeypatch):
    voices._cache.clear()

    async def boom(cfg, c):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(voices, "_respeecher", boom)
    monkeypatch.setattr(voices, "_soniox", boom)
    out = asyncio.run(voices.tts_providers(CFG))
    assert [p["id"] for p in out] == ["respeecher", "soniox"]
    assert out[0]["voices"] == [{"id": "olesia-conversation", "label": "olesia-conversation"}]
    assert out[1]["voices"] == [{"id": "Taras", "label": "Taras"}]


def test_only_providers_with_a_key_are_offered(monkeypatch):
    voices._cache.clear()

    async def one(cfg, c):
        return [{"id": "A", "label": "A"}]

    monkeypatch.setattr(voices, "_soniox", one)
    out = asyncio.run(voices.tts_providers(Config(respeecher_api_key="", soniox_api_key="s")))
    assert [p["id"] for p in out] == ["soniox"]
