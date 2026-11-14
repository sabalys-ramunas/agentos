"""Live updates (SSE), the noVNC relay and the terminal WebSocket."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import dataclasses
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from agentos.api.app import Api, access_of, endpoint
from agentos.api.routes_core import NAME_RE, WORKSPACE_RE
from agentos.core import live as live_sockets
from agentos.core.aio import maybe_await
from agentos.core.errors import AgentosError, ApprovalPending
from agentos.core.models import ControlState, Effect, LaneKind

log = logging.getLogger("agentos.api")

# WebSocket close codes (4000 + the HTTP status they mirror) so the panel can explain them.
WS_PENDING = 4202  # an approval must come first; the reason carries its id
WS_UNAUTHORIZED = 4401
WS_DENIED = 4403
WS_NOT_FOUND = 4404
WS_NOT_WARM = 4410  # the app's "closed before it was ready" (web/.../live/stream.ts)
WS_UNAVAILABLE = 4503
WS_UPSTREAM = 4502
# A member's roles and grants are compared with what a socket holds on every heartbeat, and members.sync (the one
# write on that path) runs only when they differ, or this often: a grant near its end is re-minted then.
HEARTBEAT_SYNC_S = 60.0

WS_HANDED_BACK = 4403  # with the reason "handed_back": control moved to the agents
WS_NOT_HOLDING = 4403  # with the reason "not_holding": a control view without the lane


VIEW, CONTROL = "view", "control"


def _mode(websocket: WebSocket) -> str | None:
    """`?mode=view` watches, `?mode=control` drives (the app's two views). Absent, the panel's own rule
    holds; any other word is read as a view, the safer of the two."""
    raw = websocket.query_params.get("mode")
    if raw is None:
        return None
    return CONTROL if raw == CONTROL else VIEW


def _holds(lanes: Any, lane_id: str) -> bool:
    """Whether a human holds control of the lane right now."""
    try:
        return lanes.get(lane_id).control.state == ControlState.HUMAN
    except Exception:  # noqa: BLE001  (an unknown lane is nobody's to hold)
        return False


def _keys(auth: Any, lane: str | None) -> dict:
    member = auth.access.member
    return {"lane": lane, "grant_id": auth.access.grant_id, "sid": auth.sid,
            "member_id": getattr(member, "id", None)}


@dataclass
class _Heartbeat:
    """What a live socket holds of its caller between heartbeats: the auth it was opened under, a member's roles
    and live grants as last read (members.snapshot), and when members.sync last ran."""
    auth: Any
    key: Any
    synced: float


async def _heartbeat(api: Api, conn: Any, beat: _Heartbeat) -> Any | None:
    """The caller's auth now, or None when it has ended. Read only while nothing changed: a member's session and
    what they may do are compared with what the socket holds, and only a difference (or the minute) reaches
    authenticate, which syncs the member's grants (agentos.api.app._member_auth). Anyone else is authenticated
    again, as before."""
    member = beat.auth.access.member
    if member is None or beat.auth.sid is None:
        return await api.authenticate(conn, touch=False)
    if api.store.get_session(beat.auth.sid, touch=False) is None:
        return None
    key = api.members.snapshot(member.id)
    if key is None:  # removed, suspended, or the members service is gone
        return None
    if key == beat.key and time.monotonic() - beat.synced < HEARTBEAT_SYNC_S:
        return beat.auth
    fresh = await api.authenticate(conn, touch=False)
    beat.synced = time.monotonic()
    if fresh is not None:
        beat.auth, beat.key = fresh, api.members.snapshot(member.id)
    return fresh


def _recheck(api: Api, conn: Any, kind: str, lane_id: str | None, opened: Any) -> live_sockets.Recheck:
    """What a live socket asks again on every heartbeat (agentos.core.live): is the caller still signed
    in (session, member, token and grant all still live), is the lane still theirs, and, for anyone but
    the owner in person, is it frozen? The owner keeps their own view and shell during a freeze. `opened`
    is the auth the socket was opened under."""
    member = opened.access.member
    beat = _Heartbeat(auth=opened, key=api.members.snapshot(member.id) if member is not None else None,
                      synced=time.monotonic())

    async def check() -> tuple[int, str] | None:
        auth = await _heartbeat(api, conn, beat)
        if auth is None:
            return live_sockets.CLOSE_REVOKED, "signed out, or the access was revoked"
        access = auth.access
        if kind == "events" and access.member is not None and _member_reach_frozen(access, api.runtime.get("freeze")):
            return live_sockets.CLOSE_DENIED, "frozen: everything your grants reach is frozen"
        if kind == "vnc" and not access.owner and not (access.member is not None and lane_id is not None
                                                       and access.watch_terms(lane_id) is not None):
            return live_sockets.CLOSE_DENIED, "this lane is no longer yours to watch"
        if kind == "terminal" and lane_id is not None and not _may_exec(access, lane_id):
            return live_sockets.CLOSE_DENIED, "this lane is outside your grant"
        # in person: the owner's own panel session, not a token and not a device credential
        in_person = access.owner and access.human and getattr(auth.session, "via", None) != "device"
        if not in_person:
            freeze = api.runtime.get("freeze")
            frozen_for = getattr(freeze, "frozen_for", None)
            if callable(frozen_for) and frozen_for(lane_id) is not None:
                return live_sockets.CLOSE_DENIED, "frozen: agents and grants are stopped here"
        return None

    return check


def _frozen(freeze: Any, lane_id: str) -> bool:
    """Is this lane held by a freeze (its own, its workspace's, or the node's)? agentos.freeze.service decides."""
    frozen_for = getattr(freeze, "frozen_for", None)
    return callable(frozen_for) and frozen_for(lane_id) is not None


def _has_glob(text: str) -> bool:
    return any(c in text for c in "*?[")


def _reaches_unfrozen(grant: Any, freeze: Any) -> bool:
    """Does a member's grant still reach a lane no freeze holds? A pattern that names a lane, or a whole workspace,
    is checked against the freeze. Anything else (a glob, every workspace) counts as reaching, so the stream stays
    open and its frozen events are still held back."""
    for pattern in grant.blocks[0].lane_patterns(authority=True) or []:
        ws, _, name = pattern.partition("/")
        if _has_glob(ws) or (name != "*" and _has_glob(name)):
            return True
        if not _frozen(freeze, f"{ws}/{name}"):
            return True
    return False


def _member_reach_frozen(access: Any, freeze: Any) -> bool:
    """Every lane a member's see-grants reach is frozen, so there is nothing left for their event stream to send.
    A freeze keeps a person's grant (agentos.freeze.service), so only this check ends that stream."""
    see = access.grants_with("see")
    return bool(see) and not any(_reaches_unfrozen(grant, freeze) for grant in see)


def _hidden_by_freeze(freeze: Any, access: Any, kind: str, data: Any) -> bool:
    """A member's event stream sends nothing about a lane or workspace a freeze holds: the same events
    _member_event names by lane (a lane, a run, a schedule, a login)."""
    if access.member is None or not isinstance(data, dict):
        return False
    lane = data.get("id") if kind == "lane" else data.get("lane") if kind in ("run", "schedule", "login") else None
    return isinstance(lane, str) and _frozen(freeze, lane)


def _may_exec(access: Any, lane_id: str) -> bool:
    """A shell stays open while the caller may still run commands on the lane. A member: some grant
    of theirs that covers the lane still passes the exec precheck (scope, ceiling, specials; no
    routing, so nothing is asked again). Anyone else: the lane is still in scope."""
    if not access.lane_allowed(lane_id):
        return False
    if access.member is None:
        return True
    for scope in access._scopes_for(lane_id.split("/", 1)[0]):
        try:
            access._ctx(access.grants[scope]).precheck(Effect.EXEC, lane_id=lane_id)
            return True
        except AgentosError:
            continue
    return False


class Handlers:
    """The handlers of the routes below, sharing one Api (and what they keep between calls)."""

    def __init__(self, api: Api) -> None:
        self.api = api
        self.live = live_sockets.of(api.runtime)

    # ---- SSE ----
    async def events(self, request: Request) -> StreamingResponse:
        raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
        last_id = int(raw) if raw and raw.isdigit() else None
        heartbeat = self.api.settings.sse_heartbeat
        deadline = time.monotonic() + self.api.settings.sse_max_seconds
        access = access_of(request)

        auth = request.state.auth
        freeze = self.api.runtime.get("freeze")

        async def stream() -> AsyncIterator[str]:
            yield f"retry: 3000\nid: {last_id if last_id is not None else self.api.bus.last_id}\n\n"
            last_write = time.monotonic()
            # ends when the caller's access does (agentos.core.live), not only at sse_max_seconds
            with self.live.track("events", _recheck(self.api, request, "events", None, auth), **_keys(auth, None)) as entry:
                async with self.api.bus.subscribe(last_id) as sub:
                    while not entry.closed.is_set():
                        now = time.monotonic()
                        remaining = deadline - now
                        if remaining <= 0:
                            return
                        # a comment whenever the wire has been quiet for `heartbeat` seconds; events the
                        # caller may not see are not traffic, so they do not put the next ping off
                        until_ping = max(0.0, last_write + heartbeat - now)
                        event = await _next_or_closed(sub.get, entry, min(until_ping, remaining))
                        if entry.closed.is_set():
                            return
                        if event is None:
                            yield ": ping\n\n"
                            last_write = time.monotonic()
                            continue
                        data = access.filter_event(event.kind, event.data)  # a grant sees only its own lanes
                        if data is None or _hidden_by_freeze(freeze, access, event.kind, data):
                            continue
                        yield (event if data is event.data else dataclasses.replace(event, data=data)).sse()
                        last_write = time.monotonic()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- WebSocket helpers ----
    async def ws_auth(self, websocket: WebSocket) -> Any | None:
        """Authenticate before accepting; a refusal before accept becomes an HTTP 403."""
        if not self.api.origin_ok(websocket):
            await websocket.close(code=WS_UNAUTHORIZED)
            return None
        auth = await self.api.authenticate(websocket)
        if auth is None:
            await websocket.close(code=WS_UNAUTHORIZED)
            return None
        return auth

    def ws_lane_id(self, websocket: WebSocket) -> str | None:
        ws, name = websocket.path_params["ws"], websocket.path_params["name"]
        if not WORKSPACE_RE.match(ws) or not NAME_RE.match(name):
            return None
        return f"{ws}/{name}"

    async def refuse(self, websocket: WebSocket, code: int, reason: str) -> None:
        """After auth: accept, then close with a reason the panel can show."""
        await websocket.accept()
        await websocket.close(code=code, reason=reason)

    # ---- noVNC relay ----
    async def vnc(self, websocket: WebSocket) -> None:
        auth = await self.ws_auth(websocket)
        if auth is None:
            return
        lane_id = self.ws_lane_id(websocket)
        mode = _mode(websocket)
        # A member whose grant carries `control` for the lane watches it (no input: taking over is the
        # owner's in this phase). Anyone else but the owner is refused: the relay forwards input.
        watch_only = not auth.access.owner
        if watch_only and not (auth.access.member is not None and lane_id is not None
                               and auth.access.watch_terms(lane_id) is not None):
            auth.access.trace("lane.view", status="denied", lane=lane_id, summary="owner only")
            return await self.refuse(websocket, WS_DENIED, "the live view needs the owner, or a role that watches")
        if watch_only:
            auth.access.trace("lane.view", lane=lane_id, summary=f"Watch lane {lane_id}")
        lanes = self.api.optional("lanes")
        if lanes is None:
            return await self.refuse(websocket, WS_UNAVAILABLE, "lanes are not configured on this node")
        if lane_id is None:
            return await self.refuse(websocket, WS_NOT_FOUND, "no such lane")
        try:
            view = await maybe_await(lanes.view(lane_id))
        except AgentosError:
            return await self.refuse(websocket, WS_NOT_FOUND, "no such lane")
        if view is None:
            return await self.refuse(websocket, WS_NOT_WARM, "the lane is not open")
        if mode == CONTROL and (watch_only or not _holds(lanes, lane_id)):
            return await self.refuse(websocket, WS_NOT_HOLDING, "not_holding")

        # Always loopback, always the x11vnc port the lane manager reports, never from the request.
        # The same RFB relay as the signed-link viewer: input passes only while a human holds control.
        from agentos.node.vnc_relay import VncRelay, upstream

        offered = websocket.scope.get("subprotocols") or []
        await websocket.accept(subprotocol="binary" if "binary" in offered else None)

        def allow_input() -> bool:
            # `mode=view` only watches; no mode is the panel's own rule: input while a human holds control
            if watch_only or mode == VIEW:
                return False
            return _holds(lanes, lane_id)

        note = getattr(lanes, "note_human_input", None)
        relay = VncRelay(websocket, **upstream(view), allow_input=allow_input,
                         on_input=(lambda: note(lane_id)) if callable(note) else None)
        # A control view (`mode=control`): the moment control moves away from this caller, input stops and
        # the socket closes with "handed_back", so the screen goes back to watching.
        control_view = mode == CONTROL and callable(getattr(lanes, "add_control_listener", None))
        handed = asyncio.Event()

        def on_control(changed: str, old: Any, new: Any) -> None:
            if changed != lane_id:
                return
            relay.control_changed()
            if getattr(new, "state", None) != ControlState.HUMAN:
                handed.set()

        async def run_relay() -> None:
            try:
                await relay.run()
            except OSError as exc:
                log.warning("vnc relay: upstream for %s failed: %s", lane_id, type(exc).__name__)

        if control_view:
            lanes.add_control_listener(on_control)
        try:
            with self.live.track("vnc", _recheck(self.api, websocket, "vnc", lane_id, auth), **_keys(auth, lane_id)) as entry:
                await _race(run_relay(), entry.wait_closed(), *([handed.wait()] if control_view else []))
                if entry.closed.is_set():
                    await _close_ended(websocket, entry)
                elif handed.is_set() and websocket.application_state == WebSocketState.CONNECTED:
                    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                        await websocket.close(code=WS_HANDED_BACK, reason="handed_back")
        finally:
            if control_view:
                lanes.remove_control_listener(on_control)

    # ---- terminal ----
    async def terminal_facts(self, envs: Any, lane_id: str) -> tuple[str | None, dict | None]:
        """Whether `lane_id` has a terminal: (why not, or None; the env service's facts for the check)."""
        if hasattr(envs, "has"):  # the real env service: its own lanes
            if not envs.has(lane_id):
                return "this lane has no terminal", None
            try:
                return None, envs.terminal_facts(lane_id)
            except AgentosError:
                return "no such lane", None
        lanes = self.api.optional("lanes")
        if lanes is not None:
            try:
                info = await maybe_await(lanes.get(lane_id))
                if info.spec.kind != LaneKind.TERMINAL:
                    return "this lane has no terminal", None
            except AgentosError:
                return "no such lane", None
        return None, None

    async def terminal(self, websocket: WebSocket) -> None:
        auth = await self.ws_auth(websocket)
        if auth is None:
            return
        lane_id = self.ws_lane_id(websocket)
        if lane_id is None:
            return await self.refuse(websocket, WS_NOT_FOUND, "no such lane")
        access = auth.access
        if not access.lane_allowed(lane_id):
            access.trace("lane.terminal", status="denied", lane=lane_id, summary="out of scope")
            return await self.refuse(websocket, WS_DENIED, "this lane is outside your grant")
        envs = self.api.optional("envs")
        if envs is None:
            return await self.refuse(websocket, WS_UNAVAILABLE, "terminals are not configured on this node")
        missing, facts = await self.terminal_facts(envs, lane_id)
        if missing is not None:
            return await self.refuse(websocket, WS_NOT_FOUND, missing)
        try:
            # a shell is exec on this lane: scope, the exec special and (for tokens) its routing
            await access.authorize(Effect.EXEC, action="lane.terminal", lane_id=lane_id,
                                   summary=f"Open an interactive shell in lane {lane_id} from the panel",
                                   content={"shell": lane_id, "via": "panel"}, facts=facts)
        except ApprovalPending as exc:
            return await self.refuse(websocket, WS_PENDING, f"waiting for approval {exc.approval_id}"[:120])
        except AgentosError as exc:
            return await self.refuse(websocket, WS_DENIED, exc.message[:120])
        mode = _mode(websocket)
        cols = _int(websocket.query_params.get("cols"), 80, 10, 500)
        rows = _int(websocket.query_params.get("rows"), 24, 5, 200)
        try:
            session = await maybe_await(envs.open_terminal(lane_id, principal=auth.principal, cols=cols, rows=rows))
        except AgentosError as exc:
            return await self.refuse(websocket, WS_UNAVAILABLE, exc.message[:120])
        await websocket.accept()
        try:
            with self.live.track("terminal", _recheck(self.api, websocket, "terminal", lane_id, auth),
                                 **_keys(auth, lane_id)) as entry:
                await _race(_keys_to_shell(websocket, session, mode), _shell_to_client(websocket, session),
                            entry.wait_closed())
                await _close_ended(websocket, entry)
        finally:
            await maybe_await(session.close())
            if websocket.application_state == WebSocketState.CONNECTED:
                try:
                    await websocket.close(code=1000)
                except (RuntimeError, WebSocketDisconnect):  # the client already left
                    pass


async def _keys_to_shell(websocket: WebSocket, session: Any, mode: str | None) -> None:
    """What the panel types (and its resizes) into a terminal session, until the socket closes."""
    while True:
        msg = await websocket.receive()
        if msg["type"] == "websocket.disconnect":
            return
        text = msg.get("text")
        if text is None and msg.get("bytes") is not None:
            text = msg["bytes"].decode("utf-8", errors="replace")
        if text is None:
            continue
        resize = _resize(text)
        if resize:
            await maybe_await(session.resize(*resize))
        elif mode != VIEW:  # `mode=view` reads the shell and types nothing into it
            await maybe_await(session.write(text))


async def _shell_to_client(websocket: WebSocket, session: Any) -> None:
    """What the shell prints, decoded as UTF-8 across reads, until the session ends."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        data = await maybe_await(session.read())
        if data is None:
            return
        text = decoder.decode(data) if isinstance(data, bytes) else data
        if text:
            await websocket.send_text(text)


def routes(api: Api, *, connect_upstream: Callable[[str, list[str] | None], Any] | None = None) -> list:
    h = Handlers(api)

    del connect_upstream  # the live view now goes through agentos.node.vnc_relay to x11vnc (no websockify)

    return [
        Route("/api/events", endpoint(api, h.events), methods=["GET"]),
        WebSocketRoute("/api/lanes/{ws}/{name}/vnc", h.vnc),
        WebSocketRoute("/api/lanes/{ws}/{name}/terminal", h.terminal),
    ]


async def _pump(websocket: WebSocket, upstream: Any) -> None:
    async def from_client() -> None:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])
            elif msg.get("text") is not None:
                await upstream.send(msg["text"])

    async def from_upstream() -> None:
        async for data in upstream:
            if isinstance(data, bytes):
                await websocket.send_bytes(data)
            else:
                await websocket.send_text(data)

    await _race(from_client(), from_upstream())
    if websocket.application_state == WebSocketState.CONNECTED:
        try:
            await websocket.close(code=1000)
        except (RuntimeError, WebSocketDisconnect):  # the client already left
            pass


async def _close_ended(websocket: WebSocket, entry: live_sockets.LiveEntry) -> None:
    """When access ended (agentos.core.live closed the entry), close the socket with why."""
    if not entry.closed.is_set() or websocket.application_state != WebSocketState.CONNECTED:
        return
    try:
        await websocket.close(code=entry.code or live_sockets.CLOSE_DENIED, reason=entry.reason or "")
    except (RuntimeError, WebSocketDisconnect):  # the client already left
        pass


async def _next_or_closed(get: Callable[[], Any], entry: live_sockets.LiveEntry, timeout: float) -> Any | None:
    """The next event, or None when `timeout` passes or the entry is closed first."""
    getter = asyncio.ensure_future(get())
    closer = asyncio.ensure_future(entry.wait_closed())
    try:
        done, _ = await asyncio.wait({getter, closer}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        closer.cancel()
        if not getter.done():
            getter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await getter
    return getter.result() if getter in done else None


async def _race(*coros: Any) -> None:
    """Run until the first side finishes (or fails), then cancel the rest."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t.cancelled():
                continue
            exc = t.exception()
            if exc is not None and not isinstance(exc, (WebSocketDisconnect, ConnectionError)):
                log.debug("relay side ended: %s", type(exc).__name__)
    finally:
        for t in tasks:
            t.cancel()
        # wait, not gather: if this task is cancelled meanwhile (a server or a test client ending the
        # socket), gather raises a side's own CancelledError, which an anyio cancel scope does not take
        # for its own and passes on as an error
        await asyncio.wait(tasks)
        for t in tasks:
            if not t.cancelled():
                t.exception()  # read, so asyncio does not log it as never retrieved


def _int(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(raw))) if raw else default
    except ValueError:
        return default


def _resize(text: str) -> tuple[int, int] | None:
    if not text.startswith('{"type":"resize"') and not text.startswith('{"type": "resize"'):
        return None
    try:
        msg = json.loads(text)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "resize" or set(msg) != {"type", "cols", "rows"}:
        return None
    try:
        return max(10, min(500, int(msg["cols"]))), max(5, min(200, int(msg["rows"])))
    except (TypeError, ValueError):
        return None
