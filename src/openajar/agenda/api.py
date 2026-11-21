"""Panel API routes for the agenda (docs/API.md, "Agenda"), mounted by openajar.api.mounts behind the
panel's auth as owner routes: the owner, or an admin grant. A member approves an agenda entry the
way they approve anything, in Needs you, where their role, their passkey and four eyes are checked.

`routes(runtime)` reads the caller from `request.state.principal` (default: the owner). Entries
drafted here carry that principal's grant; the owner's is the owner grant.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from openajar.agenda.models import EntryState
from openajar.agenda.rules import SERVICE_NAME
from openajar.core import catalog
from openajar.core.errors import (
    Conflict,
    InvalidRequest,
    LaneNotFound,
    NotFound,
    OpenajarError,
    PolicyDenied,
    ServiceUnavailable,
)
from openajar.core.models import Principal

STATUS = {InvalidRequest: 400, PolicyDenied: 403, NotFound: 404, LaneNotFound: 404, Conflict: 409,
          ServiceUnavailable: 503}
STATES = tuple(s.value for s in EntryState) + ("projected", "planned")
DRAFT_FIELDS = {"title", "action", "when", "notes", "propose"}
PATCH_FIELDS = {"title", "action", "when", "notes"}


def _error(e: OpenajarError) -> JSONResponse:
    code = next((v for k, v in STATUS.items() if isinstance(e, k)), 400)
    return JSONResponse(catalog.error_body(e.code, e.message), status_code=code)


def _principal(request: Request) -> Principal:
    p = getattr(request.state, "principal", None)
    return p if isinstance(p, Principal) else Principal(kind="human", id="owner", grant_id="owner")


def _by(request: Request) -> str:
    p = _principal(request)
    return "owner" if p.id == "owner" else p.id


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


def _time(value: str | None, name: str, default: datetime) -> datetime:
    if not value:
        return default
    try:
        t = datetime.fromisoformat(value.strip())
    except ValueError:
        raise InvalidRequest(f"{name} is an ISO 8601 time like 2026-11-08T00:00:00+02:00") from None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _refuse_family(request: Request) -> str | None:
    """A caller acting under a token never decides what its own grant family asked for."""
    auth = getattr(request.state, "auth", None)
    if auth is None or auth.via != "bearer":
        return None
    from openajar.api.routes_resources import grant_family

    return grant_family(auth.access)


def routes(runtime: Any) -> list[Route]:
    def agenda():
        return runtime.require(SERVICE_NAME)

    def handle(fn):
        async def endpoint(request: Request) -> Response:
            try:
                return await fn(request)
            except OpenajarError as e:
                return _error(e)
        endpoint.__name__ = fn.__name__
        return endpoint

    @handle
    async def timeline(request: Request) -> Response:
        q = request.query_params
        svc = agenda()
        svc.sync()
        start = _time(q.get("start"), "start", svc.clock.now() - timedelta(days=1))
        end = _time(q.get("end"), "end", start + timedelta(days=8))
        states = [s for s in (q.get("state") or "").split(",") if s]
        bad = [s for s in states if s not in STATES]
        if bad:
            raise InvalidRequest(f"state is one of {', '.join(STATES)}")
        managed = (q.get("managed") or "1").strip().lower() not in ("0", "false", "no", "off")
        return JSONResponse(svc.timeline(start=start, end=end, workspace=q.get("workspace") or None,
                                         lane=q.get("lane") or None, kind=q.get("kind") or None,
                                         states=states or None, managed=managed))

    @handle
    async def get_entry(request: Request) -> Response:
        svc = agenda()
        svc.sync()
        return JSONResponse(svc.describe(svc.get(request.path_params["id"])))

    @handle
    async def draft(request: Request) -> Response:
        b = await _body(request)
        unknown = set(b) - DRAFT_FIELDS
        if unknown:
            raise InvalidRequest(f"unknown fields: {', '.join(sorted(unknown))}")
        svc = agenda()
        e = svc.draft(title=b.get("title") or "", action=b.get("action") or {}, when=b.get("when"),
                      notes=b.get("notes"), principal=_principal(request))
        if b.get("propose"):
            e = await svc.propose(e.id, by=_by(request))
        return JSONResponse(svc.describe(e), status_code=201)

    @handle
    async def patch(request: Request) -> Response:
        b = await _body(request)
        unknown = set(b) - PATCH_FIELDS
        if unknown:
            raise InvalidRequest(f"unknown fields: {', '.join(sorted(unknown))}")
        svc = agenda()
        e = await svc.update(request.path_params["id"], title=b.get("title"), notes=b.get("notes"),
                             action=b.get("action"), when=b.get("when"), by=_by(request))
        return JSONResponse(svc.describe(e))

    @handle
    async def act(request: Request) -> Response:
        action = request.path_params["action"]
        svc = agenda()
        entry_id = request.path_params["id"]
        b = await _body(request)
        if action == "propose":
            e = await svc.propose(entry_id, by=_by(request))
        elif action == "approve":
            auth = getattr(request.state, "auth", None)
            if auth is not None and getattr(auth.access, "member", None) is not None:
                raise PolicyDenied("a member approves an agenda entry in Needs you, where their role and passkey "
                                   "are checked")
            note = b.get("note")
            e = await svc.approve(entry_id, by=_by(request), refuse_family=_refuse_family(request),
                                  note=str(note) if note else None)
        elif action == "cancel":
            reason = b.get("reason")
            e = await svc.cancel(entry_id, by=_by(request), reason=str(reason) if reason else None)
        else:
            raise NotFound(f"no such action: {action}")
        return JSONResponse(svc.describe(e))

    return [
        Route("/api/agenda", timeline, methods=["GET"]),
        Route("/api/agenda", draft, methods=["POST"]),
        Route("/api/agenda/{id}", get_entry, methods=["GET"]),
        Route("/api/agenda/{id}", patch, methods=["PATCH"]),
        Route("/api/agenda/{id}/{action:str}", act, methods=["POST"]),
    ]
