"""The tamper-evident action log: the canonical row form, the hash chain, signed marks, the
node audit key, and the verifier.

The specification, with worked examples, is AUDIT-LOG.md next to this file; the test vectors
are in tests/fixtures/audit/. In short:

- every trace row is hashed after redaction, exactly as stored: `digest` over its canonical
  record, `lane_tag` over its lane and id, and `hash` links the previous row's hash to both;
- marks (genesis, checkpoint, pruned, key) form their own chain and are signed with the
  node audit key (Ed25519, `secrets/audit-<kid>.key`), never the grant signing key;
- the verifier walks rows in seq order against the marks and says, one sentence per finding,
  what was edited, deleted, reordered, forged or is not provable yet.

Nothing here reads a secret: verification needs only the stored rows, the marks and the
public keys the marks carry, so an auditor can run it offline on an export bundle.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import sqlite3
from array import array
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from agentos.policy.keys import _load, _write_private
from agentos.policy.tokens import key_id
from agentos.policy.util import b64u, b64u_decode, iso

log = logging.getLogger("agentos.audit")

FORMAT = 1
# the stored columns a row's digest covers, in the order they are written
ROW_COLUMNS = ("seq", "id", "at", "kind", "principal", "principal_kind", "grant_id", "grant_path", "tool",
               "lane", "workspace", "effect", "status", "summary", "args", "error", "cost_tokens", "cost_usd",
               "duration_ms", "run_id", "parent_id")
REAL_COLUMNS = frozenset({"at", "cost_usd"})
INTEGER_COLUMNS = frozenset({"seq", "cost_tokens", "duration_ms"})
ROW_CTX = b"agentos.audit.row.v1\x00"
LANE_CTX = b"agentos.audit.lane.v1\x00"
LINK_CTX = b"agentos.audit.link.v1\x00"
MARK_CTX = b"agentos.audit.mark.v1\x00"
SIG_CTX = b"agentos.audit.sig.v1\x00"
BUNDLE_CTX = b"agentos.audit.bundle.v1\x00"
SHARE_CTX = b"agentos.audit.share.v1\x00"
MARK_KINDS = ("genesis", "checkpoint", "pruned", "key")

# exit codes of `agentos audit verify` (1 is "could not run", 2 is a usage error). Status "pending" (all
# signed rows check out, the newest wait for the next checkpoint) exits 0, as "ok" does.
EXIT_OK, EXIT_TAMPERED, EXIT_UNVERIFIABLE = 0, 3, 4


# ---------------------------------------------------------------- canonical form

def real(value: float) -> str:
    """A REAL as text: the shortest decimal that reads back as the same double, positional
    (no exponent), with a digit on each side of the point. Non-finite values are spelled out."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    text = repr(float(value))
    if "e" not in text:  # repr is already the shortest round trip, positional, with ".0"
        return text
    text = format(Decimal(text), "f")
    return text if "." in text else text + ".0"


def _typed(v: Any) -> dict[str, Any]:
    """A value of a type its column never holds when agentos wrote it (a BLOB, a boolean, text in
    a REAL column, a REAL in an INTEGER column, a number in a TEXT column), as a one-key JSON
    object naming the type. An honest record holds only strings, numbers and null, so a typed
    value can never share a digest with one."""
    if isinstance(v, bool):
        return {"bool": v}
    if isinstance(v, int):
        return {"int": str(v)}
    if isinstance(v, float):
        return {"real": real(v)}
    if isinstance(v, str):
        return {"text": v}
    if isinstance(v, bytes | bytearray | memoryview):
        return {"blob": bytes(v).hex()}
    return {"other": f"{type(v).__name__}:{v}"}


def _value(col: str, v: Any) -> Any:
    """A column value in the record: null, a string for TEXT and REAL columns, a number for
    INTEGER columns, or a typed object for anything else (see `_typed`)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return _typed(v)
    if col in REAL_COLUMNS:
        return real(float(v)) if isinstance(v, int | float) else _typed(v)
    if col in INTEGER_COLUMNS:
        return v if isinstance(v, int) else _typed(v)
    return v if isinstance(v, str) else _typed(v)


def canonical(obj: Any) -> bytes:
    """JSON with sorted keys, no whitespace, UTF-8, the RFC 8785 string escapes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


_SORTED = tuple(sorted((*ROW_COLUMNS, "v")))
_DUMPS = json.JSONEncoder(separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode


def row_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """What a row's digest covers: format version and every stored column except prev and hash,
    keys in sorted order."""
    rec: dict[str, Any] = {}
    for col in _SORTED:
        if col == "v":
            rec["v"] = FORMAT
            continue
        v = row[col]
        if col in REAL_COLUMNS:
            rec[col] = real(v) if type(v) is float else _value(col, v)
        elif v is None or (type(v) is str and col not in INTEGER_COLUMNS) or (type(v) is int and col in INTEGER_COLUMNS):
            rec[col] = v
        else:
            rec[col] = _value(col, v)
    return rec


def row_digest(row: Mapping[str, Any]) -> str:
    data = _DUMPS(row_record(row)).encode()  # the record is built in sorted key order: canonical()
    return hashlib.sha256(ROW_CTX + data).hexdigest()


def lane_tag(lane: str | None, trace_id: str) -> str:
    """Binds a row to its lane without saying which: an export can show a row it leaves out
    was not from the exported lane."""
    return hashlib.sha256(LANE_CTX + (lane or "").encode() + b"\x00" + trace_id.encode()).hexdigest()


def link(prev: str, digest: str, tag: str) -> str:
    return "sha256:" + hashlib.sha256(LINK_CTX + prev.encode() + b"\x00" + digest.encode() + b"\x00"
                                      + tag.encode()).hexdigest()


def row_hash(prev: str, row: Mapping[str, Any]) -> str:
    return link(prev, row_digest(row), lane_tag(row["lane"], row["id"]))


def mark_hash(body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(MARK_CTX + canonical(dict(body))).hexdigest()


def sign(key: Ed25519PrivateKey, ctx: bytes, data: bytes) -> str:
    return b64u(key.sign(ctx + data))


def pub_text(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    if isinstance(key, Ed25519PrivateKey):
        key = key.public_key()
    return b64u(key.public_bytes_raw())


def public_key(text: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(b64u_decode(text))


def kid_of(text: Any) -> str | None:
    """The key id of a public key as a mark carries it, or None when it does not decode."""
    try:
        return key_id(public_key(text))
    except (TypeError, ValueError):
        return None


def sig_ok(pub: str | None, ctx: bytes, data: bytes, sig: str | None) -> bool:
    if not pub or not sig:
        return False
    try:
        public_key(pub).verify(b64u_decode(sig), ctx + data)
    except (InvalidSignature, ValueError):
        return False
    return True


# ---------------------------------------------------------------- the audit key

KEY_PREFIX = "audit-"
TRUST_NAME = "audit-trust.json"


def _kid_file(kid: str) -> str:
    return KEY_PREFIX + kid.removeprefix("ed25519:") + ".key"


class AuditKeys:
    """The node audit keys: `secrets/audit-<kid hex>.key` (private, mode 600; only the active
    one is kept) and `secrets/audit-trust.json`, the public keys this node has introduced, in
    order, with its chain id. Which key is active is decided by the log's own marks (the latest
    genesis or key mark), so a key file swapped behind the log's back is never used silently."""

    def __init__(self, secrets_dir: Path):
        self.dir = Path(secrets_dir)
        self._cache: dict[str, Ed25519PrivateKey] = {}

    def create(self) -> tuple[str, Ed25519PrivateKey]:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        key = Ed25519PrivateKey.generate()
        kid = key_id(key)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        _write_private(self.dir / _kid_file(kid), pem)
        self._cache[kid] = key
        return kid, key

    def private(self, kid: str | None) -> Ed25519PrivateKey | None:
        if not kid:
            return None
        if kid in self._cache:
            return self._cache[kid]
        path = self.dir / _kid_file(kid)
        try:
            key = _load(path)
        except FileNotFoundError:
            return None
        except Exception:
            log.exception("audit key %s is unusable", path)
            return None
        if key_id(key) != kid:
            log.error("audit key file %s holds another key; not used", path)
            return None
        self._cache[kid] = key
        return key

    def forget(self, kid: str) -> None:
        """Delete a retired private key: old checkpoints verify with the public key in the log."""
        self._cache.pop(kid, None)
        with contextlib.suppress(FileNotFoundError, OSError):
            (self.dir / _kid_file(kid)).unlink()

    def trust(self) -> dict | None:
        try:
            data = json.loads((self.dir / TRUST_NAME).read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.error("%s is unreadable; verify will not trust any key", self.dir / TRUST_NAME)
            return {"chain": None, "keys": []}
        return data if isinstance(data, dict) else {"chain": None, "keys": []}

    def add_trust(self, chain: str, kid: str, pub: str, at: float) -> None:
        data = self.trust() or {"chain": chain, "keys": []}
        if data.get("chain") not in (None, chain):
            log.error("audit trust file names chain %s, not %s; left as it is", data.get("chain"), chain)
            return
        data["chain"] = chain
        keys = [k for k in data.get("keys", []) if isinstance(k, dict) and k.get("kid") != kid]
        keys.append({"kid": kid, "pub": pub, "added_at": iso(at)})
        data["keys"] = keys
        _write_private(self.dir / TRUST_NAME, json.dumps(data, indent=1).encode())

    def trusted_kids(self) -> set[str] | None:
        data = self.trust()
        if data is None:
            return None
        return {k["kid"] for k in data.get("keys", []) if isinstance(k, dict) and k.get("kid")}


# ---------------------------------------------------------------- reading a log

def read_marks(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute("SELECT n, kind, body, hash, sig FROM audit_marks ORDER BY n").fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"n": r[0], "kind": r[1], "body": r[2], "hash": r[3], "sig": r[4]} for r in rows]


def has_chain(conn: sqlite3.Connection) -> bool:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(traces)").fetchall()}
    return {"prev", "hash"} <= cols and bool(read_marks(conn))


def max_seq_used(conn: sqlite3.Connection) -> int | None:
    """The highest seq the log ever used (SQLite's AUTOINCREMENT counter)."""
    try:
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'traces'").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row else None


def iter_rows(conn: sqlite3.Connection, *, from_seq: int = 0, to_seq: int | None = None, batch: int = 5000,
              lock: Any = None) -> Iterator[dict]:
    """Rows in seq order, read in batches so a long walk never holds a lock for long."""
    cols = ", ".join((*ROW_COLUMNS, "prev", "hash"))
    last = from_seq - 1
    while True:
        sql, params = f"SELECT {cols} FROM traces WHERE seq > ?", [last]
        if to_seq is not None:
            sql += " AND seq <= ?"
            params.append(to_seq)
        with lock or contextlib.nullcontext():
            rows = conn.execute(sql + " ORDER BY seq LIMIT ?", (*params, batch)).fetchall()
        if not rows:
            return
        for r in rows:
            yield dict(zip((*ROW_COLUMNS, "prev", "hash"), r, strict=True))
        last = rows[-1][0]


def stub(row: Mapping[str, Any]) -> dict:
    """A row an export leaves out: enough to keep the chain checkable, none of its content."""
    return {"seq": row["seq"], "id": row["id"], "stub": True, "prev": row["prev"], "hash": row["hash"],
            "digest": row_digest(row), "lane_tag": lane_tag(row["lane"], row["id"])}


# ---------------------------------------------------------------- verifying

@dataclass
class Scope:
    since: float | None = None
    until: float | None = None
    lane: str | None = None

    def covers(self, row: Mapping[str, Any]) -> bool:
        if row.get("stub"):
            return False
        at = row.get("at")
        if self.since is not None and (not isinstance(at, int | float) or at < self.since):
            return False
        if self.until is not None and (not isinstance(at, int | float) or at >= self.until):
            return False
        return self.lane is None or row.get("lane") == self.lane


@dataclass
class Finding:
    level: str              # "tampered" | "unprovable" | "pending" | "note" (only the first two set the status)
    text: str
    first: int | None = None
    last: int | None = None
    mark: int | None = None

    def public(self) -> dict:
        d: dict[str, Any] = {"level": self.level, "text": self.text}
        if self.first is not None:
            d["seq"] = [self.first, self.last if self.last is not None else self.first]
        if self.mark is not None:
            d["mark"] = self.mark
        return d


def _span(a: int, b: int, singular: str = "", plural: str = "") -> str:
    text = f"row seq {a}" if a == b else f"rows seq {a}-{b}"
    verb = singular if a == b else plural
    return f"{text} {verb}" if verb else text


@dataclass
class _Anomaly:
    seq: int
    prev: str | None
    link_ok: bool
    content_ok: bool
    earlier: int | None


@dataclass
class Report:
    status: str
    checked: int
    first: int | None
    last: int | None
    proven_through: dict | None
    findings: list[Finding]
    chain: str | None
    keys: list[dict]
    bad: set[int] = field(default_factory=set)
    unchained: int = 0
    in_scope: int = 0
    unproven: set[int] = field(default_factory=set)
    anchors: dict | None = None  # what `verify --anchors` found (agentos.policy.anchors.fold)

    @property
    def exit_code(self) -> int:
        return {"ok": EXIT_OK, "pending": EXIT_OK, "tampered": EXIT_TAMPERED}.get(self.status, EXIT_UNVERIFIABLE)

    def row_status(self, row: Mapping[str, Any]) -> str:
        """One row's chain status: ok, tampered, unchained or unprovable."""
        seq = row["seq"]
        if seq in self.bad:
            return "tampered"
        if row.get("hash") is None:
            return "unchained"
        if seq in self.unproven:
            return "unprovable"
        through = (self.proven_through or {}).get("seq")
        if through is None or seq > through:
            return "unprovable"
        return "ok"

    def public(self) -> dict:
        return {"status": self.status, "checked": self.checked,
                "rows": {"first": self.first, "last": self.last, "in_scope": self.in_scope},
                "proven_through": self.proven_through, "chain": self.chain, "keys": self.keys,
                "unchained": self.unchained, "findings": [f.public() for f in self.findings],
                **({"anchors": self.anchors} if self.anchors is not None else {})}


@dataclass
class _MarkWalk:
    """Where the walk over the marks is: the audit key active at this point (kid, pub), and the mark before."""
    active: tuple[Any, Any] | None = None
    prev_n: int | None = None
    prev_hash: Any = None


def _mark_body(m: Mapping[str, Any]) -> dict | None:
    """A mark's body as stored, or None when it is not a JSON object."""
    try:
        body = json.loads(m["body"])
    except (TypeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _computed_hash(r: Mapping[str, Any]) -> str | None:
    """The hash a row's contents give (a stub's: from its digest and lane tag), or None when it cannot."""
    try:
        return link(str(r.get("prev")), str(r["digest"]), str(r["lane_tag"])) if r.get("stub")             else row_hash(str(r.get("prev")), r)
    except (KeyError, TypeError, ValueError):
        return None


class Verifier:
    """Feed rows in seq order, then `finish()`.

    `partial` is an export bundle: the first row's prev is taken as given (the proof runs
    forward, from rows to the checkpoint that covers them), and checkpoints outside the rows
    seen are only checked for their signatures. `mid` is a walk of the node's own log that
    starts at a later row (`--since`): the same, but the end of the log is still checked."""

    WINDOW = 1024  # how far apart two swapped rows can be and still be named as a reordering

    def __init__(self, marks: list[Mapping[str, Any]], *, trusted: set[str] | None = None, scope: Scope | None = None,
                 partial: bool = False, mid: bool = False, to_seq: int | None = None,
                 max_seq_used: int | None = None, trust_note: bool = True):
        self.scope = scope or Scope()
        self.partial = partial
        self.mid = mid
        self.to_seq = to_seq
        self.max_seq_used = max_seq_used
        self.findings: list[Finding] = []
        self.bad: set[int] = set()
        self.genesis: dict | None = None
        self.prune: dict | None = None            # the retention state signed marks prove
        self.prune_untrusted: dict | None = None  # a later retention mark that cannot be trusted
        self.checkpoints: list[dict] = []
        self.keys: list[dict] = []
        self.chain: str | None = None
        self._check_marks(marks, trusted, trust_note)
        g = self.genesis or {}
        self.after_seq = int(g.get("after_seq") or 0)
        self._cp_seqs = {c["seq"] for c in self.checkpoints}
        self._hash_at: dict[int, str] = {}
        self._kept = {int(k[0]): k[1] for k in self.prune["kept"]} if self.prune else {}
        self._through = int(self.prune["through_seq"]) if self.prune else 0
        self._through_u = int(self.prune_untrusted["through_seq"]) if self.prune_untrusted else 0
        self._unproven: set[int] = set()
        self._unchained_all = 0
        self._kept_seen: set[int] = set()
        self.expected_prev: str | None = None
        self.expected_seq: int | None = None
        self.count = 0
        self.first: int | None = None
        self.last: int | None = None
        self._recent: OrderedDict[str, int] = OrderedDict()
        self._wanted: dict[str, int] = {}
        self._later: dict[int, int] = {}
        self._anomalies: list[_Anomaly] = []
        self._scoped = array("q")
        self._unchained = array("q")

    # ---- marks ----
    def _tamper(self, text: str, *, first: int | None = None, last: int | None = None,
                mark: int | None = None) -> None:
        self.findings.append(Finding("tampered", text, first, last, mark))

    def _check_marks(self, marks: list[Mapping[str, Any]], trusted: set[str] | None, trust_note: bool) -> None:
        walk = _MarkWalk()
        for m in sorted(marks, key=lambda m: m["n"]):
            self._check_mark(m, walk, trusted, trust_note)
        if self.genesis is None and not self.partial:
            self.findings.append(Finding("unprovable", "the log has no genesis mark, so nothing in it can be proven"))

    def _check_mark(self, m: Mapping[str, Any], walk: _MarkWalk, trusted: set[str] | None, trust_note: bool) -> None:
        """One mark, in order: readable, intact, in sequence, signed by the key active at that point, and
        then what it says (the genesis, a key change, a checkpoint, a retention mark)."""
        n = int(m["n"])
        body = _mark_body(m)
        if body is None:
            self._tamper(f"mark {n} is not readable: it was edited", mark=n)
            walk.prev_n, walk.prev_hash = n, m.get("hash")
            return
        data = canonical(body)
        ok = True
        if data != str(m["body"]).encode() or mark_hash(body) != m.get("hash") or body.get("n") != n \
                or body.get("kind") != m.get("kind"):
            self._tamper(f"mark {n} ({m.get('kind')}) was edited: its contents no longer match its hash", mark=n)
            ok = False
        self._check_mark_order(walk, body, n)
        walk.prev_n, walk.prev_hash = n, m.get("hash")
        kind = body.get("kind")
        if kind == "genesis":
            if self.genesis is not None:
                self._tamper(f"mark {n} is a second genesis: the log was restarted on top of itself", mark=n)
                return
            genesis_ok = self._take_genesis(m, body, n, walk, trusted, trust_note)
            ok = ok and genesis_ok
        if walk.active is None:
            if kind != "genesis":
                self._tamper(f"mark {n} comes before any genesis", mark=n)
            return
        signer = walk.active
        if kind == "key" and body.get("reset"):
            signer = (body.get("next_kid"), body.get("next_pub"))
        signed_ok = self._mark_signed(m, body, data, n, kind, signer)
        ok = ok and signed_ok
        if kind == "key":
            self._key_mark(body, n, walk, trusted)
        elif kind == "checkpoint" and ok:
            self.checkpoints.append({**body, "n": n})
        elif kind == "pruned":
            if not ok:
                self.findings.append(Finding("unprovable", f"retention mark {n} cannot be trusted, so rows before "
                                                           f"seq {body.get('through_seq')} are not checked", mark=n))
            self._prune_mark(body, n, trusted=ok)

    def _check_mark_order(self, walk: _MarkWalk, body: Mapping[str, Any], n: int) -> None:
        """Marks run 1, 2, 3 ..., each naming the one before it."""
        if walk.prev_n is None:
            if n != 1 and not self.partial:
                self._tamper(f"marks 1-{n - 1} are missing: the log no longer starts at its genesis", mark=n)
        elif n != walk.prev_n + 1:
            self._tamper(f"marks {walk.prev_n + 1}-{n - 1} are missing: checkpoints were deleted", mark=n)
        elif body.get("prev") != walk.prev_hash:
            self._tamper(f"mark {n} does not follow mark {walk.prev_n}: checkpoints were replaced", mark=n)

    def _take_genesis(self, m: Mapping[str, Any], body: Mapping[str, Any], n: int, walk: _MarkWalk,
                      trusted: set[str] | None, trust_note: bool) -> bool:
        """The genesis starts the chain and names the first audit key. False when its key is unreadable
        or does not match the id it names."""
        ok = True
        self.genesis = {**body, "hash": m.get("hash")}
        self.chain = body.get("chain")
        active = walk.active = (body.get("kid"), body.get("pub"))
        if active[1] and kid_of(active[1]) is None:
            self._tamper(f"mark {n} carries an unreadable public key: it was edited", mark=n)
            ok = False
        elif active[0] and active[1] and kid_of(active[1]) != active[0]:
            self._tamper(f"mark {n} names key {active[0]} but carries another public key", mark=n)
            ok = False
        self.keys.append({"kid": active[0], "pub": active[1], "from_mark": n})
        if trusted is not None and active[0] not in trusted:
            self._tamper(f"the log starts from audit key {active[0]}, which this node never trusted "
                         f"({', '.join(sorted(trusted)) or 'no key'}): the key or the whole log was swapped",
                         mark=n)
        elif trusted is None and trust_note and active[0]:
            self.findings.append(Finding("note", f"trusting the log's own genesis key {active[0]}: "
                                                 "compare it with the key the node owner recorded", mark=n))
        return ok

    def _mark_signed(self, m: Mapping[str, Any], body: Mapping[str, Any], data: bytes, n: int, kind: Any,
                     signer: tuple[Any, Any]) -> bool:
        """Is the mark signed by the key active at that point (or, for a reset, the key it names)?"""
        sig = m.get("sig")
        if not sig:
            self.findings.append(Finding("unprovable", f"mark {n} ({kind}) is unsigned: the audit key was "
                                                       "unavailable when it was written", mark=n))
            return False
        if body.get("kid") != signer[0]:
            self._tamper(f"mark {n} ({kind}) is signed by key {body.get('kid')}, but the audit key at that "
                         f"point was {signer[0]}: the key was swapped", mark=n)
            return False
        if not sig_ok(signer[1], SIG_CTX, data, sig):
            self._tamper(f"mark {n} ({kind}) has a signature that does not verify: it was forged or edited",
                         mark=n)
            return False
        return True

    def _key_mark(self, body: Mapping[str, Any], n: int, walk: _MarkWalk, trusted: set[str] | None) -> None:
        """A key change: the key it names signs the marks after it."""
        nxt = (body.get("next_kid"), body.get("next_pub"))
        if not nxt[1] or kid_of(nxt[1]) is None:
            self._tamper(f"mark {n} carries an unreadable public key: it was edited", mark=n)
            return
        if not nxt[0] or kid_of(nxt[1]) != nxt[0]:
            self._tamper(f"mark {n} introduces a key whose id does not match it", mark=n)
            return
        if body.get("reset") and (trusted is None or nxt[0] not in trusted):
            self.findings.append(Finding(
                "unprovable", f"mark {n} reset the audit key to {nxt[0]} without a signature from "
                              f"{walk.active[0]}, so later checkpoints are only as good as that reset", mark=n))
        walk.active = nxt
        self.keys.append({"kid": nxt[0], "pub": nxt[1], "from_mark": n})

    def _prune_mark(self, body: Mapping[str, Any], n: int, *, trusted: bool) -> None:
        """Fold one retention mark into the retention state. Marks count cumulatively: retention only
        ever removes rows, so a mark that names a lower "pruned through" than the one before it (an
        older node wrote those after an unpin) only releases rows that one kept. A mark that cannot
        be trusted is kept apart: rows below it are unprovable, never called tampered for its sake."""
        try:
            through = int(body.get("through_seq") or 0)
            kept = [[int(k[0]), k[1]] for k in body.get("kept") or []] if trusted else []
        except (TypeError, ValueError, IndexError, KeyError):
            if trusted:
                self._tamper(f"retention mark {n} lists its kept rows in a form that cannot be read", mark=n)
            through, kept, trusted = 0, [], False
        mark = {"n": n, "through_seq": through, "through_hash": body.get("through_hash"), "kept": kept}
        if not trusted:
            self.prune_untrusted = mark
            return
        cur = self.prune
        if cur is not None and through < int(cur["through_seq"]):
            older = [k for k in cur["kept"] if k[0] > through]
            mark = {**cur, "n": n, "kept": [*kept, *older]}
        self.prune = mark
        u = self.prune_untrusted
        if u is not None and int(mark["through_seq"]) >= int(u["through_seq"]):
            self.prune_untrusted = None  # a signed mark covers everything the unsigned one did

    # ---- rows ----
    def feed(self, r: Mapping[str, Any]) -> None:
        seq = int(r["seq"])
        self._count(r, seq)
        stored = r.get("hash")
        if self.prune is not None and seq <= self._through:
            self._feed_pruned(r, seq, stored)
        elif self.prune_untrusted is not None and seq <= self._through_u:
            self._feed_below_untrusted(r, seq, stored)
        elif stored is None:
            self._feed_unhashed(r, seq)
        else:
            self._feed_chained(r, seq, stored)

    def _count(self, r: Mapping[str, Any], seq: int) -> None:
        """What every row adds to the walk, before its chain is checked; and a stub of the scope's own
        lane, which an export must not leave out."""
        self.count += 1
        if seq in self._kept:
            self._kept_seen.add(seq)
        if self.first is None:
            self.first = seq
        self.last = seq
        if self.scope.covers(r):
            self._scoped.append(seq)
        if r.get("stub") and self.scope.lane and r.get("lane_tag") == lane_tag(self.scope.lane, str(r.get("id"))):
            self._tamper(f"row seq {seq} belongs to lane {self.scope.lane}, but the export left it out",
                         first=seq)
            self.bad.add(seq)

    def _feed_pruned(self, r: Mapping[str, Any], seq: int, stored: Any) -> None:
        """Retention removed this part of the log: only the rows its signed mark kept may be here."""
        n = self.prune["n"]
        if seq not in self._kept:
            self._tamper(f"row seq {seq} is older than retention mark {n} (pruned through seq "
                         f"{self._through}), which did not keep it: it was added later", first=seq, mark=n)
            self.bad.add(seq)
        elif self._kept[seq] is None and stored is None:
            self._unchained_row(r, seq)  # a row from before the chain, kept as memory evidence
        elif self._kept[seq] != stored or _computed_hash(r) != stored:
            self._tamper(f"row seq {seq} was kept through retention by mark {n}, but its contents "
                         "changed", first=seq, mark=n)
            self.bad.add(seq)

    def _feed_below_untrusted(self, r: Mapping[str, Any], seq: int, stored: Any) -> None:
        """Below a retention mark that cannot be trusted: unproven, unless its own hash shows an edit."""
        if stored is not None and _computed_hash(r) != stored:
            self._tamper(f"row seq {seq} was edited after it was written: its contents no longer match "
                         "its hash", first=seq)
            self.bad.add(seq)
        else:
            self._unproven.add(seq)

    def _feed_unhashed(self, r: Mapping[str, Any], seq: int) -> None:
        """A row with no chain hash: from before the chain began, or tampered."""
        if self.genesis is not None and seq <= self.after_seq:
            self._unchained_row(r, seq)
        else:
            self._tamper(f"row seq {seq} has no chain hash, though it was written after the chain began",
                         first=seq)
            self.bad.add(seq)
        if self.expected_seq is not None:
            self.expected_seq = seq + 1

    def _feed_chained(self, r: Mapping[str, Any], seq: int, stored: str) -> None:
        """A chained row: it follows the row before it, and its contents still give its hash. What does
        not is kept as an anomaly, named once the walk ends (_classify: a reorder, a rewrite, an edit)."""
        computed = _computed_hash(r)
        if self.expected_seq is None:
            base = self._base()
            if base is None or self.partial or self.mid:
                self.expected_prev, self.expected_seq = None, seq
            else:
                self.expected_seq, self.expected_prev = base[0] + 1, base[1]
        link_ok = self.expected_prev is None or r.get("prev") == self.expected_prev
        content_ok = computed == stored
        if seq > self.expected_seq:
            self._tamper(f"{_span(self.expected_seq, seq - 1, 'is', 'are')} missing from the middle of the log (row seq {seq} links to a row that is not there)",
                         first=self.expected_seq, last=seq - 1)
            self.bad.update(range(self.expected_seq, seq))
            if not content_ok:
                self._anomalies.append(_Anomaly(seq, r.get("prev"), True, False, None))
        elif not link_ok or not content_ok:
            earlier = self._recent.get(str(r.get("prev"))) if not link_ok else None
            self._anomalies.append(_Anomaly(seq, r.get("prev"), link_ok, content_ok, earlier))
            if not link_ok and earlier is None:
                self._wanted[str(r.get("prev"))] = seq
        if stored in self._wanted:
            self._later[self._wanted.pop(stored)] = seq
        self._recent[stored] = seq
        if len(self._recent) > self.WINDOW:
            self._recent.popitem(last=False)
        if seq in self._cp_seqs:
            self._hash_at[seq] = stored
        self.expected_prev, self.expected_seq = stored, seq + 1

    def _unchained_row(self, r: Mapping[str, Any], seq: int) -> None:
        self._unchained_all += 1
        if self.scope.covers(r):
            self._unchained.append(seq)

    def _base(self) -> tuple[int, str] | None:
        """(seq, hash) the first chained row after retention, or after the genesis, links to."""
        marks = [m for m in (self.prune, self.prune_untrusted) if m is not None and m.get("through_hash")
                 and int(m["through_seq"]) > self.after_seq]
        if marks:
            m = max(marks, key=lambda m: int(m["through_seq"]))
            return int(m["through_seq"]), str(m["through_hash"])
        if self.genesis is not None:
            return self.after_seq, str(self.genesis["hash"])
        return None

    # ---- the verdict ----
    def _classify(self) -> None:
        reordered: set[int] = set()
        for a in self._anomalies:
            if a.seq in reordered:
                continue
            other = a.earlier if a.earlier is not None else self._later.get(a.seq)
            if not a.link_ok and other is not None:
                if other in reordered:
                    continue  # the row after a swapped pair links to it: explained by that pair
                lo, hi = sorted((a.seq, other))
                where = "after" if other > a.seq else "before"
                self._tamper(f"rows seq {lo} and {hi} are out of chain order: row {a.seq} links to row {other}, "
                             f"which is stored {where} it (rows were reordered)", first=lo, last=hi)
                reordered.update((lo, hi))
                self.bad.update((lo, hi))
                continue
            if not a.link_ok:
                self._tamper(f"row seq {a.seq} does not link to row seq {a.seq - 1}: row {a.seq - 1} was rewritten "
                             "or replaced", first=a.seq - 1, last=a.seq)
                self.bad.update((a.seq - 1, a.seq))
            if not a.content_ok:
                self._tamper(f"row seq {a.seq} was edited after it was written: its contents no longer match "
                             "its hash", first=a.seq)
                self.bad.add(a.seq)

    def finish(self) -> Report:
        self._classify()
        proven = self._check_checkpoints()
        self._check_kept()
        self._check_counts()
        self._note_unproven(proven)
        if any(f.level == "tampered" for f in self.findings):
            status = "tampered"
        elif any(f.level == "unprovable" for f in self.findings):
            status = "unverifiable"
        elif any(f.level == "pending" for f in self.findings):
            status = "pending"  # everything signed checks out; the newest rows wait for the next checkpoint
        else:
            status = "ok"
        return Report(status=status, checked=self.count, first=self.first, last=self.last, proven_through=proven,
                      findings=self.findings, chain=self.chain, keys=self.keys, bad=self.bad,
                      unchained=len(self._unchained), in_scope=len(self._scoped), unproven=self._unproven)

    def _check_checkpoints(self) -> dict | None:
        """Each signed checkpoint against the rows walked; the latest that vouches for one is what the
        log is proven through."""
        proven: dict | None = None
        for c in self.checkpoints:
            seq = int(c.get("seq") or 0)
            if seq <= self.after_seq or (self.to_seq is not None and seq > self.to_seq):
                continue
            if self.genesis is not None and c.get("count") != seq - self.after_seq:
                self._tamper(f"checkpoint {c['n']} counts {c.get('count')} rows but covers {seq - self.after_seq}",
                             mark=c["n"])
                continue
            if seq in self._hash_at:
                if self._hash_at[seq] != c.get("hash"):
                    self._tamper(f"checkpoint {c['n']} vouches for row seq {seq} with another hash than the log "
                                 "holds: history was rewritten after it was signed", first=seq, mark=c["n"])
                    self.bad.add(seq)
                    continue
                if proven is None or seq > proven["seq"]:
                    proven = {"mark": c["n"], "seq": seq, "at": c.get("at")}
            elif not self._outside_walk(seq) and (self.last is None or seq > self.last):
                last = self.last if self.last is not None else self.after_seq
                self._tamper(f"the log ends at seq {last}, but checkpoint {c['n']} covers rows through seq {seq}: "
                             f"{_span(last + 1, seq, 'was', 'were')} deleted from the end", first=last + 1, last=seq,
                             mark=c["n"])
                self.bad.update(range(last + 1, seq + 1))
        return proven

    def _outside_walk(self, seq: int) -> bool:
        """A checkpoint seq this walk cannot judge: removed by retention, before the rows walked, or any
        seq at all in an export bundle (or a mid walk that saw no rows)."""
        through = max(self._through, self._through_u)
        walked_from = self.first
        return seq <= through or (walked_from is not None and seq < walked_from) or self.partial \
            or (self.mid and walked_from is None)

    def _check_kept(self) -> None:
        """Every row a signed retention mark kept is still there."""
        for seq in sorted(set(self._kept) - self._kept_seen):
            if self.prune_untrusted is not None and seq <= self._through_u:
                continue  # the later retention mark that cannot be trusted may have released it
            if not (self.partial or self.mid) or (self.first is not None and seq >= self.first):
                self._tamper(f"row seq {seq} was kept through retention by mark {self.prune['n']} and is now "
                             "missing", first=seq, mark=self.prune["n"])

    def _check_counts(self) -> None:
        """No more rows from before the chain than the genesis counted, and no rows gone from the end."""
        counted = (self.genesis or {}).get("unchained")
        if not (self.partial or self.mid) and isinstance(counted, int) and self._unchained_all > counted:
            self._tamper(f"the genesis counted {counted} rows written before the chain began, but "
                         f"{self._unchained_all} are there: rows were added without a chain hash", mark=1)
        end = max(self.last or 0, self._through, self._through_u, self.after_seq)
        if not self.partial and self.to_seq is None and self.max_seq_used is not None and self.max_seq_used > end:
            self._tamper(f"the log has used seq up to {self.max_seq_used}, but it now ends at seq {end}: "
                         f"{_span(end + 1, self.max_seq_used, 'was', 'were')} deleted from the end",
                         first=end + 1, last=self.max_seq_used)

    def _note_unproven(self, proven: dict | None) -> None:
        """Rows from before the chain, and the scope's rows no checkpoint covers yet. Neither is wrong, so
        neither sets the status: rows from before the chain are counted by the genesis (more of them is
        tampering, _check_counts), and the newest rows wait for the next checkpoint, which comes every
        few minutes while the node runs. A real break in either still shows as tampered."""
        if len(self._unchained):
            first, last = self._unchained[0], self._unchained[-1]
            self.findings.append(Finding("note", f"{_span(first, last, 'was', 'were')} written before the chain "
                                                 "began (genesis, mark 1): kept and counted, but no hash covers them",
                                         first, last, 1))
        p_seq = proven["seq"] if proven else None
        tail = [s for s in self._scoped if (p_seq is None or s > p_seq) and s not in self.bad
                and not (self.genesis is not None and s <= self.after_seq)]
        if tail:
            n = len(tail)
            rows = f"{n} newer row{'s' if n != 1 else ''} ({_span(tail[0], tail[-1])})"
            after = f" after checkpoint {proven['mark']}" if proven else ""
            verb = "waits" if n == 1 else "wait"
            self.findings.append(Finding("pending", f"{rows} {verb} for the next checkpoint{after}",
                                         tail[0], tail[-1], proven["mark"] if proven else None))


def verify(rows: Iterable[Mapping[str, Any]], marks: list[Mapping[str, Any]], **kw: Any) -> Report:
    v = Verifier(marks, **kw)
    for r in rows:
        v.feed(r)
    return v.finish()


NO_CHAIN = ("this log has no chain yet: it starts the next time the node starts with this version")


def verify_db(conn: sqlite3.Connection, *, trusted: set[str] | None, scope: Scope | None = None, lock: Any = None,
              from_seq: int | None = None, to_seq: int | None = None, trust_note: bool = True) -> Report:
    """Verify a node's own traces.db (a read-only connection is enough)."""
    scope = scope or Scope()
    with lock or contextlib.nullcontext():
        if not has_chain(conn):
            return Report(status="unverifiable", checked=0, first=None, last=None, proven_through=None,
                          findings=[Finding("unprovable", NO_CHAIN)], chain=None, keys=[])
        marks = read_marks(conn)
        used = max_seq_used(conn)
        start = from_seq
        if start is None and scope.since is not None:
            # start one row before the window, so rows deleted at its start (or a quiet window, with
            # no rows at all) are judged against the row the window follows; with none before it
            # the walk starts at the beginning of the log
            row = conn.execute("SELECT MIN(seq) FROM traces WHERE at >= ?", (scope.since,)).fetchone()
            first = row[0] if row else None
            sql = "SELECT MAX(seq) FROM traces" + (" WHERE seq < ?" if first is not None else "")
            row = conn.execute(sql, (first,) if first is not None else ()).fetchone()
            start = row[0] if row else None
    v = Verifier(marks, trusted=trusted, scope=scope, mid=start is not None, to_seq=to_seq, max_seq_used=used,
                 trust_note=trust_note)
    for r in iter_rows(conn, from_seq=start or 0, to_seq=to_seq, lock=lock):
        v.feed(r)
    return v.finish()
