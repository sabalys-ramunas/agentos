"""Panel API routes for runs, flows and schedules (docs/API.md), plus the webhook receiver.

`routes(runtime)` assumes the panel's auth middleware already ran (bearer or session +
CSRF); it reads the caller from `request.state.principal` when set (default: the owner).
Runs and schedules made here carry that principal's grant; the owner's is the owner grant.
`public_routes` holds the webhook receiver, which authenticates by HMAC instead and must be
mounted outside the bearer/session check.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agentos.core.errors import (
    AgentosError,
    InvalidRequest,
    NotFound,
    ServiceUnavailable,
)
from agentos.core.http_errors import error_body, status_for
from agentos.core.models import Principal, RunStatus
from agentos.core.presence import Presence
from agentos.runner.flows import lint
from agentos.runner.models import is_owner

PERSON = Presence.PERSON


def _error(e: AgentosError) -> JSONResponse:
    return JSONResponse(error_body(e.code, e.message), status_code=status_for(e))


def _principal(request: Request) -> Principal:
    p = getattr(request.state, "principal", None)
    return p if isinstance(p, Principal) else Principal(kind="human", id="owner")


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


def _refuse_family(request: Request) -> str | None:
    """The grant family a caller may not decide runs for: its own root grant when it acts under a
    token, as for approvals. The owner signed in to the panel decides every run."""
    auth = getattr(request.state, "auth", None)
    if auth is None or auth.via != "bearer":
        return None
    from agentos.api.routes_resources import grant_family

    return grant_family(auth.access)


def _int(value: str | None, default: int, hi: int = 500) -> int:
    try:
        return max(1, min(hi, int(value))) if value else default
    except ValueError as e:
        raise InvalidRequest(f"not a number: {value!r}") from e


def routes(runtime: Any) -> list[Route]:
    def runner():
        return runtime.require("runner")

    def flows():
        return runtime.require("flows")

    def handle(fn):
        async def endpoint(request: Request) -> Response:
            try:
                return await fn(request)
            except AgentosError as e:
                return _error(e)
        endpoint.__name__ = fn.__name__
        return endpoint

    # ---- runs ----
    @handle
    async def list_runs(request: Request) -> Response:
        q = request.query_params
        status = [RunStatus(s) for s in q["status"].split(",")] if q.get("status") else None
        rs = runner().list(status=status, lane=q.get("lane") or None, flow_id=q.get("flow") or None,
                           workspace=q.get("workspace") or None, limit=_int(q.get("limit"), 50))
        return JSONResponse([runner().describe(r) for r in rs])

    @handle
    async def get_run(request: Request) -> Response:
        return JSONResponse(runner().describe(runner().get(request.path_params["id"]), steps=True))

    @handle
    async def cancel_run(request: Request) -> Response:
        run = await runner().cancel(request.path_params["id"], by=_principal(request).id)
        return JSONResponse(runner().describe(run))

    @handle
    async def decide_run(request: Request) -> Response:
        body = await _body(request)
        run = await runner().decide(request.path_params["id"], str(body.get("decision", "")),
                                    by=_principal(request).id, refuse_family=_refuse_family(request))
        return JSONResponse(runner().describe(run))

    # ---- flows ----
    @handle
    async def list_flows(request: Request) -> Response:
        out = []
        for fv in flows().list(request.query_params.get("prefix") or None):
            s = flows().summary(fv)
            s["params"] = fv.definition.params
            out.append(s)
        return JSONResponse(out)

    @handle
    async def get_flow(request: Request) -> Response:
        fid = request.path_params["id"]
        fv = flows().get(fid, request.query_params.get("version") or None)
        return JSONResponse({**flows().summary(fv), "definition": fv.definition.canonical(),
                             "warnings": lint(fv.definition, effect=fv.effect),
                             "versions": [{"version": v.version, "created_at": v.created_at.isoformat(),
                                           "created_by": v.created_by} for v in flows().versions(fid)]})

    @handle
    async def save_flow(request: Request) -> Response:
        body = await _body(request)
        auth = getattr(request.state, "auth", None)  # a token may be an agent's, even the owner's
        by: Any = _principal(request)
        presence = getattr(getattr(auth, "access", None), "presence", None)
        below = auth is not None and (auth.via == "bearer" or (presence is not None and presence < PERSON))
        if below and isinstance(by, Principal) and is_owner(by.model_dump()):
            # the device credential is the owner's session but not the owner in person (an agent may hold
            # it): its save never vouches for a version (publishes: false, a schedule following it)
            by = {"kind": "agent", "id": by.id, "owner": by.owner or "owner", "via": "device"}
        fv, created = flows().save(body.get("definition", body), by=by, agent=True if below else None)
        return JSONResponse({"id": fv.id, "version": fv.version, "created": created, "effect": str(fv.effect),
                             "warnings": lint(fv.definition, effect=fv.effect)}, status_code=201 if created else 200)

    @handle
    async def run_flow(request: Request) -> Response:
        body = await _body(request)
        run, created = runner().submit(
            flow_id=request.path_params["id"], version=body.get("version"), params=body.get("params") or {},
            lane=body.get("on"), idempotency_key=body.get("idempotency_key"), principal=_principal(request),
            source="api", authorized=False)  # under the caller's grant; the runner authorizes before it runs
        return JSONResponse({**runner().describe(run), "duplicate": not created}, status_code=202)

    # ---- schedules ----
    def scheduler():
        s = runner().scheduler
        if s is None:
            raise ServiceUnavailable("schedules are not configured on this node")
        return s

    @handle
    async def list_schedules(request: Request) -> Response:
        sch = scheduler()
        return JSONResponse([sch.describe(s) for s in sch.list(request.query_params.get("kind") or None)])

    @handle
    async def add_schedule(request: Request) -> Response:
        b = await _body(request)
        allowed = {"flow", "params", "on", "version", "cron", "timezone", "missed", "jitter_s", "watch", "webhook",
                   "deadman_minutes", "max_failures", "enabled"}
        unknown = set(b) - allowed
        if unknown:
            raise InvalidRequest(f"unknown fields: {', '.join(sorted(unknown))}")
        if not b.get("flow"):
            raise InvalidRequest("flow is required")
        s, extras = scheduler().add(**b, principal=_principal(request))
        return JSONResponse({**scheduler().describe(s), **({"webhook_setup": extras} if extras else {})},
                            status_code=201)

    @handle
    async def get_schedule(request: Request) -> Response:
        return JSONResponse(scheduler().describe(scheduler().get(request.path_params["id"])))

    @handle
    async def delete_schedule(request: Request) -> Response:
        scheduler().remove(request.path_params["id"])
        return JSONResponse({"deleted": request.path_params["id"]})

    @handle
    async def enable_schedule(request: Request) -> Response:
        action = request.path_params["action"]
        if action not in ("enable", "disable"):
            raise NotFound(f"no such action: {action}")
        on = action == "enable"
        return JSONResponse(scheduler().describe(scheduler().set_enabled(request.path_params["id"], on)))

    return [
        Route("/api/runs", list_runs, methods=["GET"]),
        Route("/api/runs/{id}", get_run, methods=["GET"]),
        Route("/api/runs/{id}/cancel", cancel_run, methods=["POST"]),
        Route("/api/runs/{id}/decide", decide_run, methods=["POST"]),
        Route("/api/flows", list_flows, methods=["GET"]),
        Route("/api/flows", save_flow, methods=["POST"]),
        Route("/api/flows/{id}", get_flow, methods=["GET"]),
        Route("/api/flows/{id}/run", run_flow, methods=["POST"]),
        Route("/api/schedules", list_schedules, methods=["GET"]),
        Route("/api/schedules", add_schedule, methods=["POST"]),
        Route("/api/schedules/{id}", get_schedule, methods=["GET"]),
        Route("/api/schedules/{id}", delete_schedule, methods=["DELETE"]),
        Route("/api/schedules/{id}/{action:str}", enable_schedule, methods=["POST"]),
    ]


def public_routes(runtime: Any) -> list[Route]:
    async def webhook(request: Request) -> Response:
        runner = runtime.get("runner")
        if runner is None or runner.scheduler is None:
            return JSONResponse({"error": {"code": "not_found", "message": "unknown hook"}}, status_code=404)
        body = await request.body()
        if len(body) > 256 * 1024:
            return JSONResponse({"error": {"code": "invalid", "message": "body too large"}}, status_code=413)
        status, payload = await runner.scheduler.handle_webhook(request.path_params["token"], body,
                                                                dict(request.headers))
        return JSONResponse(payload, status_code=status)

    return [Route("/api/hooks/{token}", webhook, methods=["POST"])]
