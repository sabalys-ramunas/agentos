"""Browser devices in the panel (AUD-02, agentos.node.devices): one pinned
fingerprint plus one proxy, shared by a company's lanes. Owner only: a device spans lanes and
names proxies, like the proxy list.

  GET    /api/browser-devices                  every device: proxy, pin summary, the lanes on it
  POST   /api/browser-devices                  {name, proxy_label?, from_lane?, note?, confirm?}: make
                                               one (409 confirm_required when other lanes of their
                                               own share the lane's proxy; confirm needs a person)
  GET    /api/browser-devices/{name}           one device, with its proxy's health (the lane
                                               check's proxy, exit IP and location checks)
  DELETE /api/browser-devices/{name}           remove a device no lane uses (its pin goes to trash)
  GET    /api/lanes/{ws}/{name}/device-move    ?to=<device|none>: what the move changes, in plain
                                               words, and whether it needs a confirm
  POST   /api/lanes/{ws}/{name}/device         {device: str | null, confirm: bool}: move the lane.
                                               A move its site would notice without confirm: 409
                                               confirm_required, nothing changed. Needs a person: a
                                               move can change the lane's exit

"Browser devices" because the panel's "devices" are the passkeys and phones people sign in with.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.routing import Route

from agentos.api.app import Api, access_of, endpoint, read_body
from agentos.api.jsonable import jsonable, maybe_await
from agentos.api.routes_core import one_lane, scoped_lane_id
from agentos.core.errors import InvalidRequest, NotFound, ServiceUnavailable
from agentos.core.models import DEVICE_NAME_RE
from agentos.core.presence import Presence

NAME = DEVICE_NAME_RE.pattern


class DeviceCreate(BaseModel):
    name: str = Field(pattern=NAME)
    proxy_label: str | None = Field(default=None, max_length=64)
    from_lane: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=200)
    confirm: bool = False


class DeviceMove(BaseModel):
    device: str | None = Field(default=None, pattern=NAME)
    confirm: bool = False


def _lanes(api: Api) -> Any:
    lanes = api.svc("lanes")
    if not callable(getattr(lanes, "devices", None)):
        raise ServiceUnavailable("This node's lane manager has no devices.")
    return lanes


def _name(request: Request) -> str:
    name = request.path_params["name"]
    if not DEVICE_NAME_RE.fullmatch(name):
        raise NotFound(f"No device {name}.")
    return name


async def _health(api: Api, name: str) -> dict | None:
    checker = api.optional("lane_check")
    fn = getattr(checker, "check_device", None)
    if fn is None:
        return None
    report = await fn(name)
    return report.to_dict()


def routes(api: Api) -> list[Route]:
    async def devices(request: Request) -> list[dict]:
        return jsonable(await maybe_await(_lanes(api).devices()))

    async def device_get(request: Request) -> dict:
        name = _name(request)
        info = jsonable(await maybe_await(_lanes(api).device_info(name)))
        return {**info, "health": await _health(api, name)}

    async def device_create(request: Request) -> dict:
        body = await read_body(request, DeviceCreate)
        if body.proxy_label is None and body.from_lane is None:
            raise InvalidRequest("Give the device its proxy, or make it from a lane its site already knows.")
        if body.confirm:  # a second computer on the exit of accounts that already use it: a person's call
            access_of(request).require_presence(Presence.PERSON, "device.create")
        info = await maybe_await(_lanes(api).create_device(body.name, proxy_label=body.proxy_label,
                                                           from_lane=body.from_lane, note=body.note,
                                                           confirm=body.confirm,
                                                           by=request.state.auth.principal.id))
        access_of(request).trace("device.create", lane=body.from_lane,
                                 summary=f"Create device {body.name}" + (f" from {body.from_lane}"
                                                                         if body.from_lane else ""))
        if body.from_lane:
            api.publish("lane", await one_lane(api, body.from_lane))
        return jsonable(info)

    async def device_delete(request: Request) -> dict:
        name = _name(request)
        moved = await maybe_await(_lanes(api).remove_device(name, by=request.state.auth.principal.id))
        access_of(request).trace("device.remove", summary=f"Remove device {name}")
        return {"removed": name, "trash": str(moved) if moved else None}

    async def device_move_plan(request: Request) -> dict:
        lane_id = scoped_lane_id(request, "lane.device")
        to = request.query_params.get("to") or "none"
        target = None if to == "none" else to
        if target is not None and not DEVICE_NAME_RE.fullmatch(target):
            raise NotFound(f"No device {target}.")
        return jsonable(await maybe_await(_lanes(api).plan_device_move(lane_id, target)))

    async def device_move(request: Request) -> dict:
        lane_id = scoped_lane_id(request, "lane.device")
        body = await read_body(request, DeviceMove)
        plan = await maybe_await(_lanes(api).set_device(lane_id, body.device, confirm=body.confirm,
                                                        by=request.state.auth.principal.id))
        access_of(request).trace("lane.device", lane=lane_id,
                                 summary=f"Move {lane_id} to {body.device or 'a device of its own'}"
                                         + (" (confirmed: a new computer to its site)" if plan.get("confirm") else ""))
        lane = await one_lane(api, lane_id)
        api.publish("lane", lane)
        return {"plan": jsonable(plan), "lane": lane}

    return [
        Route("/api/browser-devices", endpoint(api, devices, owner=True), methods=["GET"]),
        Route("/api/browser-devices", endpoint(api, device_create, owner=True), methods=["POST"]),
        Route("/api/browser-devices/{name}", endpoint(api, device_get, owner=True), methods=["GET"]),
        Route("/api/browser-devices/{name}", endpoint(api, device_delete, owner=True), methods=["DELETE"]),
        Route("/api/lanes/{ws}/{name}/device-move", endpoint(api, device_move_plan, owner=True), methods=["GET"]),
        # a move changes the lane's exit or the computer its site sees: a person's call (docs/THREAT-MODEL.md)
        Route("/api/lanes/{ws}/{name}/device", endpoint(api, device_move, owner=True, presence=Presence.PERSON),
              methods=["POST"]),
    ]
