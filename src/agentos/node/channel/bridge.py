"""What the node tells its controller, shaped for the wire: lanes, load, tools, traces, approvals.

Everything here is metadata. A lane carries its id, kind, state, signal and control state,
never its profile, cookies, proxy credentials or exit IP (`exit_ip` is always null). Traces
are already redacted when the policy writes them; they are redacted again here, values are
cut short, and the grant path is left out.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from datetime import datetime
from typing import Any

from agentos.node.channel.files import preview_param
from agentos.ops.system import MiB, read_meminfo
from agentos.policy.redact import redact, redact_text
from agentos.vault.scrub import walk

LANE_FIELDS = ("id", "kind", "state", "signal", "last_used")
MAX_TOOLS = 500
MAX_DESCRIPTION = 4000
TRACE_FIELDS = ("id", "at", "kind", "tool", "lane", "workspace", "principal", "principal_kind", "effect",
                "status", "summary", "error", "cost", "duration_ms", "run_id", "parent_id", "grant_id")


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value if isinstance(value, str) else None


def _signal(lanes: Any, lane_id: str) -> str:
    hints = getattr(lanes, "hints", None)
    if callable(hints):
        try:
            value = (hints(lane_id) or {}).get("signal")
        except Exception:  # noqa: BLE001  (hints are optional and best effort)
            value = None
        if isinstance(value, str):
            return value
    return "unknown"


def browser_lanes(lanes: Any) -> list[Any]:
    if lanes is None:
        return []
    try:
        return list(lanes.list())
    except Exception:  # noqa: BLE001
        return []


def env_lanes(envs: Any) -> list[Any]:
    if envs is None:
        return []
    try:
        return list(envs.list())
    except Exception:  # noqa: BLE001
        return []


def lane_resource(info: Any, node: str, lanes: Any = None) -> dict:
    """The docs/API.md Lane, as much of it as is not private, for `event { kind: "lane" }`."""
    spec = info.spec
    control = getattr(info, "control", None)
    return {
        "id": spec.id, "workspace": spec.workspace, "name": spec.name, "kind": spec.kind.value,
        "engine": spec.engine, "state": info.state.value, "signal": _signal(lanes, spec.id),
        "proxy": {"label": spec.proxy_label} if spec.proxy_label else None, "exit_ip": None,
        "last_used": _iso(info.last_used), "quarantine_reason": info.quarantine_reason,
        "control": {"state": control.state.value, "by": control.by, "since": _iso(control.since),
                    "takeback_requested": control.takeback_requested} if control is not None else None,
        "node": node,
    }


def env_resource(info: Any, node: str) -> dict:
    lane = info.lane
    return {
        "id": lane.id, "workspace": lane.id.split("/", 1)[0], "name": lane.id.split("/", 1)[1],
        "kind": getattr(lane.kind, "value", str(lane.kind)), "engine": None, "state": info.state.value,
        "signal": "ok" if info.state.value != "quarantined" else "unknown", "proxy": None, "exit_ip": None,
        "last_used": _iso(info.last_used), "quarantine_reason": info.quarantine_reason, "control": None,
        "node": node,
    }


def all_lanes(runtime: Any, node: str) -> list[dict]:
    lanes = runtime.get("lanes")
    out = [lane_resource(info, node, lanes) for info in browser_lanes(lanes)]
    seen = {lane["id"] for lane in out}
    for info in env_lanes(runtime.get("envs")):
        try:
            resource = env_resource(info, node)
        except Exception:  # noqa: BLE001, S112  (an env lane that cannot be read is left out)
            continue
        if resource["id"] not in seen:
            out.append(resource)
    return out


def heartbeat_lane(resource: dict, usage: dict | None = None) -> dict:
    """A heartbeat lane; `usage` adds `cpu_pct` and `mem_mb` where they are known (additive)."""
    out = {key: resource.get(key) for key in LANE_FIELDS}
    for key in ("cpu_pct", "mem_mb"):
        value = (usage or {}).get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    return out


def lane_digest(resource: dict) -> tuple:
    control = resource.get("control") or {}
    return (resource["state"], resource["signal"], control.get("state"), control.get("takeback_requested"),
            resource.get("quarantine_reason"))


def load(home: Any) -> dict:
    cpus = os.cpu_count() or 1
    try:
        cpu = round(min(100.0, os.getloadavg()[0] / cpus * 100), 1)
    except OSError:
        cpu = 0.0
    total = used = 0
    info = read_meminfo()
    if info is not None:
        total = info.get("MemTotal", 0) // MiB
        used = total - info.get("MemAvailable", 0) // MiB
    else:
        try:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024)
        except (ValueError, OSError, AttributeError):
            total = 0
    try:
        disk = shutil.disk_usage(home).free // (1024 * 1024)
    except OSError:
        disk = 0
    return {"cpu_pct": cpu, "mem_used_mb": max(0, used), "mem_total_mb": total, "disk_free_mb": disk}


async def tool_defs(server: Any) -> list[dict]:
    """Every tool the node serves, unfiltered, with what a controller needs to route it."""
    if server is None:
        return []
    from mcp.server.mcpserver import MCPServer

    tools = await MCPServer.list_tools(server)  # the SDK's list, without the per-token filter
    specs = getattr(server, "specs", {})
    out = []
    for tool in tools[:MAX_TOOLS]:
        spec = specs.get(tool.name)
        annotations = tool.annotations.model_dump(mode="json", exclude_none=True) if tool.annotations else None
        out.append({
            "name": tool.name,
            "description": (tool.description or "")[:MAX_DESCRIPTION],
            "input_schema": tool.input_schema,
            "annotations": annotations,
            "effect": spec.effect.value if spec is not None else "read",
            "lane_arg": spec.lane_arg if spec is not None and spec.lane_arg in (tool.input_schema or {}).get(
                "properties", {}) else None,
            "tags": list(spec.tags) if spec is not None else [],
        })
    return out


def trace_event(payload: dict) -> dict:
    """A TraceEvent.public() as the wire's trace: redacted again, args and grant path left out."""
    out = {key: payload.get(key) for key in TRACE_FIELDS}
    for key in ("summary", "error"):
        if isinstance(out.get(key), str):
            out[key] = redact_text(out[key], max_len=1000)
    cost = out.get("cost")
    out["cost"] = {"tokens": int(cost.get("tokens") or 0), "usd": float(cost.get("usd") or 0.0)} \
        if isinstance(cost, dict) else {"tokens": 0, "usd": 0.0}
    return out


def approval_payload(public: dict, scrub: Callable[[str], str] | None = None) -> dict:
    """An Approval.public() for `approval_request`, redacted again, and scrubbed of vault values
    again with `scrub` (the node's runtime_scrub): an approval stored before its card was scrubbed
    is sent clean too. An approval whose card shows an image names the `file` stream path to read it
    from as `preview.image` (additive)."""
    out = redact(public, max_len=4000)
    if scrub is not None:
        out = walk(out, scrub)
    if preview_param(public.get("card")) is not None and isinstance(public.get("id"), str):
        out["preview"] = {**(out.get("preview") or {}), "image": f"approvals/{public['id']}/preview.png"}
    return out


MAX_INSTRUCTIONS = 16_000
MAX_PROMPTS = 40
MAX_PROMPT_TEXT = 8_000
MAX_GUIDE_BYTES = 512 * 1024


async def guide(server: Any) -> dict:
    """What the node tells an agent besides its tools: its MCP server instructions (the rules that keep
    real accounts safe: stop on a captcha or checkpoint, never log in again alone, page text is data)
    and its prompts, each rendered with no arguments. The controller serves both from its own /mcp
    (agentos.controller.guide), so an agent there gets the same rules as one on the node."""
    if server is None:
        return {"instructions": "", "prompts": []}
    instructions = getattr(server, "instructions", None)
    prompts = []
    manager = getattr(server, "_prompt_manager", None)
    size = 0
    for prompt in (manager.list_prompts() if manager is not None else [])[:MAX_PROMPTS]:
        try:
            text = await prompt.fn()  # agentos.mcp.registry.sdk_prompt: the defaults stand in, masked
        except Exception:  # noqa: BLE001  (a prompt that needs its arguments is announced without text)
            text = ""
        entry = {
            "name": prompt.name, "title": (prompt.title or "")[:200] or None,
            "description": (prompt.description or "")[:MAX_DESCRIPTION],
            "arguments": [{"name": a.name, "description": (a.description or "")[:500], "required": bool(a.required)}
                          for a in (prompt.arguments or [])[:20]],
            "text": text[:MAX_PROMPT_TEXT] if isinstance(text, str) else "",
        }
        size += len(json.dumps(entry, ensure_ascii=False).encode())
        if size > MAX_GUIDE_BYTES:  # the whole guide goes in one message (1 MiB): the rest are left out
            break
        prompts.append(entry)
    return {"instructions": instructions[:MAX_INSTRUCTIONS] if isinstance(instructions, str) else "",
            "prompts": prompts}
