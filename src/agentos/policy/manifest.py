"""Tool-manifest hashes, for grants that pin the exact tools they may call."""

from __future__ import annotations

import inspect
from typing import Any

from agentos.core.tools import ToolSpec
from agentos.policy.util import content_hash


def handler_params(spec: ToolSpec) -> list[inspect.Parameter]:
    """The tool's parameters as an agent sees them (everything after `ctx`)."""
    sig = inspect.signature(spec.handler)
    return list(sig.parameters.values())[1:]


def manifest(spec: ToolSpec) -> dict[str, Any]:
    params = []
    for p in handler_params(spec):
        ann = p.annotation
        ann_s = ann if isinstance(ann, str) else getattr(ann, "__name__", repr(ann))
        params.append({"name": p.name, "type": "" if ann is inspect.Parameter.empty else str(ann_s),
                       "required": p.default is inspect.Parameter.empty,
                       "default": None if p.default is inspect.Parameter.empty else repr(p.default)})
    return {"name": spec.name, "description": spec.description or "", "effect": spec.effect.value,
            "lane_arg": spec.lane_arg, "params": params}


def manifest_hash(spec: ToolSpec) -> str:
    """`sha256:<hex>` over the tool's name, description, effect and parameters. A grant that
    pins this hash stops working if the tool changes under it."""
    return content_hash(manifest(spec))


def requires_lane(spec: ToolSpec) -> bool:
    if not spec.lane_arg:
        return False
    for p in handler_params(spec):
        if p.name == spec.lane_arg:
            return p.default is inspect.Parameter.empty
    return False
