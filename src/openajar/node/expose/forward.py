"""`openajar forward`: one port of a terminal or service lane, on the owner's computer.

A service lane never shares the node's network: what it listens on is reachable only inside its own
network namespace (openajar.env.services). The node opens a unix socket that only agentosd's user can
connect to (mode 600, in a directory of mode 700), and for each connection opens a socket inside the
lane's namespace, as the service health probe does (`Host.socket_in_netns`), to 127.0.0.1:<port>
there, and copies bytes both ways. The laptop's `ssh -L 127.0.0.1:<local>:<socket>` reaches it. No
port opens on the node, and nothing but that lane's own loopback is dialled: a lane that shares the
node's network (no namespace of its own) is refused, since its 127.0.0.1 is the node's.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from openajar.core.errors import InvalidRequest, ServiceUnavailable

log = logging.getLogger("openajar.node.expose")

CONNECT_TIMEOUT_S = 10.0
CHUNK = 65536

# (port) -> an open, non-blocking socket connected to 127.0.0.1:<port> inside the lane
Dialer = Callable[[int], Awaitable[socket.socket]]


async def lane_dialer(envs: Any, lane_id: str) -> Dialer:
    """A dialer into a warm env lane's own network namespace, or InvalidRequest/ServiceUnavailable."""
    if envs is None:
        raise ServiceUnavailable("this node has no terminal or service lanes")
    if not envs.has(lane_id):
        raise InvalidRequest(f"{lane_id} is not a terminal or service lane on this node (see `openajar env list`)")
    state = str(getattr(envs.state(lane_id), "value", envs.state(lane_id)))
    if state not in ("warm", "active"):
        raise InvalidRequest(f"{lane_id} is {state}: start its service (or open a shell there) first")
    rt = await envs.warm(lane_id)  # warm already: this returns its runtime
    netns = getattr(rt, "netns", None)
    if netns is None:
        raise InvalidRequest(f"{lane_id} shares the node's network, so its ports are the node's own; forward "
                             "only lanes with a network of their own")
    host = rt.host

    async def dial(port: int) -> socket.socket:
        sock = await asyncio.to_thread(host.socket_in_netns, netns)
        sock.setblocking(False)
        try:
            await asyncio.wait_for(asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", port)),
                                   CONNECT_TIMEOUT_S)
        except BaseException:
            sock.close()
            raise
        return sock

    return dial


class Forward:
    """The unix socket of one forward, serving connections into the lane until closed."""

    def __init__(self, path: Path, port: int, dial: Dialer, *, on_event: Callable[[str, dict], None] | None = None):
        self.path = path
        self.port = port
        self.dial = dial
        self.on_event = on_event
        self.server: asyncio.AbstractServer | None = None
        self.connections = 0
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        old = os.umask(0o177)  # the socket is born mode 600: only agentosd's user (and root) connect
        try:
            self.server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
            self.server = None
        for task in list(self._tasks):
            task.cancel()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            try:
                sock = await self.dial(self.port)
            except Exception as e:  # noqa: BLE001 - nothing listens there (yet): the client sees a closed connection
                log.info("forward to port %d: %s", self.port, type(e).__name__)
                return
            self.connections += 1
            lane_reader, lane_writer = await asyncio.open_connection(sock=sock)
            try:
                await asyncio.gather(_pipe(reader, lane_writer), _pipe(lane_reader, writer))
            finally:
                lane_writer.close()
        finally:
            writer.close()
            if task is not None:
                self._tasks.discard(task)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()
