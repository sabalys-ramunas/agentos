"""AI models on a node: who thinks, and adding one without the web app ever holding a key.

`POST /brains/handovers` starts one trip and answers where the node's own form is. The key goes
straight into that form (agentos.brains.form), and the app only polls how far it got.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute

from agentos.api.app import Api, access_of, read_body
from agentos.api.errors import ApiError
from agentos.api.web.base import org_route, plain_route, read
from agentos.brains import form as brain_form
from agentos.brains.form import SERVICES, Handovers, Service
from agentos.brains.models import BrainKind, BrainSpec
from agentos.core.errors import AgentosError
from agentos.runner.mail import form as mail_form

PERSON_ONLY = "Only a person signed in here can add an AI service. A token cannot, the owner token included."


class HandoverStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str = Field(min_length=1, max_length=40)
    computer: str = Field(min_length=1, max_length=80)


def models_of(info: dict[str, Any]) -> list[dict[str, Any]]:
    caps = info.get("caps") or {}
    can = ["decide"] if caps.get("decisions") else ["agent", "write", "rescue"] if caps.get("tools") else ["write", "rescue"]
    return [{"id": info.get("model") or info.get("name"), "can": can, "usd_per_step": None}]


def brain_out(api: Api, info: dict[str, Any]) -> dict[str, Any]:
    seats = info.get("seats") or {}
    return {
        "name": info["name"],
        "kind": info["kind"],
        "provider": info["provider"],
        "state": info["state"],
        "seats": {"in_use": seats.get("in_use", 0), "total": seats["total"]} if seats.get("total") else None,
        "window_used_pct": info.get("window_used_pct"),
        "computer": api.node_name,
        "models": models_of(info),
    }


def spec_of(service: Service, value: str) -> BrainSpec:
    extra: dict[str, Any] = {"model": service.model, "effort": service.effort}
    if service.takes_address:
        extra["base_url"] = value
    return BrainSpec.template(service.brain, BrainKind(service.kind), service.provider,
                              **{key: item for key, item in extra.items() if item is not None})


def routes(api: Api) -> list[BaseRoute]:
    handovers = Handovers()

    def a_person(request: Request, action: str) -> None:
        access = access_of(request)
        if not access.human:
            access.trace(action, status="denied", summary="this needs a person")
            raise ApiError("denied", PERSON_ONLY)

    async def brains(request: Request) -> list[dict[str, Any]]:
        return [brain_out(api, row) for row in await read(request, "/api/brains")]

    async def start(request: Request) -> dict[str, Any]:
        a_person(request, "brains.handover")
        body = await read_body(request, HandoverStart)
        if body.service not in SERVICES:
            raise ApiError("invalid", "This computer does not know that AI service.")
        if body.computer != api.node_name:
            raise ApiError("not_found", "This computer can only keep a key for itself.")
        access_of(request).trace("brains.handover", summary=f"Started adding {SERVICES[body.service].name}")
        return handovers.start(body.service, body.computer).public()

    async def one(request: Request) -> dict[str, Any]:
        row = handovers.get(request.path_params["id"])
        if row is None:
            raise ApiError("not_found", "That form has ended. Start again.")
        return row.public()

    async def form_get(request: Request) -> Response:
        row = handovers.by_token(request.path_params["token"])
        if row is None:
            return brain_form.ended()
        if not mail_form.direct(request):
            return mail_form.not_direct()
        return brain_form.render(SERVICES[row.service], row.computer)

    async def form_post(request: Request) -> Response:
        row = handovers.by_token(request.path_params["token"])
        if row is None:
            return brain_form.ended()
        if not mail_form.direct(request) or not mail_form.same_origin(request):
            return mail_form.not_direct()
        service = SERVICES[row.service]
        value = "".join(str(mail_form.read_form(await request.body()).get("value", "")).split())
        if not value:
            return brain_form.render(service, row.computer, error="That was empty.", status=400)
        svc = api.runtime.require("brains")
        row.stage = "checking"
        try:
            spec = spec_of(service, value)
            exists = svc.store.get_brain(spec.name) is not None
            if exists and spec.credential:
                await svc.set_credential(spec.name, value)
            elif exists:
                await svc.remove_brain(spec.name)
                await svc.add_brain(spec)
            else:
                await svc.add_brain(spec, credential=None if service.takes_address else value)
        except (AgentosError, ValueError) as exc:
            row.stage = "waiting"
            message = getattr(exc, "message", None) or "That did not work. Nothing was saved."
            return brain_form.render(service, row.computer, error=message, status=400)
        row.stage = "done"
        return brain_form.done(service, row.computer)

    return [
        org_route(api, "/brains", brains, ["GET"]),
        org_route(api, "/brains/handovers", start, ["POST"], owner=True),
        org_route(api, "/brains/handovers/{id}", one, ["GET"], owner=True),
        # On a node the form itself says it was filled, so this only reads where it got to.
        org_route(api, "/brains/handovers/{id}/filled", one, ["POST"], owner=True),
        plain_route(api, "/api/brains/form/{token}", form_get, ["GET"], public=True, action="brains.form"),
        plain_route(api, "/api/brains/form/{token}", form_post, ["POST"], public=True, action="brains.form.send"),
    ]
