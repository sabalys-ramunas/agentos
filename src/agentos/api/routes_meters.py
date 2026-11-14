"""Meters, a lane's claim and queue, and its taint in the panel (agentos.policy.meters, docs/API.md
"Meters").

  GET  /api/meters                       today's use against the limits: the owner sees every
                                         live grant and lane; a member the lanes they can see and
                                         their own agents; a grant itself and its lanes
  GET  /api/lanes/{ws}/{name}/meter      one lane's meter (anyone who can see the lane)
  PUT  /api/lanes/{ws}/{name}/meter      {limits: {...} | null}: the owner sets or removes the
                                         lane's limits (null: policy.toml applies again)
  GET  /api/lanes/{ws}/{name}/pace       the lane's posting pace (agentos.policy.pace, AUD-13): its
                                         waking hours, gap and posts a day, whose clock it reads
                                         (`zone.source`: lane, pin, workspace or node), today's posts
                                         and when it may post next (anyone who can see the lane)
  PUT  /api/lanes/{ws}/{name}/pace       {pace: {...} | null} or {preset: true}: the owner sets,
                                         removes or applies the recommended pace
  GET  /api/pace/workspaces/{ws}         the default timezone of a workspace's lanes with no pin
  PUT  /api/pace/workspaces/{ws}         {timezone: "America/New_York" | null}: the owner sets it
  GET  /api/lanes/{ws}/{name}/queue      who holds the lane, who is on it, who waits and since when
                                         (`yours`: the caller's own claim or place)
  GET  /api/lanes/{ws}/{name}/taint      the grant families that reach the lane and what untrusted
                                         text they read: every family for the owner, with grant ids
                                         and labels; for anyone else only their own (a grant's own
                                         family, a member's own agents), without them
  POST /api/grants/{id}/taint/clear      {confirm: "clear"}: the owner in person starts the family
                                         clean (as `agentos grants taint <id> --clear`)

Clearing taint is a security decision (what an agent read stops making its risky steps wait for a
person), so it always needs a fresh in-person step, whether or not the node has members: the
owner's signed-in panel session (never a token, never a device credential from `agentos
connect`), the typed confirmation, and, when the owner has a passkey, a passkey check within
`step_up_window`; without one, a panel sign-in with a new one-time code within that window (the
code is minted on the node, so it proves the owner is at the node).
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.routing import Route

from agentos.api.access import Presence
from agentos.api.app import Api, access_of, endpoint, read_body
from agentos.api.errors import ApiError
from agentos.api.jsonable import maybe_await
from agentos.api.members import OWNER_ID
from agentos.api.routes_core import scoped_lane_id
from agentos.api.routes_resources import path_id
from agentos.policy.errors import Denied
from agentos.policy.meters import Limits
from agentos.policy.pace import PRESET, Pace
from agentos.policy.util import iso

CLEAR_IN_PERSON = ("Only the owner clears taint, signed in to the panel (or with `agentos grants taint <grant> "
                   "--clear` on the node). A token cannot clear it.")
CLEAR_NOT_DEVICE = ("A device credential cannot clear taint. Sign in to the panel in a browser (or run `agentos "
                    "grants taint <grant> --clear` on the node).")
CLEAR_SIGN_IN_AGAIN = ("Clearing taint needs a fresh sign-in: run `agentos panel login` on the node, sign in with the "
                       "new code, then clear it within {minutes} minutes. A passkey makes this a single check.")
CLEAR_CONFIRM = 'Type "clear" to confirm.'
CLEAR_WORD = "clear"
NO_METERS = "Meters are not supported by this node yet."


class LimitsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limits: Limits | None


class PaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pace: Pace | None = None
    preset: bool = False


class ZoneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str | None = None


class ClearIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str | None = None


def routes(api: Api) -> list[Route]:
    def policy() -> Any:
        svc = api.runtime.get("policy")
        if svc is None or not hasattr(svc, "meters"):
            raise ApiError("unavailable", NO_METERS)
        return svc

    async def lane_ids(request: Request) -> list[str]:
        lanes = api.optional("lanes")
        if lanes is None:
            return []
        access = access_of(request)
        ids = [i.spec.id for i in await maybe_await(lanes.list())]
        return sorted(lane for lane in ids if access.lane_allowed(lane))

    def grants_for(request: Request, svc: Any) -> list[Any]:
        """The grants whose meters the caller sees: every live agent grant for the owner, a member's
        own agents, a grant's own."""
        access = access_of(request)
        if access.member is not None:
            rows = [r for r in svc.list_grants() if r["subject"].get("owner") == access.member.id]
        elif access.owner:
            rows = svc.list_grants()
        else:
            rows = [{"id": access.grant_id, "subject": {}}] if access.grant_id else []
        out = []
        for r in rows:
            if r["subject"].get("kind") == "human":
                continue
            try:
                out.append(svc.grant_by_id(r["id"]))
            except Denied:
                continue
        return out

    async def meters(request: Request) -> dict:
        svc = policy()
        m, now = svc.meters, svc.clock()
        return {"timezone": m.timezone, "day": m.day(now).isoformat(),
                "resets_at": iso(m.day_reset(now)),
                "warn_at": list(m.cfg.warn_at),
                "grants": [svc.grant_meter(g) for g in grants_for(request, svc)],
                "lanes": [svc.lane_meter(lane) for lane in await lane_ids(request)]}

    async def lane_meter(request: Request) -> dict:
        return policy().lane_meter(scoped_lane_id(request, "lane.meter"))

    async def lane_limits(request: Request) -> dict:
        lane_id = scoped_lane_id(request, "lane.limits")
        body = await read_body(request, LimitsIn)
        out = policy().set_lane_limits(lane_id, body.limits, by="owner")
        words = ", ".join(f"{k} {v}" for k, v in body.limits.given().items()) if body.limits else "removed"
        access_of(request).trace("lane.limits", lane=lane_id, summary=f"Limits for {lane_id}: {words or 'none'}")
        api.publish("meter", {"scope": "lane", "id": lane_id, "kind": "limits"})
        return out

    async def lane_pace(request: Request) -> dict:
        return policy().lane_pace(scoped_lane_id(request, "lane.pace"))

    async def set_lane_pace(request: Request) -> dict:
        lane_id = scoped_lane_id(request, "lane.pace")
        body = await read_body(request, PaceIn)
        if body.preset and body.pace is not None:
            raise ApiError("invalid", "Send either a pace or preset: true, not both.")
        pace = PRESET if body.preset else body.pace
        out = policy().set_lane_pace(lane_id, pace, by="owner")
        given = pace.given() if pace is not None else {}
        words = ", ".join(f"{k} {v}" for k, v in given.items()) or "removed"
        access_of(request).trace("lane.pace", lane=lane_id, summary=f"Posting pace for {lane_id}: {words}")
        api.publish("meter", {"scope": "lane", "id": lane_id, "kind": "pace"})
        return out

    def workspace_of(request: Request) -> str:
        from agentos.workspaces.models import WORKSPACE_ID_RE

        ws = str(request.path_params.get("ws") or "")
        if not WORKSPACE_ID_RE.fullmatch(ws):
            raise ApiError("invalid", f"{ws!r} is not a workspace id like biz-a.")
        return ws

    async def workspace_zone(request: Request) -> dict:
        ws = workspace_of(request)
        return {"workspace": ws, "timezone": policy().pace.workspace_zone(ws)}

    async def set_workspace_zone(request: Request) -> dict:
        ws = workspace_of(request)
        body = await read_body(request, ZoneIn)
        try:
            out = policy().set_workspace_zone(ws, body.timezone, by="owner")
        except ValueError as e:
            raise ApiError("invalid", str(e).split("\n")[-1].strip()) from None
        access_of(request).trace("workspace.pace", summary=f"Default timezone for {ws}: {body.timezone or 'removed'}")
        api.publish("meter", {"scope": "workspace", "id": ws, "kind": "pace"})
        return out

    async def lane_queue(request: Request) -> dict:
        lane_id = scoped_lane_id(request, "lane.queue")
        lanes = api.svc("lanes")
        info = getattr(lanes, "queue_info", None)
        if callable(info):
            return {"lane": lane_id, **info(lane_id, viewer=viewer_of(request))}
        one = await maybe_await(lanes.get(lane_id))  # a lane manager without queues (the fakes)
        return {"lane": lane_id, "claim": getattr(one, "claim", None), "queue_length": getattr(one, "queue_length", 0),
                "queue": [], "inside": [], "call_estimate_s": None}

    def viewer_of(request: Request) -> Any:
        """The caller as the lane's queue knows callers (agentos.node.tools.caller), so `yours` is set."""
        ctx = getattr(access_of(request), "ctx", None)
        if ctx is None:
            return None
        from agentos.node.tools import caller

        return caller(ctx)

    def own_roots(request: Request, svc: Any) -> set[str] | None:
        """The grant families whose taint the caller sees: None (all) for the owner; a member's own
        agents; a grant its own family."""
        access = access_of(request)
        if access.owner and access.member is None:
            return None
        rows = svc.list_grants()
        if access.member is not None:
            return {r["root_id"] for r in rows if r["subject"].get("owner") == access.member.id}
        return {r["root_id"] for r in rows if r["id"] == access.grant_id}

    async def lane_taint(request: Request) -> dict:
        lane_id = scoped_lane_id(request, "lane.taint")
        svc = policy()
        out = svc.lane_taint(lane_id, roots=own_roots(request, svc))
        if not access_of(request).owner:  # which grants: the owner's to know
            out["families"] = [{k: v for k, v in f.items() if k not in ("grant_id", "grants")} for f in out["families"]]
        return out

    def owner_has_passkey() -> bool:
        try:
            return bool(api.members.store.credentials(OWNER_ID))
        except Exception:  # noqa: BLE001  (cannot tell: ask for the passkey, toward safe)
            return True

    async def taint_clear(request: Request) -> dict:
        access = access_of(request)
        auth = request.state.auth
        if auth.via != "cookie" or not access.owner or access.member is not None or auth.session is None:
            access.trace("taint.clear", status="denied", summary="clearing taint needs the owner in person")
            raise ApiError("denied", CLEAR_IN_PERSON)
        session, window = auth.session, api.settings.step_up_window
        if session.via == "device":
            access.trace("taint.clear", status="denied", summary="a device credential never clears taint")
            raise ApiError("denied", CLEAR_NOT_DEVICE)
        body = await read_body(request, ClearIn)
        if (body.confirm or "").strip().lower() != CLEAR_WORD:
            raise ApiError("invalid", CLEAR_CONFIRM)
        now = time.time()
        if owner_has_passkey():
            verified = session.last_verified_at
            if verified is None or now - verified > window:
                access.trace("taint.clear", status="denied", summary="clearing taint needs a fresh passkey check")
                raise ApiError("step_up_required", "Confirm with your passkey first: clearing taint needs a fresh "
                                                   "passkey check.")
        elif session.via != "code" or now - session.created_at > window:
            access.trace("taint.clear", status="denied", summary="clearing taint needs a fresh sign-in")
            raise ApiError("denied", CLEAR_SIGN_IN_AGAIN.format(minutes=max(1, window // 60)))
        gid = path_id(request)
        result = policy().clear_taint(gid, by="owner")  # NotFound for a grant never issued here
        access.trace("taint.clear", summary=f"Cleared the taint of grant {gid}'s family "
                                            f"({result['origins_cleared']} origins)")
        api.publish("trace", {})
        return result

    PERSON = Presence.PERSON  # a lane's limits and pace are protections: a token never lowers them
    return [
        Route("/api/meters", endpoint(api, meters), methods=["GET"]),
        Route("/api/lanes/{ws}/{name}/meter", endpoint(api, lane_meter), methods=["GET"]),
        Route("/api/lanes/{ws}/{name}/meter", endpoint(api, lane_limits, owner=True, action="lane.limits",
                                                             presence=PERSON),
              methods=["PUT"]),
        Route("/api/lanes/{ws}/{name}/pace", endpoint(api, lane_pace), methods=["GET"]),
        Route("/api/lanes/{ws}/{name}/pace", endpoint(api, set_lane_pace, owner=True, action="lane.pace",
                                                            presence=PERSON),
              methods=["PUT"]),
        Route("/api/pace/workspaces/{ws}", endpoint(api, workspace_zone, owner=True), methods=["GET"]),
        Route("/api/pace/workspaces/{ws}", endpoint(api, set_workspace_zone, owner=True, action="workspace.pace"),
              methods=["PUT"]),
        Route("/api/lanes/{ws}/{name}/queue", endpoint(api, lane_queue), methods=["GET"]),
        Route("/api/lanes/{ws}/{name}/taint", endpoint(api, lane_taint), methods=["GET"]),
        Route("/api/grants/{id}/taint/clear", endpoint(api, taint_clear, action="taint.clear"), methods=["POST"]),
    ]
