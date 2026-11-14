"""The two tunnels of an environment, carried by one ssh connection from the node to its VM:

- `-D <socket>` on the node: a SOCKS proxy whose connections leave from the VM, on a Unix socket in
  a folder only the node's user can open. The environment's browser lane and studio's review
  browser use it, with DNS through the tunnel and localhost not bypassed, so http://localhost:3000
  in that browser is port 3000 on the VM.
- `-R 127.0.0.1:3129:127.0.0.1:<gateway port>` on the VM: the git gateway's listener for this VM
  alone (agentos.git.service.vm_listener), so git on the VM clones and pushes the environment's
  repository through the node (the credential never reaches the VM; its token works only here).

Browsers speak SOCKS over TCP only, so the node relays 127.0.0.1:<port> to that socket (`Guard`).
Who may use the port:

- Not a machine. Its own firewall refuses this computer's addresses (the docker provider), and a
  cloud VM reaches the node only through this ssh connection.
- Not another user of this computer, on Linux: the guard looks up who opened each connection (the
  socket's owner in /proc/net/tcp) and closes any that is not the node's own user. macOS has no
  such lookup for TCP, so there another account on the same Mac could use the port.

The connection uses the VM's own key and pinned host key (agentos.vms.ssh), and forwards nothing
else: no agent, no X11, no other port. If it drops, the environment's reaper starts it again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from agentos.env.models import RemoteHost
from agentos.vms.ssh import ssh_base

log = logging.getLogger("agentos.environments")

GATEWAY_PORT_ON_VM = 3129  # as inside an isolated lane (agentos.git.service.LANE_PORT)
START_WAIT_S = 10.0
RELAY_CHUNK = 65536
PROC_TCP = ("/proc/net/tcp", "/proc/net/tcp6")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def tunnel_argv(remote: RemoteHost, *, socks_path: str, gateway_port: int | None) -> list[str]:
    """ssh_base with forwarding allowed for exactly these two, and no remote command."""
    base = ssh_base(remote)
    i = base.index("ClearAllForwardings=yes")
    base = base[: i - 1] + base[i + 1:]          # drop "-o ClearAllForwardings=yes"
    target = base[-2:]                           # "-T", "user@host"
    out = base[:-2] + ["-o", "ExitOnForwardFailure=yes", "-o", "StreamLocalBindUnlink=yes",
                       "-o", "StreamLocalBindMask=0177", "-N", "-D", socks_path]
    if gateway_port:
        out += ["-R", f"127.0.0.1:{GATEWAY_PORT_ON_VM}:127.0.0.1:{gateway_port}"]
    return out + target


# ---------------------------------------------------------------- who opened a connection

def _hex_port(addr: str) -> int:
    return int(addr.rsplit(":", 1)[1], 16)


def tcp_owner(table: str, *, local_port: int, remote_port: int) -> int | None:
    """The uid that owns the loopback TCP socket local_port -> remote_port, from the text of
    /proc/net/tcp (or tcp6), else None. The client's side of a connection to our listener has
    local = the peer's port and remote = our port."""
    for line in table.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 8:
            continue
        try:
            if _hex_port(parts[1]) == local_port and _hex_port(parts[2]) == remote_port:
                return int(parts[7])
        except ValueError:
            continue
    return None


def peer_is_us(peer_port: int, our_port: int) -> bool:
    """Linux: is the other end of this loopback connection a socket of this process's user? Other
    systems: no way to tell for TCP, so yes (see the module's docstring)."""
    if not sys.platform.startswith("linux"):
        return True
    for path in PROC_TCP:
        try:
            text = Path(path).read_text()
        except OSError:
            continue
        uid = tcp_owner(text, local_port=peer_port, remote_port=our_port)
        if uid is not None:
            return uid == os.getuid()
    return False


class Guard:
    """127.0.0.1:<port> relayed to a Unix socket, for this computer's user only (`allow`)."""

    def __init__(self, port: int, target: str, *, allow: Callable[[int, int], bool] = peer_is_us):
        self.port = port
        self.target = target
        self.allow = allow
        self.server: asyncio.Server | None = None
        self.refused = 0

    @property
    def serving(self) -> bool:
        return self.server is not None and self.server.is_serving()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", self.port)

    async def stop(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=2)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("", 0)
        if not self.allow(int(peer[1]), self.port):
            self.refused += 1
            log.warning("environment tunnel on port %s: refused a connection from another user", self.port)
            writer.close()
            return
        try:
            up_r, up_w = await asyncio.open_unix_connection(self.target)
        except OSError:
            writer.close()
            return
        try:
            await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        finally:
            for w in (up_w, writer):
                w.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(RELAY_CHUNK):
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            if writer.can_write_eof():
                writer.write_eof()


async def _answers(path: str) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_unix_connection(path), timeout=1.0)
    except (OSError, TimeoutError):
        return False
    w.close()
    with contextlib.suppress(Exception):
        await w.wait_closed()
    return True


class Tunnel:
    """One ssh process holding both forwards, and the guard in front of its SOCKS socket. `start`
    returns once the SOCKS port answers."""

    def __init__(self, remote: RemoteHost, *, gateway_port: int | None, socks_port: int | None = None):
        self.remote = remote
        self.gateway_port = gateway_port
        self.socks_port = socks_port or free_port()
        self.proc: asyncio.subprocess.Process | None = None
        self.guard: Guard | None = None
        self.dir: str | None = None
        self.last_error = ""

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and self.guard is not None and self.guard.serving

    async def start(self) -> None:
        if self.alive:
            return
        await self.stop()
        # a short path (a Unix socket's is at most 104 bytes on macOS), in a folder only we can open
        self.dir = tempfile.mkdtemp(prefix="agentos-env-")
        sock = os.path.join(self.dir, "socks")
        argv = tunnel_argv(self.remote, socks_path=sock, gateway_port=self.gateway_port)
        self.proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + START_WAIT_S
        while loop.time() < deadline:
            if self.proc.returncode is not None:
                err = (await self.proc.stderr.read()).decode(errors="replace").strip() if self.proc.stderr else ""
                self.last_error = err[-300:] or f"ssh exited {self.proc.returncode}"
                await self.stop()
                raise OSError(f"the tunnel to the environment's machine did not start: {self.last_error}")
            if await _answers(sock):
                self.guard = Guard(self.socks_port, sock)
                try:
                    await self.guard.start()
                except OSError as e:
                    await self.stop()
                    raise OSError(f"the tunnel's port {self.socks_port} on this computer is taken: {e}") from None
                return
            await asyncio.sleep(0.2)
        await self.stop()
        raise OSError("the tunnel to the environment's machine did not answer within "
                      f"{START_WAIT_S:g} s")

    async def stop(self) -> None:
        guard, self.guard = self.guard, None
        if guard is not None:
            await guard.stop()
        proc, self.proc = self.proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        folder, self.dir = self.dir, None
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
