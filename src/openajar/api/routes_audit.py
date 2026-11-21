"""The audit routes (docs/API.md "Audit"): replay a lane's day, verify the log, share links.

Scope follows Activity: the owner sees every row; a member sees every principal's rows on lanes
in workspaces where their role has `see`; a grant sees only its own grant family's rows, on
lanes its grant covers. Share links are made by the owner in person (a panel session, never a
token) and revoked by the owner; opening one needs no sign-in, and every view is written to the
action log.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.routing import Route

from openajar.api.access import NOT_YOUR_ROLE
from openajar.api.app import Api, access_of, endpoint, query_str, read_body
from openajar.api.errors import ApiError
from openajar.api.routes_core import OUT_OF_SCOPE
from openajar.audit.captures import CAPTURE_KIND
from openajar.core.errors import NotFound
from openajar.core.presence import Presence


class ShareIn(BaseModel):
    trace: str | None = Field(default=None, max_length=80)
    lane: str | None = Field(default=None, max_length=200)
    day: str | None = Field(default=None, max_length=10)
    ttl: str | None = Field(default=None, max_length=10)
    screenshots: bool = False


class Handlers:
    """The handlers of the routes below, sharing one Api (and what they keep between calls)."""

    def __init__(self, api: Api) -> None:
        self.api = api
        self.made: list[Any] = []

    def audit(self) -> Any:
        """One audit service per app (it holds the audit.db connection for share links)."""
        if not self.made:
            from openajar.audit.service import AuditService

            self.made.append(AuditService.from_runtime(self.api.runtime))
        return self.made[0]

    def may_see(self, request: Request) -> None:
        """A member needs `see` somewhere before anything else is looked up."""
        access = access_of(request)
        if access.member is not None and not access.can("see"):
            access.trace("audit.replay", status="denied", summary="role: no see")
            raise ApiError("denied", NOT_YOUR_ROLE)

    def _sees(self, request: Request, lane: str) -> bool:
        access = access_of(request)
        if not access.lane_allowed(lane):
            return False
        if access.member is not None:
            spaces = access.workspaces_with("see")  # type: ignore[attr-defined]
            return spaces is None or lane.split("/", 1)[0] in spaces
        return True

    def scoped(self, request: Request, lane: str) -> str | None:
        """The grant family the caller is limited to (None: every principal on this lane)."""
        access = access_of(request)
        if not access.lane_allowed(lane):
            access.trace("audit.replay", status="denied", lane=lane, summary="lane out of scope")
            raise ApiError("denied", OUT_OF_SCOPE)
        if access.member is not None:
            spaces = access.workspaces_with("see")  # type: ignore[attr-defined]
            if spaces is not None and lane.split("/", 1)[0] not in spaces:
                raise ApiError("denied", OUT_OF_SCOPE)
            return None
        return access.viewer

    async def replay(self, request: Request) -> dict:
        self.may_see(request)
        from openajar.audit.service import today

        svc = self.audit()
        tz = query_str(request, "tz") or self.api.timezone
        lane, day = query_str(request, "lane"), query_str(request, "day") or today(tz, svc.clock())
        if not lane:  # the day's index: which lanes the caller may replay
            access = access_of(request)
            return svc.lanes_on(day, tz=tz, viewer_path=None if access.member is not None else access.viewer,
                                allowed=lambda name: self._sees(request, name))
        viewer = self.scoped(request, lane)
        is_owner = access_of(request).owner
        # a day's walk over the chain reads many rows: off the event loop
        return await asyncio.to_thread(svc.replay, lane, day, tz=tz, viewer_path=viewer, owner=is_owner,
                                       screenshot_url=lambda run_id: f"/api/audit/runs/{run_id}/screenshot",
                                       capture_url=lambda trace_id: f"/api/audit/captures/{trace_id}")

    async def run_screenshot(self, request: Request) -> FileResponse:
        self.may_see(request)
        svc = self.audit()
        run = svc.run_view(request.path_params["id"])
        lane = (run or {}).get("lane")
        if run is None or not isinstance(lane, str):
            raise NotFound("No such run.")
        self.scoped(request, lane)
        path = svc.screenshot_path(run)
        if path is None:
            raise NotFound("This run kept no screenshot.")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    async def capture(self, request: Request) -> FileResponse:
        """The screenshot kept after a call (openajar.audit.captures), by the id of its row."""
        self.may_see(request)
        svc = self.audit()
        row = svc.traces.row(request.path_params["id"])
        if row is None or row["kind"] != CAPTURE_KIND or not isinstance(row["lane"], str):
            raise NotFound("No such screenshot.")
        try:
            viewer = self.scoped(request, row["lane"])
        except ApiError:  # out of the caller's scope reads as missing: an id says nothing about other lanes
            raise NotFound("No such screenshot.") from None
        if viewer is not None and f"/{viewer}/" not in (row["grant_path"] or ""):
            raise NotFound("No such screenshot.")
        from openajar.audit.service import _capture_of

        cap = _capture_of([row])
        path = svc.capture_path(cap)
        if path is None:
            raise NotFound("This screenshot is no longer on the node.")
        kind = {"webp": "image/webp", "jpeg": "image/jpeg"}.get((cap or {}).get("format") or "", "image/png")
        return FileResponse(path, media_type=kind, headers={"Cache-Control": "no-store"})

    async def verify(self, request: Request) -> dict:
        from openajar.audit.service import day_window

        day, lane = query_str(request, "day"), query_str(request, "lane")
        since = until = None
        if day:
            since, until = day_window(day, query_str(request, "tz") or self.api.timezone)
        report = await asyncio.to_thread(self.audit().verify, since=since, until=until, lane=lane)
        return report.public()

    async def share_create(self, request: Request) -> dict:
        body = await read_body(request, ShareIn)
        out = self.audit().share_create(trace_id=body.trace, lane=body.lane, day=body.day, tz=self.api.timezone, ttl=body.ttl,
                                   screenshots=body.screenshots, by="owner:panel")
        access_of(request).trace("audit.share", lane=body.lane, summary=f"share {out['id']} ({out['kind']})",
                                 share=out["id"])
        from openajar.api.routes_members import panel_base

        base = panel_base(self.api)
        # `url` is the panel's read-only page (a token in the fragment never reaches a server log);
        # `api_url` is the public JSON route that page reads
        token = out["token"]
        return {**out, "url": f"{base}/shared#token={token}", "api_url": f"{base}/api/audit/shared/{token}"}

    async def share_list(self, request: Request) -> list[dict]:
        return self.audit().shares()

    async def share_revoke(self, request: Request) -> dict:
        out = self.audit().share_revoke(request.path_params["id"], by="owner:panel")
        access_of(request).trace("audit.share_revoke", summary=f"share {out['id']} revoked", share=out["id"])
        return out

    async def shared(self, request: Request) -> dict:
        return self.audit().share_view(request.path_params["token"],
                                  screenshot_url=lambda token, run_id: f"/api/audit/shared/{token}/runs/{run_id}"
                                                                       "/screenshot")

    async def shared_screenshot(self, request: Request) -> FileResponse:
        path = self.audit().share_screenshot(request.path_params["token"], request.path_params["id"])
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


def routes(api: Api) -> list[Route]:
    h = Handlers(api)

    def e(fn: Any) -> Any:
        return endpoint(api, fn)

    def owner(fn: Any) -> Any:
        return endpoint(api, fn, owner=True)

    def owner_in_person(fn: Any) -> Any:
        """A link anyone can open, out of the node's own log: made by a person, never by a token."""
        return endpoint(api, fn, owner=True, presence=Presence.PERSON)

    def public(fn: Any) -> Any:
        return endpoint(api, fn, public=True)

    return [
        Route("/api/audit/replay", e(h.replay), methods=["GET"]),
        Route("/api/audit/runs/{id}/screenshot", e(h.run_screenshot), methods=["GET"]),
        Route("/api/audit/captures/{id}", e(h.capture), methods=["GET"]),
        Route("/api/audit/verify", owner(h.verify), methods=["GET"]),
        Route("/api/audit/shares", owner_in_person(h.share_create), methods=["POST"]),
        Route("/api/audit/shares", owner(h.share_list), methods=["GET"]),
        Route("/api/audit/shares/{id}", owner(h.share_revoke), methods=["DELETE"]),
        Route("/api/audit/shared/{token}", public(h.shared), methods=["GET"]),
        Route("/api/audit/shared/{token}/runs/{id}/screenshot", public(h.shared_screenshot), methods=["GET"]),
    ]
