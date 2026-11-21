"""Ed25519 signatures as the wire format signs them (docs/PROTOCOL.md v1), below every layer that checks one.

Keys and signatures are unpadded base64url of the raw Ed25519 bytes. Controller-signed bodies sign
`"agentos-controller-msg:v1:" + type + ":" + node_id + ":" + canonical(body without sig)`, canonical
being sorted-key compact JSON in UTF-8. The node's channel (openajar.node.channel.wire re-exports these)
and the audit anchors (openajar.policy.anchors) both check the controller's key here.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

CONTROLLER_PREFIX = "agentos-controller-msg:v1:"

Json = dict[str, Any]

_B64U_RE = re.compile(r"^[A-Za-z0-9_-]*$")


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64d(text: str) -> bytes:
    if not isinstance(text, str) or not _B64U_RE.match(text):
        raise ValueError("not unpadded base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("not unpadded base64url") from None


def canonical(value: Any) -> bytes:
    """The bytes that are signed: sorted keys, no spaces, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def public_key(text: str) -> Ed25519PublicKey:
    raw = b64d(text)
    if len(raw) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def public_b64(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return b64e(pub.public_bytes_raw())


def verify(pub_b64: str, sig_b64: Any, message: bytes) -> bool:
    """True only for a valid signature; any malformed input is simply False."""
    if not isinstance(sig_b64, str):
        return False
    try:
        public_key(pub_b64).verify(b64d(sig_b64), message)
    except (InvalidSignature, ValueError):
        return False
    return True


def controller_message(kind: str, node_id: str, body: Json) -> bytes:
    unsigned = {key: value for key, value in body.items() if key != "sig"}
    return f"{CONTROLLER_PREFIX}{kind}:{node_id}:".encode() + canonical(unsigned)


def sign_controller(key: Ed25519PrivateKey, kind: str, node_id: str, body: Json) -> Json:
    """`body` plus its `sig`, for a controller-signed message to one node."""
    unsigned = {k: v for k, v in body.items() if k != "sig"}
    return {**unsigned, "sig": b64e(key.sign(controller_message(kind, node_id, unsigned)))}


def verify_controller(controller_pubkey: str, kind: str, node_id: str, body: Json) -> bool:
    """True only when `body.sig` is a valid signature by the pinned controller key."""
    if not controller_pubkey or not node_id:
        return False
    return verify(controller_pubkey, body.get("sig"), controller_message(kind, node_id, body))
