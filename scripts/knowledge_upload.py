"""Upload ready-made sections to «Знання» (server/knowledge.py) through the backend's API.

    uv run python scripts/knowledge_upload.py data/events/свято.json
    uv run python scripts/knowledge_upload.py data/events/свято.json --backend http://localhost:8000
    KNOWLEDGE_TOKEN=… uv run python scripts/knowledge_upload.py …   (when the server has KNOWLEDGE_TOKEN)

The file: {"sets": [{"name": …, "kind": "text" | "records", "note": …, "pinned": true|false,
                     "body": "…" (text) | "items": [{"title": …, "fields": {…}}] (records)}]}

A section with the same name is replaced (deleted, then created again), so the file can be fixed and uploaded
again. Keep such files in data/ (git ignores it): the repository is public and these are people's details.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

DEFAULT_BACKEND = "https://voice-agent-f3m4.onrender.com"  # web/config.js BACKEND_URL


def upload(path: Path, backend: str, *, token: str = "", dry_run: bool = False) -> int:
    sets = json.loads(path.read_text(encoding="utf-8"))["sets"]
    headers = {"X-Knowledge-Token": token} if token else {}
    with httpx.Client(base_url=backend.rstrip("/"), timeout=90, headers=headers) as client:  # Render's free plan wakes up to a minute
        existing = {s["name"]: s["id"] for s in client.get("/api/knowledge").raise_for_status().json()["sets"]}
        for s in sets:
            size = f"{len(s.get('body', ''))} симв." if s.get("kind") == "text" else f"{len(s.get('items', []))} записів"
            pinned = ", завжди пам'ятати" if s.get("pinned") else ""
            print(f"{'(перевірка) ' if dry_run else ''}«{s['name']}» — {size}{pinned}"
                  + (" — замінюю наявний" if s["name"] in existing else ""))
            if dry_run:
                continue
            if s["name"] in existing:
                client.delete(f"/api/knowledge/{existing[s['name']]}").raise_for_status()
            res = client.post("/api/knowledge", json=s)
            if res.status_code != 200:
                print(f"  помилка {res.status_code}: {res.text}", file=sys.stderr)
                return 1
        if not dry_run:
            listing = client.get("/api/knowledge").json()
            print(f"Готово. «Завжди пам'ятати» в кожній розмові: {listing.get('pinned_chars', 0)} з "
                  f"{listing.get('pinned_max', '?')} символів.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("file", type=Path)
    parser.add_argument("--backend", default=DEFAULT_BACKEND)
    parser.add_argument("--token", default=os.getenv("KNOWLEDGE_TOKEN", ""), help="пароль «Знань» (або KNOWLEDGE_TOKEN)")
    parser.add_argument("--dry-run", action="store_true", help="лише показати, що буде завантажено")
    args = parser.parse_args()
    return upload(args.file, args.backend, token=args.token, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
