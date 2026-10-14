"""Structured tool results for Live Responses delegation."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    ok: bool
    status: str  # "ok", "error", "needs_more_info", …
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    # A camera frame for the model that runs the tools (look_at_camera); never in to_dict / the history.
    image_jpeg: bytes | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "message": self.message, "data": dict(self.data)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def to_output(self) -> str | list[dict[str, Any]]:
        """`function_call_output.output`: the JSON text, plus the image when there is one."""
        if not self.image_jpeg:
            return self.to_json()
        url = "data:image/jpeg;base64," + base64.b64encode(self.image_jpeg).decode("ascii")
        return [
            {"type": "input_text", "text": self.to_json()},
            # high: measured as fast as low on gpt-6-luna for a 768 px frame, and small text stays legible.
            {"type": "input_image", "image_url": url, "detail": "high"},
        ]
