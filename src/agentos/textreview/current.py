"""The agent-facing text as it is now, in the four parts a review may edit, and the files of a variant.

- `instructions`: the MCP server instructions an agent reads once, at connect.
- `tools`: each tool's description, as written in its ToolSpec (Args section included).
- `recipes`: the `howto` recipes' title, summary and steps.
- `errors`: the error catalog's `agent` line: the next step every error of a code carries.

The variant files are the format `agentos lanebench run --variant DIR` reads: instructions.md, and
tools.json, recipes.json and errors.json, each a JSON object keyed like the parts above.
"""

from __future__ import annotations

import enum
import json
import logging
import types
import typing
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("agentos.textreview")

RECIPE_FIELDS = ("title", "summary", "steps")


@dataclass(frozen=True)
class CurrentText:
    instructions: str
    tools: dict[str, str]
    recipes: dict[str, dict[str, str]]
    errors: dict[str, str]
    # tool -> argument -> the values its type allows (a Literal, an Enum or a bool): the only
    # argument values evidence ever shows, because they cannot hold a page's or a person's text
    enums: dict[str, dict[str, frozenset[str]]] = field(default_factory=dict)

    def merged(self, edits: dict[str, Any]) -> CurrentText:
        """This text with an edit applied: anything the edit leaves out stays as it is."""
        recipes = {topic: dict(fields) for topic, fields in self.recipes.items()}
        for topic, fields in (edits.get("recipes") or {}).items():
            recipes[topic] = {**recipes.get(topic, {}), **fields}
        return CurrentText(instructions=edits.get("instructions") or self.instructions,
                           tools={**self.tools, **(edits.get("tools") or {})}, recipes=recipes,
                           errors={**self.errors, **(edits.get("errors") or {})}, enums=self.enums)

    def files(self) -> dict[str, str]:
        """The variant: every file whole, whatever changed."""
        return {"instructions.md": self.instructions,
                "tools.json": json.dumps(self.tools, indent=1, sort_keys=True) + "\n",
                "recipes.json": json.dumps(self.recipes, indent=1, sort_keys=True) + "\n",
                "errors.json": json.dumps(self.errors, indent=1, sort_keys=True) + "\n"}


def _choices(annotation: Any) -> frozenset[str] | None:
    """The values a parameter's type allows, when it is a closed set; None when it is not."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _choices(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        parts = [_choices(arg) for arg in typing.get_args(annotation) if arg is not type(None)]
        return frozenset().union(*parts) if parts and all(p is not None for p in parts) else None
    if origin is typing.Literal:
        values = typing.get_args(annotation)
        return frozenset(str(v) for v in values) if all(isinstance(v, str | bool) for v in values) else None
    if annotation is bool:
        return frozenset({"True", "False"})
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return frozenset(str(member.value) for member in annotation)
    return None


def enum_args(specs: list[Any]) -> dict[str, dict[str, frozenset[str]]]:
    from agentos.mcp.registry import tool_params

    out: dict[str, dict[str, frozenset[str]]] = {}
    for spec in specs:
        try:
            params = tool_params(spec)
        except Exception:  # noqa: BLE001  (a tool whose hints do not resolve shows no values)
            log.debug("the argument types of %s could not be read", spec.name)
            continue
        found = {p.name: choices for p in params if (choices := _choices(p.annotation))}
        if found:
            out[spec.name] = found
    return out


def tool_specs(runtime: Any, *, served_only: bool) -> list[Any]:
    """The tools agents see. In the daemon, what it serves (`served_only`). Anywhere else, every
    tool this build has, plugins' tools included, as `agentos tools` lists them."""
    from agentos.core.tools import ToolSpec
    from agentos.node.daemon import feature_providers, tool_providers
    from agentos.plugins.entries import PLUGINS

    providers = tool_providers()
    if not served_only:
        providers += feature_providers(tuple(m for plugin in PLUGINS for m in plugin.tools))
    specs: dict[str, Any] = {}
    for provider in providers:
        try:
            found = provider(runtime)
        except Exception:  # one feature's tools failing must not stop the review
            log.exception("a tool provider failed while the text review read the tools")
            continue
        for spec in found:
            if not isinstance(spec, ToolSpec) or spec.name in specs:
                continue
            if served_only and spec.hidden_without and not runtime.has(spec.hidden_without):
                continue
            specs[spec.name] = spec
    return list(specs.values())


def collect(runtime: Any, *, served_only: bool) -> CurrentText:
    from agentos.core import catalog
    from agentos.node import tools as node_tools

    specs = tool_specs(runtime, served_only=served_only)
    recipes = {topic: {f: str(recipe[f]) for f in RECIPE_FIELDS if f in recipe}
               for topic, recipe in node_tools.RECIPES.items()}
    return CurrentText(instructions=node_tools.INSTRUCTIONS,
                       tools={spec.name: spec.description or "" for spec in specs},
                       recipes=recipes,
                       errors={code: entry.agent for code, entry in sorted(catalog.entries().items())},
                       enums=enum_args(specs))
