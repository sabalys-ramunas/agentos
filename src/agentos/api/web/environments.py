"""Environments: each agent's cloud computer, as the app's Environments pages read them.

    GET  /api/orgs/{org}/environments?state=open|all      {"environments": [EnvSummary]}
    GET  /api/orgs/{org}/environments/{id}                EnvDetail
    POST /api/orgs/{org}/environments/{id}/close          {"discard": false} -> EnvSummary (409 unsaved_work)
    POST /api/orgs/{org}/environments/{id}/extend         {"minutes": 60} -> EnvSummary

The shapes are the contract of docs/design/environments.md. These read the environments service
(agentos.environments) themselves and decide who may: reading needs `see` and a close or an extend
`operate` (a member, in workspace `env`). The owner sees every environment; a member only those their
own agents opened (the opener the environment records); a grant only its own family's. Each is also
checked against the caller's grant by its lanes, as the jobs routes check a run's lane. One outside
is not found, so its id tells nothing. There are no live events: the app polls while one is open.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute

from agentos.api.app import Api, access_of
from agentos.api.errors import error_body
from agentos.api.web.base import org_route
from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Effect

WORKSPACE = "env"
NO_ENVIRONMENTS = "This computer has no environments: agents open them with environment_open."
EXTEND_MAX = 24 * 60


def manager(api: Api) -> Any | None:
    mgr = api.optional("environments")
    return mgr if mgr is not None and callable(getattr(mgr, "summary", None)) else None


def environment_lane(api: Api, lane_id: str) -> bool:
    """Is this lane one an environment made (env/<id>, its terminal, or env/<id>-browser)? Those are the
    agent's own computer, shown on the environment's page, not an account."""
    workspace, _, name = str(lane_id).partition("/")
    mgr = manager(api)
    if workspace != WORKSPACE or mgr is None:
        return False
    try:
        return bool(mgr.store.has(name.removesuffix("-browser")))
    except (ValueError, AttributeError):
        return False


def without_environments(api: Api, workspaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Workspaces as Accounts and Home see them: an environment's lanes are left out, and workspace env
    with them when nothing else is in it."""
    out: list[dict[str, Any]] = []
    for workspace in workspaces:
        lanes = workspace.get("lanes") or []
        kept = [lane for lane in lanes if not environment_lane(api, lane.get("id", ""))]
        if workspace.get("id") == WORKSPACE and lanes and not kept:
            continue
        out.append({**workspace, "lanes": kept} if len(kept) != len(lanes) else workspace)
    return out


def _visible(access: Any, env: Any) -> bool:
    """Whose environment the caller may see: the owner every one, a member their own agents', and a
    grant its own family's (agentos.environments.manager.reaches)."""
    if not access.lane_allowed(env.terminal_lane):
        return False
    if access.owner and access.member is None:
        return True
    member = access.member
    if member is not None:
        return env.member is not None and env.member == member.id
    grant = getattr(getattr(access, "ctx", None), "grant", None)
    return grant is not None and env.family == getattr(grant, "root_id", None)


async def _body(request: Request) -> dict[str, Any]:
    if not (await request.body()):
        return {}
    try:
        body = await request.json()
    except ValueError:
        raise InvalidRequest("The body is JSON, like {\"discard\": false}.") from None
    if not isinstance(body, dict):
        raise InvalidRequest("The body is a JSON object.")
    return body


def routes(api: Api) -> list[BaseRoute]:
    def find(request: Request, action: str) -> tuple[Any, Any]:
        mgr = manager(api)
        if mgr is None:
            raise NotFound(NO_ENVIRONMENTS)
        env_id = request.path_params["id"]
        try:
            env = mgr.get(env_id)
        except (NotFound, ValueError):
            raise NotFound(f"There is no environment {env_id}.") from None
        access = access_of(request)
        if not _visible(access, env):
            access.trace(action, status="denied", lane=env.terminal_lane, summary="out of scope")
            raise NotFound(f"There is no environment {env_id}.")
        return mgr, env

    async def listing(request: Request) -> dict[str, Any]:
        mgr = manager(api)
        if mgr is None:
            return {"environments": [], "available": False, "reason": NO_ENVIRONMENTS}
        state = request.query_params.get("state") or "open"
        if state not in ("open", "all"):
            raise InvalidRequest("state is open or all.")
        access = access_of(request)
        envs = mgr.store.open() if state == "open" else mgr.store.recent(200)
        return {"environments": [mgr.summary(e) for e in envs if _visible(access, e)], "available": True}

    async def one(request: Request) -> dict[str, Any]:
        mgr, env = find(request, "environment.read")
        await access_of(request).authorize(Effect.READ, action="environment.read", lane_id=env.terminal_lane)
        return await mgr.detail(env)

    async def close(request: Request) -> Any:
        mgr, env = find(request, "environment.close")
        access = access_of(request)
        access.require("operate", WORKSPACE, action="environment.close", lane=env.terminal_lane)
        body = await _body(request)
        discard = body.get("discard", False)
        if not isinstance(discard, bool):
            raise InvalidRequest("discard is true or false.")
        await access.authorize(Effect.READ, action="environment.close", lane_id=env.terminal_lane,
                               summary=f"Close environment {env.id} ({env.title})"
                               + (", discarding work that is not pushed" if discard else ""))
        from agentos.environments.errors import UnsavedWork

        try:
            closed = await mgr.close(env.id, discard=discard, reason="closed from the app"
                                     + (" (discarded)" if discard else ""))
        except UnsavedWork as e:
            return JSONResponse(error_body("unsaved_work", e.message, unsaved=e.details), status_code=409)
        access.done("environment.close", lane=env.terminal_lane, summary=f"closed environment {env.id}")
        return mgr.summary(closed)

    async def extend(request: Request) -> dict[str, Any]:
        mgr, env = find(request, "environment.extend")
        access = access_of(request)
        access.require("operate", WORKSPACE, action="environment.extend", lane=env.terminal_lane)
        body = await _body(request)
        minutes = body.get("minutes", 60)
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= EXTEND_MAX:
            raise InvalidRequest(f"minutes is a whole number from 1 to {EXTEND_MAX}.")
        await access.authorize(Effect.READ, action="environment.extend", lane_id=env.terminal_lane,
                               summary=f"Keep environment {env.id} ({env.title}) {minutes} min longer")
        out = await mgr.extend(env.id, minutes)
        access.done("environment.extend", lane=env.terminal_lane, summary=f"extended environment {env.id}")
        return mgr.summary(out)

    return [
        org_route(api, "/environments", listing, ["GET"], cap="see"),
        org_route(api, "/environments/{id}", one, ["GET"], cap="see"),
        org_route(api, "/environments/{id}/close", close, ["POST"], cap="operate"),
        org_route(api, "/environments/{id}/extend", extend, ["POST"], cap="operate"),
    ]
