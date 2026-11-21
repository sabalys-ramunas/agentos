"""Progress notifications and notices on MCP tool calls (openajar.core.progress), for the registry."""

from __future__ import annotations

import asyncio
from typing import Any

import pydantic_core
from mcp.types import CallToolResult, TextContent

from openajar.core import progress
from openajar.policy.redactor import Redactor


def _json(value: Any) -> str:
    return pydantic_core.to_json(value, fallback=str, indent=2).decode()


def start_heartbeat(context: Any, state: progress.CallProgress) -> asyncio.Task | None:
    """A progress notification every progress.HEARTBEAT_S while the call runs, so a client that gives
    up on a silent call (Claude Code: 300 s) keeps waiting. On both protocols the SDK sends it against
    the call's progressToken (on the 2026-07-28 one on the call's SSE stream); a client that sent no
    token gets none. None without a request context (a call from inside the node, a test)."""
    if getattr(context, "_request_context", None) is None or not callable(getattr(context, "report_progress", None)):
        return None

    async def send(n: float, text: str) -> None:
        await context.report_progress(n, None, text)

    return asyncio.ensure_future(progress.heartbeat(state, send))


def with_notices(result: Any, state: progress.CallProgress, redactor: Redactor) -> Any:
    """The call's result with the notices code under it left for the agent (progress.notice): a
    "notice" in structured content and its JSON text, else a last text block."""
    if not state.notices or not isinstance(result, CallToolResult):
        return result
    text = redactor.text(" ".join(state.notices))
    data = result.structured_content
    if isinstance(data, dict) and not result.is_error:
        data = {**data, "notice": text}
        blocks = list(result.content)
        if blocks and isinstance(blocks[0], TextContent):
            blocks[0] = TextContent(type="text", text=_json(data))
        return result.model_copy(update={"structured_content": data, "content": blocks})
    return result.model_copy(update={"content": [*result.content, TextContent(type="text", text=f"notice: {text}")]})
