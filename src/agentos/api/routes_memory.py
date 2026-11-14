"""Workspace memory in the panel: what agents remember about this person's work (agentos.memory).

  GET    /api/memory/workspaces                          every workspace with memory: its counts against the
                                                         caps, how many entries wait for a look, and whether
                                                         its record checks out
  GET    /api/memory?workspace=&q=&type=&trust=&unreviewed=1&limit=
                                                         entries, newest first, or the search hits for `q`;
                                                         every workspace when none is named
  GET    /api/memory/export?workspace=&format=jsonl|md   the memory in plaintext, as one file (every workspace
                                                         when none is named)
  GET    /api/memory/{id}?workspace=&version=            one entry, its text and its history
  POST   /api/memory/{id}/confirm                        {version, title?, body?}: the owner vouches for an
                                                         agent's entry (owner trust)
  POST   /api/memory/{id}/review                         mark an entry looked at, without promoting it
  POST   /api/memory/{id}/forget                         {reason}: erase its content in every version, for good

Only the owner in person: the signed-in panel, on a session cookie. A bearer token is refused, the owner
token included, because agents hold that token. So is a member and a grant. The memory service refuses the
same, so this is one more fence in front of it, and it says so in Activity.

Memory text was written by agents after reading pages, so it leaves here as text and nothing renders it.
Without the memory service the list answers `available: false`, and the screens say so.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agentos.api.app import Api, access_of, endpoint, owner_in_person, principal_of, read_json_object
from agentos.api.errors import ApiError
from agentos.core.errors import InvalidRequest
from agentos.memory.models import Caller

SERVICE = "memory"
PERSON_ONLY = "Only the owner signed in to the panel can read or change memory. A token cannot, the owner token included."
MAX_LIST = 200


def _scope(svc: Any, request: Request) -> list[str]:
    """The workspace named by ?workspace=, or every workspace that has memory when none is named."""
    named = request.query_params.get("workspace") or ""
    return [named] if named else sorted(svc.stores.workspaces())


def _limit(raw: str | None) -> int:
    try:
        return max(1, min(int(raw or 50), MAX_LIST))
    except ValueError:
        raise InvalidRequest("limit is a number.") from None


def routes(api: Api) -> list[Route]:
    def person(request: Request, action: str) -> Caller:
        """The owner in person, or a refusal that is traced. The service's own checks come after."""
        if not owner_in_person(request):
            access_of(request).trace(action, status="denied", summary="memory is the owner's, in person")
            raise ApiError("denied", PERSON_ONLY)
        return Caller.owner(session=getattr(request.state.auth, "sid", None) or "panel",
                            principal=principal_of(request).id)

    async def workspaces(request: Request) -> dict[str, Any]:
        who = person(request, "memory.workspaces")
        if api.optional(SERVICE) is None:
            return {"available": False, "workspaces": []}
        svc = api.svc(SERVICE)

        def build() -> list[dict[str, Any]]:
            out = []
            for name in sorted(svc.stores.workspaces()):
                stats = svc.stats(name)
                waiting = svc.list_entries(who, name, unreviewed=True, limit=1000).data["entries"]
                check = svc.verify(name)
                out.append({**stats, "waiting": len(waiting), "chain_ok": check["ok"],
                            "forgotten": check["forgotten"]})
            return out

        return {"available": True, "workspaces": await asyncio.to_thread(build)}

    async def entries(request: Request) -> dict[str, Any]:
        who = person(request, "memory.list")
        if api.optional(SERVICE) is None:
            return {"available": False, "workspace": None, "entries": []}
        svc = api.svc(SERVICE)
        q = request.query_params
        types = [q["type"]] if q.get("type") else None
        trust = q.get("trust") or None
        limit = _limit(q.get("limit"))
        text = (q.get("q") or "").strip()
        unreviewed = q.get("unreviewed") == "1"

        def build() -> list[dict[str, Any]]:
            found: list[dict[str, Any]] = []
            for workspace in _scope(svc, request):
                if text:
                    rows = svc.search(who, workspace, text, types=types, trust_min=trust, limit=limit).data["hits"]
                else:
                    rows = svc.list_entries(who, workspace, types=types, trust_min=trust, unreviewed=unreviewed,
                                            limit=limit).data["entries"]
                found += [{**row, "workspace": workspace} for row in rows]
            found.sort(key=lambda row: row.get("at") or "", reverse=True)  # newest first, across the workspaces
            return found[:limit]

        named = q.get("workspace") or None
        return {"available": True, "workspace": named, "entries": await asyncio.to_thread(build)}

    async def export(request: Request) -> Response:
        person(request, "memory.export")
        svc = api.svc(SERVICE)
        fmt = request.query_params.get("format") or "jsonl"
        if fmt not in ("jsonl", "md"):
            raise InvalidRequest("format is jsonl or md.")
        named = request.query_params.get("workspace") or ""

        def build() -> str:
            if fmt == "jsonl":
                records = [record for name in _scope(svc, request) for record in svc.export_records(name)]
                return "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in records)
            from agentos.memory.cli import (
                markdown_export,  # the same Markdown `agentos memory export --md` writes
            )

            files = [markdown_export(svc, name) for name in _scope(svc, request)]
            return "\n\n".join(text for one in files for _, text in sorted(one.items()))

        text = await asyncio.to_thread(build)
        access_of(request).trace("memory.export", summary=f"Export the memory of {named or 'every workspace'} ({fmt})")
        scope = named or "all"
        name = f"{scope}-memory.{'jsonl' if fmt == 'jsonl' else 'md'}"
        media = "application/x-ndjson" if fmt == "jsonl" else "text/markdown"
        return Response(text, media_type=media, headers={
            "content-disposition": f'attachment; filename="{name}"', "x-content-type-options": "nosniff",
            "cache-control": "no-store"})

    async def entry(request: Request) -> dict[str, Any]:
        who = person(request, "memory.get")
        svc = api.svc(SERVICE)
        q = request.query_params
        version = q.get("version")
        if version is not None and not version.isdigit():
            raise InvalidRequest("version is a number.")
        workspace = q.get("workspace") or None
        entry_id = request.path_params["id"]
        return await asyncio.to_thread(
            lambda: svc.get(who, entry_id, version=int(version) if version else None, history=True,
                            workspace=workspace).data)

    async def confirm(request: Request) -> dict[str, Any]:
        who = person(request, "memory.confirm")
        svc = api.svc(SERVICE)
        body = await read_json_object(request)
        version = body.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise InvalidRequest("version is the entry's current version number.")
        edit = {key: body[key] for key in ("title", "body") if key in body}
        for key, value in edit.items():
            if value is not None and not isinstance(value, str):
                raise InvalidRequest(f"{key} is text.")
        return await asyncio.to_thread(
            lambda: svc.confirm(who, request.path_params["id"], version, edit=edit or None,
                                workspace=body.get("workspace") or None))

    async def review(request: Request) -> dict[str, Any]:
        who = person(request, "memory.review")
        svc = api.svc(SERVICE)
        body = await read_json_object(request)
        return await asyncio.to_thread(
            lambda: svc.review(who, request.path_params["id"], workspace=body.get("workspace") or None))

    async def forget(request: Request) -> dict[str, Any]:
        who = person(request, "memory.forget")
        svc = api.svc(SERVICE)
        body = await read_json_object(request)
        reason = body.get("reason")
        if not isinstance(reason, str):
            raise InvalidRequest("say why the entry is forgotten (the reason is kept, the content is not).")
        return await asyncio.to_thread(
            lambda: svc.forget(who, request.path_params["id"], reason, workspace=body.get("workspace") or None))

    return [
        Route("/api/memory/workspaces", endpoint(api, workspaces, owner=True), methods=["GET"]),
        Route("/api/memory", endpoint(api, entries, owner=True), methods=["GET"]),
        Route("/api/memory/export", endpoint(api, export, owner=True), methods=["GET"]),
        Route("/api/memory/{id}", endpoint(api, entry, owner=True), methods=["GET"]),
        Route("/api/memory/{id}/confirm", endpoint(api, confirm, owner=True), methods=["POST"]),
        Route("/api/memory/{id}/review", endpoint(api, review, owner=True), methods=["POST"]),
        Route("/api/memory/{id}/forget", endpoint(api, forget, owner=True), methods=["POST"]),
    ]
