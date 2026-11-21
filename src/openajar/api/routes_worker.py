"""The browser worker on one lane, for the panel: is it on there, and does a publish ask first.

  GET  /api/lanes/{ws}/{name}/worker              both switches, as a `LaneWorker`
  PUT  /api/lanes/{ws}/{name}/worker              {on}: the worker on this lane
  PUT  /api/lanes/{ws}/{name}/worker/ask-first    {ask_first}: "Asks you first" or "Runs on its own"

A person signed in to the panel, and never a bearer token, the owner token included: an agent may
hold one (docs/API.md, "Access"). Turning Ask me first on is the one exception: it only makes the lane
safer, so any caller whose grant reaches the lane may, a token included, as on `POST /api/ask-first`.
Turning the worker off takes anyone signed in who reaches the lane. The looser way (worker on, "Runs on its own") takes the owner or a member
with `standing` in the lane's workspace, as turning Ask me first off does on `POST /api/ask-first`.

Every call answers with the same `LaneWorker`, so a screen can replace what it holds.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.requests import Request
from starlette.routing import Route

from openajar.api.access import Presence
from openajar.api.app import Api, access_of, endpoint, principal_of, read_body
from openajar.api.routes_core import scoped_lane_id
from openajar.core.aio import maybe_await
from openajar.core.errors import ServiceUnavailable


class WorkerChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: StrictBool


class AskFirstChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ask_first: StrictBool


class ActionLog:
    """`openajar.plugins`' ChangeLog on this daemon's own action log. As in the CLI, a row that stays
    waiting in memory counts as not kept, so the change is undone."""

    def __init__(self, traces: Any):
        self.traces = traces

    def record(self, change: Any) -> None:
        from openajar.policy.traces import TraceEvent

        if self.traces is None:
            raise ServiceUnavailable("this node has no action log")
        self.traces.write(TraceEvent(**change.trace()))
        if self.traces.flush():
            raise ServiceUnavailable("the log is busy or broken, and did not take the row")


def routes(api: Api) -> list[Route]:
    def plugins() -> Any:
        return api.svc("plugins")

    def a_person(request: Request, action: str) -> tuple[Any, str]:
        """The caller and the lane, once this is a person who reaches a lane that exists."""
        access = access_of(request)
        access.require_presence(Presence.PERSON, action)
        return access, scoped_lane_id(request, action)

    def who(request: Request, access: Any) -> str:
        return principal_of(request).id if access.member is not None else "owner"

    def ask_first_view(lane: str) -> dict[str, Any]:
        from openajar.node.tools import lane_asks_first

        store = api.svc("ask_first")
        row = store.get("lane", lane)
        chosen = store.chosen_off("lane", lane)
        policy = api.runtime.get("policy")
        downgrade = getattr(getattr(getattr(policy, "cfg", None), "taint", None), "downgrade", ())
        return {
            "choice": "runs_on_its_own" if chosen else "asks_first" if row is not None and row.ask_first else None,
            "asks": lane_asks_first(api.runtime, lane),
            "written_words_ask": not chosen,
            "after_a_page_asks": "commit" in downgrade,
            "set_by": row.set_by if row is not None else None,
            "by_person": row.by_person if row is not None else None,
            "set_at": row.public()["set_at"] if row is not None else None,
        }

    def worker_view(lane: str) -> dict[str, Any]:
        from openajar.plugins.service import LANE_WORKER

        service = plugins()
        state = service.state()
        return {"plugin": LANE_WORKER, "plugin_status": service.view(LANE_WORKER, state).status,
                "on": service.lane_stop(LANE_WORKER, lane) is None, "problem": state.problem}

    async def view(lane: str) -> dict[str, Any]:
        await maybe_await(api.svc("lanes").get(lane))  # 404 for a lane that is not there
        return {"lane": lane, "worker": worker_view(lane), "ask_first": ask_first_view(lane)}

    async def lane_worker(request: Request) -> dict[str, Any]:
        access, lane = a_person(request, "lane.worker")
        access.require("see", lane.split("/", 1)[0], action="lane.worker", lane=lane)
        return await view(lane)

    async def lane_worker_set(request: Request) -> dict[str, Any]:
        from openajar.plugins import admin

        access, lane = a_person(request, "lane.worker.set")
        body = await read_body(request, WorkerChange)
        if body.on:
            access.require("standing", lane.split("/", 1)[0], action="lane.worker.set", lane=lane)
        await maybe_await(api.svc("lanes").get(lane))
        policy = api.runtime.get("policy")
        admin.set_lane(plugins(), lane, body.on, ActionLog(getattr(policy, "traces", None)),
                       by=f"panel:{who(request, access)}")
        access.trace("lane.worker.set", lane=lane, summary=f"browser worker {'on' if body.on else 'off'}")
        return await view(lane)

    async def lane_worker_ask_first(request: Request) -> dict[str, Any]:
        # On only makes the lane safer: any caller whose grant reaches the lane, as on POST /api/ask-first.
        # Off lets a publish run without a person, so it needs one (and a member needs `standing`).
        access = access_of(request)
        lane = scoped_lane_id(request, "lane.worker.ask_first")
        body = await read_body(request, AskFirstChoice)
        if not body.ask_first:
            access.require_presence(Presence.PERSON, "lane.worker.ask_first", lane=lane)
            access.require("standing", lane.split("/", 1)[0], action="lane.worker.ask_first", lane=lane)
        await maybe_await(api.svc("lanes").get(lane))
        person = access.presence >= Presence.PERSON
        by = who(request, access) if person else f"agent:{principal_of(request).id}"
        api.svc("ask_first").set("lane", lane, body.ask_first, by=by, person=person)
        what = "asks first" if body.ask_first else "runs on its own"
        access.trace("lane.worker.ask_first", lane=lane, summary=f"lane {lane} {what}")
        if not person:  # the worker's own state is a person's to read (GET): a token hears only its switch
            return {"lane": lane, "ask_first": ask_first_view(lane)}
        return await view(lane)

    return [
        Route("/api/lanes/{ws}/{name}/worker", endpoint(api, lane_worker), methods=["GET"]),
        Route("/api/lanes/{ws}/{name}/worker", endpoint(api, lane_worker_set), methods=["PUT"]),
        Route("/api/lanes/{ws}/{name}/worker/ask-first", endpoint(api, lane_worker_ask_first), methods=["PUT"]),
    ]
