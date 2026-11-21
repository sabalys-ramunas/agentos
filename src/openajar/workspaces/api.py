"""Panel API routes for workspaces, templates, the login queue, readiness, and export and import of a
workspace (docs/API.md).

`routes(runtime)` assumes the panel's auth middleware already ran (bearer or session + CSRF). It reads
the caller from `request.state.principal` when set, and otherwise acts as the owner. The owner's click on
"apply" is the approval here: the panel first calls `POST /api/templates/plan` and shows that plan.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from openajar.core.errors import InvalidRequest, OpenajarError, PolicyDenied
from openajar.core.http_errors import error_body, status_for
from openajar.core.models import Principal
from openajar.workspaces.service import SERVICE_NAME

# This route's own answer where it differs from the shared map: a refused export or import is a conflict here
# (the shared map has no entry for export_refused, and the other surfaces answer it 500).
OWN_STATUS = {"export_refused": 409}
PASSPHRASE_HEADER = "x-agentos-passphrase"   # the export's passphrase, for an import upload
IDENTITY_HEADER = "x-agentos-identity"       # or an age identity (AGE-SECRET-KEY-1...)
MAX_UPLOAD = 16 * 1024**3
CHUNK = 1024 * 1024


def _error(e: OpenajarError) -> JSONResponse:
    return JSONResponse(error_body(e.code, e.message), status_code=OWN_STATUS.get(e.code, status_for(e)))


def _principal(request: Request) -> Principal:
    p = getattr(request.state, "principal", None)
    return p if isinstance(p, Principal) else Principal(kind="human", id="owner")


def _needs_a_person(request: Request, action: str) -> None:
    """Refuse below a person (the device credential, an admin grant, the node token): grants an import
    reissues come from the file, with no bound of the caller's, so only the owner in person reissues them."""
    from openajar.core.presence import Presence

    access = getattr(getattr(request.state, "auth", None), "access", None)
    need = getattr(access, "require_presence", None)
    if not callable(need):  # nothing says who is asking: not a person
        raise PolicyDenied("only the owner in person can do this")
    need(Presence.PERSON, action)


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise InvalidRequest("the request body must be JSON") from e
    if not isinstance(data, dict):
        raise InvalidRequest("the request body must be a JSON object")
    return data


def _flag(body: dict[str, Any], key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise InvalidRequest(f"{key} must be true or false")
    return value


def _limit(value: str | None, default: int = 50) -> int:
    try:
        return max(1, min(500, int(value))) if value else default
    except ValueError as e:
        raise InvalidRequest(f"not a number: {value!r}") from e


def _plan_args(body: dict[str, Any]) -> dict[str, Any]:
    return {"proxy_per_workspace": body.get("proxy_per_workspace"),
            "proxy_per_lane": body.get("proxy_per_lane"),
            "override_proxy_rules": _flag(body, "override_proxy_rules"),
            "reason": body.get("reason")}


def _handled(fn):
    """A route handler that answers an OpenajarError with this module's error body."""
    async def endpoint(request: Request) -> Response:
        try:
            return await fn(request)
        except OpenajarError as e:
            return _error(e)

    endpoint.__name__ = fn.__name__
    return endpoint


class Handlers:
    """The handlers of the routes below, sharing the node runtime."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def svc(self):
        return self.runtime.require(SERVICE_NAME)

    # ---- workspaces ----
    async def list_workspaces(self, request: Request) -> Response:
        return JSONResponse([ws.model_dump(mode="json") for ws in self.svc().list_workspaces()])

    async def create_workspace(self, request: Request) -> Response:
        body = await _body(request)
        view = self.svc().create_workspace(body.get("id"), name=body.get("name"), color=body.get("color"),
                                      default_proxy=body.get("default_proxy"), notes=body.get("notes"))
        return JSONResponse(view.model_dump(mode="json"), status_code=201)

    async def get_workspace(self, request: Request) -> Response:
        return JSONResponse(self.svc().get_workspace(request.path_params["id"]).model_dump(mode="json"))

    async def update_workspace(self, request: Request) -> Response:
        body = await _body(request)
        view = self.svc().update_workspace(request.path_params["id"], body)
        return JSONResponse(view.model_dump(mode="json"))

    async def delete_workspace(self, request: Request) -> Response:
        self.svc().delete_workspace(request.path_params["id"])
        return JSONResponse({"deleted": request.path_params["id"]})

    # ---- the login queue ----
    async def workspace_queue(self, request: Request) -> Response:
        return JSONResponse(self.svc().queue(request.path_params["id"]).model_dump(mode="json"))

    async def queue(self, request: Request) -> Response:
        return JSONResponse(self.svc().queue().model_dump(mode="json"))

    async def set_readiness(self, request: Request) -> Response:
        body = await _body(request)
        lane = f"{request.path_params['workspace']}/{request.path_params['name']}"
        view = self.svc().set_readiness(lane, body.get("state"), note=body.get("note"), by=_principal(request).id)
        return JSONResponse(view.model_dump(mode="json"))

    # ---- templates ----
    async def list_templates(self, request: Request) -> Response:
        templates, problems = self.svc().templates()
        return JSONResponse({"templates": [t.model_dump(mode="json") for t in templates],
                             "problems": [p.model_dump(mode="json") for p in problems]})

    async def plan_template(self, request: Request) -> Response:
        body = await _body(request)
        plan = self.svc().plan(body.get("template"), body.get("workspaces"), **_plan_args(body))
        return JSONResponse(plan.public())

    async def apply_template(self, request: Request) -> Response:
        body = await _body(request)
        result = await self.svc().apply(body.get("template"), body.get("workspaces"), by=_principal(request).id,
                                   **_plan_args(body))
        return JSONResponse(result.model_dump(mode="json"))

    async def list_overrides(self, request: Request) -> Response:
        records = self.svc().overrides(_limit(request.query_params.get("limit")))
        return JSONResponse([r.model_dump(mode="json") for r in records])

    # ---- export and import (openajar.workspaces.transfer.export, openajar.workspaces.transfer.import_); owner only (openajar.api.mounts) ----
    async def export_workspace(self, request: Request) -> Response:
        """Stream one workspace as an encrypted file. Body: {"passphrase"} or {"recipients": ["age1..."]},
        and "with_proxies"."""
        from openajar.workspaces.transfer.export import export_name
        from openajar.workspaces.transfer.export import export_workspace as run_export
        from openajar.workspaces.transfer.read import staging_dir
        from openajar.workspaces.transfer.sources import runtime_sources

        # an export holds the accounts' signed-in sessions (and, with_proxies, their proxy logins):
        # only the owner in person takes them off the node, never a token an agent may hold
        _needs_a_person(request, "workspace.export")
        body = await _body(request)
        passphrase, recipients = body.get("passphrase"), body.get("recipients")
        if passphrase is not None and not isinstance(passphrase, str):
            raise InvalidRequest("passphrase must be text")
        if recipients is not None and (not isinstance(recipients, list) or
                                       not all(isinstance(r, str) for r in recipients)):
            raise InvalidRequest("recipients must be a list of age public keys (age1...)")
        workspace = request.path_params["id"]
        src = runtime_sources(self.runtime)
        folder = staging_dir(src.home, "export")
        folder.mkdir(mode=0o700)
        dest = folder / export_name(workspace, datetime.now(UTC))
        try:
            result = await run_export(src, workspace, dest, passphrase=passphrase or None, recipients=recipients or None,
                                      with_proxies=_flag(body, "with_proxies"))
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        summary = json.dumps({"workspace": result.workspace, "counts": result.counts, "size": result.size,
                              "lanes": [{"id": lane["id"], "copied": lane["copied"]} for lane in result.lanes]})
        headers = {"content-disposition": f'attachment; filename="{dest.name}"', "content-length": str(result.size),
                   "content-encoding": "identity",  # already encrypted: the gzip middleware would only waste time
                   "x-agentos-export": summary}
        return StreamingResponse(_chunks(dest), media_type="application/octet-stream", headers=headers,
                                 background=BackgroundTask(shutil.rmtree, folder, ignore_errors=True))

    async def import_workspace(self, request: Request) -> Response:
        """Import an uploaded export (the raw file as the body). The secret travels in a header
        (X-Agentos-Passphrase or X-Agentos-Identity), the options in the query: as, dry_run,
        reissue_grants, allow_missing_proxy, map_proxy=OLD=NEW (repeatable), on_vault_conflict,
        enable_schedules. 200 with the plan (dry run or blocked: applied false), 201 when applied."""
        from openajar.core import age
        from openajar.workspaces.transfer.import_ import ImportOptions, apply_import, plan_import
        from openajar.workspaces.transfer.read import read_export, staging_dir
        from openajar.workspaces.transfer.sources import runtime_sources

        q = request.query_params
        if _qflag(q, "reissue_grants"):
            _needs_a_person(request, "workspace.import.grants")
        if _qflag(q, "enable_schedules"):  # they would run under the grants in the file, the owner's included
            _needs_a_person(request, "workspace.import.schedules")
        passphrase, identity = request.headers.get(PASSPHRASE_HEADER), request.headers.get(IDENTITY_HEADER)
        if not passphrase and not identity:
            raise InvalidRequest("send the export's passphrase in the X-Agentos-Passphrase header, or an age identity "
                                 "in X-Agentos-Identity")
        identities = None
        if identity:
            try:
                identities = age.read_identities(identity)
            except age.BadKey as e:
                raise InvalidRequest(str(e)) from None
        proxy_map = {}
        for item in q.getlist("map_proxy"):
            old, sep, new = item.partition("=")
            if not sep or not old or not new:
                raise InvalidRequest(f"map_proxy takes OLD=NEW, for example lt1=de1; got {item[:60]!r}")
            proxy_map[old] = new
        options = ImportOptions(rename_to=q.get("as") or None, reissue_grants=_qflag(q, "reissue_grants"),
                                allow_missing_proxy=_qflag(q, "allow_missing_proxy"), proxy_map=proxy_map,
                                vault_conflict=q.get("on_vault_conflict") or "error",
                                enable_schedules=_qflag(q, "enable_schedules"), by=_principal(request).id)
        src = runtime_sources(self.runtime)
        upload = staging_dir(src.home, "upload")
        try:
            await _receive(request, upload)
            contents = await asyncio.to_thread(read_export, upload, staging_dir(src.home, "import"),
                                               passphrase=None if identities else passphrase, identities=identities)
        finally:
            upload.unlink(missing_ok=True)
        try:
            plan = await plan_import(contents, src, options)
            if _qflag(q, "dry_run") or not plan.ok:
                return JSONResponse({"applied": False, "plan": plan.to_dict()}, status_code=200 if plan.ok else 409)
            # an import brings logged-in browsers (their cookies) and puts them behind a proxy here: a
            # second browser for an account, or one behind another account's exit. A person's call
            _needs_a_person(request, "workspace.import")
            result = await apply_import(plan, contents, src)
        finally:
            contents.discard()
        return JSONResponse({"applied": True, "plan": plan.to_dict(), "result": result.to_dict()}, status_code=201)


def routes(runtime: Any) -> list[Route]:
    h = Handlers(runtime)

    return [
        Route("/api/workspaces", _handled(h.list_workspaces), methods=["GET"]),
        Route("/api/workspaces", _handled(h.create_workspace), methods=["POST"]),
        Route("/api/workspaces/{id}", _handled(h.get_workspace), methods=["GET"]),
        Route("/api/workspaces/{id}", _handled(h.update_workspace), methods=["PATCH"]),
        Route("/api/workspaces/{id}", _handled(h.delete_workspace), methods=["DELETE"]),
        Route("/api/workspaces/{id}/queue", _handled(h.workspace_queue), methods=["GET"]),
        Route("/api/queue", _handled(h.queue), methods=["GET"]),
        Route("/api/lanes/{workspace}/{name}/readiness", _handled(h.set_readiness), methods=["POST"]),
        Route("/api/templates", _handled(h.list_templates), methods=["GET"]),
        Route("/api/templates/plan", _handled(h.plan_template), methods=["POST"]),
        Route("/api/templates/apply", _handled(h.apply_template), methods=["POST"]),
        Route("/api/overrides", _handled(h.list_overrides), methods=["GET"]),
        Route("/api/workspaces/{id}/export", _handled(h.export_workspace), methods=["POST"]),
        Route("/api/workspaces/import", _handled(h.import_workspace), methods=["POST"]),
    ]


def _qflag(query: Any, key: str) -> bool:
    value = (query.get(key) or "").lower()
    if value in ("", "0", "false", "no"):
        return False
    if value in ("1", "true", "yes"):
        return True
    raise InvalidRequest(f"{key} must be true or false")


async def _chunks(path: Path):
    with path.open("rb") as f:
        while chunk := await asyncio.to_thread(f.read, CHUNK):
            yield chunk


async def _receive(request: Request, target: Path) -> None:
    """Write the request body (an encrypted export) to `target`, refusing more than MAX_UPLOAD or
    more than the disk can take (the import needs about twice the file's size again)."""
    declared = request.headers.get("content-length")
    room = shutil.disk_usage(target.parent).free
    if declared and declared.isdigit() and int(declared) * 3 > room:
        raise InvalidRequest(f"not enough free space on the node for this import ({int(declared) // 1024**2} MiB "
                             f"uploaded needs about three times that; {room // 1024**2} MiB free)")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    size = 0
    with os.fdopen(fd, "wb") as out:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise InvalidRequest(f"the upload is larger than {MAX_UPLOAD // 1024**3} GiB")
            out.write(chunk)
    if size == 0:
        raise InvalidRequest("send the export file as the request body")
