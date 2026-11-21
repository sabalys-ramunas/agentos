"""The dev node's fake display: a VNC server that speaks RFB 3.8 and shows a picture of the tab."""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import struct

import pytest

from openajar.dev.display import PIXEL_FORMAT, SERVER_FORMAT, VncServer
from openajar.dev.image import Raster


def picture(color: tuple[int, int, int]) -> Raster:
    raster = Raster(8, 4, (0, 0, 0))
    raster.rect(1, 1, 1, 1, color)  # one known pixel, at (1, 1): byte 36 of a full frame
    return raster


async def connect(path: str):
    reader, writer = await asyncio.open_unix_connection(path)
    assert await reader.readexactly(12) == b"RFB 003.008\n"
    writer.write(b"RFB 003.008\n")
    types = await reader.readexactly((await reader.readexactly(1))[0])
    assert 1 in types
    writer.write(bytes([1]))  # the None security type
    assert struct.unpack(">I", await reader.readexactly(4))[0] == 0  # SecurityResult: OK
    writer.write(bytes([1]))  # ClientInit: shared
    width, height = struct.unpack(">HH", await reader.readexactly(4))
    fmt = struct.unpack(PIXEL_FORMAT, await reader.readexactly(16))
    name = await reader.readexactly(struct.unpack(">I", await reader.readexactly(4))[0])
    return reader, writer, (width, height, fmt, name)


async def request_frame(reader, writer, width: int, height: int, *, incremental: bool = False) -> bytes:
    writer.write(struct.pack(">BBHHHH", 3, int(incremental), 0, 0, width, height))
    await writer.drain()
    kind, count = struct.unpack(">BxH", await reader.readexactly(4))
    assert (kind, count) == (0, 1)
    x, y, w, h, encoding = struct.unpack(">HHHHi", await reader.readexactly(12))
    assert (x, y, w, h, encoding) == (0, 0, width, height, 0)  # one raw rectangle, the whole frame
    return await reader.readexactly(w * h * 4)


async def test_handshake_announces_the_tab_size_and_the_pixel_format():
    server = VncServer(lambda: (1, picture((10, 20, 30))), name="lane one")
    path = await server.start()
    _reader, writer, (width, height, fmt, name) = await connect(path)
    assert (width, height) == (8, 4)
    assert fmt == SERVER_FORMAT
    assert name == b"lane one"
    writer.close()
    await server.close()


async def test_full_frame_is_bgrx_in_the_clients_format():
    server = VncServer(lambda: (1, picture((10, 20, 30))))
    path = await server.start()
    reader, writer, (width, height, _, _) = await connect(path)
    data = await request_frame(reader, writer, width, height)
    assert data[36:40] == bytes([30, 20, 10, 0])  # little-endian: blue, green, red, padding
    writer.close()
    await server.close()


async def test_a_big_endian_client_gets_its_bytes_in_its_own_order():
    server = VncServer(lambda: (1, picture((10, 20, 30))))
    path = await server.start()
    reader, writer, (width, height, _, _) = await connect(path)
    # SetPixelFormat: 32 bpp, depth 24, big-endian, true colour, shifts 16, 8, 0
    writer.write(bytes([0, 0, 0, 0]) + struct.pack(PIXEL_FORMAT, 32, 24, 1, 1, 255, 255, 255, 16, 8, 0))
    await writer.drain()
    data = await request_frame(reader, writer, width, height)
    assert data[36:40] == bytes([0, 10, 20, 30])  # big-endian: padding, red, green, blue
    writer.close()
    await server.close()


async def test_an_incremental_request_waits_until_the_tab_changes():
    state = {"version": 1, "color": (10, 20, 30)}
    server = VncServer(lambda: (state["version"], picture(state["color"])))
    path = await server.start()
    reader, writer, (width, height, _, _) = await connect(path)
    await request_frame(reader, writer, width, height)  # the first, full frame

    writer.write(struct.pack(">BBHHHH", 3, 1, 0, 0, width, height))
    await writer.drain()
    await asyncio.sleep(0.3)
    state.update(version=2, color=(200, 100, 50))  # the tab changes; the waiting request is answered
    kind, count = struct.unpack(">BxH", await asyncio.wait_for(reader.readexactly(4), 5))
    assert (kind, count) == (0, 1)
    assert struct.unpack(">HHHHi", await reader.readexactly(12))[2:4] == (8, 4)
    data = await reader.readexactly(width * height * 4)
    assert data[36:40] == bytes([50, 100, 200, 0])
    writer.close()
    await server.close()


async def test_a_pixel_format_it_does_not_speak_closes_the_connection():
    server = VncServer(lambda: (1, picture((10, 20, 30))))
    path = await server.start()
    reader, writer, _ = await connect(path)
    writer.write(bytes([0, 0, 0, 0]) + struct.pack(PIXEL_FORMAT, 16, 16, 0, 1, 31, 63, 31, 11, 5, 0))
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), 5) == b""  # closed by the server
    writer.close()
    await server.close()


async def test_close_stops_listening_and_ends_viewers():
    server = VncServer(lambda: (1, picture((10, 20, 30))))
    path = await server.start()
    reader, writer, _ = await connect(path)
    await server.close()
    assert await asyncio.wait_for(reader.read(), 5) == b""
    with pytest.raises(OSError):
        await asyncio.open_unix_connection(path)
    assert not os.path.exists(os.path.dirname(path))
    writer.close()


async def test_the_display_listens_on_a_private_unix_socket_not_tcp():
    """Like a node's x11vnc: another local user cannot open it, and there is no port to find."""
    server = VncServer(lambda: (1, picture((10, 20, 30))))
    path = await server.start()
    try:
        assert stat.S_ISSOCK(os.stat(path).st_mode)
        assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
        assert all(sock.family == socket.AF_UNIX for sock in server._server.sockets)
    finally:
        await server.close()
