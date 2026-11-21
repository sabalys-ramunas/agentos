"""The accounts area of the app: status, workspaces, templates, the login queue, and every account
(lane) a screen reads or changes.

Each route runs the node's own route inside this request (`read` and `write`), so who may do what
is decided there. What this module does is reshape the answer to the type in `web/src/api/types.ts`:
a lane's control, a workspace's lanes, a template's surfaces, an ask-first switch per account.

Where the node has no such thing (a plan's limits, a lane's account or signal detail), the field is
`null`, or left out where the type makes it optional. Nothing here makes up a number.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.routing import BaseRoute

from openajar.api.actors import OWNER_SPELLINGS
from openajar.api.app import Api
from openajar.api.errors import ApiError
from openajar.api.web.base import Refused, control_of, holder_names, org_route, read, write
from openajar.api.web.environments import without_environments
from openajar.core.app_paths import lane_viewer_path

"""What a node is to the app: a computer its owner runs, with no plan caps (session.py says the same)."""

LANE_FIELDS = ("id", "workspace", "name")
CREATE_FIELDS = ("id", "kind", "preset", "proxy_label", "engine", "device")
WORKSPACE_FIELDS = ("name", "color")


async def _body(request: Request) -> dict[str, Any]:
    """The JSON object a request sent. Anything else is the caller's mistake, said plainly."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise ApiError("invalid", "Send a JSON object.")
    return body


def lane_path(ws: str, name: str, *tail: str) -> str:
    """The node's path for a lane, each value one segment so an address cannot climb out of it."""
    return "/".join(["/api/lanes", quote(ws, safe=""), quote(name, safe=""), *tail]).rstrip("/")


def lane_for_app(lane: dict[str, Any], holders: Mapping[str, str] | None = None,
                 checks: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """A node lane as the app's `Lane`. The node's `viewer` is a live view path, which the app
    reads as a URL; `account` and `signal_detail` are not known here, so they are null. `check` is the
    lane's own last check (its level and whether it is safe to use), when the caller asked for checks."""
    out = dict(lane)
    out["control"] = control_of(lane.get("control"), holders or {"owner": "The owner"})
    viewer = lane.get("viewer")
    out["viewer"] = {"url": lane_viewer_path(lane["workspace"], lane["name"])} if viewer else None
    out.setdefault("signal_detail", None)
    out.setdefault("account", None)
    if checks is not None:
        out["check"] = checks.get(lane.get("id", ""))
    return out


async def checks_of(request: Request) -> dict[str, dict[str, Any]]:
    """Each lane's own check from its last run: the level and whether the node says it is safe to use.
    The same brief the Accounts check reads, so nothing is probed. A refusal means no check is shown."""
    try:
        listed = await read(request, "/api/lanes/checks")
    except Refused:
        return {}
    return {row["lane"]: {"level": row["level"], "safe": row["safe"]} for row in listed.get("lanes", [])}


def workspace_for_app(workspace: dict[str, Any], holders: Mapping[str, str] | None = None,
                      checks: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    return {**workspace, "lanes": [lane_for_app(lane, holders, checks) for lane in workspace.get("lanes") or []]}


def template_for_app(template: dict[str, Any]) -> dict[str, Any]:
    """A template as the app's `TemplateInfo`: its name is a title, and each lane a surface."""
    return {
        "id": template["id"],
        "title": template.get("name") or template["id"],
        "description": template.get("description") or "",
        "surfaces": [
            {"name": lane["name"], "platform": lane.get("platform") or lane["name"]}
            for lane in template.get("lanes") or []
        ],
    }


def slug(name: str) -> str | None:
    """A workspace id from its name, when the app does not send one: lowercase, dashes, the node's rule.
    None when the name has no letters or numbers to make an address from."""
    text = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:31].rstrip("-")
    return text or None


def set_by_for_app(row: dict[str, Any]) -> dict[str, Any] | None:
    """Who set a switch, as the app's `AskFirstSetBy`: the computer's owner, an agent, or a member."""
    who = row.get("set_by")
    if not who:
        return None
    if row.get("by_person") and who == "owner":
        return {"kind": "computer", "id": None}
    if who.startswith("agent:"):
        return {"kind": "agent", "id": who[len("agent:"):]}
    if who in OWNER_SPELLINGS:  # the owner, by hand: `owner:cli` is their command line (openajar.api.actors)
        return {"kind": "user", "id": "owner", **({"via": "cli"} if who == "owner:cli" else {})}
    return {"kind": "user", "id": who}


def ask_first_for_app(api: Api, lane: str, row: dict[str, Any] | None) -> dict[str, Any]:
    """One account's "Ask me first": the switch as it was set, or as this node's default has it."""
    service = api.optional("ask_first")
    if row is not None:
        on = bool(row["ask_first"])
    else:
        on = bool(service.default_for("lane", lane)) if service is not None else False
    return {
        "lane": lane,
        "ask_first": on,
        "asks_first": on,  # this node has the switch, so what it says is what happens
        "ask_first_set_by": set_by_for_app(row) if row is not None else None,
        "ask_first_set_at": row.get("set_at") if row is not None else None,
        "ask_first_supported": service is not None,
        "ask_first_online": True,
    }


def routes(api: Api) -> list[BaseRoute]:
    # ---- status ----
    async def status(request: Request) -> dict[str, Any]:
        node = await read(request, "/api/status")
        brain = node.get("brain") or {}
        out: dict[str, Any] = {
            "node": node["node"],
            "version": node["version"],
            "csrf": node["csrf"],
            "needs_you": node.get("needs_you_all", node["needs_you"]),
            "needs_you_detail": node.get("needs_you_detail"),
            "timezone": node.get("timezone"),
            "cost_today": node["cost_today"],
            "brain": {"name": brain["name"], "state": brain["state"], "offline_ready": brain["offline_ready"]}
            if brain.get("name") else None,
            "nodes": [{"id": item["id"], "state": item["state"], "degraded": item["degraded"]} for item in node["nodes"]],
            # Where an agent on this computer reaches openajar: the computer's own address, not the cloud's.
            # A computer sends no plan: it has nothing to pay for, so there is nothing to show.
            "mcp_url": api.mcp_url,
        }
        # The usage is counted only where the node lets the caller see it: members are the owner's to list.
        if (node.get("principal") or {}).get("owner"):
            workspaces = await read(request, "/api/workspaces")
            members = await read(request, "/api/members")
            out["usage"] = {
                "nodes": len(node["nodes"]),
                "active_lanes": node["lanes"]["total"],
                "workspaces": len(workspaces),
                "members": len(members),
            }
        return out

    # ---- workspaces ----
    async def workspaces(request: Request) -> list[dict[str, Any]]:
        holders = await holder_names(request)
        checks = await checks_of(request)
        listed = without_environments(api, await read(request, "/api/workspaces"))
        return [workspace_for_app(item, holders, checks) for item in listed]

    async def workspace_get(request: Request) -> dict[str, Any]:
        """One business, with its accounts. What the caller may see of the list is what they may see of it."""
        ws = request.path_params["id"]
        holders = await holder_names(request)
        checks = await checks_of(request)
        for item in await read(request, "/api/workspaces"):
            if item["id"] == ws:
                shown = without_environments(api, [item]) or [{**item, "lanes": []}]
                return workspace_for_app(shown[0], holders, checks)
        raise ApiError("not_found", "No such workspace.")

    async def workspace_create(request: Request) -> dict[str, Any]:
        body = await _body(request)
        name = body.get("name")
        given = body.get("id")
        if not given and isinstance(name, str):
            given = slug(name.strip()) or ""  # no address from this name: the node refuses it, after it checks the caller
        payload = {"id": given or "", "name": name.strip() if isinstance(name, str) else name,
                   **{k: body[k] for k in ("color",) if body.get(k)}}
        return workspace_for_app(await write(request, "POST", "/api/workspaces", payload))

    async def workspace_update(request: Request) -> dict[str, Any]:
        ws = request.path_params["id"]
        body = await _body(request)
        await write(request, "PATCH", f"/api/workspaces/{quote(ws, safe='')}",
                    {k: body[k] for k in WORKSPACE_FIELDS if k in body})
        for item in await read(request, "/api/workspaces"):  # the node's answer is its own shape: read it back
            if item["id"] == ws:
                return workspace_for_app(item)
        raise ApiError("not_found", "No such workspace.")

    # ---- templates ----
    async def templates(request: Request) -> dict[str, Any]:
        return {"templates": [template_for_app(item) for item in await read(request, "/api/templates")]}

    async def template_apply(request: Request) -> dict[str, Any]:
        result = await write(request, "POST", "/api/templates/apply", await _body(request))
        created = [lane["id"] if isinstance(lane, dict) else lane for lane in result.get("created") or []]
        return {
            "status": result.get("status") or "complete",
            "created": created,
            "existing": result.get("existing", result.get("skipped", [])),
            "failed": result.get("failed"),
            "remaining": result.get("remaining", []),
            "warnings": result.get("warnings", []),
        }

    # ---- lanes ----
    async def lane_get(request: Request) -> dict[str, Any]:
        ws, name = request.path_params["ws"], request.path_params["name"]
        lane = await read(request, lane_path(ws, name))
        return lane_for_app(lane, await holder_names(request), await checks_of(request))

    async def lane_create(request: Request) -> dict[str, Any]:
        body = await _body(request)
        payload = {k: body[k] for k in CREATE_FIELDS if body.get(k) is not None}
        return lane_for_app(await write(request, "POST", "/api/lanes", payload))

    async def lane_open(request: Request) -> dict[str, Any]:
        ws, name = request.path_params["ws"], request.path_params["name"]
        return lane_for_app(await write(request, "POST", lane_path(ws, name, "open")))

    async def lane_close(request: Request) -> dict[str, Any]:
        ws, name = request.path_params["ws"], request.path_params["name"]
        return lane_for_app(await write(request, "POST", lane_path(ws, name, "close")))

    async def lane_control(request: Request) -> dict[str, Any]:
        ws, name = request.path_params["ws"], request.path_params["name"]
        body = await _body(request)
        # The node answers with its own lane shape and whether control changed; the app wants the lane.
        await write(request, "POST", lane_path(ws, name, "control"), {"action": body.get("action")})
        return lane_for_app(await read(request, lane_path(ws, name)), await holder_names(request))

    async def lane_release(request: Request) -> dict[str, Any]:
        ws, name = request.path_params["ws"], request.path_params["name"]
        await read(request, lane_path(ws, name))  # the account must be this computer's to name
        raise ApiError("conflict", "Nothing here is missing. This computer reports every account it has, "
                                   "so there is none to release.")

    async def ask_first_list(request: Request) -> list[dict[str, Any]]:
        lanes = await read(request, "/api/lanes")
        rows = {item["id"]: item for item in await read(request, "/api/ask-first") if item.get("scope") == "lane"}
        return [ask_first_for_app(api, lane["id"], rows.get(lane["id"])) for lane in lanes]

    async def lane_ask_first_set(request: Request) -> dict[str, Any]:
        ws, name = request.path_params["ws"], request.path_params["name"]
        body = await _body(request)
        lane = f"{ws}/{name}"
        row = await write(request, "POST", "/api/ask-first",
                          {"scope": "lane", "id": lane, "ask_first": body.get("ask_first")})
        return ask_first_for_app(api, lane, row)

    return [
        org_route(api, "/status", status, ["GET"]),
        org_route(api, "/workspaces", workspaces, ["GET"]),
        org_route(api, "/workspaces", workspace_create, ["POST"]),
        org_route(api, "/workspaces/{id}", workspace_get, ["GET"]),
        org_route(api, "/workspaces/{id}", workspace_update, ["PATCH"]),
        org_route(api, "/templates", templates, ["GET"]),
        org_route(api, "/templates/apply", template_apply, ["POST"]),
        org_route(api, "/lanes", lane_create, ["POST"]),
        org_route(api, "/lanes/{ws}/{name}", lane_get, ["GET"]),
        org_route(api, "/lanes/{ws}/{name}/open", lane_open, ["POST"]),
        org_route(api, "/lanes/{ws}/{name}/close", lane_close, ["POST"]),
        org_route(api, "/lanes/{ws}/{name}/control", lane_control, ["POST"]),
        org_route(api, "/lanes/{ws}/{name}/release", lane_release, ["POST"], owner=True),
        org_route(api, "/ask-first", ask_first_list, ["GET"]),
        org_route(api, "/lanes/{ws}/{name}/ask-first", lane_ask_first_set, ["PUT"]),
    ]
