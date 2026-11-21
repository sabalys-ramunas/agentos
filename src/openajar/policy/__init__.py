"""Policy: grants, authorization and effect routing, approvals, traces and notifications.

Integration surface (everything else is internal):

    from openajar.policy import build, wrap_tool, visible_tools, bind_request_token, tool_specs

    policy = build(runtime)          # registers policy, approvals, traces, notifier
    await policy.start()             # background: expiry sweep, outbox retry, retention
    with bind_request_token(bearer, session=mcp_session_id):
        result = await wrap_tool(spec, runtime)(**arguments)
    visible = visible_tools(bearer, specs, runtime)
    unsubscribe = runtime.traces.subscribe(lambda kind, payload: ...)   # "trace", "approval", "grant"
"""

from openajar.policy.context import Decision, PolicyContext
from openajar.policy.service import PolicyService, build
from openajar.policy.tools import tool_specs
from openajar.policy.wrap import (
    ToolCallError,
    bind_request_token,
    request_meta,
    request_token,
    visible_tools,
    wrap_tool,
)

__all__ = [
    "Decision",
    "PolicyContext",
    "PolicyService",
    "ToolCallError",
    "bind_request_token",
    "build",
    "request_meta",
    "request_token",
    "tool_specs",
    "visible_tools",
    "wrap_tool",
]
