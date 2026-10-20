from __future__ import annotations

import os
import tempfile

# config.py raises EnvironmentError at import time if OPENAI_API_KEY isn't set — set a dummy
# value before any test imports config, so tests never depend on a real .env.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy-key")
# The stores' default files (a save timer may fire after a test's own store is gone) go to a temp dir,
# never the repo's data/.
_TMP = tempfile.mkdtemp(prefix="eva-tests-")
os.environ.setdefault("KNOWLEDGE_SQLITE", os.path.join(_TMP, "knowledge.db"))
os.environ.setdefault("CONVERSATIONS_SQLITE", os.path.join(_TMP, "conversations.db"))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _temporary_stores(tmp_path, monkeypatch):
    """Knowledge and saved conversations go to throwaway SQLite files, never the repo's data/ or a real database."""
    from server import conversations, knowledge

    monkeypatch.setattr(knowledge, "store", knowledge.KnowledgeStore("", str(tmp_path / "knowledge.db")))
    monkeypatch.setattr(conversations, "store", conversations.ConversationStore("", str(tmp_path / "conversations.db")))
