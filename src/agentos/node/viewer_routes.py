"""The owner's lane control API, mounted under /api behind the bearer (or panel session) check:
control, view links, site lists, the gate log and effect rule health (docs/API.md, "Lane control").
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agentos.core.errors import AgentosError, InvalidRequest
from agentos.core.presence import NEEDS_A_PERSON
from agentos.node.viewer_actions import ACTIONS, apply_action, change_sites, handback_warning, person_needed
from agentos.node.viewer_app import VIEWER_BY, ViewerApp, _agentos_error, _error, _owner_in_person, _the_owner
from agentos.node.viewer_state import lane_dict
from agentos.node.viewer_tokens import ViewMode, ViewTokens, make_view_link


def control_api_routes(lanes: Any, tokens: ViewTokens, *, by: str = VIEWER_BY, base: str = "",
                       traces: Any = None) -> list:
    """The owner's control API. Mount under /api behind the bearer or panel session check."""

    async def control(request: Request) -> Response:
        try:
            lane_id = ViewerApp._lane_id(request.path_params)
            body = await request.json()
        except ValueError:
            return _error(400, "invalid", 'send JSON like {"action": "take"}')
        action = body.get("action") if isinstance(body, dict) else None
        note = body.get("note") if isinstance(body, dict) else None
        done = body.get("done") if isinstance(body, dict) else None
        try:
            if action in ACTIONS and person_needed(lanes.get(lane_id), action) \
                    and not _owner_in_person(request, lanes):
                auth = getattr(request.state, "auth", None)
                if auth is not None:
                    auth.access.trace("lane.control", status="denied", lane=lane_id, summary=f"{action}: not in person")
                return _error(403, "presence_required", NEEDS_A_PERSON)
            info, changed = await apply_action(lanes, lane_id, action, by=by, note=note, done=done)
        except AgentosError as e:
            return _agentos_error(e)
        out = {**lane_dict(info), "changed": changed}
        if (warning := handback_warning(action, info)) is not None:
            out["warning"] = warning
        return JSONResponse(out)

    async def view_link(request: Request) -> Response:
        try:
            lane_id = ViewerApp._lane_id(request.path_params)
            raw = await request.body()
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return _error(400, "invalid", 'send JSON like {"mode": "control"}')
        if not isinstance(body, dict):
            return _error(400, "invalid", 'send JSON like {"mode": "control"}')
        mode = body.get("mode", "view")
        ttl = body.get("ttl_s")
        if mode not in (ViewMode.VIEW.value, ViewMode.CONTROL.value):
            return _error(400, "invalid", "mode must be view or control")
        if ttl is not None and (not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0):
            return _error(400, "invalid", "ttl_s must be a positive number of seconds")
        try:
            lanes.get(lane_id)
        except AgentosError as e:
            return _agentos_error(e)
        return JSONResponse(make_view_link(tokens, lane_id, mode, ttl, base=base,
                                           owner=_owner_in_person(request, lanes)))

    out = [
        Route("/lanes/{workspace}/{name}/control", control, methods=["POST"]),
        Route("/lanes/{workspace}/{name}/view-link", view_link, methods=["POST"]),
    ]
    return (out + sites_api_routes(lanes, by=by) + gate_log_api_routes(lanes, by=by, traces=traces)
            + rule_health_api_routes(lanes))


def gate_log_api_routes(lanes: Any, *, by: str = VIEWER_BY, traces: Any = None) -> list:
    """GET and POST /api/lanes/{ws}/{name}/gate-log (agentos.node.gateshapes): the shapes the commit
    gate classified without a rule in this lane, and the owner's promotions of one to the node's
    read allowlist or to a named commit rule (or their removal). The owner reads it (`_the_owner`); a
    change needs the owner in person (`_owner_in_person`);
    a refusal and every change are traced, a change as `gate_shape` in the action log."""
    from agentos.node import gateshapes

    def refuse(request: Request, lane_id: str) -> Response:
        auth = getattr(request.state, "auth", None)
        if auth is not None:
            auth.access.trace("lane.gate_log", status="denied", lane=lane_id, summary="not the owner in person")
        access = getattr(auth, "access", None)
        owners = access is None or (bool(getattr(access, "owner", False)) and getattr(access, "member", None) is None)
        code = "presence_required" if owners and request.method == "POST" else "denied"  # the owner, not in person
        return _error(403, code, "Only the owner reads the gate log, and only the owner in person (the panel, "
                                     "or `agentos lane gate-log` on the node) changes what the gate allows. A "
                                     "member, a grant, the node token or a saved device credential cannot.")

    def home_and_dir(lane_id: str) -> tuple[Path, Path]:
        lanes.get(lane_id)  # the lane exists
        config = lanes.config
        return Path(config.home), config.lane_dir(lane_id)

    async def gate_log(request: Request) -> Response:
        try:
            lane_id = ViewerApp._lane_id(request.path_params)
        except AgentosError as e:
            return _agentos_error(e)
        if not (_owner_in_person(request, lanes) if request.method == "POST" else _the_owner(request)):
            return refuse(request, lane_id)
        try:
            home, lane_dir = home_and_dir(lane_id)
            if request.method == "GET":
                since = gateshapes.since_of(request.query_params.get("since"))
                promoted = gateshapes.load(gateshapes.store_path(home))
                rows = gateshapes.trace_rows(lane_dir)
                shapes = gateshapes.log(rows, since=since, promoted=promoted,
                                        observed=request.query_params.get("observed") in ("1", "true"))
                return JSONResponse({"lane": lane_id, "shapes": shapes, "promoted": promoted,
                                     "identities": gateshapes.identities(rows, since=since)},
                                    headers={"Cache-Control": "no-store"})
            try:
                body = await request.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                raise InvalidRequest('send JSON like {"allow_read": "sh_..."} or {"as_commit": "post.media", '
                                     '"shape": "sh_..."}')
            if isinstance(body.get("remove"), str):
                entry = gateshapes.remove(home, body["remove"])
                removed = True
            elif isinstance(body.get("allow_read"), str):
                entry = gateshapes.promote(home, lane_dir, body["allow_read"], as_="read", lane=lane_id, by=by,
                                           base=_base_rules(lanes, lane_id), all_lanes=body.get("all_lanes") is True)
                removed = False
            elif isinstance(body.get("as_commit"), str) and isinstance(body.get("shape"), str):
                entry = gateshapes.promote(home, lane_dir, body["shape"], as_="commit", name=body["as_commit"],
                                           lane=lane_id, by=by, base=_base_rules(lanes, lane_id),
                                           all_lanes=body.get("all_lanes") is True)
                removed = False
            else:
                raise InvalidRequest('send {"allow_read": id}, {"as_commit": name, "shape": id} or {"remove": id}')
        except AgentosError as e:
            return _agentos_error(e)
        if traces is not None:
            gateshapes.audit(traces, entry, lane=lane_id, by=by, removed=removed)
        else:
            auth = getattr(request.state, "auth", None)
            if auth is not None:
                auth.access.trace("lane.gate_log", lane=lane_id, summary=f"gate shape {entry['id']} "
                                  f"{'removed' if removed else 'promoted as ' + str(entry.get('rule') or 'read')}")
        return JSONResponse({"lane": lane_id, "removed" if removed else "promoted": entry},
                            headers={"Cache-Control": "no-store"})

    return [Route("/lanes/{workspace}/{name}/gate-log", gate_log, methods=["GET", "POST"])]


def rule_health_api_routes(lanes: Any) -> list:
    """GET /api/rules/health?lane=&stale_days= (agentos.node.rulehealth): when each commit and write
    rule last matched, the shapes no rule names per site, and what looks like drift. The owner only,
    like the gate log (names and shapes only, but they say what each account does)."""
    from agentos.node import rulehealth

    async def health(request: Request) -> Response:
        if not _the_owner(request):  # a read, like the gate log's GET: the owner, not in person
            auth = getattr(request.state, "auth", None)
            if auth is not None:
                auth.access.trace("rules.health", status="denied", summary="not the owner")
            return _error(403, "denied", "Only the owner reads effect rule health (the node token or the owner's "
                                         "panel session). A member or a grant cannot.")
        lane = request.query_params.get("lane") or None
        try:
            stale = float(request.query_params.get("stale_days") or rulehealth.STALE_DAYS)
        except ValueError:
            return _error(400, "invalid", "stale_days is a number of days")
        try:
            if lane is not None:
                lanes.get(lane)  # the lane exists
            specs = [info.spec for info in lanes.list()]
            done = await asyncio.to_thread(rulehealth.for_node, Path(lanes.config.home), specs, lane=lane,
                                           stale_days=stale)
        except AgentosError as e:
            return _agentos_error(e)
        return JSONResponse(done, headers={"Cache-Control": "no-store"})

    return [Route("/rules/health", health, methods=["GET"])]


def _base_rules(lanes: Any, lane_id: str) -> Any:
    """The built-in rules the lane's guard classifies by (with the dev site's on the dev node)."""
    from agentos.node import egress

    lane = getattr(lanes, "_lanes", {}).get(lane_id)
    guard = getattr(getattr(lane, "session", None), "guard", None)
    return getattr(guard, "_base_rules", None) or egress.EFFECT_RULES


def sites_api_routes(lanes: Any, *, by: str = VIEWER_BY) -> list:
    """POST /api/lanes/{ws}/{name}/sites: the owner changes a lane's site list on the daemon's own
    lane manager, so a warm lane's guard has it at once (`agentos lane sites` calls it while agentosd
    runs). The owner in person only (Presence.PERSON: the panel, or the node's CLI with its own session):
    an agent's grant, the node token and the device credential never widen a lane."""

    async def sites(request: Request) -> Response:
        try:
            lane_id = ViewerApp._lane_id(request.path_params)
            body = await request.json()
        except ValueError:
            return _error(400, "invalid", 'send JSON like {"op": "add", "sites": ["reddit.com"]}')
        if not _owner_in_person(request, lanes):
            auth = getattr(request.state, "auth", None)
            if auth is not None:
                auth.access.trace("lane.sites", status="denied", lane=lane_id, summary="a grant, not the owner")
            return _error(403, "presence_required", "Only the owner in person changes a lane's site list (the "
                                         "panel, or `agentos lane sites` on the node). A grant, the node token or "
                                         "a saved device credential cannot, because an agent may hold them.")
        try:
            return JSONResponse(await change_sites(lanes, lane_id, body, by=by), headers={"Cache-Control": "no-store"})
        except AgentosError as e:
            return _agentos_error(e)

    return [Route("/lanes/{workspace}/{name}/sites", sites, methods=["POST"])]


def routes(runtime: Any) -> list:
    """The control API for the panel to mount behind its own auth (agentos.api.mounts): take,
    hand back, stop and deny, signed view links, and the lane's site list. Paths are relative to /api."""
    lanes = runtime.get("lanes")
    config = runtime.get("config")
    if lanes is None or config is None:
        return []
    if not callable(getattr(lanes, "take_control", None)):  # no take over on this runtime: no control routes
        return sites_api_routes(lanes) if callable(getattr(lanes, "set_sites", None)) else []
    tokens = runtime.get("viewer_links")
    if tokens is None:
        tokens = ViewTokens.for_config(config)
        runtime.register("viewer_links", tokens)
    return control_api_routes(lanes, tokens, traces=runtime.get("traces"))
