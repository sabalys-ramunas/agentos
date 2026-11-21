"""Panel API routes for brains (docs/API.md, Brains). Mounted under /api by the panel.

Authentication and CSRF are the mounting app's job (its middleware sets
`request.state.principal` when it knows the caller). No route returns a credential, a
child grant token or a prompt body.

  GET  /api/brains                       brains with state, seats and window use
  POST /api/brains/order                 {"order": ["claude-max", "ollama-local", ...]}
  GET  /api/brains/chip                  the top-bar Brain chip (name, state, offline_ready)
  GET  /api/brains/tasks                 ?status=&workspace=&limit=
  GET  /api/brains/tasks/{id}            status plus a transcript tail (?since=&limit=)
  POST /api/brains/tasks/{id}/cancel
  POST /api/brains/tasks/{id}/resume     {"brain"?}
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from openajar.core.errors import InvalidRequest, OpenajarError
from openajar.core.http_errors import error_body, status_for


def _err(exc: OpenajarError) -> JSONResponse:
    body: dict[str, Any] = error_body(exc.code, exc.message)
    if getattr(exc, "approval_id", None):
        body["error"]["approval_id"] = exc.approval_id
    return JSONResponse(body, status_code=status_for(exc))


def _int(v: str | None, default: int | None) -> int | None:
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError as exc:
        raise InvalidRequest(f"not a number: {v}") from exc


def routes(runtime: Any) -> list[Route]:
    def svc():
        return runtime.require("brains")

    def principal(request: Request):
        return getattr(request.state, "principal", None)

    def handler(fn):
        async def wrapped(request: Request):
            try:
                return JSONResponse(await fn(request))
            except OpenajarError as exc:
                return _err(exc)
        return wrapped

    async def list_brains(request: Request):
        return await svc().list_brains()

    async def order(request: Request):
        try:
            body = await request.json()
        except ValueError as exc:
            raise InvalidRequest("body must be JSON") from exc
        names = body.get("order") if isinstance(body, dict) else None
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise InvalidRequest('body must be {"order": ["brain", ...]}')
        svc().set_order(names)
        return await svc().list_brains()

    async def chip(request: Request):
        return await svc().chip()

    async def tasks(request: Request):
        q = request.query_params
        return svc().list_tasks(principal(request), status=q.get("status") or None,
                                workspace=q.get("workspace") or None, limit=_int(q.get("limit"), 50) or 50)

    async def task(request: Request):
        tid = request.path_params["id"]
        q = request.query_params
        out = svc().status(tid, principal(request))
        out["transcript"] = svc().tail(tid, principal(request), since=_int(q.get("since"), None),
                                       limit=_int(q.get("limit"), 100) or 100)
        return out

    async def cancel(request: Request):
        return await svc().cancel(request.path_params["id"], principal(request))

    async def resume(request: Request):
        body = {}
        if await request.body():
            try:
                body = await request.json()
            except ValueError as exc:
                raise InvalidRequest("body must be JSON") from exc
        return await svc().resume(request.path_params["id"], principal(request),
                                  brain=(body or {}).get("brain"))

    return [
        Route("/api/brains", handler(list_brains), methods=["GET"]),
        Route("/api/brains/order", handler(order), methods=["POST"]),
        Route("/api/brains/chip", handler(chip), methods=["GET"]),
        Route("/api/brains/tasks", handler(tasks), methods=["GET"]),
        Route("/api/brains/tasks/{id}", handler(task), methods=["GET"]),
        Route("/api/brains/tasks/{id}/cancel", handler(cancel), methods=["POST"]),
        Route("/api/brains/tasks/{id}/resume", handler(resume), methods=["POST"]),
    ]
