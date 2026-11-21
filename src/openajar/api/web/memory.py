"""What agents remember about this person's work, as the app reads it (web/src/api/types.ts, `MemoryEntry`).

The node's `/api/memory` routes do the work and the checks (routes_memory.py). This module only shapes what
they answer into the app's words: an entry's title, who wrote it and how far it is trusted, and its text,
which is always plain text. Every call is the owner's, in person, so a refusal goes back as the node sent it.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute

from openajar.api.app import Api
from openajar.api.web.base import Refused, call, org_route, read, write
from openajar.core.errors import InvalidRequest

WORKSPACE_KEYS = ("workspace", "live_entries", "live_entries_cap", "content_bytes", "content_bytes_cap", "waiting",
                  "chain_ok", "forgotten")
ENTRY_KEYS = ("id", "type", "title", "trust", "state", "suspicious", "pinned", "labels", "lane", "writer", "at",
              "version", "snippet", "withheld")


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        raise InvalidRequest("The request body must be JSON.") from None
    if not isinstance(body, dict):
        raise InvalidRequest("The request body must be an object.")
    return body


def _workspace_of(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in WORKSPACE_KEYS}


def _entry_of(row: dict[str, Any], workspace: str) -> dict[str, Any]:
    out = {key: row.get(key) for key in ENTRY_KEYS}
    out["workspace"] = workspace
    out["reviewed"] = None if "reviewed_at" not in row else row.get("reviewed_at") is not None
    out["expires_at"] = row.get("expires_at")
    return out


def _detail_of(data: dict[str, Any]) -> dict[str, Any]:
    """One entry in full: its text as text, its evidence and its versions, without the node's internals."""
    prov = data.get("prov") or {}
    forgotten = data.get("tombstone")
    return {
        "id": data["id"], "workspace": data["workspace"], "type": data["type"],
        "state": data["state"], "version": data["version"], "current": data["current"], "trust": data["trust"],
        "labels": data.get("labels") or [], "pinned": bool(data.get("pinned")),
        "suspicious": bool(data.get("suspicious")), "flagged": data.get("flagged") or [],
        "writer": data.get("writer"), "lane": data.get("lane"), "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"), "reviewed_at": data.get("reviewed_at"),
        "expires_at": data.get("expires_at"), "title": data.get("title"), "body": data.get("body"),
        "attrs": data.get("attrs"), "warning": data.get("warning"),
        "withheld": data.get("withheld"),
        "forgotten": None if not forgotten else {"at": forgotten.get("at"), "by": forgotten.get("by"),
                                                  "reason": forgotten.get("reason")},
        "confirmed_by": prov.get("confirmed_by"),
        "evidence": [{"at": ev.get("at"), "tool": ev.get("tool"), "summary": ev.get("summary")}
                     for ev in prov.get("evidence") or []],
        "history": [{"version": v["version"], "at": v["at"], "trust": v["trust"], "title": v.get("title"),
                     "forgotten": bool(v.get("forgotten"))} for v in data.get("history") or []],
    }


def routes(api: Api) -> list[BaseRoute]:
    async def workspaces(request: Request) -> dict[str, Any]:
        out = await read(request, "/api/memory/workspaces")
        return {"available": out["available"], "workspaces": [_workspace_of(row) for row in out["workspaces"]]}

    async def entries(request: Request) -> dict[str, Any]:
        q = request.query_params
        query: dict[str, Any] = {"workspace": q.get("workspace"), "q": q.get("q") or None, "type": q.get("type") or None,
                                 "trust": q.get("trust") or None, "unreviewed": q.get("unreviewed") or None,
                                 "limit": q.get("limit") or None}
        out = await read(request, "/api/memory", query=query)
        ws = out["workspace"]
        # Each row names its own workspace, which matters when every workspace is listed (ws is then null).
        return {"available": out["available"], "workspace": ws,
                "entries": [_entry_of(row, row.get("workspace") or ws) for row in out["entries"]]}

    async def export(request: Request) -> Response:
        q = request.query_params
        reply = await call(request, "GET", "/api/memory/export",
                           query={"workspace": q.get("workspace"), "format": q.get("format")})
        if not 200 <= reply.status < 300:
            raise Refused(reply)
        keep = ("content-disposition", "x-content-type-options", "cache-control")
        headers = {name: reply.header(name) for name in keep if reply.header(name)}
        return Response(reply.body, status_code=reply.status, media_type=reply.header("content-type") or "application/octet-stream",
                        headers=headers)

    async def entry(request: Request) -> dict[str, Any]:
        q = request.query_params
        data = await read(request, f"/api/memory/{request.path_params['id']}",
                          query={"workspace": q.get("workspace"), "version": q.get("version")})
        return _detail_of(data)

    async def confirm(request: Request) -> dict[str, Any]:
        body = await _body(request)
        out = await write(request, "POST", f"/api/memory/{request.path_params['id']}/confirm", body)
        return {"id": out["id"], "workspace": out["workspace"], "version": out["version"], "trust": out["trust"]}

    async def review(request: Request) -> dict[str, Any]:
        body = await _body(request)
        out = await write(request, "POST", f"/api/memory/{request.path_params['id']}/review", body)
        return {"id": out["id"], "reviewed_at": out["reviewed_at"]}

    async def forget(request: Request) -> dict[str, Any]:
        body = await _body(request)
        out = await write(request, "POST", f"/api/memory/{request.path_params['id']}/forget", body)
        return {"id": out["id"], "state": out["state"], "already": bool(out.get("already")), "at": out.get("at")}

    return [
        org_route(api, "/memory/workspaces", workspaces, ["GET"], owner=True),
        org_route(api, "/memory", entries, ["GET"], owner=True),
        org_route(api, "/memory/export", export, ["GET"], owner=True),
        org_route(api, "/memory/{id}", entry, ["GET"], owner=True),
        org_route(api, "/memory/{id}/confirm", confirm, ["POST"], owner=True),
        org_route(api, "/memory/{id}/review", review, ["POST"], owner=True),
        org_route(api, "/memory/{id}/forget", forget, ["POST"], owner=True),
    ]
