"""Local control socket of the health service, so the CLI can pause or drain lanes.

`AGENTOS_HOME/run/ops.sock` (or /tmp/agentos-<hash>/ when the home is too long for a socket path): chmod 600 inside a 700 directory; on Linux the peer's uid must
be ours or root (SO_PEERCRED). One JSON line in, one JSON line out:

    {"op": "ping"}                      -> {"ok": true}
    {"op": "status"}                    -> {"ok": true, "status": {...last health report...}}
    {"op": "check"}                     -> {"ok": true, "status": {...}} (runs the checks now)
    {"op": "lanes"}                     -> {"ok": true, "lanes": [{"id", "state", "proxy"}]}
    {"op": "holders"}                   -> {"ok": true, "lanes": [{"id", "state", "holders": [{"kind", "who"}]}]}
    {"op": "close", "lane": "ws/name"}  -> {"ok": true, "state": "cold"}  (waits for the lane)
    {"op": "open", "lane": "ws/name"}   -> {"ok": true}  (warms it in the background)
    {"op": "drain", "timeout": 60}      -> {"ok": true, "closed": [...], "failed": {...}}
    {"op": "reload_tools"}              -> {"ok": true, "tools": {"added", "removed", "changed"}}
    {"op": "lane_check", "lane": "ws/name"} -> {"ok": true, "reports": [{...one lane report...}]}
    {"op": "lane_check"}                -> {"ok": true, "reports": [...every lane...]}
    {"op": "lane_health", "lane": "ws/name", "days": 30, "fresh": false}
                                        -> {"ok": true, "lanes": [{...one lane's health...}]}
    {"op": "lane_health", "days": 30}   -> {"ok": true, "lanes": [...every lane...]}
    {"op": "keepalive", "lane": "ws/name"}               -> {"ok": true, "keepalive": {...the plan...}}
    {"op": "keepalive", "lane": "ws/name", "now": true}  -> {"ok": true, "visit": {...}, "keepalive": {...}}
    {"op": "planned", "source": "keepalive" | "engage", "start": iso, "end": iso, "workspace": "ws" | null}
                                        -> {"ok": true, "planned": {...what that service plans in the range...}}
    errors                              -> {"ok": false, "error": {"code", "message"}}
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
from pathlib import Path
from typing import Any

from agentos.core.errors import AgentosError
from agentos.ops.layout import prepare_socket

MAX_LINE = 64 * 1024


def _peer_ok(writer: asyncio.StreamWriter) -> bool:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return False
    if hasattr(socket, "SO_PEERCRED"):
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        except OSError:
            return False
        _, uid, _ = struct.unpack("3i", raw)
        return uid in (0, os.geteuid())
    return True  # macOS and BSD: rely on the 600 socket inside a 700 directory


async def _handle(service: Any, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    resp: dict[str, Any]
    try:
        if not _peer_ok(writer):
            resp = {"ok": False, "error": {"code": "denied", "message": "not allowed"}}
        else:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            req = json.loads(line or b"{}")
            resp = await dispatch(service, req if isinstance(req, dict) else {})
    except AgentosError as e:
        resp = {"ok": False, "error": {"code": e.code, "message": e.message}}
    except Exception:  # noqa: BLE001
        resp = {"ok": False, "error": {"code": "invalid", "message": "bad request"}}
    try:
        writer.write(json.dumps(resp, default=str).encode() + b"\n")
        await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def dispatch(service: Any, req: dict[str, Any]) -> dict[str, Any]:
    op = req.get("op")
    lane = req.get("lane")
    if op == "ping":
        return {"ok": True}
    if op == "status":
        return {"ok": True, "status": service.status()}
    if op == "check":
        return {"ok": True, "status": await service.check_now()}
    if op == "lanes":
        return {"ok": True, "lanes": service.lanes_summary()}
    if op == "holders":
        return {"ok": True, "lanes": service.holders()}
    if op in ("close", "open"):
        if not isinstance(lane, str):
            return {"ok": False, "error": {"code": "invalid", "message": "lane is required"}}
        if op == "close":
            return {"ok": True, "state": await service.close_lane(lane)}
        service.open_lane_soon(lane)
        return {"ok": True}
    if op == "lane_check":
        if lane is not None and not isinstance(lane, str):
            return {"ok": False, "error": {"code": "invalid", "message": "lane must be a string"}}
        return {"ok": True, "reports": await service.lane_check(lane)}
    if op == "lane_health":
        days, fresh = req.get("days", 30), req.get("fresh", False)
        if (lane is not None and not isinstance(lane, str)) or not isinstance(days, int) or isinstance(days, bool):
            return {"ok": False, "error": {"code": "invalid", "message": "lane must be a string, days a number"}}
        return {"ok": True, "lanes": await service.lane_health(lane, days=days, fresh=fresh is True)}
    if op == "keepalive":
        if not isinstance(lane, str):
            return {"ok": False, "error": {"code": "invalid", "message": "lane is required"}}
        keepalive = service.runtime.get("keepalive") if getattr(service, "runtime", None) is not None else None
        if keepalive is None:
            return {"ok": False, "error": {"code": "unavailable", "message": "keepalive is not running in agentosd"}}
        out: dict[str, Any] = {"ok": True}
        if req.get("now") is True:
            out["visit"] = await keepalive.visit_now(lane)
        out["keepalive"] = keepalive.status(lane)
        return out
    if op in ("site_api_discover", "site_api_try"):
        return await _site_api(service, op, req)
    if op == "planned":  # `agentos agenda list` asks the daemon: it owns the lanes these plans read
        return _planned(service, req)
    if op == "reload_tools":
        watch = service.runtime.get("tool_reload") if getattr(service, "runtime", None) is not None else None
        if watch is None:
            return {"ok": False, "error": {"code": "unavailable", "message": "this agentosd does not reload tools"}}
        return {"ok": True, "tools": (await watch.reload()).public()}
    if op == "audit_anchors":  # `agentos audit verify --anchors`: the heads the controller holds
        anchors = service.runtime.get("anchors") if getattr(service, "runtime", None) is not None else None
        if anchors is None or not anchors.to_controller:
            return {"ok": False, "error": {"code": "unavailable", "message": "this node does not anchor to a controller"}}
        return {"ok": True, **await anchors.fetch_controller(), "status": anchors.status()}
    if op == "drain":
        timeout = req.get("timeout", 60)
        timeout = float(timeout) if isinstance(timeout, int | float) else 60.0
        return {"ok": True, **await service.drain(timeout=timeout)}
    return {"ok": False, "error": {"code": "invalid", "message": "unknown op"}}


async def _site_api(service: Any, op: str, req: dict[str, Any]) -> dict[str, Any]:
    """`agentos site-api discover|try`: agentosd owns the lanes' browsers. Whoever has this socket is
    the owner, as for `agentos connector run`."""
    site_apis = service.runtime.get("site_apis") if getattr(service, "runtime", None) is not None else None
    if site_apis is None:
        return {"ok": False, "error": {"code": "unavailable", "message": "site APIs are not running in agentosd"}}
    try:
        if op == "site_api_discover":
            return {"ok": True, "result": await site_apis.discover(req.get("on"), req.get("url"))}
        owner = {"kind": "human", "id": "owner", "owner": "owner"}
        return {"ok": True, "result": await site_apis.try_endpoint(req.get("api"), req.get("endpoint"), req.get("params"), by=owner,
                                                                   on=req.get("on"), grant_id="owner")}
    except AgentosError as e:
        return {"ok": False, "error": {"code": e.code, "message": e.message}}


def _planned(service: Any, req: dict[str, Any]) -> dict[str, Any]:
    from datetime import UTC, datetime

    source, workspace = req.get("source"), req.get("workspace")
    if source not in ("keepalive", "engage") or (workspace is not None and not isinstance(workspace, str)):
        return {"ok": False, "error": {"code": "invalid", "message": "source is keepalive or engage"}}
    try:
        start, end = (datetime.fromisoformat(str(req.get(k))) for k in ("start", "end"))
    except ValueError:
        return {"ok": False, "error": {"code": "invalid", "message": "start and end are ISO 8601 times"}}
    start, end = (t if t.tzinfo else t.replace(tzinfo=UTC) for t in (start, end))
    runtime = getattr(service, "runtime", None)
    found = runtime.get(source) if runtime is not None else None
    if not callable(getattr(found, "planned", None)):
        return {"ok": False, "error": {"code": "unavailable", "message": f"{source} is not running in agentosd"}}
    return {"ok": True, "planned": found.planned(start, end, workspace=workspace)}


def _live(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(str(path))
        return True
    except OSError:
        return False


async def start_server(service: Any, path: Path) -> asyncio.AbstractServer:
    path = Path(path)
    prepare_socket(path)  # a path too long, or a short dir that is not ours: a clear error
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if path.exists() or path.is_symlink():
        if _live(path):
            raise AgentosError(f"another agentos process already serves {path}")
        path.unlink()
    server = await asyncio.start_unix_server(lambda r, w: _handle(service, r, w), path=str(path), limit=MAX_LINE)
    os.chmod(path, 0o600)
    return server
