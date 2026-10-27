import asyncio

import httpx

from server.app import keep_awake


def test_keep_awake_pings_healthz_repeatedly_and_survives_a_failed_ping():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if len(seen) == 1:
            raise httpx.ConnectError("sleeping")
        return httpx.Response(200, json={"ok": True})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        task = asyncio.create_task(keep_awake("https://x.onrender.com", 0.01, client))
        await asyncio.sleep(0.08)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert len(seen) >= 3 and set(seen) == {"/healthz"}
