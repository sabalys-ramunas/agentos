"""Surfaces and connectors on a node, as the app reads them (web/src/api/types.ts: `Surface`,
`ConnectorOperation`).

A surface is what an account is connected to (who it acts as, through which lane). A connector
operation is what it can do, with the health of each surface it runs on (docs/design/connectors.md,
`agentos.runner.connectors.api`). The writes are passed on with the caller's own cookie and CSRF
header, so the node's own refusals reach the app as they are.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.routing import BaseRoute

from agentos.api.app import Api, read_json_object
from agentos.api.web.base import org_route, read, write

# A surface's id is `workspace/name`: the lane's shape, so it splits in two for the app.


def surface_of(row: dict[str, Any]) -> dict[str, Any]:
    """A surface, as the app shows it: who it acts as and the lane it goes through, nothing internal."""
    workspace, _, name = str(row["id"]).partition("/")
    acts_as = row.get("acts_as") or {}
    return {
        "id": row["id"], "workspace": workspace, "name": name, "site": row["site"], "lane": row["lane"],
        "acts_as": {"kind": acts_as.get("kind"), "name": acts_as.get("name"), "id": acts_as.get("id"),
                    "user_id": acts_as.get("user_id")},
        "account_language": row.get("account_language") or "en", "title": row.get("title"),
    }


def operation_of(row: dict[str, Any]) -> dict[str, Any]:
    """A connector operation with its surfaces. `version` and `contract` are for Technical details."""
    surfaces = []
    for s in row.get("surfaces") or []:
        out = {"surface": s["surface"], "acts_as": s.get("acts_as"), "health": s.get("health") or "unknown",
               "lane": s.get("lane")}
        if s.get("health_detail"):
            out["health_detail"] = s["health_detail"]
        surfaces.append(out)
    return {
        "operation": row["operation"], "tool": row["tool"], "title": row["title"], "effect": row["effect"],
        "acts_as": row.get("acts_as"), "version": row.get("version"), "contract": row.get("contract"),
        "surfaces": surfaces,
    }


def routes(api: Api) -> list[BaseRoute]:
    # --- surfaces and connectors -----------------------------------------------------------------
    async def surfaces(request: Request) -> list[dict[str, Any]]:
        return [surface_of(row) for row in await read(request, "/api/surfaces")]

    async def surface_add(request: Request) -> dict[str, Any]:
        body = await read_json_object(request)
        return surface_of(await write(request, "POST", "/api/surfaces", body))

    async def surface(request: Request) -> dict[str, Any]:
        ws, name = request.path_params["ws"], request.path_params["name"]
        return surface_of(await read(request, f"/api/surfaces/{ws}/{name}"))

    async def surface_remove(request: Request) -> dict[str, Any]:
        ws, name = request.path_params["ws"], request.path_params["name"]
        return await write(request, "DELETE", f"/api/surfaces/{ws}/{name}")

    async def connectors(request: Request) -> list[dict[str, Any]]:
        return [operation_of(row) for row in await read(request, "/api/connectors")]

    async def connector_run(request: Request) -> dict[str, Any]:
        body = await read_json_object(request)
        return await write(request, "POST", f"/api/connectors/{request.path_params['operation']}/run", body)

    return [
        org_route(api, "/surfaces", surfaces, ["GET"]),
        org_route(api, "/surfaces", surface_add, ["POST"]),
        org_route(api, "/surfaces/{ws}/{name}", surface, ["GET"]),
        org_route(api, "/surfaces/{ws}/{name}", surface_remove, ["DELETE"]),
        org_route(api, "/connectors", connectors, ["GET"]),
        org_route(api, "/connectors/{operation}/run", connector_run, ["POST"]),
    ]
