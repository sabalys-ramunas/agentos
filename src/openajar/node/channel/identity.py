"""Ed25519 key files and the node's identity signatures.

The node key lives at `OPENAJAR_HOME/secrets/node.key` (PEM, mode 600). Like ssh and the
policy key, a key file that other users can read is refused rather than used. Creation is
race-free: a 600 temp file is hard-linked into place, so two processes starting at once
end up with the same key. The controller keeps its own keys the same way.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openajar.core import signing
from openajar.node.channel import wire

NODE_KEY = "node.key"


class KeyFileError(Exception):
    pass


def _load(path: Path) -> Ed25519PrivateKey:
    st = path.stat()
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise KeyFileError(f"{path} is accessible by other users (mode {oct(st.st_mode & 0o777)}); "
                           "run chmod 600 on it")
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (ValueError, TypeError):
        raise KeyFileError(f"{path} is not a valid key file") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyFileError(f"{path} is not an Ed25519 key")
    return key


def load_or_create_key(path: Path) -> Ed25519PrivateKey:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists():
        return _load(path)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    tmp = path.parent / f".{path.name}.{secrets.token_hex(6)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _load(path)
    finally:
        tmp.unlink(missing_ok=True)
    return key


class NodeKey:
    """The node's own identity key. Its public half is recorded by the controller at enrollment."""

    def __init__(self, key: Ed25519PrivateKey, path: Path):
        self.key = key
        self.path = path

    @classmethod
    def load_or_create(cls, secrets_dir: Path) -> NodeKey:
        path = secrets_dir / NODE_KEY
        return cls(load_or_create_key(path), path)

    @property
    def public(self) -> str:
        return signing.public_b64(self.key)

    def sign_hello(self, nonce: str, node_id: str) -> str:
        return signing.b64e(self.key.sign(wire.hello_message(nonce, node_id)))

    def sign_enroll(self, nonce: str) -> str:
        return signing.b64e(self.key.sign(wire.enroll_message(nonce, self.public)))
