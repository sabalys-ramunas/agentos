"""A scripted controller for node-side protocol tests: it sends `challenge`, checks `hello`
against the node key, answers `welcome`, and then lets the test read and send messages."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fakes import FakeInteraction, FakeLaneManager
from websockets.asyncio.server import ServerConnection, serve

from openajar.core import signing
from openajar.core.config import NodeConfig
from openajar.core.models import ControlState, LaneSpec
from openajar.core.runtime import Runtime
from openajar.mcp import providers as mcp_providers
from openajar.node.channel import wire
from openajar.node.channel.identity import NodeKey
from openajar.node.channel.node import Timings
from openajar.node.daemon import build_node_app, build_services, start_services, stop_services
from openajar.policy.ceiling import ControllerCeiling

NODE_ID = "node_01TESTNODE00000000000000"
FAST = Timings(ping_interval=0.5, dead_after=3.0, heartbeat=0.3, backoff_min=0.05, backoff_max=0.2, handshake=3.0,
               lane_poll=0.1, trace_flush=0.05, housekeeping=60.0, stream_stall=2.0)


class ControlLanes(FakeLaneManager):
    """FakeLaneManager with take over and hand back, and control listeners."""

    def __init__(self, config):
        super().__init__(config)
        self.listeners = []
        self.human_inputs = 0

    def add_control_listener(self, callback):
        self.listeners.append(callback)

    def _set(self, lane_id, state, by):
        info = self._require(lane_id)
        old = info.control.model_copy()
        info.control = info.control.model_copy(update={"state": state, "by": by})
        for callback in self.listeners:
            callback(lane_id, old, info.control)
        return info

    async def take_control(self, lane_id, *, by):
        return self._set(lane_id, ControlState.HUMAN, by)

    async def hand_back(self, lane_id, *, by):
        return self._set(lane_id, ControlState.AGENT, by)

    async def stop_agent(self, lane_id, *, by):
        return self._set(lane_id, ControlState.PAUSED, by)

    async def deny_request(self, lane_id, *, by):
        return self._require(lane_id)

    def note_human_input(self, lane_id):
        self.human_inputs += 1


@dataclass
class Peer:
    """One accepted node connection, after welcome. `recv` finds the first message it has not
    returned yet that matches, wherever it is in the order of arrival."""
    ws: ServerConnection
    binary: asyncio.Queue = field(default_factory=asyncio.Queue)
    seen: list = field(default_factory=list)
    taken: set = field(default_factory=set)
    new: asyncio.Event = field(default_factory=asyncio.Event)

    def add(self, envelope: wire.Envelope) -> None:
        self.seen.append(envelope)
        self.new.set()

    async def send(self, kind: str, body: dict | None = None, **kw: Any) -> wire.Envelope:
        envelope = wire.Envelope(kind, body or {}, **kw)
        await self.ws.send(envelope.encode())
        return envelope

    async def send_raw(self, frame: str | bytes) -> None:
        await self.ws.send(frame)

    async def recv(self, kind: str | None = None, timeout: float = 5.0, where=None) -> wire.Envelope:
        async with asyncio.timeout(timeout):
            while True:
                for index, envelope in enumerate(self.seen):
                    if index in self.taken:
                        continue
                    if (kind is None or envelope.type == kind) and (where is None or where(envelope)):
                        self.taken.add(index)
                        return envelope
                self.new.clear()
                await self.new.wait()

    async def reply_to(self, re: str, timeout: float = 5.0) -> wire.Envelope:
        return await self.recv(where=lambda e: e.reply_to == re, timeout=timeout)


class FakeController:
    def __init__(self, node_id: str, node_pubkey: str):
        self.key = Ed25519PrivateKey.generate()
        self.grant_key = Ed25519PrivateKey.generate()
        self.node_id = node_id
        self.node_pubkey = node_pubkey
        self.peers: asyncio.Queue[Peer] = asyncio.Queue()
        self.hellos: list[dict] = []
        self.welcome_extra: dict = {}
        self.refuse_next = False
        self.refuse_all = False
        self.server = None
        self.port = 0

    def sign(self, kind: str, body: dict) -> dict:
        return signing.sign_controller(self.key, kind, self.node_id, body)

    def fresh(self, kind: str, body: dict) -> dict:
        """As the controller sends every signed kind: dated now, with a nonce never used before."""
        return self.sign(kind, {"ts": wire.iso(), "nonce": secrets.token_urlsafe(12), **body})

    async def __aenter__(self) -> FakeController:  # noqa: PYI034
        self.server = await serve(self._handle, "127.0.0.1", 0, max_size=wire.HARD_FRAME_BYTES, ping_interval=None)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self.server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.server.wait_closed(), 5)

    @property
    def url(self) -> str:
        return f"ws://localhost:{self.port}/v1/node/connect"

    async def _handle(self, ws: ServerConnection) -> None:
        nonce = wire.ulid()
        await ws.send(wire.Envelope("challenge", {"nonce": nonce}).encode())
        hello = wire.decode(await ws.recv())
        self.hellos.append(hello.body)
        ok = hello.type == "hello" and hello.body.get("nonce") == nonce and signing.verify(
            self.node_pubkey, hello.body.get("sig"), wire.hello_message(nonce, self.node_id))
        if not ok or self.refuse_next or self.refuse_all:
            self.refuse_next = False
            await ws.send(wire.error_envelope("denied", "no").encode())
            await ws.close()
            return
        await ws.send(wire.Envelope("welcome", {"node_id": self.node_id, "server_time": wire.iso(), "config": {},
                                                "revoked_grants": [], **self.welcome_extra}).encode())
        peer = Peer(ws)
        await self.peers.put(peer)
        with contextlib.suppress(Exception):
            async for frame in ws:
                if isinstance(frame, bytes):
                    await peer.binary.put(frame)
                else:
                    peer.add(wire.decode(frame))

    async def peer(self, timeout: float = 5.0) -> Peer:
        return await asyncio.wait_for(self.peers.get(), timeout)


async def node_runtime(home: Path, fake: FakeController | None = None, *, lanes=("personal/reddit",),
                       providers=None, config: NodeConfig | None = None) -> Runtime:
    """A node runtime with the real policy and channel services; the channel is built, not started."""
    from channel_support import extra_tools


    home.mkdir(parents=True, exist_ok=True)
    key = NodeKey.load_or_create(home / "secrets")
    if config is None:
        assert fake is not None
        config = NodeConfig(home=home, name="node-t", mcp_token="node-owner-token", mcp_port=0,
                            controller_url=fake.url, node_id=fake.node_id, org_id="org_test",
                            controller_pubkey=signing.public_b64(fake.key), grant_pubkey=signing.public_b64(fake.grant_key),
                            **ControllerCeiling.full().settings())
    manager = ControlLanes(config)
    for lane in lanes:
        await manager.create(LaneSpec(id=lane))
    runtime = Runtime(config=config, lanes=manager, interaction=FakeInteraction())
    build_services(runtime, ["openajar.policy.service", "openajar.node.channel.service"])
    build_node_app(config, runtime, providers=providers or [*mcp_providers.tool_providers(), extra_tools])
    channel = runtime.get("channel")
    if channel is not None:
        channel.timings = FAST
        channel._backoff.low, channel._backoff.high = FAST.backoff_min, FAST.backoff_max
    runtime.register("node_key", key)
    return runtime


async def start(runtime: Runtime) -> None:
    await start_services(runtime)


async def stop(runtime: Runtime) -> None:
    await stop_services(runtime)


def node_pubkey(home: Path) -> str:
    home.mkdir(parents=True, exist_ok=True)
    return NodeKey.load_or_create(home / "secrets").public


def frame_json(envelope: wire.Envelope) -> dict:
    return json.loads(envelope.encode())
