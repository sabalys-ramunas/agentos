"""Streams over the channel: credit flow control, the live view's input rules, terminal PTYs."""

import asyncio
import struct
import time
from types import SimpleNamespace

import pytest
from channel_fake_controller import NODE_ID, FakeController, node_pubkey, node_runtime, start, stop
from testkit.timing import until

from openajar.core.models import LaneState, ViewerInfo
from openajar.node import vnc_relay
from openajar.node.channel import wire
from openajar.node.channel.node_streams import TerminalHolds
from openajar.node.channel.streams import StreamPipe, StreamStalled, StreamTable

RFB_CLIENT_HANDSHAKE = b"RFB 003.008\n" + bytes([1]) + bytes([1])  # version, security None, shared init
KEY_DOWN = struct.pack(">BBxxI", 4, 1, 0x61)


class Wire:
    """Both ends of a pipe in memory."""

    def __init__(self):
        self.text: list[wire.Envelope] = []
        self.binary: list[bytes] = []

    async def send_text(self, envelope):
        self.text.append(envelope)

    async def send_binary(self, frame):
        self.binary.append(frame)


async def test_a_writer_never_has_more_than_its_credit_in_flight():
    out = Wire()
    pipe = StreamPipe(wire.ulid(), "file", out.send_text, out.send_binary, credit=1000, stall_after=1)
    task = asyncio.create_task(pipe.write(b"x" * 2500))
    await asyncio.sleep(0.05)
    assert sum(len(f) - 16 for f in out.binary) == 1000 and not task.done()
    pipe.add_credit(1000)
    assert await until(lambda: sum(len(f) - 16 for f in out.binary) == 2000)
    pipe.add_credit(5000)
    await task
    assert sum(len(f) - 16 for f in out.binary) == 2500
    assert all(wire.parse_stream_frame(f)[0] == pipe.id for f in out.binary)
    stalled = StreamPipe(wire.ulid(), "file", out.send_text, out.send_binary, credit=0, stall_after=0.1)
    with pytest.raises(StreamStalled):
        await stalled.write(b"y")


async def test_a_reader_returns_credit_as_it_consumes_and_closes_a_peer_that_overruns():
    out = Wire()
    table = StreamTable(out.send_text, out.send_binary)
    pipe = table.new(wire.ulid(), "terminal")
    await table.on_binary(wire.stream_frame(pipe.id, b"a" * 100_000))
    assert await pipe.read() == b"a" * 100_000
    await pipe.consumed(100_000)
    credits = [e for e in out.text if e.type == "stream_credit"]
    assert credits and credits[0].body == {"stream_id": pipe.id, "bytes": 100_000}
    # the peer may now send its remaining window plus what was returned, and not a byte more
    await table.on_binary(wire.stream_frame(pipe.id, b"b" * (wire.INITIAL_CREDIT + 1)))
    assert table.get(pipe.id) is None and pipe.closed
    closes = [e for e in out.text if e.type == "stream_close"]
    assert closes and closes[0].body["reason"] == "credit_exceeded"
    with pytest.raises(ValueError):
        table.on_credit({"stream_id": pipe.id, "bytes": -5})
    await table.on_binary(b"short")  # a bad header is dropped, nothing breaks


class FakeVnc:
    """A TCP server standing in for x11vnc: sends a greeting, records what reaches it. Only
    connections that speak RFB count: a local port probe (some desktops run one) is ignored."""

    def __init__(self):
        self.connections: list[tuple[asyncio.StreamWriter, bytearray]] = []
        self.port = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()

    @property
    def received(self) -> bytes:
        return b"".join(bytes(data) for _, data in self.connections if data.startswith(b"RFB "))

    def write(self, payload: bytes) -> None:
        for writer, data in self.connections:
            if not data or data.startswith(b"RFB "):
                writer.write(payload)

    async def _client(self, reader, writer):
        data = bytearray()
        self.connections.append((writer, data))
        writer.write(b"RFB 003.008\n")
        await writer.drain()
        while chunk := await reader.read(65536):
            data += chunk


@pytest.fixture(autouse=True)
def tcp_upstream(monkeypatch):
    """The fake x11vnc here is a TCP server: only a test may let the relay dial one."""
    monkeypatch.setattr(vnc_relay, "TEST_TCP_UPSTREAM", True)


@pytest.fixture
async def node(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        await start(runtime)
        peer = await fake.peer()
        await asyncio.wait_for(runtime.channel.welcomed.wait(), 15)  # the fake sees the peer before the node is in
        yield fake, runtime, peer
        await stop(runtime)


async def open_vnc(peer, mode):
    stream_id = wire.ulid()
    sent = await peer.send("stream_open", {"stream_id": stream_id, "kind": "vnc", "lane": "personal/reddit",
                                           "params": {"mode": mode}})
    ready = await peer.reply_to(sent.id)
    assert ready.type == "stream_ready", ready.body
    return stream_id


async def test_live_view_forwards_input_only_while_a_human_holds_control(node):
    _fake, runtime, peer = node
    async with FakeVnc() as vnc:
        await runtime.lanes.open("personal/reddit")
        runtime.lanes.lanes["personal/reddit"].viewer = ViewerInfo(display=":280", vnc_port=vnc.port,
                                                                     viewer_port=6280)
        stream_id = await open_vnc(peer, "control")
        picture = wire.parse_stream_frame(await asyncio.wait_for(peer.binary.get(), 5))
        assert picture == (stream_id, b"RFB 003.008\n")  # the picture flows from x11vnc
        await peer.send_raw(wire.stream_frame(stream_id, RFB_CLIENT_HANDSHAKE + KEY_DOWN))
        await asyncio.sleep(0.3)
        assert vnc.received == RFB_CLIENT_HANDSHAKE  # agents hold the lane: the key is dropped
        await runtime.lanes.take_control("personal/reddit", by="owner")
        await peer.send_raw(wire.stream_frame(stream_id, KEY_DOWN))
        assert await until(lambda: vnc.received == RFB_CLIENT_HANDSHAKE + KEY_DOWN)
        assert runtime.lanes.human_inputs == 1
        # control goes back to agents: the held key is released at once
        await runtime.lanes.hand_back("personal/reddit", by="owner")
        assert await until(lambda: vnc.received.endswith(struct.pack(">BBxxI", 4, 0, 0x61)))
        await peer.send("stream_close", {"stream_id": stream_id, "reason": "closed"})


async def test_a_view_only_stream_never_forwards_input(node):
    _fake, runtime, peer = node
    async with FakeVnc() as vnc:
        await runtime.lanes.open("personal/reddit")
        runtime.lanes.lanes["personal/reddit"].viewer = ViewerInfo(display=":280", vnc_port=vnc.port,
                                                                     viewer_port=6280)
        await runtime.lanes.take_control("personal/reddit", by="owner")
        stream_id = await open_vnc(peer, "view")
        await peer.send_raw(wire.stream_frame(stream_id, RFB_CLIENT_HANDSHAKE + KEY_DOWN))
        await asyncio.sleep(0.3)
        assert vnc.received == RFB_CLIENT_HANDSHAKE


async def test_live_view_output_respects_the_controllers_credit(node):
    _fake, runtime, peer = node
    async with FakeVnc() as vnc:
        await runtime.lanes.open("personal/reddit")
        runtime.lanes.lanes["personal/reddit"].viewer = ViewerInfo(display=":280", vnc_port=vnc.port,
                                                                     viewer_port=6280)
        stream_id = await open_vnc(peer, "view")
        await asyncio.wait_for(peer.binary.get(), 5)  # the greeting
        vnc.write(b"z" * (600 * 1024))
        got = len(b"RFB 003.008\n")
        await asyncio.sleep(0.5)
        while not peer.binary.empty():
            got += len(wire.parse_stream_frame(peer.binary.get_nowait())[1])
        assert got == wire.INITIAL_CREDIT
        await peer.send("stream_credit", {"stream_id": stream_id, "bytes": 1 << 20})
        async with asyncio.timeout(5):
            while got < 600 * 1024 + 12:
                got += len(wire.parse_stream_frame(await peer.binary.get())[1])
        assert got == 600 * 1024 + 12


class FakeShell:
    def __init__(self, lane_id, sid="sh_test"):
        self.id = sid
        self.lane_id = lane_id
        self.running = True
        self.written = bytearray()
        self.sizes = []
        self.queue = asyncio.Queue()

    def subscribe(self, replay=True):
        self.queue.put_nowait(b"$ ")
        return self.queue

    def unsubscribe(self, queue):
        pass

    async def write(self, data):
        self.written += data
        self.queue.put_nowait(b"echo:" + data)
        return len(data)

    def resize(self, cols, rows):
        self.sizes.append((cols, rows))


class FakeEnvs:
    def __init__(self, lanes=("dev/box",)):
        self.shells = {}
        self.lanes = set(lanes)

    async def shell_open(self, lane_id, *, cols, rows, term):
        shell = FakeShell(lane_id, "sh_test" if not self.shells else f"sh_test{len(self.shells)}")
        shell.sizes.append((cols, rows))
        self.shells[shell.id] = shell
        return shell

    def shell(self, sid):
        return self.shells[sid]

    def has(self, lane_id):
        return lane_id in self.lanes

    def list(self):
        return [SimpleNamespace(lane=SimpleNamespace(id=lane, kind="terminal"), state=LaneState.WARM,
                                last_used=None, quarantine_reason=None) for lane in sorted(self.lanes)]


async def take(fake, peer, lane="dev/box", by="u_1", action="take"):
    sent = await peer.send("lane_control", fake.fresh("lane_control", {"lane": lane, "action": action, "by": by,
                                                                      "ts": wire.iso()}))
    await asyncio.sleep(0.2)
    assert not [e for e in peer.seen if e.reply_to == sent.id], "lane_control was refused"


async def open_terminal(peer, **params):
    stream_id = wire.ulid()
    sent = await peer.send("stream_open", {"stream_id": stream_id, "kind": "terminal", "lane": "dev/box",
                                           "params": {"cols": 100, "rows": 30, **params}})
    ready = await peer.reply_to(sent.id)
    assert ready.type == "stream_ready", ready.body
    return stream_id, ready


async def typed(peer, stream_id, shell, data: bytes) -> bytes:
    before = bytes(shell.written)
    await peer.send_raw(wire.stream_frame(stream_id, data))
    await asyncio.sleep(0.2)
    return bytes(shell.written)[len(before):]


async def test_terminal_streams_bytes_both_ways_and_resizes(node):
    fake, runtime, peer = node
    envs = FakeEnvs()
    runtime.register("envs", envs)
    await take(fake, peer)
    stream_id, ready = await open_terminal(peer, mode="control", by="u_1")
    assert ready.body["session"] == "sh_test"
    assert wire.parse_stream_frame(await asyncio.wait_for(peer.binary.get(), 5)) == (stream_id, b"$ ")
    await peer.send_raw(wire.stream_frame(stream_id, b"ls\n"))
    assert wire.parse_stream_frame(await asyncio.wait_for(peer.binary.get(), 5))[1] == b"echo:ls\n"
    shell = envs.shells["sh_test"]
    assert bytes(shell.written) == b"ls\n"
    await peer.send("resize", {"stream_id": stream_id, "cols": 120, "rows": 40})
    assert await until(lambda: shell.sizes == [(100, 30), (120, 40)])
    bad = await peer.send("resize", {"stream_id": stream_id, "cols": 0, "rows": 40})
    assert (await peer.reply_to(bad.id)).body["code"] == "invalid"
    shell.queue.put_nowait(None)  # the shell exits
    closed = await peer.recv("stream_close", where=lambda e: e.body["stream_id"] == stream_id)
    assert closed.body["reason"] == "exit"


async def test_terminal_input_needs_control_mode_and_the_holder(node):
    fake, runtime, peer = node
    envs = FakeEnvs()
    runtime.register("envs", envs)
    viewer, _ = await open_terminal(peer)  # view mode, nobody holds the lane
    shell = envs.shells["sh_test"]
    assert await typed(peer, viewer, shell, b"rm -rf /\n") == b""
    driver, _ = await open_terminal(peer, mode="control", by="u_1", session="sh_test")
    assert await typed(peer, driver, shell, b"ls\n") == b""  # control mode, but nobody took the lane
    await take(fake, peer, by="u_1")
    assert await typed(peer, viewer, shell, b"whoami\n") == b""  # a viewer never types, even now
    other, _ = await open_terminal(peer, mode="control", by="u_2", session="sh_test")
    assert await typed(peer, other, shell, b"id\n") == b""  # not the holder
    assert await typed(peer, driver, shell, b"ls\n") == b"ls\n"
    await take(fake, peer, action="hand_back")
    assert await typed(peer, driver, shell, b"pwd\n") == b""  # handed back: input stops at once
    assert runtime.channel.terminal_holds.holder("dev/box") is None


async def test_a_person_at_the_node_holding_a_terminal_is_not_a_controller_holder(node):
    fake, runtime, peer = node
    envs = FakeEnvs()
    runtime.register("envs", envs)
    await take(fake, peer, by=None)  # a take with no `by` names the controller itself
    assert runtime.channel.terminal_holds.holder("dev/box") == "controller"
    driver, _ = await open_terminal(peer, mode="control", by="u_1")
    assert await typed(peer, driver, envs.shells["sh_test"], b"ls\n") == b""


async def test_a_held_terminal_shows_in_its_lane_event_and_goes_back_when_idle(node):
    fake, runtime, peer = node
    envs = FakeEnvs()
    runtime.register("envs", envs)
    await take(fake, peer, by="u_1")
    event = await peer.recv("event", where=lambda e: e.body["kind"] == "lane" and e.body["payload"]["id"] == "dev/box"
                            and (e.body["payload"]["control"] or {}).get("state") == "human")
    assert event.body["payload"]["control"]["by"] == "controller:u_1"
    holds = runtime.channel.terminal_holds
    holds.idle_seconds = 0.3
    driver, _ = await open_terminal(peer, mode="control", by="u_1")
    shell = envs.shells["sh_test"]
    for _ in range(3):  # typing keeps the hold past the idle time
        assert await typed(peer, driver, shell, b"x") == b"x"
    await asyncio.sleep(0.5)
    assert await typed(peer, driver, shell, b"late") == b""  # idle: back to agents
    back = await peer.recv("event", where=lambda e: e.body["kind"] == "lane" and e.body["payload"]["id"] == "dev/box"
                           and e.body["payload"]["control"] is None)
    assert back.body["payload"]["state"] == "warm"


async def test_a_reconnect_with_the_session_attaches_to_the_same_shell(node):
    _fake, runtime, peer = node
    envs = FakeEnvs()
    runtime.register("envs", envs)
    first, ready = await open_terminal(peer)
    await peer.send("stream_close", {"stream_id": first, "reason": "closed"})
    _again, ready_again = await open_terminal(peer, session=ready.body["session"])
    assert ready_again.body["session"] == ready.body["session"] and list(envs.shells) == ["sh_test"]
    sent = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": "terminal", "lane": "dev/box",
                                           "params": {"mode": "type"}})
    assert (await peer.reply_to(sent.id)).body["code"] == "invalid"


async def test_terminal_lanes_take_only_take_and_hand_back(node):
    fake, runtime, peer = node
    runtime.register("envs", FakeEnvs())
    sent = await peer.send("lane_control", fake.fresh("lane_control", {"lane": "dev/box", "action": "stop_agent",
                                                                      "ts": wire.iso()}))
    assert (await peer.reply_to(sent.id)).body["code"] == "invalid"


def test_terminal_holds_count_input_and_go_back_when_idle():
    now = [1000.0]
    holds = TerminalHolds(600, clock=lambda: now[0])
    holds.take("dev/box", "controller:u_1")
    now[0] += 500
    holds.note_input("dev/box")
    now[0] += 500
    assert holds.holder("dev/box") == "controller:u_1"  # input 500 s ago: still held
    assert holds.reap() == []
    now[0] += 101
    assert holds.reap() == ["dev/box"] and holds.holder("dev/box") is None and holds.control("dev/box") is None
    holds.note_input("dev/box")  # input after hand back takes nothing back
    assert holds.holder("dev/box") is None


def test_a_terminal_hold_always_ends_and_uses_a_monotonic_clock():
    now = [1000.0]
    holds = TerminalHolds(0, clock=lambda: now[0], wall=lambda: 1.7e9)  # 0: a browser lane's "never"
    assert holds.idle_seconds == 600
    holds.take("dev/box", "controller:u_1")
    assert holds.control("dev/box")["since"] == wire.iso(1.7e9)
    now[0] += 601
    assert holds.reap() == ["dev/box"]
    assert TerminalHolds(60).clock is time.monotonic


class FakeFreeze:
    def __init__(self):
        self.lanes: set[str] = set()

    def frozen_for(self, lane_id):
        return SimpleNamespace(scope="lane", target=lane_id) if lane_id in self.lanes else None

    def message(self, lane_id, rec):
        return f"{lane_id} is frozen"


async def test_a_freeze_closes_control_streams_and_refuses_new_ones_but_viewers_stay(node):
    fake, runtime, peer = node
    envs = FakeEnvs()
    runtime.register("envs", envs)
    freeze = FakeFreeze()
    runtime.register("freeze", freeze)
    await take(fake, peer, by="u_1")
    driver, _ = await open_terminal(peer, mode="control", by="u_1")
    viewer, _ = await open_terminal(peer, session="sh_test")
    freeze.lanes.add("dev/box")
    runtime.get("live_sockets").poke()  # what FreezeService.mark does
    closed = await peer.recv("stream_close", where=lambda e: e.body["stream_id"] == driver)
    assert closed.body["reason"] == "frozen"
    assert runtime.channel.conn.streams.get(viewer) is not None  # a person may still look
    sent = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": "terminal", "lane": "dev/box",
                                           "params": {"mode": "control", "by": "u_1", "session": "sh_test"}})
    refused = await peer.reply_to(sent.id)
    assert refused.type == "error" and refused.body["code"] == "frozen"
    assert runtime.get("live_sockets").entries() == []
