"""The fake display: a VNC server per lane that shows the lane's tab (RFB 3.8, raw rectangles, no auth).

The live view is the real one: openajar.node.viewer_app relays a WebSocket to this server's unix
socket (in a fresh mode 0700 directory, like a node's x11vnc) and the panel's noVNC draws it, so a dev lane shows its page as a picture instead of an error. The server
speaks the part of RFB those clients use: the None security type, 32-bit true colour in either byte
order, raw rectangles. Input messages are read and dropped; the tab is driven through the agent's
tools, not from the viewer.

A frame goes out when the tab changes: an incremental request waits for a change, a full request is
answered at once. `frame()` returns (version, Raster); the same version is never sent twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import struct
import tempfile
from collections.abc import Callable
from pathlib import Path

from openajar.dev.image import Raster

log = logging.getLogger("openajar.dev.display")

RFB_VERSION = b"RFB 003.008\n"
SECURITY_NONE = 1
# bits per pixel, depth, big-endian flag, true colour, max red/green/blue, shift red/green/blue, padding
SERVER_FORMAT = (32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
PIXEL_FORMAT = ">BBBBHHHBBB3x"
POLL_SECONDS = 0.2


class UnsupportedClient(Exception):
    """A client this server does not speak to: its connection is closed."""


def _byte_order(fmt: tuple) -> dict[str, int]:
    """For a 32-bit true colour format: the byte of each pixel (in memory order) that holds red,
    green and blue. Anything else is refused rather than drawn wrong."""
    bpp, _depth, big_endian, true_colour, rmax, gmax, bmax, rs, gs, bs = fmt
    if bpp != 32 or not true_colour or (rmax, gmax, bmax) != (255, 255, 255) or any(s % 8 for s in (rs, gs, bs)):
        raise UnsupportedClient(f"pixel format {fmt} is not 32-bit true colour")

    def index(shift: int) -> int:
        return 3 - shift // 8 if big_endian else shift // 8

    return {"r": index(rs), "g": index(gs), "b": index(bs)}


def _pixels(raster: Raster, order: dict[str, int]) -> bytes:
    out = bytearray(4 * raster.width * raster.height)
    for name, offset in (("r", 0), ("g", 1), ("b", 2)):
        out[order[name]::4] = raster.rgb[offset::3]
    return bytes(out)


class VncServer:
    """One lane's display. `frame` is called for every update (it may be the same frame again)."""

    def __init__(self, frame: Callable[[], tuple[int, Raster]], *, name: str = "openajar dev"):
        self.frame = frame
        self.name = name
        self.path: str | None = None
        self._dir: Path | None = None
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> str:
        # mkdtemp is mode 0700; a short temp path keeps the socket under macOS's 104 byte limit
        self._dir = Path(tempfile.mkdtemp(prefix="openajar-vnc-"))
        self.path = str(self._dir / "vnc.sock")
        self._server = await asyncio.start_unix_server(self._connection, self.path)
        return self.path

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        for writer in list(self._writers):
            writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), 2)
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            await self._handshake(reader, writer)
            await self._messages(reader, writer)
        except (asyncio.IncompleteReadError, OSError, UnsupportedClient) as exc:
            log.info("%s: viewer connection ended: %s", self.name, type(exc).__name__)
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(RFB_VERSION)
        await reader.readexactly(len(RFB_VERSION))  # the client's version; 3.8 is answered whatever it is
        writer.write(bytes([1, SECURITY_NONE]))
        if (await reader.readexactly(1))[0] != SECURITY_NONE:
            raise UnsupportedClient("security type")
        writer.write(struct.pack(">I", 0))  # SecurityResult: OK
        await reader.readexactly(1)  # ClientInit: the shared flag
        _, raster = self.frame()
        name = self.name.encode()
        writer.write(struct.pack(">HH", raster.width, raster.height) + struct.pack(PIXEL_FORMAT, *SERVER_FORMAT)
                     + struct.pack(">I", len(name)) + name)
        await writer.drain()

    async def _messages(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        order = _byte_order(SERVER_FORMAT)
        sent = -1  # the version of the last frame this viewer was sent
        while True:
            kind = (await reader.readexactly(1))[0]
            if kind == 0:  # SetPixelFormat
                order = _byte_order(struct.unpack(PIXEL_FORMAT, (await reader.readexactly(19))[3:]))
            elif kind == 2:  # SetEncodings: raw is the only encoding sent, whatever is asked
                _, count = struct.unpack(">BH", await reader.readexactly(3))
                await reader.readexactly(4 * count)
            elif kind == 3:  # FramebufferUpdateRequest
                incremental = (await reader.readexactly(9))[0]
                version, raster = self.frame()
                while incremental and version == sent:
                    await asyncio.sleep(POLL_SECONDS)
                    version, raster = self.frame()
                sent = version
                header = struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, raster.width, raster.height, 0)
                writer.write(header + _pixels(raster, order))
                await writer.drain()
            elif kind == 4:  # KeyEvent: dropped
                await reader.readexactly(7)
            elif kind == 5:  # PointerEvent: dropped
                await reader.readexactly(5)
            elif kind == 6:  # ClientCutText: dropped
                (length,) = struct.unpack(">3xI", await reader.readexactly(7))
                await reader.readexactly(length)
            else:
                raise UnsupportedClient(f"message type {kind}")
