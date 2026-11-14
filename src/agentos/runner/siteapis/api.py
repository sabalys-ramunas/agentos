"""Owner routes for site APIs (docs/SITE-APIS.md, "Owner API and CLI"), served through
agentos.api.mounts behind the panel's auth, as the owner or an admin.

    GET    /api/site-apis                 every API and draft
    POST   /api/site-apis                 save a draft: a definition, or {api, url, candidates, surfaces},
                                          or {api, endpoint, recording, candidate, surfaces}
    POST   /api/site-apis/discover        {on, url}: candidates from a page
    GET    /api/site-apis/{id}            one, with its definition
    PATCH  /api/site-apis/{id}            change fields of its definition; what is live does not move
    DELETE /api/site-apis/{id}            refused while a schedule or workflow uses it
    POST   /api/site-apis/{id}/activate   {vouch}: the owner makes this content live
    POST   /api/site-apis/{id}/pause
    POST   /api/site-apis/{id}/try        {endpoint, params, on}
    GET    /api/site-apis/discover?on=    candidates from the page on that lane; null without one
    POST   /api/site-apis/{id}/read       start a read of each endpoint that needs no input
    POST   /api/site-apis/{id}/resume     wake a paused API with what was live (owner)
    POST   /api/site-apis/{id}/decline    say no to a proposal (person)
    POST   /api/site-apis/{id}/accept-empty   an empty page is right: minimums to 0, then activate (owner)
    POST   /api/site-apis/{id}/repair     refused: a changed field is saved again, not repaired from here
    POST   /api/site-apis/{id}/keep-updated   refused: keeping one up to date is a watch in Routines

Who is a person is decided here, as for workflows: the owner or a member signed in to the panel. A
bearer token is not one, even the owner's, so it saves and tries as an agent would, and activate
answers 403 (SiteApiService).
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agentos.core.errors import AgentosError, InvalidRequest
from agentos.runner.api import _body, _error
from agentos.runner.runner import OWNER_GRANT
from agentos.runner.siteapis.discover import definition_from
from agentos.runner.siteapis.service import SERVICE_NAME


def _by(request: Request) -> dict[str, Any]:
    auth = request.state.auth
    access, p = auth.access, auth.principal
    if access.human and access.owner:
        return {"kind": "human", "id": "owner", "owner": "owner"}
    return {"kind": "human" if access.human else "agent", "id": p.id, "owner": p.owner}


def _grant(request: Request) -> str | None:
    auth = request.state.auth
    return auth.principal.grant_id or (OWNER_GRANT if auth.access.owner else auth.access.grant_id)


def routes(runtime: Any) -> list[Route]:
    def svc() -> Any:
        return runtime.require(SERVICE_NAME)

    def handle(fn):
        async def endpoint(request: Request) -> Response:
            try:
                return await fn(request)
            except AgentosError as e:
                return _error(e)
        endpoint.__name__ = fn.__name__
        return endpoint

    @handle
    async def list_apis(request: Request) -> Response:
        return JSONResponse({"apis": svc().apis()})

    @handle
    async def create(request: Request) -> Response:
        body = await _body(request)
        by = _by(request)
        if "recording" in body:
            row = svc().from_recording(body["recording"], api=str(body.get("api")), endpoint=str(body.get("endpoint")), by=by,
                                       surfaces=body.get("surfaces"), candidate=body.get("candidate"),
                                       vouch=bool(body.get("vouch")))
        elif "candidates" in body:
            row = svc().save(definition_from(str(body.get("api")), str(body.get("url")), body["candidates"],
                                             surfaces=body.get("surfaces"), title=body.get("title")), by=by, bump=True)
        else:
            row = svc().save(body.get("definition", body), by=by, bump=True)
        return JSONResponse(row, status_code=201)

    @handle
    async def discover(request: Request) -> Response:
        body = await _body(request)
        if not body.get("on"):
            raise InvalidRequest("on names the lane or surface to read")
        return JSONResponse(await svc().discover(body["on"], body.get("url")))

    @handle
    async def get(request: Request) -> Response:
        return JSONResponse(svc().get(request.path_params["id"]))

    @handle
    async def patch(request: Request) -> Response:
        have = svc().get(request.path_params["id"])["definition"]
        body = await _body(request)
        return JSONResponse(svc().save({**have, **body.get("definition", body)}, by=_by(request), bump=True))

    @handle
    async def delete(request: Request) -> Response:
        return JSONResponse(svc().remove(request.path_params["id"], by=_by(request)))

    @handle
    async def activate(request: Request) -> Response:
        body = await _body(request)
        return JSONResponse(svc().activate(request.path_params["id"], by=_by(request), vouch=body.get("vouch") is True))

    @handle
    async def pause(request: Request) -> Response:
        return JSONResponse(svc().pause(request.path_params["id"], by=_by(request)))

    @handle
    async def try_it(request: Request) -> Response:
        body = await _body(request)
        if not body.get("endpoint"):
            raise InvalidRequest("endpoint names the endpoint to try")
        return JSONResponse(await svc().try_endpoint(request.path_params["id"], body["endpoint"], body.get("params"), by=_by(request),
                                                     on=body.get("on"), grant_id=_grant(request)))

    @handle
    async def discover_view(request: Request) -> Response:
        on = request.query_params.get("on")
        if not on:
            return JSONResponse(None)
        return JSONResponse(await svc().discover(on, None))

    @handle
    async def read_now(request: Request) -> Response:
        return JSONResponse(await svc().read_now(request.path_params["id"], by=_by(request), grant_id=_grant(request)))

    @handle
    async def resume(request: Request) -> Response:
        return JSONResponse(svc().resume(request.path_params["id"], by=_by(request)))

    @handle
    async def decline(request: Request) -> Response:
        return JSONResponse(svc().decline(request.path_params["id"], by=_by(request)))

    @handle
    async def accept_empty(request: Request) -> Response:
        return JSONResponse(svc().accept_empty(request.path_params["id"], by=_by(request)))

    @handle
    async def repair(request: Request) -> Response:
        raise InvalidRequest("A changed field is not repaired from here yet. Read the page again with discover, and save "
                             "the endpoint with the new field; a person then activates it.")

    @handle
    async def keep_updated(request: Request) -> Response:
        raise InvalidRequest("This node does not keep a Site API up to date. A routine can watch one for changes: "
                           "see Routines, When it changes.")

    return [
        Route("/api/site-apis", list_apis, methods=["GET"]),
        Route("/api/site-apis", create, methods=["POST"]),
        Route("/api/site-apis/discover", discover, methods=["POST"]),
        Route("/api/site-apis/discover", discover_view, methods=["GET"]),
        Route("/api/site-apis/{id}", get, methods=["GET"]),
        Route("/api/site-apis/{id}", patch, methods=["PATCH"]),
        Route("/api/site-apis/{id}", delete, methods=["DELETE"]),
        Route("/api/site-apis/{id}/activate", activate, methods=["POST"]),
        Route("/api/site-apis/{id}/pause", pause, methods=["POST"]),
        Route("/api/site-apis/{id}/try", try_it, methods=["POST"]),
        Route("/api/site-apis/{id}/read", read_now, methods=["POST"]),
        Route("/api/site-apis/{id}/resume", resume, methods=["POST"]),
        Route("/api/site-apis/{id}/decline", decline, methods=["POST"]),
        Route("/api/site-apis/{id}/accept-empty", accept_empty, methods=["POST"]),
        Route("/api/site-apis/{id}/repair", repair, methods=["POST"]),
        Route("/api/site-apis/{id}/keep-updated", keep_updated, methods=["POST"]),
    ]
