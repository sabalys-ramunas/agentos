"""MCP tools for the events layer. Agents read events and triggers and may propose a trigger;
they never create one (only the owner writes triggers, in the panel or the CLI)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agentos.core.errors import InvalidRequest, PolicyDenied
from agentos.core.models import Effect
from agentos.core.tools import ToolContext, ToolSpec, covers_workspace, pattern_within
from agentos.events.ports import SERVICE_NAME
from agentos.events.service import delivery_public, event_public

MAX_LIMIT = 200


def _svc(ctx: ToolContext) -> Any:
    return ctx.runtime.require(SERVICE_NAME)


def _visible(ctx: ToolContext, workspace: str, lane: str | None) -> bool:
    """A browser's events need that browser in the grant; a lane-less event needs the whole workspace."""
    return _lane_ok(ctx, lane) if lane else covers_workspace(ctx, workspace)


def _lane_ok(ctx: Any, lane: str) -> bool:
    try:
        return bool(ctx.can_access_lane(lane))
    except Exception:  # noqa: BLE001 - a malformed lane is outside every scope
        return False


def _grant_of(ctx: Any) -> tuple[str | None, str | None]:
    g = getattr(ctx, "grant", None)
    gid = getattr(g, "id", None) or getattr(getattr(ctx, "principal", None), "grant_id", None)
    return gid, getattr(g, "path", None)


async def events_list(ctx: ToolContext, *, source: str | None = None, workspace: str | None = None,
                      since: str | None = None, limit: int = 50) -> dict:
    """List recent events on this node (newest first) with their deliveries: what happened
    (source, kind, browser, node facts), which triggers took it, and what each delivery did and
    why (waiting for quiet hours, dropped by a rate limit, delivered, dead). Content written by
    others is shown only as its size."""
    svc = _svc(ctx)
    when = None
    if since:
        try:
            when = datetime.fromisoformat(since)
        except ValueError:
            raise InvalidRequest("since is an ISO 8601 time") from None
    out = []
    for e in svc.list_events(source=source, workspace=workspace, since=when, limit=min(limit, MAX_LIMIT) * 3):
        if not _visible(ctx, e.workspace, e.lane):
            continue
        item = event_public(e)
        item["deliveries"] = [{k: v for k, v in delivery_public(d).items() if k in
                               ("id", "route_id", "state", "reason", "attempts", "target_kind", "target_ref",
                                "outcome", "due_at")} for d in svc.deliveries_for(e.id)]
        out.append(item)
        if len(out) >= min(limit, MAX_LIMIT):
            break
    return {"events": out}


async def triggers_list(ctx: ToolContext, *, workspace: str | None = None) -> dict:
    """List the owner's triggers (routes): when what happens, what it wakes (a flow, a brain
    task or an agent browser), with which instruction and grant, its controls (quiet hours, rate
    limits, batching), whether it is paused and what is waiting."""
    svc = _svc(ctx)
    out = [svc.describe_route(r) for r in svc.list_routes(workspace) if _visible(ctx, r.workspace, _lane_of(r))]
    return {"triggers": out}


def _lane_of(route: Any) -> str | None:
    lane = route.target_lane or route.match.lane
    return lane if lane and not any(c in lane for c in "*?[") else None


async def trigger_propose(ctx: ToolContext, *, trigger: dict) -> dict:
    """Propose a trigger to the owner: {name, match: {source, kind?, workspace, lane?, where?},
    target: {kind: flow|brain|agent, ...}, instruction?, grant: {ceiling, browsers, flows,
    ttl_s, budget}, controls?, priority?}. This never creates it: it puts the full trigger on an
    approval card, and the owner's approval creates it exactly as shown. Returns approval_id."""
    if not isinstance(trigger, dict):
        raise InvalidRequest("trigger is a JSON object")
    svc = _svc(ctx)
    match = trigger.get("match") if isinstance(trigger.get("match"), dict) else {}
    target = trigger.get("target") if isinstance(trigger.get("target"), dict) else {}
    grant = trigger.get("grant") if isinstance(trigger.get("grant"), dict) else {}
    ws = str(match.get("workspace") or "")
    lanes = [x for x in [match.get("lane"), target.get("lane"), target.get("on"), *(grant.get("lanes") or [])]
             if isinstance(x, str)]  # match.lane too: a trigger hears that lane's events
    if not isinstance(match.get("lane"), str) and not covers_workspace(ctx, ws):  # it hears the whole workspace
        raise PolicyDenied(f"you can only propose triggers for what your grant reaches: one with no match.lane "
                           f"hears every browser of {ws or 'its workspace'}")
    for lane in lanes:  # a pattern is checked as a pattern: every lane it can match is in the grant
        if not (pattern_within(ctx, lane) if any(c in lane for c in "*?[") else _lane_ok(ctx, lane)):
            raise PolicyDenied(f"you can only propose triggers for browsers your grant reaches ({lane} is not)")
    first = next((x for x in lanes if not any(c in x for c in "*?[")), None)
    await ctx.authorize(Effect.DRAFT, lane_id=first, summary=f"Propose a trigger in {ws or 'a workspace'}")
    gid, path = _grant_of(ctx)
    out = svc.propose_route(trigger, principal=ctx.principal, grant_id=gid, grant_path=path)
    ctx.trace("trigger_propose", approval_id=out["approval_id"], trigger=out["trigger_id"])
    out["next"] = "The owner decides in Needs you. If approved, the trigger exists as you wrote it."
    return out


STATIC = [
    ("events_list", events_list, Effect.READ),
    ("triggers_list", triggers_list, Effect.READ),
    ("trigger_propose", trigger_propose, Effect.DRAFT),
]


def tool_specs(runtime: Any) -> list[ToolSpec]:
    return [ToolSpec(name=n, handler=h, effect=e, lane_arg=None, tags=("events",), hidden_without=SERVICE_NAME)
            for n, h, e in STATIC]
