"""Public web search (Tavily) for the `web_search` tool. Never raises: errors come back as text for the model."""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_URL = "https://api.tavily.com/search"
_MAX_QUERY = 400
_MAX_RESULTS = 5
_SNIPPET = 260


def _source(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


async def web_search(query: str, *, api_key: str, recency_days: int | None = None, timeout_s: float = 9.0) -> str:
    """Search and return a compact plain-text digest for the model (title, source, date, snippet)."""
    q = re.sub(r"\s+", " ", (query or "").strip())[:_MAX_QUERY]
    if not q:
        return "Порожній пошуковий запит."
    payload: dict = {"api_key": api_key, "query": q, "max_results": _MAX_RESULTS, "search_depth": "basic"}
    if recency_days and recency_days > 0:
        payload["days"] = int(recency_days)
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(_URL, json=payload, headers={"Authorization": f"Bearer {api_key}"})
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except httpx.TimeoutException:
        log.warning("web_search timeout")
        return "Пошук не встиг відповісти. Скажи про це коротко й відповідай з того, що знаєш."
    except (httpx.HTTPError, ValueError) as e:
        log.warning("web_search failed: %s", type(e).__name__)
        return "Пошук зараз недоступний. Скажи про це коротко й відповідай з того, що знаєш."
    lines = []
    for r in results[:_MAX_RESULTS]:
        if not isinstance(r, dict):
            continue
        url = str(r.get("url") or "")
        head = str(r.get("title") or _source(url))
        meta = ", ".join(x for x in (_source(url), str(r.get("published_date") or "")) if x)
        snippet = str(r.get("content") or "")[:_SNIPPET]
        lines.append(f"- {head} ({meta}): {snippet}" if meta else f"- {head}: {snippet}")
    log.info('web_search "%s": %d results', q[:80], len(lines))
    return "\n".join(lines) or "Надійних результатів за цим запитом не знайдено."
