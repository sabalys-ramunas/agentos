"""Signed connector bundles (docs/design/connectors.md 4.6).

A bundle is one JSON document:

    {"format": "agentos.connectors.bundle/1",
     "payload": {"created_at": "...", "connectors": [<connector documents>],
                 "index": [{"connector", "site", "version", "operations": {name: {effect, contract,
                                                                         implementation}}}]},
     "signature": {"alg": "ed25519", "key_id": "ed25519:<16 hex>", "sig": "<base64url>"}}

The signature covers the canonical JSON of `payload` (sorted keys, no spaces, UTF-8). A node
verifies it against a trusted key before it installs anything, then recompiles every
connector and checks the contract hashes against the index (ConnectorService). The catalog
key is held offline by the maintainer; its public half ships in CATALOG_KEYS with an agentos
release. There is none yet: phase 1 installs from the repo directory, and tests sign with a
test key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from agentos.core.errors import PolicyDenied
from agentos.core.models import utcnow
from agentos.runner.connectors.compile import CompiledConnector

BUNDLE_FORMAT = "agentos.connectors.bundle/1"
# key id -> base64url raw Ed25519 public key. The catalog key ships here with a release.
CATALOG_KEYS: dict[str, str] = {}


class BundleRejected(PolicyDenied):
    code = "bundle_rejected"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def public_raw(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def public_text(key: Ed25519PublicKey | Ed25519PrivateKey) -> str:
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return _b64(public_raw(pub))


def key_id(key: Ed25519PublicKey | Ed25519PrivateKey | str) -> str:
    raw = _unb64(key) if isinstance(key, str) else public_raw(
        key.public_key() if isinstance(key, Ed25519PrivateKey) else key)
    return "ed25519:" + hashlib.sha256(raw).hexdigest()[:16]


def trusted_from(keys: Iterable[str]) -> dict[str, str]:
    """{key id: key} for base64url raw public keys."""
    return {key_id(k): k for k in keys}


def load_private_key(path: Path | str) -> Ed25519PrivateKey:
    """A PEM (PKCS8) Ed25519 private key, as `openssl genpkey -algorithm ed25519` writes it."""
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise BundleRejected(f"{path} is not an Ed25519 private key")
    return key


def private_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def build_payload(compiled: Iterable[CompiledConnector], *, created_at: datetime | None = None) -> dict[str, Any]:
    items = sorted(compiled, key=lambda c: c.key)
    keys = [c.key for c in items]
    if len(set(keys)) != len(keys):
        raise BundleRejected("a bundle holds each connector once")
    return {"created_at": (created_at or utcnow()).isoformat(),
            "connectors": [c.connector.canonical() for c in items],
            "index": [c.index() for c in items]}


def sign_payload(payload: dict[str, Any], key: Ed25519PrivateKey) -> dict[str, Any]:
    sig = key.sign(canonical_bytes(payload))
    return {"format": BUNDLE_FORMAT, "payload": payload,
            "signature": {"alg": "ed25519", "key_id": key_id(key), "sig": _b64(sig)}}


def verify_bundle(bundle: Any, trusted: Mapping[str, str] | None = None) -> tuple[dict[str, Any], str]:
    """(payload, key id) of a bundle signed by a trusted key; BundleRejected otherwise.
    `trusted` maps key ids to base64url public keys (default CATALOG_KEYS)."""
    trusted = CATALOG_KEYS if trusted is None else trusted
    if not isinstance(bundle, Mapping) or bundle.get("format") != BUNDLE_FORMAT:
        raise BundleRejected(f"not a connector bundle (format {BUNDLE_FORMAT})")
    sig = bundle.get("signature")
    payload = bundle.get("payload")
    if not isinstance(sig, Mapping) or not sig.get("sig"):
        raise BundleRejected("the bundle is not signed; only signed bundles install (or install from the repo "
                             "directory as the owner)")
    if sig.get("alg") != "ed25519":
        raise BundleRejected(f"unsupported signature algorithm {sig.get('alg')!r}")
    kid = str(sig.get("key_id") or "")
    public = trusted.get(kid)
    if public is None:
        raise BundleRejected(f"the bundle is signed by {kid or 'an unnamed key'}, which this node does not trust")
    if key_id(public) != kid:
        raise BundleRejected(f"trusted key {kid} does not match its id")
    if not isinstance(payload, Mapping):
        raise BundleRejected("the bundle has no payload")
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(public)).verify(_unb64(str(sig["sig"])), canonical_bytes(payload))
    except (InvalidSignature, ValueError, binascii.Error) as e:
        raise BundleRejected("the bundle signature does not verify: it was changed after signing") from e
    connectors, index = payload.get("connectors"), payload.get("index")
    if not isinstance(connectors, list) or not isinstance(index, list) or len(connectors) != len(index):
        raise BundleRejected("the bundle payload needs matching connectors and index lists")
    return dict(payload), kid
