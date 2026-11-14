"""Webhooks, as the app reads them (web/src/api/types.ts, `Webhook` and `WebhookDelivery`).

The node's own service does the work (agentos.webhooks.service): it keeps the webhooks, records each
delivery and sends it. These routes are the owner's, in person: a webhook sends this computer's approvals
and failures out of the machine, so neither a token (the owner's own included) nor a member may make,
change, read or test one.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute

from agentos.api.app import Api, access_of, read_body
from agentos.api.errors import ApiError
from agentos.api.web.base import org_route
from agentos.webhooks.outbound import OutboundRefused
from agentos.webhooks.service import EVENTS

Event = Literal["approval.requested", "approval.decided", "approval.expired", "run.failed"]  # = webhooks.service.EVENTS
PERSON_ONLY = "Only the owner, signed in to the panel, can use webhooks. A token cannot, the owner token included."


class WebhookCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048)
    events: list[Event] = Field(default_factory=lambda: list(EVENTS), min_length=1, max_length=len(EVENTS))


class WebhookUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool | None = None
    events: list[Event] | None = Field(default=None, min_length=1, max_length=len(EVENTS))


def routes(api: Api) -> list[BaseRoute]:
    def service() -> Any:
        svc = api.runtime.get("webhooks")
        if svc is None:
            raise ApiError("unavailable", "This computer cannot send webhooks.")
        return svc

    def person(request: Request) -> Any:
        access = access_of(request)
        if not (access.owner and access.human):
            access.trace("webhook", status="denied", summary="webhooks need the owner in person")
            raise ApiError("denied", PERSON_ONLY)
        return access

    async def list_hooks(request: Request) -> list[dict[str, Any]]:
        person(request)
        return service().list()

    async def create(request: Request) -> dict[str, Any]:
        access = person(request)
        body = await read_body(request, WebhookCreate)
        try:
            made = await service().create(body.url.strip(), list(body.events))
        except OutboundRefused as refused:
            raise ApiError("invalid", str(refused)) from None
        access.trace("webhook.created", summary=f"webhook to {made['origin']}")
        return made

    async def update(request: Request) -> dict[str, Any]:
        access = person(request)
        hook_id = request.path_params["id"]
        body = await read_body(request, WebhookUpdate)
        changed = service().update(hook_id, enabled=body.enabled,
                                   events=None if body.events is None else list(body.events))
        access.trace("webhook.updated", summary=f"webhook {changed['origin']}: "
                     f"{'on' if changed['enabled'] else 'off'}, events {', '.join(changed['events'])}")
        return changed

    async def delete(request: Request) -> Response:
        access = person(request)
        hook_id = request.path_params["id"]
        origin = service().get(hook_id).origin
        service().delete(hook_id)
        access.trace("webhook.deleted", summary=f"webhook {origin} removed")
        return Response(status_code=204)

    async def rotate(request: Request) -> dict[str, str]:
        access = person(request)
        hook_id = request.path_params["id"]
        origin = service().get(hook_id).origin
        secret = service().rotate(hook_id)
        access.trace("webhook.rotated", summary=f"webhook {origin}: secret rotated")
        return {"secret": secret}

    async def test(request: Request) -> dict[str, str]:
        access = person(request)
        hook_id = request.path_params["id"]
        delivery_id = service().test(hook_id)
        access.trace("webhook.tested", summary=f"test sent to {service().get(hook_id).origin}")
        return {"id": delivery_id}

    async def deliveries(request: Request) -> list[dict[str, Any]]:
        person(request)
        raw = request.query_params.get("limit", "50")
        if not raw.isdigit():
            raise ApiError("invalid", "limit should be a whole number.")
        return service().deliveries(request.path_params["id"], int(raw))

    return [
        org_route(api, "/webhooks", list_hooks, ["GET"], owner=True),
        org_route(api, "/webhooks", create, ["POST"], owner=True),
        org_route(api, "/webhooks/{id}", update, ["PATCH"], owner=True),
        org_route(api, "/webhooks/{id}", delete, ["DELETE"], owner=True),
        org_route(api, "/webhooks/{id}/rotate", rotate, ["POST"], owner=True),
        org_route(api, "/webhooks/{id}/test", test, ["POST"], owner=True),
        org_route(api, "/webhooks/{id}/deliveries", deliveries, ["GET"], owner=True),
    ]
