"""The tool reference, generated from the same ToolSpecs the MCP server serves.

`agentos tools` prints it and `make tools-doc` writes docs/TOOLS.md; CI checks the file is
current, so the reference cannot drift from the tools agents actually see.
"""

from __future__ import annotations

import enum
import inspect
import types
import typing
from collections.abc import Iterable
from typing import Any

from agentos.core.tools import ToolSpec
from agentos.mcp.registry import tool_description, tool_params
from agentos.mcp.toolsets import (
    CALL_DESCRIPTION,
    CORE,
    OTHER,
    QUERY_PARAM,
    TOOLSETS,
    more_description,
    toolset_of,
)


def type_name(annotation: Any) -> str:
    """A short, readable type: `str`, `int | None`, `list[str]`, `click | type | ...` for enums."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return type_name(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        return " | ".join(type_name(a) for a in typing.get_args(annotation))
    if origin is typing.Literal:
        return " | ".join(repr(a) for a in typing.get_args(annotation))
    if origin is not None:
        args = ", ".join(type_name(a) for a in typing.get_args(annotation))
        return f"{getattr(origin, '__name__', str(origin))}[{args}]" if args else getattr(origin, "__name__", "")
    if annotation is type(None):
        return "None"
    if inspect.isclass(annotation) and issubclass(annotation, enum.Enum):
        return " | ".join(str(m.value) for m in annotation)
    return getattr(annotation, "__name__", str(annotation))


def _one_line(text: str) -> str:
    return " ".join(text.split())


def reference_rows(specs: Iterable[ToolSpec]) -> list[dict[str, Any]]:
    rows = []
    for spec in sorted(specs, key=lambda s: s.name):
        rows.append({
            "name": spec.name,
            "toolset": toolset_of(spec),
            "effect": spec.effect.value,
            "tags": list(spec.tags),
            "needs": spec.hidden_without,
            "description": tool_description(spec),
            "params": [{"name": p.name, "type": type_name(p.annotation), "required": p.required,
                        "default": None if p.required else repr(p.default), "description": p.description or ""}
                       for p in tool_params(spec)],
        })
    return rows


def toolsets_markdown(rows: list[dict[str, Any]]) -> list[str]:
    """The toolsets section: what an agent sees by default and how it reaches the rest."""
    members: dict[str, list[str]] = {}
    for r in rows:
        members.setdefault(r["toolset"], []).append(f"`{r['name']}`")
    out = [
        "## Toolsets",
        "",
        f"An agent's tool list starts small: the `{CORE}` toolset (the everyday lane loop) plus two meta tools.",
        "Every other tool is in a named toolset and stays reachable:",
        "",
        "- `tools_more(toolset)` returns a toolset's tools with their descriptions and argument schemas, adds",
        "  them to the session's list and sends `notifications/tools/list_changed`. `tools_more()` lists every",
        "  toolset. A tool's name works in place of its toolset's.",
        "- `tools_call(name, arguments)` calls any tool by name, listed or not, for clients that do not re-read",
        "  their list. It is the same call as calling the tool directly.",
        f"- The MCP URL can ask for toolsets per connection: `/mcp?{QUERY_PARAM}=files,flows`, or",
        f"  `/mcp?{QUERY_PARAM}=full` for every tool and no meta tools (the list before toolsets).",
        "- `mcp_toolsets` in node.toml sets what a connection that does not ask gets (default `[\"core\"]`;",
        "  `[\"full\"]` keeps the old list for every agent). A controller's combined `/mcp` works the same way,",
        "  with `mcp_toolsets` in controller.toml; its own `nodes_list` is always listed.",
        "- `agentos connect <host> --toolsets files,flows` (or `full`, or `default`) puts the choice on the URL it",
        "  registers with Claude Code, and later runs keep it.",
        "",
        "Toolsets change what tools/list shows, never what a caller may do: every call, direct or through",
        "tools_call, goes through the same grant check, argument validation, approvals and redaction, and",
        "tools_more lists only tools the caller's grant can see. Connector operations (`<site>_<op>`) land in",
        "`connectors` and saved flows (`flow_<name>`) in `flows`, also when they are added while the node runs.",
        "",
        "| Toolset | What it holds | Tools |",
        "|---|---|---|",
    ]
    for ts in TOOLSETS:
        tools = members.get(ts.name, [])
        dynamic = {"connectors": ", and every `<site>_<op>`", "flows": ", and every `flow_<name>`"}.get(ts.name, "")
        out.append(f"| `{ts.name}` | {ts.summary} | {', '.join(tools)}{dynamic} |")
    if members.get(OTHER):
        out.append(f"| `{OTHER}` | tools no other toolset holds | {', '.join(members[OTHER])} |")
    out += [
        "",
        "### tools_more",
        "",
        "Meta tool, listed whenever the list is not `full`. Effect: none (it lists tools).",
        "",
        more_description(),
        "",
        "| Argument | Type | Required | Description |",
        "|---|---|---|---|",
        "| `toolset` | `str` | no | A toolset name, or a tool's name to load its toolset. |",
        "",
        "### tools_call",
        "",
        "Meta tool, listed whenever the list is not `full`. Effect: that of the tool it calls.",
        "",
        CALL_DESCRIPTION,
        "",
        "| Argument | Type | Required | Description |",
        "|---|---|---|---|",
        "| `name` | `str` | yes | The tool to call, for example files_put. |",
        "| `arguments` | `dict` | no | The tool's arguments, as you would pass them. |",
    ]
    return out


def reference_markdown(specs: Iterable[ToolSpec]) -> str:
    rows = reference_rows(specs)
    out = [
        "# MCP tools",
        "",
        "Generated from the ToolSpecs by `make tools-doc` (`agentos tools --markdown`). Do not edit by hand.",
        "Every call goes through the node's policy: the effect is the default class a call is checked",
        "against, and a handler may ask for a stricter one (a click that publishes is commit).",
        "",
        *toolsets_markdown(rows),
        "",
        "## Every tool",
        "",
        "| Tool | Toolset | Effect | Summary |",
        "|---|---|---|---|",
    ]
    for r in rows:
        summary = _one_line(r["description"]).split(". ")[0].rstrip(".").replace("|", "\\|")
        out.append(f"| [`{r['name']}`](#{r['name']}) | {r['toolset']} | {r['effect']} | {summary} |")
    for r in rows:
        out += ["", f"## {r['name']}", "", f"Toolset: `{r['toolset']}`. Effect: `{r['effect']}`"
                + (f". Needs the `{r['needs']}` service." if r["needs"] else "."), ""]
        out.append(r["description"].strip() or "(no description)")
        if r["params"]:
            out += ["", "| Argument | Type | Required | Description |", "|---|---|---|---|"]
            for p in r["params"]:
                req = "yes" if p["required"] else f"no (default {p['default']})"
                desc = _one_line(p["description"]).replace("|", "\\|")
                out.append(f"| `{p['name']}` | `{p['type'].replace('|', '\\|')}` | {req} | {desc} |")
    return "\n".join(out) + "\n"
