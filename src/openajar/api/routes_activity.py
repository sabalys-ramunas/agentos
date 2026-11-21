"""Activity, runs, flows and schedules: what agents did and what is set to run (docs/API.md)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator
from starlette.requests import Request

from openajar.api.access import Presence
from openajar.api.actors import trace_actor
from openajar.api.app import (
    Api,
    _takes,
    access_of,
    principal_of,
    query_int,
    query_str,
    read_body,
    scoped_list,
)
from openajar.api.errors import ApiError
from openajar.api.jsonable import jsonable
from openajar.api.route_ids import path_id
from openajar.api.routes_core import OUT_OF_SCOPE
from openajar.core.aio import maybe_await
from openajar.core.errors import (
    ApprovalPending,
    InvalidRequest,
    NotFound,
)
from openajar.core.models import RunStatus
from openajar.core.tools import pattern_reaches


def withheld_run(run: dict) -> dict:
    """A run a stranger's text fed, for a token: a route has no family to label, so it leaves that out,
    as openajar.runner.workflows.mirror does for a routine's own run."""
    if not run.get("from_outside"):
        return run
    held = {"params": {"withheld": True}, "steps": [], "summary": None, "proof": None, "output": None, "error": None}
    return {**run, **{k: v for k, v in held.items() if k in run}}


class FlowRun(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    on: str | None = Field(default=None, max_length=200)  # the lane, for flows bound to a lane pattern


class ScheduleCreate(BaseModel):
    flow: str = Field(min_length=1, max_length=128)
    cron: str | None = Field(default=None, max_length=100)
    watch: str | None = Field(default=None, max_length=2000)
    params: dict[str, Any] = Field(default_factory=dict)
    deadman_minutes: int | None = Field(default=None, ge=1, le=7 * 24 * 60)

    @model_validator(mode="after")
    def _one_trigger(self) -> ScheduleCreate:
        if bool(self.cron) == bool(self.watch):
            raise ValueError("give exactly one of cron or watch")
        return self


class ScheduleUpdate(BaseModel):
    enabled: bool


def with_handles(api: Api, events: list[dict]) -> list[dict]:
    """Activity names a member by their handle (`handle`), not only their member id."""
    handles = api.members.store.handles()
    for e in events:
        pid = e.get("principal")
        if isinstance(e, dict) and e.get("principal_kind") == "human" and pid in handles:
            e["handle"] = handles[pid]
        if isinstance(e, dict):  # who and how, one wording (openajar.api.actors)
            e["who"], e["how"] = trace_actor(e)
    return events


class ActivityHandlers:
    """The activity, run, flow and schedule handlers, sharing one Api."""

    def __init__(self, api: Api) -> None:
        self.api = api

    # ---- activity and runs ----
    async def activity(self, request: Request) -> list[dict]:
        access = access_of(request)
        lane = query_str(request, "lane")
        workspace = query_str(request, "workspace")
        if lane and not access.lane_allowed(lane):
            raise ApiError("denied", OUT_OF_SCOPE)
        query = self.api.svc("traces").query
        kw = {"lane": lane, "workspace": workspace, "kind": query_str(request, "kind"),
              "before": query_str(request, "before"), "limit": query_int(request, "limit", 100)}
        if access.member is not None:  # every principal's events in the member's workspaces
            spaces = access.workspaces_with("see")  # type: ignore[attr-defined]
            if spaces is not None and workspace is not None and workspace not in spaces:
                raise ApiError("denied", OUT_OF_SCOPE)
            if spaces is None:
                events = await maybe_await(query(**kw))
            elif not spaces or not _takes(query, "viewer_workspaces"):
                events = []
            else:
                events = await maybe_await(query(viewer_workspaces=sorted(spaces), **kw))
        else:
            events = await scoped_list(query, access.viewer, **kw)
        return with_handles(self.api, jsonable(events))

    def _run_visible(self, request: Request, run: dict) -> bool:
        access = access_of(request)
        lane = run.get("lane")
        return access.owner or (isinstance(lane, str) and access.lane_allowed(lane))

    async def runs(self, request: Request) -> list[dict]:
        status = query_str(request, "status")
        if status and status not in {s.value for s in RunStatus}:
            raise InvalidRequest("Unknown run status.")
        flow = query_str(request, "flow")  # a recorded routine's own runs, newest first (additive)
        extra = {"flow": flow} if flow and _takes(self.api.svc("runner").list_runs, "flow") else {}
        items = jsonable(await maybe_await(self.api.svc("runner").list_runs(status=status,
                                                                       limit=query_int(request, "limit", 50), **extra)))
        return [r for r in items if self._run_visible(request, r)]

    async def run_get(self, request: Request) -> dict:
        run = jsonable(await maybe_await(self.api.svc("runner").get_run(path_id(request))))
        if not self._run_visible(request, run):
            raise NotFound(f"No run {path_id(request)}.")
        return run if access_of(request).human else withheld_run(run)

    # ---- flows and schedules ----
    async def flows(self, request: Request) -> list[dict]:
        access = access_of(request)
        items = jsonable(await maybe_await(self.api.svc("flows").list()))
        if access.owner:
            return items
        # a flow bound to one lane shows when that lane is in scope; a pattern when it can match a lane in
        # the grant (a member's session: its patterns are checked at run time)
        ctx = getattr(access, "ctx", None)

        def seen(lane: str) -> bool:
            if any(c in lane for c in "*?["):
                return pattern_reaches(ctx, lane) if ctx is not None else True
            return access.lane_allowed(lane)

        return [f for f in items if isinstance(f.get("lane"), str) and seen(f["lane"])]

    async def flow_run(self, request: Request) -> dict:
        body = await read_body(request, FlowRun)
        svc = self.api.svc("flows")
        flow_id, access, principal = path_id(request), access_of(request), principal_of(request)
        plan = getattr(svc, "plan", None)
        if plan is None:  # a flow service that cannot say what it would do: it authorizes on its own
            run = jsonable(await maybe_await(svc.run(flow_id, params=body.params, principal=principal)))
        else:
            p = await maybe_await(plan(flow_id, params=body.params, on=body.on))
            if not access.lane_allowed(p["lane"]):
                access.trace("flow.run", status="denied", lane=p["lane"], summary="out of scope")
                raise ApiError("denied", OUT_OF_SCOPE)
            if access.member is not None:
                access.require("operate", p["lane"].split("/", 1)[0], action="flow.run", lane=p["lane"])
                principal = access.principal_for(principal, p["lane"])
            approval_id = decision = None
            try:
                decision = await access.authorize(p["effect"], action="flow.run", lane_id=p["lane"],
                                                  summary=p["summary"], content=p["content"])
            except ApprovalPending as exc:
                approval_id = exc.approval_id  # the run waits for the owner, then starts
            authorized = approval_id is None and access.grant_id is not None  # without policy the runner decides
            ids = getattr(decision, "pace_ids", None) or []  # the pace row its authorization wrote, kept on the run
            row = {"pace_id": ids[0]} if authorized and ids else {}
            if p.get("version"):  # the version it checked runs, not one saved since
                row["version"] = p["version"]
            if authorized and getattr(decision, "route", None) == "auto":
                row["auto"] = True  # let through on its own: the runner reads Ask me first again before it runs
            run = jsonable(await maybe_await(svc.run(flow_id, params=p["params"], principal=principal, on=p["lane"],
                                                     authorized=authorized, approval_id=approval_id, **row)))
        self.api.publish("run", run)
        return run

    # A grant sees its own schedules (the grant_id they were created under); the owner sees all; a
    # member with `standing` (an admin) the schedules on their workspaces' lanes.
    def _schedule_visible(self, request: Request, schedule: dict) -> bool:
        return access_of(request).schedule_visible(schedule)

    async def _own_schedule(self, request: Request, sid: str, action: str) -> dict:
        """The schedule a member may change (404 for one they do not see, before anything changes)."""
        schedule = jsonable(await maybe_await(self.api.svc("runner").get_schedule(sid)))
        if not self._schedule_visible(request, schedule):
            access_of(request).trace(action, status="denied", summary=f"schedule {sid} is not in your workspaces")
            raise NotFound(f"No schedule {sid}.")
        return schedule

    async def schedules(self, request: Request) -> list[dict]:
        items = jsonable(await maybe_await(self.api.svc("runner").list_schedules()))
        return [s for s in items if self._schedule_visible(request, s)]

    async def schedule_get(self, request: Request) -> dict:
        sid = path_id(request)
        schedule = jsonable(await maybe_await(self.api.svc("runner").get_schedule(sid)))
        if not self._schedule_visible(request, schedule):
            raise NotFound(f"No schedule {sid}.")  # as for an item that does not exist: nothing leaks
        return schedule

    def _owner_schedule_needs_a_person(self, request: Request, grant_id: str | None, action: str) -> None:
        """A schedule under the owner's own grant runs as the owner and never ends, so making one or
        turning one on is a person's word: the node token and the device credential carry that grant,
        and an agent may hold them. An admin grant's schedule is bound to that grant instead."""
        if (grant_id or "owner") == "owner":
            access_of(request).require_presence(Presence.PERSON, action)

    async def schedule_fires(self, request: Request) -> list[dict]:
        """The runs a schedule started, oldest first in what the node reports: `ScheduleFire`s."""
        sid = path_id(request)
        await self._own_schedule(request, sid, "schedule.fires")
        runs = await maybe_await(self.api.svc("runner").schedule_runs(sid, limit=query_int(request, "limit", 20, hi=100)))
        return [{"at": run.get("started_at") or run.get("created_at"), "status": run["status"], "run_id": run["id"]}
                for run in runs]

    async def schedule_create(self, request: Request) -> dict:
        body = await read_body(request, ScheduleCreate)
        self._owner_schedule_needs_a_person(request, principal_of(request).grant_id, "schedule.create")
        created = jsonable(await maybe_await(self.api.svc("runner").create_schedule(
            flow=body.flow, cron=body.cron, watch=body.watch, params=body.params,
            deadman_minutes=body.deadman_minutes, principal=principal_of(request))))
        access_of(request).trace("schedule.create", summary=f"Schedule flow {body.flow}")
        self.api.bridge.publish_changed(self.api.bus, "schedule", created["id"], created)
        return created

    async def schedule_update(self, request: Request) -> dict:
        body = await read_body(request, ScheduleUpdate)
        sid = path_id(request)
        before = await self._own_schedule(request, sid, "schedule.update")
        if body.enabled:  # a token may stop a schedule, never start one, whoever's grant it runs under
            self._owner_schedule_needs_a_person(request, before.get("grant_id"), "schedule.update")
            access_of(request).require_presence(Presence.PERSON, "schedule.update")
        updated = jsonable(await maybe_await(self.api.svc("runner").set_schedule_enabled(sid, body.enabled)))
        access_of(request).trace("schedule.update", summary=f"{'Enable' if body.enabled else 'Disable'} schedule {sid}")
        self.api.bridge.publish_changed(self.api.bus, "schedule", sid, updated)
        return updated

    async def schedule_delete(self, request: Request) -> dict:
        sid = path_id(request)
        svc = self.api.svc("runner")
        before = await self._own_schedule(request, sid, "schedule.delete")  # 404 before anything changes
        await maybe_await(svc.delete_schedule(sid))
        access_of(request).trace("schedule.delete", summary=f"Delete schedule {sid}")
        self.api.bridge.retire(self.api.bus, "schedule", sid, before)
        return {"ok": True}
