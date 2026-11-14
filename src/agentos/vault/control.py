"""Local control socket, so `agentos vault unseal|seal|status` reach the running daemon.

`AGENTOS_HOME/run/vault.sock` (or /tmp/agentos-<hash>/ when the home is too long for a socket path): chmod 600 inside a 700 directory; on Linux the peer's
uid must be ours or root (SO_PEERCRED). One JSON line in, one JSON line out:

    {"op": "status"} | {"op": "seal"} | {"op": "unseal", "passphrase": "..."}
    -> {"ok": true, "status": {...}} | {"ok": false, "error": {"code", "message"}}
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
from pathlib import Path
from typing import Any

from agentos.core.errors import AgentosError
from agentos.ops.layout import prepare_socket, trusted_socket
from agentos.vault.crypto import ensure_private_dir
from agentos.vault.errors import VaultBusy

MAX_LINE = 64 * 1024


def _peer_ok(writer: asyncio.StreamWriter) -> bool:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return False
    if hasattr(socket, "SO_PEERCRED"):
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        except OSError:
            return False
        _, uid, _ = struct.unpack("3i", raw)
        return uid in (0, os.geteuid())
    return True  # macOS and BSD: rely on the 600 socket inside a 700 directory


async def _handle(service: Any, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    resp: dict[str, Any]
    try:
        if not _peer_ok(writer):
            resp = {"ok": False, "error": {"code": "denied", "message": "not allowed"}}
        else:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            req = json.loads(line or b"{}")
            op = req.get("op") if isinstance(req, dict) else None
            if op == "status":
                resp = {"ok": True, "status": service.status()}
            elif op == "seal":
                resp = {"ok": True, "status": service.seal()}
            elif op == "unseal":
                passphrase = req.get("passphrase")
                if passphrase is not None and not isinstance(passphrase, str):
                    raise ValueError("bad passphrase")
                resp = {"ok": True, "status": await service.unseal(passphrase)}
            else:
                resp = {"ok": False, "error": {"code": "invalid", "message": "unknown op"}}
    except AgentosError as e:
        resp = {"ok": False, "error": {"code": e.code, "message": e.message}}
    except Exception:  # noqa: BLE001 - never echo the request (it may hold a passphrase)
        resp = {"ok": False, "error": {"code": "invalid", "message": "bad request"}}
    try:
        writer.write(json.dumps(resp).encode() + b"\n")
        await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


def _live(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(str(path))
        return True
    except OSError:
        return False


async def start_server(service: Any, path: Path) -> asyncio.AbstractServer:
    path = Path(path)
    prepare_socket(path)  # a path too long, or a short dir that is not ours: a clear error
    ensure_private_dir(path.parent)
    if path.exists() or path.is_symlink():
        if _live(path):
            raise VaultBusy(f"another agentos process already serves {path}")
        path.unlink()
    server = await asyncio.start_unix_server(lambda r, w: _handle(service, r, w), path=str(path), limit=MAX_LINE)
    os.chmod(path, 0o600)
    return server


def request(path: Path, payload: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
    """Blocking client for the CLI. Raises OSError when no daemon is listening."""
    trusted_socket(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall(json.dumps(payload).encode() + b"\n")
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return json.loads(b"".join(chunks) or b"{}")
