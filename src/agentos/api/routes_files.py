"""The files of an account in the panel: its inbox and outbox (agentos.node.files, the tools' own store).

  GET    /api/lanes/{ws}/{name}/files                  the inbox and the outbox, and the size limits
  GET    /api/lanes/{ws}/{name}/files/{id}/download    one complete file, as a download
  POST   /api/lanes/{ws}/{name}/files?name=            store the body (the bytes, as they are) in the inbox
  DELETE /api/lanes/{ws}/{name}/files/{id}             delete a file from the inbox or the outbox

Reads need `see` in the account's workspace, and writes need `operate` there (as the lane's own controls do).
A grant is refused, and so is a bearer token: an account's files are a person's, read and changed in the panel.
Any account a member's role does not reach is refused as well. Every path into the store goes
through `LaneFiles`, which refuses symlinks, hard links and anything outside its own box, and every size
limit is the one the tools use (`files_max_mb`, `downloads_max_mb`). An uploaded file is only stored, never
opened: the bytes are checked by their type and written, and a download is sent as an attachment with
`nosniff`, so the browser never renders one.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

from agentos.api.app import Api, access_of, at_the_panel, endpoint
from agentos.api.errors import ApiError
from agentos.api.routes_core import scoped_lane_id
from agentos.core.errors import InvalidRequest
from agentos.core.models import Effect, LaneKind

DOWNLOAD_HEADERS = {"x-content-type-options": "nosniff", "cache-control": "no-store"}
PERSON_ONLY = "Only a person signed in to the panel can read or change an account's files. A token cannot, the owner token included."


def routes(api: Api) -> list[Route]:
    from agentos.node.files import human_size
    from agentos.node.io_tools import lane_files

    def account(request: Request, action: str) -> tuple[str, str]:
        """The account in the path (refused before any lookup when a grant does not reach it), and its
        workspace, after the caller's role is checked for that workspace."""
        if not at_the_panel(request):
            access_of(request).trace(action, status="denied", summary="an account's files are a person's")
            raise ApiError("denied", PERSON_ONLY)
        lane_id = scoped_lane_id(request, action)
        ws = lane_id.split("/", 1)[0]
        return lane_id, ws

    def browser_account(lane_id: str) -> None:
        info = api.runtime.require("lanes").get(lane_id)  # LaneNotFound when there is no such account
        if info.spec.kind is not LaneKind.BROWSER:
            raise InvalidRequest(f"{lane_id} is a {info.spec.kind.value} account; files are for browser accounts.")

    def files_of(lane_id: str) -> Any:
        return lane_files(api.runtime, lane_id)

    def public(stored: Any) -> dict[str, Any]:
        return stored.public()

    async def listing(request: Request) -> dict[str, Any]:
        lane_id, ws = account(request, "files.list")
        access_of(request).require("see", ws, action="files.list", lane=lane_id)
        browser_account(lane_id)
        files = files_of(lane_id)
        listed = await asyncio.to_thread(files.list)
        return {
            "lane": lane_id,
            "inbox": [public(f) for f in listed if f.box == "inbox"],
            "outbox": [public(f) for f in listed if f.box != "inbox"],
            "limits": {"put_bytes": files.config.max_bytes, "put": human_size(files.config.max_bytes),
                       "download_bytes": files.config.download_max_bytes,
                       "download": human_size(files.config.download_max_bytes)},
        }

    async def download(request: Request) -> Response:
        lane_id, ws = account(request, "files.download")
        access_of(request).require("see", ws, action="files.download", lane=lane_id)
        browser_account(lane_id)
        files = files_of(lane_id)
        file_id = request.path_params["id"]
        meta = await asyncio.to_thread(files.get, file_id)
        path = await asyncio.to_thread(files.path, file_id)
        access_of(request).trace("files.download", lane=lane_id, file_id=file_id, summary=f"Download {meta.name}")
        return FileResponse(path, media_type="application/octet-stream", filename=meta.name, headers=DOWNLOAD_HEADERS)

    async def put(request: Request) -> dict[str, Any]:
        lane_id, ws = account(request, "files.put")
        access = access_of(request)
        access.require("operate", ws, action="files.put", lane=lane_id)
        browser_account(lane_id)
        name = request.query_params.get("name") or ""
        if not name:
            raise InvalidRequest("Say the file's name: ?name=.")
        files = files_of(lane_id)
        cap = files.config.max_bytes
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > cap:
            raise InvalidRequest(f"the file is larger than the {human_size(cap)} limit")
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():  # read up to the limit, and no further
            total += len(chunk)
            if total > cap:
                raise InvalidRequest(f"the file is larger than the {human_size(cap)} limit")
            chunks.append(chunk)
        data = b"".join(chunks)
        await access.authorize(Effect.DRAFT, action="files.put", lane_id=lane_id,
                               summary=f"Store a file named {name!r} in the inbox of {lane_id}",
                               content={"name": name, "sha256": hashlib.sha256(data).hexdigest()})
        stored = await asyncio.to_thread(files.put_bytes, name, data)  # sanitizes the name and sniffs the type
        access.trace("files.put", lane=lane_id, file_id=stored.id, mime=stored.mime, size=stored.size,
                     summary=f"Store {stored.name} in the inbox of {lane_id}")
        return {"lane": lane_id, "file": public(stored)}

    async def delete(request: Request) -> dict[str, Any]:
        lane_id, ws = account(request, "files.delete")
        access = access_of(request)
        access.require("operate", ws, action="files.delete", lane=lane_id)
        browser_account(lane_id)
        files = files_of(lane_id)
        file_id = request.path_params["id"]
        stored = await asyncio.to_thread(files.get, file_id)
        await access.authorize(Effect.DRAFT, action="files.delete", lane_id=lane_id,
                               summary=f"Delete {stored.name} ({file_id}) from the {stored.box} of {lane_id}")
        deleted = await files.delete(file_id)
        access.trace("files.delete", lane=lane_id, file_id=file_id, summary=f"Delete {stored.name}")
        return {"lane": lane_id, "deleted": public(deleted)}

    return [
        Route("/api/lanes/{ws}/{name}/files", endpoint(api, listing, cap="see", action="files.list"), methods=["GET"]),
        Route("/api/lanes/{ws}/{name}/files", endpoint(api, put, cap="operate", action="files.put"), methods=["POST"]),
        Route("/api/lanes/{ws}/{name}/files/{id}/download",
              endpoint(api, download, cap="see", action="files.download"), methods=["GET"]),
        Route("/api/lanes/{ws}/{name}/files/{id}",
              endpoint(api, delete, cap="operate", action="files.delete"), methods=["DELETE"]),
    ]
