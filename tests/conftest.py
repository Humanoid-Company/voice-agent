from __future__ import annotations

import os

# config.py raises EnvironmentError at import time if OPENAI_API_KEY isn't set — set a dummy
# value before any test imports config, so tests never depend on a real .env.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy-key")
