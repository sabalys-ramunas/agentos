"""Workers on a node, as the app reads them (web/src/api/types.ts, `WorkerOverview`).

Everything here is what the node really knows. A thing is one pilot run. Its steps are the run's own
trail. The day's counts come from the same place as the limits they are held to.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.requests import Request
from starlette.routing import BaseRoute

from agentos import __version__
from agentos.api.access import Presence
from agentos.api.app import Api, access_of, principal_of, read_body
from agentos.api.errors import ApiError
from agentos.api.routes_worker import ActionLog
from agentos.api.web.base import org_route, read, write
from agentos.core.errors import NotFound
from agentos.pilot.day import Day, count
from agentos.pilot.service import Settings, load_settings
from agentos.pilot.tools import MAX_GOAL_CHARS
from agentos.pilot.trail import trail
from agentos.plugins import admin
from agentos.plugins.models import Skill
from agentos.plugins.service import LANE_WORKER, brain_skills

JOBS = ("decide", "write", "rescue")
STOPS = {"held": "robot_check", "needs_help": "unsure", "refused": "unsure", "gave_up": "limit"}
DONE = ("succeeded_verified", "succeeded_unverified")


class Switch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: StrictBool


class AccountSwitch(Switch):
    lane: str = Field(min_length=3, max_length=80)


class ThingStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lane: str = Field(min_length=3, max_length=80)
    told: str = Field(min_length=1, max_length=MAX_GOAL_CHARS)


class JobChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brain: str = Field(min_length=1, max_length=64)


class LimitsChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps_per_day: int | None = Field(default=None, ge=1, le=100_000)
    help_per_day: int | None = Field(default=None, ge=0, le=10_000)
    steps_per_thing: int | None = Field(default=None, ge=1, le=100)
    usd_per_day: float | None = Field(default=None, gt=0, le=1_000)


def thing_of(run: Any, node: str, rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    """One pilot run, as one thing a worker did."""
    output = run.output or {}
    status = str(run.status)
    steps = list(output.get("trail") or trail(rows))
    pilot = output.get("pilot_status")
    if status == "waiting_approval":
        state, stop = "asked", None
        steps.append({"kind": "yes", "what": "Waiting for your yes", "model": None, "ms": 0, "usd": 0.0})
    elif status in DONE:
        state, stop = "done", None
    elif run.terminal:
        state = "stopped"
        reason = str(output.get("reason") or "")
        stop = "new_device" if reason == "needs_owner" else STOPS.get(str(pilot), "unsure")
        said = output.get("message") or (run.error or {}).get("message") or "It stopped and asked a person"
        steps.append({"kind": "you", "what": str(said), "model": None, "ms": 0, "usd": 0.0})
    else:
        state, stop = "working", None
    principal = run.principal or {}
    return {
        "id": run.id,
        "worker": "browser",
        "lane": run.lane,
        "node": node,
        "said_by": {"kind": principal.get("kind", "agent"), "id": principal.get("id", "agent"),
                    **({"thread": principal["thread"]} if principal.get("thread") else {})},
        "told": str((run.params or {}).get("goal") or ""),
        "state": state,
        "steps": steps,
        "stop": stop,
        "limit": int((run.params or {}).get("max_steps") or limit),
        "started_at": (run.started_at or run.created_at).isoformat(),
        "ended_at": run.finished_at.isoformat() if run.finished_at else None,
        "run_id": run.id,
    }


class Handlers:
    """The handlers of the routes below, sharing one Api (and what they keep between calls)."""

    def __init__(self, api: Api) -> None:
        self.api = api

    def plugins(self) -> Any:
        return self.api.svc("plugins")

    def log(self) -> ActionLog:
        return ActionLog(getattr(self.api.runtime.get("policy"), "traces", None))

    def a_person(self, request: Request, action: str) -> Any:
        access = access_of(request)
        access.require_presence(Presence.PERSON, action)  # never a token, the owner token included
        return access

    def who(self, request: Request, access: Any) -> str:
        return f"panel:{principal_of(request).id if access.member is not None else 'owner'}"

    def pilot(self) -> Any | None:
        return self.api.runtime.get("pilot")

    def settings(self) -> Settings:
        service = self.pilot()
        return service.settings if service is not None else load_settings(getattr(self.api.config, "home", None))

    def runs(self, lane: str | None = None) -> list[Any]:
        runner = self.api.runtime.get("runner")
        if runner is None:
            return []
        from agentos.core.models import RunKind

        filters = {"lane": lane} if lane else {}
        return runner.list(kind=RunKind.PILOT, limit=500, **filters)

    def thing(self, run: Any) -> dict[str, Any]:
        service = self.pilot()
        return thing_of(run, self.api.node_name, service.live_rows(run.id) if service is not None else [],
                        self.settings().max_steps)

    def today(self) -> Day:
        service = self.pilot()
        return service.today() if service is not None else count(self.runs())

    def downgrades_commit(self) -> bool:
        taint = getattr(getattr(self.api.runtime.get("policy"), "cfg", None), "taint", None)
        return "commit" in getattr(taint, "downgrade", ())

    def brain_spec(self, name: str | None) -> Any | None:
        brains = self.api.runtime.get("brains")
        if brains is None or name is None:
            return None
        try:
            return brains.get_spec(name)
        except NotFound:
            return None

    def can_decide(self) -> bool:
        brains = self.api.runtime.get("brains")
        return brains is not None and any(Skill.DECISION in brain_skills(spec) for spec in brains.store.list_brains())

    def worker_word(self, view: Any) -> str:
        if view.status in ("running", "enabled"):
            return "on"
        return "off" if self.can_decide() else "needs_service"

    async def browser_lanes(self, request: Request) -> list[str]:
        rows = await read(request, "/api/lanes")
        return [row["id"] for row in rows if row.get("kind", "browser") == "browser"]

    async def place(self, request: Request, view: Any, day: Day) -> dict[str, Any]:
        from agentos.node.tools import lane_asks_first

        lanes = await self.browser_lanes(request)
        off = sorted(lane for lane in view.lanes_off if lane in lanes)
        working = [lane for lane in lanes if lane not in off]
        word = self.worker_word(view)
        return {
            "node": self.api.node_name,
            "state": "up",
            "worker": word,
            "missing": "An AI service that can decide each step" if word == "needs_service" else None,
            "things_today": day.things,
            "accounts": len(working),
            "accounts_off": off,
            "accounts_asking": sum(1 for lane in working if lane_asks_first(self.api.runtime, lane)),
            "after_a_page_asks": self.downgrades_commit(),
            "suggested_by": None,
            "restart_needed": view.status == "enabled",
        }

    def jobs(self, view: Any) -> list[dict[str, Any]]:
        brains = {row["job"]: row.get("brain") for row in view.jobs}
        out = []
        for job in JOBS:
            name = brains.get(job)
            spec = self.brain_spec(name)
            out.append({"job": job, "brain": name, "model": getattr(spec, "model", None) if spec else None})
        return out

    def limits(self) -> dict[str, Any]:
        current = self.settings()
        return {"steps_per_day": current.steps_per_day, "help_per_day": current.help_per_day,
                "steps_per_thing": current.max_steps, "usd_per_day": current.usd_per_day}

    def later(self) -> list[dict[str, Any]]:
        out = []
        for view in self.plugins().views():
            kind = view.id.removesuffix("-worker")
            if view.id == LANE_WORKER or not view.lane_kinds:
                continue  # the one here, and plugins that drive no lane (text-review): they are not workers
            reasons = view.unmet or ["It is not built yet."]
            out.append({"kind": kind, "sentence": view.description,
                        "places": [{"node": self.api.node_name, "why": reasons[0], "fact": reasons[-1]}]})
        return out

    async def overview_of(self, request: Request) -> dict[str, Any]:
        view = self.plugins().view(LANE_WORKER)
        day = self.today()
        rows = self.runs()
        return {
            "plugin": LANE_WORKER,
            "version": __version__,
            "places": [await self.place(request, view, day)],
            "jobs": self.jobs(view),
            "limits": self.limits(),
            "today": day.public(),
            "latest": self.thing(rows[0]) if rows else None,
            "later": self.later(),
        }

    async def overview(self, request: Request) -> dict[str, Any]:
        return await self.overview_of(request)

    def here(self, request: Request) -> None:
        if request.path_params["node"] != self.api.node_name:
            raise ApiError("not_found", "This computer only speaks for itself.")

    async def on_computer(self, request: Request) -> dict[str, Any]:
        self.here(request)
        view = self.plugins().view(LANE_WORKER)
        rows = self.runs()
        word = self.worker_word(view)
        brains = self.api.runtime.get("brains")
        cannot = [{"kind": item["kind"], "why": item["places"][0]["why"]} for item in self.later()]
        if word != "on":
            # Without a final period: the app ends each reason with one, as it does for every other reason here.
            cannot.append({"kind": "browser", "why": "It has no AI service here that can decide each step"
                           if word == "needs_service" else "Nobody has turned it on here"})
        return {
            "node": self.api.node_name,
            "runs": {"kind": "browser", "on": True, "latest": self.thing(rows[0]) if rows else None,
                     "things_today": self.today().things} if word == "on" else None,
            "cannot": cannot,
            "keeps": [spec.name for spec in brains.store.list_brains()] if brains is not None else [],
        }

    def give_decide_job(self, request: Request, access: Any) -> None:
        """Turning it on with one service that can decide needs no second question."""
        service = self.plugins()
        if service.state().brains_of(LANE_WORKER).get("decide"):
            return
        brains = self.api.runtime.get("brains")
        able = [spec.name for spec in (brains.store.list_brains() if brains is not None else ())
                if Skill.DECISION in brain_skills(spec)]
        if able:
            admin.assign(service, LANE_WORKER, "decide", able[0], self.log(), by=self.who(request, access))

    async def set_on_computer(self, request: Request) -> dict[str, Any]:
        self.here(request)
        access = self.a_person(request, "worker.set")
        body = await read_body(request, Switch)
        if body.on:
            access.require_owner("worker.set")
            self.give_decide_job(request, access)
            admin.enable(self.plugins(), LANE_WORKER, self.log(), by=self.who(request, access))
        else:
            admin.disable(self.plugins(), LANE_WORKER, self.log(), by=self.who(request, access))
        access.trace("worker.set", summary=f"browser worker {'on' if body.on else 'off'}")
        return await self.overview_of(request)

    def lane_of(self, request: Request) -> str:
        lane = request.query_params.get("lane") or ""
        if "/" not in lane:
            raise ApiError("invalid", "Say which account: lane=workspace/name.")
        return lane

    async def account_view(self, lane: str, request: Request) -> dict[str, Any] | None:
        view = self.plugins().view(LANE_WORKER)
        if self.worker_word(view) != "on":
            return None
        ws, name = lane.split("/", 1)
        out = await read(request, f"/api/lanes/{ws}/{name}/worker")
        return {"lane": lane, "on": bool(out["worker"]["on"]), "online": True,
                "after_a_page_asks": bool(out["ask_first"]["after_a_page_asks"])}

    async def account(self, request: Request) -> dict[str, Any] | None:
        return await self.account_view(self.lane_of(request), request)

    async def set_account(self, request: Request) -> dict[str, Any] | None:
        body = await read_body(request, AccountSwitch)
        if "/" not in body.lane:
            raise ApiError("invalid", "Say which account: workspace/name.")
        ws, name = body.lane.split("/", 1)
        out = await write(request, "PUT", f"/api/lanes/{ws}/{name}/worker", {"on": body.on})
        return {"lane": body.lane, "on": bool(out["worker"]["on"]), "online": True,
                "after_a_page_asks": bool(out["ask_first"]["after_a_page_asks"])}

    async def things(self, request: Request) -> dict[str, Any] | None:
        lane = self.lane_of(request)
        ws, name = lane.split("/", 1)
        await read(request, f"/api/lanes/{ws}/{name}")  # a run belongs to its lane: refused if the lane is
        rows = self.runs(lane)
        return self.thing(rows[0]) if rows else None

    async def start_thing(self, request: Request) -> dict[str, Any]:
        """A person hands the worker a job, as an agent does with `browser_task`. It runs the same way, under
        a child grant of this person's, so it goes no further on this account than they could."""
        from agentos.core.models import RunKind

        access = self.a_person(request, "worker.start")
        body = await read_body(request, ThingStart)
        told = " ".join(body.told.split())
        if "/" not in body.lane or not told:
            raise ApiError("invalid", "Say which account, and what it should do.")
        ws, name = body.lane.split("/", 1)
        await read(request, f"/api/lanes/{ws}/{name}")  # refused if this person does not reach the lane
        access.require("operate", ws, action="worker.start", lane=body.lane)
        service = self.pilot()
        if service is None:
            raise ApiError("conflict", "The browser worker is not running on this computer.")
        service.check_worker(body.lane)
        service.check_lane(body.lane)
        params = {"goal": told, "values": [], "files": [], "max_steps": None, "read": None}
        run, _ = self.api.runtime.require("runner").submit(kind=RunKind.PILOT, lane=body.lane, params=params,
                                                      principal=principal_of(request), source="panel", authorized=True)
        access.trace("worker.start", lane=body.lane, summary=f"Gave the browser worker a job on {body.lane}")
        return self.thing(run)

    async def set_job(self, request: Request) -> dict[str, Any]:
        access = self.a_person(request, "worker.job")
        access.require_owner("worker.job")
        job = request.path_params["job"]
        if job not in JOBS:
            raise ApiError("invalid", "A worker has three jobs: decide, write and rescue.")
        body = await read_body(request, JobChange)
        admin.assign(self.plugins(), LANE_WORKER, job, body.brain, self.log(), by=self.who(request, access))
        access.trace("worker.job", summary=f"{job} job given to {body.brain}")
        return await self.overview_of(request)

    async def set_limits(self, request: Request) -> dict[str, Any]:
        access = self.a_person(request, "worker.limits")
        access.require_owner("worker.limits")
        body = await read_body(request, LimitsChange)
        changes = body.model_dump(exclude_none=True)
        if "steps_per_thing" in changes:
            changes["max_steps"] = changes.pop("steps_per_thing")
        service = self.pilot()
        if service is not None:
            service.change_limits(changes)
        else:
            from agentos.pilot.service import save_settings

            save_settings(self.api.config.home, changes)
        access.trace("worker.limits", summary="Changed the worker's limits")
        return await self.overview_of(request)


def routes(api: Api) -> list[BaseRoute]:
    h = Handlers(api)

    return [
        org_route(api, "/workers", h.overview, ["GET"], owner=True),
        org_route(api, "/workers/computers/{node}", h.on_computer, ["GET"], owner=True),
        org_route(api, "/workers/computers/{node}", h.set_on_computer, ["POST"]),
        org_route(api, "/workers/accounts", h.account, ["GET"]),
        org_route(api, "/workers/accounts", h.set_account, ["POST"]),
        org_route(api, "/workers/things", h.things, ["GET"]),
        org_route(api, "/workers/things", h.start_thing, ["POST"]),
        org_route(api, "/workers/jobs/{job}", h.set_job, ["POST"], owner=True),
        org_route(api, "/workers/limits", h.set_limits, ["POST"], owner=True),
    ]
