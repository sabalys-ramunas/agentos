"""The controller's SQLite file (`<controller home>/controller.db`, WAL, mode 600).

Every tenant row has an `org_id`, and every query that reads or changes tenant data takes
one, so a caller can never reach another org's nodes, lanes, approvals or traces (even with
a single org, the default). Join tokens and agent tokens are stored as SHA-256 hashes only.
What is stored is metadata: no vault entry, cookie, proxy credential, profile byte or exit
IP ever reaches this file, because no node message carries one.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from agentos.core.sqlite import SqliteStore
from agentos.node.channel import wire

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
  id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS join_tokens (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, name TEXT, capabilities TEXT,
  created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL, node_id TEXT
);
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL, pubkey TEXT NOT NULL, version TEXT, os TEXT,
  arch TEXT, capabilities TEXT NOT NULL, max_warm_lanes INTEGER, created_at REAL NOT NULL, last_seen REAL,
  removed_at REAL, last_seq INTEGER NOT NULL DEFAULT 0, state TEXT, seq_epoch TEXT, UNIQUE (org_id, name)
);
CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, node_id TEXT, used_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS lanes (
  org_id TEXT NOT NULL, node_id TEXT NOT NULL, lane_id TEXT NOT NULL, payload TEXT NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY (org_id, node_id, lane_id)
);
CREATE TABLE IF NOT EXISTS grants (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, rid TEXT UNIQUE NOT NULL, parent_id TEXT, subject TEXT NOT NULL,
  block TEXT NOT NULL, token_hash TEXT UNIQUE, note TEXT, created_at REAL NOT NULL, expires_at REAL, revoked_at REAL
);
CREATE TABLE IF NOT EXISTS approvals (
  org_id TEXT NOT NULL, node_id TEXT NOT NULL, id TEXT NOT NULL, state TEXT NOT NULL, content_hash TEXT,
  payload TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, decided_by TEXT,
  PRIMARY KEY (org_id, node_id, id)
);
CREATE TABLE IF NOT EXISTS traces (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, node_id TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS traces_org ON traces (org_id, seq);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, node_id TEXT NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL, at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_org ON events (org_id, seq);
CREATE TABLE IF NOT EXISTS mesh_freeze (
  org_id TEXT PRIMARY KEY, freeze_id TEXT NOT NULL, reason TEXT NOT NULL, by TEXT NOT NULL, at REAL NOT NULL,
  revoked TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS standing (
  org_id TEXT NOT NULL, node_id TEXT NOT NULL, id TEXT NOT NULL, item TEXT NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY (org_id, node_id, id)
);
CREATE TABLE IF NOT EXISTS standing_revokes (
  org_id TEXT NOT NULL, node_id TEXT NOT NULL, id TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (org_id, node_id, id)
);
CREATE TABLE IF NOT EXISTS mesh_freeze_nodes (
  org_id TEXT NOT NULL, node_id TEXT NOT NULL, freeze_id TEXT NOT NULL, applied_at REAL NOT NULL,
  PRIMARY KEY (org_id, node_id)
);
-- audit anchors (agentos.policy.anchors): one checkpoint head per (node, chain, mark), never
-- updated or deleted, so a node that rewrites its log cannot make these match it again
CREATE TABLE IF NOT EXISTS audit_anchors (
  org_id TEXT NOT NULL, node_id TEXT NOT NULL, chain TEXT NOT NULL, n INTEGER NOT NULL, seq INTEGER NOT NULL,
  hash TEXT NOT NULL, kid TEXT, at TEXT, received_at TEXT NOT NULL, sig TEXT NOT NULL,
  PRIMARY KEY (org_id, node_id, chain, n)
);
CREATE TABLE IF NOT EXISTS audit_anchor_conflicts (
  org_id TEXT NOT NULL, node_id TEXT NOT NULL, chain TEXT NOT NULL, n INTEGER NOT NULL, seq INTEGER NOT NULL,
  hash TEXT NOT NULL, kid TEXT, at TEXT, received_at TEXT NOT NULL
);
"""

DEFAULT_ORG = "default"
JOIN_TOKEN_TTL = 3600
MAX_TRACES = 200_000
MAX_EVENTS = 50_000


def token_hash(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


class EnrollRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _anchor_receipt(row: dict) -> dict:
    anchor = {"v": 1, "chain": row["chain"], "n": row["n"], "seq": row["seq"], "hash": row["hash"],
              "kid": row["kid"], "at": row["at"]}
    if row.get("kind"):  # a retention mark; a checkpoint's anchor has no kind, as it always had
        anchor["kind"] = row["kind"]
    return {"anchor": anchor, "received_at": row["received_at"], "sig": row["sig"]}


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class ControllerStore(SqliteStore):
    """One kept connection per thread (agentos.core.sqlite). A node's last_seen goes through
    soon(); on the event loop, other writes go through `await store.run(...)` (durable)."""

    row_factory = sqlite3.Row

    def __init__(self, path: Path, *, clock=time.time):
        self.clock = clock
        super().__init__(path, timeout=10.0, private=False)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        db = self._conn()
        db.executescript(SCHEMA)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(nodes)")}
        if "seq_epoch" not in columns:  # a controller.db from before the node announced its seq epoch
            try:
                db.execute("ALTER TABLE nodes ADD COLUMN seq_epoch TEXT")
            except sqlite3.OperationalError as e:  # another process added it first
                if "duplicate column" not in str(e):
                    raise
        for table in ("audit_anchors", "audit_anchor_conflicts"):  # from before retention anchors
            if "kind" not in {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}:
                try:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN kind TEXT")
                except sqlite3.OperationalError as e:  # another process added it first
                    if "duplicate column" not in str(e):
                        raise

    def tx(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        """An IMMEDIATE transaction on this thread's connection (not reentrant)."""
        return self._tx()

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        return _row(self._conn().execute(sql, params).fetchone())

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self._conn().execute(sql, params).fetchall()]

    def _exec(self, sql: str, params: tuple = ()) -> int:
        return self._conn().execute(sql, params).rowcount

    # ---- orgs ----
    def default_org(self) -> dict:
        org = self.org_by_slug(DEFAULT_ORG)
        if org is None:
            org = self.create_org(DEFAULT_ORG, "Default")
        return org

    def create_org(self, slug: str, name: str) -> dict:
        org = {"id": wire.prefixed("org"), "slug": slug, "name": name, "created_at": self.clock()}
        self._exec("INSERT OR IGNORE INTO orgs (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
                   (org["id"], slug, name, org["created_at"]))
        return self.org_by_slug(slug) or org

    def org_by_slug(self, slug: str) -> dict | None:
        return self._one("SELECT * FROM orgs WHERE slug = ?", (slug,))

    def org(self, org_id: str) -> dict | None:
        return self._one("SELECT * FROM orgs WHERE id = ?", (org_id,))

    def orgs(self) -> list[dict]:
        return self._all("SELECT * FROM orgs ORDER BY created_at")

    # ---- join tokens ----
    def create_join_token(self, org_id: str, *, name: str | None = None, capabilities: list[str] | None = None,
                          ttl: int = JOIN_TOKEN_TTL) -> tuple[dict, str]:
        token = "ajt_" + secrets.token_urlsafe(32)
        now = self.clock()
        row = {"id": wire.prefixed("jt"), "org_id": org_id, "name": name,
               "capabilities": json.dumps(capabilities) if capabilities is not None else None,
               "created_at": now, "expires_at": now + ttl}
        self._exec("INSERT INTO join_tokens (id, org_id, token_hash, name, capabilities, created_at, expires_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (row["id"], org_id, token_hash(token), name, row["capabilities"], now, now + ttl))
        return row, token

    def join_tokens(self, org_id: str) -> list[dict]:
        rows = self._all("SELECT id, org_id, name, capabilities, created_at, expires_at, used_at, node_id "
                         "FROM join_tokens WHERE org_id = ? ORDER BY created_at DESC", (org_id,))
        for row in rows:
            row["capabilities"] = json.loads(row["capabilities"]) if row["capabilities"] else None
        return rows

    def delete_join_token(self, org_id: str, token_id: str) -> bool:
        return self._exec("DELETE FROM join_tokens WHERE org_id = ? AND id = ? AND used_at IS NULL",
                          (org_id, token_id)) == 1

    def enroll(self, token: str, *, pubkey: str, name: str, version: str | None, os_name: str | None,
               arch: str | None, capabilities: list[str], max_warm_lanes: int | None) -> dict:
        """Consume a join token and record the node, in one transaction."""
        now = self.clock()
        with self.tx() as db:
            jt = db.execute("SELECT * FROM join_tokens WHERE token_hash = ?", (token_hash(token),)).fetchone()
            if jt is None:
                raise EnrollRefused("denied", "this join token is not valid")
            if jt["used_at"] is not None:
                raise EnrollRefused("already_used", "this join token was already used")
            if jt["expires_at"] <= now:
                raise EnrollRefused("expired", "this join token has expired; create a new one")
            if jt["name"]:
                name = jt["name"]
            allowed = json.loads(jt["capabilities"]) if jt["capabilities"] else None
            if allowed is not None:
                capabilities = [c for c in capabilities if c in allowed]
            clash = db.execute("SELECT 1 FROM nodes WHERE org_id = ? AND name = ? AND removed_at IS NULL",
                               (jt["org_id"], name)).fetchone()
            if clash is not None:
                raise EnrollRefused("conflict", f"a node named {name} is already enrolled; join with another "
                                                "--name, or remove the old node first")
            db.execute("DELETE FROM nodes WHERE org_id = ? AND name = ? AND removed_at IS NOT NULL",
                       (jt["org_id"], name))
            node_id = wire.prefixed("node")
            db.execute("INSERT INTO nodes (id, org_id, name, pubkey, version, os, arch, capabilities, max_warm_lanes, "
                       "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (node_id, jt["org_id"], name, pubkey, version, os_name, arch, json.dumps(capabilities),
                        max_warm_lanes, now))
            db.execute("UPDATE join_tokens SET used_at = ?, node_id = ? WHERE id = ?", (now, node_id, jt["id"]))
        node = self.node(node_id)
        assert node is not None
        return node

    # ---- nodes ----
    def _node(self, row: dict | None) -> dict | None:
        if row is None:
            return None
        row["capabilities"] = json.loads(row["capabilities"] or "[]")
        row["state"] = json.loads(row["state"]) if row.get("state") else None
        return row

    def node(self, node_id: str) -> dict | None:
        """By id alone: only for the handshake, before the org is known."""
        return self._node(self._one("SELECT * FROM nodes WHERE id = ?", (node_id,)))

    def org_node(self, org_id: str, node_id: str) -> dict | None:
        return self._node(self._one("SELECT * FROM nodes WHERE org_id = ? AND id = ? AND removed_at IS NULL",
                                    (org_id, node_id)))

    def nodes(self, org_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM nodes WHERE org_id = ? AND removed_at IS NULL ORDER BY name", (org_id,))
        return [n for n in (self._node(r) for r in rows) if n is not None]

    def remove_node(self, org_id: str, node_id: str) -> bool:
        with self.tx() as db:
            n = db.execute("UPDATE nodes SET removed_at = ? WHERE org_id = ? AND id = ? AND removed_at IS NULL",
                           (self.clock(), org_id, node_id)).rowcount
            db.execute("DELETE FROM lanes WHERE org_id = ? AND node_id = ?", (org_id, node_id))
        return n == 1

    def seen(self, node_id: str, state: dict | None = None, *, version: str | None = None) -> None:
        if state is None:
            self._exec("UPDATE nodes SET last_seen = ? WHERE id = ?", (self.clock(), node_id))
        else:
            self._exec("UPDATE nodes SET last_seen = ?, state = ?, version = COALESCE(?, version) WHERE id = ?",
                       (self.clock(), json.dumps(state), version, node_id))

    def last_seq(self, node_id: str) -> int:
        row = self._one("SELECT last_seq FROM nodes WHERE id = ?", (node_id,))
        return int(row["last_seq"]) if row else 0

    def set_last_seq(self, node_id: str, seq: int) -> None:
        self._exec("UPDATE nodes SET last_seq = ? WHERE id = ? AND last_seq < ?", (seq, node_id, seq))

    def set_seq_epoch(self, node_id: str, epoch: str) -> bool:
        """Record the node's seq epoch. True when it changed: the node's seq counter started over
        (its channel.db was lost), so what was applied under the old epoch says nothing and the
        dedupe starts again from 0."""
        return self._exec("UPDATE nodes SET seq_epoch = ?, last_seq = 0 WHERE id = ? "
                          "AND (seq_epoch IS NULL OR seq_epoch != ?)", (epoch, node_id, epoch)) == 1

    # ---- nonces ----
    def use_nonce(self, nonce: str, node_id: str) -> bool:
        """Single use: False when this nonce was already used."""
        try:
            self._exec("INSERT INTO nonces (nonce, node_id, used_at) VALUES (?, ?, ?)", (nonce, node_id, self.clock()))
        except sqlite3.IntegrityError:
            return False
        self._exec("DELETE FROM nonces WHERE used_at < ?", (self.clock() - 7 * 86400,))
        return True

    # ---- lanes (from lane events) ----
    def put_lane(self, org_id: str, node_id: str, lane: dict) -> None:
        self._exec("INSERT INTO lanes (org_id, node_id, lane_id, payload, updated_at) VALUES (?, ?, ?, ?, ?) "
                   "ON CONFLICT (org_id, node_id, lane_id) DO UPDATE SET payload = excluded.payload, "
                   "updated_at = excluded.updated_at", (org_id, node_id, lane["id"], json.dumps(lane), self.clock()))

    def lanes(self, org_id: str, node_id: str | None = None) -> list[dict]:
        if node_id is None:
            rows = self._all("SELECT node_id, payload FROM lanes WHERE org_id = ? ORDER BY lane_id", (org_id,))
        else:
            rows = self._all("SELECT node_id, payload FROM lanes WHERE org_id = ? AND node_id = ? ORDER BY lane_id",
                             (org_id, node_id))
        return [{**json.loads(r["payload"]), "node_id": r["node_id"]} for r in rows]

    # ---- grants ----
    def add_grant(self, row: dict) -> None:
        self._exec("INSERT INTO grants (id, org_id, rid, parent_id, subject, block, token_hash, note, created_at, "
                   "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (row["id"], row["org_id"], row["rid"], row.get("parent_id"), json.dumps(row["subject"]),
                    json.dumps(row["block"]), row.get("token_hash"), row.get("note"), row["created_at"],
                    row.get("expires_at")))

    def _grant(self, row: dict | None) -> dict | None:
        if row is None:
            return None
        row["subject"] = json.loads(row["subject"])
        row["block"] = json.loads(row["block"])
        return row

    def grant_by_token(self, token: str) -> dict | None:
        return self._grant(self._one("SELECT * FROM grants WHERE token_hash = ?", (token_hash(token),)))

    def grant(self, org_id: str, grant_id: str) -> dict | None:
        return self._grant(self._one("SELECT * FROM grants WHERE org_id = ? AND id = ?", (org_id, grant_id)))

    def grants(self, org_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM grants WHERE org_id = ? ORDER BY created_at DESC", (org_id,))
        return [g for g in (self._grant(r) for r in rows) if g is not None]

    def set_grant_token(self, org_id: str, grant_id: str, token: str) -> None:
        self._exec("UPDATE grants SET token_hash = ? WHERE org_id = ? AND id = ?", (token_hash(token), org_id, grant_id))

    def revoke_grant(self, org_id: str, grant_id: str) -> list[dict]:
        """Revoke a grant and everything issued under it. Returns the rows revoked now."""
        now = self.clock()
        revoked: list[dict] = []
        with self.tx() as db:
            frontier = [grant_id]
            while frontier:
                marks = ",".join("?" * len(frontier))
                rows = db.execute(f"SELECT id, rid FROM grants WHERE org_id = ? AND id IN ({marks}) "
                                  "AND revoked_at IS NULL", (org_id, *frontier)).fetchall()
                revoked += [dict(r) for r in rows]
                db.execute(f"UPDATE grants SET revoked_at = ? WHERE org_id = ? AND id IN ({marks}) "
                           "AND revoked_at IS NULL", (now, org_id, *frontier))
                frontier = [r["id"] for r in db.execute(
                    f"SELECT id FROM grants WHERE org_id = ? AND parent_id IN ({marks})", (org_id, *frontier))]
        return revoked

    def revoked_ids(self, org_id: str) -> list[str]:
        rows = self._all("SELECT id, rid FROM grants WHERE org_id = ? AND revoked_at IS NOT NULL", (org_id,))
        return [value for r in rows for value in (r["rid"], r["id"])]

    # ---- mesh freeze (agentos.freeze): one per org, sent to every node, pending for offline ones ----
    def set_mesh_freeze(self, org_id: str, *, freeze_id: str, reason: str, by: str, revoked: list[str]) -> dict:
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO mesh_freeze (org_id, freeze_id, reason, by, at, revoked) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (org_id, freeze_id, reason, by, self.clock(), json.dumps(revoked)))
        return self.mesh_freeze(org_id) or {}

    def mesh_freeze(self, org_id: str) -> dict | None:
        row = self._one("SELECT * FROM mesh_freeze WHERE org_id = ?", (org_id,))
        if row is not None:
            row["revoked"] = json.loads(row["revoked"] or "[]")
        return row

    def clear_mesh_freeze(self, org_id: str) -> bool:
        with self.tx() as db:
            db.execute("DELETE FROM mesh_freeze_nodes WHERE org_id = ?", (org_id,))
            return db.execute("DELETE FROM mesh_freeze WHERE org_id = ?", (org_id,)).rowcount > 0

    def mesh_freeze_applied(self, org_id: str, node_id: str, freeze_id: str) -> None:
        self._exec("INSERT OR REPLACE INTO mesh_freeze_nodes (org_id, node_id, freeze_id, applied_at) "
                   "VALUES (?, ?, ?, ?)", (org_id, node_id, freeze_id, self.clock()))

    def mesh_freeze_nodes(self, org_id: str, freeze_id: str) -> dict[str, float]:
        rows = self._all("SELECT node_id, applied_at FROM mesh_freeze_nodes WHERE org_id = ? AND freeze_id = ?",
                         (org_id, freeze_id))
        return {r["node_id"]: r["applied_at"] for r in rows}

    # ---- standing approvals (what each node should hold; agentos.controller.hub push_standing) ----
    def standing(self, org_id: str, node_id: str) -> list[dict]:
        rows = self._all("SELECT item FROM standing WHERE org_id = ? AND node_id = ? ORDER BY id", (org_id, node_id))
        return [json.loads(r["item"]) for r in rows]

    def put_standing(self, org_id: str, node_id: str, items: list[dict], *, whole: bool = False) -> None:
        """Add or replace items by id; `whole` replaces the node's set."""
        with self.tx() as db:
            if whole:
                db.execute("DELETE FROM standing WHERE org_id = ? AND node_id = ?", (org_id, node_id))
            for item in items:
                db.execute("INSERT OR REPLACE INTO standing (org_id, node_id, id, item, updated_at) "
                           "VALUES (?, ?, ?, ?, ?)", (org_id, node_id, item["id"], json.dumps(item), self.clock()))

    def drop_standing(self, org_id: str, node_id: str, ids: list[str]) -> None:
        with self.tx() as db:
            for standing_id in ids:
                db.execute("DELETE FROM standing WHERE org_id = ? AND node_id = ? AND id = ?",
                           (org_id, node_id, standing_id))

    def standing_used(self, org_id: str, node_id: str, standing_id: str, used: int) -> None:
        """The node's count, as it reported it (it only grows)."""
        with self.tx() as db:
            row = db.execute("SELECT item FROM standing WHERE org_id = ? AND node_id = ? AND id = ?",
                             (org_id, node_id, standing_id)).fetchone()
            if row is None:
                return
            item = json.loads(row["item"])
            if used > (item.get("used") or 0):
                db.execute("UPDATE standing SET item = ? WHERE org_id = ? AND node_id = ? AND id = ?",
                           (json.dumps({**item, "used": used}), org_id, node_id, standing_id))

    def standing_revokes(self, org_id: str, node_id: str) -> list[str]:
        """Revokes the node has not confirmed yet (it was offline, or did not answer)."""
        rows = self._all("SELECT id FROM standing_revokes WHERE org_id = ? AND node_id = ? ORDER BY at, id",
                         (org_id, node_id))
        return [r["id"] for r in rows]

    def add_standing_revokes(self, org_id: str, node_id: str, ids: list[str]) -> None:
        with self.tx() as db:
            for standing_id in ids:
                db.execute("DELETE FROM standing WHERE org_id = ? AND node_id = ? AND id = ?",
                           (org_id, node_id, standing_id))
                db.execute("INSERT OR IGNORE INTO standing_revokes (org_id, node_id, id, at) VALUES (?, ?, ?, ?)",
                           (org_id, node_id, standing_id, self.clock()))

    def clear_standing_revokes(self, org_id: str, node_id: str, ids: list[str]) -> None:
        with self.tx() as db:
            for standing_id in ids:
                db.execute("DELETE FROM standing_revokes WHERE org_id = ? AND node_id = ? AND id = ?",
                           (org_id, node_id, standing_id))

    # ---- audit anchors ----
    def put_anchor(self, org_id: str, node_id: str, anchor: dict, received_at: str, sig_for) -> tuple[dict, str]:
        """Keep one head; never overwrite it. Returns (the stored receipt, "new" | "same" |
        "conflict"). `sig_for(anchor, received_at)` signs a new receipt. A head that differs from
        the one already held is kept aside as evidence and answered with the stored receipt."""
        key = (org_id, node_id, anchor["chain"], anchor["n"])
        with self.tx() as db:
            row = db.execute("SELECT * FROM audit_anchors WHERE org_id = ? AND node_id = ? AND chain = ? AND n = ?",
                             key).fetchone()
            if row is None:
                sig = sig_for(anchor, received_at)
                db.execute("INSERT INTO audit_anchors (org_id, node_id, chain, n, seq, hash, kid, at, received_at, sig, "
                           "kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (*key, anchor["seq"], anchor["hash"], anchor.get("kid"), anchor.get("at"), received_at, sig,
                            anchor.get("kind")))
                return {"anchor": anchor, "received_at": received_at, "sig": sig}, "new"
            stored = _anchor_receipt(dict(row))
            same = all(stored["anchor"].get(k) == anchor.get(k) for k in ("seq", "hash", "kid", "at", "kind"))
            if not same:
                db.execute("INSERT INTO audit_anchor_conflicts (org_id, node_id, chain, n, seq, hash, kid, at, "
                           "received_at, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (*key, anchor["seq"], anchor["hash"], anchor.get("kid"), anchor.get("at"), received_at,
                            anchor.get("kind")))
            return stored, "same" if same else "conflict"

    def anchors(self, org_id: str, node_id: str, *, limit: int = 5000) -> list[dict]:
        """The newest `limit` heads a node anchored here, oldest first, each as its signed receipt."""
        rows = self._all("SELECT * FROM (SELECT * FROM audit_anchors WHERE org_id = ? AND node_id = ? "
                         "ORDER BY received_at DESC, n DESC LIMIT ?) ORDER BY chain, n", (org_id, node_id, limit))
        return [_anchor_receipt(r) for r in rows]

    def anchors_page(self, org_id: str, node_id: str, *, after: tuple[str, int] | None, limit: int
                     ) -> tuple[list[dict], tuple[str, int] | None]:
        """One page of a node's heads in (chain, mark) order, after the (chain, mark) `after`.
        Returns (receipts, the cursor for the next page, or None when this page is the last)."""
        chain, n = after if after is not None else ("", 0)
        rows = self._all("SELECT * FROM audit_anchors WHERE org_id = ? AND node_id = ? AND (chain > ? OR "
                         "(chain = ? AND n > ?)) ORDER BY chain, n LIMIT ?",
                         (org_id, node_id, chain, chain, n, limit + 1))
        more = len(rows) > limit
        rows = rows[:limit]
        last = rows[-1] if rows and more else None
        return [_anchor_receipt(r) for r in rows], ((last["chain"], int(last["n"])) if last else None)

    def anchor_conflicts(self, org_id: str, node_id: str) -> list[dict]:
        return self._all("SELECT chain, n, seq, hash, kid, at, kind, received_at FROM audit_anchor_conflicts "
                         "WHERE org_id = ? AND node_id = ? ORDER BY received_at", (org_id, node_id))

    # ---- approvals ----
    def put_approval(self, org_id: str, node_id: str, approval: dict) -> bool:
        """From approval_request. False when it was already known (a redelivery)."""
        now = self.clock()
        cur = self._exec(
            "INSERT OR IGNORE INTO approvals (org_id, node_id, id, state, content_hash, payload, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (org_id, node_id, approval["id"], approval.get("state") or "pending", approval.get("content_hash"),
             json.dumps(approval), now, now))
        return cur == 1

    def set_approval_state(self, org_id: str, node_id: str, approval_id: str, state: str,
                           decided_by: str | None = None) -> bool:
        return self._exec("UPDATE approvals SET state = ?, decided_by = COALESCE(?, decided_by), updated_at = ? "
                          "WHERE org_id = ? AND node_id = ? AND id = ?",
                          (state, decided_by, self.clock(), org_id, node_id, approval_id)) == 1

    def approval(self, org_id: str, node_id: str, approval_id: str) -> dict | None:
        row = self._one("SELECT * FROM approvals WHERE org_id = ? AND node_id = ? AND id = ?",
                        (org_id, node_id, approval_id))
        return self._approval(row)

    def approval_node(self, org_id: str, approval_id: str) -> str | None:
        row = self._one("SELECT node_id FROM approvals WHERE org_id = ? AND id = ?", (org_id, approval_id))
        return row["node_id"] if row else None

    def _approval(self, row: dict | None) -> dict | None:
        if row is None:
            return None
        payload = json.loads(row["payload"])
        return {**payload, "id": row["id"], "state": row["state"], "node_id": row["node_id"],
                "decided_by": row["decided_by"] or payload.get("decided_by")}

    def approvals(self, org_id: str, state: str | None = None, limit: int = 200) -> list[dict]:
        if state:
            rows = self._all("SELECT * FROM approvals WHERE org_id = ? AND state = ? ORDER BY created_at DESC LIMIT ?",
                             (org_id, state, limit))
        else:
            rows = self._all("SELECT * FROM approvals WHERE org_id = ? ORDER BY created_at DESC LIMIT ?",
                             (org_id, limit))
        return [a for a in (self._approval(r) for r in rows) if a is not None]

    # ---- traces and events ----
    def add_traces(self, org_id: str, node_id: str, events: list[dict]) -> None:
        with self.tx() as db:
            db.executemany("INSERT INTO traces (org_id, node_id, payload) VALUES (?, ?, ?)",
                           [(org_id, node_id, json.dumps(e)) for e in events])
            db.execute("DELETE FROM traces WHERE seq <= (SELECT MAX(seq) FROM traces) - ?", (MAX_TRACES,))

    def traces(self, org_id: str, limit: int = 100, node_id: str | None = None) -> list[dict]:
        if node_id:
            rows = self._all("SELECT node_id, payload FROM traces WHERE org_id = ? AND node_id = ? "
                             "ORDER BY seq DESC LIMIT ?", (org_id, node_id, limit))
        else:
            rows = self._all("SELECT node_id, payload FROM traces WHERE org_id = ? ORDER BY seq DESC LIMIT ?",
                             (org_id, limit))
        return [{**json.loads(r["payload"]), "node_id": r["node_id"]} for r in rows]

    def add_event(self, org_id: str, node_id: str, kind: str, payload: Any) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO events (org_id, node_id, kind, payload, at) VALUES (?, ?, ?, ?, ?)",
                       (org_id, node_id, kind, json.dumps(payload), self.clock()))
            db.execute("DELETE FROM events WHERE seq <= (SELECT MAX(seq) FROM events) - ?", (MAX_EVENTS,))

    def events(self, org_id: str, limit: int = 100) -> list[dict]:
        rows = self._all("SELECT node_id, kind, payload, at FROM events WHERE org_id = ? ORDER BY seq DESC LIMIT ?",
                         (org_id, limit))
        return [{"node_id": r["node_id"], "kind": r["kind"], "payload": json.loads(r["payload"]), "at": r["at"]}
                for r in rows]
