"""An environment's SOCKS tunnel on the node: ssh listens on a Unix socket only the node's user can
open, and the TCP port browsers need is a relay that serves the node's own user only."""

from __future__ import annotations

import asyncio
import os
import stat
import sys

import pytest

from agentos.env.models import RemoteHost
from agentos.environments import tunnels
from agentos.environments.tunnels import Guard, free_port, tcp_owner, tunnel_argv

REMOTE = RemoteHost(vm_id="vm_1", host="127.0.0.1", port=2222, key_path="/k/id", known_hosts="/k/kh",
                    host_alias="agentos-vm_1", provider="docker", label="docker")

# /proc/net/tcp: our listener on 127.0.0.1:0x9C40 (40000), a client of uid 1000 from port 0xC350
# (50000), and one of uid 1001 from port 0xC351 (50001)
TABLE = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:9C40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 1
   1: 0100007F:C350 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1000        0 2 1
   2: 0100007F:C351 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1001        0 3 1
"""


def test_ssh_serves_socks_on_a_unix_socket_not_a_tcp_port():
    argv = tunnel_argv(REMOTE, socks_path="/tmp/agentos-env-x/socks", gateway_port=4000)
    d = argv.index("-D")
    assert argv[d + 1] == "/tmp/agentos-env-x/socks"
    assert "StreamLocalBindMask=0177" in argv and "ClearAllForwardings=yes" not in argv
    assert "127.0.0.1:3129:127.0.0.1:4000" in argv and argv[-1].endswith("127.0.0.1")


def test_the_owner_of_a_loopback_connection_is_read_from_proc_net_tcp():
    assert tcp_owner(TABLE, local_port=50000, remote_port=40000) == 1000
    assert tcp_owner(TABLE, local_port=50001, remote_port=40000) == 1001
    assert tcp_owner(TABLE, local_port=50002, remote_port=40000) is None


async def _echo_socket(path: str) -> asyncio.Server:
    async def echo(reader, writer):
        data = await reader.read(100)
        writer.write(b"from the vm: " + data)
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(echo, path)


async def test_the_guard_relays_for_the_nodes_user_and_refuses_anyone_else(tmp_path):
    import tempfile

    folder = tempfile.mkdtemp(prefix="agentos-t-")
    path = os.path.join(folder, "s")
    upstream = await _echo_socket(path)
    seen: list[int] = []

    def only_first(peer_port: int, our_port: int) -> bool:
        seen.append(peer_port)
        return len(seen) == 1

    port = free_port()
    guard = Guard(port, path, allow=only_first)
    await guard.start()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"hello")
        await w.drain()
        assert await asyncio.wait_for(r.read(100), 5) == b"from the vm: hello"
        w.close()
        r, w = await asyncio.open_connection("127.0.0.1", port)  # "another user"
        try:
            w.write(b"hello")
            got = await asyncio.wait_for(r.read(100), 5)
        except ConnectionResetError:
            got = b""
        assert got == b"" and guard.refused == 1
        w.close()
    finally:
        await guard.stop()
        upstream.close()
        os.unlink(path)
        os.rmdir(folder)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the owner of a TCP socket is read on Linux only")
async def test_on_linux_the_guard_knows_our_own_connections():
    port = free_port()
    seen: list[bool] = []

    async def note(reader, writer):
        peer = writer.get_extra_info("peername")
        seen.append(tunnels.peer_is_us(peer[1], port))
        writer.close()

    server = await asyncio.start_server(note, "127.0.0.1", port)
    _r, w = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.sleep(0.2)
    w.close()
    server.close()
    assert seen == [True]


def test_the_socket_folder_is_the_node_users_alone(tmp_path):
    import tempfile

    folder = tempfile.mkdtemp(prefix="agentos-env-")
    try:
        assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
        assert len(os.path.join(folder, "socks")) < 100  # a Unix socket path fits (104 bytes on macOS)
    finally:
        os.rmdir(folder)
