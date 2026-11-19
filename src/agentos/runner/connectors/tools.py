"""MCP tools for connectors (docs/design/connectors.md 4.9).

- One tool per installed operation, named `<site>_<op>` (CompiledOperation.tool_name), for
  example `instagram_post_photo`. Its typed arguments are the operation's input schema (the
  binding params acts_as_id and account_language are never arguments), plus `on`: the surface
  to act as. In tools/list, `on` is an enum of the surfaces bound to this connector that the
  caller's grant can see (ToolSpec.view); a caller that can see none does not get the tool. A
  call binds through the surface (its browser, who it acts as), checks health and pace, asks for
  the operation's exact-content approval and queues the run (runner.submit). The result is the
  run's description, with `output` once it is done.
- `connectors_list`: the installed operations, their tools, versions, contract hashes, and
  the surfaces the caller can see with each one's health.
- `connector_status`: the health of one operation on one surface and its last run.

Like `flow_<id>` tools, the operation tools are built from the flows store: when the MCP server
starts, and again whenever an install, an upgrade or a removal changes the store (agentosd polls
it, agentos.mcp.reload), without a restart. A newly bound surface shows up in `on` at once (the
view reads the store on every tools/list).

A learned operation (agentos.profiles.learn, saved_by kind `learned`) is never a tool: only the
profile sync runs it (RunnerService.learned_problem refuses every other caller), so an agent
changes a profile field by saving the profile, never by calling the site's learned save.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field

from agentos.core.errors import ApprovalPending, InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import Effect, RunStatus
from agentos.core.tools import TOOL_NAME_RE, LaneCheck, ToolContext, ToolSpec, ToolView
from agentos.runner.connectors.models import BINDING_PARAMS
from agentos.runner.connectors.surfaces import dotted, operation_meta
from agentos.runner.connectors.taint import label_context
from agentos.runner.execution import learned_flow
from agentos.runner.flowdoc import FlowVersion
from agentos.runner.names import CONNECTOR_PREFIX
from agentos.runner.tools import (
    DEFAULT_WAIT_S,
    MAX_WAIT_S,
    _py_type,
    grant_of,
    pace_row,
    parse_not_before,
    slot_of,
    twin_answer,
)

log = logging.getLogger("agentos.runner")

ON_HELP = "the surface to act as, like 'biz-a/instagram' (connectors_list shows yours)"
NO_MODEL = "Runs with no model; returns a verified result or an honest status."


def _runner(ctx: ToolContext) -> Any:
    return ctx.runtime.require("runner")


def _allowed(ctx: Any) -> LaneCheck:
    fn = getattr(ctx, "can_access_lane", None)
    return fn if callable(fn) else (lambda _lane: True)


def tool_name(fv: FlowVersion) -> str:
    meta = operation_meta(fv)
    return f"{meta['connector']}_{meta['operation']}".replace("-", "_")  # a site API key may hold a hyphen


def _surfaces_for(runner: Any, fv: FlowVersion, allowed: LaneCheck) -> list[Any]:
    """Surfaces bound to this operation's connector that can run it and `allowed` covers."""
    meta = operation_meta(fv)
    kind = (meta.get("acting_as") or {}).get("kind")
    langs = meta.get("account_languages") or []
    only = meta.get("surfaces")  # a site API runs on the surfaces it names
    return [s for s in runner.surfaces.list(site=meta.get("site") or meta["connector"], allowed=allowed)
            if s.acts_as.kind == kind and s.account_language in langs and (not only or s.id in only)]


def _health(runner: Any, fv: FlowVersion, surface: str) -> str:
    return runner.health.get(dotted(fv), surface)["state"] if runner.health is not None else "unknown"


def describe_operation(runner: Any, fv: FlowVersion, surfaces: list[Any] | None = None) -> str:
    """The node-written tool description: what it does, its effect, who it can act as, health."""
    meta = operation_meta(fv)
    d = fv.definition
    title = (d.title or dotted(fv)).rstrip(".")
    lead = d.description.split(" Connector operation ")[0].strip()
    parts = [f"{title}." + (f" {lead}" if lead else ""),
             f"Connector operation {dotted(fv)} {meta.get('version')}, effect {fv.effect}."]
    if surfaces is not None:
        rows = [f"{s.id} as {s.acts_as.label()} ({_health(runner, fv, s.id)})" for s in surfaces]
        parts.append("Acts as: " + ("; ".join(rows) if rows else "no surface yet") + ".")
    if fv.effect == Effect.COMMIT:
        parts.append("It posts on its own unless Ask me first is on for the account (then the owner approves the "
                     "exact content first); pace limits always apply.")
    parts.append(f"{NO_MODEL} A broken operation answers connector_broken at once: do not post another way.")
    return " ".join(parts)


async def run_operation(ctx: ToolContext, flow_id: str, on: str, params: dict[str, Any] | None,
                        wait_seconds: float = DEFAULT_WAIT_S, idempotency_key: str | None = None,
                        not_before: str | None = None) -> dict[str, Any]:
    """Queue one connector run on a surface, through its binding, its health, its pace and its
    exact-content approval; wait up to wait_seconds for its result."""
    runner = _runner(ctx)
    if not _allowed(ctx)(on):
        raise PolicyDenied(f"this grant does not cover surface {on}")
    fv = runner.flows.get(flow_id)
    start = parse_not_before(not_before, runner.clock.now())
    if idempotency_key:
        existing = runner.existing_run(idempotency_key, flow_id=fv.id, lane=on)
        if existing is not None:
            await ctx.authorize(Effect.READ, lane_id=existing.target)
            return {**runner.describe(existing), "duplicate": True}
    target = runner.connector_target(fv, on, params)
    if idempotency_key:
        return await _start_operation(ctx, runner, fv, on, target, start, wait_seconds, idempotency_key)
    # no key: an identical call while the first run is still open gets that run (QA-2 #11)
    async with runner.twin_guard(fv.id, fv.version, on, target.params, grant_of(ctx), start):
        twin = await runner.open_twin(flow_id=fv.id, version=fv.version, surface=on, params=target.params,
                                      grant_id=grant_of(ctx), not_before=start,
                                      content=runner.surfaces.approval(fv, target)[1])
        if twin is not None:
            await ctx.authorize(Effect.READ, lane_id=twin.target)
            ctx.trace("connector_run", run_id=twin.id, operation=dotted(fv), version=fv.version, surface=on,
                      lane=twin.lane, status="duplicate")
            out = await twin_answer(runner, twin, wait_seconds)
            label_context(ctx, runner.get(twin.id).output, source=f"{tool_name(fv)}:{twin.id}")
            return out
        return await _start_operation(ctx, runner, fv, on, target, start, wait_seconds, None)


async def _start_operation(ctx: ToolContext, runner: Any, fv: FlowVersion, on: str, target: Any,
                           start: datetime | None, wait_seconds: float, idempotency_key: str | None) -> dict[str, Any]:
    runner.check_connector(fv, on)  # broken, pace: before any approval routing
    summary, content = runner.surfaces.approval(fv, target)
    approval_id = decision = None
    try:
        decision = await ctx.authorize(fv.effect, lane_id=on, domain=fv.definition.domain, summary=summary,
                                       content=content, **slot_of(start, runner))  # the pace at the slot
    except ApprovalPending as e:
        approval_id = e.approval_id
    run, created = runner.submit(flow_id=fv.id, version=fv.version, params=target.caller_params, surface=on,
                                 idempotency_key=idempotency_key, principal=ctx.principal, source="tool",
                                 authorized=approval_id is None, approval_id=approval_id, grant_id=grant_of(ctx),
                                 not_before=start, auto=getattr(decision, "route", None) == "auto",
                                 pace_id=pace_row(decision))
    ctx.trace("connector_run", run_id=run.id, operation=dotted(fv), version=fv.version, surface=on,
              lane=target.lane, status=str(run.status))
    if approval_id:
        return {**runner.describe(run), "message": "Waiting for the owner to approve. Check back with run_status."}
    if start is not None and start > runner.clock.now():
        return {**runner.describe(run), "message": f"Queued to start at {start.isoformat()}. Check back with "
                "run_status."}
    if created and wait_seconds > 0:
        run = await runner.wait(run.id, min(float(wait_seconds), MAX_WAIT_S))
    out = runner.describe(run, steps=run.terminal and run.status != RunStatus.SUCCEEDED_VERIFIED)
    label_context(ctx, run.output, source=f"{tool_name(fv)}:{run.id}")
    if not run.terminal:
        out["message"] = "Still going. Check back with run_status."
    if not created:
        out["duplicate"] = True
    return out


def make_operation_handler(fv: FlowVersion, name: str):
    """A handler whose keyword-only signature is the operation's input plus `on` (like
    runner.tools.make_flow_handler, without the binding params)."""
    d = fv.definition
    props = {k: v for k, v in d.params.get("properties", {}).items() if k not in BINDING_PARAMS}
    required = set(d.params.get("required", [])) - set(BINDING_PARAMS)
    sig = [inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD),
           inspect.Parameter("on", inspect.Parameter.KEYWORD_ONLY, annotation=Annotated[str, Field(description=ON_HELP)])]
    for pname, prop in props.items():
        prop = prop if isinstance(prop, dict) else {}
        py = _py_type(prop)
        desc = prop.get("description") or ("a file id or name in the surface browser's inbox (browser_file_put)"
                                            if prop.get("format") == "agentos-file" else None)
        if pname in required and "default" not in prop:
            ann, default = py, inspect.Parameter.empty
        else:
            default = prop.get("default")
            ann = py if default is not None else (py | None if py is not Any else Any)
        if desc:
            ann = Annotated[ann, Field(description=desc)]
        sig.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=ann))
    sig += [inspect.Parameter("wait_seconds", inspect.Parameter.KEYWORD_ONLY, default=DEFAULT_WAIT_S, annotation=float),
            inspect.Parameter("idempotency_key", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None),
            inspect.Parameter("not_before", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None)]
    flow_id = d.id

    async def handler(ctx: ToolContext, **kwargs: Any) -> dict:
        on = kwargs.pop("on")
        wait = kwargs.pop("wait_seconds", DEFAULT_WAIT_S)
        key = kwargs.pop("idempotency_key", None)
        when = kwargs.pop("not_before", None)
        params = {k: v for k, v in kwargs.items() if v is not None}
        return await run_operation(ctx, flow_id, on, params, wait, key, when)

    handler.__name__ = handler.__qualname__ = name
    handler.__signature__ = inspect.Signature(sig, return_annotation=dict)
    handler.__annotations__ = {p.name: p.annotation for p in sig[1:]} | {"return": dict}
    return handler


def operation_specs(runtime: Any, taken: set[str] | None = None) -> list[ToolSpec]:
    """One `<site>_<op>` tool per installed connector operation."""
    runner = runtime.get("runner")
    flows = runtime.get("flows")
    if runner is None or flows is None or runner.surfaces is None:
        return []
    used = set(taken or ())
    specs = []
    for fv in sorted(flows.list(CONNECTOR_PREFIX.rstrip(".")), key=lambda f: f.id):
        if not fv.definition.connector or learned_flow(fv):
            continue  # a learned operation runs only for the profile sync
        name = tool_name(fv)
        if not TOOL_NAME_RE.match(name) or name in used:
            log.error("connector operation %s cannot be served as tool %r (invalid or taken)", fv.id, name)
            continue
        used.add(name)
        handler = make_operation_handler(fv, name)
        specs.append(ToolSpec(name=name, handler=handler, effect=fv.effect, description=describe_operation(runner, fv),
                              lane_arg="on", tags=("connectors", "connector"), hidden_without="runner",
                              view=_viewer(runner, fv.id)))
    return specs


def _viewer(runner: Any, flow_id: str):
    def view(allowed: LaneCheck) -> ToolView | None:
        try:
            fv = runner.flows.get(flow_id)  # the head now: a newer version may be installed
        except NotFound:
            return None
        if learned_flow(fv):
            return None
        surfaces = _surfaces_for(runner, fv, allowed)
        if not surfaces:
            return None
        return ToolView(choices={"on": tuple(s.id for s in surfaces)},
                        description=describe_operation(runner, fv, surfaces))

    return view


# ---------------------------------------------------------------- static tools


def _row(runner: Any, fv: FlowVersion, allowed: LaneCheck) -> dict[str, Any]:
    meta = operation_meta(fv)
    surfaces = []
    for s in _surfaces_for(runner, fv, allowed):
        h = runner.health.get(dotted(fv), s.id) if runner.health is not None else {"state": "unknown"}
        row = {"surface": s.id, "acts_as": s.acts_as.label(), "account_language": s.account_language,
               "health": h["state"]}
        if allowed(s.lane):
            row["lane"] = s.lane
        if h["state"] in ("broken", "held"):
            row["health_detail"] = {k: h.get(k) for k in ("since", "code", "step", "run_id") if h.get(k)}
        surfaces.append(row)
    return {"tool": None if learned_flow(fv) else tool_name(fv), "operation": dotted(fv), "title": fv.definition.title, "effect": str(fv.effect),
            "version": meta.get("version"), "contract": meta.get("contract"), "implementation": fv.version,
            "acts_as": (meta.get("acting_as") or {}).get("kind"), "pace": meta.get("pace") or {},
            "surfaces": surfaces}


async def connectors_list(ctx: ToolContext, *, site: str | None = None) -> dict:
    """List installed connector operations: each one's tool (<site>_<op>; none for a learned
    profile operation, which only the profile sync runs), effect, version and contract hash, and
    the surfaces you can act as with it, each with its health (healthy, broken, held or unknown).
    Cheap; reads no page.

    Args:
        site: only this connector, like 'instagram'.
    """
    runner = _runner(ctx)
    await ctx.authorize(Effect.READ)
    allowed = _allowed(ctx)
    ops = [fv for fv in runner.flows.list(CONNECTOR_PREFIX.rstrip(".")) if fv.definition.connector
           and (site is None or fv.definition.connector.get("connector") == site)]
    rows = [_row(runner, fv, allowed) for fv in sorted(ops, key=lambda f: f.id)]
    out: dict[str, Any] = {"operations": rows}
    if not rows:
        out["hint"] = "No connector is installed on this node (agentos connector install)."
    elif not any(r["surfaces"] for r in rows):
        out["hint"] = "No surface you can act as is bound to these connectors yet; the owner binds them."
    return out


async def connector_status(ctx: ToolContext, *, name: str, on: str) -> dict:
    """The health of one connector operation on one surface, and its last run there.

    Args:
        name: the operation, like 'instagram.post_photo', or its tool name 'instagram_post_photo'.
        on: the surface, like 'biz-a/instagram'.
    """
    runner = _runner(ctx)
    await ctx.authorize(Effect.READ, lane_id=on)
    fv = _find(runner, name)
    health = runner.health.get(dotted(fv), on) if runner.health is not None else {"state": "unknown"}
    last = next((r for r in runner.list(flow_id=fv.id, limit=200) if r.surface == on), None)
    return {"operation": dotted(fv), "tool": tool_name(fv), "surface": on, "health": health,
            "last_run": runner.describe(last) if last else None}


def _find(runner: Any, name: str) -> FlowVersion:
    want = name.strip()
    for fv in runner.flows.list(CONNECTOR_PREFIX.rstrip(".")):
        if fv.definition.connector and want in (dotted(fv), tool_name(fv), fv.id):
            return fv
    raise InvalidRequest(f"no connector operation {name!r} on this node (connectors_list shows them)")


STATIC = [
    ("connectors_list", connectors_list, None),
    ("connector_status", connector_status, "on"),
]


def tool_specs(runtime: Any, taken: set[str] | None = None) -> list[ToolSpec]:
    specs = [ToolSpec(name=n, handler=h, effect=Effect.READ, lane_arg=lane, tags=("connectors",),
                      hidden_without="runner") for n, h, lane in STATIC]
    if runtime is not None and runtime.get("flows") is not None:
        specs += operation_specs(runtime, taken=(set(taken or ()) | {s.name for s in specs}))
    return specs
