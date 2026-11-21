"""Panel API routes for surfaces and connectors (docs/design/connectors.md section 5, `api`).

    GET    /api/surfaces                       every surface: who it acts as, through which lane
    POST   /api/surfaces                       add or replace one (the body is a Surface)
    GET    /api/surfaces/{workspace}/{name}
    DELETE /api/surfaces/{workspace}/{name}    refused while a schedule posts to it
    GET    /api/connectors                     installed operations, each with its surfaces and health
    POST   /api/connectors/{operation}/run     {on, params, idempotency_key}: queue a run on a surface

openajar.api.mounts serves them behind the panel's auth as owner routes (signed in, the owner
or an admin, CSRF on cookie writes); writing a surface needs the owner himself
(SurfaceService). A run queued here carries the caller's grant and is authorized by the
runner right before it runs, so a commit waits for its exact-content approval like a tool call.
`version` pins a catalog version; a site API runs only its active one (RunnerService._check_active).
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from openajar.core.errors import InvalidRequest, OpenajarError
from openajar.runner.api import _body, _error, _principal
from openajar.runner.names import CONNECTOR_PREFIX


def routes(runtime: Any) -> list[Route]:
    def runner():
        return runtime.require("runner")

    def handle(fn):
        async def endpoint(request: Request) -> Response:
            try:
                return await fn(request)
            except OpenajarError as e:
                return _error(e)
        endpoint.__name__ = fn.__name__
        return endpoint

    def surface_id(request: Request) -> str:
        return f"{request.path_params['workspace']}/{request.path_params['name']}"

    @handle
    async def list_surfaces(request: Request) -> Response:
        site = request.query_params.get("site") or None
        return JSONResponse([s.public() for s in runner().surfaces.list(site=site)])

    @handle
    async def put_surface(request: Request) -> Response:
        s, created = runner().surfaces.put(await _body(request), by=_principal(request))
        return JSONResponse(s.public(), status_code=201 if created else 200)

    @handle
    async def get_surface(request: Request) -> Response:
        return JSONResponse(runner().surfaces.get(surface_id(request)).public())

    @handle
    async def delete_surface(request: Request) -> Response:
        s = runner().surfaces.remove(surface_id(request), by=_principal(request))
        return JSONResponse({"deleted": s.id})

    @handle
    async def list_connectors(request: Request) -> Response:
        from openajar.runner.connectors.tools import _row

        r = runner()
        ops = [fv for fv in r.flows.list(CONNECTOR_PREFIX.rstrip(".")) if fv.definition.connector]
        return JSONResponse([_row(r, fv, lambda _id: True) for fv in sorted(ops, key=lambda f: f.id)])

    @handle
    async def run_operation(request: Request) -> Response:
        body = await _body(request)
        unknown = set(body) - {"on", "params", "idempotency_key", "version"}
        if unknown:
            raise InvalidRequest(f"unknown fields: {', '.join(sorted(unknown))}")
        op = request.path_params["operation"]
        run, created = runner().submit(
            flow_id=op if op.startswith(CONNECTOR_PREFIX) else CONNECTOR_PREFIX + op, version=body.get("version"),
            params=body.get("params") or {}, surface=body.get("on"), idempotency_key=body.get("idempotency_key"),
            principal=_principal(request), source="api", authorized=False)
        return JSONResponse({**runner().describe(run), "duplicate": not created}, status_code=202)

    return [
        Route("/api/surfaces", list_surfaces, methods=["GET"]),
        Route("/api/surfaces", put_surface, methods=["POST"]),
        Route("/api/surfaces/{workspace}/{name}", get_surface, methods=["GET"]),
        Route("/api/surfaces/{workspace}/{name}", delete_surface, methods=["DELETE"]),
        Route("/api/connectors", list_connectors, methods=["GET"]),
        Route("/api/connectors/{operation}/run", run_operation, methods=["POST"]),
    ]
