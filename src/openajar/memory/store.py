"""SQLite storage for workspace memory: one file per workspace (4.1, 4.15).

`$OPENAJAR_HOME/memory/<workspace>.db`, WAL, `synchronous=FULL`, mode 600 in a 700 directory.
A separate file per workspace means no query can join, rank or count across workspaces, and
FTS statistics (term frequencies) belong to one workspace only.

- `entries` is the mutable index of what is current (state, trust, labels, title, filters).
- `versions` is append-only: a trigger aborts every UPDATE and DELETE. Each version holds the
  hashes of its title, body and attributes, its provenance, and `version_hash`, chained to the
  workspace's previous version through `prev_hash` (ordered by `seq`).
- `blobs` holds the content, keyed by `sha256(nonce || text)`. The nonce lives in the blob row,
  so once a blob is deleted (forgetting) its hash no longer confirms a guess of the content.
- `fts` (FTS5) indexes the current version of each entry that is not forgotten.

Forgetting deletes the blobs, writes a tombstone and removes the FTS row; version rows stay, so
`verify` still checks the whole chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openajar.core.errors import ServiceUnavailable
from openajar.memory.errors import MemoryConflict
from openajar.memory.models import WORKSPACE_RE, check_workspace

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
  id TEXT PRIMARY KEY,
  key TEXT UNIQUE,
  type TEXT NOT NULL,
  audience TEXT NOT NULL DEFAULT 'workspace',
  state TEXT NOT NULL,
  current INTEGER NOT NULL,
  trust TEXT NOT NULL,
  labels TEXT NOT NULL,
  title TEXT,
  subject TEXT,
  writer TEXT,
  lane TEXT,
  pinned INTEGER NOT NULL DEFAULT 0,
  suspicious INTEGER NOT NULL DEFAULT 0,
  flagged TEXT,
  valid_from REAL, valid_to REAL,
  reviewed_at REAL, expires_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS entries_type ON entries(type, state);
CREATE INDEX IF NOT EXISTS entries_updated ON entries(updated_at);
CREATE INDEX IF NOT EXISTS entries_subject ON entries(subject);
CREATE TABLE IF NOT EXISTS versions (
  seq INTEGER NOT NULL UNIQUE,
  entry_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  at REAL NOT NULL,
  type TEXT NOT NULL,
  audience TEXT NOT NULL,
  trust TEXT NOT NULL,
  labels TEXT NOT NULL,
  title_hash TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  attrs_hash TEXT,
  valid_from REAL, valid_to REAL,
  supersedes INTEGER,
  prov TEXT NOT NULL,
  version_hash TEXT NOT NULL,
  prev_hash TEXT,
  PRIMARY KEY (entry_id, version)
);
CREATE TRIGGER IF NOT EXISTS versions_no_update BEFORE UPDATE ON versions
BEGIN SELECT RAISE(ABORT, 'memory versions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS versions_no_delete BEFORE DELETE ON versions
BEGIN SELECT RAISE(ABORT, 'memory versions are append-only'); END;
CREATE TABLE IF NOT EXISTS blobs (hash TEXT PRIMARY KEY, nonce TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tombstones (entry_id TEXT PRIMARY KEY, at REAL NOT NULL, by TEXT NOT NULL,
  reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS links (src TEXT NOT NULL, kind TEXT NOT NULL, dst TEXT NOT NULL,
  PRIMARY KEY (src, kind, dst));
CREATE INDEX IF NOT EXISTS links_dst ON links(dst, kind);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(entry_id UNINDEXED, title, body, attrs,
  tokenize = 'porter unicode61 remove_diacritics 2');
"""
TRIGGERS = ("versions_no_update", "versions_no_delete")
DB_SUFFIX = ".db"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def blob_hash(nonce: str, data: str) -> str:
    return "sha256:" + hashlib.sha256(bytes.fromhex(nonce) + data.encode()).hexdigest()


def version_record(row: dict[str, Any]) -> dict[str, Any]:
    """What `version_hash` covers: everything in the version row except the hash itself."""
    return {
        "seq": row["seq"], "entry_id": row["entry_id"], "version": row["version"], "at": row["at"],
        "type": row["type"], "audience": row["audience"], "trust": row["trust"],
        "labels": json.loads(row["labels"]) if isinstance(row["labels"], str) else row["labels"],
        "title_hash": row["title_hash"], "body_hash": row["body_hash"], "attrs_hash": row["attrs_hash"],
        "valid_from": row["valid_from"], "valid_to": row["valid_to"], "supersedes": row["supersedes"],
        "prov": json.loads(row["prov"]) if isinstance(row["prov"], str) else row["prov"],
        "prev_hash": row["prev_hash"],
    }


def version_hash(row: dict[str, Any]) -> str:
    return sha256(canonical(version_record(row)))


@dataclass
class NewVersion:
    """A version the service has fully decided; the store adds seq, prev_hash and the hash."""
    entry_id: str
    version: int
    at: float
    type: str
    audience: str
    trust: str
    labels: list[str]
    title: str
    body: str
    attrs: dict[str, Any] | None
    valid_from: float | None
    valid_to: float | None
    supersedes: int | None
    prov: dict[str, Any]
    # the entries row after this version
    key: str | None
    subject: str | None
    writer: str
    lane: str | None
    pinned: bool
    suspicious: bool
    expires_at: float | None
    links: Sequence[tuple[str, str]] = ()      # (kind, dst)
    fts_attrs: str = ""


class WorkspaceDB:
    """One workspace's memory file."""

    def __init__(self, path: Path, workspace: str):
        self.path = path
        self.workspace = workspace
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if not path.exists():
                os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
            self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        except (sqlite3.DatabaseError, OSError) as exc:
            raise self._unreadable(exc) from None
        self._db.row_factory = sqlite3.Row
        with self._guard():
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.execute("PRAGMA secure_delete=ON")    # deleted content is overwritten, not left in free pages
            if self._db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("the integrity check failed")
            self._check_version()
            self._db.executescript(SCHEMA)
            self._stamp()
        try:
            path.chmod(0o600)
        except OSError:
            pass

    # ---- plumbing ----------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _guard(self) -> Iterator[None]:
        try:
            yield
        except sqlite3.IntegrityError:
            raise
        except sqlite3.DatabaseError as exc:
            raise self._unreadable(exc) from None

    def _unreadable(self, exc: Exception) -> ServiceUnavailable:
        return ServiceUnavailable(
            f"the memory of {self.workspace} ({self.path.name}) could not be read or written ({exc}). "
            "Nothing was saved. If the file is damaged, restore it from a backup. Other workspaces are "
            "not affected.")

    def _check_version(self) -> None:
        has = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'").fetchone()
        if not has:
            return
        row = self._db.execute("SELECT version FROM schema_version").fetchone()
        if row is not None and row["version"] > SCHEMA_VERSION:
            raise ServiceUnavailable(
                f"the memory of {self.workspace} was written by a newer openajar (schema {row['version']}, "
                f"this build reads {SCHEMA_VERSION}). Upgrade openajar, or restore an older copy of the file.")

    def _stamp(self) -> None:
        if self._db.execute("SELECT version FROM schema_version").fetchone() is None:
            self._db.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        self._db.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('workspace', ?)", (self.workspace,))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._guard():
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _all(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        with self._lock, self._guard():
            return [dict(r) for r in self._db.execute(sql, params)]

    def _one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> dict[str, Any] | None:
        with self._lock, self._guard():
            row = self._db.execute(sql, params).fetchone()
            return dict(row) if row else None

    # ---- reads -------------------------------------------------------------------

    def entry(self, entry_id: str) -> dict[str, Any] | None:
        return self._one("SELECT rowid AS rid, * FROM entries WHERE id = ?", (entry_id,))

    def entry_by_key(self, key: str) -> dict[str, Any] | None:
        return self._one("SELECT rowid AS rid, * FROM entries WHERE key = ?", (key,))

    def entries(self, where: str = "1", params: Sequence[Any] | dict[str, Any] = (), *,
                order: str = "updated_at DESC", limit: int = 1000) -> list[dict[str, Any]]:
        return self._all(f"SELECT rowid AS rid, * FROM entries WHERE {where} ORDER BY {order} LIMIT {int(limit)}",
                         params)

    def version(self, entry_id: str, version: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM versions WHERE entry_id = ? AND version = ?", (entry_id, version))

    def versions(self, entry_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM versions WHERE entry_id = ? ORDER BY version", (entry_id,))

    def blob(self, h: str | None) -> str | None:
        if h is None:
            return None
        row = self._one("SELECT data FROM blobs WHERE hash = ?", (h,))
        return row["data"] if row else None

    def tombstone(self, entry_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM tombstones WHERE entry_id = ?", (entry_id,))

    def links_from(self, entry_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT kind, dst FROM links WHERE src = ? ORDER BY kind, dst", (entry_id,))

    def links_to(self, entry_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT kind, src FROM links WHERE dst = ? ORDER BY kind, src", (entry_id,))

    def live_count(self) -> int:
        row = self._one("SELECT count(*) AS n FROM entries WHERE state = 'live'")
        return int(row["n"]) if row else 0

    def content_bytes(self) -> int:
        row = self._one("SELECT COALESCE(SUM(length(CAST(data AS BLOB))), 0) AS n FROM blobs")
        return int(row["n"]) if row else 0

    def search(self, match: str, where: str, params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        """FTS5 hits joined with their entries, best bm25 first (title weighs most)."""
        sql = ("SELECT e.rowid AS rid, e.*, bm25(fts, 0.0, 5.0, 1.0, 0.5) AS score, "
               "snippet(fts, 2, '[', ']', ' ... ', 16) AS snip "
               "FROM fts JOIN entries e ON e.rowid = fts.rowid "
               f"WHERE fts MATCH :match AND {where} ORDER BY score LIMIT {int(limit)}")
        try:
            return self._all(sql, {**params, "match": match})
        except sqlite3.OperationalError:
            return []

    # ---- writes ------------------------------------------------------------------

    def _put_blob(self, db: sqlite3.Connection, data: str) -> str:
        nonce = secrets.token_hex(16)
        h = blob_hash(nonce, data)
        db.execute("INSERT INTO blobs (hash, nonce, data) VALUES (?, ?, ?)", (h, nonce, data))
        return h

    def append(self, nv: NewVersion, *, expect_current: int | None) -> dict[str, Any]:
        """Store one version and move the entry to it, in one transaction. `expect_current` is
        the compare-and-set: None for a new entry (the id and key must be free), N to supersede
        version N. Returns the stored version row."""
        with self.transaction() as db:
            existing = db.execute("SELECT rowid AS rid, * FROM entries WHERE id = ?", (nv.entry_id,)).fetchone()
            if expect_current is None:
                if existing is not None:
                    raise MemoryConflict("the entry id is taken")
                if nv.key is not None:
                    taken = db.execute("SELECT id, current, trust FROM entries WHERE key = ?", (nv.key,)).fetchone()
                    if taken is not None:
                        raise MemoryConflict(f"the key {nv.key} is taken; pass supersedes to write a new version",
                                             {"id": taken["id"], "version": taken["current"],
                                              "trust": taken["trust"]})
            else:
                if existing is None or existing["state"] == "forgotten":
                    raise MemoryConflict("the entry no longer exists")
                if existing["current"] != expect_current:
                    raise MemoryConflict(f"version {expect_current} is no longer current",
                                         {"id": nv.entry_id, "version": existing["current"],
                                          "trust": existing["trust"]})
            head = db.execute("SELECT seq, version_hash FROM versions ORDER BY seq DESC LIMIT 1").fetchone()
            seq = (head["seq"] + 1) if head else 1
            row: dict[str, Any] = {
                "seq": seq, "entry_id": nv.entry_id, "version": nv.version, "at": nv.at, "type": nv.type,
                "audience": nv.audience, "trust": nv.trust, "labels": canonical(sorted(nv.labels)),
                "title_hash": self._put_blob(db, nv.title), "body_hash": self._put_blob(db, nv.body),
                "attrs_hash": self._put_blob(db, canonical(nv.attrs)) if nv.attrs else None,
                "valid_from": nv.valid_from, "valid_to": nv.valid_to, "supersedes": nv.supersedes,
                "prov": canonical(nv.prov), "prev_hash": head["version_hash"] if head else None,
            }
            row["version_hash"] = version_hash(row)
            db.execute("INSERT INTO versions (seq, entry_id, version, at, type, audience, trust, labels, "
                       "title_hash, body_hash, attrs_hash, valid_from, valid_to, supersedes, prov, version_hash, "
                       "prev_hash) VALUES (:seq, :entry_id, :version, :at, :type, :audience, :trust, :labels, "
                       ":title_hash, :body_hash, :attrs_hash, :valid_from, :valid_to, :supersedes, :prov, "
                       ":version_hash, :prev_hash)", row)
            fields = {
                "id": nv.entry_id, "key": nv.key, "type": nv.type, "audience": nv.audience, "state": "live",
                "current": nv.version, "trust": nv.trust, "labels": row["labels"], "title": nv.title,
                "subject": nv.subject, "writer": nv.writer, "lane": nv.lane, "pinned": int(nv.pinned),
                "suspicious": int(nv.suspicious), "valid_from": nv.valid_from, "valid_to": nv.valid_to,
                "expires_at": nv.expires_at, "at": nv.at,
            }
            if existing is None:
                db.execute("INSERT INTO entries (id, key, type, audience, state, current, trust, labels, title, "
                           "subject, writer, lane, pinned, suspicious, valid_from, valid_to, expires_at, created_at, "
                           "updated_at) VALUES (:id, :key, :type, :audience, :state, :current, :trust, :labels, "
                           ":title, :subject, :writer, :lane, :pinned, :suspicious, :valid_from, :valid_to, "
                           ":expires_at, :at, :at)", fields)
                rid = db.execute("SELECT rowid FROM entries WHERE id = ?", (nv.entry_id,)).fetchone()[0]
            else:
                db.execute("UPDATE entries SET key = COALESCE(:key, key), audience = :audience, state = :state, "
                           "current = :current, trust = :trust, labels = :labels, title = :title, "
                           "subject = :subject, writer = :writer, lane = :lane, pinned = :pinned, "
                           "suspicious = :suspicious, valid_from = :valid_from, valid_to = :valid_to, "
                           "expires_at = :expires_at, reviewed_at = NULL, updated_at = :at WHERE id = :id", fields)
                rid = existing["rid"]
                db.execute("DELETE FROM fts WHERE rowid = ?", (rid,))
            db.execute("INSERT INTO fts (rowid, entry_id, title, body, attrs) VALUES (?, ?, ?, ?, ?)",
                       (rid, nv.entry_id, nv.title, nv.body, nv.fts_attrs))
            for kind, dst in nv.links:
                db.execute("INSERT OR IGNORE INTO links (src, kind, dst) VALUES (?, ?, ?)", (nv.entry_id, kind, dst))
            return row

    def set_fields(self, entry_id: str, **fields: Any) -> None:
        allowed = {"state", "reviewed_at", "flagged", "suspicious"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"not settable: {sorted(set(fields) - allowed)}")
        assignments = ", ".join(f"{k} = :{k}" for k in fields)
        with self._lock, self._guard():
            self._db.execute(f"UPDATE entries SET {assignments} WHERE id = :id", {**fields, "id": entry_id})

    def forget(self, entry_id: str, *, at: float, by: str, reason: str) -> int:
        """Erase the content of every version of the entry. Returns how many blobs went."""
        with self.transaction() as db:
            entry = db.execute("SELECT rowid AS rid, * FROM entries WHERE id = ?", (entry_id,)).fetchone()
            if entry is None:
                return 0
            hashes: set[str] = set()
            for v in db.execute("SELECT title_hash, body_hash, attrs_hash FROM versions WHERE entry_id = ?",
                                (entry_id,)):
                hashes.update(h for h in (v["title_hash"], v["body_hash"], v["attrs_hash"]) if h)
            gone = 0
            for h in hashes:
                gone += db.execute("DELETE FROM blobs WHERE hash = ?", (h,)).rowcount
            db.execute("INSERT OR REPLACE INTO tombstones (entry_id, at, by, reason) VALUES (?, ?, ?, ?)",
                       (entry_id, at, by, reason))
            db.execute("UPDATE entries SET state = 'forgotten', key = NULL, title = NULL, subject = NULL, "
                       "pinned = 0, suspicious = 0, flagged = NULL, expires_at = NULL, updated_at = ? WHERE id = ?",
                       (at, entry_id))
            db.execute("DELETE FROM fts WHERE rowid = ?", (entry["rid"],))
            db.execute("INSERT INTO fts (fts) VALUES ('optimize')")   # drop index segments that still hold its terms
        self._flush_wal()
        return gone

    def _flush_wal(self) -> None:
        """Copy the WAL into the file and truncate it, so old frames with erased content go."""
        with self._lock, self._guard():
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ---- export and import (4.13) ------------------------------------------------

    TABLES = ("entries", "versions", "blobs", "tombstones", "links")

    def dump(self) -> dict[str, list[dict[str, Any]]]:
        """Every row of the workspace's tables, in a stable order (versions by seq). Forgotten
        content is not there to dump: its blobs were deleted."""
        order = {"entries": "created_at, id", "versions": "seq", "blobs": "hash", "tombstones": "entry_id",
                 "links": "src, kind, dst"}
        return {t: self._all(f"SELECT * FROM {t} ORDER BY {order[t]}") for t in self.TABLES}

    def is_empty(self) -> bool:
        return self._one("SELECT 1 AS x FROM versions LIMIT 1") is None and \
            self._one("SELECT 1 AS x FROM entries LIMIT 1") is None

    def load(self, rows: dict[str, list[dict[str, Any]]], fts: Sequence[tuple[str, str, str, str]]) -> None:
        """Insert dumped rows into this empty file in one transaction, then index `fts`
        (entry_id, title, body, attrs text) for the entries that are not forgotten."""
        cols = {
            "entries": ("id", "key", "type", "audience", "state", "current", "trust", "labels", "title", "subject",
                        "writer", "lane", "pinned", "suspicious", "flagged", "valid_from", "valid_to", "reviewed_at",
                        "expires_at", "created_at", "updated_at"),
            "versions": ("seq", "entry_id", "version", "at", "type", "audience", "trust", "labels", "title_hash",
                         "body_hash", "attrs_hash", "valid_from", "valid_to", "supersedes", "prov", "version_hash",
                         "prev_hash"),
            "blobs": ("hash", "nonce", "data"),
            "tombstones": ("entry_id", "at", "by", "reason"),
            "links": ("src", "kind", "dst"),
        }
        with self.transaction() as db:
            if not self.is_empty():
                raise MemoryConflict(f"the memory of {self.workspace} is not empty; an import never merges")
            for table in self.TABLES:
                names = cols[table]
                sql = (f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(':' + n for n in names)})")
                for row in rows.get(table) or []:
                    db.execute(sql, {n: row.get(n) for n in names})
            for entry_id, title, body, attrs in fts:
                rid = db.execute("SELECT rowid FROM entries WHERE id = ?", (entry_id,)).fetchone()[0]
                db.execute("INSERT INTO fts (rowid, entry_id, title, body, attrs) VALUES (?, ?, ?, ?, ?)",
                           (rid, entry_id, title, body, attrs))

    def meta(self, k: str) -> str | None:
        row = self._one("SELECT v FROM meta WHERE k = ?", (k,))
        return row["v"] if row else None

    def set_meta(self, k: str, v: str) -> None:
        with self._lock, self._guard():
            self._db.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                             (k, v))

    # ---- verify ------------------------------------------------------------------

    def verify(self) -> dict[str, Any]:
        """Recompute every version hash, follow the chain, and check each blob still present
        against its hash. Forgotten content is expected to be missing; nothing else is."""
        forgotten = {r["entry_id"] for r in self._all("SELECT entry_id FROM tombstones")}
        triggers = {r["name"] for r in self._all("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        problems = [f"the append-only trigger {t} is missing" for t in TRIGGERS if t not in triggers]
        rows = self._all("SELECT * FROM versions ORDER BY seq")
        check = verify_rows(rows, self._all("SELECT hash, nonce, data FROM blobs"), forgotten)
        problems += index_problems(self._all("SELECT id, type, audience, trust, labels, current FROM entries"), rows)
        return {"workspace": self.workspace, "ok": not (problems or check["problems"]), "versions": len(rows),
                "forgotten": len(forgotten), "head": check["head"], "problems": problems + check["problems"]}


INDEXED = ("type", "audience", "trust", "labels")   # what the entries index copies from its current version


def index_problems(entries: Sequence[dict[str, Any]], versions: Sequence[dict[str, Any]]) -> list[str]:
    """The entries index is what reads rank and filter by, and no trigger guards it: each entry
    must point at a version in the chain and carry that version's type, audience, trust and
    labels, so an edit on disk (trust raised to owner) cannot pass verify (QA-memory-1)."""
    problems: list[str] = []
    by_version = {(v["entry_id"], v["version"]): v for v in versions}
    for e in entries:
        v = by_version.get((e["id"], e["current"]))
        if v is None:
            problems.append(f"{e['id']}: the index points at version {e['current']}, which the chain does not hold")
            continue
        for col in INDEXED:
            if e[col] != v[col]:
                problems.append(f"{e['id']}: {col} in the index differs from version {e['current']} in the chain")
    indexed = {e["id"] for e in entries}
    problems += [f"{eid}: in the chain but missing from the index"
                 for eid in sorted({v["entry_id"] for v in versions} - indexed)]
    return problems


def verify_rows(versions: Sequence[dict[str, Any]], blobs: Sequence[dict[str, Any]],
                forgotten: set[str]) -> dict[str, Any]:
    """The chain check over version and blob rows (from a file, or from an export before it is
    imported): `{head, problems}`."""
    problems: list[str] = []
    by_hash = {b["hash"]: b for b in blobs}
    prev: str | None = None
    expected_seq = 1
    for row in sorted(versions, key=lambda r: r["seq"]):
        ref = f"{row['entry_id']}@{row['version']}"
        if row["seq"] != expected_seq:
            problems.append(f"{ref}: sequence {row['seq']} where {expected_seq} was expected (a version is missing)")
        expected_seq = row["seq"] + 1
        if row["prev_hash"] != prev:
            problems.append(f"{ref}: prev_hash does not match the previous version")
        if version_hash(row) != row["version_hash"]:
            problems.append(f"{ref}: version_hash does not match its contents")
        for col in ("title_hash", "body_hash", "attrs_hash"):
            h = row[col]
            if not h:
                continue
            blob = by_hash.get(h)
            if blob is None:
                if row["entry_id"] not in forgotten:
                    problems.append(f"{ref}: {col.removesuffix('_hash')} content is missing")
            elif blob_hash(blob["nonce"], blob["data"]) != h:
                problems.append(f"{ref}: {col.removesuffix('_hash')} content was altered")
        prev = row["version_hash"]
    return {"head": prev, "problems": problems}


class MemoryStores:
    """Opens workspace files on demand and keeps them open. A workspace with no file has no
    memory; reads never create one."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._open: dict[str, WorkspaceDB] = {}
        self._lock = threading.Lock()

    def path(self, workspace: str) -> Path:
        return self.root / f"{check_workspace(workspace)}{DB_SUFFIX}"

    def get(self, workspace: str, *, create: bool = False) -> WorkspaceDB | None:
        check_workspace(workspace)
        with self._lock:
            db = self._open.get(workspace)
            if db is not None:
                return db
            path = self.path(workspace)
            if not path.exists() and not create:
                return None
            if not self.root.exists():
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            db = WorkspaceDB(path, workspace)
            self._open[workspace] = db
            return db

    def workspaces(self) -> list[str]:
        if not self.root.exists():
            return []
        out = []
        for p in sorted(self.root.glob(f"*{DB_SUFFIX}")):
            name = p.name.removesuffix(DB_SUFFIX)
            if WORKSPACE_RE.match(name):
                out.append(name)
        return out

    def drop(self, workspace: str) -> None:
        """Close and delete one workspace's file (an import rolled back, or the owner deleting
        the workspace with its memory)."""
        path = self.path(workspace)
        self.close(workspace)
        for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            p.unlink(missing_ok=True)

    def close(self, workspace: str | None = None) -> None:
        with self._lock:
            names = [workspace] if workspace else list(self._open)
            for name in names:
                db = self._open.pop(name, None)
                if db is not None:
                    db.close()
