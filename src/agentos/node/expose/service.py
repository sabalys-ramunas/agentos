"""The node's exposes and forwards: what is open, for which lane, until when, and their routes.

Every link is the owner's (the API needs the owner's device credential or a person: an agent's grant
and the node token are refused, agentos.api.mounts) and lives only while three things hold:

- its hard end (`--for`, at most names.MAX_TTL_S), which the laptop cannot move;
- its lease: the laptop renews it every names.HEARTBEAT_S through the same tunnel, and a link not
  renewed for names.LEASE_S ends (Ctrl-C ended the command, the laptop slept, the tunnel dropped);
- agentosd itself: nothing is kept across a restart, so a link never outlives the process that
  checks it.

`lookup` reads all three on every request, so a request after the end is refused even before the
reaper (every second) has removed the route. Starting and ending are written to the action log
(traces, `expose.start` and `expose.end` with the reason) and to the lane's events.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentos.core.errors import Conflict, InvalidRequest, NotFound
from agentos.node.expose import names
from agentos.node.expose.forward import Forward, lane_dialer
from agentos.node.expose.route import LaneExposeRoute

log = logging.getLogger("agentos.node.expose")

SERVICE_NAME = "expose"
REAP_EVERY_S = 1.0


@dataclass
class Link:
    """One expose (the laptop's port into a browser lane) or forward (a lane's port onto the laptop)."""
    id: str
    kind: str                 # expose | forward
    lane: str
    port: int                 # expose: the port on the owner's computer; forward: the port in the lane
    socket: Path
    by: str
    created_at: float
    expires_at: float
    lease_until: float
    state: str = "waiting"    # waiting (the SSH forward is not up yet) | active | ended
    name: str | None = None
    host: str | None = None
    ended_reason: str | None = None
    forward: Forward | None = None

    def live(self, now: float) -> bool:
        return self.state == "active" and now < self.lease_until and now < self.expires_at

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "lane": self.lane, "port": self.port,
                               "state": self.state, "by": self.by, "created_at": _iso(self.created_at),
                               "expires_at": _iso(self.expires_at), "socket": str(self.socket)}
        if self.host:
            out.update(name=self.name, host=self.host, url=names.origin_for(self.host) + "/")
        if self.ended_reason:
            out["ended_reason"] = self.ended_reason
        return out


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class ExposeService:
    """The registry of links on this node (runtime service `expose`). See the module docstring."""

    def __init__(self, runtime: Any, *, home: Path, clock: Callable[[], float] = time.time,
                 uid: int | None = None):
        self.runtime = runtime
        self.dir = Path(home) / "run" / "expose"
        self.clock = clock
        self.uid = os.geteuid() if uid is None else uid
        self.links: dict[str, Link] = {}
        self._attached: dict[str, tuple[Any, LaneExposeRoute]] = {}  # lane -> (session, its route)
        self._reaper: asyncio.Task | None = None
        self._listening = False

    # ---- lifecycle ---------------------------------------------------------------------------

    def _lanes(self) -> Any:
        return self.runtime.get("lanes") if self.runtime is not None else None

    def _prepare_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        for leftover in self.dir.glob("*.sock"):  # a previous run's: nothing serves or routes them now
            with contextlib.suppress(OSError):
                leftover.unlink()

    async def start(self) -> None:
        await asyncio.to_thread(self._prepare_dir)
        lanes = self._lanes()
        add = getattr(lanes, "add_session_listener", None)
        if callable(add) and not self._listening:
            add(self.on_session)
            self._listening = True
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop(), name="agentos-expose-reaper")

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reaper
            self._reaper = None
        for link in list(self.links.values()):
            if link.state != "ended":
                await self._end(link, "the node stopped")

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(REAP_EVERY_S)
            try:
                await self.reap()
            except Exception:
                log.exception("the expose reaper failed")

    # ---- what the API does -------------------------------------------------------------------

    def _check_room(self, lane: str) -> None:
        open_ = [x for x in self.links.values() if x.state != "ended"]
        if len(open_) >= names.MAX_TOTAL:
            raise Conflict(f"this node already has {names.MAX_TOTAL} exposes and forwards open; end one first")
        if sum(1 for x in open_ if x.lane == lane) >= names.MAX_PER_LANE:
            raise Conflict(f"{lane} already has {names.MAX_PER_LANE} exposes and forwards open; end one first")

    def _new(self, kind: str, lane: str, port: int, ttl_s: float, by: str, **extra: Any) -> Link:
        now = self.clock()
        link_id = secrets.token_hex(8)
        self.dir.mkdir(parents=True, exist_ok=True)
        return Link(id=link_id, kind=kind, lane=lane, port=port, socket=self.dir / f"{link_id}.sock", by=by,
                    created_at=now, expires_at=now + ttl_s, lease_until=now + names.READY_WITHIN_S, **extra)

    async def create_expose(self, lane: str, *, name: str, port: object, ttl_s: object = None,
                            by: str = "owner") -> dict[str, Any]:
        """Reserve `<name>.expose.agentos.invalid` on a browser lane and the socket path the laptop's
        `ssh -R` binds. Nothing is routed until `ready` finds that socket."""
        host = names.host_for(name)
        port_n, ttl = names.validate_port(port), names.validate_ttl(ttl_s)
        lanes = self._lanes()
        if lanes is None:
            raise InvalidRequest("this node has no browser lanes")
        info = lanes.get(lane)  # LaneNotFound
        if str(getattr(info.spec.kind, "value", info.spec.kind)) != "browser":
            raise InvalidRequest(f"{lane} is not a browser lane; a dev server is exposed to a browser lane "
                                 "(to see a port of a terminal or service lane, use `agentos forward`)")
        await self.reap()
        if any(x.host == host and x.lane == lane and x.state != "ended" for x in self.links.values()):
            raise Conflict(f"{lane} already exposes {host}; end that one first or choose another --name")
        self._check_room(lane)
        link = self._new("expose", lane, port_n, ttl, by, name=names.validate_name(name), host=host)
        self.links[link.id] = link
        out = link.public()
        out.update(owner_uid=self.uid, lease_s=names.LEASE_S, heartbeat_s=names.HEARTBEAT_S)
        return out

    async def create_forward(self, lane: str, *, port: object, ttl_s: object = None,
                             by: str = "owner") -> dict[str, Any]:
        """Open the socket that carries the laptop's `ssh -L` into a terminal or service lane's port."""
        port_n, ttl = names.validate_port(port), names.validate_ttl(ttl_s)
        await self.reap()
        self._check_room(lane)
        dial = await lane_dialer(self.runtime.get("envs") if self.runtime else None, lane)
        link = self._new("forward", lane, port_n, ttl, by)
        fwd = Forward(link.socket, port_n, dial)
        await fwd.start()
        link.forward, link.state = fwd, "active"
        self.links[link.id] = link
        self._trace("expose.start", link, f"forward port {port_n} of {lane} to the owner's computer")
        out = link.public()
        out.update(lease_s=names.LEASE_S, heartbeat_s=names.HEARTBEAT_S)
        return out

    def _get(self, link_id: str) -> Link:
        link = self.links.get(link_id)
        if link is None:
            raise NotFound("no such expose or forward on this node (it ended, or agentosd restarted)")
        if link.state == "ended":
            raise NotFound(f"it ended: {link.ended_reason or 'ended'}")
        return link

    async def ready(self, link_id: str) -> dict[str, Any]:
        """The laptop's SSH forward is up: check its socket, then route the host in the lane."""
        link = self._get(link_id)
        if link.kind != "expose":
            return link.public()
        if link.state == "active":
            return link.public()
        await asyncio.to_thread(self._check_socket, link.socket)
        now = self.clock()
        link.state, link.lease_until = "active", min(now + names.LEASE_S, link.expires_at)
        self._trace("expose.start", link, f"expose the owner's port {link.port} to {link.lane} as {link.host}")
        self._lane_event(link, "expose_started")
        lanes = self._lanes()
        session = getattr(lanes, "live_session", lambda _lane: None)(link.lane)
        routed = await self._attach(link.lane, session) if session is not None else False
        out = link.public()
        out["routed"] = routed
        return out

    def _check_socket(self, path: Path) -> None:
        """The socket sshd bound: a socket, agentosd's user's, and no one else's to open."""
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise InvalidRequest("the SSH forward's socket is not on the node yet") from None
        if not stat.S_ISSOCK(st.st_mode):
            raise InvalidRequest("the SSH forward's path on the node is not a socket")
        if st.st_uid != self.uid:
            raise InvalidRequest(f"the SSH forward's socket belongs to uid {st.st_uid}, not agentosd's user "
                                 f"({self.uid}); sign in over SSH as root or as that user")
        if st.st_mode & 0o077:
            os.chmod(path, 0o600)

    def heartbeat(self, link_id: str) -> dict[str, Any]:
        """The laptop still holds the link: renew its lease (never past its hard end)."""
        link = self._get(link_id)
        now = self.clock()
        if now >= link.expires_at:
            raise NotFound("it ended: its time is up")
        if link.state == "waiting":
            return link.public()
        if now >= link.lease_until:
            raise NotFound("it ended: the computer stopped renewing it")
        link.lease_until = min(now + names.LEASE_S, link.expires_at)
        return link.public()

    async def end(self, link_id: str, *, reason: str = "the owner ended it") -> dict[str, Any]:
        link = self.links.get(link_id)
        if link is None:
            raise NotFound("no such expose or forward on this node (it ended, or agentosd restarted)")
        if link.state != "ended":
            await self._end(link, reason)
        return link.public()

    async def reap(self, now: float | None = None) -> list[str]:
        """End every link past its hard end or lease (or never made ready). Returns their ids."""
        now = self.clock() if now is None else now
        ended = []
        for link in list(self.links.values()):
            if link.state == "ended":
                if now - link.expires_at > 3600:
                    self.links.pop(link.id, None)  # kept a while, so a late heartbeat hears why
                continue
            if now >= link.expires_at:
                reason = "its time is up"
            elif now >= link.lease_until:
                reason = ("the SSH forward never came up" if link.state == "waiting"
                          else "the computer stopped renewing it (Ctrl-C, sleep, or the tunnel dropped)")
            else:
                continue
            await self._end(link, reason)
            ended.append(link.id)
        return ended

    async def _end(self, link: Link, reason: str) -> None:
        was = link.state
        link.state, link.ended_reason = "ended", reason
        link.expires_at = min(link.expires_at, self.clock())
        if link.forward is not None:
            await link.forward.close()
            link.forward = None
        if link.kind == "expose" and not any(x.live(self.clock()) for x in self.links.values()
                                             if x.lane == link.lane and x.kind == "expose"):
            await self._detach(link.lane)
        if link.kind == "expose":
            with contextlib.suppress(OSError):
                link.socket.unlink()
        if was != "waiting" or link.kind == "forward":
            self._trace("expose.end", link, f"ended {link.kind} {link.host or link.port} on {link.lane}: {reason}")
            if link.kind == "expose":  # a browser lane's events (a forward's lane is a terminal or service lane)
                self._lane_event(link, "expose_ended", reason=reason)

    # ---- what lanes and the guard read -------------------------------------------------------

    def lookup(self, lane: str, host: str) -> Link | None:
        """The live expose of `host` on `lane`, or None (checked on every request)."""
        now = self.clock()
        for link in self.links.values():
            if link.kind == "expose" and link.lane == lane and link.host == host and link.live(now):
                return link
        return None

    def hosts(self, lane: str) -> frozenset[str]:
        now = self.clock()
        return frozenset(x.host for x in self.links.values()
                         if x.kind == "expose" and x.lane == lane and x.host and x.live(now))

    def lane_notes(self, lane: str) -> list[dict[str, Any]]:
        """What an agent may know about the lane's exposes (lanes_list): the address and the end."""
        now = self.clock()
        return [{"url": names.origin_for(x.host) + "/", "until": _iso(x.expires_at),
                 "note": "the owner's dev server, open in this lane only while the owner keeps it open"}
                for x in self.links.values()
                if x.kind == "expose" and x.lane == lane and x.host and x.live(now)]

    def list(self) -> list[dict[str, Any]]:
        return [x.public() for x in self.links.values() if x.state != "ended"]

    # ---- the lane's browser --------------------------------------------------------------------

    async def on_session(self, lane_id: str, session: Any) -> None:
        """Lane manager session listener: bind the guard, and route a live expose in the new browser."""
        guard = getattr(session, "guard", None)
        if guard is not None:
            guard.exposed = lambda: self.hosts(lane_id)
        if self.hosts(lane_id):
            await self._attach(lane_id, session)

    async def _attach(self, lane_id: str, session: Any) -> bool:
        """Route the lane's exposes in this browser (once per browser). False: it cannot be routed."""
        current = self._attached.get(lane_id)
        if current is not None and current[0] is session:
            return True
        if current is not None:
            await self._detach(lane_id)
        routes = getattr(session, "routes", None)
        context = getattr(routes, "context", None) or getattr(session, "context", None)
        if routes is None or context is None:
            log.warning("%s: this browser has no route dispatcher, so the expose is not routed", lane_id)
            return False
        guard = getattr(session, "guard", None)
        if guard is not None:
            guard.exposed = lambda: self.hosts(lane_id)
        lane_route = LaneExposeRoute(lane_id, lookup=self.lookup, context=context,
                                     on_event=lambda kind, detail: self._event(lane_id, kind, detail))
        await routes.route(names.URL_RE, lane_route.handle)
        context.on("request", lane_route.on_request)
        self._attached[lane_id] = (session, lane_route)
        return True

    async def _detach(self, lane_id: str) -> None:
        current = self._attached.pop(lane_id, None)
        if current is None:
            return
        session, lane_route = current
        routes: Any = getattr(session, "routes", None)
        if routes is not None:
            with contextlib.suppress(Exception):  # a browser that closed took its routes with it
                await routes.unroute(names.URL_RE, lane_route.handle)
        context: Any = getattr(routes, "context", None) or getattr(session, "context", None)
        if context is not None:
            with contextlib.suppress(Exception):
                context.remove_listener("request", lane_route.on_request)

    # ---- records -------------------------------------------------------------------------------

    def _event(self, lane_id: str, kind: str, detail: dict[str, Any]) -> None:
        event = getattr(self._lanes(), "_event", None)
        if callable(event):
            with contextlib.suppress(Exception):
                event(lane_id, kind, detail)

    def _lane_event(self, link: Link, kind: str, **extra: Any) -> None:
        detail = {"id": link.id, "port": link.port, "by": link.by, "until": _iso(link.expires_at), **extra}
        if link.host:
            detail["host"] = link.host
        self._event(link.lane, kind, detail)

    def _trace(self, tool: str, link: Link, summary: str) -> None:
        traces = self.runtime.get("traces") if self.runtime is not None else None
        write = getattr(traces, "write", None)
        if not callable(write):
            return
        try:
            from agentos.policy.traces import TraceEvent

            write(TraceEvent(kind="expose", tool=tool, principal=link.by[:80], principal_kind="owner",
                             lane=link.lane, workspace=link.lane.split("/", 1)[0], status="ok",
                             summary=summary[:500],
                             args={"id": link.id, "kind": link.kind, "port": link.port, "host": link.host,
                                   "until": _iso(link.expires_at), "reason": link.ended_reason}))
        except Exception:
            log.warning("expose: could not write the %s trace", tool, exc_info=True)


def build(runtime: Any) -> ExposeService:
    cfg = runtime.get("config") if runtime is not None else None
    if cfg is None:
        from agentos.core.config import home_from_env

        home = home_from_env()
    else:
        home = cfg.home
    service = ExposeService(runtime, home=home)
    runtime.register(SERVICE_NAME, service)
    return service
