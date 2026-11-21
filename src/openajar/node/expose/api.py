"""The panel routes of exposes and forwards, mounted at /api/exposes by openajar.api.mounts.

The mount needs the owner, at least the device credential (`openajar connect` on the owner's
computer) or a person: an agent's grant, an admin grant and the node token are refused with
presence_required, and no MCP tool reaches these. Every write is traced as a panel action, and the
service traces each start and end with its reason.

- `GET /api/exposes`: what is open.
- `POST /api/exposes` `{"kind": "expose", "lane", "name", "port", "ttl_s"?}` or
  `{"kind": "forward", "lane", "port", "ttl_s"?}`.
- `POST /api/exposes/{id}/ready`: the laptop's `ssh -R` is up (an expose).
- `POST /api/exposes/{id}/heartbeat`: renew the lease; 404 once it ended, with the reason.
- `DELETE /api/exposes/{id}`: end it now.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from openajar.core.errors import InvalidRequest, ServiceUnavailable
from openajar.core.models import parse_lane_id


def _by(request: Request) -> str:
    """Who made it, for the action log: the principal and, for a device credential, its computer."""
    auth = getattr(request.state, "auth", None)
    principal = getattr(auth, "principal", None) or getattr(request.state, "principal", None)
    who = str(getattr(principal, "id", None) or "owner")
    label = getattr(getattr(auth, "session", None), "device_label", None)
    return f"{who} on {label}" if label else who


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise InvalidRequest("send a JSON object") from None
    if not isinstance(body, dict):
        raise InvalidRequest("send a JSON object")
    return body


def _lane(body: dict[str, Any]) -> str:
    lane = body.get("lane")
    if not isinstance(lane, str):
        raise InvalidRequest("lane is required, for example personal/linkedin")
    try:
        parse_lane_id(lane)
    except ValueError as e:
        raise InvalidRequest(str(e)) from None
    return lane


def routes(runtime: Any) -> list[Route]:
    def svc() -> Any:
        service = runtime.get("expose") if runtime is not None else None
        if service is None:
            raise ServiceUnavailable("this node cannot expose or forward (agentosd is older, or the service failed)")
        return service

    async def list_links(request: Request) -> Response:
        service = runtime.get("expose") if runtime is not None else None
        return JSONResponse({"links": service.list() if service is not None else []})

    async def create(request: Request) -> Response:
        body = await _body(request)
        kind = body.get("kind", "expose")
        lane = _lane(body)
        if kind == "expose":
            name = body.get("name")
            if not isinstance(name, str):
                raise InvalidRequest("name is required, for example myapp")
            out = await svc().create_expose(lane, name=name, port=body.get("port"), ttl_s=body.get("ttl_s"),
                                            by=_by(request))
        elif kind == "forward":
            out = await svc().create_forward(lane, port=body.get("port"), ttl_s=body.get("ttl_s"), by=_by(request))
        else:
            raise InvalidRequest("kind must be expose or forward")
        return JSONResponse(out)

    async def ready(request: Request) -> Response:
        return JSONResponse(await svc().ready(request.path_params["id"]))

    async def heartbeat(request: Request) -> Response:
        return JSONResponse(svc().heartbeat(request.path_params["id"]))

    async def end(request: Request) -> Response:
        return JSONResponse(await svc().end(request.path_params["id"]))

    return [
        Route("/exposes", list_links, methods=["GET"]),
        Route("/exposes", create, methods=["POST"]),
        Route("/exposes/{id}/ready", ready, methods=["POST"]),
        Route("/exposes/{id}/heartbeat", heartbeat, methods=["POST"]),
        Route("/exposes/{id}", end, methods=["DELETE"]),
    ]
