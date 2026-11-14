"""The wire format of docs/PROTOCOL.md (v1), shared by the node and the open-source controller.

Pinned encodings (PROTOCOL.md "v1 clarifications", the same as the hosted cloud's):
- keys and signatures are unpadded base64url of the raw Ed25519 bytes;
- a nonce is an opaque string, signed as its exact text;
- an envelope `id` is `m_` + ULID; a bare `stream_id` is a 26 character ULID, case-insensitive
  and compared in upper case (each end normalizes it on parse);
- controller-signed bodies sign `"agentos-controller-msg:v1:" + type + ":" + node_id + ":" +
  canonical(body without sig)`, canonical being sorted-key compact JSON in UTF-8.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PROTOCOL_VERSION = 1
CONNECT_PATH = "/v1/node/connect"

MAX_JSON_BYTES = 1 << 20          # a text frame over this gets `error invalid`, the socket stays up
HARD_FRAME_BYTES = 8 << 20        # a frame over this closes the socket (1009)
STREAM_HEADER_BYTES = 16
INITIAL_CREDIT = 256 * 1024       # per stream, per direction
MAX_TRACES_PER_BATCH = 200

PING_INTERVAL = 20.0
DEAD_AFTER = 60.0
HEARTBEAT_INTERVAL = 15.0
BACKOFF_MIN = 1.0
BACKOFF_MAX = 60.0
HANDSHAKE_TIMEOUT = 15.0
RESULT_TTL = 300.0                # at-most-once cache per call_id

OUTBOX_TYPES = frozenset({"event", "traces", "approval_request"})
# Every controller-signed kind. Each one carries `ts` (fresh within SIGNED_FRESHNESS of the controller's
# now) and a `nonce` the node has not seen within NONCE_KEEP_S; the node checks all three for every kind
# (docs/PROTOCOL.md "Freshness and replay").
SIGNED_TYPES = frozenset({"revoke", "config_update", "lane_control", "mesh_freeze", "standing", "ask_first",
                          "workflow"})
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# Longer than the freshness window (300 s) either side plus the clock offset the node trusts at most
# (3600 s) either side, so a replay is caught however the offset moves between the two.
NONCE_KEEP_S = 2 * (300.0 + 3600.0)
CAPABILITIES = ("browser", "terminal", "service", "android", "gpu", "render", "brain")
EVENT_KINDS = ("lane", "run", "approval", "node", "standing", "workflow_run")

HELLO_PREFIX = "agentos-node-hello:v1:"
ENROLL_PREFIX = "agentos-node-enroll:v1:"
CONTROLLER_PREFIX = "agentos-controller-msg:v1:"

Json = dict[str, Any]

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_DECODE = {ch: i for i, ch in enumerate(_ULID_ALPHABET)}
ULID_LENGTH = 26
_B64U_RE = re.compile(r"^[A-Za-z0-9_-]*$")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class WireError(ValueError):
    """A frame that is not a usable message. `code` is what the `error` reply carries."""

    def __init__(self, code: str, message: str, re: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.re = re


class Oversize(WireError):
    """An outgoing message would break the 1 MiB limit."""

    def __init__(self, message: str):
        super().__init__("invalid", message)


# ---- ids ----

def ulid(now_ms: int | None = None) -> str:
    """48 bits of milliseconds, then 80 random bits, as 26 Crockford base32 characters."""
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    return ulid_text(stamp.to_bytes(6, "big") + secrets.token_bytes(10))


def ulid_text(raw: bytes) -> str:
    if len(raw) != 16:
        raise ValueError("a ULID is 16 bytes")
    value = int.from_bytes(raw, "big")
    out = []
    for _ in range(ULID_LENGTH):
        out.append(_ULID_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def ulid_bytes(text: str) -> bytes:
    if not isinstance(text, str) or len(text) != ULID_LENGTH:
        raise ValueError("a ULID is 26 characters")
    value = 0
    for ch in text.upper():
        digit = _ULID_DECODE.get(ch)
        if digit is None:
            raise ValueError("not a ULID")
        value = (value << 5) | digit
    if value >> 128:
        raise ValueError("ULID overflows 128 bits")
    return value.to_bytes(16, "big")


def normal_ulid(text: Any) -> Any:
    """A ULID in its one canonical spelling (upper case), so ids compare the same whichever case a
    peer sent; anything that is not a ULID comes back as it is."""
    return text.upper() if is_ulid(text) else text


def is_ulid(text: Any) -> bool:
    try:
        ulid_bytes(text)
    except ValueError:
        return False
    return True


def prefixed(prefix: str) -> str:
    return f"{prefix}_{ulid()}"


def message_id() -> str:
    return prefixed("m")


# ---- encodings ----

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


def hello_message(nonce: str, node_id: str) -> bytes:
    return f"{HELLO_PREFIX}{nonce}:{node_id}".encode()


def enroll_message(nonce: str, node_pubkey: str) -> bytes:
    """What `enroll.proof` signs: the node key proves it is held, on this socket's challenge."""
    return f"{ENROLL_PREFIX}{nonce}:{node_pubkey}".encode()


def controller_message(kind: str, node_id: str, body: Json) -> bytes:
    unsigned = {key: value for key, value in body.items() if key != "sig"}
    return f"{CONTROLLER_PREFIX}{kind}:{node_id}:".encode() + canonical(unsigned)


def sign_controller(key: Ed25519PrivateKey, kind: str, node_id: str, body: Json) -> Json:
    """`body` plus its `sig`, for a controller-signed message to one node."""
    unsigned = {k: v for k, v in body.items() if k != "sig"}
    return {**unsigned, "sig": b64e(key.sign(controller_message(kind, node_id, unsigned)))}


# ---- time ----

def iso(moment: datetime | float | None = None) -> str:
    """Envelope time, for example 2026-11-04T03:00:00Z."""
    if isinstance(moment, int | float):
        moment = datetime.fromtimestamp(moment, UTC)
    value = (moment or datetime.now(UTC)).astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> float | None:
    """ISO 8601 text (or epoch seconds) as epoch seconds; None when unusable. JSON numbers may be
    NaN or Infinity (`NaN`, `1e999`), and NaN passes every `abs(now - ts) > window` freshness
    check, so a time that is not finite is unusable (fail closed)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        try:
            seconds = float(value)
        except OverflowError:  # an int too large for a float
            return None
        return seconds if math.isfinite(seconds) else None
    if isinstance(value, str) and len(value) <= 40:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    return None


# ---- the envelope ----

@dataclass
class Envelope:
    type: str
    body: Json = field(default_factory=dict)
    id: str = field(default_factory=message_id)
    ts: str = field(default_factory=iso)
    seq: int | None = None
    re: str | None = None

    def to_dict(self) -> Json:
        data: Json = {"v": PROTOCOL_VERSION, "type": self.type, "id": self.id, "ts": self.ts}
        if self.seq is not None:
            data["seq"] = self.seq
        if self.re is not None:
            data["re"] = self.re
        data["body"] = self.body
        return data

    def encode(self) -> str:
        return encode(self.to_dict())

    @property
    def reply_to(self) -> str | None:
        """Replies carry `re` in the envelope, and result, progress and error also in the body."""
        if self.re:
            return self.re
        value = self.body.get("re")
        return value if isinstance(value, str) else None


def encode(data: Json) -> str:
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if len(text.encode()) > MAX_JSON_BYTES:
        raise Oversize(f"a {data.get('type')} message would exceed 1 MiB")
    return text


def decode(frame: str | bytes) -> Envelope:
    """Parse one text frame. Raises WireError with the code the `error` reply should carry."""
    raw = frame.encode() if isinstance(frame, str) else frame
    if len(raw) > MAX_JSON_BYTES:
        raise WireError("invalid", "the message exceeds the 1 MiB limit")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise WireError("invalid", "the message is not valid JSON") from None
    if not isinstance(data, dict):
        raise WireError("invalid", "the message is not a JSON object")
    ref = data.get("id") if isinstance(data.get("id"), str) and len(data["id"]) <= 100 else None
    if data.get("v") != PROTOCOL_VERSION:
        raise WireError("unsupported", f"protocol version {str(data.get('v'))[:20]!r} is not supported", ref)
    kind = data.get("type")
    if not isinstance(kind, str) or not kind or len(kind) > 64:
        raise WireError("invalid", "the message has no type", ref)
    body = data.get("body", {})
    if not isinstance(body, dict):
        raise WireError("invalid", "the message body is not an object", ref)
    seq = data.get("seq")
    re_ = data.get("re")
    return Envelope(
        type=kind,
        body=body,
        id=ref or "",
        ts=data["ts"] if isinstance(data.get("ts"), str) else "",
        seq=seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else None,
        re=re_ if isinstance(re_, str) and len(re_) <= 100 else None,
    )


def error_envelope(code: str, message: str, re: str | None = None, **extra: Any) -> Envelope:
    body: Json = {"code": code, "message": message[:600]}
    if re:
        body["re"] = re
    body.update({k: v for k, v in extra.items() if v is not None})
    return Envelope("error", body, re=re)


# ---- binary frames ----

def stream_frame(stream_id: str, payload: bytes) -> bytes:
    return ulid_bytes(stream_id) + payload


def parse_stream_frame(frame: bytes) -> tuple[str, bytes]:
    if len(frame) < STREAM_HEADER_BYTES:
        raise WireError("invalid", "the binary frame is shorter than the stream header")
    return ulid_text(frame[:STREAM_HEADER_BYTES]), frame[STREAM_HEADER_BYTES:]


# ---- the controller URL ----

def is_local_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.strip("[]").lower()
    if host in LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def connect_url(url: str) -> str:
    """The node channel URL from what the owner typed: http(s) becomes ws(s), the path defaults
    to /v1/node/connect, and plain ws is refused unless the controller is on this machine."""
    text = (url or "").strip()
    if "://" not in text:
        text = "wss://" + text
    parts = urlsplit(text)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme.lower(), parts.scheme.lower())
    if scheme not in ("ws", "wss"):
        raise ValueError(f"the controller URL must start with wss:// or https:// (got {parts.scheme}://)")
    if not parts.hostname:
        raise ValueError("the controller URL has no host")
    if parts.username or parts.password:
        raise ValueError("the controller URL must not carry a user name or password")
    if scheme == "ws" and not is_local_host(parts.hostname):
        raise ValueError("plain ws:// is only allowed to localhost; use wss:// (TLS) for a remote controller")
    path = parts.path if parts.path not in ("", "/") else CONNECT_PATH
    return urlunsplit((scheme, parts.netloc, path, parts.query, ""))
