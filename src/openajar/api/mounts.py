"""The feature packages' own panel routes (their api.py), mounted where the panel does not
already cover them, behind the panel's auth. The decision per package is in docs/API.md.

Only the (path, method) pairs listed here are mounted; everything else in those modules is a
duplicate of a panel route (served through openajar.api.bridge instead) and stays unmounted.
Every mounted route needs a signed-in caller (401), the owner or an admin grant (403), the
CSRF header on cookie writes, and writes are traced as panel actions. The packages are found
by import, like the daemon's FEATURE_SERVICES, so a node without one simply lacks its routes.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from openajar.api.app import PERSON_ONLY, Api, access_of, endpoint, owner_in_person
from openajar.api.errors import ApiError
from openajar.core.aio import maybe_await
from openajar.core.presence import Presence

log = logging.getLogger("openajar.api")

# module -> {(path in the module, method): (path on the node, action name for traces)}
MOUNTS: dict[str, dict[tuple[str, str], tuple[str, str]]] = {
    "openajar.vault.api": {
        ("/vault/status", "GET"): ("/api/vault/status", "vault.status"),
        ("/vault/unseal", "POST"): ("/api/vault/unseal", "vault.unseal"),
        ("/vault/seal", "POST"): ("/api/vault/seal", "vault.seal"),
    },
    "openajar.runner.api": {
        ("/api/runs/{id}/cancel", "POST"): ("/api/runs/{id}/cancel", "run.cancel"),
        ("/api/runs/{id}/decide", "POST"): ("/api/runs/{id}/decide", "run.decide"),
        ("/api/flows/{id}", "GET"): ("/api/flows/{id}", "flow.get"),
        ("/api/flows", "POST"): ("/api/flows", "flow.save"),
        ("/api/schedules/{id}/{action:str}", "POST"): ("/api/schedules/{id}/{action:str}", "schedule.toggle"),
    },
    "openajar.runner.connectors.api": {  # AUD-01 surfaces and connector operations (docs/design/connectors.md)
        ("/api/surfaces", "GET"): ("/api/surfaces", "surface.list"),
        ("/api/surfaces", "POST"): ("/api/surfaces", "surface.put"),
        ("/api/surfaces/{workspace}/{name}", "GET"): ("/api/surfaces/{workspace}/{name}", "surface.get"),
        ("/api/surfaces/{workspace}/{name}", "DELETE"): ("/api/surfaces/{workspace}/{name}", "surface.delete"),
        ("/api/connectors", "GET"): ("/api/connectors", "connector.list"),
        ("/api/connectors/{operation}/run", "POST"): ("/api/connectors/{operation}/run", "connector.run"),
    },
    "openajar.runner.siteapis.api": {  # a person's site APIs (docs/SITE-APIS.md); /usage and /api/site-keys are http.py's
        ("/api/site-apis", "GET"): ("/api/site-apis", "site_api.list"),
        ("/api/site-apis", "POST"): ("/api/site-apis", "site_api.save"),
        ("/api/site-apis/discover", "POST"): ("/api/site-apis/discover", "site_api.discover"),
        ("/api/site-apis/discover", "GET"): ("/api/site-apis/discover", "site_api.discover_view"),
        ("/api/site-apis/{id}", "GET"): ("/api/site-apis/{id}", "site_api.get"),
        ("/api/site-apis/{id}", "PATCH"): ("/api/site-apis/{id}", "site_api.update"),
        ("/api/site-apis/{id}", "DELETE"): ("/api/site-apis/{id}", "site_api.delete"),
        ("/api/site-apis/{id}/activate", "POST"): ("/api/site-apis/{id}/activate", "site_api.activate"),
        ("/api/site-apis/{id}/pause", "POST"): ("/api/site-apis/{id}/pause", "site_api.pause"),
        ("/api/site-apis/{id}/try", "POST"): ("/api/site-apis/{id}/try", "site_api.try"),
        ("/api/site-apis/{id}/read", "POST"): ("/api/site-apis/{id}/read", "site_api.read"),
        ("/api/site-apis/{id}/resume", "POST"): ("/api/site-apis/{id}/resume", "site_api.resume"),
        ("/api/site-apis/{id}/decline", "POST"): ("/api/site-apis/{id}/decline", "site_api.decline"),
        ("/api/site-apis/{id}/accept-empty", "POST"): ("/api/site-apis/{id}/accept-empty", "site_api.accept_empty"),
        ("/api/site-apis/{id}/repair", "POST"): ("/api/site-apis/{id}/repair", "site_api.repair"),
        ("/api/site-apis/{id}/keep-updated", "POST"): ("/api/site-apis/{id}/keep-updated", "site_api.keep_updated"),
    },
    "openajar.node.viewer_routes": {  # lane control (take over, hand back, stop, deny), view links, site lists
        ("/lanes/{workspace}/{name}/control", "POST"): ("/api/lanes/{workspace}/{name}/control", "lane.control"),
        ("/lanes/{workspace}/{name}/view-link", "POST"): ("/api/lanes/{workspace}/{name}/view-link",
                                                           "lane.view_link"),
        ("/lanes/{workspace}/{name}/sites", "POST"): ("/api/lanes/{workspace}/{name}/sites", "lane.sites"),
    },
    "openajar.node.expose.api": {  # the owner's dev server in a lane, a lane's port on the laptop (DEVICE_ACTIONS)
        ("/exposes", "GET"): ("/api/exposes", "expose.list"),
        ("/exposes", "POST"): ("/api/exposes", "expose.create"),
        ("/exposes/{id}/ready", "POST"): ("/api/exposes/{id}/ready", "expose.ready"),
        ("/exposes/{id}/heartbeat", "POST"): ("/api/exposes/{id}/heartbeat", "expose.heartbeat"),
        ("/exposes/{id}", "DELETE"): ("/api/exposes/{id}", "expose.end"),
    },
    "openajar.workspaces.api": {  # the panel serves list, create, templates and apply itself
        ("/api/workspaces/{id}", "GET"): ("/api/workspaces/{id}", "workspace.get"),
        ("/api/workspaces/{id}", "PATCH"): ("/api/workspaces/{id}", "workspace.update"),
        ("/api/workspaces/{id}", "DELETE"): ("/api/workspaces/{id}", "workspace.delete"),
        ("/api/workspaces/{id}/queue", "GET"): ("/api/workspaces/{id}/queue", "workspace.queue"),
        ("/api/queue", "GET"): ("/api/queue", "queue.get"),
        ("/api/lanes/{workspace}/{name}/readiness", "POST"): ("/api/lanes/{workspace}/{name}/readiness",
                                                              "lane.readiness"),
        ("/api/templates/plan", "POST"): ("/api/templates/plan", "template.plan"),
        ("/api/overrides", "GET"): ("/api/overrides", "overrides.get"),
        ("/api/workspaces/{id}/export", "POST"): ("/api/workspaces/{id}/export", "workspace.export"),
        ("/api/workspaces/import", "POST"): ("/api/workspaces/import", "workspace.import"),
    },
    "openajar.agenda.api": {  # the agenda: planned actions, their approval and proof (docs/AGENDA.md)
        ("/api/agenda", "GET"): ("/api/agenda", "agenda.list"),
        ("/api/agenda", "POST"): ("/api/agenda", "agenda.draft"),
        ("/api/agenda/{id}", "GET"): ("/api/agenda/{id}", "agenda.get"),
        ("/api/agenda/{id}", "PATCH"): ("/api/agenda/{id}", "agenda.update"),
        ("/api/agenda/{id}/{action:str}", "POST"): ("/api/agenda/{id}/{action:str}", "agenda.act"),
    },
    "openajar.brains.api": {
        ("/api/brains/tasks/{id}/cancel", "POST"): ("/api/brains/tasks/{id}/cancel", "brain_task.cancel"),
        ("/api/brains/tasks/{id}/resume", "POST"): ("/api/brains/tasks/{id}/resume", "brain_task.resume"),
    },
}
SAFE = frozenset({"GET", "HEAD"})
# mounted actions that are a decision, so they need a person (Presence.PERSON; docs/THREAT-MODEL.md): retrying
# or skipping a partial run lets its next step run, as approving does
PERSON_DECISIONS = frozenset({"run.decide"})

# A path between the owner's computer and a lane (openajar.node.expose): the owner's own computer (the device
# credential) or a person, never a grant (an admin grant included) or the node token, which an agent may hold.
DEVICE_ACTIONS = frozenset({"expose.list", "expose.create", "expose.ready", "expose.heartbeat", "expose.end"})
# writes that are no action of their own: a lease renewed every 10 seconds (its start and end are traced)
UNTRACED_WRITES = frozenset({"expose.heartbeat"})

# A surface is who an account acts as: the owner in person adds and removes one. A token may read them
# and run a connector (the runner still asks before a commit), but never changes who the node acts as.
PERSON_ONLY_ACTIONS = frozenset({"surface.put", "surface.delete"})


def _module_routes(name: str, runtime: Any) -> list[Route]:
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name and name.startswith(exc.name):
            return []
        raise
    return [r for r in module.routes(runtime) if isinstance(r, Route)]


async def _schedule_on_needs_a_person(api: Api, request: Request) -> None:
    """Turning on a schedule under the owner's own grant (as PATCH /api/schedules/{id} does)."""
    if request.path_params.get("action") != "enable":
        return
    schedule = await maybe_await(api.svc("runner").get_schedule(request.path_params["id"]))
    if (schedule.get("grant_id") or "owner") == "owner":
        request.state.auth.access.require_presence(Presence.PERSON, "schedule.toggle")


# A lane control's `action` (node.viewer_actions.ACTIONS): what a person did, kept on the trace so History can say it.
CONTROL_ACTIONS = ("take", "hand_back", "stop_agent", "deny_request")


async def _control_fields(request: Request, action: str) -> dict[str, str]:
    """For a lane control, the action its JSON body names, as a trace field. Nothing else, and nothing for a body
    that names none: the trace keeps what it always did, and only adds this."""
    if action != "lane.control":
        return {}
    try:
        body = await request.json()  # cached by Starlette: the endpoint reads the same body
    except ValueError:
        return {}
    named = body.get("action") if isinstance(body, dict) else None
    return {"control": named} if named in CONTROL_ACTIONS else {}


async def _trace_write(request: Request, action: str) -> None:
    """Trace a write as a panel action. A lane route names its account, so the row carries the lane (History
    reads an account's own changes by it); a lane control also carries its action."""
    ids = {k: v for k, v in request.path_params.items() if k in ("id", "action", "workspace", "name", "operation")}
    workspace, name = request.path_params.get("workspace"), request.path_params.get("name")
    lane = {"lane": f"{workspace}/{name}"} if workspace and name else {}
    fields = await _control_fields(request, action)
    request.state.auth.access.trace(action, summary=f"{action} {' '.join(map(str, ids.values()))}".strip(), **lane, **fields)


def _wrap(api: Api, inner: Any, method: str, action: str) -> Any:
    async def call(request: Request) -> Response:
        if action == "schedule.toggle":
            await _schedule_on_needs_a_person(api, request)
        if action in PERSON_ONLY_ACTIONS and not owner_in_person(request):
            access_of(request).trace(action, status="denied", summary="needs the owner in person")
            raise ApiError("denied", PERSON_ONLY)
        if method not in SAFE and action not in UNTRACED_WRITES:
            await _trace_write(request, action)
        return await inner(request)

    call.__name__ = action.replace(".", "_")
    presence = Presence.PERSON if action in PERSON_DECISIONS else Presence.DEVICE if action in DEVICE_ACTIONS else None
    return endpoint(api, call, owner=True, action=action, presence=presence)


def _site_api_http() -> Any | None:
    try:
        return importlib.import_module("openajar.runner.siteapis.http")
    except ModuleNotFoundError as exc:
        if exc.name and "openajar.runner.siteapis.http".startswith(exc.name):
            return None
        raise


def site_api_routes(api: Api) -> list[Route]:
    """/v1/sites has its own auth (a site key, docs/SITE-APIS.md), so `endpoint` does not wrap it."""
    module = _site_api_http()
    return module.routes(api) if module is not None else []


def site_api_middleware() -> list[Middleware]:
    module = _site_api_http()
    return [Middleware(module.KeysStayOnV1)] if module is not None else []


def routes(api: Api) -> list[Route]:
    out: list[Route] = []
    for module, wanted in MOUNTS.items():
        for route in _module_routes(module, api.runtime):
            for method in sorted((route.methods or set()) - {"HEAD"}):
                target = wanted.get((route.path, method))
                if target is None:
                    continue
                path, action = target
                out.append(Route(path, _wrap(api, route.endpoint, method, action), methods=[method]))
    return out
