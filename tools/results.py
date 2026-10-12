"""Structured tool results for Live Responses delegation."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    ok: bool
    status: str  # "ok", "error", "needs_more_info", …
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "message": self.message, "data": dict(self.data)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
