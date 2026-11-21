"""Run one `invoke` exactly as a local MCP call runs, and shape its outcome for `result`.

The call goes to the node's own ToolServer (openajar.mcp.registry) with the invoke's
`grant_token` bound as the request token, plus the policy package's binder, so the policy
wrapper resolves and verifies the grant on this node (signature, expiry, revocation, scope)
exactly as it would for a call on the node's localhost port. The controller's word on who
the caller is counts for nothing: the principal comes from the verified token. The whole call
runs inside the policy's `channel_call` mark, the one place a controller's short-lived member
call token (a person acting in its panel, subject kind human) is accepted.

Stored result shape (also the `result` body minus `re`):
    {"ok": true, "content": [{"type": "text", "text": ...}, {"type": "image", "mime_type", "data"}]}
    {"ok": false, "content": [], "error": {"code", "message", "approval_id"?}}
Image `data` (base64) is internal: on the wire an image becomes a `stream_id` pushed as a stream.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from contextlib import AbstractContextManager, ExitStack, nullcontext
from typing import Any

from openajar.core.catalog import without_mcp_lines
from openajar.core.errors import EngineError, NotFound, OpenajarError, ServiceUnavailable
from openajar.core.tools import bind_request_token

log = logging.getLogger("openajar.channel.invoke")

CODE_TEXT_RE = re.compile(r"^(?P<code>[a-z][a-z0-9_]{0,40}): (?P<message>.*)$", re.DOTALL)
MAX_ERROR_CHARS = 600


def channel_call(policy: Any, token: str) -> AbstractContextManager:
    """The policy service's mark that `token` is this invoke's grant (PolicyService.channel_call):
    inside it, and nowhere else, a controller's short-lived member call token resolves."""
    mark = getattr(policy, "channel_call", None)
    return mark(token) if mark is not None else nullcontext()


def failure(code: str, message: str, **extra: Any) -> dict:
    error = {"code": code, "message": message[:MAX_ERROR_CHARS]}
    error.update({k: v for k, v in extra.items() if v is not None})
    return {"ok": False, "content": [], "error": error}


def _takes_session(binder: Any) -> bool:
    try:
        params = inspect.signature(binder).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "session" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def _pending(result: Any) -> dict | None:
    """The policy wrapper's (or the registry's) `approval_pending` result, if this is one."""
    data = getattr(result, "structured_content", None)
    if not isinstance(data, dict):
        texts = [b.text for b in result.content if getattr(b, "type", None) == "text"]
        if len(texts) != 1:
            return None
        try:
            data = json.loads(texts[0])
        except ValueError:
            return None
    if isinstance(data, dict) and data.get("status") == "approval_pending" and isinstance(data.get("approval_id"),
                                                                                         str):
        return data
    return None


def from_mcp_result(result: Any) -> dict:
    """A CallToolResult as the stored result shape."""
    content: list[dict] = []
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            content.append({"type": "text", "text": block.text})
        elif kind == "image":
            content.append({"type": "image", "mime_type": block.mime_type, "data": block.data})
    if getattr(result, "is_error", False):
        text = without_mcp_lines(next((c["text"] for c in content if c["type"] == "text"), ""))
        match = CODE_TEXT_RE.match(text)
        if match:
            return failure(match.group("code"), match.group("message"))
        return failure(EngineError.code, text or "the tool failed")
    pending = _pending(result)
    if pending is not None:
        summary = str(pending.get("summary") or "a human must approve this call first")
        out = failure("approval_pending", summary, approval_id=pending["approval_id"])
        out["content"] = content
        return out
    return {"ok": True, "content": content}


async def run_tool(runtime: Any, tool: str, args: dict, *, token: str, session: str | None) -> dict:
    """Run `tool` through the node's MCP server with `token` bound. Never raises for a tool
    failure; returns the stored result shape."""
    server = runtime.get("mcp")
    if server is None:
        raise ServiceUnavailable("this node is not serving MCP tools yet")
    specs = getattr(server, "specs", {})
    if tool not in specs:
        raise NotFound(f"no tool named {tool!r} on this node")
    policy = runtime.get("policy")
    hooks = runtime.get("mcp_hooks")
    binder = getattr(hooks, "bind_token", None)
    with ExitStack() as stack:
        stack.enter_context(channel_call(policy, token))
        if policy is not None:
            # The real reason a grant is refused (revoked, expired, out of the offline rule), as the
            # node's HTTP layer refuses a bad bearer; tools/call would only say the tool is not there.
            policy.resolve(token)
        stack.enter_context(bind_request_token(token))
        if binder is not None:
            if _takes_session(binder):
                stack.enter_context(binder(token, session=session))
            else:
                stack.enter_context(binder(token))
        try:
            result = await server.call_tool(tool, args)
        except OpenajarError:
            raise
        except Exception as exc:  # noqa: BLE001  (the SDK raises for a few argument errors)
            message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            match = CODE_TEXT_RE.match(message)
            if match:
                return failure(match.group("code"), match.group("message"))
            log.warning("tool %s raised %s through the channel", tool, type(exc).__name__)
            return failure("invalid", message)
    return from_mcp_result(result)
