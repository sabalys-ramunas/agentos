"""An environment's port on the person's own computer (environment_port), as E2B and Daytona give a
preview link.

The node already holds one SSH connection to the machine, with a SOCKS proxy whose connections leave
from the machine (agentos.environments.tunnels). A forward is a listener on 127.0.0.1:<free port> of
the node that opens each connection through that proxy to 127.0.0.1:<port> on the machine, so it
rides the same connection and needs no new SSH session, key or firewall rule. Only the node's own
user may use it (the tunnels' Guard check), and it closes with the environment.

Where the person is:

- a laptop node (agentos local): the node is the person's computer, so `url` is http://127.0.0.1:<p>/;
- a server node: the forward is on the server, and the person reaches it with the same `ssh -N -L`
  line lane_view gives (`tunnel`), then opens `url` on their own computer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from typing import Any

from agentos.core.errors import InvalidRequest
from agentos.environments.errors import EnvironmentNotReady
from agentos.environments.tunnels import Guard, _pipe, free_port, peer_is_us

log = logging.getLogger("agentos.environments")

_KEY = "_port_forwards"
SOCKS_TIMEOUT_S = 10.0
MAX_FORWARDS = 8        # per environment
RESERVED = {22, 3129}   # the machine's ssh and the git gateway's reverse tunnel


async def socks_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int) -> None:
    """SOCKS5, no auth, CONNECT 127.0.0.1:<port> (on the machine, through ssh -D). Raises OSError."""
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    if await reader.readexactly(2) != b"\x05\x00":
        raise OSError("the environment's proxy refused the handshake")
    writer.write(b"\x05\x01\x00\x01" + bytes([127, 0, 0, 1]) + struct.pack(">H", port))
    await writer.drain()
    head = await reader.readexactly(4)
    if head[1] != 0:
        raise OSError(f"nothing answers on port {port} of the machine (SOCKS reply {head[1]})")
    tail = {1: 4, 4: 16}.get(head[3])
    if tail is None:
        tail = (await reader.readexactly(1))[0]
    await reader.readexactly(tail + 2)


class PortForward(Guard):
    """127.0.0.1:<local> on the node, relayed through the environment's SOCKS socket to <port> on its
    machine. `target()` is the tunnel's socket now (the tunnel may have been started again)."""

    def __init__(self, local: int, port: int, target: Any, *, allow: Any = peer_is_us):
        super().__init__(local, "", allow=allow)
        self.remote_port = port
        self.socks = target

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("", 0)
        if not self.allow(int(peer[1]), self.port):
            self.refused += 1
            writer.close()
            return
        path = self.socks()
        if not path:
            writer.close()
            return
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_unix_connection(path), SOCKS_TIMEOUT_S)
        except (OSError, TimeoutError):
            writer.close()
            return
        try:
            await asyncio.wait_for(socks_connect(up_r, up_w, self.remote_port), SOCKS_TIMEOUT_S)
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            for w in (up_w, writer):
                w.close()
            return
        try:
            await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        finally:
            for w in (up_w, writer):
                w.close()


def _forwards(mgr: Any) -> dict[str, dict[int, PortForward]]:
    return mgr.__dict__.setdefault(_KEY, {})


def on_laptop(mgr: Any) -> bool:
    """agentos local: the node is the person's computer (agentos.local.node sets `on_laptop`)."""
    return bool(getattr(mgr, "on_laptop", False))


def _ssh_host(mgr: Any) -> str:
    try:
        from agentos.node.tools.lanes import ssh_host

        return ssh_host(getattr(getattr(mgr, "runtime", None), "config", None))
    except Exception:  # noqa: BLE001 - a hint with a placeholder rather than none
        return "<your node>"


def view(mgr: Any, env_id: str, port: int, fwd: PortForward) -> dict:
    """What environment_port returns and what environment()'s ports show for a forwarded port."""
    url = f"http://127.0.0.1:{fwd.port}/"
    out: dict = {"port": port, "local_port": fwd.port, "url": url}
    if not on_laptop(mgr):
        out["tunnel"] = f"ssh -N -L {fwd.port}:127.0.0.1:{fwd.port} {_ssh_host(mgr)}"
    return out


def link_for(mgr: Any, env_id: str, port: int) -> dict | None:
    fwd = _forwards(mgr).get(env_id, {}).get(port)
    if fwd is None or not fwd.serving:
        return None
    return view(mgr, env_id, port, fwd)


def with_links(mgr: Any, env_id: str, ports: list[dict]) -> list[dict]:
    """environment()'s ports, each forwarded one with its `link` (and `tunnel` on a server node)."""
    out = []
    for p in ports:
        got = link_for(mgr, env_id, int(p.get("port") or 0))
        if got is not None:
            p = {**p, "link": got["url"], **({"tunnel": got["tunnel"]} if "tunnel" in got else {})}
        out.append(p)
    return out


async def forward(mgr: Any, env: Any, port: int) -> dict:
    """Forward the machine's `port` to a free port here (the same one again when it is forwarded)."""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535 or port in RESERVED:
        raise InvalidRequest("port is the machine's port a server listens on, 1 to 65535 (not 22 or 3129), "
                             "like 3000 or 5173")
    if env.state != "ready":
        raise EnvironmentNotReady(f"environment {env.id} is {env.state}; call environment() until it is ready")
    tunnel = mgr.tunnel_of(env.id)
    if tunnel is None or not tunnel.alive:
        raise EnvironmentNotReady(f"environment {env.id}'s connection to its machine is down; the node starts it "
                                  "again within a minute, then call environment_port again")
    mine = _forwards(mgr).setdefault(env.id, {})
    fwd = mine.get(port)
    if fwd is not None and fwd.serving:
        return view(mgr, env.id, port, fwd)
    if len(mine) >= MAX_FORWARDS:
        raise InvalidRequest(f"environment {env.id} forwards {MAX_FORWARDS} ports already "
                             f"({', '.join(str(p) for p in sorted(mine))}); use one of those")
    fwd = PortForward(free_port(), port, lambda: getattr(mgr.tunnel_of(env.id), "socks_path", None))
    await fwd.start()
    mine[port] = fwd
    log.info("environment %s: port %d on its machine is 127.0.0.1:%d here", env.id, port, fwd.port)
    return view(mgr, env.id, port, fwd)


async def close_all(mgr: Any, env_id: str) -> None:
    """The environment's forwards end with it."""
    for fwd in (_forwards(mgr).pop(env_id, None) or {}).values():
        with contextlib.suppress(Exception):
            await fwd.stop()


async def ended(mgr: Any, env_id: str) -> None:
    """A closed environment: its forwards stop, and the sign-ins the node held for its browser go (a
    save holds its own copy)."""
    from agentos.environments import signins

    await close_all(mgr, env_id)
    signins.drop(mgr, env_id)
