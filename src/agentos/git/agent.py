"""An SSH agent for one deploy key, served only to the node's own ssh for one git operation.

agentosd opens the vault, loads the Ed25519 key into its own memory, and serves the SSH agent
protocol (draft-miller-ssh-agent) on a fresh socket in a 0700 directory of its own, for as long as
one `git fetch` or `git push` to the real remote takes. The node's ssh is pointed at it with
`IdentityAgent`, so the private key is never written to a file and never reaches a lane: a lane
talks to the gateway, never to this socket.

It answers two requests and refuses the rest:

- REQUEST_IDENTITIES: the one public key;
- SIGN_REQUEST: only for that key, only for an SSH login (a publickey user-auth request for the
  `ssh-connection` service as the expected user, `git` by default), and at most MAX_SIGNATURES
  times. Anything else to be signed (a commit or file signature, another user) is refused. A
  host-bound login (OpenSSH 8.9 and later) names the server's host key, and it must be one of the
  declared host's known keys: the signature is good for that host only, whoever asks for it.

On Linux the peer must be agentosd's own uid or root (SO_PEERCRED).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import socket
import struct
from pathlib import Path
from types import TracebackType

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentos.git.keys import key_blob

log = logging.getLogger("agentos.git.agent")

FAILURE = 5
REQUEST_IDENTITIES = 11
IDENTITIES_ANSWER = 12
SIGN_REQUEST = 13
SIGN_RESPONSE = 14
USERAUTH_REQUEST = 50
MAX_MESSAGE = 256 * 1024
MAX_SIGNATURES = 4


def _string(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def _read_string(buf: bytes, pos: int) -> tuple[bytes, int]:
    if pos + 4 > len(buf):
        raise ValueError("short string")
    (n,) = struct.unpack(">I", buf[pos:pos + 4])
    if pos + 4 + n > len(buf):
        raise ValueError("short string")
    return buf[pos + 4:pos + 4 + n], pos + 4 + n


HOSTBOUND = b"publickey-hostbound-v00@openssh.com"


def login_request(data: bytes, blob: bytes) -> tuple[str, bytes | None] | None:
    """(user, server host key blob or None) of an SSH publickey user-auth request (RFC 4252 7) signed
    with `blob`, or None when `data` is anything else. OpenSSH 8.9 and later sign the
    `publickey-hostbound-v00@openssh.com` form, which ends with the server's host key: the key of
    the host this login is for."""
    try:
        _, pos = _read_string(data, 0)               # session identifier
        if pos >= len(data) or data[pos] != USERAUTH_REQUEST:
            return None
        user, pos = _read_string(data, pos + 1)
        service, pos = _read_string(data, pos)
        method, pos = _read_string(data, pos)
        if service != b"ssh-connection" or method not in (b"publickey", HOSTBOUND) \
                or pos >= len(data) or data[pos] != 1:
            return None
        _alg, pos = _read_string(data, pos + 1)
        key, pos = _read_string(data, pos)
        hostkey = None
        if method == HOSTBOUND:
            hostkey, pos = _read_string(data, pos)
        if key != blob or pos != len(data):
            return None
        return user.decode("utf-8", "replace"), hostkey
    except ValueError:
        return None


def login_user(data: bytes, blob: bytes) -> str | None:
    found = login_request(data, blob)
    return found[0] if found else None


class SingleKeyAgent:
    """`async with SingleKeyAgent(key, public_line, run_dir) as sock: ...` serves `sock` until the
    block ends, then removes it and its directory."""

    def __init__(self, key: Ed25519PrivateKey, public_line: str, run_dir: Path, *, user: str = "git",
                 host_keys: list[bytes] | None = None):
        self._key: Ed25519PrivateKey | None = key
        self.blob = key_blob(public_line)
        self.public_line = public_line
        self.user = user
        self.host_keys = set(host_keys or [])  # a host-bound login must be to one of these
        self.dir = Path(run_dir) / f"agent-{secrets.token_hex(6)}"
        self.path = self.dir / "s"
        self.signed = 0
        self.refused = 0
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> Path:
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir.parent, 0o700)
        self.dir.mkdir(mode=0o700)
        self._server = await asyncio.start_unix_server(self._client, path=str(self.path))
        os.chmod(self.path, 0o600)
        return self.path

    async def __aexit__(self, et: type[BaseException] | None, e: BaseException | None,
                        tb: TracebackType | None) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
        shutil.rmtree(self.dir, ignore_errors=True)
        self._key = None  # the object goes with the last reference

    def _peer_ok(self, writer: asyncio.StreamWriter) -> bool:
        sock = writer.get_extra_info("socket")
        if sock is None or not hasattr(socket, "SO_PEERCRED"):
            return sock is not None  # macOS: the 0600 socket in a 0700 directory of ours
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        except OSError:
            return False
        _, uid, _ = struct.unpack("3i", raw)
        return uid in (0, os.geteuid())

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if not self._peer_ok(writer):
                return
            while True:
                head = await asyncio.wait_for(reader.readexactly(4), 30)
                (n,) = struct.unpack(">I", head)
                if n == 0 or n > MAX_MESSAGE:
                    return
                msg = await asyncio.wait_for(reader.readexactly(n), 30)
                reply = self.answer(msg)
                writer.write(struct.pack(">I", len(reply)) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    def answer(self, msg: bytes) -> bytes:
        """The reply to one agent message (type byte and payload)."""
        kind, body = msg[0], msg[1:]
        if kind == REQUEST_IDENTITIES:
            return bytes([IDENTITIES_ANSWER]) + struct.pack(">I", 1) + _string(self.blob) + _string(b"agentos")
        if kind == SIGN_REQUEST and self._key is not None:
            try:
                blob, pos = _read_string(body, 0)
                data, pos = _read_string(body, pos)
            except ValueError:
                return bytes([FAILURE])
            found = login_request(data, self.blob) if blob == self.blob else None
            if found is None or found[0] != self.user or self.signed >= MAX_SIGNATURES:
                self.refused += 1
                log.warning("git agent: refused a signature that is not an SSH login as %s", self.user)
                return bytes([FAILURE])
            if found[1] is not None and self.host_keys and found[1] not in self.host_keys:
                self.refused += 1
                log.warning("git agent: refused a login to a host whose key is not the declared host's")
                return bytes([FAILURE])
            self.signed += 1
            sig = self._key.sign(data)
            return bytes([SIGN_RESPONSE]) + _string(_string(b"ssh-ed25519") + _string(sig))
        return bytes([FAILURE])
