"""The engagement digest in the panel (agentos.engage.digest, docs/ENGAGEMENT.md "Daily digest").

  GET /api/engage/digest                  every surface the caller may see: its day (?day=YYYY-MM-DD,
                                          default yesterday in each surface's time zone)
  GET /api/engage/digest/{ws}/{name}      one surface's day (?day=), plus `week`: the seven days up
                                          to it, one line each, for the drilldown's day picker
  GET /api/engage/programs/{ws}/{name}    one surface's program: the approved one, or its newest draft,
                                          every version, its YAML and the caps it runs under
  POST /api/engage/programs/{id}/approve  approve a program version (body `{version}`, default newest)
  PUT /api/engage/caps/{ws}/{name}/{kind} set one daily cap (body `{value}`; null goes back to the ramp)
  POST /api/engage/holds/{ws}/{name}/clear  clear the holds of an account (body `{kind, id}`)
  GET /api/engage/items/{ws}/{name}      the account's items, oldest first (?status=)
  POST /api/engage/items/{id}/withdraw   take a waiting item back

Like a lane's meter and keepalive plan, these read: anyone signed in sees the surfaces their
grant or roles reach (a surface outside them is refused before any lookup, so nothing leaks),
and the owner sees every one. A node without the engage service answers `available: false`.

Draft text is untrusted (a provider wrote it) and can be sensitive: only the owner in person (a
signed-in panel session) gets a short preview of each draft awaiting approval. The node token
(the owner's bearer token, which agents and scripts hold), admin grants, members and grants get
the ids alone, as the MCP tool does. A notice that names an account the caller may not see says
"another account" instead. The records are built off the event loop (asyncio.to_thread).
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr
from starlette.requests import Request
from starlette.routing import Route

from agentos.api.access import Presence
from agentos.api.app import PERSON_ONLY, Api, access_of, endpoint, owner_in_person, principal_of, read_body
from agentos.api.errors import ApiError
from agentos.api.routes_core import scoped_lane_id
from agentos.core.errors import InvalidRequest, ServiceUnavailable
from agentos.core.models import Effect
from agentos.engage.caps import SITES, Kind

SERVICE = "engage"


def owner_reads_text(request: Request) -> bool:
    """The owner with a session (Presence.DEVICE or more: the panel, or the owner's own computer), who
    sees the text of their own engagement. A bearer token never is, the node token included: agents
    use it."""
    access = access_of(request)
    return bool(access.owner and access.member is None and access.presence >= Presence.DEVICE)


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt | None = None


class CapBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: StrictInt | None  # null: back to the ramp (the owner's choice, never above the hard ceiling)


class HoldsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: StrictStr | None = None
    id: StrictInt | None = None


def person_only(request: Request, action: str) -> None:
    """Refuse anyone but the owner in a panel session, traced. Tokens and members never pass."""
    if not owner_in_person(request):
        access_of(request).trace(action, status="denied", summary="needs the owner in person")
        raise ApiError("denied", PERSON_ONLY)


def item_view(item: dict[str, Any], *, preview: bool) -> dict[str, Any]:
    """An item as the account's list shows it. Its words are the owner's, so only the owner in person
    reads them (the same rule as a draft's preview in the digest)."""
    out = {key: value for key, value in item.items() if key != "text"}
    out["text"] = item.get("text") if preview else None
    return out


def program_view(svc: Any, surface: str, *, preview: bool) -> dict[str, Any]:
    """One surface's program: the approved version, else the newest draft; every version; its YAML;
    and each cap of its site with the owner's own value and today's plan, as the engine applies them."""
    from agentos.engage.governor import ceiling_table
    from agentos.engage.service import summarize, to_yaml

    versions = svc.store.programs(surface)
    active = svc.store.active(surface)
    shown = active or (versions[-1] if versions else None)
    site = shown.definition.site if shown is not None else None
    manual = svc.store.caps(surface)
    plan = None
    if shown is not None:
        today = svc.today(shown.definition)
        saved = svc.store.plans(surface, today, today)
        plan = saved[0] if saved else None
    caps = []
    for row in ceiling_table(site) if site in SITES else []:
        kind = Kind(row["kind"])
        caps.append({**row, "manual": manual.get(kind), "today": plan.caps.get(kind) if plan else None,
                     "hourly_today": plan.hourly.get(kind) if plan else None})
    return {
        "available": True, "surface": surface, "site": site,
        "state": ("approved" if shown is not None and shown.state.value == "approved" else
                  "draft" if shown is not None else None),
        "program": {"id": shown.id, "version": shown.version, "hash": shown.hash, "state": shown.state.value,
                    "approved_at": shown.approved_at.isoformat() if shown.approved_at else None,
                    "approved_by": shown.approved_by, "risk": shown.definition.risk.value,
                    "summary": summarize(shown.definition),
                    "yaml": to_yaml(shown.definition)} if shown is not None else None,
        "versions": [{"program": pv.id, "version": pv.version, "state": pv.state.value, "hash": pv.hash,
                      "created_at": pv.created_at.isoformat(), "created_by": (pv.created_by or {}).get("id"),
                      "approved_at": pv.approved_at.isoformat() if pv.approved_at else None,
                      "approved_by": pv.approved_by}
                     for pv in versions],
        "caps": caps,
        "items": svc.items.counts(surface),
        "may_change": preview,
    }


def routes(api: Api) -> list[Route]:
    from agentos.engage import digest

    def engage() -> Any | None:
        return api.optional(SERVICE)

    async def digest_all(request: Request) -> dict:
        day = digest.parse_day(request.query_params.get("day"))
        svc = engage()
        if svc is None:
            return {"available": False, "schema": digest.SCHEMA, "sessions_on": None, "surfaces": []}
        access = access_of(request)
        out = await asyncio.to_thread(digest.digest, svc, None, day, can_see=access.lane_allowed,
                                      include_text=owner_reads_text(request))
        return {"available": True, **out}

    async def digest_one(request: Request) -> dict:
        surface = scoped_lane_id(request, "engage.digest")
        day = digest.parse_day(request.query_params.get("day"))
        access = access_of(request)
        await access.authorize(Effect.READ, action="engage.digest", lane_id=surface,
                               summary=f"Engagement digest of {surface}")
        svc = engage()
        if svc is None:
            return {"available": False, "surface": surface}
        preview = owner_reads_text(request)

        def build() -> dict:
            rec = digest.record_for(svc, surface, day, include_text=preview, can_see=access.lane_allowed)
            # the picker always shows the surface's last seven days (to its yesterday), whatever day is open
            return {"available": True, "record": rec.public(), "week": digest.week(svc, surface)}

        return await asyncio.to_thread(build)

    def ready() -> Any:
        svc = engage()
        if svc is None:
            raise ServiceUnavailable("this node runs no engagement service")
        return svc

    async def program_get(request: Request) -> dict:
        surface = scoped_lane_id(request, "engage.program")
        svc = engage()
        if svc is None:
            return {"available": False, "surface": surface}
        preview = owner_reads_text(request)
        return await asyncio.to_thread(program_view, svc, surface, preview=preview)

    async def program_approve(request: Request) -> dict:
        person_only(request, "engage.program.approve")
        body = await read_body(request, ApproveBody)
        svc = ready()
        program_id = request.path_params["program"]
        pv = await asyncio.to_thread(svc.approve, program_id, body.version, by=principal_of(request))
        surface = pv.definition.surface
        access_of(request).trace("engage.program.approve", lane=surface,
                                 summary=f"approved {program_id} version {pv.version}")
        return await asyncio.to_thread(program_view, svc, surface, preview=True)

    async def cap_set(request: Request) -> dict:
        person_only(request, "engage.cap.set")
        surface = f"{request.path_params['ws']}/{request.path_params['name']}"
        body = await read_body(request, CapBody)
        kind = request.path_params["kind"]
        try:
            Kind(kind)
        except ValueError:
            raise InvalidRequest(f"{kind} is not a kind of action an account can take") from None
        svc = ready()
        out = await asyncio.to_thread(svc.set_cap, surface, Kind(kind), body.value, by=principal_of(request))
        access_of(request).trace("engage.cap.set", lane=surface,
                                 summary=f"{kind} cap {'back to the ramp' if body.value is None else body.value}")
        return out

    async def holds_clear(request: Request) -> dict:
        person_only(request, "engage.hold.clear")
        surface = f"{request.path_params['ws']}/{request.path_params['name']}"
        body = await read_body(request, HoldsBody)
        svc = ready()
        if svc.executor is None:
            raise ServiceUnavailable("this node runs no engagement sessions, so it holds nothing to clear")
        n = await asyncio.to_thread(svc.executor.clear_hold, surface, by=principal_of(request), kind=body.kind,
                                    hold_id=body.id)
        access_of(request).trace("engage.hold.clear", lane=surface, summary=f"{n} hold(s) cleared")
        return {"surface": surface, "cleared": n}

    async def items_get(request: Request) -> dict:
        surface = scoped_lane_id(request, "engage.items")
        svc = engage()
        if svc is None:
            return {"available": False, "surface": surface, "items": []}
        status = request.query_params.get("status") or None
        rows = await asyncio.to_thread(svc.items.list, surface, status=status)
        preview = owner_reads_text(request)
        return {"available": True, "surface": surface, "items": [item_view(r, preview=preview) for r in rows]}

    async def item_withdraw(request: Request) -> dict:
        person_only(request, "engage.item.withdraw")
        svc = ready()
        item_id = request.path_params["item"]
        out = await asyncio.to_thread(svc.items.withdraw, item_id, by=principal_of(request))
        access_of(request).trace("engage.item.withdraw", lane=out.get("surface"), summary=f"withdrew {item_id}")
        return item_view(out, preview=True)

    return [
        Route("/api/engage/digest", endpoint(api, digest_all, action="engage.digest"), methods=["GET"]),
        Route("/api/engage/digest/{ws}/{name}", endpoint(api, digest_one, action="engage.digest"), methods=["GET"]),
        Route("/api/engage/programs/{ws}/{name}", endpoint(api, program_get, action="engage.program"),
              methods=["GET"]),
        Route("/api/engage/programs/{program}/approve", endpoint(api, program_approve, owner=True,
                                                                action="engage.program.approve"), methods=["POST"]),
        Route("/api/engage/caps/{ws}/{name}/{kind}", endpoint(api, cap_set, owner=True, action="engage.cap.set"),
              methods=["PUT"]),
        Route("/api/engage/holds/{ws}/{name}/clear", endpoint(api, holds_clear, owner=True,
                                                              action="engage.hold.clear"), methods=["POST"]),
        Route("/api/engage/items/{ws}/{name}", endpoint(api, items_get, action="engage.items"), methods=["GET"]),
        Route("/api/engage/items/{item}/withdraw", endpoint(api, item_withdraw, owner=True,
                                                            action="engage.item.withdraw"), methods=["POST"]),
    ]
