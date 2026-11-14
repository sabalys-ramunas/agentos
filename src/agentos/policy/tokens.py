"""Grant tokens: an Ed25519-signed, append-only chain of blocks.

This is Biscuit's signature construction, implemented on `cryptography`. The properties
kept from Biscuit:

- offline verification with only the root public key;
- offline attenuation by any holder, with no minting secret: the token carries the
  private half of an ephemeral key, and appending a block signs it with that key and
  hands over a fresh one;
- a holder cannot strip or reorder blocks: each block's signature covers its index, its
  data, the next public key and the previous signature, and the proof key only matches
  the last block;
- sealing: replace the proof key with a signature so nobody can append further;
- (v2) revocation ids derived from each block's signature, never written by the block's
  author, and a key id in the authority block that selects the verification key.

What Biscuit's Datalog would add (declarative checks) lives in agentos.policy.grants as
plain Python: every block is a set of caveats and a call must satisfy all of them, so a
block can only narrow.

Two versions share one framing (TOKENS.md has the full specification and test vectors):
    b"AG" | u8 version | u8 nblocks | block* | proof
    block = u32be len | data | next_pub(32) | sig(64)
    proof = 0x01 | next_priv(32)   attenuable
          | 0x02 | seal_sig(64)    sealed

- v1 (`agt1_`): data is JSON, signed as written; the revocation id is block data. Still
  verified during the migration window (policy.accept_v1_tokens), never minted by the node.
- v2 (`agt2_`): data must be canonical JSON (`canonical`), parsed with a decoder that
  refuses duplicate keys, NaN, Infinity, null and any byte that differs from the canonical
  re-encoding; the signature input carries the data length; each block's revocation id is
  `revocation_id(sig)`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from agentos.policy.errors import TokenInvalid
from agentos.policy.util import b64u, b64u_decode, canonical_json, sha256_hex

PREFIXES = {1: "agt1_", 2: "agt2_"}
PREFIX = PREFIXES[2]
VERSION = 2          # what mint() produces by default
_MAGIC = b"AG"
_BLOCK_CTX = {1: b"agentos.grant.block.v1\x00", 2: b"agentos.grant.block.v2\x00"}
_SEAL_CTX = {1: b"agentos.grant.seal.v1\x00", 2: b"agentos.grant.seal.v2\x00"}
_RID_CTX = b"agentos.grant.revocation.v2\x00"
_PROOF_KEY = 1
_PROOF_SEAL = 2

MAX_TOKEN_CHARS = 32_768
MAX_BLOCKS = 12
MAX_BLOCK_BYTES = 8_192
MAX_SAFE_INT = 2**53 - 1      # integers a JSON number can carry exactly in every language
MAX_DECIMALS = 6              # fractional digits a v2 number may have (micro-dollars)
MAX_FRACTIONAL = 10**15       # a non-integer's magnitude stays below this
MAX_DEPTH = 16                # nesting of arrays and objects in one block

RawBlock = tuple[bytes, bytes, bytes]  # data, next_pub, sig


@dataclass(frozen=True)
class Block:
    """One verified block: its parsed data, the exact signed bytes, its signature and its
    revocation id (v2: derived from the signature; v1: whatever `rid` the data carries)."""
    data: dict
    raw: bytes
    sig: bytes
    rid: str | None


@dataclass(frozen=True)
class Token:
    version: int
    blocks: tuple[Block, ...]
    sealed: bool

    @property
    def authority(self) -> dict:
        return self.blocks[0].data

    @property
    def data(self) -> list[dict]:
        return [b.data for b in self.blocks]


# ---------------------------------------------------------------- ids

def key_id(pub: Ed25519PublicKey | Ed25519PrivateKey) -> str:
    """`ed25519:` plus the first 16 bytes of SHA-256 over the raw 32-byte public key, in hex."""
    if isinstance(pub, Ed25519PrivateKey):
        pub = pub.public_key()
    return "ed25519:" + hashlib.sha256(pub.public_bytes_raw()).hexdigest()[:32]


def revocation_id(sig: bytes) -> str:
    """A v2 block's revocation id: `rv_` plus the first 20 bytes of
    SHA-256("agentos.grant.revocation.v2" NUL || sig) in lowercase unpadded base32."""
    digest = hashlib.sha256(_RID_CTX + sig).digest()[:20]
    return "rv_" + base64.b32encode(digest).decode().rstrip("=").lower()


# ---------------------------------------------------------------- canonical JSON (v2)

def _number(value: float) -> str:
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise ValueError(f"integers in a grant block must be within +-(2**53 - 1), not {value}")
        return str(value)
    if not math.isfinite(value):
        raise ValueError("NaN and Infinity cannot appear in a grant block")
    if value.is_integer():
        if abs(value) > MAX_SAFE_INT:
            raise ValueError(f"integers in a grant block must be within +-(2**53 - 1), not {value!r}")
        return str(int(value))
    if abs(value) >= MAX_FRACTIONAL:
        raise ValueError(f"a fractional number in a grant block must be below 1e15, not {value!r}")
    text = format(Decimal(repr(value)), "f")  # the shortest round-trip digits, never an exponent
    if len(text.partition(".")[2]) > MAX_DECIMALS:
        raise ValueError(f"a number in a grant block has at most {MAX_DECIMALS} decimals, not {value!r}")
    return text


def _encode_value(value: object, depth: int, out: list[str]) -> None:
    if depth > MAX_DEPTH:
        raise ValueError("a grant block is nested too deeply")
    if value is None:
        raise ValueError("null cannot appear in a grant block; leave the field out")
    if value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int | float):
        out.append(_number(value))
    elif isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, list | tuple):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _encode_value(item, depth + 1, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        for i, key in enumerate(sorted(value)):
            if not isinstance(key, str) or not key.isascii() or not key:
                raise ValueError("grant block keys must be non-empty ASCII strings")
            if i:
                out.append(",")
            out.append(json.dumps(key))
            out.append(":")
            _encode_value(value[key], depth + 1, out)
        out.append("}")
    else:
        raise ValueError(f"a grant block cannot hold a {type(value).__name__}")


def canonical(obj: dict) -> bytes:
    """The v2 canonical encoding of a block (TOKENS.md, "Canonical JSON"): keys sorted, no
    whitespace, minimal string escapes, UTF-8, integers within 2**53, at most 6 decimals, no
    null, NaN or Infinity. ValueError for anything that has no canonical form."""
    if not isinstance(obj, dict):
        raise ValueError("a grant block is a JSON object")  # noqa: TRY004  (callers catch ValueError)
    out: list[str] = []
    _encode_value(obj, 0, out)
    try:
        return "".join(out).encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("a grant block string holds a lone surrogate") from None


def _refuse_constant(name: str) -> object:
    raise ValueError(f"{name} is not allowed")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def decode_canonical(raw: bytes) -> dict:
    """Parse v2 block bytes. TokenInvalid unless they are a JSON object without duplicate keys,
    NaN or Infinity, and byte-for-byte equal to their canonical re-encoding."""
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_refuse_constant)
        if not isinstance(parsed, dict) or canonical(parsed) != raw:
            raise ValueError("not canonical")
    except (ValueError, RecursionError):
        raise TokenInvalid("a block of the token is not canonical JSON") from None
    return parsed


def _decode_v1(raw: bytes) -> dict:
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise TokenInvalid() from None
    if not isinstance(parsed, dict):
        raise TokenInvalid()
    return parsed


def block_bytes(version: int, block: dict) -> bytes:
    """The bytes a block is signed as: canonical JSON for v2, the util canonical JSON for v1."""
    data = canonical(block) if version >= 2 else canonical_json(block)
    if len(data) > MAX_BLOCK_BYTES:
        raise ValueError("grant block too large")
    return data


# ---------------------------------------------------------------- framing

def _payload(version: int, index: int, data: bytes, next_pub: bytes, prev_sig: bytes) -> bytes:
    if version == 1:
        return _BLOCK_CTX[1] + struct.pack(">I", index) + data + next_pub + prev_sig
    return _BLOCK_CTX[version] + struct.pack(">II", index, len(data)) + data + next_pub + prev_sig


def _seal_payload(version: int, data: bytes, next_pub: bytes, sig: bytes) -> bytes:
    if version == 1:
        return _SEAL_CTX[1] + data + next_pub + sig
    return _SEAL_CTX[version] + struct.pack(">I", len(data)) + data + next_pub + sig


def _raw_pub(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes_raw()


def _encode(version: int, blocks: list[RawBlock], tag: int, proof: bytes) -> str:
    out = bytearray(_MAGIC + bytes([version, len(blocks)]))
    for data, nxt, sig in blocks:
        out += struct.pack(">I", len(data)) + data + nxt + sig
    out += bytes([tag]) + proof
    return PREFIXES[version] + b64u(bytes(out))


def _decode(token: str) -> tuple[int, list[RawBlock], int, bytes]:
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS:
        raise TokenInvalid()
    version = next((v for v, p in PREFIXES.items() if token.startswith(p)), None)
    if version is None:
        raise TokenInvalid()
    try:
        raw = b64u_decode(token[len(PREFIXES[version]):])
    except ValueError:
        raise TokenInvalid() from None
    if len(raw) < 4 or raw[:2] != _MAGIC or raw[2] != version:
        raise TokenInvalid()
    if b64u(raw) != token[len(PREFIXES[version]):] and version >= 2:
        raise TokenInvalid()  # v2: one string per token (no stray base64 trailing bits)
    count = raw[3]
    if not 1 <= count <= MAX_BLOCKS:
        raise TokenInvalid()
    pos = 4
    blocks = []
    for _ in range(count):
        if pos + 4 > len(raw):
            raise TokenInvalid("the token is truncated")
        (length,) = struct.unpack(">I", raw[pos:pos + 4])
        pos += 4
        if length > MAX_BLOCK_BYTES or pos + length + 96 > len(raw):
            raise TokenInvalid("the token is truncated")
        data = raw[pos:pos + length]
        nxt = raw[pos + length:pos + length + 32]
        sig = raw[pos + length + 32:pos + length + 96]
        pos += length + 96
        blocks.append((data, nxt, sig))
    if pos >= len(raw):
        raise TokenInvalid("the token is truncated")
    tag = raw[pos]
    proof = raw[pos + 1:]
    if (tag, len(proof)) not in ((_PROOF_KEY, 32), (_PROOF_SEAL, 64)):
        raise TokenInvalid("the token is truncated")
    return version, blocks, tag, proof


def version_of(token: str) -> int:
    return _decode(token)[0]


# ---------------------------------------------------------------- mint, append, seal

def mint(root: Ed25519PrivateKey, block: dict, *, next_key: Ed25519PrivateKey | None = None) -> str:
    """A new token with one authority block, signed by the root key. The version is the
    block's `v` (1 when absent, for the migration window's tests and tools)."""
    version = int(block.get("v", 1))
    if version not in PREFIXES:
        raise ValueError(f"unknown grant block version {version}")
    data = block_bytes(version, block)
    nxt = next_key or Ed25519PrivateKey.generate()
    nxt_pub = _raw_pub(nxt)
    sig = root.sign(_payload(version, 0, data, nxt_pub, b""))
    return _encode(version, [(data, nxt_pub, sig)], _PROOF_KEY, nxt.private_bytes_raw())


def append(token: str, block: dict, *, next_key: Ed25519PrivateKey | None = None) -> str:
    """Offline attenuation: append a block. Needs only the token, never the root key.
    It does not check that the block narrows anything; verification makes every block
    binding, so a block can only ever take permissions away. A v2 block must say `"v": 2`."""
    version, blocks, tag, proof = _decode(token)
    if tag != _PROOF_KEY:
        raise TokenInvalid("the token is sealed and cannot be narrowed further")
    if len(blocks) >= MAX_BLOCKS:
        raise TokenInvalid("the token has too many blocks")
    if version >= 2 and block.get("v") != version:
        raise ValueError(f"a block appended to a version {version} token must carry \"v\": {version}")
    holder = Ed25519PrivateKey.from_private_bytes(proof)
    data = block_bytes(version, block)
    nxt = next_key or Ed25519PrivateKey.generate()
    nxt_pub = _raw_pub(nxt)
    sig = holder.sign(_payload(version, len(blocks), data, nxt_pub, blocks[-1][2]))
    return _encode(version, [*blocks, (data, nxt_pub, sig)], _PROOF_KEY, nxt.private_bytes_raw())


def seal(token: str) -> str:
    version, blocks, tag, proof = _decode(token)
    if tag == _PROOF_SEAL:
        return token
    holder = Ed25519PrivateKey.from_private_bytes(proof)
    data, nxt, sig = blocks[-1]
    return _encode(version, blocks, _PROOF_SEAL, holder.sign(_seal_payload(version, data, nxt, sig)))


# ---------------------------------------------------------------- verify

def _parse(version: int, data: bytes, sig: bytes) -> Block:
    if version >= 2:
        parsed = decode_canonical(data)
        return Block(parsed, data, sig, revocation_id(sig))
    parsed = _decode_v1(data)
    rid = parsed.get("rid")
    return Block(parsed, data, sig, rid if isinstance(rid, str) else None)


def peek(token: str) -> tuple[int, dict]:
    """(version, authority block data) WITHOUT checking any signature: only for choosing the
    key to verify with (`iss`, `kid`). Never trust what it returns."""
    version, blocks, _, _ = _decode(token)
    return version, _parse(version, blocks[0][0], blocks[0][2]).data


KeyFor = Callable[[int, dict], Ed25519PublicKey]


def open_token(token: str, key_for: KeyFor | Ed25519PublicKey) -> Token:
    """Check every signature and the proof; return the parsed blocks, authority first.
    `key_for(version, authority data)` picks the root key (it may raise a Denied, e.g. for an
    unknown key id); a plain public key is used as is."""
    version, blocks, tag, proof = _decode(token)
    parsed = [_parse(version, data, sig) for data, _, sig in blocks]
    pub = key_for if isinstance(key_for, Ed25519PublicKey) else key_for(version, parsed[0].data)
    prev_sig = b""
    for i, (data, nxt, sig) in enumerate(blocks):
        try:
            pub.verify(sig, _payload(version, i, data, nxt, prev_sig))
            pub = Ed25519PublicKey.from_public_bytes(nxt)
        except (InvalidSignature, ValueError):
            raise TokenInvalid() from None
        prev_sig = sig
    last_data, last_next, last_sig = blocks[-1]
    if tag == _PROOF_KEY:
        try:
            if _raw_pub(Ed25519PrivateKey.from_private_bytes(proof)) != last_next:
                raise TokenInvalid()
        except ValueError:
            raise TokenInvalid() from None
    else:
        try:
            pub.verify(proof, _seal_payload(version, last_data, last_next, last_sig))
        except InvalidSignature:
            raise TokenInvalid() from None
    return Token(version, tuple(parsed), tag == _PROOF_SEAL)


def verify(token: str, root_pub: Ed25519PublicKey) -> list[dict]:
    """Check every signature and the proof with `root_pub`; return the block data dicts."""
    return open_token(token, root_pub).data


def inspect(token: str) -> Token:
    """The blocks of a token WITHOUT checking signatures: for a token this process just built
    (to learn its revocation ids). Never use it on a presented token."""
    version, blocks, tag, _ = _decode(token)
    return Token(version, tuple(_parse(version, d, s) for d, _, s in blocks), tag == _PROOF_SEAL)


def is_sealed(token: str) -> bool:
    return _decode(token)[2] == _PROOF_SEAL


def token_hash(token: str) -> str:
    """What the registry stores instead of the token."""
    return "sha256:" + sha256_hex(token)
