"""One MCP tool list across every node of an org, and where each call goes.

Lane ids across nodes (docs/CONTROLLER.md):
- A lane id is `workspace/name`, the same id the node uses. When exactly one online node of
  the org has that lane, the plain id routes to it. That is the normal case: a workspace's
  lanes live on one node.
- When two nodes have the same lane id, the plain id is ambiguous and the call fails with
  `invalid`, naming the qualified ids: `node/workspace/name` (the node's name first; three
  segments can never be a node-local id). The qualified id routes to that node and reaches
  it as plain `workspace/name`.
- Every tool also takes an optional `node` argument (a node name) that picks the node
  outright. A tool with no lane routes by `node`, else by an `approval_id` argument the
  controller has seen, else to the only online node that offers it.

Node-supplied tool definitions are untrusted: names, sizes and schema shapes are checked,
and anything odd is dropped rather than shown to an agent.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from openajar.core.models import LANE_ID_RE, Effect
from openajar.core.tools import TOOL_NAME_RE

NODE_ARG = "node"
NODE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
QUALIFIED_LANE_RE = re.compile(r"^(?P<node>[a-z0-9][a-z0-9-]{0,30})/(?P<lane>[a-z0-9][a-z0-9-]{0,30}/"
                               r"[a-z0-9][a-z0-9-]{0,40})$")
MAX_TOOLS = 500
RESERVED = frozenset({"nodes_list", "tools_more", "tools_call"})  # the controller's own tool names
MAX_SCHEMA_CHARS = 20_000
NODE_ARG_SCHEMA = {"type": "string", "description": "Which node runs this call (a node name from nodes_list). "
                                                    "Needed only when several nodes could."}


class RouteError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict
    effect: Effect = Effect.READ
    lane_arg: str | None = None
    annotations: dict | None = None
    tags: list[str] = field(default_factory=list)


def parse_tools(value: Any) -> dict[str, ToolDef]:
    """Tool definitions from a node's `event { kind: "node", payload: { tools } }`."""
    out: dict[str, ToolDef] = {}
    if not isinstance(value, list):
        return out
    for item in value[:MAX_TOOLS]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        schema = item.get("input_schema")
        if not isinstance(name, str) or not TOOL_NAME_RE.match(name) or name in RESERVED:
            continue
        if not isinstance(schema, dict) or schema.get("type", "object") != "object" or len(str(schema)) > \
                MAX_SCHEMA_CHARS:
            continue
        properties = schema.get("properties") or {}
        if not isinstance(properties, dict) or NODE_ARG in properties:
            continue
        try:
            effect = Effect(item.get("effect") or "read")
        except ValueError:
            continue
        lane_arg = item.get("lane_arg")
        if not (isinstance(lane_arg, str) and lane_arg in properties):
            lane_arg = None
        annotations = item.get("annotations") if isinstance(item.get("annotations"), dict) else None
        tags = [t for t in item.get("tags") or [] if isinstance(t, str) and len(t) <= 40][:20]
        description = item.get("description") if isinstance(item.get("description"), str) else ""
        out[name] = ToolDef(name, description[:4000], schema, effect, lane_arg, annotations, tags)
    return out


@dataclass
class NodeView:
    """What routing needs to know about one online node."""
    node_id: str
    name: str
    tools: dict[str, ToolDef]
    lanes: set[str]


def canonical(name: str, nodes: list[NodeView]) -> ToolDef | None:
    """The one definition of a tool the agent sees and routing reads, whatever order the nodes
    connected in: the first by node name among the nodes that declare a lane argument for it,
    else the first by node name."""
    offering = sorted((n for n in nodes if name in n.tools), key=lambda n: n.name)
    with_lane = [n for n in offering if n.tools[name].lane_arg]
    chosen = (with_lane or offering)[:1]
    return chosen[0].tools[name] if chosen else None


def union(nodes: list[NodeView]) -> dict[str, tuple[ToolDef, list[str]]]:
    """Tool name -> (its canonical definition, names of the nodes offering it)."""
    offered: dict[str, list[str]] = {}
    for node in sorted(nodes, key=lambda n: n.name):
        for name in node.tools:
            offered.setdefault(name, []).append(node.name)
    out: dict[str, tuple[ToolDef, list[str]]] = {}
    for name, names in offered.items():
        tool = canonical(name, nodes)
        if tool is not None:
            out[name] = (tool, names)
    return out


def public_schema(tool: ToolDef, node_names: list[str]) -> dict:
    schema = copy.deepcopy(tool.input_schema)
    properties = schema.setdefault("properties", {})
    properties[NODE_ARG] = {**NODE_ARG_SCHEMA, "enum": sorted(node_names)}
    if tool.lane_arg and tool.lane_arg in properties:
        lane = properties[tool.lane_arg]
        if isinstance(lane, dict):
            lane.pop("pattern", None)  # the qualified form node/workspace/name is accepted too
            note = ' When two nodes have this lane, use "node/workspace/name".'
            lane["description"] = (lane.get("description") or "Lane id as \"workspace/name\".") + note
    return schema


def public_description(tool: ToolDef, node_names: list[str]) -> str:
    where = ", ".join(sorted(node_names))
    return f"{tool.description}\n\n(Runs on node{'s' if len(node_names) > 1 else ''} {where}.)".strip()


def qualified_lanes(nodes: list[NodeView]) -> dict[str, list[str]]:
    """Plain lane id -> the names of the online nodes that have it."""
    out: dict[str, list[str]] = {}
    for node in nodes:
        for lane in node.lanes:
            out.setdefault(lane, []).append(node.name)
    return out


def lane_label(lane: str, node: str, holders: dict[str, list[str]]) -> str:
    """The id an agent should use for this lane: plain when unique, qualified otherwise."""
    return f"{node}/{lane}" if len(holders.get(lane, [])) > 1 else lane


def route(name: str, arguments: dict, nodes: list[NodeView], *,
          approval_node: Any = None) -> tuple[NodeView, dict]:
    """(the node to run on, the arguments to send). Raises RouteError with what to do instead."""
    offering = [n for n in nodes if name in n.tools]
    if not offering:
        raise RouteError(f"no online node offers {name}; call nodes_list to see what is connected")
    args = dict(arguments)
    wanted = args.pop(NODE_ARG, None)
    by_name = {n.name: n for n in offering}
    target: NodeView | None = None
    if wanted is not None:
        if not isinstance(wanted, str) or wanted not in by_name:
            raise RouteError(f"node {str(wanted)[:40]!r} is not online or does not offer {name}; nodes that do: "
                             + ", ".join(sorted(by_name)))
        target = by_name[wanted]
    tool = canonical(name, offering)
    lane_arg = tool.lane_arg if tool is not None else None
    lane = args.get(lane_arg) if lane_arg else None
    if isinstance(lane, str):
        qualified = QUALIFIED_LANE_RE.match(lane)
        if qualified and not LANE_ID_RE.fullmatch(lane):
            node_name, plain = qualified.group("node"), qualified.group("lane")
            if node_name not in by_name:
                raise RouteError(f"node {node_name} is not online or does not offer {name}")
            if target is not None and target.name != node_name:
                raise RouteError(f"the lane {lane} is on node {node_name}, not {target.name}")
            target = by_name[node_name]
            args[lane_arg] = plain
        elif target is None:
            holders = [n for n in offering if lane in n.lanes]
            if len(holders) == 1:
                target = holders[0]
            elif len(holders) > 1:
                options = ", ".join(f"{n.name}/{lane}" for n in sorted(holders, key=lambda n: n.name))
                raise RouteError(f"lane {lane} exists on several nodes; use one of {options}")
    if target is None and approval_node is not None:
        approval_id = args.get("approval_id")
        if isinstance(approval_id, str):
            node_id = approval_node(approval_id)
            target = next((n for n in offering if n.node_id == node_id), None)
    if target is None:
        if len(offering) == 1:
            target = offering[0]
        else:
            raise RouteError(f"{len(offering)} nodes offer {name}; pass node with one of "
                             + ", ".join(sorted(by_name)))
    return target, args
