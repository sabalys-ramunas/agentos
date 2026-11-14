"""MCP tools for flows and runs, one typed tool per saved flow (`flow_<name>`), and the
connector tools (agentos.runner.connectors.tools: `<site>_<op>`, connectors_list,
connector_status)."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from agentos.core.errors import ApprovalPending, InvalidRequest, NotFound, PolicyDenied, ServiceUnavailable
from agentos.core.models import Effect, RunStatus
from agentos.core.tools import (
    ToolContext,
    ToolSpec,
    covers_workspace,
    pattern_reaches,
    refuse_frozen,
    require_lane_patterns,
)
from agentos.runner.flows import FlowService, lint
from agentos.runner.models import FlowVersion, is_connector_id
from agentos.runner.runner import SAME_RUN
from agentos.runner.util import maybe_await

MAX_WAIT_S = 120.0
DEFAULT_WAIT_S = 15.0
MAX_NOT_BEFORE_DAYS = 30


def _flows(ctx: ToolContext) -> FlowService:
    return ctx.runtime.require("flows")


def saver(ctx: Any) -> dict[str, Any]:
    """Who saves a flow through MCP: always an agent, even one holding the owner's token, so
    its "publishes": false marks never lower a flow (only the panel and the CLI can)."""
    p = ctx.principal
    return {"kind": "agent", "id": p.id, "owner": p.owner, "via": "mcp"}


def flow_seen(ctx: Any, lane: str) -> bool:
    """Does the caller see a flow bound to `lane`: its lane in the grant, or for a pattern, a lane the
    grant reaches that the pattern can match (it could run the flow there)."""
    try:
        return pattern_reaches(ctx, lane) if any(c in lane for c in "*?[") else bool(ctx.can_access_lane(lane))
    except Exception:  # noqa: BLE001 - a lane the policy cannot parse is outside every scope
        return False


def seen_flow(ctx: Any, flow_id: str, version: str | None = None) -> FlowVersion:
    """The flow, or not_found as for a flow that is not there when the caller does not see it, so
    neither its lane nor its params reach a grant outside it."""
    fv = _flows(ctx).get(flow_id, version)
    if not flow_seen(ctx, fv.definition.lane):
        raise NotFound(f"flow {flow_id!r} is not saved on this node (see flows_list)")
    return fv


def check_replace(ctx: Any, flow_id: str) -> None:
    """A new version of a flow that exists is bound where the old one was too: refuse unless the grant
    covers every lane the saved head is bound to, so no grant re-points another workspace's flow."""
    try:
        head = _flows(ctx).get(flow_id)
    except NotFound:
        return
    lane = head.definition.lane
    try:
        require_lane_patterns(ctx, [lane], Effect.DRAFT)
    except PolicyDenied:
        raise PolicyDenied(f"{flow_id} is taken by a flow outside your grant; save under another id") from None


def seen_run(ctx: Any, run_id: str) -> Any:
    """The run, or not_found as for a run that is not there. A run with no lane or surface (a workflow's
    run) is the workflow's workspace's: the grant must cover all of it, or be an admin grant."""
    run = ctx.runtime.require("runner").get(run_id)
    admin = getattr(ctx, "admin", False) is True
    if run.target is not None:
        if admin:
            refuse_frozen(ctx, lanes=[run.target])  # an admin grant sees every run, but not into a freeze
        elif not flow_seen(ctx, run.target):  # its lane is outside the grant: not named back
            raise NotFound(f"run {run_id!r} not found")
        return run
    workflows = ctx.runtime.get("workflows")
    workspace = None
    if workflows is not None and run.flow_id:
        try:
            workspace = workflows.get_version(run.flow_id).definition.workspace
        except NotFound:
            workspace = None
    if admin:
        refuse_frozen(ctx, workspace=workspace)
    elif not covers_workspace(ctx, workspace):
        raise NotFound(f"run {run_id!r} not found")
    return run


def grant_of(ctx: Any) -> str | None:
    """The grant a tool call runs under (a PolicyContext's grant, else the principal's)."""
    gid = getattr(getattr(ctx, "grant", None), "id", None)
    return gid or getattr(getattr(ctx, "principal", None), "grant_id", None)


def parse_not_before(value: str | None, now: datetime) -> datetime | None:
    """An ISO 8601 time with a timezone, at most MAX_NOT_BEFORE_DAYS ahead; None for none."""
    if value is None or value == "":
        return None
    try:
        when = datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise InvalidRequest("not_before is an ISO 8601 time like 2026-11-08T09:00:00+02:00") from None
    if when.tzinfo is None:
        raise InvalidRequest("not_before needs a timezone, like 2026-11-08T09:00:00+02:00")
    if when > now + timedelta(days=MAX_NOT_BEFORE_DAYS):
        raise InvalidRequest(f"not_before is at most {MAX_NOT_BEFORE_DAYS} days ahead")
    return when


def pace_row(decision: Any) -> int | None:
    """The pace ledger row an authorization wrote for a commit (agentos.policy.pace), kept on the run."""
    ids = getattr(decision, "pace_ids", None) or []
    return ids[0] if ids and isinstance(ids[0], int) else None


def slot_of(start: datetime | None, runner: Any) -> dict[str, float]:
    """`at` for ctx.authorize when a run is queued for a later slot (not_before): the lane's posting
    pace is read at that time (agentos.policy.pace), not now. Nothing for a run that goes now."""
    if start is None or start <= runner.clock.now():
        return {}
    return {"at": start.timestamp()}



async def _run_flow(ctx: ToolContext, flow_id: str, params: dict[str, Any] | None, on: str | None,
                    version: str | None, wait_seconds: float, idempotency_key: str | None,
                    not_before: str | None = None) -> dict[str, Any]:
    runner = ctx.runtime.require("runner")
    flows = _flows(ctx)
    start = parse_not_before(not_before, runner.clock.now())
    fv = seen_flow(ctx, flow_id, version)
    lane = flows.resolve_lane(fv, on)
    params = flows.validate_params(fv, params)
    if idempotency_key:
        existing = runner.existing_run(idempotency_key, flow_id=fv.id, lane=lane)
        if existing is not None:
            await ctx.authorize(Effect.READ, lane_id=existing.lane)
            return {**runner.describe(existing), "duplicate": True}
        return await _start_flow(ctx, runner, flows, fv, lane, params, start, wait_seconds, idempotency_key)
    # no key: an identical call while the first run is still open gets that run (QA-2 #11)
    async with runner.twin_guard(fv.id, fv.version, lane, params, grant_of(ctx), start):
        twin = await runner.open_twin(flow_id=fv.id, version=fv.version, lane=lane, params=params,
                                      grant_id=grant_of(ctx), not_before=start,
                                      content=flows.approval_content(fv, lane, params))
        if twin is not None:
            await ctx.authorize(Effect.READ, lane_id=twin.lane)
            ctx.trace("flow_run", run_id=twin.id, flow=fv.id, version=fv.version, lane=lane, status="duplicate")
            return await twin_answer(runner, twin, wait_seconds)
        return await _start_flow(ctx, runner, flows, fv, lane, params, start, wait_seconds, None)


async def twin_answer(runner: Any, twin: Any, wait_seconds: float) -> dict[str, Any]:
    """What a keyless retry of a run still open gets: that run, released now when a person already
    approved it, and waited for as a first call would be."""
    run = await runner.settle(twin)
    if run.status == RunStatus.WAITING_APPROVAL:
        return {**await runner.waiting_view(run), "duplicate": True, "message": SAME_RUN}
    if run.status == RunStatus.QUEUED and run.authorized and wait_seconds > 0 and (
            run.not_before is None or run.not_before <= runner.clock.now()):
        run = await runner.wait(run.id, min(float(wait_seconds), MAX_WAIT_S))
    out = {**runner.describe(run, steps=run.terminal and run.status != RunStatus.SUCCEEDED_VERIFIED),
           "duplicate": True}
    if not run.terminal:
        out["message"] = SAME_RUN
    return out


async def _start_flow(ctx: ToolContext, runner: Any, flows: Any, fv: FlowVersion, lane: str,
                      params: dict[str, Any], start: datetime | None, wait_seconds: float,
                      idempotency_key: str | None) -> dict[str, Any]:
    d = fv.definition
    approval_id = decision = None
    try:  # the effective effect: an agent can never lower a flow below what its steps do
        decision = await ctx.authorize(fv.effect, lane_id=lane, domain=d.domain,
                                       summary=flows.run_summary(fv, lane, params),
                                       content=flows.approval_content(fv, lane, params),
                                       **slot_of(start, runner))  # the lane's pace is read at the slot
    except ApprovalPending as e:
        approval_id = e.approval_id
    run, created = runner.submit(flow_id=fv.id, version=fv.version, params=params, lane=lane,
                                 idempotency_key=idempotency_key, principal=ctx.principal, source="tool",
                                 authorized=approval_id is None, approval_id=approval_id, grant_id=grant_of(ctx),
                                 not_before=start, auto=getattr(decision, "route", None) == "auto",
                                 pace_id=pace_row(decision))
    ctx.trace("flow_run", run_id=run.id, flow=fv.id, version=fv.version, lane=lane, status=str(run.status))
    if approval_id:
        return {**await runner.waiting_view(run), "message": "Waiting for the owner to approve. Once approved it "
                "starts on its own: check back with run_status, no need to call flow_run again."}
    if start is not None and start > runner.clock.now():
        return {**runner.describe(run), "message": f"Queued to start at {start.isoformat()}. Check back with "
                "run_status."}
    if created and wait_seconds > 0:
        run = await runner.wait(run.id, min(float(wait_seconds), MAX_WAIT_S))
    show_steps = run.terminal and run.status != RunStatus.SUCCEEDED_VERIFIED
    out = runner.describe(run, steps=show_steps)
    if not run.terminal:
        out["message"] = "Still going. Check back with run_status."
    if not created:
        out["duplicate"] = True
    return out


async def flows_list(ctx: ToolContext, *, prefix: str | None = None) -> dict:
    """List saved flows by namespace, lazily: with no prefix you get the top namespaces
    (for example 'biz-a'), then call again with a prefix ('biz-a.instagram') to see the
    flows inside. Each flow shows its lane, effect (the effective one: input steps count as
    commit), params, readbacks and its typed tool. Only flows bound to lanes your grant reaches."""
    return _flows(ctx).catalog(prefix, seen=lambda lane: flow_seen(ctx, lane),
                               lane_seen=lambda lane: flow_seen(ctx, lane))


async def flow_get(ctx: ToolContext, *, name: str, version: str | None = None) -> dict:
    """One saved flow's definition: the document flow_save takes, as the node holds it, with its
    version (the content hash), its effective effect and the lint warnings. Read it before you
    repair a flow, change it, then save it again with flow_save. `version` reads an older one.
    Only flows bound to lanes your grant reaches; installed connector operations are not read
    here (connectors_list)."""
    if is_connector_id(name):
        raise PolicyDenied(f"{name} is an installed connector operation; see connectors_list")
    flows = _flows(ctx)
    fv = seen_flow(ctx, name, version)
    await ctx.authorize(Effect.READ, lane_id=None if fv.definition.lane_is_pattern else fv.definition.lane)
    head = flows.get(fv.id)
    out = {"id": fv.id, "version": fv.version, "head": head.version == fv.version, "lane": fv.definition.lane,
           "effect": str(fv.effect), "declared_effect": str(fv.definition.effect),
           "definition": fv.definition.canonical(), "warnings": lint(fv.definition, effect=fv.effect)}
    if fv.effect_reason:
        out["effect_note"] = fv.effect_reason
    return out


async def flow_run(ctx: ToolContext, *, name: str, params: dict | None = None, on: str | None = None,
                   version: str | None = None, wait_seconds: float = DEFAULT_WAIT_S,
                   idempotency_key: str | None = None, not_before: str | None = None) -> dict:
    """Run a saved flow by id (for example 'biz-a.instagram.post') with typed params.
    Replays with no model. `on` picks the lane when the flow's lane is a pattern. Waits up
    to wait_seconds and returns the run with an honest status: succeeded_verified (every
    readback of the target's own state passed), succeeded_unverified, partial or failed,
    with the failing step, locator, expected vs found and a screenshot ref. Pass an
    idempotency_key to make retries safe. `not_before` (an ISO 8601 time with a timezone)
    queues the run for a slot: a commit that asks first (Ask me first on, or after untrusted
    text) asks the owner now, and an approved run waits for its slot."""
    if is_connector_id(name):  # its binding (lane, acting as) is filled by the connector layer, never the caller
        raise PolicyDenied(f"{name} is a connector operation; call its connector tool instead of flow_run")
    return await _run_flow(ctx, name, params, on, version, wait_seconds, idempotency_key, not_before)


async def run_status(ctx: ToolContext, *, id: str, steps: bool = True) -> dict:
    """Status of a run: status, proof (readbacks of the target's own state), error with
    the failing step, and the step journal."""
    runner = ctx.runtime.require("runner")
    run = seen_run(ctx, id)
    await ctx.authorize(Effect.READ, lane_id=run.target)  # a connector run: its surface
    if run.output is not None:
        from agentos.runner.connectors.taint import label_context

        label_context(ctx, run.output, source=f"run_status:{run.id}")
    origins = (run.trigger or {}).get("origins") or []
    if origins:  # a routine's child run: its params hold what a stranger wrote
        from agentos.runner.connectors.taint import label_context

        label_context(ctx, {"untrusted_fields": ["params"], "private_fields": ["params"]}, source=f"run_status:{run.id}",
                      kind=origins[0].get("kind") or "connector_output")
    if getattr(run, "status", None) == RunStatus.WAITING_APPROVAL and callable(getattr(runner, "waiting_view", None)):
        return await runner.waiting_view(run, steps=steps)  # just approved reads queued; still waiting says why
    return runner.describe(run, steps=steps)


async def run_cancel(ctx: ToolContext, *, id: str) -> dict:
    """Cancel a queued, waiting or running run. A running flow stops at its next step."""
    runner = ctx.runtime.require("runner")
    run = seen_run(ctx, id)
    await ctx.authorize(Effect.DRAFT, lane_id=run.target, summary=f"Cancel run {id} on {run.target}")
    run = await runner.cancel(id, by=ctx.principal.id)
    return runner.describe(run)


def _ask_first_store(ctx: Any) -> Any:
    store = ctx.runtime.get("ask_first")
    if store is None:
        raise ServiceUnavailable("Ask me first needs the policy service on this node")
    return store


def agent_by(ctx: Any) -> str:
    return f"agent:{getattr(ctx.principal, 'id', None) or 'unknown'}"


async def flow_save(ctx: ToolContext, *, definition: dict, ask_first: bool | None = None) -> dict:
    """Save a flow definition as a new immutable version (the version is its content hash).
    Use it to create a flow or to repair one after a failed run. Steps use structural
    locators ({role, name, within, nth, css}), never snapshot refs. Steps that change the
    target ("writes": true) must declare a readback of the target's own state. The flow's
    effect is at least what its steps do: any click, press, type, fill, select or upload
    counts as commit. Saved routines run on their own; pass ask_first=true to make every
    commit of this flow wait for a person ("Ask me first"). Only a person turns it off, and
    a new version keeps the flow's setting; payments and irreversible steps always ask. A new
    flow on a lane where a person set a routine to ask first starts with it on (ask_first_why
    says why), so a copy under a new id still asks."""
    flows = _flows(ctx)
    if ask_first is not None and not isinstance(ask_first, bool):
        raise InvalidRequest("ask_first is true or false")
    if isinstance(definition, dict) and is_connector_id(str(definition.get("id", ""))):
        raise PolicyDenied("flow ids under connector.* belong to installed connectors; an agent cannot "
                           "save one (repairs go through a reviewed connector update). Save under another id.")
    defn = flows.parse(definition)
    lane = None if defn.lane_is_pattern else defn.lane
    require_lane_patterns(ctx, [defn.lane], Effect.DRAFT)  # a pattern: every lane it can match
    check_replace(ctx, defn.id)
    await ctx.authorize(Effect.DRAFT, lane_id=lane, summary=f"Save flow {defn.id} for {defn.lane}",
                        content=defn.canonical())
    store = _ask_first_store(ctx) if ask_first is not None else None  # before saving: refused whole
    if store is not None and ask_first is False and (store.is_on("flow", defn.id)
                                                      or flows.asks_first_by_inheritance(defn)):
        raise PolicyDenied(f"only a person turns Ask me first off for {defn.id}; save without ask_first, or "
                           "ask the owner to turn it off in the panel")
    fv, created = flows.save(defn, by=saver(ctx))
    if store is not None and ask_first and not getattr(store.get("flow", fv.id), "ask_first", False):
        store.set("flow", fv.id, True, by=agent_by(ctx), person=False)
    ctx.trace("flow_save", flow=fv.id, version=fv.version, created=created)
    out = {"id": fv.id, "version": fv.version, "created": created, "lane": defn.lane, "effect": str(fv.effect),
           "declared_effect": str(defn.effect), "tool": flows.tool_names().get(fv.id),
           "warnings": lint(defn, effect=fv.effect), **flows.ask_first_view(fv.id)}
    if fv.effect_reason:
        out["effect_note"] = fv.effect_reason
    return out


async def ask_first_on(ctx: ToolContext, *, flow: str | None = None, lane: str | None = None) -> dict:
    """Turn "Ask me first" on for a saved routine (flow id) or for an account (lane), so its
    commits wait for a person. Routines and accounts run on their own by default. You can only
    turn it on; a person turns it off. Payments and irreversible actions always ask anyway. On a
    site agentos has no publish rules for, it holds a form your click submits by POST, not a
    request the page's own script sends."""
    if (flow is None) == (lane is None):
        raise InvalidRequest("name one: flow (a flow id) or lane (workspace/name)")
    store = _ask_first_store(ctx)
    if flow is not None:
        fv = seen_flow(ctx, flow)
        on = None if fv.definition.lane_is_pattern else fv.definition.lane
        await ctx.authorize(Effect.DRAFT, lane_id=on, summary=f"Ask first before {flow} commits")
        setting = store.set("flow", fv.id, True, by=agent_by(ctx), person=False)
    else:
        await ctx.authorize(Effect.DRAFT, lane_id=lane, summary=f"Ask first before commits on {lane}")
        setting = store.set("lane", str(lane), True, by=agent_by(ctx), person=False)
    ctx.trace("ask_first", scope=setting.scope, id=setting.id, ask_first=True)
    return {**setting.public(), "message": "Ask me first is on: its commits now wait for a person."}


async def flow_record_start(ctx: ToolContext, *, on: str) -> dict:
    """Start recording your browser actions in a lane. Act with the browser tools as usual,
    then call flow_record_stop to get a draft flow with params detected from typed text."""
    await ctx.authorize(Effect.DRAFT, lane_id=on, summary=f"Record actions in {on} into a draft flow")
    rec = _flows(ctx).recorder.start(on, ctx.principal.model_dump())
    rec["next"] = "Act in the lane, then call flow_record_stop."
    return rec


async def flow_record_stop(ctx: ToolContext, *, on: str, name: str | None = None, effect: str | None = None,
                           title: str | None = None, save: bool = True) -> dict:
    """Stop recording in a lane and turn the actions into a draft flow. `name` is the flow
    id (default '<workspace>.<lane>.draft-<time>'), `effect` its effect class. Saved as a new
    version unless save is false. Review the notes and warnings before running it."""
    await ctx.authorize(Effect.DRAFT, lane_id=on, summary=f"Save the recording from {on} as a draft flow")
    flows = _flows(ctx)
    rec = flows.recorder.stop(on)
    events = rec["events"]
    if not events:
        events = await _pull_trace_events(ctx, on, rec["started_at"])
    draft, notes, problem = flows.recorder.build_checked(events, lane=on, flow_id=name, effect=effect, title=title,
                                                         started_at=rec["started_at"])
    out: dict[str, Any] = {"definition": draft, "notes": notes, "events": len(events)}
    if problem is not None:  # a flow that lost what it records is never saved (QA-2 #15)
        out["saved"] = False
        out["problem"] = problem
        return out
    try:
        defn = flows.parse(draft)
    except InvalidRequest as e:
        out["saved"] = False
        out["problem"] = e.message
        return out
    out["warnings"] = lint(defn)
    if save:
        check_replace(ctx, defn.id)
        fv, _ = flows.save(defn, by=saver(ctx))
        out.update(saved=True, id=fv.id, version=fv.version, tool=flows.tool_names().get(fv.id),
                   effect=str(fv.effect))
    else:
        out["saved"] = False
    return out


async def _pull_trace_events(ctx: ToolContext, lane: str, since: Any) -> list[dict[str, Any]]:
    traces = ctx.runtime.get("traces")
    query = getattr(traces, "query", None)
    if query is None:
        return []
    try:
        items = await maybe_await(query(lane=lane, since=since))
    except Exception:  # noqa: BLE001 - recording falls back to "nothing recorded"
        return []
    out = []
    for item in items or []:
        data = item if isinstance(item, dict) else getattr(item, "model_dump", dict)()
        ev = data.get("action_event") or data.get("event") or data
        if isinstance(ev, dict) and ev.get("action"):
            out.append(ev)
    return out


STATIC = [
    ("flows_list", flows_list, Effect.READ, None, "flows"),
    ("flow_get", flow_get, Effect.READ, None, "flows"),
    ("flow_run", flow_run, Effect.READ, "on", "runner"),
    ("run_status", run_status, Effect.READ, None, "runner"),
    ("run_cancel", run_cancel, Effect.DRAFT, None, "runner"),
    ("flow_save", flow_save, Effect.DRAFT, None, "flows"),
    ("ask_first_on", ask_first_on, Effect.DRAFT, "lane", "flows"),
    ("flow_record_start", flow_record_start, Effect.DRAFT, "on", "flows"),
    ("flow_record_stop", flow_record_stop, Effect.DRAFT, "on", "flows"),
]


def tool_specs(runtime: Any) -> list[ToolSpec]:
    """Static tools plus one typed `flow_<name>` tool per saved flow. Call again after a
    flow changes (FlowService.on_change) to refresh the list."""
    # flow_run stays READ for grants (each step is authorized on its own) but it acts, so it does not read as read only
    specs = [ToolSpec(name=n, handler=h, effect=e, lane_arg=lane,
                      tags=("flows", "changes_state") if n == "flow_run" else ("flows",), hidden_without=svc)
             for n, h, e, lane, svc in STATIC]
    if runtime is not None and runtime.get("flows") is not None:
        specs += flow_tool_specs(runtime, taken={s.name for s in specs})
    from agentos.runner.connectors.tools import tool_specs as connector_specs

    specs += connector_specs(runtime, taken={s.name for s in specs})
    from agentos.runner.workflows.tools import tool_specs as workflow_specs

    specs += workflow_specs(runtime)
    from agentos.runner.siteapis.tools import tool_specs as site_api_specs

    specs += site_api_specs(runtime)
    return specs


def tools_signature(runtime: Any) -> Any:
    """What the dynamic tools above depend on: every flow head, connector operations included.
    agentosd polls it (agentos.mcp.reload) and lists the tools again when it changes, so a flow
    saved or a connector installed by the CLI (another process) needs no restart."""
    flows = runtime.get("flows") if runtime is not None else None
    stamp = getattr(getattr(flows, "store", None), "flow_heads_stamp", None)
    return stamp() if callable(stamp) else None


# ---------------------------------------------------------------- dynamic flow tools

_JSON_TYPES: dict[str, Any] = {"string": str, "integer": int, "number": float, "boolean": bool,
                               "array": list, "object": dict}


def _py_type(prop: dict[str, Any]) -> Any:
    enum = prop.get("enum")
    if isinstance(enum, list) and enum and all(isinstance(v, str) for v in enum):
        return Literal[tuple(enum)]
    t = prop.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    return _JSON_TYPES.get(t, Any)


def flow_tool_specs(runtime: Any, taken: set[str] | None = None) -> list[ToolSpec]:
    flows: FlowService = runtime.get("flows")
    names = flows.tool_names(set(taken) if taken is not None else None)
    specs = []
    for fv in sorted(flows.list(), key=lambda f: f.id):
        if not fv.definition.expose_as_tool:
            continue
        name = names[fv.id]
        handler = make_flow_handler(fv, name)
        specs.append(ToolSpec(name=name, handler=handler, effect=fv.effect,
                              description=handler.__doc__,
                              lane_arg="on" if fv.definition.lane_is_pattern else None,
                              tags=("flows", "flow"), hidden_without="runner",
                              view=flow_view(fv.definition.lane)))
    return specs


def flow_view(lane: str) -> Any:
    """A flow's typed tool is listed only to a grant that sees the flow (flow_seen): its title,
    description and params are the flow's, and a grant outside its lane is not told it exists. The
    policy hands a view the grant's own `lane_allowed`; a pattern is checked against that grant."""
    from types import SimpleNamespace

    from agentos.core.tools import ToolView

    def view(allowed: Any) -> ToolView | None:
        grant = getattr(allowed, "__self__", None)
        if any(c in lane for c in "*?["):
            seen = pattern_reaches(SimpleNamespace(grant=grant), lane) if grant is not None else True
        else:
            seen = bool(allowed(lane))
        return ToolView() if seen else None

    return view


def make_flow_handler(fv: FlowVersion, name: str):
    """A handler whose keyword-only signature is the flow's params, so the MCP layer
    derives a typed input schema. The full JSON schema is on `__agentos_input_schema__`."""
    d = fv.definition
    props: dict[str, Any] = d.params.get("properties", {})
    required = set(d.params.get("required", []))
    sig = [inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    for pname, prop in props.items():
        prop = prop if isinstance(prop, dict) else {}
        py = _py_type(prop)
        desc = prop.get("description")
        if pname in required and "default" not in prop:
            ann, default = py, inspect.Parameter.empty
        else:
            default = prop.get("default")
            ann = py if default is not None else (py | None if py is not Any else Any)
        if desc:
            ann = Annotated[ann, Field(description=desc)]
        sig.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=ann))
    if d.lane_is_pattern:
        sig.append(inspect.Parameter("on", inspect.Parameter.KEYWORD_ONLY, annotation=Annotated[
            str, Field(description=f"the lane to run on, matching {d.lane}")]))
    sig.append(inspect.Parameter("wait_seconds", inspect.Parameter.KEYWORD_ONLY, default=DEFAULT_WAIT_S,
                                 annotation=float))
    sig.append(inspect.Parameter("idempotency_key", inspect.Parameter.KEYWORD_ONLY, default=None,
                                 annotation=str | None))
    flow_id = d.id

    async def handler(ctx: ToolContext, **kwargs: Any) -> dict:
        on = kwargs.pop("on", None)
        wait = kwargs.pop("wait_seconds", DEFAULT_WAIT_S)
        key = kwargs.pop("idempotency_key", None)
        params = {k: v for k, v in kwargs.items() if v is not None}
        return await _run_flow(ctx, flow_id, params, on, None, wait, key)

    where = f"lanes matching {d.lane} (pass on)" if d.lane_is_pattern else d.lane
    handler.__name__ = name
    handler.__qualname__ = name
    handler.__doc__ = (f"{d.title or d.id}. {d.description}".strip() +
                       f" Runs the saved flow {d.id} on {where} with no model; effect {fv.effect}. "
                       "Returns the run id and, when quick, the verified result.")
    handler.__signature__ = inspect.Signature(sig, return_annotation=dict)
    handler.__annotations__ = {p.name: p.annotation for p in sig[1:]} | {"return": dict}
    schema_props = dict(props)
    schema_required = sorted(required)
    if d.lane_is_pattern:
        schema_props["on"] = {"type": "string", "description": f"the lane to run on, matching {d.lane}"}
        schema_required.append("on")
    schema_props["wait_seconds"] = {"type": "number", "default": DEFAULT_WAIT_S}
    schema_props["idempotency_key"] = {"type": "string"}
    handler.__agentos_input_schema__ = {"type": "object", "properties": schema_props, "required": schema_required}
    return handler
