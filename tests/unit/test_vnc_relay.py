"""The RFB client-to-server filter and the relay, with crafted byte streams (no X, no x11vnc)."""

from __future__ import annotations

import asyncio
import os
import random
import shutil
import struct
import tempfile
from typing import Self

import pytest
from testkit.timing import until

from openajar.core.models import ViewerInfo
from openajar.node import vnc_relay
from openajar.node.vnc_relay import (
    CLOSE_LANE_NOT_RUNNING,
    CLOSE_NORMAL,
    CLOSE_PROTOCOL,
    CLOSE_UNSUPPORTED,
    CLOSE_VNC_UNREACHABLE,
    MAX_CUT_TEXT,
    RfbClientFilter,
    RfbProtocolError,
    VncRelay,
    upstream,
)

assert CLOSE_LANE_NOT_RUNNING == 4404

# ---- message builders ------------------------------------------------------------------


def version(minor: int = 8) -> bytes:
    return f"RFB 003.00{minor}\n".encode()


def pixel_format() -> bytes:
    return bytes([0, 0, 0, 0]) + bytes([32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 16, 8, 0, 0, 0, 0])


def encodings(*encs: int) -> bytes:
    return struct.pack(f">BxH{len(encs)}i", 2, len(encs), *encs)


def fbur(incremental: int = 1) -> bytes:
    return struct.pack(">BBHHHH", 3, incremental, 0, 0, 1920, 1080)


def key(down: int, keysym: int) -> bytes:
    return struct.pack(">BBxxI", 4, down, keysym)


def pointer(mask: int, x: int, y: int) -> bytes:
    return struct.pack(">BBHH", 5, mask, x, y)


def cut(text: bytes, extended: bool = False) -> bytes:
    return struct.pack(">Bxxxi", 6, -len(text) if extended else len(text)) + text


def continuous_updates() -> bytes:
    return struct.pack(">BBHHHH", 150, 1, 0, 0, 1920, 1080)


def fence(payload: bytes = b"") -> bytes:
    return struct.pack(">BxxxIB", 248, 0x80000000, len(payload)) + payload


def xvp() -> bytes:
    return struct.pack(">BxBB", 250, 1, 2)


def desktop_size(screens: int = 1) -> bytes:
    return struct.pack(">BxHHBx", 251, 1280, 720, screens) + bytes(16 * screens)


def qemu_key(down: int, keysym: int, keycode: int) -> bytes:
    return struct.pack(">BBHII", 255, 0, down, keysym, keycode)


HANDSHAKE_IN = version(8) + bytes([1]) + bytes([0])   # picks None, asks for exclusive access
HANDSHAKE_OUT = version(8) + bytes([1]) + bytes([1])  # always shared
NOVNC_ENCODINGS = (1, 7, -260, 16, 21, 5, 2, 6, 0, -26, -254, -223, -224, -258, -261, -308, -309, -312,
                   -313, -307, 0xC0A1E5CE - (1 << 32), -316, 0x574D5664, -239)
STRIPPED = tuple(e for e in NOVNC_ENCODINGS if e not in (-309, -316))

CONTROL = [pixel_format(), encodings(*NOVNC_ENCODINGS), fbur(0), continuous_updates(), fence(b"abc"), fbur()]
CONTROL_OUT = [pixel_format(), encodings(*STRIPPED), fbur(0), continuous_updates(), fence(b"abc"), fbur()]
INPUT = [pointer(0, 10, 20), pointer(1, 11, 21), pointer(0, 11, 21), key(1, 0x61), key(0, 0x61),
         cut(b"hello"), cut(b"\x00\x00\x00\x01xyz", extended=True), qemu_key(1, 0x62, 48), qemu_key(0, 0x62, 48)]
NEVER = [xvp(), desktop_size(2)]


def stream() -> tuple[bytes, bytes, bytes]:
    """(client bytes, forwarded without control, forwarded with control)."""
    parts_in, out_view, out_ctl = [HANDSHAKE_IN], [HANDSHAKE_OUT], [HANDSHAKE_OUT]
    for i, msg in enumerate(CONTROL):
        parts_in.append(msg)
        out_view.append(CONTROL_OUT[i])
        out_ctl.append(CONTROL_OUT[i])
        if i < len(INPUT):
            parts_in.append(INPUT[i])
            out_ctl.append(INPUT[i])
    for msg in INPUT[len(CONTROL):]:
        parts_in.append(msg)
        out_ctl.append(msg)
    parts_in.extend(NEVER)
    parts_in.append(fbur())
    out_view.append(fbur())
    out_ctl.append(fbur())
    return b"".join(parts_in), b"".join(out_view), b"".join(out_ctl)


def make(allow: bool | list = False, inputs: list | None = None) -> RfbClientFilter:
    flag = allow if isinstance(allow, list) else [allow]
    return RfbClientFilter(lambda: flag[0], (lambda: inputs.append(1)) if inputs is not None else None)


# ---- handshake -------------------------------------------------------------------------


def test_handshake_38_forces_shared():
    f = make()
    assert f.feed(HANDSHAKE_IN) == HANDSHAKE_OUT
    assert f.in_messages


def test_handshake_37_and_33():
    f = make()
    assert f.feed(version(7) + bytes([1, 0])) == version(7) + bytes([1, 1])
    f = make()  # 3.3: the server picks the security type, the client sends no choice
    assert f.feed(version(3) + bytes([0])) == version(3) + bytes([1])
    assert f.in_messages


@pytest.mark.parametrize("bad", [b"RFB 004.001\n", b"RFB 003.889\n", b"GET / HTTP/1.", b"RFB 003.008 ", b"\x00" * 12])
def test_handshake_rejects_unknown_versions(bad):
    with pytest.raises(RfbProtocolError, match="version"):
        make().feed(bad)


@pytest.mark.parametrize("security", [0, 2, 16, 19, 30, 255])
def test_handshake_only_allows_security_none(security):
    with pytest.raises(RfbProtocolError, match="security"):
        make().feed(version(8) + bytes([security]))


def test_partial_handshake_waits():
    f = make()
    assert f.feed(b"RFB 003") == b""
    assert f.feed(b".008\n") == version(8)
    assert f.feed(b"") == b""
    assert f.feed(bytes([1])) == bytes([1])
    assert not f.in_messages
    assert f.feed(bytes([0])) == bytes([1])
    assert f.in_messages


# ---- messages --------------------------------------------------------------------------


def test_view_only_forwards_the_picture_but_no_input():
    data, view, _ = stream()
    inputs: list = []
    f = make(False, inputs)
    assert f.feed(data) == view
    assert f.stats.forwarded_inputs == 0 and f.stats.dropped_inputs == len(INPUT)
    assert f.stats.dropped_other == len(NEVER)
    assert inputs == []


def test_control_forwards_input_and_counts_it():
    data, _, ctl = stream()
    inputs: list = []
    f = make(True, inputs)
    assert f.feed(data) == ctl
    assert f.stats.forwarded_inputs == len(INPUT) == len(inputs)
    assert f.stats.dropped_other == len(NEVER)  # xvp and SetDesktopSize never pass


@pytest.mark.parametrize("allow", [False, True])
def test_every_split_point_gives_the_same_output(allow):
    data, view, ctl = stream()
    expected = ctl if allow else view
    for i in range(len(data) + 1):
        f = make(allow)
        assert f.feed(data[:i]) + f.feed(data[i:]) == expected, f"split at {i}"


@pytest.mark.parametrize("allow", [False, True])
def test_every_pair_of_split_points(allow):
    data, view, ctl = stream()
    expected = ctl if allow else view
    for i in range(0, len(data) + 1, 3):
        for j in range(i, len(data) + 1, 5):
            f = make(allow)
            assert f.feed(data[:i]) + f.feed(data[i:j]) + f.feed(data[j:]) == expected, f"splits {i},{j}"


@pytest.mark.parametrize("allow", [False, True])
def test_byte_by_byte_and_random_chunks(allow):
    data, view, ctl = stream()
    expected = ctl if allow else view
    f = make(allow)
    assert b"".join(f.feed(bytes([b])) for b in data) == expected
    rng = random.Random(7)
    for _ in range(200):
        f = make(allow)
        out, pos = bytearray(), 0
        while pos < len(data):
            n = rng.randint(1, 40)
            out += f.feed(data[pos:pos + n])
            pos += n
        assert bytes(out) == expected


def test_output_is_always_whole_messages():
    """Whatever the chunking, each feed() returns a prefix-aligned run of whole messages."""
    data, _, ctl = stream()
    f = make(True)
    total = b""
    for b in data:
        out = f.feed(bytes([b]))
        total += out
        assert ctl.startswith(total)
        if total[len(HANDSHAKE_OUT):]:
            # the forwarded tail must end exactly on a message boundary of the expected stream
            assert len(total) in boundaries(ctl)


def boundaries(expected: bytes) -> set[int]:
    sizes = [12, 1, 1]
    rest = expected[14:]
    pos = 0
    f = RfbClientFilter(lambda: True)
    f._phase = vnc_relay._Phase.MESSAGES
    while pos < len(rest):
        f._buf = bytearray(rest[pos:])
        n = f._length()
        sizes.append(n)
        pos += n
    edges, total = set(), 0
    for s in sizes:
        total += s
        edges.add(total)
    return edges


def test_control_is_checked_per_message():
    flag = [True]
    f = RfbClientFilter(lambda: flag[0])
    f.feed(HANDSHAKE_IN)
    assert f.feed(pointer(0, 1, 1)) == pointer(0, 1, 1)
    flag[0] = False
    assert f.feed(pointer(0, 2, 2)) == b""
    flag[0] = True
    assert f.feed(pointer(0, 3, 3) + key(1, 0x41) + key(0, 0x41)) == pointer(0, 3, 3) + key(1, 0x41) + key(0, 0x41)


def test_losing_control_releases_held_keys_and_buttons():
    flag = [True]
    f = RfbClientFilter(lambda: flag[0])
    f.feed(HANDSHAKE_IN)
    f.feed(key(1, 0xFFE1) + key(1, 0x61) + pointer(1, 300, 200) + qemu_key(1, 0x62, 48))
    assert f.holding
    flag[0] = False
    out = f.feed(pointer(1, 310, 205))  # dropped, but first let go of everything held
    assert out == key(0, 0x61) + key(0, 0xFFE1) + qemu_key(0, 0x62, 48) + pointer(0, 300, 200)
    assert not f.holding
    assert f.feed(pointer(0, 320, 210) + key(0, 0x61)) == b""  # nothing more while not allowed
    assert f.stats.releases == 1


def test_releases_track_key_up_and_button_release():
    f = make(True)
    f.feed(HANDSHAKE_IN)
    f.feed(key(1, 0x61) + key(0, 0x61) + pointer(1, 5, 5) + pointer(0, 5, 5))
    assert not f.holding and f.release_all() == b""
    f.feed(pointer(4, 9, 9))  # wheel pressed and its release never came
    assert f.release_all() == pointer(0, 9, 9)


def test_release_all_before_the_handshake_is_empty():
    f = make(True)
    assert f.release_all() == b""
    f.feed(version(8))
    assert f.release_all() == b""


@pytest.mark.parametrize("bad", [bytes([7, 0, 0, 0]), bytes([1]), bytes([9, 9]), bytes([127]), bytes([249]),
                                 bytes([252, 0]), bytes([253, 0]), bytes([254, 0])])
def test_unknown_message_types_end_the_stream(bad):
    f = make(True)
    f.feed(HANDSHAKE_IN)
    with pytest.raises(RfbProtocolError, match="unknown"):
        f.feed(bad)


def test_bad_fence_and_qemu_messages_end_the_stream():
    f = make(True)
    f.feed(HANDSHAKE_IN)
    with pytest.raises(RfbProtocolError, match="fence"):
        f.feed(struct.pack(">BxxxIB", 248, 0, 65))
    f = make(True)
    f.feed(HANDSHAKE_IN)
    with pytest.raises(RfbProtocolError, match="QEMU"):
        f.feed(bytes([255, 1, 0, 0]))


def test_partial_headers_wait_for_more():
    f = make(True)
    f.feed(HANDSHAKE_IN)
    for msg in (encodings(1, 2, 3), cut(b"abc"), fence(b"x"), desktop_size(1), qemu_key(1, 1, 1)):
        for i in range(1, len(msg)):
            g = make(True)
            g.feed(HANDSHAKE_IN)
            assert g.feed(msg[:i]) == b""
            g.feed(msg[i:])


def test_oversized_clipboard_is_dropped_as_it_streams():
    f = make(True)
    f.feed(HANDSHAKE_IN)
    size = MAX_CUT_TEXT + 1
    header = struct.pack(">Bxxxi", 6, size)
    out = f.feed(header + b"a" * 1000)
    payload_left = size - 1000
    while payload_left > 0:
        n = min(payload_left, 300_000)
        out += f.feed(b"b" * n)
        payload_left -= n
    out += f.feed(pointer(0, 1, 2))
    assert out == pointer(0, 1, 2)
    assert f.stats.dropped_other == 1


def test_oversized_clipboard_in_one_chunk_with_a_message_after_it():
    f = make(True)
    f.feed(HANDSHAKE_IN)
    big = struct.pack(">Bxxxi", 6, MAX_CUT_TEXT + 5) + bytes(MAX_CUT_TEXT + 5)
    assert f.feed(big + key(1, 0x61)) == key(1, 0x61)


def test_extended_clipboard_negative_length_and_the_largest_negative():
    f = make(True)
    f.feed(HANDSHAKE_IN)
    msg = cut(b"\x10\x00\x00\x01", extended=True)
    assert f.feed(msg) == msg
    huge = struct.pack(">Bxxxi", 6, -(1 << 31))  # abs() is 2**31: dropped, not buffered
    assert f.feed(huge + bytes(10)) == b""
    assert f._skip == (1 << 31) - 10


def test_set_encodings_unchanged_when_nothing_to_strip():
    f = make()
    f.feed(HANDSHAKE_IN)
    msg = encodings(7, 16, 0, -223)
    assert f.feed(msg) == msg
    assert f.feed(encodings()) == encodings()
    assert f.feed(encodings(-316, -309)) == encodings()


def test_on_input_failure_never_breaks_the_stream():
    def boom():
        raise RuntimeError("bookkeeping")

    f = RfbClientFilter(lambda: True, boom)
    f.feed(HANDSHAKE_IN)
    assert f.feed(pointer(0, 1, 1) + pointer(0, 2, 2)) == pointer(0, 1, 1) + pointer(0, 2, 2)


# ---- the relay, against a fake WebSocket and a fake VNC server ----------------------------


class FakeWebSocket:
    """The parts of a Starlette WebSocket the relay uses."""

    def __init__(self, send_delay: float = 0.0):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent = bytearray()
        self.closed: tuple[int, str] | None = None
        self.send_delay = send_delay
        self.fail_sends = False

    def push(self, data: bytes) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "bytes": data})

    def push_text(self, text: str) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": text})

    def leave(self) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1001})

    async def receive(self) -> dict:
        return await self.incoming.get()

    async def send_bytes(self, data: bytes) -> None:
        if self.fail_sends:
            raise OSError("gone")
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        self.sent += data

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class FakeVnc:
    """A TCP server that sends a server greeting and records what it receives."""

    def __init__(self, greeting: bytes = b"RFB 003.008\n", *, path: str | None = None):
        self.greeting = greeting
        self.path = path
        self.received = bytearray()
        self.connections = 0
        self.closed = asyncio.Event()
        self.changed = asyncio.Event()  # set on every connection, chunk and close; see until()
        self.writer: asyncio.StreamWriter | None = None
        self.server: asyncio.Server | None = None

    async def __aenter__(self) -> Self:
        if self.path is not None:  # x11vnc's real shape on a node: a unix socket
            self.server = await asyncio.start_unix_server(self._handle, self.path)
            return self
        # A port the kernel picks, below the top 64: the viewer tests pass it as vnc_base, which
        # NodeConfig wants to leave room for max_slots ports below 65536 (macOS hands out ports up to
        # 65535, so a fixture without this failed now and then).
        for _ in range(100):
            self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
            if self.port <= 65535 - 64:
                break
            self.server.close()
            await self.server.wait_closed()
        return self

    async def __aexit__(self, *exc) -> None:
        if self.writer is not None:
            self.writer.close()
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        self.connections += 1
        self.writer = writer
        self.poke()
        try:
            writer.write(self.greeting)
            await writer.drain()
            while data := await reader.read(65536):
                self.received += data
                self.poke()
        except ConnectionError:  # closing with unread data in the buffer sends a reset
            pass
        finally:
            self.closed.set()
            self.poke()
            writer.close()

    def poke(self) -> None:
        """Wake every until(): something it may be waiting for changed. Tests that wait on state
        outside this server (a lane's counters) poke it from where that state changes."""
        self.changed.set()

    async def until(self, predicate, timeout: float = 10.0) -> None:
        """Wait until predicate() holds, re-checking it on every poke, never on a timer. The timeout
        is a budget for a slow machine, not part of the test."""
        try:
            async with asyncio.timeout(timeout):
                while not predicate():
                    self.changed.clear()
                    await self.changed.wait()
        except TimeoutError:
            raise AssertionError(f"timed out; received {bytes(self.received)!r}") from None


async def run_relay(ws, port, allow, **kw):
    relay = VncRelay(ws, host="127.0.0.1", port=port, allow_input=lambda: allow[0], **kw)
    return relay, asyncio.create_task(relay.run())


async def test_relay_pumps_both_ways_and_filters(tmp_path):
    async with FakeVnc() as vnc:
        ws = FakeWebSocket()
        allow = [False]
        noted: list = []
        relay, task = await run_relay(ws, vnc.port, allow, on_input=lambda: noted.append(1))
        await vnc.until(lambda: vnc.connections == 1)
        await until(lambda: ws.sent)
        assert bytes(ws.sent) == b"RFB 003.008\n"
        ws.push(HANDSHAKE_IN + fbur() + pointer(0, 5, 5))
        await vnc.until(lambda: len(vnc.received) >= len(HANDSHAKE_OUT) + 10)
        allow[0] = True
        ws.push(pointer(1, 7, 7))
        await vnc.until(lambda: bytes(vnc.received).endswith(pointer(1, 7, 7)))
        assert bytes(vnc.received) == HANDSHAKE_OUT + fbur() + pointer(1, 7, 7)
        assert noted == [1]
        allow[0] = False
        relay.control_changed()  # the human lost control while holding the button
        await vnc.until(lambda: bytes(vnc.received).endswith(pointer(0, 7, 7)))
        relay.control_changed()  # nothing held any more: nothing sent
        vnc.writer.write(b"framebuffer bytes")
        await vnc.writer.drain()
        await until(lambda: ws.sent.endswith(b"framebuffer bytes"))
        assert ws.sent.endswith(b"framebuffer bytes")
        ws.leave()
        await asyncio.wait_for(task, 2.0)
        await asyncio.wait_for(vnc.closed.wait(), 2.0)  # the viewer left: the TCP side closed too
        assert ws.closed is None and relay.close_reason == "the viewer left"


async def test_relay_closes_the_websocket_when_vnc_ends():
    async with FakeVnc() as vnc:
        ws = FakeWebSocket()
        _, task = await run_relay(ws, vnc.port, [False])
        await vnc.until(lambda: vnc.writer is not None)
        await asyncio.sleep(0.02)
        vnc.writer.close()
        await asyncio.wait_for(task, 2.0)
        assert ws.closed == (CLOSE_NORMAL, "the live view ended")


async def test_relay_protocol_error_closes_both_sides():
    async with FakeVnc() as vnc:
        ws = FakeWebSocket()
        _, task = await run_relay(ws, vnc.port, [True])
        ws.push(HANDSHAKE_IN)
        await vnc.until(lambda: bytes(vnc.received) == HANDSHAKE_OUT)
        ws.push(pointer(0, 1, 1) + bytes([99, 0, 0, 0]))  # fail closed: nothing of this chunk passes
        await asyncio.wait_for(task, 2.0)
        assert ws.closed[0] == CLOSE_PROTOCOL
        await asyncio.wait_for(vnc.closed.wait(), 2.0)
        assert bytes(vnc.received) == HANDSHAKE_OUT


async def test_relay_rejects_text_frames():
    async with FakeVnc() as vnc:
        ws = FakeWebSocket()
        _, task = await run_relay(ws, vnc.port, [True])
        ws.push_text("UkZCIDAwMy4wMDgK")  # websockify's base64 mode is not supported
        await asyncio.wait_for(task, 2.0)
        assert ws.closed[0] == CLOSE_UNSUPPORTED


async def test_relay_unreachable_vnc():
    with __import__("socket").socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # bound but not listening: connection refused
        ws = FakeWebSocket()
        relay = VncRelay(ws, host="127.0.0.1", port=port, allow_input=lambda: False, connect_timeout=1.0)
        await asyncio.wait_for(relay.run(), 3.0)
    assert ws.closed[0] == CLOSE_VNC_UNREACHABLE


async def test_relay_reaches_x11vnc_over_its_unix_socket():
    directory = tempfile.mkdtemp(prefix="openajar-t-")  # tmp_path can pass macOS's 104 byte socket limit
    try:
        async with FakeVnc(path=os.path.join(directory, "display-150.sock")) as vnc:
            ws = FakeWebSocket()
            relay = VncRelay(ws, **upstream(ViewerInfo(display=":150", vnc_socket=vnc.path)), allow_input=lambda: False)
            assert relay.port is None and relay.where == vnc.path
            task = asyncio.create_task(relay.run())
            await vnc.until(lambda: vnc.connections == 1)
            ws.push(HANDSHAKE_IN + fbur())
            await vnc.until(lambda: len(vnc.received) >= len(HANDSHAKE_OUT) + 10)
            ws.leave()
            await asyncio.wait_for(task, 2.0)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_upstream_prefers_the_socket_and_the_relay_takes_exactly_one(monkeypatch):
    monkeypatch.setattr(vnc_relay, "TEST_TCP_UPSTREAM", True)
    both = ViewerInfo(display=":150", vnc_socket="/s/display-150.sock", vnc_port=5910)
    assert upstream(both) == {"path": "/s/display-150.sock"}
    assert upstream(ViewerInfo(display=":150", vnc_port=5910)) == {"host": "127.0.0.1", "port": 5910}
    with pytest.raises(ValueError):
        upstream(ViewerInfo(display=":150"))


def test_a_node_never_dials_a_tcp_port_for_a_live_view(monkeypatch):
    """vnc_port is a test fake's upstream. A live view on a node dials its unix socket only: without the
    test flag a TCP port is refused, whatever the viewer says."""
    tcp_only = ViewerInfo(display=":150", vnc_port=5910)
    with pytest.raises(ValueError, match="unix socket"):
        upstream(tcp_only)
    monkeypatch.setattr(vnc_relay, "TEST_TCP_UPSTREAM", True)
    assert upstream(tcp_only) == {"host": "127.0.0.1", "port": 5910}
    with pytest.raises(ValueError):
        VncRelay(FakeWebSocket(), port=5910, path="/s/x.sock", allow_input=lambda: False)
    with pytest.raises(ValueError):
        VncRelay(FakeWebSocket(), allow_input=lambda: False)


async def test_relay_browser_gone_mid_send():
    async with FakeVnc() as vnc:
        ws = FakeWebSocket()
        ws.fail_sends = True
        relay, task = await run_relay(ws, vnc.port, [False])
        await asyncio.wait_for(task, 2.0)  # the greeting cannot be delivered
        assert ws.closed is None and relay.close_reason == "the viewer left"
        await asyncio.wait_for(vnc.closed.wait(), 2.0)


async def test_relay_backpressure_delivers_everything_in_order():
    async with FakeVnc() as vnc:
        ws = FakeWebSocket(send_delay=0.001)
        _, task = await run_relay(ws, vnc.port, [False], read_size=4096)
        await vnc.until(lambda: vnc.writer is not None)
        blob = bytes(range(256)) * 4096  # 1 MiB after the greeting
        vnc.writer.write(blob)
        await vnc.writer.drain()
        for _ in range(1000):
            if len(ws.sent) >= 12 + len(blob):
                break
            await asyncio.sleep(0.01)
        assert bytes(ws.sent) == b"RFB 003.008\n" + blob
        ws.leave()
        await asyncio.wait_for(task, 2.0)
