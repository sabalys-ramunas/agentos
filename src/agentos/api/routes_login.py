"""Assisted login routes (agentos.node.login_assist): the owner logs a lane in through a form.

    GET  /api/lanes/{ws}/{name}/login               ?after=<version>&wait=<s>: the login, or {state: none}
    POST /api/lanes/{ws}/{name}/login               {"url"?}: start one (or get the one running)
    POST /api/lanes/{ws}/{name}/login/form          {"url"?}: the same, and a form on this computer for it
    POST /api/lanes/{ws}/{name}/login/vault         sign in from the saved login, with no form (the vault decides)
    POST /api/lanes/{ws}/{name}/login/step          {"step", "values"?: {name: text}, "choice"?}
    POST /api/lanes/{ws}/{name}/login/refresh       read the page again
    POST /api/lanes/{ws}/{name}/login/take          the owner goes to the live view
    POST /api/lanes/{ws}/{name}/login/resume        back to the form
    POST /api/lanes/{ws}/{name}/login/cancel        {"done"?: bool}
    POST /api/lanes/{ws}/{name}/login/save          {"name"?}: save what was typed to the vault
    GET  /api/lanes/{ws}/{name}/login/screenshot    the step's screenshot, password and code fields painted over

Who: the owner in person (the panel session or the node token, never a grant, not even an admin
grant), or a member whose grant may take control of the lane (not watch only). Saving also needs the
member's `vault` role. Values are read from the body by hand into Secrets, so no validation error can
echo one, and they go straight to the service. Nothing here traces, logs or returns a value.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agentos.api.access import Presence
from agentos.api.app import Api, access_of, endpoint, read_json_object
from agentos.api.errors import ApiError
from agentos.api.routes_core import OUT_OF_SCOPE, lane_id_from
from agentos.core.errors import InvalidRequest
from agentos.core.presence import NEEDS_A_PERSON
from agentos.node.login_assist import read_values

NOT_YOURS = ("Only the owner logs a lane in (the panel or the node's CLI), or a member whose role may take "
             "control of it. An agent's grant never can: agents ask with lane_login_request.")
MAX_WAIT = 30


def _in_person(request: Request) -> bool:
    """The owner in person (Presence.PERSON: a panel session, or the node's CLI with its one-time code),
    never the node token or the device credential alone, and never a grant. A login takes a held lane
    and, signed in, hands it back, which clears the hold: that is a person's (docs/THREAT-MODEL.md)."""
    return access_of(request).presence >= Presence.PERSON


def login_lane(request: Request, action: str) -> tuple[str, str]:
    """(lane id, who) for a login route, or 403 (traced). `who` is "owner" or the member's handle."""
    lane_id = lane_id_from(request)
    access = access_of(request)
    if not access.lane_allowed(lane_id):
        access.trace(action, status="denied", lane=lane_id, summary="out of scope")
        raise ApiError("denied", OUT_OF_SCOPE)
    member = access.member
    if member is not None:
        terms = access.watch_terms(lane_id)  # type: ignore[attr-defined]
        if terms is None or terms.get("watch_only"):
            access.trace(action, status="denied", lane=lane_id, summary="role: no control of the lane")
            raise ApiError("denied", NOT_YOURS)
        return lane_id, member.handle
    if not access.owner or access.presence < Presence.NODE_TOKEN:  # a grant, even an admin grant
        access.trace(action, status="denied", lane=lane_id, summary="a grant, not the owner")
        raise ApiError("denied", NOT_YOURS)
    if not _in_person(request):
        access.trace(action, status="denied", lane=lane_id, summary="the owner's token, not in person")
        raise ApiError("presence_required", NEEDS_A_PERSON)
    return lane_id, "owner"


def routes(api: Api) -> list[Route]:
    def svc() -> Any:
        from agentos.node.login_assist import service_for

        return service_for(api.runtime)

    def none(lane_id: str) -> dict:
        return {"lane": lane_id, "state": "none"}

    async def get(request: Request) -> dict:
        lane_id, _ = login_lane(request, "lane.login.get")
        service = svc()
        after = request.query_params.get("after")
        wait = request.query_params.get("wait")
        if after is not None and wait is not None:
            try:
                version, seconds = int(after), max(0, min(MAX_WAIT, int(wait)))
            except ValueError:
                raise InvalidRequest("after and wait are whole numbers.") from None
            return await service.wait_change(lane_id, version, seconds) or none(lane_id)
        return service.get(lane_id) or none(lane_id)

    async def start(request: Request) -> dict:
        lane_id, who = login_lane(request, "lane.login.start")
        body = await read_json_object(request)
        url = body.get("url")
        if url is not None and (not isinstance(url, str) or len(url) > 2048):
            raise InvalidRequest("url is the page to open, as text.")
        access_of(request).trace("lane.login.start", lane=lane_id, summary=f"Start an assisted login on {lane_id}")
        out = await svc().start(lane_id, by=who, url=url)
        return out

    async def form(request: Request) -> dict:
        """Start the login, and a trip for the page that types the owner's values (agentos.node.login_form).
        The page is the node's own: the app opens its `form_url` and never sees a value."""
        from agentos.node.forms import trips_of

        lane_id, who = login_lane(request, "lane.login.form")
        body = await read_json_object(request)
        url = body.get("url")
        if url is not None and (not isinstance(url, str) or len(url) > 2048):
            raise InvalidRequest("url is the page to open, as text.")
        access_of(request).trace("lane.login.form", lane=lane_id, summary=f"Open the sign-in form for {lane_id}")
        login = await svc().start(lane_id, by=who, url=url)
        trip = trips_of(api.runtime).start("login", who, {}, subject=lane_id)
        return {**trip.public(), "login": login}

    async def vault_sign_in(request: Request) -> dict:
        """The saved login of this account signs it in. No question is asked: the vault's entry is the owner's
        permission. A member may do it only where their role may use the vault (an admin of the workspace)."""
        lane_id, who = login_lane(request, "lane.login.vault")
        access = access_of(request)
        if access.member is not None and not access.can("vault", lane_id.split("/", 1)[0]):
            access.trace("lane.login.vault", status="denied", lane=lane_id, summary="role: no vault")
            raise ApiError("denied", "Signing in from a saved login needs the owner or an admin of this workspace.")
        access.trace("lane.login.vault", lane=lane_id, summary=f"Sign in to {lane_id} from its saved login")

        def sink(kind: str, fields: dict) -> None:
            access.trace(kind, lane=lane_id, summary=kind.replace(".", " "))

        return await svc().vault_sign_in(lane_id, by=who, trace=sink)

    async def step(request: Request) -> dict:
        lane_id, who = login_lane(request, "lane.login.step")
        body = await read_json_object(request)
        step_id = body.get("step")
        choice = body.get("choice")
        values = read_values(body.pop("values", None))
        body.clear()
        if not isinstance(step_id, str) or not step_id:
            raise InvalidRequest('Send the step id the form showed, like {"step": "s2", "values": {...}}.')
        if choice is not None and not isinstance(choice, str):
            raise InvalidRequest("choice is the id of one of the step's buttons.")
        names = ", ".join(sorted(values)) or f"choice {choice}"
        access_of(request).trace("lane.login.step", lane=lane_id,
                                 summary=f"Assisted login on {lane_id}: step {step_id} ({names})")
        try:
            return await svc().submit(lane_id, step=step_id, values=values, choice=choice, by=who)
        finally:
            values.clear()

    def simple(action: str, call: str, summary: str, **extra: Any) -> Any:
        async def handler(request: Request) -> dict:
            lane_id, who = login_lane(request, f"lane.login.{action}")
            kwargs = dict(extra)
            if action == "cancel":
                body = await read_json_object(request)
                kwargs["done"] = body.get("done") is True
            access_of(request).trace(f"lane.login.{action}", lane=lane_id, summary=summary.format(lane=lane_id))
            return await getattr(svc(), call)(lane_id, by=who, **kwargs)

        handler.__name__ = f"login_{action}"
        return handler

    async def save(request: Request) -> dict:
        lane_id, who = login_lane(request, "lane.login.save")
        access = access_of(request)
        if access.member is not None and not access.can("vault", lane_id.split("/", 1)[0]):
            access.trace("lane.login.save", status="denied", lane=lane_id, summary="role: no vault")
            raise ApiError("denied", "Saving to the vault needs the owner or an admin of this workspace.")
        body = await read_json_object(request)
        name = body.get("name")
        if name is not None and (not isinstance(name, str) or len(name) > 64):
            raise InvalidRequest("name is the vault entry's name, at most 64 characters.")
        result = await svc().save(lane_id, by=who, name=name)
        access.trace("lane.login.save", lane=lane_id, summary=f"Save the login of {lane_id} as vault entry "
                                                              f"{result.get('saved')}")
        return result

    async def screenshot(request: Request) -> Response:
        lane_id, _ = login_lane(request, "lane.login.screenshot")
        png = svc().screenshot(lane_id)
        if png is None:
            raise ApiError("not_found", "There is no screenshot of this step.")
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})

    base = "/api/lanes/{ws}/{name}/login"
    e = lambda fn: endpoint(api, fn)
    return [
        Route(base, e(get), methods=["GET"]),
        Route(base, e(start), methods=["POST"]),
        Route(f"{base}/form", e(form), methods=["POST"]),
        Route(f"{base}/vault", e(vault_sign_in), methods=["POST"]),
        Route(f"{base}/step", e(step), methods=["POST"]),
        Route(f"{base}/refresh", e(simple("refresh", "refresh", "Read the login page of {lane} again")),
              methods=["POST"]),
        Route(f"{base}/take", e(simple("take", "take", "Take control of {lane} from the login form")),
              methods=["POST"]),
        Route(f"{base}/resume", e(simple("resume", "resume", "Back to the login form on {lane}")), methods=["POST"]),
        Route(f"{base}/cancel", e(simple("cancel", "cancel", "End the assisted login on {lane}")), methods=["POST"]),
        Route(f"{base}/save", e(save), methods=["POST"]),
        Route(f"{base}/screenshot", e(screenshot), methods=["GET"]),
    ]
