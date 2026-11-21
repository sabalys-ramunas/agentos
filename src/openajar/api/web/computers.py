"""Computers: the one computer this node is, as the app's Computers pages read it.

The node's own routes (`openajar.api.routes_computers`) answer most of the cloud's shapes, so the
system, programs, services, schedules, tools, health, connections, metrics and logs pass through.
Two things are reshaped here:

- the list: each row is cut to `FleetNode`, because a row carries the detail the list does not show;
- the detail: the node's lanes come in the panel's shape, and each is shaped to `Lane`. What a node
  cannot know about a lane (its signal's rule, its account, a takeback request) is null or left out.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from starlette.requests import Request

from openajar.api.app import Api, access_of
from openajar.api.web.accounts import checks_of
from openajar.api.web.base import call, control_of, holder_names, org_route, read
from openajar.api.web.jobs import manager as job_manager

FLEET_NODE = (
    "id",
    "state",
    "capabilities",
    "cpu",
    "mem",
    "warm_lanes",
    "max_warm_lanes",
    "last_seen",
    "version",
    "degraded",
    "timezone",
)


def app_lane(lane: dict[str, Any], holders: Mapping[str, str],
             checks: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """A lane of this node as `Lane`. The node does not keep a takeback request, the rule that
    matched a signal, or the account a lane is pinned to, so those are null. `frozen` and `check` are
    what Accounts reads too, so the computer's rows and the account's page say the same word."""
    control = control_of(lane.get("control"), holders)
    return {
        "id": lane["id"],
        "workspace": lane["workspace"],
        "name": lane["name"],
        "kind": lane["kind"],
        "engine": lane.get("engine"),
        "preset": lane.get("preset") or "account",
        "state": lane["state"],
        "signal": lane.get("signal") or "unknown",
        "proxy": lane.get("proxy"),
        "exit_ip": lane.get("exit_ip"),
        "last_used": lane.get("last_used"),
        "node": lane.get("node") or "",
        "quarantine_reason": lane.get("quarantine_reason"),
        "logged_in_hint": lane.get("logged_in_hint") or "unknown",
        "control": {
            "state": control.get("state") or "agent",
            "by": control.get("by"),
            "by_id": control.get("by_id"),
            "since": control.get("since"),
            "takeback_reason": None,
            "takeback_by": None,
            "last_human_input": None,
        },
        "hold": lane.get("hold"),
        "hold_until": lane.get("hold_until"),
        "signal_detail": None,
        "account": None,
        "frozen": lane.get("frozen") is True,
        "check": (checks or {}).get(lane["id"]),
        "login_request": lane.get("login_request"),
    }


VM_FIELDS = ("id", "lane", "state", "profile", "server_type", "location", "created_at", "ready_at", "expires_at",
             "minutes_left", "price_hourly_eur", "cost_eur_so_far")
NO_VMS = "This computer rents no cloud machines: no VM provider is set up on it."


async def busy(request: Request, api: Api, detail: Mapping[str, Any], waiting: int | None) -> dict[str, Any]:
    """What a computer is doing now, from what it really has: runs going in its lanes, its lanes by kind and
    how many a call holds, and the approvals waiting that the caller may see. Left out when not known."""
    access = access_of(request)
    lanes = [lane for lane in detail.get("lanes") or [] if isinstance(lane, dict)]
    out: dict[str, Any] = {
        "lanes_by_kind": dict(Counter(str(lane.get("kind")) for lane in lanes if lane.get("kind"))),
        "lanes_active": sum(1 for lane in lanes if lane.get("state") == "active"),
    }
    mgr = job_manager(api)
    if mgr is not None:
        out["jobs_running"] = sum(1 for info in mgr.job_list() if info.state == "running"
                                  and access.lane_allowed(info.lane))
    if waiting is not None:
        out["approvals_waiting"] = waiting
    return out


async def approvals_waiting(request: Request) -> int | None:
    reply = await call(request, "GET", "/api/approvals", query={"state": "pending", "limit": 200})
    rows = reply.json() if reply.status == 200 else None
    if not isinstance(rows, list):
        return None
    return sum(1 for item in rows if isinstance(item, dict) and item.get("kind") != "question")


def vm_out(vm: Mapping[str, Any]) -> dict[str, Any]:
    by = vm.get("created_by")
    purpose = str(vm.get("purpose") or "")
    return {**{key: vm.get(key) for key in VM_FIELDS},
            "purpose": purpose if len(purpose) <= 300 else purpose[:299] + "…",
            # a grant id is never shown: the owner by name, anyone else as an agent
            "started_by": "The owner" if by == "owner" else ("An agent" if by else None)}


def routes(api: Api) -> list:
    async def nodes(request: Request) -> list[dict[str, Any]]:
        out = []
        waiting = await approvals_waiting(request)
        for row in await read(request, "/api/nodes"):
            detail = await read(request, f"/api/nodes/{quote(str(row['id']), safe='')}")
            # one computer per node: what waits for a person waits on it
            out.append({**{key: detail.get(key) for key in FLEET_NODE},
                        **(await busy(request, api, detail, waiting))})
        return out

    async def vms(request: Request) -> dict[str, Any]:
        svc = api.optional("vms")
        if svc is None or not callable(getattr(svc, "list", None)):
            return {"available": False, "reason": NO_VMS, "vms": []}
        access = access_of(request)
        rows = [vm_out(vm) for vm in svc.list(include_past=True, limit=50)
                if isinstance(vm, dict) and vm.get("lane") and access.lane_allowed(str(vm["lane"]))]
        return {"available": True, "vms": rows}

    async def join_tokens(request: Request) -> Any:
        # Listed before `{id}`, which would otherwise take `join-tokens` for a computer's name.
        return await read(request, "/api/nodes/join-tokens")

    async def node(request: Request) -> dict[str, Any]:
        detail = await read(request, f"/api/nodes/{quote(request.path_params['id'], safe='')}")
        holders = await holder_names(request)
        checks = await checks_of(request)
        return {**detail, **(await busy(request, api, detail, await approvals_waiting(request))),
                "lanes": [app_lane(lane, holders, checks) for lane in detail["lanes"]]}

    return [
        org_route(api, "/nodes", nodes, ["GET"]),
        org_route(api, "/nodes/join-tokens", join_tokens, ["GET"]),
        org_route(api, "/nodes/{id}", node, ["GET"]),
        # a VM is a computer the owner rents, and Computers are the owner's (as /nodes): a member is refused
        org_route(api, "/vms", vms, ["GET"], owner=True),
    ]
