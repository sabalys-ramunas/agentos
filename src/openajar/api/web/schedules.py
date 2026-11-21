"""Schedules on a node, as the app reads them (web/src/api/types.ts, `Schedule`).

A schedule is one the node keeps. One that starts a routine is shown with that routine as its `flow`. A
builder routine's own schedule (its time is set in the routine) is shown under the routine's name, marked
`routine`, and it is on while the routine is on: switching it switches the routine, through the routine's
own route, so a schedule cannot be turned on by a token. Any other schedule, one that starts nothing the
app knows (an event route's own trigger, say), is shown too, with no routine (`flow` is empty), so that a
person can still pause it or remove it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute

from openajar.api.app import Api, read_body, read_json_object
from openajar.api.errors import ApiError
from openajar.api.web.base import org_route, read, write

CREATE_FIELDS = ("flow", "cron", "watch", "params", "deadman_minutes")


class EnabledChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


def schedule_of(row: dict[str, Any], flow: str, *, routine: bool = False, enabled: bool | None = None) -> dict[str, Any]:
    """A node schedule, as a `Schedule`: the routine or flow it starts, and when it next runs. A routine's own
    schedule is on only while its routine is on, so `enabled` is then the routine's and the schedule's together."""
    on = row.get("enabled") if enabled is None else enabled
    out = {
        "id": row["id"], "flow": flow, "cron": row.get("cron"), "watch": row.get("watch"),
        "params": row.get("params") or {}, "deadman_minutes": row.get("deadman_minutes"),
        "next_run": row.get("next_run"), "enabled": on, "next_fire_at": row.get("next_fire_at") if on is not False else None,
        "lane": row.get("lane"), "last_run": row.get("last_run"),
    }
    return {**out, "routine": True} if routine else out


async def routine_owners(request: Request) -> dict[str, dict[str, Any]]:
    """The workflow each routine-owned schedule belongs to, by the schedule's id."""
    rows = await read(request, "/api/workflows")
    return {row["schedule_id"]: row for row in rows if row.get("schedule_id")}


def owned(row: dict[str, Any], owners: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The routine a schedule belongs to, as its own `Schedule` (on only while the routine is on), or None."""
    owner = owners.get(row["id"])
    if owner is None:
        return None
    return schedule_of(row, owner["id"], routine=True, enabled=row.get("enabled") is not False and owner.get("enabled") is not False)


def routes(api: Api) -> list[BaseRoute]:
    async def schedules(request: Request) -> list[dict[str, Any]]:
        owners = await routine_owners(request)
        return [owned(row, owners) or schedule_of(row, row.get("flow") or "") for row in await read(request, "/api/schedules")]

    async def schedule(request: Request) -> dict[str, Any]:
        sid = request.path_params["id"]
        owners = await routine_owners(request)
        row = await read(request, f"/api/schedules/{sid}")
        return owned(row, owners) or schedule_of(row, row.get("flow") or "")

    async def set_enabled(request: Request) -> dict[str, Any]:
        """A routine's schedule follows the routine: turning it on is the routine's own enable, which
        refuses a token. Any other schedule is the node's own switch, which refuses a token to start it."""
        sid = request.path_params["id"]
        body = await read_body(request, EnabledChange)
        owner = (await routine_owners(request)).get(sid)
        if owner is None:
            row = await write(request, "PATCH", f"/api/schedules/{sid}", {"enabled": body.enabled})
            return schedule_of(row, row.get("flow") or "")
        await write(request, "POST", f"/api/workflows/{owner['id']}/{'enable' if body.enabled else 'disable'}")
        row = await read(request, f"/api/schedules/{sid}")
        routine = await read(request, f"/api/workflows/{owner['id']}")
        return owned(row, {sid: routine}) or schedule_of(row, owner["id"], routine=True)

    async def delete(request: Request) -> Response:
        """A routine's schedule stops when it is deleted, and the routine stays, as the words say. The node
        keeps the schedule, so it is only switched off there, and the routine's own disable does that."""
        sid = request.path_params["id"]
        owner = (await routine_owners(request)).get(sid)
        if owner is None:
            await write(request, "DELETE", f"/api/schedules/{sid}")
        else:
            await write(request, "POST", f"/api/workflows/{owner['id']}/disable")
        return Response(status_code=204)

    async def create(request: Request) -> dict[str, Any]:
        body = await read_json_object(request)
        routines = {row["id"] for row in await read(request, "/api/workflows")}
        if body.get("flow") in routines:
            raise ApiError("invalid", "A routine's time is set on the routine itself. Change how it starts there.")
        row = await write(request, "POST", "/api/schedules", {key: body.get(key) for key in CREATE_FIELDS})
        return schedule_of(row, row.get("flow") or "")

    async def node_schedules(request: Request) -> list[dict[str, Any]]:
        """The computer's own report, with each schedule named for the routine or flow it belongs to."""
        rows = await read(request, f"/api/nodes/{quote(request.path_params['id'], safe='')}/schedules")
        flows = {item["id"]: item["flow"] for item in await schedules(request) if item["flow"]}
        return [{**row, "flow": flows[row["id"]]} for row in rows or [] if row["id"] in flows]

    return [
        org_route(api, "/nodes/{id}/schedules", node_schedules, ["GET"]),
        org_route(api, "/schedules", schedules, ["GET"]),
        org_route(api, "/schedules", create, ["POST"]),
        org_route(api, "/schedules/{id}", schedule, ["GET"]),
        org_route(api, "/schedules/{id}", set_enabled, ["PATCH"]),
        org_route(api, "/schedules/{id}", delete, ["DELETE"]),
    ]
