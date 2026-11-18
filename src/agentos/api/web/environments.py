"""Environments: each agent's cloud computer, as the app's Environments pages read them.

    GET  /api/orgs/{org}/environments?state=open|all      {"environments": [EnvSummary]}
    GET  /api/orgs/{org}/environments/{id}                EnvDetail, with proofs [{id, title, taken_at,
                                                          page_url, width, height, src}]
    GET  /api/orgs/{org}/environments/{id}/proofs/{proof}.png   a proof's PNG (src above)
    POST /api/orgs/{org}/environments/{id}/close          {"discard": false} -> EnvSummary (409 unsaved_work)
    POST /api/orgs/{org}/environments/{id}/extend         {"minutes": 60} -> EnvSummary
    DELETE /api/orgs/{org}/environments/saves/{save_id}   forget a save and its snapshot -> {forgot,
                                                          snapshots_deleted}. The owner in person
    POST /api/orgs/{org}/environments/saves/{save_id}/resume   resume a save, held by no chat
                                                          (agentos.environments.waking) -> EnvSummary with
                                                          opened, resumed and links. The owner in person
    GET  /api/orgs/{org}/environments/{id}/checkpoints/{checkpoint}  one checkpoint against the work now: per
                                                          repository its files and patch (capped). The owner

The detail's `checkpoints` (agentos.environments.checkpoints) carry `diff` in each repository: what
changed since, against the work tree now (files added, changed, removed; lines added, removed).

The listing also gives `saved`: [{id, title, repo, branch, head, saved_at, snapshot_gb, files_bytes,
cost_eur_month, note, agent}], the saves of environments that are not open (agentos.environments.saving),
and `snapshots_eur_month`, what the kept snapshots cost. Each summary has `keep` (none, live, save).

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
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute

from agentos.api.access import Presence
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


def environment_id(lane_id: str | None) -> str | None:
    """The environment a lane belongs to by its id alone (env/<id> or env/<id>-browser), else None."""
    workspace, _, name = str(lane_id or "").partition("/")
    return name.removesuffix("-browser") or None if workspace == WORKSPACE else None


def titles(api: Api, limit: int = 500) -> dict[str, str]:
    """Every recent environment's id to its title: the one name a person knows it by."""
    mgr = manager(api)
    if mgr is None:
        return {}
    try:
        return {env.id: env.title for env in mgr.store.recent(limit) if getattr(env, "title", None)}
    except (AttributeError, ValueError):
        return {}


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


def _save_visible(access: Any, m: Any) -> bool:
    """Whose saves the caller may see, as for environments: the owner every one, a member their own
    agents', a grant its own family's."""
    if access.owner and access.member is None:
        return True
    member = access.member
    if member is not None:
        return m.member is not None and m.member == member.id
    grant = getattr(getattr(access, "ctx", None), "grant", None)
    return grant is not None and m.family == getattr(grant, "root_id", None)


SAVED_KEYS = ("id", "title", "repo", "branch", "head", "saved_at", "snapshot_gb", "files_bytes", "cost_eur_month",
              "note", "agent", "prune_at")


def proofs_of(mgr: Any, env: Any, org: str) -> list[dict[str, Any]]:
    """The screenshots the agent kept as proof (agentos.environments.proofs), each with `src`: the
    app's own route for its PNG, behind the same sign-in as this page."""
    listed = getattr(mgr, "proofs", None)
    if not callable(listed):
        return []
    try:
        records = listed(env, links=False)
    except Exception:  # noqa: BLE001 - the page without its proofs rather than none at all
        return []
    return [{**r, "src": f"/api/orgs/{org}/environments/{env.id}/proofs/{r['id']}.png"} for r in records]


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
        out: dict[str, Any] = {"environments": [mgr.summary(e) for e in envs if _visible(access, e)],
                               "available": True}
        saved_for = getattr(mgr, "saved_for", None)
        if callable(saved_for):
            try:
                rows = saved_for(None, visible=lambda m: _save_visible(access, m))
            except Exception:  # noqa: BLE001 - the page without its saves rather than none at all
                rows = []
            out["saved"] = [{k: r.get(k) for k in SAVED_KEYS} for r in rows]
            out["snapshots_eur_month"] = mgr.snapshots_eur_month()
        return out

    async def one(request: Request) -> dict[str, Any]:
        mgr, env = find(request, "environment.read")
        await access_of(request).authorize(Effect.READ, action="environment.read", lane_id=env.terminal_lane)
        out = await mgr.detail(env)
        out["proofs"] = proofs_of(mgr, env, request.path_params["org"])
        summaries = getattr(mgr, "checkpoint_summaries", None)
        if out.get("checkpoints") and callable(summaries):
            out["checkpoints"] = await summaries(env, out["checkpoints"])
        return out

    async def proof_png(request: Request) -> Response:
        mgr, env = find(request, "environment.proof")
        await access_of(request).authorize(Effect.READ, action="environment.proof", lane_id=env.terminal_lane)
        proof_id = request.path_params["proof"]
        read = getattr(mgr, "proof_png", None)
        data = read(env.id, proof_id) if callable(read) else None
        if not data:
            raise NotFound(f"Environment {env.id} has no proof {proof_id}.")
        return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=3600",
                                                               "X-Content-Type-Options": "nosniff"})

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

        try:  # the owner's close deletes a kept ("live") machine too
            closed = await mgr.close(env.id, discard=discard, final=True, reason="closed from the app"
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

    async def forget_save(request: Request) -> dict[str, Any]:
        mgr = manager(api)
        if mgr is None or not callable(getattr(mgr, "forget", None)):
            raise NotFound(NO_ENVIRONMENTS)
        access = access_of(request)
        save_id = request.path_params["save_id"]
        from agentos.environments.saves import check_save_id

        try:
            m, _ = mgr.saves().find(check_save_id(save_id))
        except (NotFound, InvalidRequest):
            raise NotFound(f"There is no save {save_id}.") from None
        await access.authorize(Effect.READ, action="environment.forget",
                               summary=f"Forget the save {m.id} of environment \"{m.title}\" and its snapshot")
        out = await mgr.forget(None, None, m.id)
        access.done("environment.forget", summary=f"forgot save {m.id} ({m.title})")
        return out

    return [
        org_route(api, "/environments/saves/{save_id}", forget_save, ["DELETE"], owner=True,
                  presence=Presence.PERSON),
        org_route(api, "/environments", listing, ["GET"], cap="see"),
        org_route(api, "/environments/{id}", one, ["GET"], cap="see"),
        org_route(api, "/environments/{id}/proofs/{proof}.png", proof_png, ["GET"], cap="see"),
        org_route(api, "/environments/{id}/close", close, ["POST"], cap="operate"),
        org_route(api, "/environments/{id}/extend", extend, ["POST"], cap="operate"),
        *_owner_routes(api, find),
    ]


def _owner_routes(api: Api, find: Any) -> list[BaseRoute]:
    """The owner's resume of a save, and one checkpoint's diff (agentos.environments.waking, checkpoints)."""

    async def checkpoint_diff(request: Request) -> dict[str, Any]:
        mgr, env = find(request, "environment.checkpoint")
        raw = request.path_params["checkpoint"]
        if not raw.isdigit() or not 0 < int(raw) < 10**9:
            raise InvalidRequest("a checkpoint is its number, like 3.")
        await access_of(request).authorize(Effect.READ, action="environment.checkpoint", lane_id=env.terminal_lane)
        return await mgr.checkpoint_diff(env, int(raw))

    async def resume_save(request: Request) -> dict[str, Any]:
        mgr = manager(api)
        if mgr is None or not callable(getattr(mgr, "resume_saved", None)):
            raise NotFound(NO_ENVIRONMENTS)
        access = access_of(request)
        save_id = request.path_params["save_id"]
        from agentos.environments.saves import check_save_id

        try:
            m, _ = mgr.saves().find(check_save_id(save_id))
        except (NotFound, InvalidRequest):
            raise NotFound(f"There is no save {save_id}.") from None
        await access.authorize(Effect.READ, action="environment.resume",
                               summary=f"Resume the save {m.id} of environment \"{m.title}\" on a machine")
        env, how = await mgr.resume_saved(m.id)
        access.done("environment.resume", lane=env.terminal_lane,
                    summary=f"resumed save {m.id} ({m.title}) as {env.id}" if how == "resumed" else
                    f"\"{m.title}\" was open already ({env.id})")
        return mgr.owner_view(env, how)

    return [
        org_route(api, "/environments/saves/{save_id}/resume", resume_save, ["POST"], owner=True,
                  presence=Presence.PERSON),
        org_route(api, "/environments/{id}/checkpoints/{checkpoint}", checkpoint_diff, ["GET"], owner=True),
    ]
