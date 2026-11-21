"""The node end of the controller channel (docs/PROTOCOL.md v1).

One outbound WebSocket, kept up with backoff and jitter. On each connection the controller
sends `challenge`; the node answers `hello` signed with its node key, and gets `welcome`.
Then, concurrently: a reader for everything the controller sends, a keepalive (ping every
20 s, dead after 60 s with no frame and no pong), heartbeats (every 15 s and on any lane
change), and a sender that delivers the persistent outbox in `seq` order from the oldest
unacknowledged item. Independently of the connection, a trace flusher, an approval watcher
and a lane watcher write into the outbox, so nothing is lost while the controller is away.

`invoke` is at-most-once per call_id: a retry while the call runs joins it, a retry later
gets the cached result (5 minutes). Every call runs through the node's own MCP wrapper with
the invoke's grant token bound (see invoke.py), so the grant is verified here whatever the
controller says. Every controller-signed kind (wire.SIGNED_TYPES) must carry a valid signature by the
controller key pinned at enrollment, a fresh `ts` and a nonce not seen before (`_signed`); anything else is
refused and changes nothing. `standing` delivers the owner's standing approvals from the controller's
panel (standing.py): each is checked, bounded by the controller ceiling and stored with its issuer, and
the node reports each use back as a `standing` event.

The owner's controller ceiling (openajar.policy.ceiling, node.toml) bounds what the controller
may do here: the policy service clamps its grants, `lane_control` `take` needs control,
`stream_open` needs its kind, and `approval_decision` may approve only an action within it (a
denial always passes). The node reports the ceiling in `hello`.

Time: the node learns the controller's clock from `welcome.server_time` and judges what the
controller dated (call deadlines, per-call grants, signed `ts`) on that clock, so a node whose
own clock is off still runs the calls the controller has time for. The offset is bounded and
logged when large.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI, WebSocketException

from openajar import __version__
from openajar.core.config import NodeConfig
from openajar.core.errors import EngineError, InvalidRequest, OpenajarError, ServiceUnavailable
from openajar.node.channel import bridge, node_streams, reports, standing, usage, wire, workflows
from openajar.node.channel.approvals import ApprovalsMixin
from openajar.node.channel.calls import Call, CallsMixin
from openajar.node.channel.connection import Backoff, Connection, Timings
from openajar.node.channel.identity import NodeKey
from openajar.node.channel.invoke import failure
from openajar.node.channel.outbox import OutboxMixin
from openajar.node.channel.signed import SignedMixin
from openajar.node.channel.store import ChannelStore
from openajar.node.channel.stream_messages import StreamMessagesMixin
from openajar.policy.ceiling import ControllerCeiling
from openajar.runner.workflows.mirror import mirror

# Names other packages import from here; they live in the modules above.
__all__ = [
    "Backoff",
    "Connection",
    "Timings",
]


log = logging.getLogger("openajar.channel")
INTERRUPTED = "the node restarted while this call was running, so its outcome is unknown"
CLOCK_NOISE = 2.0          # an offset this small is the handshake's own latency and rounding, not skew
CLOCK_WARN = 30.0          # an offset past this is logged as a warning
MAX_CLOCK_OFFSET = 3600.0  # never trust the controller's clock further than this from our own


class HandshakeFailed(Exception):
    """Authentication did not complete on this connection; the node backs off and dials again."""


class NodeChannel(SignedMixin, ApprovalsMixin, OutboxMixin, CallsMixin, StreamMessagesMixin):
    """Runtime service "channel"."""

    def __init__(self, config: NodeConfig, runtime: Any, *, issuers: Any = None, timings: Timings | None = None,
                 store: ChannelStore | None = None, key: NodeKey | None = None):
        if not config.enrolled:
            raise ValueError("this node has not joined a controller; run `openajar node join`")
        self.config = config
        self.runtime = runtime
        self.issuers = issuers
        self.timings = timings or Timings()
        self.url = wire.connect_url(config.controller_url)
        self.store = store or ChannelStore.for_home(config.home)
        self.key = key or NodeKey.load_or_create(config.secrets_dir)
        self.ceiling = ControllerCeiling.from_config(config)
        self.issuer = standing.org_issuer(config)
        self._standing_reply: wire.Envelope | None = None  # the answer to welcome.standing, sent once online
        self._hook_gate: workflows.NodeHookGate | None = None  # made on the first relayed webhook
        self._standing_synced = False  # this session's controller sent its whole set (welcome.standing or set)
        self.terminal_holds = node_streams.TerminalHolds(config.control_idle_seconds)
        self._stream_closers: set[asyncio.Task] = set()  # stream_close sends for streams a freeze ended
        self.version = __version__
        self.conn: Connection | None = None
        self.connected = False
        self.calls: dict[str, Call] = {}
        self.resumable: dict[str, Call] = {}  # approval-pending calls a same-call_id retry may resume
        self.last_error: str | None = None
        self.sessions = 0
        self.degraded_since: str | None = self.store.get_meta("degraded_since")
        self.controller_config: dict = self.store.get_meta("controller_config", {}) or {}
        self.clock_offset = 0.0  # the controller's clock minus ours, from the last welcome
        self._policy_clock: tuple[Any, Callable[[], float]] | None = None
        self._recovery_seq: int | None = None
        self._backoff = Backoff(self.timings.backoff_min, self.timings.backoff_max)
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._outbox_wake = asyncio.Event()
        self._beat_wake = asyncio.Event()
        self._lane_wake = asyncio.Event()
        self.welcomed = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._traces: list[dict] = []
        self._unsubscribe: Callable[[], None] | None = None
        self._lanes_seen: dict[str, tuple] = {}
        self._control = None
        self._started = time.time()
        self._throttle = reports.Throttle(self.timings.report_min)
        self._reports_cut: set[str] = set()  # kinds logged once as too large
        self._report_wake = asyncio.Event()
        self._doctor: list[dict] | None = None
        self._runs_sent: dict[str, str] = {}
        self._run_effects: dict = {}
        self._usage = usage.LaneUsage()
        self._interrupted_calls()
        if issuers is not None:
            issuers.connected = lambda: self.connected

    # ---- lifecycle ----
    @property
    def node_id(self) -> str:
        return self.config.node_id

    @property
    def degraded(self) -> bool:
        return self.degraded_since is not None

    def status(self) -> dict:
        return {"controller_url": self.url, "node_id": self.node_id, "connected": self.connected,
                "degraded": self.degraded, "degraded_since": self.degraded_since, "outbox": self.store.depth(),
                "high_seq": self.store.high_seq, "sessions": self.sessions, "last_error": self.last_error}

    async def start(self) -> None:
        if self._tasks:
            return
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        policy = self.runtime.get("policy")
        bus = getattr(policy, "bus", None)
        if bus is not None:
            self._unsubscribe = bus.subscribe(self._on_bus)
        base = getattr(policy, "clock", None)
        if callable(base) and self._policy_clock is None:
            # the grants a controller sends are dated on its clock, so the policy judges them on it too
            self._policy_clock = (policy, base)
            policy.clock = lambda: base() + self.clock_offset
        lanes = self.runtime.get("lanes")
        if lanes is not None and callable(getattr(lanes, "add_control_listener", None)):
            lanes.add_control_listener(self._control_changed)
        self._control = node_streams.ControlView(lanes) if lanes is not None else None
        self._lanes_seen = {lane["id"]: bridge.lane_digest(lane) for lane in self._all_lanes()}
        runner = self.runtime.get("runner")
        if callable(getattr(runner, "subscribe", None)) and callable(getattr(runner, "describe", None)):
            runner.subscribe(self._on_run)
        self._throttle.min_interval = self.timings.report_min
        self._announce_pending_approvals()
        self._tasks = [asyncio.create_task(coro, name=name) for name, coro in (
            ("channel", self._run()), ("channel-traces", self._flush_traces()),
            ("channel-lanes", self._watch_lanes()), ("channel-housekeeping", self._housekeeping()),
            ("channel-reports", self._watch_reports()), ("channel-doctor", self._run_doctor()))]
        log.info("channel to %s started (node %s)", self.url, self.node_id)

    def _control_changed(self, *_args: Any) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._lane_wake.set)

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._policy_clock is not None:
            policy, base = self._policy_clock
            policy.clock = base
            self._policy_clock = None
        conn = self.conn
        if conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(conn.ws.close(), 2)
        calls = [call.task for call in self.calls.values() if call.task is not None]
        for task in [*self._tasks, *calls]:
            task.cancel()
        await asyncio.gather(*self._tasks, *calls, return_exceptions=True)
        self._tasks = []
        self._flush_trace_buffer()

    def close(self) -> None:
        self.store.close()

    def _interrupted_calls(self) -> None:
        """A call that was running when the process died must not run again on a retry."""
        for call_id in self.store.running_calls():
            self.store.put_call(call_id, "done", result=failure(ServiceUnavailable.code, INTERRUPTED))

    # ---- the connection loop ----
    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, WebSocketException, HandshakeFailed, wire.WireError, InvalidURI,
                    InvalidHandshake) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.info("channel: connection ended: %s", self.last_error)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.exception("channel: unexpected failure")
            if self._stop.is_set():
                break
            await self._pause(self._backoff.next())

    async def _pause(self, seconds: float) -> None:
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._wake.wait()

    def _dial(self) -> Any:
        return connect(self.url, max_size=wire.HARD_FRAME_BYTES, ping_interval=None, ping_timeout=None,
                       compression=None, open_timeout=self.timings.handshake, close_timeout=2, proxy=None,
                       user_agent_header=f"openajar-node/{self.version}")

    async def _session(self) -> None:
        async with self._dial() as ws:
            await self._authenticate(ws)
            conn = Connection(ws)
            self.conn = conn
            self._backoff.reset()
            await self._on_online(conn)
            tasks = [asyncio.create_task(coro, name=name) for name, coro in (
                ("channel-reader", self._reader(conn)), ("channel-keepalive", self._keepalive(conn)),
                ("channel-heartbeat", self._heartbeats(conn)), ("channel-sender", self._sender(conn)))]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    error = task.exception()
                    if error is not None and not isinstance(error, ConnectionClosed | OSError):
                        log.error("channel: %s failed", task.get_name(), exc_info=error)
            finally:
                self.conn = None
                self._on_offline()
                conn.streams.close_all()
                for task in [*tasks, *conn.tasks]:
                    task.cancel()
                await asyncio.gather(*tasks, *conn.tasks, return_exceptions=True)

    async def _frame(self, ws: ClientConnection) -> wire.Envelope:
        async with asyncio.timeout(self.timings.handshake):
            while True:
                frame = await ws.recv()
                if isinstance(frame, str):
                    return wire.decode(frame)

    async def _authenticate(self, ws: ClientConnection) -> None:
        while True:
            message = await self._frame(ws)
            if message.type == "challenge":
                nonce = message.body.get("nonce")
                if not isinstance(nonce, str) or not 8 <= len(nonce) <= 200:
                    raise HandshakeFailed("the challenge has no usable nonce")
                hello = {"node_id": self.node_id, "nonce": nonce, "sig": self.key.sign_hello(nonce, self.node_id),
                         "epoch": self.store.epoch, "ceiling": self.ceiling.report(), "supports": self._supports()}
                await ws.send(wire.Envelope("hello", hello).encode())
            elif message.type == "welcome":
                if message.body.get("node_id") not in (None, self.node_id):
                    raise HandshakeFailed("the welcome is for another node")
                self._apply_welcome(message.body, message.id)
                return
            elif message.type == "error":
                code, text = message.body.get("code"), message.body.get("message")
                raise HandshakeFailed(f"the controller refused this node: {code}: {text}"[:300])
            else:
                log.debug("channel: ignored %s before welcome", message.type)

    def _apply_welcome(self, body: wire.Json, welcome_id: str | None = None) -> None:
        server_time = wire.parse_time(body.get("server_time"))
        if server_time is not None:
            self._set_clock_offset(server_time - time.time())
        config = body.get("config")
        if isinstance(config, dict):
            self._set_config(config, replace=True)
        revoked = body.get("revoked_grants")
        if isinstance(revoked, list):
            self._apply_revocations([r for r in revoked if isinstance(r, str)])
        mesh = body.get("mesh_freeze")  # additive, signed: applied before anything the controller sends runs
        if isinstance(mesh, dict):
            try:
                self._apply_mesh_freeze(mesh)
            except OpenajarError as exc:
                log.warning("channel: refused the mesh freeze state in the welcome: %s", exc.message)
        held = body.get("standing")  # additive, signed: the whole set this controller wants the node to hold
        if isinstance(held, dict):
            try:
                result = self._apply_standing(held)
                self._standing_reply = wire.Envelope("standing_result", result, re=welcome_id)
                self._standing_synced = held.get("set") is not None
            except OpenajarError as exc:
                log.warning("channel: refused the standing approvals in the welcome: %s", exc.message)
                self._standing_reply = wire.error_envelope(exc.code, exc.message, welcome_id)

    def _set_clock_offset(self, offset: float) -> None:
        if not math.isfinite(offset):  # NaN would make every freshness check pass
            log.error("channel: the controller sent an unreadable time; keeping the clock offset at %.0f s",
                      self.clock_offset)
            return
        if abs(offset) < CLOCK_NOISE:
            offset = 0.0
        elif abs(offset) > MAX_CLOCK_OFFSET:
            log.error("channel: the controller's clock is %.0f s from this node's; trusting only %.0f s of it. "
                      "Fix the clock on one of them", offset, MAX_CLOCK_OFFSET)
            offset = max(-MAX_CLOCK_OFFSET, min(MAX_CLOCK_OFFSET, offset))
        elif abs(offset) > CLOCK_WARN:
            log.warning("channel: the controller's clock is %.0f s from this node's; deadlines follow the "
                        "controller's", offset)
        self.clock_offset = offset

    def controller_time(self) -> float:
        """Now, on the controller's clock (as far as the last welcome told)."""
        return time.time() + self.clock_offset

    async def _on_online(self, conn: Connection) -> None:
        self.connected = True
        self.sessions += 1
        reply, self._standing_reply = self._standing_reply, None
        if reply is not None:
            await conn.send(reply)
        self._confirm_standing()
        self._recovery_seq = self.store.high_seq if self.degraded else None
        try:
            tools = await bridge.tool_defs(self.runtime.get("mcp"))
        except Exception:
            log.exception("channel: could not list tools for the controller")
            tools = []
        self._enqueue_tools(tools)
        try:  # additive (docs/PROTOCOL.md): the instructions and prompts the controller serves with the tools
            self._enqueue_event("node", {"guide": await bridge.guide(self.runtime.get("mcp"))})
        except Exception:
            log.exception("channel: could not send the node's instructions to the controller")
        for lane in self._all_lanes():
            self._enqueue_event("lane", lane)
        self._throttle.force()
        self._report_wake.set()
        self.welcomed.set()
        self._outbox_wake.set()
        log.info("channel: online as %s", self.node_id)

    def _on_offline(self) -> None:
        self.connected = False
        self._standing_synced = False
        self.welcomed.clear()
        if self.degraded_since is None and not self._stop.is_set():
            self.degraded_since = wire.iso()
            self.store.set_meta("degraded_since", self.degraded_since)
            self._enqueue_event("node", {"degraded": True, "since": self.degraded_since})
        log.info("channel: offline")

    def _maybe_recover(self) -> None:
        """Back to normal once everything buffered while offline has been acknowledged."""
        if self._recovery_seq is None or not self.degraded:
            return
        oldest = self.store.oldest_seq()
        if oldest is not None and oldest <= self._recovery_seq:
            return
        since = self.degraded_since
        self.degraded_since = None
        self._recovery_seq = None
        self.store.set_meta("degraded_since", None)
        self._enqueue_event("node", {"degraded": False, "since": since})
        self._beat_wake.set()

    # ---- loops on a connection ----
    async def _keepalive(self, conn: Connection) -> None:
        def seen(_: Any) -> None:
            conn.last_rx = time.monotonic()

        next_ping = time.monotonic() + self.timings.ping_interval
        while True:
            now = time.monotonic()
            if now - conn.last_rx >= self.timings.dead_after:
                log.warning("channel: nothing from the controller for %.0f s; the connection is dead",
                            now - conn.last_rx)
                if conn.ws.transport is not None:
                    conn.ws.transport.abort()
                return
            if now >= next_ping:
                waiter = await conn.ws.ping()
                asyncio.ensure_future(waiter).add_done_callback(seen)
                next_ping = now + self.timings.ping_interval
            wait = min(next_ping, conn.last_rx + self.timings.dead_after) - time.monotonic()
            await asyncio.sleep(max(0.01, wait))

    async def lane_usage(self) -> dict[str, dict]:
        """What each lane uses, read from /proc in a thread (a directory scan is no work for the loop)."""
        try:
            return await asyncio.to_thread(self._usage.sample, self.runtime)
        except Exception:  # noqa: BLE001  (what a lane uses is best effort; the heartbeat is not)
            return {}

    def heartbeat_body(self, used: dict[str, dict] | None = None) -> dict:
        lanes = self._all_lanes()
        used = used or {}
        return {"load": bridge.load(self.config.home),
                "lanes": [bridge.heartbeat_lane(lane, used.get(lane["id"])) for lane in lanes],
                "degraded": self.degraded, "version": self.version}

    async def _heartbeats(self, conn: Connection) -> None:
        while True:
            self._beat_wake.clear()
            await conn.send(wire.Envelope("heartbeat", self.heartbeat_body(await self.lane_usage())))
            interval = float(self.controller_config.get("heartbeat_seconds") or self.timings.heartbeat)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(interval):
                    await self._beat_wake.wait()

    async def _sender(self, conn: Connection) -> None:
        """The outbox in seq order; a new connection starts from the oldest unacknowledged item."""
        while True:
            self._outbox_wake.clear()
            rows = self.store.outbox_after(conn.sent_seq)
            if not rows:
                await self._outbox_wake.wait()
                continue
            for seq, frame in rows:
                await conn.send_raw(frame)
                conn.sent_seq = seq

    async def _reader(self, conn: Connection) -> None:
        async for frame in conn.ws:
            conn.last_rx = time.monotonic()
            if isinstance(frame, bytes):
                await conn.streams.on_binary(frame)
                continue
            try:
                message = wire.decode(frame)
            except wire.WireError as exc:
                await conn.send(wire.error_envelope(exc.code, exc.message, exc.re))
                continue
            handler = self._handlers().get(message.type)
            if handler is None:
                await conn.send(wire.error_envelope("unsupported", f"unknown message type {message.type[:40]!r}",
                                                    message.id or None))
                continue
            key = self._off_loop_key(message)
            if key is None:
                await self._dispatch(conn, message, handler)
            else:
                self._off_loop(conn, key, message, handler)

    async def _dispatch(self, conn: Connection, message: wire.Envelope,
                        handler: Callable[[Connection, wire.Envelope], Awaitable[None]]) -> None:
        """One message. Whatever goes wrong fails that message only (an `error` back to the controller),
        never the reader and with it the session and every open stream."""
        try:
            await handler(conn, message)
        except OpenajarError as exc:
            await conn.send(wire.error_envelope(exc.code, exc.message, message.id or None))
        except (ValueError, TypeError, KeyError) as exc:
            await conn.send(wire.error_envelope("invalid", str(exc)[:300] or "invalid message", message.id or None))
        except ConnectionClosed:
            raise
        except Exception:
            log.exception("channel: handling a %s failed", message.type[:40])
            await conn.send(wire.error_envelope(EngineError.code, f"the node could not handle this "
                                                f"{message.type[:40]}; it was not applied", message.id or None))

    @staticmethod
    def _off_loop_key(message: wire.Envelope) -> str | None:
        """Handlers that can take seconds run beside the reader, so heartbeats, acks, credit and stream
        frames keep flowing. Messages with the same key still run one at a time, in arrival order: a
        `take` then a `hand_back` on one lane apply in that order."""
        if message.type == "lane_control":
            lane = message.body.get("lane")
            return f"lane:{lane if isinstance(lane, str) else ''}"
        if message.type in ("workflow", "hook_relay", "unsubscribe_relay"):
            return message.type
        return None

    def _off_loop(self, conn: Connection, key: str, message: wire.Envelope,
                  handler: Callable[[Connection, wire.Envelope], Awaitable[None]]) -> None:
        before = conn.chains.get(key)

        async def run() -> None:
            if before is not None and not before.done():
                await asyncio.wait([before])  # its outcome is its own; only the order matters here
            await self._dispatch(conn, message, handler)

        task = asyncio.create_task(run(), name=f"channel-{message.type[:30]}")
        conn.chains[key] = task
        conn.tasks.add(task)

        def done(t: asyncio.Task) -> None:
            conn.tasks.discard(t)
            if conn.chains.get(key) is t:
                conn.chains.pop(key, None)
            if not t.cancelled() and t.exception() is not None and not isinstance(t.exception(), ConnectionClosed):
                log.error("channel: a %s failed", message.type[:40], exc_info=t.exception())

        task.add_done_callback(done)

    def _handlers(self) -> dict[str, Callable[[Connection, wire.Envelope], Awaitable[None]]]:
        return {
            "invoke": self._on_invoke, "cancel": self._on_cancel, "ack": self._on_ack,
            "approval_decision": self._on_decision, "revoke": self._on_revoke,
            "config_update": self._on_config_update, "lane_control": self._on_lane_control,
            "mesh_freeze": self._on_mesh_freeze, "standing": self._on_standing, "ask_first": self._on_ask_first,
            "workflow": self._on_workflow, "hook_relay": self._on_hook_relay,
            "unsubscribe_relay": self._on_unsubscribe_relay,
            "stream_open": self._on_stream_open, "stream_close": self._on_stream_close,
            "stream_credit": self._on_stream_credit, "resize": self._on_resize, "error": self._on_peer_error,
            "challenge": self._ignore, "welcome": self._ignore, "enrolled": self._ignore,
            "audit_anchor_receipt": self._on_anchor_reply, "audit_anchors_result": self._on_anchor_reply,
        }

    def envelope(self, kind: str, body: wire.Json) -> wire.Envelope:
        """A new message for the controller, to send on `conn`: the anchors service (openajar.node.anchors,
        a layer below the channel) builds its requests here, and waits for the reply by its id."""
        return wire.Envelope(kind, body)

    async def _on_anchor_reply(self, conn: Connection, message: wire.Envelope) -> None:
        """A controller's answer to the anchors service (openajar.node.anchors)."""
        anchors = self.runtime.get("anchors")
        if anchors is None or not anchors.on_reply(message):
            log.debug("channel: %s answers nothing waiting", message.type)

    async def _ignore(self, conn: Connection, message: wire.Envelope) -> None:
        log.debug("channel: ignored %s after welcome", message.type)

    async def _on_peer_error(self, conn: Connection, message: wire.Envelope) -> None:
        anchors = self.runtime.get("anchors")
        if anchors is not None and anchors.on_reply(message):
            return  # an older controller that does not know audit_anchor yet: the anchors service backs off
        log.warning("channel: controller error: %s: %s", str(message.body.get("code"))[:40],
                    str(message.body.get("message"))[:200])

    async def _on_ack(self, conn: Connection, message: wire.Envelope) -> None:
        upto = message.body.get("upto_seq")
        if not isinstance(upto, int) or isinstance(upto, bool) or upto < 0:
            raise InvalidRequest("ack needs an integer upto_seq")
        # never trim past what this connection delivered: a bogus ack must not lose items
        self.store.trim(min(upto, conn.sent_seq))
        self._maybe_recover()

    # ---- workflows (docs/WORKFLOWS.md) ----
    async def _on_workflow(self, conn: Connection, message: wire.Envelope) -> None:
        """A person's action on a workflow, relayed and signed: answered with `workflow_result`, or
        `error` when the body is refused (a bad signature, a stale `ts`, a seen nonce)."""
        service = self.runtime.get("workflows")
        if service is None:
            await conn.send(wire.error_envelope("unsupported", "this node has no workflows", message.id or None))
            return
        self._signed("workflow", message.body)
        sync = workflows.WorkflowSync(service, get_meta=self.store.get_meta, set_meta=self.store.set_meta)
        result = sync.apply(sync.check_body(message.body), message.body)
        await conn.send(wire.Envelope("workflow_result", result, re=message.id or None))

    async def _on_hook_relay(self, conn: Connection, message: wire.Envelope) -> None:
        """An inbound webhook, relayed byte for byte. The sender's signature is what is checked, here."""
        service = self.runtime.get("workflows")
        if service is None:
            await conn.send(wire.error_envelope("unsupported", "this node has no workflows", message.id or None))
            return
        if self._hook_gate is None:
            self._hook_gate = workflows.NodeHookGate(waiting=service.hook_backlog)
        result = await workflows.relay_hook(service.runner.scheduler, message.body, self._hook_gate)
        await conn.send(wire.Envelope("hook_relay_result", result, re=message.id or None))

    async def _on_unsubscribe_relay(self, conn: Connection, message: wire.Envelope) -> None:
        service = self.runtime.get("workflows")
        if service is None:
            await conn.send(wire.error_envelope("unsupported", "this node has no workflows", message.id or None))
            return
        result = workflows.relay_unsubscribe(service.sends, message.body)
        await conn.send(wire.Envelope("unsubscribe_relay_result", result, re=message.id or None))

    def unsubscribe_base(self) -> str | None:
        """Where an unsubscribe link points: the controller, which relays the token to this node."""
        if not self.node_id:
            return None
        parts = urlsplit(self.url)
        scheme = "https" if parts.scheme == "wss" else "http"
        return f"{scheme}://{parts.netloc}/u/{self.node_id}"

    def _supports(self) -> list[str]:
        """`hello.supports`: the additive messages this node applies."""
        return ["standing", *(["ask_first"] if self._ask_first() is not None else []),
                *(["workflows"] if self.runtime.get("workflows") is not None else []),
                *(["confirmations"] if self.runtime.get("confirmation") is not None else [])]

    # ---- runs and node reports ----
    def _on_run(self, kind: str, payload: Any) -> None:
        """The runner's feed, from any thread: every status change of a run becomes a `run` event."""
        loop = self._loop
        if kind == "run" and isinstance(payload, dict) and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._enqueue_run, payload)

    def _enqueue_run(self, described: dict) -> None:
        try:
            effect = reports.run_effect(self.runtime.get("runner"), described, self._run_effects)
            payload = reports.run_payload(described, effect=effect, home=self.config.home)
        except Exception:
            log.exception("channel: could not report a run")
            return
        mark = reports.digest(payload)
        if self._runs_sent.get(payload["id"]) == mark:
            return
        if len(self._runs_sent) > 2000:
            self._runs_sent.clear()
        self._runs_sent[payload["id"]] = mark
        self._enqueue_event("run", payload)
        self._enqueue_workflow_run(described)

    def _enqueue_workflow_run(self, described: dict) -> None:
        """A workflow's run, with its steps, for the panel: what `mirror` lets leave, so no content."""
        service = self.runtime.get("workflows")
        if service is None or described.get("kind") != "workflow":
            return
        try:
            view = service.get_run_view(described.get("flow"), described["id"])
        except Exception:
            log.exception("channel: could not report a workflow run")
            return
        self._enqueue_event("workflow_run", {"run": mirror(view)})

    async def _watch_reports(self) -> None:
        while True:
            self._report_wake.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.timings.reports):
                    await self._report_wake.wait()
            if self.connected:
                await self.send_reports()

    async def send_reports(self) -> None:
        """Queue each report that changed (or that a welcome asks for again), within the rate limit."""
        forced = set(self._throttle.forced)
        builders: list[tuple[str, Callable[[], Any]]] = [
            ("inventory", lambda: reports.inventory(self.runtime)),
            ("programs", lambda: asyncio.to_thread(reports.programs, self.runtime)),
            ("services", lambda: reports.services(self.runtime, started=self._started)),
        ]
        if self.runtime.get("workflows") is not None:  # a node without them sends nothing new
            builders.append(("workflows_catalog", lambda: reports.workflows_catalog(self.runtime)))
        if "system" in forced:  # what the machine is changes with a restart, not while it runs
            builders.append(("system", lambda: asyncio.to_thread(reports.system, self.config, started=self._started)))
        if self._doctor is not None:
            builders.append(("doctor", lambda: self._doctor))
        for kind, build in builders:
            try:
                value = build()
                if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
                    value = await value
            except Exception:
                log.warning("channel: could not build the %s report", kind, exc_info=True)
                continue
            if not value and kind == "inventory":
                continue  # a node with no runner, brains or vault has no inventory to report
            value, cut = reports.fit(kind, value)
            if cut and kind not in self._reports_cut:
                self._reports_cut.add(kind)
                log.warning("channel: the %s report is over %d KiB; %s", kind, reports.MAX_REPORT_BYTES // 1024,
                            "it is sent cut short" if value is not None else "it is not sent")
            if value is None:
                continue
            ready = self._throttle.offer(kind, value)
            if ready is not None:
                self._enqueue_event("node", {kind: ready})
        for kind, value in self._throttle.due().items():
            self._enqueue_event("node", {kind: value})

    async def _run_doctor(self) -> None:
        """`openajar doctor`, offline, in a thread: first a while after start, then every so often."""
        from openajar.core import logs

        await asyncio.sleep(self.timings.doctor_after)
        while True:
            try:
                self._doctor = await asyncio.to_thread(reports.doctor, self.config, logs.scrub)
                self._report_wake.set()
            except Exception:
                log.warning("channel: the doctor report failed", exc_info=True)
            await asyncio.sleep(self.timings.doctor_every)
