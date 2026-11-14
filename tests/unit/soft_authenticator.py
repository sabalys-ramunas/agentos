"""A software passkey authenticator for tests: it answers navigator.credentials.create() and
.get() options the way a browser and a platform authenticator would, as PublicKeyCredential
JSON (binary fields base64url). ES256 (P-256) by default, EdDSA on request."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def cbor(value) -> bytes:
    """Encode ints, bytes, str, lists, dicts, bools and None (canonical enough for WebAuthn)."""
    def head(major: int, n: int) -> bytes:
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
        raise ValueError("too large")

    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if value is None:
        return b"\xf6"
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode()
        return head(3, len(raw)) + raw
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor(v) for v in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(type(value))


class SoftAuthenticator:
    def __init__(self, origin: str = "http://localhost:7400", *, alg: str = "es256", uv: bool = True):
        self.origin = origin
        self.alg = alg
        self.uv = uv
        self.keys: dict[bytes, tuple[object, str, int]] = {}  # credential id -> (private key, rp id, count)
        self.counter_step = 1

    def _cose(self, key) -> dict:
        if self.alg == "eddsa":
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
            return {1: 1, 3: -8, -1: 6, -2: key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
        nums = key.public_key().public_numbers()
        return {1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")}

    def _client(self, kind: str, challenge: str, origin: str | None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin,
                           "crossOrigin": False}).encode()

    def _flags(self, *, attested: bool) -> int:
        return 0x01 | (0x04 if self.uv else 0) | (0x40 if attested else 0)

    def create(self, options: dict, *, origin: str | None = None, rp_id: str | None = None) -> dict:
        """navigator.credentials.create({publicKey: options}) -> PublicKeyCredential JSON."""
        rp = rp_id or options["rp"]["id"]
        key = ed25519.Ed25519PrivateKey.generate() if self.alg == "eddsa" else ec.generate_private_key(ec.SECP256R1())
        cred_id = secrets.token_bytes(16)
        self.keys[cred_id] = (key, rp, 0)
        auth = (hashlib.sha256(rp.encode()).digest() + bytes([self._flags(attested=True)]) + struct.pack(">I", 0)
                + bytes(16) + struct.pack(">H", len(cred_id)) + cred_id + cbor(self._cose(key)))
        client = self._client("webauthn.create", options["challenge"], origin)
        att = cbor({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"id": b64(cred_id), "rawId": b64(cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(client), "attestationObject": b64(att),
                             "transports": ["internal"]},
                "clientExtensionResults": {}, "authenticatorAttachment": "platform"}

    def get(self, options: dict, *, origin: str | None = None, cred_id: bytes | None = None) -> dict:
        """navigator.credentials.get({publicKey: options}) -> PublicKeyCredential JSON."""
        allowed = [unb64(c["id"]) for c in options.get("allowCredentials") or []]
        if cred_id is None:
            cred_id = next(c for c in self.keys if not allowed or c in allowed)
        key, rp, count = self.keys[cred_id]
        count += self.counter_step
        self.keys[cred_id] = (key, rp, count)
        auth = hashlib.sha256(options.get("rpId", rp).encode()).digest() + bytes([self._flags(attested=False)]) \
            + struct.pack(">I", count)
        client = self._client("webauthn.get", options["challenge"], origin)
        signed = auth + hashlib.sha256(client).digest()
        sig = key.sign(signed) if self.alg == "eddsa" else key.sign(signed, ec.ECDSA(hashes.SHA256()))
        return {"id": b64(cred_id), "rawId": b64(cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(client), "authenticatorData": b64(auth), "signature": b64(sig),
                             "userHandle": None},
                "clientExtensionResults": {}, "authenticatorAttachment": "platform"}

    @property
    def last_id(self) -> bytes:
        return list(self.keys)[-1]
