"""Tool executor for GPT-Live Responses delegation: parse arguments, run the registered handler,
return a structured result. New tools are added with `register` (see server/web_users.py and
assistant.py) and described for the model in tools/live_schemas.py."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tools.results import ToolResult

logger = logging.getLogger(__name__)

HandlerFn = Callable[[dict[str, Any], "ToolExecutionContext"], "ToolResult | str"]


@dataclass
class _HandlerSpec:
    handler: HandlerFn
    run_in_thread: bool = False


@dataclass
class ToolExecutionContext:
    session_id: str | None = None
    delegation_id: str | None = None
    # The user's recent lines (a voice change must have been asked for by them).
    user_utterances: list[str] | None = None
    # Her recent lines: what she offered, for a tool that acts on the user's «так».
    assistant_utterances: list[str] | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class ToolExecutor:
    """Parse/validate args, route to handlers, serialize structured results."""

    def __init__(self, handlers: dict[str, HandlerFn] | None = None) -> None:
        self._handlers: dict[str, _HandlerSpec] = {
            name: _HandlerSpec(handler) for name, handler in (handlers or {}).items()
        }

    def register(self, name: str, handler: HandlerFn, *, run_in_thread: bool = False) -> None:
        """run_in_thread: blocking work (network, files) that must not stall the voice loop."""
        self._handlers[name] = _HandlerSpec(handler, run_in_thread=run_in_thread)

    def is_offloaded(self, name: str) -> bool:
        spec = self._handlers.get(name)
        return spec is not None and spec.run_in_thread

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | str | None,
        context: ToolExecutionContext,
    ) -> ToolResult:
        parsed = self._parse_arguments(arguments)
        if parsed is None:
            return ToolResult(ok=False, status="error", message="Некоректні аргументи інструмента.")
        spec = self._handlers.get(name)
        if spec is None:
            return ToolResult(ok=False, status="error", message=f"Невідома команда: {name}")
        started = time.monotonic()
        logger.info("live.tool.started tool_name=%s session_id=%s", name, context.session_id)
        try:
            if spec.run_in_thread:
                raw = await asyncio.to_thread(spec.handler, parsed, context)
            else:
                raw = spec.handler(parsed, context)
        except TypeError as exc:
            logger.warning("live.tool.failed tool_name=%s error=TypeError", name)
            return ToolResult(
                ok=False,
                status="error",
                message="Не вистачає параметрів команди. Повтори, що саме зробити.",
                data={"error_type": type(exc).__name__},
            )
        except Exception as exc:
            logger.exception("live.tool.failed tool_name=%s", name)
            return ToolResult(
                ok=False, status="error", message="Не вдалося виконати запит.", data={"error_type": type(exc).__name__}
            )
        result = raw if isinstance(raw, ToolResult) else ToolResult(ok=True, status="ok", message=str(raw))
        logger.info(
            "live.tool.completed tool_name=%s status=%s duration_ms=%s",
            name,
            result.status,
            int((time.monotonic() - started) * 1000),
        )
        return result

    def execute_sync(
        self,
        name: str,
        arguments: dict[str, Any] | str | None,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Sync entry for unit tests / sync callers."""
        return asyncio.run(self.execute(name, arguments, context))

    @staticmethod
    def _parse_arguments(arguments: dict[str, Any] | str | None) -> dict[str, Any] | None:
        if arguments is None:
            return {}
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str):
            text = arguments.strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
        return None
