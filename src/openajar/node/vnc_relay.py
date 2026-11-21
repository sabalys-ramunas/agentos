"""Live-view relay: the viewer's WebSocket straight to the lane's x11vnc (its unix socket).

On a node x11vnc listens only on a unix socket in agentosd's private directory
(openajar.node.display), so this relay is the one way in. `upstream(viewer)` picks the socket;
a TCP host and port remain for the test fakes, which serve RFB on 127.0.0.1.

It replaces websockify on this path because read-only has to be enforced on the server.
The server-to-client stream (the picture) passes through untouched. The client-to-server
stream goes through `RfbClientFilter`, which parses it message by message:

- the RFB handshake (3.3, 3.7 or 3.8) is forwarded; the client must pick security type
  None (x11vnc runs with -nopw behind its private socket) and ClientInit is forced to shared, so a
  viewer can never disconnect the other viewers;
- SetPixelFormat, SetEncodings, FramebufferUpdateRequest, EnableContinuousUpdates and
  ClientFence are always forwarded (SetEncodings minus ExtendedMouseButtons and xvp, so
  every PointerEvent stays 6 bytes and nobody can ask the server to reboot);
- KeyEvent, PointerEvent, ClientCutText and the QEMU extended key event are input: they
  are forwarded only while `allow_input()` is true (control is HUMAN and the link's mode
  is control), and every forwarded one calls `on_input()`;
- SetDesktopSize and xvp are always dropped (the screen size is part of the lane's
  pinned fingerprint);
- anything else ends the connection, because its length is unknown and guessing would
  let a crafted stream smuggle input past the filter.

Only whole messages are ever forwarded, so `release_all()` can inject key and button
releases at any time: when input stops being allowed while the human still holds a key
or a mouse button, the lane must not be left with it stuck down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

log = logging.getLogger("openajar.vnc_relay")

# client-to-server message types (RFC 6143 section 7.5 and the community registry)
SET_PIXEL_FORMAT = 0
SET_ENCODINGS = 2
FRAMEBUFFER_UPDATE_REQUEST = 3
KEY_EVENT = 4
POINTER_EVENT = 5
CLIENT_CUT_TEXT = 6
ENABLE_CONTINUOUS_UPDATES = 150
CLIENT_FENCE = 248
XVP = 250
SET_DESKTOP_SIZE = 251
QEMU_CLIENT_MESSAGE = 255
QEMU_EXTENDED_KEY_EVENT = 0  # submessage of QEMU_CLIENT_MESSAGE

SECURITY_NONE = 1
ENCODING_XVP = -309
ENCODING_EXTENDED_MOUSE_BUTTONS = -316
STRIPPED_ENCODINGS = frozenset({ENCODING_XVP, ENCODING_EXTENDED_MOUSE_BUTTONS})

MAX_CUT_TEXT = 1 << 20     # larger clipboard pastes are dropped as they stream past
MAX_FENCE_PAYLOAD = 64     # RFC: a fence payload is at most 64 bytes
VERSION_RE = re.compile(rb"^RFB 003\.00([378])\n$")

ALWAYS_FORWARD = frozenset({SET_PIXEL_FORMAT, FRAMEBUFFER_UPDATE_REQUEST, ENABLE_CONTINUOUS_UPDATES, CLIENT_FENCE})
INPUT = frozenset({KEY_EVENT, POINTER_EVENT, CLIENT_CUT_TEXT, QEMU_CLIENT_MESSAGE})
NEVER_FORWARD = frozenset({XVP, SET_DESKTOP_SIZE})


class RfbProtocolError(Exception):
    """The client sent something the filter cannot parse safely; the relay closes."""


class _Phase(IntEnum):
    VERSION = 0
    SECURITY = 1
    CLIENT_INIT = 2
    MESSAGES = 3


@dataclass
class FilterStats:
    forwarded_inputs: int = 0
    dropped_inputs: int = 0
    dropped_other: int = 0
    releases: int = 0


class RfbClientFilter:
    """Streaming filter for the RFB client-to-server byte stream. Feed it whatever chunks
    arrive (a message may be split across frames, or one frame may hold many); it returns
    the bytes to forward, always whole messages."""

    def __init__(self, allow_input: Callable[[], bool], on_input: Callable[[], None] | None = None):
        self._allow_input = allow_input
        self._on_input = on_input
        self._buf = bytearray()
        self._phase = _Phase.VERSION
        self._skip = 0  # bytes left of an oversized message being dropped
        self._buttons = 0
        self._pointer = (0, 0)
        self._keys: set[int] = set()
        self._qemu_keys: dict[int, int] = {}  # keycode -> keysym
        self.stats = FilterStats()

    @property
    def in_messages(self) -> bool:
        """True once the handshake is done."""
        return self._phase == _Phase.MESSAGES

    @property
    def holding(self) -> bool:
        """True while a forwarded key or button is still down."""
        return bool(self._buttons or self._keys or self._qemu_keys)

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        if self._skip:
            n = min(self._skip, len(data))
            self._skip -= n
            data = data[n:]
        self._buf += data
        while self._buf and self._step(out):
            pass
        return bytes(out)

    def release_all(self) -> bytes:
        """Messages that let go of every key and button the human still holds."""
        if self._phase != _Phase.MESSAGES or not self.holding:
            return b""
        out = bytearray()
        for key in sorted(self._keys):
            out += struct.pack(">BBxxI", KEY_EVENT, 0, key)
        for keycode, keysym in sorted(self._qemu_keys.items()):
            out += struct.pack(">BBHII", QEMU_CLIENT_MESSAGE, QEMU_EXTENDED_KEY_EVENT, 0, keysym, keycode)
        if self._buttons:
            out += struct.pack(">BBHH", POINTER_EVENT, 0, *self._pointer)
        self._keys.clear()
        self._qemu_keys.clear()
        self._buttons = 0
        self.stats.releases += 1
        return bytes(out)

    # ---- parsing ----

    def _take(self, n: int) -> bytes | None:
        if len(self._buf) < n:
            return None
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def _step(self, out: bytearray) -> bool:
        """Handle one handshake item or one message. False when more bytes are needed."""
        if self._phase == _Phase.VERSION:
            chunk = self._take(12)
            if chunk is None:
                return False
            m = VERSION_RE.match(chunk)
            if not m:
                raise RfbProtocolError(f"unsupported RFB client version {chunk[:12]!r}")
            out += chunk
            self._phase = _Phase.SECURITY if m.group(1) in (b"7", b"8") else _Phase.CLIENT_INIT
            return True
        if self._phase == _Phase.SECURITY:
            chunk = self._take(1)
            if chunk is None:
                return False
            if chunk[0] != SECURITY_NONE:
                raise RfbProtocolError(f"security type {chunk[0]} is not allowed (only None)")
            out += chunk
            self._phase = _Phase.CLIENT_INIT
            return True
        if self._phase == _Phase.CLIENT_INIT:
            if self._take(1) is None:
                return False
            out.append(1)  # shared: a viewer never kicks the others off
            self._phase = _Phase.MESSAGES
            return True
        return self._message(out)

    def _length(self) -> int | None:
        """Total length of the message at the head of the buffer, or None until its header
        is complete."""
        buf = self._buf
        kind = buf[0]
        if kind == SET_PIXEL_FORMAT:
            return 20
        if kind == SET_ENCODINGS:
            return None if len(buf) < 4 else 4 + 4 * int.from_bytes(buf[2:4], "big")
        if kind == FRAMEBUFFER_UPDATE_REQUEST:
            return 10
        if kind == KEY_EVENT:
            return 8
        if kind == POINTER_EVENT:
            return 6
        if kind == CLIENT_CUT_TEXT:
            if len(buf) < 8:
                return None
            # Extended clipboard sends a negative length; the payload is its absolute value.
            return 8 + abs(int.from_bytes(buf[4:8], "big", signed=True))
        if kind == ENABLE_CONTINUOUS_UPDATES:
            return 10
        if kind == CLIENT_FENCE:
            if len(buf) < 9:
                return None
            if buf[8] > MAX_FENCE_PAYLOAD:
                raise RfbProtocolError(f"fence payload of {buf[8]} bytes (at most {MAX_FENCE_PAYLOAD})")
            return 9 + buf[8]
        if kind == XVP:
            return 4
        if kind == SET_DESKTOP_SIZE:
            return None if len(buf) < 8 else 8 + 16 * buf[6]
        if kind == QEMU_CLIENT_MESSAGE:
            if len(buf) < 2:
                return None
            if buf[1] != QEMU_EXTENDED_KEY_EVENT:
                raise RfbProtocolError(f"unsupported QEMU client message {buf[1]}")
            return 12
        raise RfbProtocolError(f"unknown client message type {kind}")

    def _message(self, out: bytearray) -> bool:
        need = self._length()
        if need is None:
            return False
        if need > 8 + MAX_CUT_TEXT:  # only a clipboard message can be this large
            take = min(need, len(self._buf))
            del self._buf[:take]
            self._skip = need - take
            self.stats.dropped_other += 1
            log.info("dropped a %d byte clipboard message (limit %d)", need - 8, MAX_CUT_TEXT)
            return True
        msg = self._take(need)
        if msg is None:
            return False
        kind = msg[0]
        if kind in ALWAYS_FORWARD:
            out += msg
        elif kind == SET_ENCODINGS:
            out += _strip_encodings(msg)
        elif kind in INPUT:
            self._input(msg, out)
        else:  # NEVER_FORWARD
            self.stats.dropped_other += 1
        return True

    def _input(self, msg: bytes, out: bytearray) -> None:
        if not self._allow_input():
            self.stats.dropped_inputs += 1
            out += self.release_all()
            return
        kind = msg[0]
        if kind == KEY_EVENT:
            down, key = msg[1], int.from_bytes(msg[4:8], "big")
            (self._keys.add if down else self._keys.discard)(key)
        elif kind == POINTER_EVENT:
            self._buttons = msg[1]
            self._pointer = (int.from_bytes(msg[2:4], "big"), int.from_bytes(msg[4:6], "big"))
        elif kind == QEMU_CLIENT_MESSAGE:
            down = int.from_bytes(msg[2:4], "big")
            keysym, keycode = int.from_bytes(msg[4:8], "big"), int.from_bytes(msg[8:12], "big")
            if down:
                self._qemu_keys[keycode] = keysym
            else:
                self._qemu_keys.pop(keycode, None)
        out += msg
        self.stats.forwarded_inputs += 1
        if self._on_input is not None:
            try:
                self._on_input()
            except Exception:  # bookkeeping must never break the live view
                log.exception("on_input failed")


def _strip_encodings(msg: bytes) -> bytes:
    count = int.from_bytes(msg[2:4], "big")
    encodings = struct.unpack(f">{count}i", msg[4:4 + 4 * count])
    kept = [e for e in encodings if e not in STRIPPED_ENCODINGS]
    if len(kept) == count:
        return msg
    return struct.pack(f">BxH{len(kept)}i", SET_ENCODINGS, len(kept), *kept)


# ---- the relay ------------------------------------------------------------------------

CLOSE_NORMAL = 1000
CLOSE_PROTOCOL = 1002
CLOSE_UNSUPPORTED = 1003
CLOSE_POLICY = 1008
CLOSE_ERROR = 1011
CLOSE_LANE_NOT_RUNNING = 4404
CLOSE_VNC_UNREACHABLE = 4502


# Test fakes only: a TCP upstream (ViewerInfo.vnc_port) is refused unless a test, or the fake-services
# demo, turns this on. No config key or environment variable sets it, so a node's live view dials its
# unix socket and nothing else.
TEST_TCP_UPSTREAM = False


def upstream(viewer: Any) -> dict[str, Any]:
    """VncRelay's upstream keywords for a ViewerInfo: its unix socket, else (with TEST_TCP_UPSTREAM) its
    TCP port."""
    if viewer.vnc_socket:
        return {"path": viewer.vnc_socket}
    if viewer.vnc_port is None or not TEST_TCP_UPSTREAM:
        raise ValueError("the viewer has no VNC upstream: a node's live view is its unix socket")
    return {"host": "127.0.0.1", "port": int(viewer.vnc_port)}


class VncRelay:
    """Pump bytes between an accepted Starlette WebSocket and the lane's VNC server.

    Backpressure is natural in both directions: the next WebSocket message is not read
    until the previous one is written and drained to the VNC server, and the next chunk is
    not read from the VNC server until the WebSocket send has completed.
    """

    def __init__(
        self,
        websocket: Any,
        *,
        host: str = "127.0.0.1",
        port: int | None = None,
        path: str | None = None,
        allow_input: Callable[[], bool],
        on_input: Callable[[], None] | None = None,
        connect_timeout: float = 5.0,
        read_size: int = 1 << 16,
        open_connection: Callable[..., Any] = asyncio.open_connection,
        open_unix_connection: Callable[..., Any] = asyncio.open_unix_connection,
    ):
        if (path is None) == (port is None):
            raise ValueError("VncRelay needs exactly one of path (a unix socket) or port")
        self.websocket = websocket
        self.host, self.port, self.path = host, port, path
        self.where = path if path is not None else f"{host}:{port}"
        self.filter = RfbClientFilter(allow_input, on_input)
        self.connect_timeout = connect_timeout
        self.read_size = read_size
        self._open_connection = open_connection
        self._open_unix_connection = open_unix_connection
        self._writer: asyncio.StreamWriter | None = None
        self.close_reason = ""

    def control_changed(self) -> None:
        """Call on every control change: lets go of held keys and buttons once input is no
        longer allowed. Safe at any time, since the filter only ever emits whole messages."""
        writer = self._writer
        if writer is None or writer.is_closing():
            return
        try:
            if self.filter._allow_input():
                return
        except Exception:
            log.exception("allow_input failed; releasing held input anyway")
        released = self.filter.release_all()
        if released:
            writer.write(released)

    async def run(self) -> None:
        """Connect, pump until either side ends, then close both sides cleanly."""
        try:
            opening = (self._open_unix_connection(self.path) if self.path is not None
                       else self._open_connection(self.host, self.port))
            reader, writer = await asyncio.wait_for(opening, self.connect_timeout)
        except (OSError, TimeoutError) as e:
            log.info("live view: cannot reach VNC on %s: %s", self.where, e)
            await self._close_ws(CLOSE_VNC_UNREACHABLE, "the live view is not reachable")
            return
        self._writer = writer
        code, reason = CLOSE_NORMAL, "the live view ended"
        client_gone = False
        tasks = {
            asyncio.create_task(self._client_to_server(writer), name="vnc-relay-in"),
            asyncio.create_task(self._server_to_client(reader), name="vnc-relay-out"),
        }
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if pending:
                # wait, not gather: a cancel of this task meanwhile stays this task's own (see
                # openajar.api.realtime._race)
                await asyncio.wait(pending)
                for task in pending:
                    if not task.cancelled():
                        task.exception()
            for task in done:
                exc = task.exception()
                if isinstance(exc, RfbProtocolError):
                    code, reason = CLOSE_PROTOCOL, "unsupported message from the viewer"
                    log.warning("live view on %s: %s", self.where, exc)
                elif isinstance(exc, _TextFrame):
                    code, reason = CLOSE_UNSUPPORTED, "binary frames only"
                elif exc is not None and not isinstance(exc, _ClientGone):
                    code, reason = CLOSE_ERROR, "the live view failed"
                    log.error("live view on %s failed: %s", self.where, exc, exc_info=exc)
                elif isinstance(exc, _ClientGone) or task.result() == "client":
                    client_gone = True
        finally:
            for task in tasks:
                task.cancel()
            self._writer = None
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2.0)
            self.close_reason = "the viewer left" if client_gone else reason
        if not client_gone:
            await self._close_ws(code, reason)

    async def _client_to_server(self, writer: asyncio.StreamWriter) -> str:
        while True:
            message = await self.websocket.receive()
            if message["type"] == "websocket.disconnect":
                return "client"
            data = message.get("bytes")
            if data is None:
                if message.get("text") is not None:
                    raise _TextFrame()
                continue
            out = self.filter.feed(data)
            if out:
                writer.write(out)
                await writer.drain()

    async def _server_to_client(self, reader: asyncio.StreamReader) -> str:
        while True:
            data = await reader.read(self.read_size)
            if not data:
                return "server"
            try:
                await self.websocket.send_bytes(data)
            except Exception as e:  # the browser went away mid-send
                raise _ClientGone() from e

    async def _close_ws(self, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await self.websocket.close(code=code, reason=reason)


class _TextFrame(Exception):
    pass


class _ClientGone(Exception):
    pass
