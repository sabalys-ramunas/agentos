"""Live sockets on viewer links (openajar.core.live): a freeze, an expiry or a revoke closes the relay and
the event stream; an owner link keeps them through a freeze; a renew before expiry moves the expiry out.

Built on test_viewer_app's fakes: a FakeVnc behind a real uvicorn, the lane manager from test_lanes, the
clock and the SSE reader. The heartbeat never fires (the interval is an hour): each test calls
`live.sweep()` itself, after it has moved the fake clock or the freeze.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from test_lanes import make
from test_viewer_app import LANE, Clock, EventReader, serve, tokens_at
from test_vnc_relay import HANDSHAKE_IN, HANDSHAKE_OUT, FakeVnc, fbur
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from openajar.core.live import LiveSockets
from openajar.core.models import LaneSpec
from openajar.node import viewer_app, vnc_relay
from openajar.node.viewer_app import build_viewer_app
from openajar.node.viewer_tokens import ViewTokens

GREETING = b"RFB 003.008\n"
WORKSPACE = LANE.split("/", 1)[0]
# What a freeze covers, as the node's registry answers frozen_for: this lane, its workspace, or the node.
SCOPES = {
    "lane": lambda lane_id: lane_id == LANE,
    "workspace": lambda lane_id: lane_id.split("/", 1)[0] == WORKSPACE,
    "node": lambda lane_id: True,
}


@pytest.fixture(autouse=True)
def tcp_upstream(monkeypatch):
    """The lanes here hand out the TCP port of a FakeVnc: only a test may let the relay dial one."""
    monkeypatch.setattr(vnc_relay, "TEST_TCP_UPSTREAM", True)


class Freezer:
    """The freeze registry as the viewer reads it: frozen_for(lane_id) is a record while the lane is
    covered by the freeze, else None. What the freeze covers is the test's choice."""

    def __init__(self):
        self.covers = lambda lane_id: False

    def freeze(self, covers) -> None:
        self.covers = covers

    def frozen_for(self, lane_id: str) -> dict | None:
        return {"reason": "test freeze"} if self.covers(lane_id) else None


class Stream(EventReader):
    async def end(self, limit: float = 5.0) -> None:
        """Read the rest of the body: the server must end the stream after `ended`."""

        async def drain():
            async for _ in self._chunks:
                pass

        await asyncio.wait_for(drain(), limit)


@dataclass
class LiveNode:
    mgr: Any
    vnc: FakeVnc
    host: str
    live: LiveSockets
    freezer: Freezer
    clock: Clock
    tokens: ViewTokens

    def token(self, mode: str = "control", *, ttl: int | None = None, owner: bool = False) -> str:
        return self.tokens.issue(LANE, mode, ttl, owner=owner).token

    def ws(self, token: str) -> str:
        return f"ws://{self.host}/viewer-ws/{LANE}?t={token}"

    def events_url(self, token: str) -> str:
        return f"http://{self.host}/view/{LANE}/events?t={token}"


@pytest.fixture
async def live_node(tmp_path, monkeypatch):
    """A warm lane whose 'x11vnc' is a FakeVnc, behind the viewer app on a real server, with a live
    registry and a freeze registry the test controls. Tokens run on a fake clock."""
    monkeypatch.setattr(viewer_app, "EVENT_POLL_SECONDS", 0.05)
    clock = Clock()
    tokens = tokens_at(tmp_path, clock, default_ttl=600)
    freezer = Freezer()
    live = LiveSockets(interval=3600)
    async with FakeVnc() as vnc:
        mgr, *_ = make(tmp_path, vnc_base=vnc.port, control_idle_seconds=600)
        await mgr.create(LaneSpec(id=LANE))
        await mgr.open(LANE)
        app = build_viewer_app(mgr, mgr.config, tokens=tokens, live=live, freeze=lambda: freezer)
        async with serve(app) as host:
            yield LiveNode(mgr, vnc, host, live, freezer, clock, tokens)
        live.poke()  # the registry's task ends once nothing is left to watch
        await mgr.shutdown()


@dataclass
class Watch:
    ws: Any
    stream: Stream


@contextlib.asynccontextmanager
async def watching(node: LiveNode, token: str):
    """One relay (past its greeting) and one event stream (past its hello) on the same link."""
    async with connect(node.ws(token)) as ws:
        assert await ws.recv() == GREETING
        async with httpx.AsyncClient(timeout=5) as client, client.stream("GET", node.events_url(token)) as r:
            assert r.status_code == 200
            stream = Stream(r)
            await stream.until(lambda ev: ev[-1][0] == "hello")
            yield Watch(ws, stream)


def assert_open(live: LiveSockets, count: int = 2) -> None:
    entries = live.entries()
    assert len(entries) == count and not any(e.closed.is_set() for e in entries), entries


async def closed_with(ws: Any, code: int) -> None:
    """The server closed the socket with `code`."""
    with pytest.raises(ConnectionClosed) as e:
        await asyncio.wait_for(ws.recv(), 5)
    assert e.value.rcvd is not None and e.value.rcvd.code == code, e.value


async def ended(stream: Stream) -> None:
    """The stream sent `ended` with a reason, and then its body ended."""
    events = await stream.until(lambda ev: ev[-1][0] == "ended")
    reason = events[-1][1].get("reason")
    assert isinstance(reason, str) and reason
    await stream.end()


async def until_empty(live: LiveSockets, limit: float = 5.0) -> None:
    """A relay or a stream leaves the registry in its own handler, a moment after the client sees the close.
    This waits for that handler; nothing here is timed."""

    async def wait():
        while live.entries():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), limit)


# ---- a freeze ------------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["lane", "workspace", "node"])
async def test_a_control_link_closes_on_a_freeze(live_node, scope):
    node = live_node
    async with watching(node, node.token("control")) as w:
        assert sorted((e.kind, e.lane) for e in node.live.entries()) == [
            ("viewer", LANE),
            ("viewer_events", LANE),
        ]
        node.freezer.freeze(SCOPES[scope])
        await node.live.sweep()
        await closed_with(w.ws, 4403)
        await ended(w.stream)


async def test_an_owner_link_keeps_both_sockets_through_a_freeze(live_node):
    node = live_node
    async with watching(node, node.token("control", owner=True)) as w:
        node.freezer.freeze(SCOPES["node"])
        assert await node.live.sweep() == 0
        assert_open(node.live)
        # the relay still carries the owner's traffic to the lane
        await w.ws.send(HANDSHAKE_IN)
        await node.vnc.until(lambda: bytes(node.vnc.received) == HANDSHAKE_OUT)
        await w.ws.send(fbur())
        await node.vnc.until(lambda: bytes(node.vnc.received) == HANDSHAKE_OUT + fbur())
        # and the owner can still take over: the event stream carries the change
        await node.mgr.take_control(LANE, by="owner")
        events = await w.stream.until(lambda ev: ev[-1][0] == "control")
        assert events[-1][1]["control"]["state"] == "human"


async def test_a_freeze_of_another_workspace_leaves_the_link_open(live_node):
    node = live_node
    async with watching(node, node.token("control")):
        node.freezer.freeze(lambda lane_id: lane_id.startswith("work/"))
        await node.live.sweep()
        assert_open(node.live)


async def test_a_new_non_owner_connection_is_refused_while_frozen(live_node):
    node = live_node
    node.freezer.freeze(SCOPES["lane"])
    token = node.token("control")
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(node.events_url(token))
    assert r.status_code == 409 and r.json()["error"]["code"] == "frozen"
    async with connect(node.ws(token)) as ws:
        await closed_with(ws, 4403)
    await until_empty(node.live)


async def test_the_owners_link_still_connects_while_frozen(live_node):
    node = live_node
    node.freezer.freeze(SCOPES["node"])
    async with watching(node, node.token("control", owner=True)):
        assert_open(node.live)


# ---- expiry, renew and revoke --------------------------------------------------------------


@pytest.mark.parametrize("owner", [False, True], ids=["link", "owner_link"])
async def test_expiry_closes_both_sockets(live_node, owner):
    node = live_node
    async with watching(node, node.token("control", ttl=60, owner=owner)) as w:
        node.clock.now += 59
        await node.live.sweep()
        assert_open(node.live)
        node.clock.now += 1  # now == expires_at: expired
        await node.live.sweep()
        await closed_with(w.ws, 4401)
        await ended(w.stream)


async def test_a_renew_before_expiry_keeps_both_sockets_past_the_original_expiry(live_node):
    node = live_node
    token = node.token("control", ttl=60)
    original = node.tokens.verify(token, LANE).expires_at
    async with (
        watching(node, token) as w,
        httpx.AsyncClient(headers={"origin": f"http://{node.host}"}) as client,
    ):
        node.clock.now = original - 5
        r = await client.post(f"http://{node.host}/view/{LANE}/renew?t={token}")
        assert r.status_code == 200
        renewed = node.tokens.verify(r.json()["token"], LANE).expires_at
        assert renewed > original

        node.clock.now = original + 1  # past the original expiry, inside the renewed one
        await node.live.sweep()
        assert_open(node.live)

        node.clock.now = renewed - 1
        await node.live.sweep()
        assert_open(node.live)

        node.clock.now = renewed  # the renewed link expires too
        await node.live.sweep()
        await closed_with(w.ws, 4401)
        await ended(w.stream)


@pytest.mark.parametrize("owner", [False, True], ids=["link", "owner_link"])
async def test_revoke_closes_both_sockets(live_node, owner):
    node = live_node
    async with watching(node, node.token("control", owner=owner)) as w:
        node.tokens.revoke()  # replaces the key: every link already out stops verifying
        await node.live.sweep()
        await closed_with(w.ws, 4401)
        await ended(w.stream)


# ---- the registry ----------------------------------------------------------------------------


async def test_entries_leave_the_registry_once_their_sockets_close(live_node):
    node = live_node
    async with watching(node, node.token("control")) as w:
        assert_open(node.live)
        node.freezer.freeze(SCOPES["lane"])
        await node.live.sweep()
        await closed_with(w.ws, 4403)
        await ended(w.stream)
    await until_empty(node.live)
    assert node.live.entries() == []


# ---- acting on a frozen lane, and after the freeze ----------------------------------------------


async def test_a_non_owner_link_cannot_act_on_a_frozen_lane(live_node):
    """Hand back would clear the pause the freeze put on the lane: only the owner's own link may act."""
    node = live_node
    token, owner = node.token("control"), node.token("control", owner=True)
    node.freezer.freeze(SCOPES["lane"])
    async with httpx.AsyncClient(timeout=5, headers={"origin": f"http://{node.host}"}) as client:
        for path in ("control", "renew"):
            r = await client.post(f"http://{node.host}/view/{LANE}/{path}?t={token}", json={"action": "hand_back"})
            assert r.status_code == 403 and "frozen" in r.json()["error"]["message"].lower(), (path, r.text)
        r = await client.post(f"http://{node.host}/view/{LANE}/control?t={owner}", json={"action": "take"})
        assert r.status_code == 200, r.text


async def test_a_link_the_freeze_ended_stays_ended_after_the_unfreeze(live_node):
    """As a grant the freeze revoked: lifting the freeze does not bring the link back."""
    node = live_node
    token = node.token("control")
    async with watching(node, token) as w:
        node.freezer.freeze(SCOPES["lane"])
        await node.live.sweep()
        await closed_with(w.ws, 4403)
        await ended(w.stream)
    node.freezer.freeze(lambda lane_id: False)  # unfrozen
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(node.events_url(token))
    assert r.status_code == 409 and r.json()["error"]["code"] == "frozen"
    async with watching(node, node.token("control")):  # a new link works again
        assert_open(node.live)


async def test_a_burst_of_pointer_moves_never_pushes_out_a_control_change_or_the_end():
    import asyncio

    from openajar.node.viewer_app import put_event

    q: asyncio.Queue = asyncio.Queue(maxsize=4)
    put_event(q, ("control", "human"))
    for i in range(10):
        put_event(q, ("pointer", i))  # the queue fills; the newest moves are dropped, never the control
    put_event(q, ("ended", "frozen"))  # an old pointer move makes room
    got = [q.get_nowait() for _ in range(q.qsize())]
    assert got[0] == ("control", "human") and got[-1] == ("ended", "frozen") and len(got) == 4
    for i in range(3):
        put_event(q, ("action", i))
    put_event(q, ("action", 3))
    put_event(q, ("control", "agent"))  # no pointer to drop: the oldest goes, as before
    assert [q.get_nowait() for _ in range(q.qsize())][-1] == ("control", "agent")
