"""The files of an account, as the app reads them on the account page (web/src/api/types.ts, `AccountFiles`).

The node keeps the files (routes_files.py, which uses the tools' own store and limits). Here an upload goes
through as the bytes it is, never parsed, and a download is streamed as it is read, so a large file is never
held whole. The app gets each file as a row (its name, size, where it is and whether it is complete), and the
limits as numbers and as words. A refusal, such as a size over the limit, goes back as the node sent it.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import BaseRoute

from agentos.api.app import Api
from agentos.api.web.base import open_stream, org_route, read, write, write_raw
from agentos.core.errors import InvalidRequest

DOWNLOAD_HEADERS = ("content-type", "content-disposition", "content-length", "x-content-type-options", "cache-control")
FILE_KEYS = ("id", "box", "name", "status", "size", "size_text", "kind", "mime", "source", "created_at",
             "finished_at", "error", "sha256")
# Reading needs `see` in the account's workspace, and changing needs `operate` (the node checks the workspace).
# The role check runs before the body is read, so a viewer's upload is refused without taking any of it in.
SEE, OPERATE = "see", "operate"


def _file_of(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in FILE_KEYS}


def _account(data: dict[str, Any]) -> dict[str, Any]:
    limits = data["limits"]
    return {
        "lane": data["lane"],
        "inbox": [_file_of(row) for row in data["inbox"]],
        "outbox": [_file_of(row) for row in data["outbox"]],
        "limits": {"put_bytes": limits["put_bytes"], "put": limits["put"],
                   "download_bytes": limits["download_bytes"], "download": limits["download"]},
    }


def routes(api: Api) -> list[BaseRoute]:
    def node_path(request: Request, tail: str = "") -> str:
        return f"/api/lanes/{request.path_params['ws']}/{request.path_params['name']}/files{tail}"

    async def listing(request: Request) -> dict[str, Any]:
        return _account(await read(request, node_path(request)))

    async def upload(request: Request) -> dict[str, Any]:
        name = request.query_params.get("name") or ""
        # The node's own limit (files_max_mb), read from its listing, so the body is never taken in past it.
        limits = (await read(request, node_path(request)))["limits"]
        cap, human = limits["put_bytes"], limits["put"]
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > cap:
            raise InvalidRequest(f"the file is larger than the {human} limit")
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > cap:
                raise InvalidRequest(f"the file is larger than the {human} limit")
            chunks.append(chunk)
        content_type = request.headers.get("content-type") or "application/octet-stream"
        out = await write_raw(request, "POST", node_path(request), b"".join(chunks), content_type=content_type,
                              query={"name": name})
        return {"lane": out["lane"], "file": _file_of(out["file"])}

    async def download(request: Request) -> StreamingResponse:
        opened = await open_stream(request, node_path(request, f"/{request.path_params['id']}/download"))
        headers: dict[str, str] = {}
        for key, value in opened.headers:
            name = key.decode("latin-1").lower()
            if name in DOWNLOAD_HEADERS:
                headers[name] = value.decode("latin-1")
        return StreamingResponse(opened.chunks, status_code=opened.status, headers=headers)

    async def remove(request: Request) -> dict[str, Any]:
        out = await write(request, "DELETE", node_path(request, f"/{request.path_params['id']}"))
        return {"lane": out["lane"], "deleted": _file_of(out["deleted"])}

    return [
        org_route(api, "/lanes/{ws}/{name}/files", listing, ["GET"], cap=SEE, action="files.list"),
        org_route(api, "/lanes/{ws}/{name}/files", upload, ["POST"], cap=OPERATE, action="files.put"),
        org_route(api, "/lanes/{ws}/{name}/files/{id}/download", download, ["GET"], cap=SEE, action="files.download"),
        org_route(api, "/lanes/{ws}/{name}/files/{id}", remove, ["DELETE"], cap=OPERATE, action="files.delete"),
    ]

