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

The owner's controller ceiling (agentos.policy.ceiling, node.toml) bounds what the controller
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
import base64
import contextlib
import hashlib
import json
import logging
import math
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI, WebSocketException

from agentos import __version__
from agentos.core import live as live_sockets
from agentos.core.config import NodeConfig
from agentos.core.errors import (
    AgentosError,
    Cancelled,
    DeadlineExceeded,
    EngineError,
    InvalidRequest,
    NotFound,
    PolicyDenied,
    ServiceUnavailable,
)
from agentos.core.tools import TOOL_NAME_RE
from agentos.freeze.errors import Frozen
from agentos.node.channel import (
    ask_first,
    bridge,
    files,
    logstream,
    node_streams,
    reports,
    standing,
    usage,
    wire,
    workflows,
)
from agentos.node.channel.identity import NodeKey, verify_controller
from agentos.node.channel.invoke import channel_call, failure, run_tool
from agentos.node.channel.store import ChannelStore
from agentos.node.channel.streams import StreamClosed, StreamPipe, StreamStalled, StreamTable
from agentos.policy.ceiling import ControllerCeiling
from agentos.policy.confirm import UNPROVEN_REACH, ConfirmLevel, Proof, controller_proof
from agentos.policy.errors import Denied
from agentos.runner.workflows.mirror import mirror
from agentos.vault.scrub import runtime_scrub

log = logging.getLogger("agentos.channel")

CALL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
INTERRUPTED = "the node restarted while this call was running, so its outcome is unknown"
CONFIG_KEYS = {"heartbeat_seconds": (5, 60)}  # what a controller may tune; anything else is kept, not applied
CLOCK_NOISE = 2.0          # an offset this small is the handshake's own latency and rounding, not skew
CLOCK_WARN = 30.0          # an offset past this is logged as a warning
MAX_CLOCK_OFFSET = 3600.0  # never trust the controller's clock further than this from our own
SIGNED_FRESHNESS = 300.0   # a signed message dated (`ts`) further than this from the controller's now is stale
SIGNED_NONCES = "signed_nonces"  # store meta: `kind:nonce` -> when it was seen (wire.NONCE_KEEP_S)
OWN_NONCES = frozenset({"standing", "ask_first", "workflow"})  # these keep their nonces with their bodies
IDEMPOTENT_SIGNED = frozenset({"revoke"})  # safe to apply twice: an undated one is still applied
TOOLS_PART_BYTES = 768 * 1024  # tool definitions per `node` event, well under the 1 MiB message limit
TOOL_DESCRIPTION_CHARS = 4000  # what the controller keeps of a description


@dataclass
class Timings:
    ping_interval: float = wire.PING_INTERVAL
    dead_after: float = wire.DEAD_AFTER
    heartbeat: float = wire.HEARTBEAT_INTERVAL
    backoff_min: float = wire.BACKOFF_MIN
    backoff_max: float = wire.BACKOFF_MAX
    handshake: float = wire.HANDSHAKE_TIMEOUT
    result_ttl: float = wire.RESULT_TTL
    lane_poll: float = 2.0
    trace_flush: float = 0.5
    housekeeping: float = 30.0
    stream_stall: float = 30.0
    reports: float = 30.0          # how often node reports (inventory, services, ...) are rebuilt
    report_min: float = 60.0       # never one report kind more often than this
    doctor_after: float = 20.0     # the first doctor run, after start
    doctor_every: float = 900.0


class HandshakeFailed(Exception):
    """Authentication did not complete on this connection; the node backs off and dials again."""


class Backoff:
    """Exponential from `low` to `high`, plus up to 25% jitter on top."""

    def __init__(self, low: float, high: float, rng: random.Random | None = None):
        self.low, self.high = low, high
        self.rng = rng or random.Random()
        self.attempt = 0

    def next(self) -> float:
        base = min(self.high, self.low * float(2 ** min(self.attempt, 30)))
        self.attempt += 1
        return base + self.rng.uniform(0, base * 0.25)

    def reset(self) -> None:
        self.attempt = 0


@dataclass
class Call:
    call_id: str
    tool: str
    args: dict
    token: str
    session: str | None
    deadline: float | None
    replies: list[str]
    chain: set[str] = field(default_factory=set)
    caller: tuple | None = None  # who the token is (kind, id, owner) and its grant family root
    task: asyncio.Task | None = None
    cancel_reason: str | None = None
    created: float = field(default_factory=time.monotonic)


class Connection:
    """One authenticated socket."""

    def __init__(self, ws: ClientConnection):
        self.ws = ws
        self.last_rx = time.monotonic()
        self.sent_seq = 0
        self._lock = asyncio.Lock()
        self.streams = StreamTable(self.send, self.send_binary)
        self.tasks: set[asyncio.Task] = set()            # handlers running beside the reader (_off_loop)
        self.chains: dict[str, asyncio.Task] = {}        # the last of them per ordering key

    async def send(self, envelope: wire.Envelope) -> None:
        text = envelope.encode()
        async with self._lock:
            await self.ws.send(text)

    async def send_raw(self, frame: str) -> None:
        async with self._lock:
            await self.ws.send(frame)

    async def send_binary(self, frame: bytes) -> None:
        async with self._lock:
            await self.ws.send(frame)


class NodeChannel:
    """Runtime service "channel"."""

    def __init__(self, config: NodeConfig, runtime: Any, *, issuers: Any = None, timings: Timings | None = None,
                 store: ChannelStore | None = None, key: NodeKey | None = None):
        if not config.enrolled:
            raise ValueError("this node has not joined a controller; run `agentos node join`")
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

    def reconnect_now(self) -> None:
        self._backoff.reset()
        self._wake.set()

    def _dial(self) -> Any:
        return connect(self.url, max_size=wire.HARD_FRAME_BYTES, ping_interval=None, ping_timeout=None,
                       compression=None, open_timeout=self.timings.handshake, close_timeout=2, proxy=None,
                       user_agent_header=f"agentos-node/{self.version}")

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
            except AgentosError as exc:
                log.warning("channel: refused the mesh freeze state in the welcome: %s", exc.message)
        held = body.get("standing")  # additive, signed: the whole set this controller wants the node to hold
        if isinstance(held, dict):
            try:
                result = self._apply_standing(held)
                self._standing_reply = wire.Envelope("standing_result", result, re=welcome_id)
                self._standing_synced = held.get("set") is not None
            except AgentosError as exc:
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
        except AgentosError as exc:
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

    async def _on_anchor_reply(self, conn: Connection, message: wire.Envelope) -> None:
        """A controller's answer to the anchors service (agentos.node.anchors)."""
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

    # ---- controller-signed messages ----
    def _signed(self, kind: str, body: wire.Json) -> None:
        """Every controller-signed message (wire.SIGNED_TYPES): the signature, then a `ts` within
        SIGNED_FRESHNESS of the controller's now, then a `nonce` not seen in the last NONCE_KEEP_S.
        The nonce is kept from here on, so a copy replayed later is refused whatever it carries
        (OWN_NONCES keep theirs in their own checks: standing.py, ask_first.py, workflows.py)."""
        if kind not in wire.SIGNED_TYPES:
            raise InvalidRequest(f"{kind} is not a controller-signed message")
        if not verify_controller(self.config.controller_pubkey, kind, self.node_id, body):
            log.warning("channel: refused a %s with a bad signature", kind)
            raise PolicyDenied("the signature does not match the controller key pinned at enrollment")
        if kind in IDEMPOTENT_SIGNED and "ts" not in body and "nonce" not in body:
            return  # a replayed revoke only revokes again; a sender that does not date it must still reach us
        dated = wire.parse_time(body.get("ts"))
        if dated is None:
            raise InvalidRequest(f"{kind} needs ts (ISO 8601 UTC)")
        if abs(self.controller_time() - dated) > SIGNED_FRESHNESS:
            log.warning("channel: refused a %s dated %s: too far from the controller's now", kind,
                        str(body.get("ts"))[:40])
            raise PolicyDenied(f"this {kind} is stale (dated more than {int(SIGNED_FRESHNESS)} s from now)")
        nonce = body.get("nonce")
        if not isinstance(nonce, str) or not wire.NONCE_RE.fullmatch(nonce):
            raise InvalidRequest(f"{kind} needs a nonce (8 to 64 letters, digits, _ -)")
        if kind in OWN_NONCES:  # checked and kept with the body, only once it is applied (standing.py and co.)
            return
        now = time.time()
        seen = {k: v for k, v in (self.store.get_meta(SIGNED_NONCES, {}) or {}).items()
                if isinstance(v, int | float) and v > now - wire.NONCE_KEEP_S}
        key = f"{kind}:{nonce}"
        if key in seen:
            log.warning("channel: refused a replayed %s", kind)
            raise PolicyDenied(f"this {kind} message was already applied (its nonce was seen before)")
        seen[key] = now
        self.store.set_meta(SIGNED_NONCES, seen)

    async def _on_revoke(self, conn: Connection, message: wire.Envelope) -> None:
        self._signed("revoke", message.body)
        ids = message.body.get("revocation_ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise InvalidRequest("revoke needs a list of revocation_ids")
        self._apply_revocations(ids)  # in effect at once
        if self.issuers is not None:  # and on disk before the controller is answered (raises if refused)
            await self.issuers.run(self.issuers.save_revoked, ids)

    def _apply_revocations(self, ids: list[str]) -> None:
        if self.issuers is not None:
            self.issuers.revoke(ids)
        gone = set(ids)
        for call in list(self.calls.values()):
            if call.chain & gone and call.task is not None and not call.task.done():
                call.cancel_reason = "the grant was revoked"
                call.task.cancel()
        for call_id, call in list(self.resumable.items()):
            if call.chain & gone:
                self.resumable.pop(call_id, None)

    async def _on_config_update(self, conn: Connection, message: wire.Envelope) -> None:
        self._signed("config_update", message.body)
        config = message.body.get("config")
        if not isinstance(config, dict):
            raise InvalidRequest("config_update needs a config object")
        config = dict(config)
        sent = "confirmation" in config  # null drops the layer; leaving the key out keeps it
        confirmation = config.pop("confirmation", None)  # signed here only: never from `welcome`, never kept in meta
        self._set_config(config, replace=False)
        if sent:
            self._set_confirmation(confirmation)

    def _set_confirmation(self, layer: Any) -> None:
        """A controller's layer (docs/CONFIRMATIONS.md, The node channel): apart from the node's own
        setting, so it can only ask for more. What cannot be read is left out, and logged."""
        setting = self.runtime.get("confirmation")
        if setting is None:
            log.warning("channel: this node has no confirmation setting; ignored the controller's")
            return
        result = setting.apply_controller(layer, by=f"controller:{self.issuer}"[:120])
        if result["refused"]:
            fields = ", ".join(r["field"] for r in result["refused"])
            log.warning("channel: could not read part of the controller's confirmation setting: %s", fields)
            self._trace("config", status="denied", summary=f"Could not read the controller's confirmation setting for {fields}")

    def _set_config(self, config: dict, *, replace: bool) -> None:
        """Apply the controller's config. A value out of its range is not applied: the value in
        force stays (docs/PROTOCOL.md, config_update)."""
        def valid(key: str, value: Any) -> bool:
            low, high = CONFIG_KEYS[key]
            return isinstance(value, int | float) and not isinstance(value, bool) and low <= value <= high

        merged = dict(config) if replace else {**self.controller_config, **config}
        for key in CONFIG_KEYS:
            value = merged.get(key)
            if value is not None and not valid(key, value):
                log.warning("channel: ignored %s=%r from the controller (out of range)", key, str(value)[:40])
                before = self.controller_config.get(key)
                if before is not None and valid(key, before):
                    merged[key] = before
                else:
                    merged.pop(key)
        self.controller_config = merged
        self.store.set_meta("controller_config", merged)
        self._beat_wake.set()

    async def _on_lane_control(self, conn: Connection, message: wire.Envelope) -> None:
        """A human at the controller takes or hands back a lane (v1 clarification, signed)."""
        self._signed("lane_control", message.body)
        lanes = self.runtime.get("lanes")
        lane, action = message.body.get("lane"), message.body.get("action")
        if action == "take":
            self.ceiling.check_take()
        by = node_streams.holder_name(message.body.get("by"))
        if self._terminal_lane(lane):
            if action == "take":
                self.terminal_holds.take(lane, by)
            elif action == "hand_back":
                self.terminal_holds.hand_back(lane)
            else:
                raise InvalidRequest("a terminal lane takes take and hand_back only")
            self._lane_wake.set()
            return
        if lanes is None or not callable(getattr(lanes, "take_control", None)):
            raise ServiceUnavailable("this node has no lane control")
        from agentos.node.viewer_app import apply_action

        await apply_action(lanes, str(lane), action, by=by)
        self._lane_wake.set()

    def _terminal_lane(self, lane: Any) -> bool:
        """An env lane (agentos.env) the browser lane manager does not have."""
        envs = self.runtime.get("envs")
        if not isinstance(lane, str) or envs is None or not callable(getattr(envs, "has", None)):
            return False
        lanes = self.runtime.get("lanes")
        with contextlib.suppress(Exception):
            if lanes is not None:
                lanes.get(lane)
                return False
        try:
            return bool(envs.has(lane))
        except Exception:  # noqa: BLE001
            return False

    async def _on_mesh_freeze(self, conn: Connection, message: wire.Envelope) -> None:
        """The controller freezes (or lifts its freeze on) every node of the mesh (signed, additive). As with
        lane_control, the node answers only a refusal (`error`)."""
        self._apply_mesh_freeze(message.body)
        self._lane_wake.set()

    def _apply_mesh_freeze(self, body: wire.Json) -> str:
        self._signed("mesh_freeze", body)
        freeze = self.runtime.get("freeze")
        if freeze is None or not callable(getattr(freeze, "apply_mesh", None)):
            if body.get("frozen") is False:
                return "not_frozen"  # nothing to lift on a node without the freeze service
            raise ServiceUnavailable("this node has no freeze service")
        state = freeze.apply_mesh(body)
        if state == "frozen":  # calls the controller sent before the freeze stop with it
            for call in list(self.calls.values()):
                if call.task is not None and not call.task.done():
                    call.cancel_reason = "the mesh is frozen"
                    call.task.cancel()
        if state == "frozen":
            log.warning("channel: mesh freeze from the controller: %s", state)
        else:  # not frozen, or lifted: the normal state, not a warning
            log.info("channel: mesh freeze from the controller: %s", state)
        return state

    async def _on_standing(self, conn: Connection, message: wire.Envelope) -> None:
        """Standing approvals from the controller (signed, additive): answered with `standing_result
        { applied, refused, revoked, withdrawn }`, or `error` when the whole body is refused."""
        result = self._apply_standing(message.body)
        if message.body.get("set") is not None:
            self._standing_synced = True
        await conn.send(wire.Envelope("standing_result", result, re=message.id or None))

    def _apply_standing(self, body: wire.Json) -> dict:
        self._signed("standing", body)
        approvals = self.runtime.get("approvals")
        if approvals is None or not callable(getattr(approvals, "put_issued_standing", None)):
            raise ServiceUnavailable("this node has no approvals")
        sync = standing.Standing(approvals, issuer=self.issuer, ceiling=self.ceiling,
                                 get_meta=self.store.get_meta, set_meta=self.store.set_meta,
                                 freeze=self.runtime.get("freeze"), limits=standing.Limits.from_config(self.config))
        sync.check_body(body)
        result = sync.apply(body)
        if result["refused"]:
            log.warning("channel: refused %d standing approval(s) from the controller: %s", len(result["refused"]),
                        ", ".join(f"{r['id']} ({r['code']})" for r in result["refused"][:5]))
        return result

    async def _on_ask_first(self, conn: Connection, message: wire.Envelope) -> None:
        """A person in the controller's panel turned "Ask me first" on or off (signed, additive):
        answered with `ask_first_result { applied, refused }`, or `error` when the body is refused."""
        if self._ask_first() is None:  # as a node that never heard of it answers
            await conn.send(wire.error_envelope("unsupported", "this node has no Ask me first switches",
                                                message.id or None))
            return
        result = self._apply_ask_first(message.body)
        await conn.send(wire.Envelope("ask_first_result", result, re=message.id or None))

    def _ask_first(self) -> Any:
        store = self.runtime.get("ask_first")
        return store if callable(getattr(store, "set", None)) else None

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

    def _apply_ask_first(self, body: wire.Json) -> dict:
        self._signed("ask_first", body)
        sync = ask_first.AskFirstSync(self._ask_first(), ceiling=self.ceiling, get_meta=self.store.get_meta,
                                      set_meta=self.store.set_meta)
        result = sync.apply(sync.check_body(body))
        if result["refused"]:
            log.warning("channel: refused %d Ask me first change(s) from the controller: %s", len(result["refused"]),
                        ", ".join(f"{r['scope']} {r['id']} ({r['code']})" for r in result["refused"][:5]))
        return result

    # ---- approvals ----
    async def _on_decision(self, conn: Connection, message: wire.Envelope) -> None:
        body = message.body
        approval_id, decision = body.get("approval_id"), body.get("decision")
        if not isinstance(approval_id, str) or decision not in ("approved", "denied"):
            raise InvalidRequest("approval_decision needs approval_id and decision approved or denied")
        approvals = self.runtime.get("approvals")
        if approvals is None:
            raise ServiceUnavailable("this node has no approvals")

        async def refuse(code: str, text: str, requires: str | None = None, state: str | None = None) -> None:
            await conn.send(wire.error_envelope(code, text, message.id or None, approval_id=approval_id,
                                                requires=requires, state=state))

        try:
            approval = approvals.get(approval_id)
        except NotFound:
            await refuse("not_found", f"no approval {approval_id} on this node")
            return
        if approval.state != "pending":
            await refuse("already_decided", f"approval {approval_id} is already {approval.state}", state=approval.state)
            return
        if approval.kind == "action" and body.get("content_hash") != approval.content_hash:
            log.warning("channel: refused a decision on %s: the content hash does not match", approval_id)
            self._trace("approval", status="denied", tool=approval.tool, lane=approval.lane,
                        summary=f"Refused a decision on {approval_id}: the content hash does not match")
            await refuse("invalid", "the content hash does not match this approval")
            return
        if decision == "approved" and approval.kind == "action":
            # the owner's ceiling: approving lets the action run, so the controller may approve only
            # what it could do itself; the rest is decided on the node. The decision names no grant,
            # so the self-approval rule has no family to apply here.
            try:
                self.ceiling.check_approval(approval.effect)
            except Denied as exc:
                log.warning("channel: refused the controller's approval of %s: %s", approval_id, exc.detail)
                self._trace("approval", status="denied", tool=approval.tool, lane=approval.lane,
                            summary=f"Refused the controller's approval of {approval_id}: above the "
                                    "controller ceiling")
                await refuse(exc.code, exc.message)
                return
        who = body.get("decided_by")
        by = f"controller:{who}" if isinstance(who, str) and 0 < len(who) <= 80 else "controller"
        answer = body.get("answer") if isinstance(body.get("answer"), str) else None
        try:
            # a denial needs no proof, so a bad one never stops it: the node records none
            proof = self._relayed_proof(body.get("proof")) if decision == "approved" else Proof(None, None)
            approvals.decide(approval_id, "approve" if decision == "approved" else "deny", by=by, answer=answer,
                             proof=proof)
        except AgentosError as exc:
            if exc.code in ("proof_too_weak", "method_unavailable"):
                log.warning("channel: refused the controller's approval of %s: %s", approval_id, exc.code)
                self._trace("approval", status="denied", tool=approval.tool, lane=approval.lane,
                            summary=f"Refused the controller's approval of {approval_id}: {exc.code}")
            if exc.code == "conflict":  # someone at the node got there first: say what they decided
                await refuse("already_decided", exc.message, state=approvals.get(approval_id).state)
                return
            await refuse(exc.code, exc.message, getattr(exc, "requires", None))

    def _relayed_proof(self, claim: Any) -> Proof:
        """What the controller's word counts for, up to the limit the owner set on this node."""
        setting = self.runtime.get("confirmation")
        if setting is None:  # no setting on this node: as today, it proves no person
            return Proof(None, None, time.time(), reach=UNPROVEN_REACH)
        limit = ConfirmLevel.of(setting.current().controller_max_level)
        return controller_proof(claim, setting.methods, limit, at=time.time())

    def _announce_pending_approvals(self) -> None:
        approvals = self.runtime.get("approvals")
        if approvals is None:
            return
        try:
            pending = approvals.list(state="pending", limit=500)
        except Exception:  # noqa: BLE001
            return
        for approval in reversed(pending):
            self._on_approval(approval.public())

    def _on_approval(self, public: dict) -> None:
        if isinstance(public.get("standing"), dict):
            self._on_standing_change(public["standing"], public.get("state"))
            return
        approval_id, state = public.get("id"), public.get("state")
        if not isinstance(approval_id, str) or not isinstance(state, str):
            return
        known = self.store.approval_state(approval_id)
        if known == state:
            return
        if known is None and state == "pending":
            self._enqueue("approval_request", {"approval": bridge.approval_payload(public, runtime_scrub(self.runtime))})
        elif known is not None:
            payload = {"id": approval_id, "state": state}
            if public.get("decided_by"):
                payload["decided_by"] = str(public["decided_by"])[:120]
            for key in ("decided_method", "decided_level"):
                if public.get(key):
                    payload[key] = str(public[key])[:40]
            if public.get("note"):  # the decision's note; the approval's own `reason` is why it was asked
                payload["reason"] = str(public["note"])[:300]
            self._enqueue_event("approval", payload)
        self.store.set_approval_state(approval_id, state)

    def _on_standing_change(self, sa: dict, change: Any) -> None:
        """A use or an end of one this controller sent goes back as `event { kind: "standing" }`, so its
        panel's count is the node's. The node's own standing approvals are not the controller's."""
        if sa.get("issuer") != self.issuer or change not in ("used", "revoked", "withdrawn"):
            return
        payload = standing.event_payload(sa, self.controller_time())
        if payload is not None:
            self._report_standing(payload)

    def _report_standing(self, payload: dict) -> None:
        reported = self.store.get_meta("standing_reported", {}) or {}
        mark = [payload["used"], payload["state"]]
        if reported.get(payload["id"]) == mark:
            return
        reported[payload["id"]] = mark
        self.store.set_meta("standing_reported", reported)
        self._enqueue_event("standing", payload)

    def _confirm_standing(self) -> None:
        """While the controller is connected and has sent its whole set this session, what it holds
        here is confirmed: a revoke would reach the node now. Without that, one it sent stops
        covering calls `controller_standing_confirm_hours` after it was last confirmed."""
        approvals = self.runtime.get("approvals")
        if not (self.connected and self._standing_synced) or not callable(getattr(approvals, "confirm_issued", None)):
            return
        try:
            approvals.confirm_issued(self.issuer)
        except Exception:
            log.debug("channel: could not confirm the standing approvals", exc_info=True)

    def check_standing(self) -> None:
        """What changed outside this process (the owner's `agentos approvals standing revoke`, an
        expiry) is reported too: housekeeping compares what the node holds with what it last said."""
        approvals = self.runtime.get("approvals")
        if approvals is None or not callable(getattr(approvals, "issued_standing", None)):
            return
        try:
            held = approvals.issued_standing(self.issuer)
        except Exception:
            log.debug("channel: could not read the standing approvals", exc_info=True)
            return
        now = self.controller_time()
        reported = self.store.get_meta("standing_reported", {}) or {}
        for sa in held:
            payload = standing.held(sa, now)
            if reported.get(sa.id) is None and payload["state"] == "active" and payload["used"] == 0:
                continue  # just applied: the standing_result said so
            self._report_standing(payload)

    def _on_bus(self, event_type: str, payload: dict) -> None:
        """Policy events, from any thread: traces are batched, approvals forwarded in order."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if event_type == "trace" and isinstance(payload, dict):
            loop.call_soon_threadsafe(self._traces.append, payload)
        elif event_type == "approval" and isinstance(payload, dict):
            loop.call_soon_threadsafe(self._on_approval, payload)

    # ---- the outbox ----
    def _enqueue(self, kind: str, body: dict) -> None:
        try:
            self.store.enqueue(kind, body)
        except wire.Oversize:
            log.error("channel: dropped a %s over the 1 MiB limit", kind)
            return
        self._outbox_wake.set()

    def _enqueue_event(self, kind: str, payload: dict) -> None:
        self._enqueue("event", {"kind": kind, "payload": payload})

    async def announce_tools(self) -> None:
        """The tool list changed while agentosd runs (agentos.mcp.reload: a connector installed or
        removed): send the controller the whole list again. Offline, the next welcome sends it."""
        if not self.connected:
            return
        self._enqueue_tools(await bridge.tool_defs(self.runtime.get("mcp")))
        self._outbox_wake.set()

    def _enqueue_tools(self, tools: list[dict]) -> None:
        """The tool list as one `node` event, or as parts `tools_part { index, count }` when it would
        not fit one message. A single definition too large to send is logged, not sent."""
        parts: list[list[dict]] = [[]]
        size = 0
        for tool in tools:
            if isinstance(tool.get("description"), str):
                tool = {**tool, "description": tool["description"][:TOOL_DESCRIPTION_CHARS]}
            length = len(json.dumps(tool, separators=(",", ":"), ensure_ascii=False).encode())
            if length > TOOLS_PART_BYTES:
                log.error("channel: tool %s is too large to announce (%d bytes); the controller will not list it",
                          str(tool.get("name"))[:64], length)
                continue
            if parts[-1] and size + length > TOOLS_PART_BYTES:
                parts.append([])
                size = 0
            parts[-1].append(tool)
            size += length
        if len(parts) == 1:
            self._enqueue_event("node", {"tools": parts[0]})
            return
        for index, part in enumerate(parts):
            self._enqueue_event("node", {"tools": part, "tools_part": {"index": index, "count": len(parts)}})

    def _enqueue_traces(self, events: list[dict]) -> None:
        try:
            self.store.enqueue("traces", {"events": events})
        except wire.Oversize:
            if len(events) == 1:
                log.error("channel: dropped a trace over the size limit")
                return
            middle = len(events) // 2
            self._enqueue_traces(events[:middle])
            self._enqueue_traces(events[middle:])
            return
        self._outbox_wake.set()

    def _flush_trace_buffer(self) -> None:
        pending, self._traces = self._traces, []
        events = [bridge.trace_event(p) for p in pending]
        for start in range(0, len(events), wire.MAX_TRACES_PER_BATCH):
            self._enqueue_traces(events[start:start + wire.MAX_TRACES_PER_BATCH])

    async def _flush_traces(self) -> None:
        while True:
            await asyncio.sleep(self.timings.trace_flush)
            if self._traces:
                self._flush_trace_buffer()

    def _trace(self, kind: str, **fields: Any) -> None:
        traces = self.runtime.get("traces")
        if traces is None:
            return
        from agentos.policy.traces import TraceEvent

        try:
            traces.write(TraceEvent(kind=kind, principal="controller", principal_kind="controller", **fields))
        except Exception:
            log.exception("channel: trace failed")

    # ---- lanes ----
    def _all_lanes(self) -> list[dict]:
        """Every lane as the controller sees it; a terminal lane a person at the controller holds
        shows that in `control` (node_streams.TerminalHolds)."""
        lanes = bridge.all_lanes(self.runtime, self.config.name)
        for lane in lanes:
            if lane.get("control") is None and (control := self.terminal_holds.control(lane["id"])) is not None:
                lane["control"] = control
        return lanes

    async def _watch_lanes(self) -> None:
        while True:
            self._lane_wake.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.timings.lane_poll):
                    await self._lane_wake.wait()
            self.check_lanes()

    def check_lanes(self) -> None:
        current = self._all_lanes()
        changed = False
        seen: dict[str, tuple] = {}
        for lane in current:
            digest = bridge.lane_digest(lane)
            seen[lane["id"]] = digest
            if self._lanes_seen.get(lane["id"]) != digest:
                self._enqueue_event("lane", lane)
                changed = True
        if set(seen) != set(self._lanes_seen):
            changed = True
        self._lanes_seen = seen
        if changed:
            self._beat_wake.set()

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(self.timings.housekeeping)
            self.store.purge_calls(self.timings.result_ttl)
            if self.terminal_holds.reap():
                self._lane_wake.set()
            now = time.monotonic()
            for call_id, call in list(self.resumable.items()):
                if now - call.created > self.timings.result_ttl:
                    self.resumable.pop(call_id, None)
            self._confirm_standing()
            self.check_standing()

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
        """`agentos doctor`, offline, in a thread: first a while after start, then every so often."""
        from agentos.core import logs

        await asyncio.sleep(self.timings.doctor_after)
        while True:
            try:
                self._doctor = await asyncio.to_thread(reports.doctor, self.config, logs.scrub)
                self._report_wake.set()
            except Exception:
                log.warning("channel: the doctor report failed", exc_info=True)
            await asyncio.sleep(self.timings.doctor_every)

    # ---- invoke ----
    async def _on_invoke(self, conn: Connection, message: wire.Envelope) -> None:
        body = message.body
        call_id, tool = body.get("call_id"), body.get("tool")
        if not isinstance(call_id, str) or not CALL_ID_RE.match(call_id):
            raise InvalidRequest("invoke needs a call_id (1 to 128 letters, digits, _ . : -)")
        if not isinstance(tool, str) or not TOOL_NAME_RE.match(tool):
            raise InvalidRequest("invoke needs a tool name")
        args = body.get("args") if body.get("args") is not None else {}
        if not isinstance(args, dict):
            raise InvalidRequest("invoke args must be an object")
        live = self.calls.get(call_id)
        if live is not None:
            live.replies.append(message.id)
            return
        call = self._new_call(message, call_id, tool, args)
        cached = self.store.get_call(call_id)
        if cached is not None and cached.result is not None:
            fresh = time.time() - cached.updated_at < self.timings.result_ttl
            if fresh and cached.state == "waiting":
                await self._retry_waiting(conn, message, cached, call)
                return
            if fresh:
                await self._send_result(message.id, call_id, cached.result)
                return
        self._start(call)

    def _new_call(self, message: wire.Envelope, call_id: str, tool: str, args: dict) -> Call:
        body = message.body
        deadline_value = body.get("deadline")
        deadline = wire.parse_time(deadline_value)
        if deadline_value is not None and deadline is None:
            raise InvalidRequest("deadline must be ISO 8601 UTC")
        token = body.get("grant_token") if isinstance(body.get("grant_token"), str) else ""
        principal = body.get("principal")
        session = principal.get("thread") if isinstance(principal, dict) else None
        session = session if isinstance(session, str) and len(session) <= 200 else None
        return Call(call_id, tool, args, token, session, deadline, [message.id])

    def _start(self, call: Call) -> None:
        resolved = self._resolved(call.token)
        call.chain = set() if resolved is None else {*resolved[1].chain_ids, *resolved[1].rids}
        call.caller = None if resolved is None else _caller_of(*resolved)
        self.store.drop_waiting(call.call_id)
        self.store.put_call(call.call_id, "running", tool=call.tool)
        self.calls[call.call_id] = call
        call.task = asyncio.create_task(self._perform(call), name=f"invoke-{call.call_id}")

    def _resolved(self, token: str) -> tuple[Any, Any] | None:
        policy = self.runtime.get("policy")
        try:
            with channel_call(policy, token):
                return policy.resolve(token)
        except Exception:  # noqa: BLE001  (the wrapper refuses it with the right code)
            return None

    def _renewed(self, waited: Call, retry: Call) -> bool:
        """May the retry's grant token stand in for the one the call waited under? A controller's
        member call token lapses within minutes, often before the approval, so its retry carries a
        fresh one. It does only when it resolves now, is the same principal under the same grant
        family root, and covers the call's lane at the tool's effect."""
        if not retry.token or retry.token == waited.token or waited.caller is None:
            return False
        resolved = self._resolved(retry.token)
        if resolved is None or _caller_of(*resolved) != waited.caller:
            return False
        spec = getattr(self.runtime.get("mcp"), "specs", {}).get(waited.tool)
        if spec is None:
            return False
        lane = waited.args.get(spec.lane_arg) if spec.lane_arg else None
        try:
            self.runtime.get("policy").precheck(resolved[1], spec.effect, lane_id=lane if isinstance(lane, str) else None)
        except AgentosError:
            return False
        return True

    async def _retry_waiting(self, conn: Connection, message: wire.Envelope, cached: Any, retry: Call) -> None:
        """A retry of a call that waited for approval: once approved it runs (and uses the
        approval); still pending, the cached answer; denied or expired, a refusal. After a node
        restart the waiting call is gone from memory, so the retry itself runs, once, if it is the
        same call (tool and arguments) that waited."""
        approvals = self.runtime.get("approvals")
        state = "pending"
        if approvals is not None and cached.approval_id:
            with contextlib.suppress(AgentosError):
                state = approvals.get(cached.approval_id).state
        resumable = self.resumable.get(cached.call_id)
        if state == "approved" and resumable is not None:
            self.resumable.pop(cached.call_id, None)
            resumable.replies = [message.id]
            resumable.task = None
            resumable.cancel_reason = None
            if self._renewed(resumable, retry):
                resumable.token, resumable.deadline = retry.token, retry.deadline
            self._start(resumable)
            return
        if state == "approved":
            waited = self.store.waiting_call(cached.call_id)
            if waited is not None and waited == (retry.tool, args_digest(retry.args)):
                self._start(retry)
                return
        if state in ("denied", "expired"):
            result = failure(PolicyDenied.code, f"approval {cached.approval_id} was {state}")
            self.store.put_call(cached.call_id, "done", result=result)
            self.store.drop_waiting(cached.call_id)
            self.resumable.pop(cached.call_id, None)
            await self._send_result(message.id, cached.call_id, result)
            return
        await self._send_result(message.id, cached.call_id, cached.result)

    async def _perform(self, call: Call) -> None:
        stored: dict
        try:
            if self.runtime.get("policy") is None:
                raise ServiceUnavailable("this node has no policy service, so it refuses calls from a controller")
            remaining = None
            if call.deadline is not None:
                remaining = call.deadline - self.controller_time()
                if remaining <= 0:
                    raise DeadlineExceeded("the call was past its deadline before it started")
            async with asyncio.timeout(remaining):
                stored = await run_tool(self.runtime, call.tool, call.args, token=call.token, session=call.session)
        except TimeoutError:
            stored = failure(DeadlineExceeded.code, "the call ran past its deadline and was stopped")
        except asyncio.CancelledError:
            if call.cancel_reason is None:
                self.calls.pop(call.call_id, None)
                raise  # shutting down: the row stays `running` and reads as interrupted on the next start
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            stored = failure(Cancelled.code, call.cancel_reason)
        except AgentosError as exc:
            stored = failure(exc.code, exc.message)
        except Exception:
            log.exception("channel: %s failed", call.tool)
            stored = failure("engine_error", f"{call.tool} failed unexpectedly")
        self.calls.pop(call.call_id, None)
        error = stored.get("error") or {}
        if error.get("code") == "approval_pending":
            self.store.put_call(call.call_id, "waiting", result=stored, approval_id=error.get("approval_id"))
            self.store.put_waiting(call.call_id, call.tool, args_digest(call.args))
            self.resumable[call.call_id] = call
        else:
            self.store.put_call(call.call_id, "done", result=stored)
        for reply in call.replies:
            await self._send_result(reply, call.call_id, stored)

    async def _send_result(self, re: str, call_id: str, stored: dict) -> None:
        """Results are not outboxed: with no connection, the cache answers the retry instead."""
        conn = self.conn
        if conn is None:
            return
        content: list[dict] = []
        pushes: list[tuple[StreamPipe, bytes]] = []
        for item in stored.get("content") or []:
            if item.get("type") == "image" and isinstance(item.get("data"), str):
                try:
                    data = base64.b64decode(item["data"])
                    pipe = conn.streams.new(wire.ulid(), "file", stall_after=self.timings.stream_stall)
                except (ValueError, OverflowError):
                    continue
                pushes.append((pipe, data))
                content.append({"type": "image", "mime_type": item.get("mime_type") or "image/png",
                                "stream_id": pipe.id})
            elif item.get("type") == "text":
                content.append({"type": "text", "text": item.get("text", "")})
        body: dict = {"re": re, "ok": bool(stored.get("ok")), "content": content}
        if stored.get("error"):
            body["error"] = stored["error"]
        try:
            envelope = wire.Envelope("result", body, re=re)
            envelope.encode()
        except wire.Oversize:
            for pipe, _ in pushes:
                conn.streams.drop(pipe.id)
            body = {"re": re, **failure("invalid", "the result is larger than the 1 MiB message limit")}
            envelope, pushes = wire.Envelope("result", body, re=re), []
        with contextlib.suppress(ConnectionClosed):
            await conn.send(envelope)
            for pipe, data in pushes:
                pipe.task = asyncio.create_task(self._push(conn, pipe, data), name=f"push-{pipe.id}")

    async def _push(self, conn: Connection, pipe: StreamPipe, data: bytes) -> None:
        reason = "eof"
        try:
            await pipe.write(data)
        except StreamStalled:
            reason = "stalled"
        except (StreamClosed, ConnectionClosed):
            return
        finally:
            conn.streams.drop(pipe.id)
        await pipe.close(reason)

    async def _on_cancel(self, conn: Connection, message: wire.Envelope) -> None:
        call_id = message.body.get("call_id")
        if not isinstance(call_id, str):
            raise InvalidRequest("cancel needs a call_id")
        live = self.calls.get(call_id)
        if live is not None and live.task is not None and not live.task.done():
            live.cancel_reason = "the call was cancelled"
            live.task.cancel()
            return
        cached = self.store.get_call(call_id)
        if self.resumable.pop(call_id, None) is not None or (cached is not None and cached.state == "waiting"):
            self.store.drop_waiting(call_id)
            self.store.put_call(call_id, "done", result=failure(Cancelled.code, "the call was cancelled"))

    # ---- streams ----
    async def _on_stream_open(self, conn: Connection, message: wire.Envelope) -> None:
        body = message.body
        stream_id, kind, lane = wire.normal_ulid(body.get("stream_id")), body.get("kind"), body.get("lane")
        params = body.get("params") if body.get("params") is not None else {}
        try:
            if not wire.is_ulid(stream_id):
                raise InvalidRequest("stream_id must be a ULID")
            if conn.streams.get(stream_id) is not None:
                raise InvalidRequest("this stream_id is already open")
            if not isinstance(params, dict):
                raise InvalidRequest("stream params must be an object")
            if len(conn.streams.pipes) >= conn.streams.limit:
                raise ServiceUnavailable("too many open streams on this connection")
            extra = None
            mode = str(params.get("mode", "view"))
            self.ceiling.check_stream(str(kind), mode)
            if mode == "control":
                self._refuse_frozen(lane)
            if kind == "vnc":
                worker = node_streams.prepare_vnc(self.runtime.get("lanes"), self._control, lane, params)
            elif kind == "terminal":
                worker, extra = await node_streams.prepare_terminal(self.runtime.get("envs"), lane, params,
                                                                    self.terminal_holds)
            elif kind == "file":
                worker = await files.prepare_file(self.runtime, self.config.home, lane, params)
            elif kind == "logs":
                worker = logstream.prepare_logs(self.runtime, self.config, params)
            else:
                raise InvalidRequest(f"unknown stream kind {str(kind)[:20]!r}")
        except AgentosError as exc:
            await conn.send(wire.error_envelope(exc.code, exc.message, message.id or None,
                                                stream_id=stream_id if isinstance(stream_id, str) else None))
            return
        stall = self.timings.stream_stall if kind == "file" else None  # a file has an end; a viewer may idle
        pipe = conn.streams.new(stream_id, kind, lane=lane if isinstance(lane, str) else None,
                                **({"stall_after": stall} if stall is not None else {}))
        pipe.extra = extra
        if mode == "control":
            self._track_live(conn, pipe)
        ready = {"stream_id": stream_id}
        if kind == "terminal" and isinstance(getattr(extra, "id", None), str):
            ready["session"] = extra.id  # a reconnect passes it as params.session to attach to the same shell
        try:
            await conn.send(wire.Envelope("stream_ready", ready, re=message.id or None))
        except BaseException:
            conn.streams.drop(pipe.id)
            if pipe.live is not None:
                live_sockets.of(self.runtime).remove(pipe.live)
            raise
        pipe.task = asyncio.create_task(self._supervise(conn, pipe, worker), name=f"stream-{stream_id}")

    async def _supervise(self, conn: Connection, pipe: StreamPipe, worker: Any) -> None:
        reason = "error"
        try:
            reason = await worker(pipe)
        except asyncio.CancelledError:
            raise
        except StreamClosed:
            reason = pipe.close_reason or "closed"
        except StreamStalled:
            reason = "stalled"
        except ConnectionClosed:
            return
        except Exception:
            log.exception("channel: stream %s failed", pipe.id)
        finally:
            conn.streams.drop(pipe.id)
            if pipe.live is not None:
                live_sockets.of(self.runtime).remove(pipe.live)
        await pipe.close(reason)

    def _refuse_frozen(self, lane: Any) -> None:
        """During a freeze a person at the controller may still look, but not type (docs/PROTOCOL.md)."""
        freeze = self.runtime.get("freeze")
        frozen_for = getattr(freeze, "frozen_for", None)
        rec = frozen_for(lane if isinstance(lane, str) else None) if callable(frozen_for) else None
        if rec is not None:
            raise Frozen(freeze.message(lane if isinstance(lane, str) else None, rec))

    def _track_live(self, conn: Connection, pipe: StreamPipe) -> None:
        """A control stream closes (reason `frozen`) once a freeze covers its lane (agentos.core.live)."""
        async def recheck() -> tuple[int, str] | None:
            try:
                self._refuse_frozen(pipe.lane)
            except Frozen:
                return live_sockets.CLOSE_DENIED, "frozen"
            return None

        def end(entry: live_sockets.LiveEntry) -> None:
            live_sockets.of(self.runtime).remove(entry)  # also when the worker never got to start
            conn.streams.drop(pipe.id)
            if pipe.task is not None:
                pipe.task.cancel()
            task = asyncio.get_running_loop().create_task(pipe.close(entry.reason or "frozen"))
            self._stream_closers.add(task)
            task.add_done_callback(self._stream_closers.discard)

        pipe.live = live_sockets.of(self.runtime).add(
            live_sockets.LiveEntry(f"channel:{pipe.kind}", recheck, lane=pipe.lane, on_close=end))

    async def _on_stream_close(self, conn: Connection, message: wire.Envelope) -> None:
        conn.streams.on_close(message.body)

    async def _on_stream_credit(self, conn: Connection, message: wire.Envelope) -> None:
        conn.streams.on_credit(message.body)

    async def _on_resize(self, conn: Connection, message: wire.Envelope) -> None:
        pipe = conn.streams.get(str(message.body.get("stream_id")))
        session = getattr(pipe, "extra", None) if pipe is not None else None
        if session is None:
            raise InvalidRequest("resize needs an open terminal stream")
        node_streams.resize(session, message.body)


def _caller_of(principal: Any, grant: Any) -> tuple:
    return (principal.kind, principal.id, principal.owner, grant.root_id)


def args_digest(args: dict) -> str:
    """What identifies a call's arguments, so a retry after a restart runs only the call that waited."""
    return hashlib.sha256(wire.canonical(args)).hexdigest()
