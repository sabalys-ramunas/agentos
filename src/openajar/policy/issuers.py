"""Grant issuers this node trusts besides its own key: the org of the controller it enrolled with.

A controller signs grants with its org's Ed25519 grant key, in the same token format as this
node's own grants (openajar.policy.tokens). The node received that public key at enrollment
(docs/PROTOCOL.md) and verifies such tokens offline, exactly like its own: signature, chain,
expiry, scope and budget all go through the policy service unchanged. What differs:

- the grant was never registered on this node, so the "issued here" check is skipped;
- revocations come from the controller (`revoke`, and `welcome.revoked_grants`), are stored
  here and checked on every call; an id matches a block's revocation id (`rid`) or its grant
  id, and since every narrowed token carries its parents' blocks, revoking a grant revokes
  everything narrowed from it;
- while the channel is down, a grant this node has never seen is refused (it may have been
  minted after a revocation the node could not receive), and grants it has seen keep
  working until they expire;
- a foreign grant can never carry `admin`: node administration stays with the node's owner;
- every chain admitted is remembered (`chains`: each block's ids, lane patterns and expiry, no
  secret), so a local freeze can revoke the foreign grants that reach its scope
  (openajar.policy.freeze).

Token v2 (TOKENS.md): the authority block names its issuer (`iss`, e.g. `org:<org id>`) and key
(`kid`), and the key is looked up by both, never tried in turn. An issuer may have several keys:
trusting a new key for an issuer schedules its other keys to retire after an overlap
(`DEFAULT_KEY_OVERLAP_S`), and `retire` ends one at a given time. Keys are kept in
grant_issuers.db, so the overlap survives a restart. v1 tokens (no kid) are tried against every
live key while the node accepts them. The policy service checks the audience (`aud`).

Denial reasons added here: `revoked` (as for local grants), `degraded`, `foreign_admin`,
`unknown_issuer`, `unknown_key` and `retired_key`.
`openajar.policy.service` calls `verify_foreign` and `check_live`; the channel registers the
`TrustedIssuers` service on the runtime and keeps it up to date.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from openajar.core.sqlite import SqliteStore
from openajar.policy import tokens
from openajar.policy.errors import Denied, TokenInvalid
from openajar.policy.util import b64u_decode

log = logging.getLogger("openajar.policy.issuers")

SERVICE_NAME = "grant_issuers"
DB_NAME = "grant_issuers.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS revoked (id TEXT PRIMARY KEY, issuer TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS seen (root_id TEXT PRIMARY KEY, issuer TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chains (
  leaf_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, issuer TEXT NOT NULL, blocks TEXT NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS keys (
  issuer TEXT NOT NULL, kid TEXT NOT NULL, pubkey TEXT NOT NULL, added_at REAL NOT NULL, retire_at REAL,
  PRIMARY KEY (issuer, kid)
);
"""
MAX_IDS_PER_REVOKE = 10_000
DEFAULT_KEY_OVERLAP_S = 7 * 86400


def load_key(text: str) -> Ed25519PublicKey:
    raw = b64u_decode(text)
    if len(raw) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


class TrustedIssuers(SqliteStore):
    """Runtime service "grant_issuers".

    On the shared SQLite base (openajar.core.sqlite). What a check reads (keys, revocations) is
    held in memory and changes there at once, under `_lock`; the database writes run outside
    that lock. A revocation, a first-seen root and an admitted chain never wait for another
    process's lock on the event loop: done at once when the lock is free, else queued in order
    on the worker thread; the controller's revoke then waits for `save_revoked` off the loop
    before it is answered. Off the loop (a freeze's sweep on policy.db's worker) they are
    committed before return. Key changes (trust, retire, forget) are always committed before
    return."""

    def __init__(self, home: Path, *, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._keys: dict[str, dict[str, tuple[Ed25519PublicKey, float | None]]] = {}
        self._lock = threading.Lock()
        super().__init__(Path(home) / DB_NAME, timeout=10.0, private=False)
        self._db.executescript(SCHEMA)
        self._revoked = {row[0] for row in self._db.execute("SELECT id FROM revoked")}
        for issuer, kid, pub, retire_at in self._db.execute("SELECT issuer, kid, pubkey, retire_at FROM keys"):
            try:
                self._keys.setdefault(issuer, {})[kid] = (load_key(pub), retire_at)
            except ValueError:
                log.error("ignoring a stored grant key of %s that does not parse", issuer)
        self._chains = {row[0] for row in self._db.execute("SELECT leaf_id FROM chains")}
        self._seen: set[str] = set()  # roots admitted by this process
        self._unsaved: dict[str, tuple] = {}  # chains admitted whose row has not landed yet
        self.connected: Callable[[], bool] | None = None  # set by the channel; None: no offline rule

    @property
    def _db(self) -> sqlite3.Connection:
        """This thread's connection."""
        return self._conn()

    def _x(self, sql: str, args: tuple = ()) -> None:
        self._conn().execute(sql, args)

    def _write(self, fn: Callable[..., Any], *args: Any) -> None:
        """A write: never waiting on the event loop (soon), committed before return off it."""
        if _on_loop():
            self.soon(fn, *args)
        else:
            fn(*args)

    def _save_key_changes(self, retiring: list[tuple[float, str, str]], issuer: str, kid: str, public_key: str,
                          now: float) -> None:
        with self._tx() as c:
            for row in retiring:
                c.execute("UPDATE keys SET retire_at = ? WHERE issuer = ? AND kid = ?", row)
            c.execute("INSERT INTO keys (issuer, kid, pubkey, added_at, retire_at) VALUES (?, ?, ?, ?, NULL) "
                      "ON CONFLICT(issuer, kid) DO UPDATE SET retire_at = NULL, pubkey = excluded.pubkey",
                      (issuer, kid, public_key, now))

    def save_revoked(self, ids: Iterable[str], *, issuer: str = "controller") -> None:
        """Commit these revocation ids, the ones already known included (a queued write that
        failed is written again): what the controller's revoke waits for before it is answered.
        Raises when the database refuses them."""
        now = self.clock()
        rows = [(v, issuer, now) for v in list(ids)[:MAX_IDS_PER_REVOKE] if isinstance(v, str) and 1 <= len(v) <= 100]
        if rows:
            self._save_revoked(rows)

    def _save_revoked(self, rows: list[tuple[str, str, float]]) -> None:
        with self._tx() as c:
            c.executemany("INSERT OR IGNORE INTO revoked (id, issuer, at) VALUES (?, ?, ?)", rows)

    # ---- keys ----
    def trust(self, issuer: str, public_key: str, *, overlap_s: float = DEFAULT_KEY_OVERLAP_S) -> str:
        """Accept grants signed by `public_key` (unpadded base64url Ed25519) as `issuer`; returns
        its kid. The issuer's other live keys keep working for `overlap_s` seconds (a rotation),
        then retire. Trusting a key again clears its retirement."""
        key = load_key(public_key)
        kid = tokens.key_id(key)
        now = self.clock()
        retiring: list[tuple[float, str, str]] = []
        with self._lock:
            known = self._keys.setdefault(issuer, {})
            for other, (pub, retire_at) in list(known.items()):
                if other != kid and retire_at is None:
                    known[other] = (pub, now + overlap_s)
                    retiring.append((now + overlap_s, issuer, other))
            if kid not in known or known[kid][1] is not None:
                log.info("trusting grant key %s of %s", kid, issuer)
            known[kid] = (key, None)
        self._save_key_changes(retiring, issuer, kid, public_key, now)  # durable: committed before return
        return kid

    def retire(self, issuer: str, kid: str, *, at: float | None = None) -> None:
        """Stop accepting grants signed with this key from `at` (now by default)."""
        when = self.clock() if at is None else at
        with self._lock:
            known = self._keys.get(issuer, {})
            if kid not in known:
                return
            known[kid] = (known[kid][0], when)
        self._x("UPDATE keys SET retire_at = ? WHERE issuer = ? AND kid = ?", (when, issuer, kid))  # durable

    def forget(self, issuer: str) -> None:
        with self._lock:
            self._keys.pop(issuer, None)
        self._x("DELETE FROM keys WHERE issuer = ?", (issuer,))  # durable: committed before return

    @property
    def issuers(self) -> list[str]:
        return sorted(i for i, keys in self._keys.items() if keys)

    def live(self, issuer: str) -> bool:
        """Whether this node still trusts `issuer`: it has a key that has not retired. What an issuer
        sent besides grants (its standing approvals) counts only while this holds."""
        return any(self._live(retire_at) for _, retire_at in self._keys.get(issuer, {}).values())

    def keys(self, issuer: str) -> list[dict]:
        now = self.clock()
        return [{"kid": kid, "retire_at": retire_at,
                 "state": "retired" if retire_at is not None and retire_at <= now else
                          ("retiring" if retire_at is not None else "active")}
                for kid, (_, retire_at) in sorted(self._keys.get(issuer, {}).items())]

    def _live(self, retire_at: float | None) -> bool:
        return retire_at is None or retire_at > self.clock()

    def open(self, token: str) -> tuple[str, tokens.Token]:
        """(issuer, verified token) for a token signed by a trusted issuer. v2: the key named by
        the authority block's `iss` and `kid` (Denied unknown_issuer, unknown_key or retired_key);
        v1: every live key in turn. TokenInvalid for a bad signature."""
        version, authority = tokens.peek(token)
        if version < 2:
            for issuer, keys in list(self._keys.items()):
                for key, retire_at in list(keys.values()):
                    if not self._live(retire_at):
                        continue
                    try:
                        return issuer, tokens.open_token(token, key)
                    except TokenInvalid:
                        continue
            raise TokenInvalid()
        issuer = authority.get("iss")
        keys = self._keys.get(issuer) if isinstance(issuer, str) else None
        if not keys:
            raise Denied("unknown_issuer", "this grant was not issued here or by an issuer this node trusts")
        entry = keys.get(authority.get("kid")) if isinstance(authority.get("kid"), str) else None
        if entry is None:
            raise Denied("unknown_key", f"this grant was signed with a key {issuer} has not given this node")
        if not self._live(entry[1]):
            raise Denied("retired_key", f"this grant was signed with a key {issuer} has retired")
        return issuer, tokens.open_token(token, entry[0])

    def verify(self, token: str) -> tuple[str, list[dict]]:
        """(issuer, block data) for a token signed by a trusted issuer (see `open`)."""
        issuer, opened = self.open(token)
        return issuer, opened.data

    # ---- revocations ----
    def revoke(self, ids: Iterable[str], *, issuer: str = "controller") -> list[str]:
        """Record revocation ids. Returns the ones that were new."""
        fresh = []
        now = self.clock()
        with self._lock:  # in memory first: every check refuses the grant from here on
            for value in list(ids)[:MAX_IDS_PER_REVOKE]:
                if not isinstance(value, str) or not 1 <= len(value) <= 100 or value in self._revoked:
                    continue
                self._revoked.add(value)
                fresh.append(value)
        if fresh:  # durable off the loop; on it, queued, and the caller awaits save_revoked()
            self._write(self._save_revoked, [(value, issuer, now) for value in fresh])
        if fresh:
            log.info("%d grant revocations from %s", len(fresh), issuer)
        return fresh

    def is_revoked(self, ids: Iterable[str]) -> bool:
        return any(value in self._revoked for value in ids)

    @property
    def revoked(self) -> frozenset[str]:
        return frozenset(self._revoked)

    # ---- checks ----
    def check_live(self, grant: Any) -> None:
        if getattr(grant, "owner_token", False):
            return
        if self.is_revoked([*grant.chain_ids, *grant.rids]):
            raise Denied("revoked", "this grant, or one it was narrowed from, was revoked by the controller")

    def admit(self, issuer: str, grant: Any) -> None:
        """Rules for a grant this node did not issue (see the module docstring)."""
        if any(block.admin for block in grant.blocks):
            raise Denied("foreign_admin", "a grant from the controller cannot carry admin rights on this node")
        self.check_live(grant)
        root = grant.root_id
        known = root in self._seen or \
            self._db.execute("SELECT 1 FROM seen WHERE root_id = ?", (root,)).fetchone() is not None
        online = self.connected() if self.connected is not None else True
        if not known:
            if not online:
                raise Denied("degraded", "the controller is unreachable, so this node accepts only grants it has "
                                         "already seen; try again once it is back")
            self._seen.add(root)  # read before the row lands (a queued write)
            self._write(self._x, "INSERT OR IGNORE INTO seen (root_id, issuer, at) VALUES (?, ?, ?)",
                        (root, issuer, self.clock()))
        self._remember(issuer, grant)

    def _remember(self, issuer: str, grant: Any) -> None:
        """Keep what a freeze needs to know about an admitted chain: per block its grant id,
        revocation id, lane patterns and expiry, and whether a person holds it."""
        leaf = grant.blocks[-1].id
        if leaf in self._chains:
            return
        blocks = [{"id": b.id, "rid": b.rid, "pats": b.lane_patterns(authority=i == 0), "exp": b.exp}
                  for i, b in enumerate(grant.blocks)]
        sub = grant.blocks[0].sub
        data = {"blocks": blocks, "human": bool(sub is not None and sub.kind == "human"),
                "subject": f"{sub.kind}:{sub.id}" if sub is not None else "?"}
        row = (leaf, grant.root_id, issuer, json.dumps(data), self.clock())
        with self._lock:
            self._unsaved[leaf] = row  # chains() lists it until the write lands
        try:
            self._write(self._save_chain, row)
        except sqlite3.Error:
            log.warning("could not remember the chain of foreign grant %s", leaf)
            return
        self._chains.add(leaf)

    def _save_chain(self, row: tuple) -> None:
        self._x("INSERT OR IGNORE INTO chains (leaf_id, root_id, issuer, blocks, at) VALUES (?, ?, ?, ?, ?)", row)
        with self._lock:  # only once it landed: a failed write stays listed by chains() in this process
            self._unsaved.pop(row[0], None)

    def chains(self) -> list[dict]:
        """Every foreign chain this node admitted, and (`blocks` None) each root seen before chains
        were kept: {leaf_id, root_id, issuer, blocks, human, subject}."""
        with self._lock:
            unsaved = list(self._unsaved.values())
        rows = self._db.execute("SELECT leaf_id, root_id, issuer, blocks FROM chains ORDER BY at").fetchall()
        saved = {r[0] for r in rows}
        rows += [r[:4] for r in unsaved if r[0] not in saved]
        kept = {r[1] for r in rows}  # roots with a chain, saved or not: never listed as legacy
        legacy = [r for r in self._db.execute("SELECT root_id, issuer FROM seen WHERE root_id NOT IN "
                                              "(SELECT root_id FROM chains) ORDER BY at").fetchall()
                  if r[0] not in kept]
        out: list[dict] = []
        for leaf, root, issuer, text in rows:
            try:
                data = json.loads(text)
                blocks = list(data["blocks"])
            except (ValueError, KeyError, TypeError):
                data, blocks = {}, None
            out.append({"leaf_id": leaf, "root_id": root, "issuer": issuer, "blocks": blocks,
                        "human": bool(data.get("human")), "subject": str(data.get("subject") or "?")})
        out += [{"leaf_id": root, "root_id": root, "issuer": issuer, "blocks": None, "human": False, "subject": "?"}
                for root, issuer in legacy]
        return out



def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _issuers(service: Any) -> TrustedIssuers | None:
    runtime = getattr(service, "runtime", None)
    getter = getattr(runtime, "get", None)
    found = getter(SERVICE_NAME) if callable(getter) else None
    return found if isinstance(found, TrustedIssuers) else None


def open_foreign(service: Any, token: str) -> tuple[tokens.Token, TrustedIssuers, str]:
    """For PolicyService.resolve: a token its own key did not sign. TokenInvalid (or a Denied
    naming the issuer or key) unless a trusted issuer signed it."""
    issuers = _issuers(service)
    if issuers is None:
        if tokens.version_of(token) >= 2:
            raise Denied("unknown_issuer", "this grant was not issued here or by an issuer this node trusts")
        raise TokenInvalid()
    issuer, opened = issuers.open(token)
    return opened, issuers, issuer


def verify_foreign(service: Any, token: str) -> tuple[list[dict], TrustedIssuers, str]:
    """(block data, issuers, issuer): `open_foreign` for callers that need only the data."""
    opened, found, issuer = open_foreign(service, token)
    return opened.data, found, issuer


def check_live(service: Any, grant: Any) -> None:
    """For PolicyService.check_live: revocations pushed by a controller. A no-op without one."""
    issuers = _issuers(service)
    if issuers is not None:
        issuers.check_live(grant)
