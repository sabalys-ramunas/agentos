"""The controller's MCP endpoint: one URL whose tools are the union of the org's online nodes.

An agent sends `Authorization: Bearer <agent token>`. The token maps to an agent grant
(agentos.controller.grants); the tool list is filtered by that grant's ceiling and specials,
the way a node filters its own list. Each tool keeps the node's schema plus an optional
`node` argument, and calls route as agentos.controller.routing describes. For every call the
controller mints a short-lived grant token signed with the org key and sends `invoke`; the
node verifies that grant itself and runs the call through its own policy wrapper, so the
controller's filtering is a convenience, never the enforcement.

The controller adds one tool of its own, `nodes_list`, and lists tools by toolset the way a node
does (agentos.mcp.toolsets): `core` plus `nodes_list` and the meta tools `tools_more` and
`tools_call` by default, what the URL asks for with `?toolsets=`, or every tool with `full`.
controller.toml's `mcp_toolsets` sets the default. As on a node, a toolset only changes the list:
a call, direct or through tools_call, takes the same grant check and routing.
"""

from __future__ import annotations

import base64
import contextvars
import json
import logging
from typing import Any

from mcp import types as mcp_types
from mcp.server.mcpserver import MCPServer
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    ImageContent,
    Prompt,
    PromptArgument,
    PromptMessage,
    TextContent,
    Tool,
    ToolAnnotations,
)
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from agentos import __version__
from agentos.controller import guide, routing
from agentos.controller.grants import AgentGrant
from agentos.core import http_errors
from agentos.mcp import toolsets as ts
from agentos.mcp.registry import (
    APPROVAL_NEXT,
    advertise_tools_changed,
    lean_schema,
    tools_changed,
    unknown_tool,
    with_catalog_lines,
)

log = logging.getLogger("agentos.controller.mcp")

current_grant: contextvars.ContextVar[AgentGrant | None] = contextvars.ContextVar("agentos_controller_grant",
                                                                                   default=None)
current_thread: contextvars.ContextVar[str | None] = contextvars.ContextVar("agentos_controller_thread",
                                                                            default=None)

INSTRUCTIONS = guide.instructions([])  # before any node has sent its own (agentos.controller.guide)
NODES_LIST = Tool(
    name="nodes_list",
    description="The nodes connected to this controller, with their lanes. Use a node's name as the `node` "
                "argument of any tool, and the lane ids exactly as shown (\"node/workspace/name\" only appears "
                "when two nodes have the same lane).",
    input_schema={"type": "object", "properties": {}},
    annotations=ToolAnnotations(read_only_hint=True),
)


def error_result(text: str) -> CallToolResult:
    """`<code>: <message>`, plus the error catalog's lines for the agent (as on a node)."""
    return CallToolResult(content=[TextContent(type="text", text=with_catalog_lines(text))], is_error=True)


def to_result(tool: str, body: dict, images: list[bytes | None], approvals_url: str) -> CallToolResult:
    error = body.get("error")
    if not body.get("ok") and isinstance(error, dict):
        if error.get("code") == "approval_pending" and error.get("approval_id"):
            data = {"status": "approval_pending", "approval_id": error["approval_id"], "tool": tool,
                    "summary": error.get("message") or "", "approve_at": approvals_url, "next": APPROVAL_NEXT}
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, indent=2))],
                                  structured_content=data)
        return error_result(f"{error.get('code') or 'error'}: {error.get('message') or ''}".strip())
    blocks: list[TextContent | ImageContent] = []
    for item in body.get("content") or []:
        if item.get("type") == "text":
            blocks.append(TextContent(type="text", text=item["text"]))
        elif item.get("type") == "image":
            index = item.get("image")
            data = images[index] if isinstance(index, int) and 0 <= index < len(images) else None
            if data is None:
                blocks.append(TextContent(type="text", text="(an image could not be delivered)"))
            else:
                blocks.append(ImageContent(type="image", data=base64.b64encode(data).decode(),
                                           mime_type=item.get("mime_type") or "image/png"))
    structured = None
    if len(blocks) == 1 and isinstance(blocks[0], TextContent):
        try:
            parsed = json.loads(blocks[0].text)
        except ValueError:
            parsed = None
        structured = parsed if isinstance(parsed, dict) else None
    return CallToolResult(content=blocks, structured_content=structured)


class ControllerMcp(MCPServer):
    def __init__(self, controller: Any):
        super().__init__("agentos-controller", version=__version__, instructions=INSTRUCTIONS, log_level="WARNING")
        self.controller = controller
        toolsets = getattr(getattr(controller, "config", None), "mcp_toolsets", [ts.CORE])
        self.default_toolsets = ts.parse_toolsets(toolsets)
        self.loaded = ts.Loaded()
        advertise_tools_changed(self)
        lowlevel = self._lowlevel_server
        base_options = lowlevel.create_initialization_options

        def init_options(*args: Any, **kwargs: Any) -> Any:  # read at each client's initialize
            return base_options(*args, **kwargs).model_copy(update={"instructions": self.instructions_for(
                current_grant.get())})

        lowlevel.create_initialization_options = init_options  # type: ignore[method-assign]
        base_discover = lowlevel._handle_discover

        async def discover(ctx: Any, params: Any) -> Any:  # the modern handshake reads them here
            result = await base_discover(ctx, params)
            return result.model_copy(update={"instructions": self.instructions_for(current_grant.get())})

        lowlevel.add_request_handler("server/discover", mcp_types.RequestParams, discover)

    # ---- the nodes' instructions and prompts (agentos.controller.guide) ----
    def _guides(self, grant: AgentGrant | None) -> list[dict]:
        return guide.of_sessions(self.controller.hub.online(grant.org_id)) if grant is not None else []

    def instructions_for(self, grant: AgentGrant | None) -> str:
        return guide.instructions(self._guides(_live(grant)))

    async def list_prompts(self) -> list[Prompt]:
        return [Prompt(name=p["name"], title=p["title"], description=p["description"],
                       arguments=[PromptArgument(name=a["name"], description=a["description"], required=a["required"])
                                  for a in p["arguments"]])
                for p in guide.prompts(self._guides(_live(current_grant.get()))).values()]

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None, context: Any = None) -> Any:
        grant = current_grant.get()
        prompt = guide.prompts(self._guides(grant)).get(name) if grant is not None and grant.live else None
        if prompt is None:
            raise ValueError(f"Unknown prompt: {name}")
        return GetPromptResult(description=prompt["description"], messages=[
            PromptMessage(role="user", content=TextContent(type="text", text=guide.render(prompt, arguments)))])

    def _tools(self, grant: AgentGrant) -> dict[str, tuple[routing.ToolDef, list[str]]]:
        views = self.controller.hub.views(grant.org_id)
        allowed = grant.policy_grant()
        lane_scope = allowed.has_lane_scope()
        out = {}
        for name, (tool, nodes) in routing.union(views).items():
            if not allowed.effect_allowed(tool.effect):
                continue
            if tool.lane_arg and not lane_scope:
                continue
            out[name] = (tool, nodes)
        return out

    def shown_toolsets(self, grant: AgentGrant) -> frozenset[str] | None:
        """What this connection's list shows (None: every tool): its URL's toolsets, else the
        controller's default, plus what it loaded with tools_more in this MCP session."""
        connection = ts.current_connection()
        base = self.default_toolsets if connection.toolsets is None else ts.parse_query(connection.toolsets)
        if base is None:
            return None
        return base | self.loaded.get(grant.id, connection.session)

    @staticmethod
    def public_tool(name: str, tool: routing.ToolDef, nodes: list[str]) -> Tool:
        annotations = None
        if tool.annotations:
            try:
                annotations = ToolAnnotations.model_validate(tool.annotations)
            except ValueError:
                annotations = None
        return Tool(name=name, description=routing.public_description(tool, nodes),
                    input_schema=lean_schema(routing.public_schema(tool, nodes)), annotations=annotations)

    def groups(self, grant: AgentGrant) -> dict[str, list[Tool]]:
        """Every tool this grant may use, as listed, by toolset."""
        out: dict[str, list[Tool]] = {}
        for name, (tool, nodes) in sorted(self._tools(grant).items()):
            out.setdefault(ts.toolset_of(tool), []).append(self.public_tool(name, tool, nodes))
        return out

    async def list_tools(self) -> list[Tool]:
        grant = current_grant.get()
        if grant is None:
            return []
        shown = self.shown_toolsets(grant)
        out = [NODES_LIST]
        for name, tools in self.groups(grant).items():
            if shown is None or name in shown:
                out.extend(tools)
        out[1:] = sorted(out[1:], key=lambda t: t.name)
        return out if shown is None else out + ts.meta_tools()

    async def tools_more(self, grant: AgentGrant, arguments: dict[str, Any], context: Any) -> CallToolResult:
        session = ts.current_connection().session
        result, error, changed = ts.more(self.groups(grant), self.shown_toolsets(grant), arguments.get("toolset"),
                                         lambda key: self.loaded.add(grant.id, session, key))
        if error is not None:
            return error_result(error)
        if changed:
            await tools_changed(context)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, indent=2))],
                              structured_content=result)

    def nodes_list(self, grant: AgentGrant) -> CallToolResult:
        sessions = self.controller.hub.online(grant.org_id)
        holders = routing.qualified_lanes([s.view() for s in sessions])
        allowed = grant.policy_grant()
        nodes = []
        for node in self.controller.store.nodes(grant.org_id):
            session = next((s for s in sessions if s.node_id == node["id"]), None)
            lanes = []
            if session is not None:
                for lane in session.heartbeat.get("lanes", []):
                    if not allowed.lane_allowed(lane["id"]):
                        continue
                    lanes.append({"id": routing.lane_label(lane["id"], session.name, holders), "kind": lane["kind"],
                                  "state": lane["state"], "signal": lane["signal"]})
            nodes.append({"name": node["name"], "online": session is not None,
                          "degraded": session.degraded if session else None,
                          "capabilities": node["capabilities"], "lanes": lanes})
        data = {"nodes": nodes}
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, indent=2))],
                              structured_content=data)

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> CallToolResult:
        grant = current_grant.get()
        if grant is None or not grant.live:
            return error_result("unauthorized: this agent token is not valid")
        if name == ts.TOOLS_MORE:
            return await self.tools_more(grant, arguments or {}, context)
        if name == ts.TOOLS_CALL:  # the same call as a direct one: grant check, freeze, routing
            target = ts.call_target(arguments or {})
            if isinstance(target, str):
                return error_result(target)
            name, arguments = target
        if name == "nodes_list":
            return self.nodes_list(grant)
        mesh = self.controller.store.mesh_freeze(grant.org_id)
        if mesh is not None:  # agentos.freeze: the mesh is frozen; no call goes out to a node
            return error_result(f"frozen: every node is frozen ({mesh['reason']}). Every agent is stopped and its "
                                "grants were revoked. Do not retry; tell your user. Only the owner can unfreeze.")
        tools = self._tools(grant)
        if name not in tools:  # its own fix line: tools_more lists them all, tools/list only the loaded ones
            return CallToolResult(content=[TextContent(type="text", text=unknown_tool(name, tools))], is_error=True)
        hub = self.controller.hub
        try:
            node, args = routing.route(name, arguments or {}, hub.views(grant.org_id),
                                       approval_node=lambda aid: self.controller.store.approval_node(grant.org_id, aid))
        except routing.RouteError as exc:
            return error_result(f"invalid: {exc.message}")
        thread = current_thread.get()
        timeout = float(self.controller.config.invoke_timeout_s)
        principal = {"kind": "agent", "id": grant.subject.get("id"), "thread": thread}
        token = self.controller.grants.mint(grant, deadline=hub.clock() + timeout, thread=thread,
                                            node_id=node.node_id)
        try:
            body, images = await hub.invoke(grant.org_id, node.node_id, name, args, principal=principal,
                                            grant_token=token, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "unavailable")
            message = getattr(exc, "message", None) or f"{type(exc).__name__}"
            return error_result(f"{code}: {message}")
        return to_result(name, body, images, f"{self.controller.config.local_url}/#approvals")


class AgentAuth:
    """Pure ASGI: `/mcp` needs `Authorization: Bearer <agent token>`; binds the grant per request."""

    def __init__(self, app: ASGIApp, grants: Any):
        self.app = app
        self.grants = grants

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = b""
        session = None
        for key, value in scope.get("headers", ()):
            if key == b"authorization":
                header = value
            elif key == b"mcp-session-id":
                session = value.decode("latin-1")[:200]
        scheme, _, token = header.partition(b" ")
        grant = self.grants.resolve(token.strip().decode("latin-1")) if scheme.lower() == b"bearer" else None
        if grant is None:
            response = JSONResponse(http_errors.error_body("unauthorized", "Send Authorization: Bearer <agent token>.",
                                                                           surface="controller_mcp"),
                                    status_code=http_errors.status_of("unauthorized"),
                                    headers=http_errors.WWW_AUTHENTICATE)
            await response(scope, receive, send)
            return
        t1 = current_grant.set(grant)
        t2 = current_thread.set(session)
        try:
            with ts.bind_connection(session=session, query_string=scope.get("query_string", b"")):
                await self.app(scope, receive, send)
        finally:
            current_grant.reset(t1)
            current_thread.reset(t2)


def _live(grant: AgentGrant | None) -> AgentGrant | None:
    return grant if grant is not None and grant.live else None
