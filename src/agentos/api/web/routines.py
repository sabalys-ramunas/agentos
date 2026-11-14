"""Routines on a node, as the app reads them (web/src/api/types.ts: `Flow`, `RoutineRun`, `Run`,
`RoutineCatalog`, `RoutineDay`).

A routine is one of two things the node keeps. A workflow (`/api/workflows`) is built in the app: it has
steps and a trigger the app can show. A recorded routine (`/api/flows`) is a flow the node keeps as a
recording: it has a name, parameters and runs, but no steps the app can draw. Both answer as one `Flow`
list, so a person sees one set of routines. Nothing here keeps its own copy. What a row says the node
really knows: a field it cannot know is left out, or is `null` where the type allows it. Turning a
workflow on is a person's act and goes through the node's own route, which refuses a token.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.requests import Request
from starlette.routing import BaseRoute

from agentos.api.app import Api, principal_of, read_body, read_json_object
from agentos.api.errors import ApiError
from agentos.api.web.base import call, org_route, read, write

# What the app says for a run that ended without words of its own.
STATUS_WORDS = {"failed": "Did not work", "cancelled": "Cancelled", "partial": "Finished part of the way"}
# What a recorded routine's run came to, in plain words, from its status alone (the node's own summary is its words).
RECORDED_WORDS = {
    "succeeded_verified": "Done and checked", "succeeded_unverified": "Done, but not checked",
    "partial": "Finished part of the way", "failed": "Did not work", "cancelled": "Cancelled",
    "waiting_approval": "Waiting for you", "queued": "Working on it now", "running": "Working on it now",
}
PAID = ("pay", "irreversible")
# How a routine can start on this computer. A webhook needs an address the computer does not show yet, so a
# routine that waits for one is not offered here: a page must never offer what the computer cannot do.
TRIGGERS_HERE = ["schedule", "email", "page_change", "manual"]
# A step that sends or drafts mail, or a routine that starts from mail, needs a mailbox connected on this computer.
MAIL_OPERATION = "tag:mail."
PARAM_TYPES = {"number": "number", "integer": "number", "boolean": "boolean"}


def needs_mailbox(template: dict[str, Any]) -> bool:
    """True when the routine reads or sends mail anywhere in its steps (nested loops and branches included)."""
    def sends(value: Any) -> bool:
        if isinstance(value, dict):
            operation = value.get("operation")
            if isinstance(operation, str) and operation.startswith(MAIL_OPERATION):
                return True
            return any(sends(item) for item in value.values())
        if isinstance(value, list):
            return any(sends(item) for item in value)
        return False

    return template["trigger"]["kind"] == "email" or sends(template["steps"])


class AskFirstChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ask_first: StrictBool


def arms_of(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Steps as the app reads them. A branch always has a `then` and an `otherwise` arm, and an arm the
    node leaves out is empty, which is what it does: nothing on that side. The same goes for a loop's body."""
    out = []
    for step in steps:
        step = dict(step)
        if step.get("kind") == "branch":
            step["then"] = arms_of(step.get("then") or [])
            step["otherwise"] = arms_of(step.get("otherwise") or [])
        elif step.get("kind") == "loop":
            step["body"] = arms_of(step.get("body") or [])
        out.append(step)
    return out


def step_of(step: dict[str, Any]) -> dict[str, Any]:
    """One step of a run, as a `RoutineStepRun`. Optional fields are left out where they are not known."""
    out = {key: step.get(key) for key in ("step", "label", "short", "kind", "status", "started_at", "finished_at",
                                          "input", "output", "note", "attempts", "approval_id")}
    if step.get("untrusted") is not None:
        out["untrusted"] = step["untrusted"]
    if step.get("items") is not None:
        out["items"] = step["items"]
    return out


def run_of(view: dict[str, Any]) -> dict[str, Any]:
    """A run of a workflow, as a `RoutineRun`. `version` is the revision the run was started on."""
    return {
        "id": view["id"], "flow": view["flow"], "version": view.get("revision") or 0,
        "status": view["status"], "cause": view["cause"], "result": view.get("result"), "by": view["by"],
        "started_at": view["started_at"], "finished_at": view.get("finished_at"), "output": view.get("output"),
        "steps": [step_of(step) for step in view.get("steps") or []], "cost": view["cost"],
    }


def trigger_of(row: dict[str, Any]) -> dict[str, Any]:
    """The trigger as the app reads it. A webhook's address is the controller's to build, and a node has
    no public address of its own, so the hook says nothing here rather than a made-up address."""
    trigger = dict(row["trigger"])
    if trigger.get("kind") == "webhook":
        trigger["hook"] = None
    return trigger


def flow_of(row: dict[str, Any]) -> dict[str, Any]:
    """A workflow's row, as a `Flow`. `version` is the number of versions saved, the one the app shows."""
    always = any(item["because"] in PAID for item in row["always_asks_steps"])
    switched = bool(row["ask_first"])
    return {
        "id": row["id"], "lane": row["lane"], "version": row.get("revision") or 0, "params": row.get("params") or {},
        "effect": row["effect"], "last_run": row["last_run"], "recent_runs": row["recent_runs"],
        "ask_first": switched, "ask_first_by": row.get("ask_first_by"), "ask_first_set_by": None,
        "ask_first_set_at": None, **({"ask_first_why": row["ask_first_why"]} if row.get("ask_first_why") else {}),
        "always_asks": always, "asks_first": switched or always,
        "asks_because": "always" if always else "routine" if switched else None,
        "ask_first_supported": True, "ask_first_online": True,
        "name": row["name"], "trigger": trigger_of(row), "steps": arms_of(row["steps"]), "sites": row["sites"],
        "enabled": row["enabled"], "needs_review": bool(row.get("needs_review")), "next_at": row["next_at"],
        "latest": latest_of(row["latest"]), "stuck": row["stuck"], "waiting": row["waiting"],
        "always_asks_steps": row["always_asks_steps"], "send_limits": row["send_limits"], "steps_supported": row["steps_supported"],
    }


def latest_of(latest: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the newest finished run came to. A run that left no words says what its status is."""
    if latest is None:
        return None
    result = latest.get("result") or STATUS_WORDS.get(latest["status"], "Finished")
    return {"at": latest["at"], "result": result, "status": latest["status"]}


def started_by(view: dict[str, Any]) -> tuple[str, str]:
    """How a run of a recorded routine began, as `(by, cause)`: a person's hand, a schedule, a route or an agent."""
    source = str(view.get("source") or "")
    if view.get("schedule_id") or source.startswith(("cron:", "watch:", "schedule:")):
        return "trigger", "On its schedule"
    if source.startswith("route:"):
        return "trigger", "When something came in"
    if (view.get("principal") or {}).get("kind") == "human" or source in ("panel", "cli", "api", "workflow:run"):
        return "person", "Started by a person"
    return "agent", "Started by an agent"


def recorded_params(schema: Any) -> dict[str, dict[str, Any]]:
    """A recorded routine's parameters, from the JSON schema the node keeps, as the app's `FlowParam`s."""
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return {}
    required = set(schema.get("required") or [])
    out: dict[str, dict[str, Any]] = {}
    for name, prop in schema["properties"].items():
        if not isinstance(prop, dict):
            continue
        field: dict[str, Any] = {"type": PARAM_TYPES.get(str(prop.get("type")), "string")}
        label = prop.get("label") or prop.get("title") or prop.get("description")
        if label:
            field["label"] = label
        if name in required or prop.get("required") is True:
            field["required"] = True
        out[name] = field
    return out


def recorded_latest(last: dict[str, Any] | None) -> dict[str, Any] | None:
    """A recorded routine's newest run, in plain words. The node keeps no words of its own for a recording."""
    if last is None:
        return None
    status = last["status"]
    at = last.get("finished_at") or last.get("started_at") or last.get("created_at")
    return {"at": at, "result": RECORDED_WORDS.get(status, "Finished"), "status": status}


def recorded_flow_of(row: dict[str, Any], next_at: str | None) -> dict[str, Any]:
    """A recorded routine, as a `Flow`. The node keeps it as a recording, so it has no steps or trigger for the
    app to draw, and those are left out. Its switch is the node's own Ask me first. It is not "on" or "off" on
    its own: its schedule is, so `enabled` is left out, and `next_at` is its schedule's next run, if it has one."""
    always = row.get("effect") in PAID
    switched = bool(row.get("ask_first"))
    last = row.get("last_run")
    return {
        "id": row["id"], "lane": row["lane"], "name": row.get("title") or None, "version": row.get("version") or "",
        "params": recorded_params(row.get("params")), "effect": row["effect"],
        "last_run": {"id": last["id"], "status": last["status"], "started_at": last.get("started_at") or last.get("created_at"),
                     "cost": last.get("cost") or {"tokens": 0, "usd": 0.0}} if last else None,
        "latest": recorded_latest(last),
        "ask_first": switched, "ask_first_by": row.get("ask_first_by"), "ask_first_set_by": None, "ask_first_set_at": None,
        "always_asks": always, "asks_first": switched or always,
        "asks_because": "always" if always else "routine" if switched else None,
        "ask_first_supported": True, "ask_first_online": True,
        "next_at": next_at, "always_asks_steps": [], "send_limits": [],
    }


def recorded_run_of(view: dict[str, Any]) -> dict[str, Any]:
    """A run of a recorded routine, as a `RoutineRun`. A recording has no steps the app can show, and its run's
    output is what a connector returned, which is not a routine's output, so neither is sent."""
    by, cause = started_by(view)
    status = view["status"]
    return {
        "id": view["id"], "flow": view["flow"], "version": view.get("version") or "",
        "status": status, "cause": cause, "result": RECORDED_WORDS.get(status), "by": by,
        "started_at": view.get("started_at") or view.get("created_at"), "finished_at": view.get("finished_at"),
        "output": None, "steps": [], "cost": view["cost"],
    }


def run_started(view: dict[str, Any], flow_id: str, lane: str | None, principal: dict[str, Any]) -> dict[str, Any]:
    """A run a person started by hand, as the app's `Run`: the run as the list shows it, with who started it."""
    return {
        "id": view["id"], "flow": flow_id, "lane": lane, "status": view["status"],
        "summary": view.get("summary") or view["status"], "principal": principal,
        "started_at": view.get("started_at") or view.get("created_at"), "finished_at": view.get("finished_at"),
        "cost": view["cost"], "proof": None,
    }


def conditions_in_full(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A condition such as "not empty" has nothing on its right. The app's type always has the field."""
    for step in steps:
        if isinstance(step.get("when"), dict):
            step["when"].setdefault("right", "")
        for key in ("then", "otherwise", "steps"):
            if isinstance(step.get(key), list):
                conditions_in_full(step[key])
    return steps


def site_of(lane: str) -> str:
    """The site a lane is on: `biz-a/social` is `social`."""
    return lane.split("/", 1)[1] if "/" in lane else lane


async def recorded_rows(request: Request) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The workflows and the recorded routines the node keeps, the recordings only: a workflow is never listed twice."""
    workflows = await read(request, "/api/workflows")
    listed = {row["id"] for row in workflows}
    return workflows, [row for row in await read(request, "/api/flows") if row["id"] not in listed]


async def next_fires(request: Request) -> dict[str, str]:
    """When each recorded routine runs next: the earliest switched-on schedule the node keeps for it."""
    reply = await call(request, "GET", "/api/schedules")
    out: dict[str, str] = {}
    for row in reply.json() if reply.status == 200 else []:
        at, flow = row.get("next_fire_at"), row.get("flow")
        if at and flow and row.get("enabled") is not False and (flow not in out or at < out[flow]):
            out[flow] = at
    return out


async def is_workflow(request: Request, flow_id: str) -> bool:
    """Is this routine a workflow the app builds, rather than a recording? Only the node can say."""
    return (await call(request, "GET", f"/api/workflows/{flow_id}")).status == 200


async def recording_of(request: Request, flow_id: str) -> dict[str, Any]:
    """A recorded routine the caller may see, or a 404 as for one that is not there."""
    for row in await read(request, "/api/flows"):
        if row["id"] == flow_id:
            return row
    raise ApiError("not_found", f"No routine {flow_id}.")


def routes(api: Api) -> list[BaseRoute]:
    zone = ZoneInfo(api.timezone)

    async def flows(request: Request) -> list[dict[str, Any]]:
        workflows, recorded = await recorded_rows(request)
        out = [flow_of(row) for row in workflows]
        if recorded:
            fires = await next_fires(request)
            out += [recorded_flow_of(row, fires.get(row["id"])) for row in recorded]
        return out

    async def flow_create(request: Request) -> dict[str, Any]:
        body = await read_json_object(request)
        return flow_of(await write(request, "POST", "/api/workflows", body))

    async def flow_save(request: Request) -> dict[str, Any]:
        body = await read_json_object(request)
        return flow_of(await write(request, "PUT", f"/api/workflows/{request.path_params['id']}", body))

    async def flow_enable(request: Request) -> dict[str, Any]:
        """Turns a workflow on, through the node's own route, which refuses a token and checks it is finished. A
        workflow started by a call from outside shows its signing secret once, so it is turned on at the computer."""
        flow_id = request.path_params["id"]
        body = await read_json_object(request)
        current = await read(request, f"/api/workflows/{flow_id}")
        if current["trigger"]["kind"] == "webhook":
            raise ApiError("invalid", "A routine started by a call from your system shows its signing "
                                                      "secret once, so it is turned on at the computer.")
        version = body.get("version")
        return flow_of(await write(request, "POST", f"/api/workflows/{flow_id}/enable", {"version": version} if version else {}))

    async def flow_ask_first(request: Request) -> dict[str, Any]:
        flow_id = request.path_params["id"]
        body = await read_body(request, AskFirstChange)
        await write(request, "POST", "/api/ask-first", {"scope": "flow", "id": flow_id, "ask_first": body.ask_first})
        if await is_workflow(request, flow_id):
            return flow_of(await read(request, f"/api/workflows/{flow_id}"))
        row = await recording_of(request, flow_id)  # the recording as the list has it, so its switch reads as it is
        return recorded_flow_of(row, (await next_fires(request)).get(flow_id))

    async def flow_run(request: Request) -> dict[str, Any]:
        """A run started by a person, under the grant of whoever saved the routine. The app's `Run` is
        the run as the list shows it, with the caller as who started it."""
        flow_id = request.path_params["id"]
        body = await read_json_object(request)
        params = body.get("params")
        principal = principal_of(request).model_dump(exclude_none=True)
        if not await is_workflow(request, flow_id):
            row = await recording_of(request, flow_id)
            view = await write(request, "POST", f"/api/flows/{flow_id}/run", {"params": params or {}})
            return run_started(view, flow_id, row["lane"], principal)
        view = await write(request, "POST", f"/api/workflows/{flow_id}/run", {"values": params or {}})
        flow = await read(request, f"/api/workflows/{flow_id}")
        return {
            "id": view["id"], "flow": flow_id, "lane": flow["lane"], "status": view["status"],
            "summary": view.get("result") or view["cause"], "principal": principal,
            "started_at": view["started_at"], "finished_at": view.get("finished_at"), "cost": view["cost"],
            "proof": None,
        }

    async def flow_runs(request: Request) -> list[dict[str, Any]]:
        flow_id = request.path_params["id"]
        if await is_workflow(request, flow_id):
            return [run_of(view) for view in await read(request, f"/api/workflows/{flow_id}/runs")]
        await recording_of(request, flow_id)  # not there: 404, before anything is read
        return [recorded_run_of(view) for view in await read(request, "/api/runs", query={"flow": flow_id, "limit": 50})]

    async def flow_retry(request: Request) -> dict[str, Any]:
        flow_id, run_id = request.path_params["id"], request.path_params["run"]
        return run_of(await write(request, "POST", f"/api/workflows/{flow_id}/runs/{run_id}/retry", {}))

    async def catalog(request: Request) -> dict[str, Any]:
        found = await read(request, "/api/workflows/catalog")
        templates = [template for template in found["templates"] if template["trigger"]["kind"] in TRIGGERS_HERE]
        return {**found, "templates": [
                    {**template, "steps": conditions_in_full(arms_of(template["steps"])),
                     **({"needs": ["mailbox"]} if needs_mailbox(template) else {})}
                    for template in templates],
                "triggers": TRIGGERS_HERE}

    async def day(request: Request) -> dict[str, Any]:
        """What ran today and what is planned in the next day, for the line through now. A workflow's part is the
        node's; a recorded routine's runs are read from the node's runs, and its next run from its schedule."""
        found = await read(request, "/api/workflows/day")
        _, recorded = await recorded_rows(request)
        if not recorded:
            return found
        now = datetime.now(UTC)
        midnight = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        ids = {row["id"]: site_of(row["lane"]) for row in recorded}
        marks, ran, checked = list(found["marks"]), found["ran_today"], found["checked_today"]
        for view in await read(request, "/api/runs", query={"limit": 200}):
            at = view.get("started_at") or view.get("created_at")
            if view.get("flow") not in ids or not at:
                continue
            when = datetime.fromisoformat(at)
            if not now - timedelta(days=1) <= when <= now:
                continue
            marks.append({"flow": view["flow"], "at": at, "site": ids[view["flow"]], "status": view["status"]})
            if when >= midnight:
                ran += 1
                checked += int(view["status"] == "succeeded_verified")
        for flow_id, at in (await next_fires(request)).items():
            when = datetime.fromisoformat(at)
            if flow_id in ids and now < when <= now + timedelta(days=1):
                marks.append({"flow": flow_id, "at": at, "site": ids[flow_id], "status": None})
        return {"marks": sorted(marks, key=lambda mark: mark["at"]), "ran_today": ran, "checked_today": checked}

    return [
        org_route(api, "/flows", flows, ["GET"]),
        org_route(api, "/flows", flow_create, ["POST"]),
        org_route(api, "/flows/{id}/routine", flow_save, ["PUT"]),
        org_route(api, "/flows/{id}/enable", flow_enable, ["POST"]),
        org_route(api, "/flows/{id}/ask-first", flow_ask_first, ["PUT"]),
        org_route(api, "/flows/{id}/run", flow_run, ["POST"]),
        org_route(api, "/flows/{id}/runs", flow_runs, ["GET"]),
        org_route(api, "/flows/{id}/runs/{run}/retry", flow_retry, ["POST"]),
        org_route(api, "/routines/catalog", catalog, ["GET"]),
        org_route(api, "/routines/day", day, ["GET"]),
    ]
