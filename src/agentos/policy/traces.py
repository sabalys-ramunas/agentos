"""The action log: append-only, redacted at write time, in its own SQLite file (WAL).

Writing never fails a tool call. If the database is locked or broken, the event waits in
memory (and, past a bound, in a private spill file) and is written on the next success.
Updates are blocked by a trigger; only retention deletes old rows.

The log is tamper-evident (AUDIT-LOG.md, agentos.policy.auditlog): each row is hashed as
stored, after redaction, and linked to the previous row when it is inserted, inside the same
transaction that reads the chain head, so events waiting in memory or in the spill file get
their place in the chain only when they reach the database and two writers can never fork
it. Signed marks (genesis, checkpoints every N rows or M minutes and at shutdown, retention
anchors, key rotations) sit in `audit_marks`.

The event bus is a list of callables. The panel API subscribes to stream new events
(`trace`) and approval changes (`approval`) over SSE.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agentos.core.errors import InvalidRequest
from agentos.core.sqlite import _is_busy
from agentos.policy import auditlog
from agentos.policy.auditlog import (
    SIG_CTX,
    AuditKeys,
    Report,
    Scope,
    canonical,
    mark_hash,
    pub_text,
    row_hash,
    sign,
)
from agentos.policy.redact import redact, redact_text
from agentos.policy.store import DB
from agentos.policy.util import b64u, b64u_decode, iso, new_id
from agentos.vault.scrub import walk

log = logging.getLogger("agentos.traces")

STATUSES = ("started", "ok", "denied", "pending", "error")

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT UNIQUE NOT NULL,
  at REAL NOT NULL,
  kind TEXT NOT NULL,
  principal TEXT, principal_kind TEXT, grant_id TEXT, grant_path TEXT,
  tool TEXT, lane TEXT, workspace TEXT, effect TEXT,
  status TEXT NOT NULL,
  summary TEXT, args TEXT, error TEXT,
  cost_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
  duration_ms INTEGER, run_id TEXT, parent_id TEXT,
  prev TEXT, hash TEXT
);
CREATE INDEX IF NOT EXISTS traces_at ON traces(at);
CREATE INDEX IF NOT EXISTS traces_lane ON traces(lane, seq);
CREATE INDEX IF NOT EXISTS traces_grant ON traces(grant_id, seq);
CREATE INDEX IF NOT EXISTS traces_run ON traces(run_id, seq);
CREATE INDEX IF NOT EXISTS traces_parent ON traces(parent_id);
CREATE TRIGGER IF NOT EXISTS traces_append_only BEFORE UPDATE ON traces
BEGIN SELECT RAISE(ABORT, 'traces are append-only'); END;
CREATE TABLE IF NOT EXISTS trace_pins (
  trace_id TEXT NOT NULL, ref TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (trace_id, ref)
);
CREATE INDEX IF NOT EXISTS trace_pins_ref ON trace_pins(ref);
CREATE TABLE IF NOT EXISTS audit_marks (
  n INTEGER PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL, hash TEXT NOT NULL, sig TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_marks_no_update BEFORE UPDATE ON audit_marks
BEGIN SELECT RAISE(ABORT, 'audit marks are append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_marks_no_delete BEFORE DELETE ON audit_marks
BEGIN SELECT RAISE(ABORT, 'audit marks are append-only'); END;
"""
_INSERT = ("INSERT OR IGNORE INTO traces (seq, id, at, kind, principal, principal_kind, grant_id, grant_path, tool,"
           " lane, workspace, effect, status, summary, args, error, cost_tokens, cost_usd, duration_ms, run_id,"
           " parent_id, prev, hash) VALUES (:seq, :id, :at, :kind, :principal, :principal_kind, :grant_id,"
           " :grant_path, :tool, :lane, :workspace, :effect, :status, :summary, :args, :error, :cost_tokens,"
           " :cost_usd, :duration_ms, :run_id, :parent_id, :prev, :hash)")
# a row (not the database) is at fault: it becomes a stub instead of waiting forever. When the stub
# fails too (a closed or broken database), the error reaches the caller and the rows wait.
_ROW_ERRORS = (sqlite3.IntegrityError, sqlite3.InterfaceError, sqlite3.DataError, sqlite3.ProgrammingError,
               OverflowError, ValueError, TypeError)
CHECKPOINT_ROWS = 1000   # a signed checkpoint at least every this many rows
CHECKPOINT_S = 600       # and every this many seconds while rows keep coming
SPILL_AFTER_S = 2.0      # rows the database refuses this long go to the spill file, not just memory

MAX_PINS = 10_000   # pins per node; past it the oldest pins stop protecting their traces
# events a handler wrote (ctx.trace): their args are node-observed, unlike the agent's own
# arguments on tool_start and tool_end
_CALL_KINDS = ("tool_start", "tool_end")


class Cost(BaseModel):
    tokens: int = 0
    usd: float = 0.0


class TraceEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tr"))
    at: float | None = None  # set from the store clock at write time
    kind: str = "event"
    principal: str | None = None
    principal_kind: str | None = None
    grant_id: str | None = None
    grant_path: str | None = None
    tool: str | None = None
    lane: str | None = None
    workspace: str | None = None
    effect: str | None = None
    status: str = "ok"
    summary: str | None = None
    args: Any = None
    error: str | None = None
    cost: Cost = Field(default_factory=Cost)
    duration_ms: int | None = None
    run_id: str | None = None
    parent_id: str | None = None

    def public(self) -> dict:
        """The API shape (docs/API.md Activity)."""
        d = self.model_dump(exclude={"grant_path"})
        d["at"] = iso(self.at)
        return d


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class EventBus:
    """Callables `fn(event_type, payload)`; sync or async. A failing subscriber is logged
    and never affects the publisher."""

    def __init__(self) -> None:
        self._subs: list[Callable[[str, dict], Any]] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Callable[[str, dict], Any]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)

        return unsubscribe

    def publish(self, event_type: str, payload: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                res = fn(event_type, payload)
                if inspect.isawaitable(res):
                    try:
                        asyncio.get_running_loop().create_task(res)  # type: ignore[arg-type]
                    except RuntimeError:
                        res.close() if hasattr(res, "close") else None
            except Exception:
                log.exception("event subscriber failed")


INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1


def _text(value: Any) -> str | None:
    """A string SQLite and the canonical form can both store: each lone surrogate (U+D800 to
    U+DFFF) becomes U+FFFD (AUDIT-LOG.md "Write path")."""
    if value is None:
        return None
    value = value if isinstance(value, str) else str(value)
    try:
        value.encode()
    except UnicodeEncodeError:
        return "".join("\ufffd" if "\ud800" <= ch <= "\udfff" else ch for ch in value)
    return value


def _int(value: Any) -> int | None:
    """An integer SQLite can bind: clamped to the signed 64-bit range."""
    if value is None:
        return None
    return max(INT64_MIN, min(INT64_MAX, int(value)))


def _real(value: Any, default: float) -> float:
    """A finite REAL, with -0.0 as 0.0 (SQLite stores it so); anything else is `default`."""
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return v + 0.0 if math.isfinite(v) else default


def _encode_cursor(seq: int) -> str:
    return "c_" + b64u(str(seq).encode())


def _decode_cursor(cursor: str) -> int:
    try:
        if not cursor.startswith("c_"):
            raise ValueError
        return int(b64u_decode(cursor[2:]).decode())
    except (ValueError, UnicodeDecodeError):
        raise InvalidRequest("bad cursor") from None


class TraceStore:
    """Runtime service "traces"."""

    MAX_PENDING = 10_000

    def __init__(self, path: Path, *, bus: EventBus | None = None, busy_timeout_ms: int = 250,
                 retention_days: int = 90, scrub: Iterable[str] = (), clock: Callable[[], float] = time.time,
                 max_pins: int = MAX_PINS, secrets_dir: Path | None = None, checkpoint_rows: int = CHECKPOINT_ROWS,
                 checkpoint_s: float = CHECKPOINT_S):
        self.path = path
        self.max_pins = max_pins
        self.bus = bus or EventBus()
        self.retention_days = retention_days
        self.clock = clock
        self._scrub = [s for s in scrub if s]
        # On the node, the vault's values (agentos.vault.scrub.runtime_scrub): a trace's summary or
        # args can quote page text, a URL or an engine error.
        self.value_scrub: Callable[[str], str] | None = None
        self._pending: deque[dict] = deque()
        # rows written and not yet taken into _pending: an append never waits for the lock, so a
        # write on the event loop never waits behind a flush on the worker thread (in order)
        self._intake: deque[dict] = deque()
        self._flush_queued = False  # a flush is queued on traces.db's worker thread
        self._nowait = False  # set while the event loop flushes: a locked database defers, it does not fail
        self._lock = threading.RLock()
        self.spill_path = path.with_name(path.name + ".spill.jsonl")
        self.db = DB(path, SCHEMA, busy_timeout_ms=busy_timeout_ms)
        self.keys = AuditKeys(secrets_dir if secrets_dir is not None else path.parent / "secrets")
        self.checkpoint_rows = max(1, int(checkpoint_rows))
        self.checkpoint_s = float(checkpoint_s)
        self._genesis: dict | None = None
        # ((connection, data_version), seq, hash)
        self._head_cache: tuple[tuple[sqlite3.Connection, int], int, str] | None = None
        self._floor_n: int | None = -1
        self._floor_v: tuple[int, str | None] | None = None
        self._since_cp = 0
        self._cp_at = self.clock()
        self._spilled = any(self._restoring_files()) or self.spill_path.exists()
        self._closed = False
        # The write path's health (agentos.policy.audit_health): since when the database has refused
        # rows, and since when rows sit only in memory (neither the database nor the spill file took
        # them). None while all is well.
        self.db_failing_since: float | None = None
        self.unwritten_since: float | None = None
        self.last_write_error: str | None = None
        # Called with each new checkpoint body, after it commits. Never raises.
        self.on_checkpoint: Callable[[dict], Any] | None = None
        # Called with each new checkpoint and retention mark body, after it commits
        # (agentos.node.anchors anchors both). Never raises.
        self.on_mark: Callable[[dict], Any] | None = None
        # Lines of a spill file that broke off mid-write and were set aside (the health monitor
        # names them): {"lines", "file", "at"}. None until it happens.
        self.spill_torn: dict | None = None
        self._init_chain()

    # ---- the chain (AUDIT-LOG.md) ----
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE on the trace connection; a failed BEGIN leaves nothing to roll back."""
        conn = self.db.conn
        with self.db.lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                self._head_cache = None  # the head this transaction moved to was never committed
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    log.warning("trace rollback failed")
                raise
            try:
                conn.execute("COMMIT")
            except BaseException:
                self._head_cache = None
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise

    def _init_chain(self) -> None:
        """Add the chain columns to a log made before them and write the genesis mark once:
        rows already there stay unchained (the genesis says up to which seq), never chained after
        the fact."""
        trust: tuple | None = None
        try:
            with self._tx() as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(traces)").fetchall()}
                for col in ("prev", "hash"):
                    if col not in cols:
                        c.execute(f"ALTER TABLE traces ADD COLUMN {col} TEXT")
                if c.execute("SELECT 1 FROM audit_marks LIMIT 1").fetchone() is None:
                    trust = self._write_genesis(c)
                self._genesis = self._load_genesis(c)
                last = c.execute("SELECT body FROM audit_marks WHERE kind = 'checkpoint' ORDER BY n DESC LIMIT 1"
                                 ).fetchone()
                if last is not None:
                    body = json.loads(last[0])
                    head = self._head(c)[0]
                    self._since_cp = max(0, head - int(body.get("seq") or 0))
        except (sqlite3.Error, ValueError):
            log.exception("the audit chain could not start; rows are kept and chained once it does")
            return
        if trust is not None:
            try:
                self.keys.add_trust(*trust)
            except OSError:
                log.exception("audit trust file not written")

    def _write_genesis(self, c: sqlite3.Connection) -> tuple | None:
        used = auditlog.max_seq_used(c) or 0
        after = max(int(c.execute("SELECT COALESCE(MAX(seq), 0) FROM traces").fetchone()[0]), used)
        unchained = int(c.execute("SELECT COUNT(*) FROM traces").fetchone()[0])
        chain = new_id("ch")
        try:
            kid, key = self.keys.create()
            pub: str | None = pub_text(key)
        except OSError:
            log.exception("audit key not created; the genesis mark is unsigned")
            kid, key, pub = None, None, None
        now = self.clock()
        body = {"v": auditlog.FORMAT, "kind": "genesis", "chain": chain, "n": 1, "prev": None, "at": iso(now),
                "kid": kid, "pub": pub, "after_seq": after, "unchained": unchained}
        data = canonical(body)
        c.execute("INSERT INTO audit_marks (n, kind, body, hash, sig) VALUES (1, 'genesis', ?, ?, ?)",
                  (data.decode(), mark_hash(body), sign(key, SIG_CTX, data) if key else None))
        if unchained:
            log.warning("audit chain started after seq %s: %s earlier rows stay unchained", after, unchained)
        return (chain, kid, pub, now) if kid and pub else None

    @staticmethod
    def _load_genesis(c: sqlite3.Connection) -> dict | None:
        row = c.execute("SELECT body, hash FROM audit_marks WHERE n = 1 AND kind = 'genesis'").fetchone()
        if row is None:
            return None
        return {**json.loads(row[0]), "hash": row[1]}

    def _head(self, c: sqlite3.Connection) -> tuple[int, str]:
        """(the highest seq used, the hash the next row links to). After retention removed every
        row up to the newest, the head is the retention mark's "pruned through seq N, hash H".
        Cached between this store's own transactions: `PRAGMA data_version` changes when any
        other connection commits, and only then is the head read again."""
        # data_version is per connection, and each thread has its own: the key names both
        dv = c.execute("PRAGMA data_version").fetchone()[0]
        cache = self._head_cache
        if cache is not None and cache[0][0] is c and cache[0][1] == dv:
            return cache[1], cache[2]
        seq, h = self._read_head(c)
        self._head_cache = ((c, dv), seq, h)
        return seq, h

    def _read_head(self, c: sqlite3.Connection) -> tuple[int, str]:
        """The head, never below a seq the log already used (SQLite's counter): a seq is never
        given twice, even after retention emptied the table and the store was opened again."""
        seq, h = self._chain_head(c)
        return max(seq, auditlog.max_seq_used(c) or 0), h

    def _chain_head(self, c: sqlite3.Connection) -> tuple[int, str]:
        g = self._genesis or {}
        base = (int(g.get("after_seq") or 0), str(g.get("hash") or ""))
        r = c.execute("SELECT seq, hash FROM traces ORDER BY seq DESC LIMIT 1").fetchone()
        floor = self._floor(c)
        if floor is not None and (r is None or int(r[0]) <= floor[0]):
            return (floor[0], floor[1]) if floor[1] else (max(floor[0], base[0]), base[1])
        if r is None:
            return base
        if r[1] is not None:
            return int(r[0]), str(r[1])
        chained = c.execute("SELECT hash FROM traces WHERE hash IS NOT NULL ORDER BY seq DESC LIMIT 1").fetchone()
        return max(int(r[0]), base[0]), str(chained[0]) if chained else base[1]

    def _floor(self, c: sqlite3.Connection) -> tuple[int, str | None] | None:
        """How far retention has removed the log: the highest (through_seq, through_hash) of every
        retention mark (an older node could write one below the one before), cached until a mark is
        added."""
        n = c.execute("SELECT MAX(n) FROM audit_marks").fetchone()[0]
        if n != self._floor_n:
            best: tuple[int, str | None] | None = None
            for (text,) in c.execute("SELECT body FROM audit_marks WHERE kind = 'pruned' ORDER BY n").fetchall():
                try:
                    body = json.loads(text)
                    mark = (int(body["through_seq"]), body.get("through_hash"))
                except (ValueError, KeyError, TypeError):
                    continue
                if best is None or mark[0] >= best[0]:
                    best = mark
            self._floor_v = best
            self._floor_n = n
        return self._floor_v

    def _insert(self, c: sqlite3.Connection, rows: Iterable[dict]) -> int:
        """Chain and insert rows in order; a row whose id is already there (a spill file
        replayed after a crash) is skipped and takes no seq."""
        seq, prev = self._head(c)
        chained = self._genesis is not None
        n = 0
        for row in rows:
            try:
                rec, done = self._insert_one(c, row, seq, prev, chained)
            except _ROW_ERRORS:
                # this one row cannot be stored (a value SQLite refuses): a stub keeps its place, so
                # it never blocks the rows behind it
                log.exception("trace %s could not be stored; a stub was written instead", str(row.get("id"))[:40])
                rec, done = self._insert_one(c, self._stub_row(row), seq, prev, chained)
            if done:
                seq += 1
                prev = rec["hash"] or prev
                n += 1
        if self._head_cache is not None:
            self._head_cache = (self._head_cache[0], seq, prev)
        return n

    @staticmethod
    def _insert_one(c: sqlite3.Connection, row: dict, seq: int, prev: str, chained: bool) -> tuple[dict, bool]:
        if row.get("id") is not None and c.execute("SELECT 1 FROM traces WHERE id = ?", (row["id"],)).fetchone():
            # already in the log (a spill file read twice): an ignored INSERT would still move SQLite's
            # seq counter past the head, and verify would read that as rows deleted from the end
            return {**row}, False
        rec = {**row, "seq": seq + 1}
        h = row_hash(prev, rec) if chained else None
        rec["prev"], rec["hash"] = (prev, h) if chained else (None, None)
        return rec, bool(c.execute(_INSERT, rec).rowcount)

    def _signing_key(self, c: sqlite3.Connection) -> tuple[str | None, Any]:
        """The audit key the log's own marks make active (the latest genesis or key mark)."""
        row = c.execute("SELECT kind, body FROM audit_marks WHERE kind IN ('genesis', 'key') ORDER BY n DESC LIMIT 1"
                        ).fetchone()
        if row is None:
            return None, None
        body = json.loads(row[1])
        kid = body.get("next_kid") if row[0] == "key" else body.get("kid")
        return kid, self.keys.private(kid)

    def _add_mark(self, c: sqlite3.Connection, kind: str, fields: dict, *, signer: tuple | None = None) -> dict:
        last = c.execute("SELECT n, hash FROM audit_marks ORDER BY n DESC LIMIT 1").fetchone()
        n, prev = (int(last[0]) + 1, last[1]) if last else (1, None)
        kid, key = signer or self._signing_key(c)
        if key is None:
            log.error("audit key %s is not on this node: mark %s (%s) is written unsigned", kid, n, kind)
        body = {"v": auditlog.FORMAT, "kind": kind, "chain": (self._genesis or {}).get("chain"), "n": n,
                "prev": prev, "at": iso(self.clock()), "kid": kid, **fields}
        data = canonical(body)
        c.execute("INSERT INTO audit_marks (n, kind, body, hash, sig) VALUES (?, ?, ?, ?, ?)",
                  (n, kind, data.decode(), mark_hash(body), sign(key, SIG_CTX, data) if key is not None else None))
        return body

    def checkpoint(self, reason: str = "manual") -> dict | None:
        """Sign the chain head (seq, hash, time, count). None when nothing new was written since
        the last checkpoint, or the log is not chained yet. Never raises on a locked database."""
        with self._lock:
            if self._genesis is None:
                return None
            try:
                with self._tx() as c:
                    seq, h = self._head(c)
                    after = int(self._genesis.get("after_seq") or 0)
                    last = c.execute("SELECT body FROM audit_marks WHERE kind = 'checkpoint' ORDER BY n DESC LIMIT 1"
                                     ).fetchone()
                    if seq <= after or (last is not None and int(json.loads(last[0]).get("seq") or 0) >= seq):
                        body = None
                    else:
                        body = self._add_mark(c, "checkpoint", {"seq": seq, "hash": h, "count": seq - after,
                                                                "reason": reason})
            except sqlite3.Error as e:
                log.warning("audit checkpoint deferred: %s", e)
                return None
            self._since_cp, self._cp_at = 0, self.clock()
        if body is not None and self.on_checkpoint is not None:
            try:
                self.on_checkpoint(body)
            except Exception:
                log.exception("audit checkpoint hook failed")
        self._mark_written(body)
        return body

    def _mark_written(self, body: dict | None) -> None:
        if body is not None and self.on_mark is not None:
            try:
                self.on_mark(body)
            except Exception:
                log.exception("audit mark hook failed")

    def _maybe_checkpoint(self) -> None:
        if not self._since_cp:
            return
        if self._since_cp >= self.checkpoint_rows:
            self.checkpoint("rows")
        elif self.clock() - self._cp_at >= self.checkpoint_s:
            self.checkpoint("time")

    def rotate_audit_key(self, *, reset: bool = False) -> dict:
        """Make a new audit key active with a key mark signed by the old one (`reset`: signed by
        the new key only, for a node that lost its key; verify reports it). The old private key
        is deleted; its public half stays in the log."""
        with self._lock:
            if self._genesis is None:
                raise InvalidRequest("the audit chain has not started on this node yet")
            with self._tx() as c:
                old_kid, old_key = self._signing_key(c)
                if old_key is None and not reset:
                    raise InvalidRequest(f"the active audit key {old_kid} is not on this node; rotate with --reset")
                new_kid, new_key = self.keys.create()
                fields = {"next_kid": new_kid, "next_pub": pub_text(new_key), "reset": bool(reset)}
                body = self._add_mark(c, "key", fields, signer=(new_kid, new_key) if reset else None)
            try:
                self.keys.add_trust(str(self._genesis.get("chain")), new_kid, fields["next_pub"], self.clock())
            except OSError:
                log.exception("audit trust file not updated")
            if old_kid and old_kid != new_kid:
                self.keys.forget(old_kid)
            return {"kid": new_kid, "previous": old_kid, "mark": body["n"], "reset": bool(reset)}

    def marks(self) -> list[dict]:
        with self.db.lock:
            return auditlog.read_marks(self.db.conn)

    def chain_rows(self, *, from_seq: int = 0, to_seq: int | None = None) -> Iterator[dict]:
        return auditlog.iter_rows(self.db.conn, from_seq=from_seq, to_seq=to_seq, lock=self.db.lock)

    def audit_sign(self, ctx: bytes, data: bytes) -> tuple[str | None, str | None]:
        """Sign `data` with the active audit key (export manifests, share links): (kid, sig)."""
        with self._lock, self.db.lock:
            kid, key = self._signing_key(self.db.conn)
        return kid, (sign(key, ctx, data) if key is not None else None)

    def audit_keys(self) -> dict[str, str]:
        """kid -> public key (base64url) of every audit key the log's marks introduced."""
        out: dict[str, str] = {}
        for m in self.marks():
            try:
                body = json.loads(m["body"])
            except ValueError:
                continue
            if m["kind"] == "genesis" and body.get("kid") and body.get("pub"):
                out[body["kid"]] = body["pub"]
            elif m["kind"] == "key" and body.get("next_kid") and body.get("next_pub"):
                out[body["next_kid"]] = body["next_pub"]
        return out

    def seq_bounds(self, since: float, until: float) -> tuple[int | None, int | None]:
        """The lowest and highest seq of rows written in [since, until)."""
        row = self.db.one("SELECT MIN(seq) AS lo, MAX(seq) AS hi FROM traces WHERE at >= ? AND at < ?", (since, until))
        return (row["lo"], row["hi"]) if row else (None, None)

    def lane_rows(self, lane: str, since: float, until: float, *, viewer_path: str | None = None,
                  limit: int = 5000) -> list[dict]:
        """A lane's rows in [since, until) in seq order, with the events traced inside those calls
        (a handler's ctx.trace rows may carry no lane). `viewer_path` keeps one grant family."""
        cols = ", ".join((*auditlog.ROW_COLUMNS, "prev", "hash"))
        where, params = "lane = ? AND at >= ? AND at < ?", [lane, since, until]
        if viewer_path is not None:
            where += " AND grant_path LIKE ?"
            params.append(f"%/{viewer_path}/%")
        rows = [dict(r) for r in self.db.all(f"SELECT {cols} FROM traces WHERE {where} ORDER BY seq LIMIT ?",
                                            (*params, limit))]
        ids = [r["id"] for r in rows if r["kind"] == "tool_start"]
        seen = {r["id"] for r in rows}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            extra = self.db.all(f"SELECT {cols} FROM traces WHERE parent_id IN ({', '.join('?' * len(chunk))})"
                                " AND (lane IS NULL OR lane != ?) ORDER BY seq", (*chunk, lane))
            rows += [dict(r) for r in extra if r["id"] not in seen and
                     (viewer_path is None or f"/{viewer_path}/" in (r["grant_path"] or ""))]
        rows.sort(key=lambda r: r["seq"])
        return rows

    def lane_counts(self, since: float, until: float, *, viewer_path: str | None = None) -> list[tuple[str, int]]:
        """(lane, rows) for every lane with rows in [since, until)."""
        where, params = "lane IS NOT NULL AND at >= ? AND at < ?", [since, until]
        if viewer_path is not None:
            where += " AND grant_path LIKE ?"
            params.append(f"%/{viewer_path}/%")
        return [(r["lane"], int(r["n"])) for r in self.db.all(
            f"SELECT lane, COUNT(*) AS n FROM traces WHERE {where} GROUP BY lane ORDER BY lane", params)]

    def children(self, call_id: str) -> list[dict]:
        """The rows traced inside one call (its tool_end and handler events), in seq order."""
        cols = ", ".join((*auditlog.ROW_COLUMNS, "prev", "hash"))
        return [dict(r) for r in self.db.all(f"SELECT {cols} FROM traces WHERE parent_id = ? ORDER BY seq LIMIT 500",
                                            (call_id,))]

    def row(self, trace_id: str) -> dict | None:
        cols = ", ".join((*auditlog.ROW_COLUMNS, "prev", "hash"))
        r = self.db.one(f"SELECT {cols} FROM traces WHERE id = ?", (trace_id,))
        return dict(r) if r else None

    def verify(self, *, scope: Scope | None = None, from_seq: int | None = None, to_seq: int | None = None,
               trust_note: bool = True) -> Report:
        """Check the chain, the marks and their signatures (agentos.policy.auditlog.verify_db)."""
        self.flush()
        return auditlog.verify_db(self.db.conn, trusted=self.keys.trusted_kids(), scope=scope, lock=self.db.lock,
                                  from_seq=from_seq, to_seq=to_seq, trust_note=trust_note)

    def close(self) -> None:
        """Write what is waiting, sign a shutdown checkpoint, close the file. Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._flush_locked()
                if self._since_cp:
                    self.checkpoint("shutdown")
            except Exception:
                log.exception("trace shutdown flush failed")
        self.db.close()  # outside the lock: a flush queued on the worker may still be waiting for it

    # ---- writing ----
    def add_scrub(self, value: str) -> None:
        if value and value not in self._scrub:
            self._scrub.append(value)

    def subscribe(self, fn: Callable[[str, dict], Any]) -> Callable[[], None]:
        return self.bus.subscribe(fn)

    def _redacted(self, ev: TraceEvent, extra: Iterable[str]) -> TraceEvent:
        scrub = [*self._scrub, *extra]
        if self.value_scrub is not None:
            ev.args = walk(ev.args, self.value_scrub) if ev.args is not None else None
            ev.summary = self.value_scrub(ev.summary) if ev.summary else ev.summary
            ev.error = self.value_scrub(ev.error) if ev.error else ev.error
        ev.args = redact(ev.args, extra=scrub) if ev.args is not None else None
        ev.summary = redact_text(ev.summary, extra=scrub, max_len=1000) if ev.summary else ev.summary
        ev.error = redact_text(ev.error, extra=scrub, max_len=1000) if ev.error else ev.error
        if ev.lane and not ev.workspace and "/" in ev.lane:
            ev.workspace = ev.lane.split("/", 1)[0]
        if ev.status not in STATUSES:
            ev.status = "error"
        if ev.at is None:
            ev.at = self.clock()
        return ev

    def write(self, ev: TraceEvent, *, extra_scrub: Iterable[str] = ()) -> TraceEvent:
        """Redact and append. Never raises."""
        try:
            ev = self._redacted(ev, extra_scrub)
            row = self._row(ev)
        except Exception:
            log.exception("trace redaction failed; event dropped to a stub")
            ev = TraceEvent(kind=ev.kind, tool=ev.tool, status="error", summary="[trace could not be redacted]")
            row = self._row(ev)
        self._intake.append(row)
        if _on_loop():
            self._write_nowait()
        else:  # the CLI, a thread: waits for the lock as it always did
            with self._lock:
                self._flush_locked()
                try:
                    self._maybe_checkpoint()
                except Exception:
                    log.exception("audit checkpoint failed")
        try:
            self.bus.publish("trace", ev.public())
        except Exception:
            log.exception("trace publish failed")
        return ev

    def _write_nowait(self) -> None:
        """On the event loop: write at once when nothing holds traces.db's write lock, else hand
        the rows to the worker thread, which waits for the lock (busy_timeout) and, past it, keeps
        or spills them as a failed write always did (the fail-closed clock starts there). The
        loop never waits: not for SQLite's lock, nor for a flush the worker has running."""
        if self._closed:
            return
        if self._flush_queued or self._spilled or not self._lock.acquire(blocking=False):
            self._queue_flush()
            return
        if not self.db.lock.acquire(blocking=False):  # a reader holds a snapshot (the anchors, a verify)
            self._lock.release()
            self._queue_flush()
            return
        try:
            if self._closed:
                return
            if self._genesis is None or self._spill_waits():
                self._queue_flush()  # the slow paths (chain set up, spill replay) run on the worker
                return
            conn = self.db.conn
            conn.execute("PRAGMA busy_timeout=0")
            self._nowait = True
            try:
                self._flush_locked()
                if not self._intake and not self._pending:
                    with contextlib.suppress(Exception):
                        self._maybe_checkpoint()  # a locked one is deferred to the tick
            finally:
                self._nowait = False
                conn.execute(f"PRAGMA busy_timeout={int(self.db.timeout * 1000)}")
            if self._intake or self._pending:
                self._queue_flush()
        finally:
            self.db.lock.release()
            self._lock.release()

    def _queue_flush(self) -> None:
        if self._flush_queued:
            return
        self._flush_queued = True
        try:
            self.db.later(self._flush_on_worker)
        except RuntimeError:  # the worker is shut down (closing): the close flush takes the rows
            self._flush_queued = False

    def _flush_on_worker(self) -> None:
        with self._lock:
            self._flush_queued = False
            if self._closed:
                return
            self._flush_locked()
            try:
                self._maybe_checkpoint()
            except Exception:
                log.exception("audit checkpoint failed")

    @property
    def deferred(self) -> bool:
        """Rows written on the event loop still wait for the worker thread."""
        return self._flush_queued or bool(self._intake)

    async def settled(self) -> None:
        """Wait, off the loop, until rows this loop wrote have been tried with the full lock wait
        (written, or kept and spilled as a failed write is): an MCP call's rows before it returns."""
        if self.deferred:
            await self.db.flush()

    async def aflush(self) -> int:
        """flush() on the worker thread (the policy tick)."""
        return await self.db.run(self.flush)

    def _row(self, ev: TraceEvent) -> dict:
        """The row as stored. Every value is one SQLite binds and the canonical form encodes, so
        no value an agent sends can fail the write (AUDIT-LOG.md "Write path")."""
        now = self.clock()
        return {
            "id": _text(ev.id), "at": _real(ev.at, now) if ev.at is not None else now, "kind": _text(ev.kind) or "event",
            "principal": _text(ev.principal), "principal_kind": _text(ev.principal_kind),
            "grant_id": _text(ev.grant_id), "grant_path": _text(ev.grant_path), "tool": _text(ev.tool),
            "lane": _text(ev.lane), "workspace": _text(ev.workspace), "effect": _text(ev.effect),
            "status": _text(ev.status) or "error", "summary": _text(ev.summary),
            "args": json.dumps(ev.args, default=str) if ev.args is not None else None,
            "error": _text(ev.error), "cost_tokens": _int(ev.cost.tokens) or 0, "cost_usd": _real(ev.cost.usd, 0.0),
            "duration_ms": _int(ev.duration_ms), "run_id": _text(ev.run_id), "parent_id": _text(ev.parent_id),
        }

    def _stub_row(self, row: dict) -> dict:
        """What is stored in place of a row SQLite refused: its id, time and kind, nothing else."""
        at = row.get("at")
        return {**{c: None for c in auditlog.ROW_COLUMNS if c != "seq"},
                "id": _text(row.get("id")) or new_id("tr"), "at": _real(at, self.clock()),
                "kind": _text(row.get("kind")) or "event", "status": "error", "cost_tokens": 0, "cost_usd": 0.0,
                "summary": "[trace could not be stored]"}

    def _spill_waits(self) -> bool:
        """A spill file is there, or cannot be looked for (the worker's flush then finds out why)."""
        try:
            return self.spill_path.exists()
        except OSError:
            return True

    def _flush_locked(self) -> None:
        """Write what waits. Any error that keeps rows out is a failed write (_write_failed), so the
        fail-closed clock starts for it, not only for an error SQLite raised."""
        try:
            self._flush_rows()
        except Exception as e:  # noqa: BLE001  (every error that keeps rows out starts the clock)
            log.warning("trace write failed: %s: %s", type(e).__name__, e)
            self._write_failed(f"{type(e).__name__}: {e}")

    def _flush_rows(self) -> None:
        while self._intake:  # in the order written
            self._pending.append(self._intake.popleft())
        if (self._spilled or self.spill_path.exists()) and not self._spill_restore():
            self._write_failed("the database still refuses the spilled rows")
            return
        if self._genesis is None:
            self._init_chain()
        if not self._pending:
            self.db_failing_since = self.unwritten_since = None
            return
        rows = list(self._pending)
        try:
            with self._tx() as c:
                n = self._insert(c, rows)
        except sqlite3.Error as e:
            if self._nowait and _is_busy(e):
                return  # another connection holds the lock: the worker waits for it (not a failure yet)
            log.warning("trace write deferred: %s", e)
            self._write_failed(str(e))
            return
        self._pending.clear()
        self._since_cp += n
        self.db_failing_since = self.unwritten_since = None

    def _write_failed(self, why: str) -> None:
        """The database refused rows: keep them in memory for a blip, then in the spill file. Rows
        neither takes start the clock the fail-closed rule reads (agentos.policy.audit_health)."""
        now = self.clock()
        self.last_write_error = why
        if self.db_failing_since is None:
            self.db_failing_since = now
        if not self._pending:
            return
        if len(self._pending) > self.MAX_PENDING or self._spilled or now - self.db_failing_since >= SPILL_AFTER_S:
            try:
                self._spill()
            except OSError as e:
                log.error("trace spill failed: %s", e)
                self.last_write_error = f"{why}; the spill file also failed: {e}"
            else:
                self.unwritten_since = None
                return
        if self.unwritten_since is None:
            self.unwritten_since = now

    def _spill(self) -> None:
        """Move what waits in memory to the spill file, unchained: rows get their seq and hash
        only when they reach the database, in the order they were written."""
        rows = list(self._pending)
        fd = os.open(self.spill_path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            size = os.fstat(fd).st_size
            # a write that broke off mid-line leaves no newline: start on a line of our own, so the
            # torn line stays alone and the rows after it are read back
            lead = "\n" if size and os.pread(fd, 1, size - 1) != b"\n" else ""
            f.write(lead + "".join(json.dumps(r) + "\n" for r in rows))
            f.flush()
            os.fsync(f.fileno())
        self._spilled = True
        for _ in rows:  # only once they are on disk: a failed spill keeps them waiting in memory
            self._pending.popleft()

    def _restoring_files(self) -> list[Path]:
        return sorted(self.spill_path.parent.glob(self.spill_path.name + ".*.restoring"))

    def _spill_restore(self) -> bool:
        """Insert spilled rows, oldest first, before anything waiting in memory. A spill file is
        deleted only after its rows are committed, so a crash in between replays it: rows already
        in the log are skipped by id, so the chain never forks or repeats a row. Once committed,
        the file is renamed and read once more, for rows another process appended meanwhile.
        False while the database still refuses writes."""
        for f in self._restoring_files():
            if not self._replay(f):
                return False
            f.unlink(missing_ok=True)
        if self.spill_path.exists():
            if not self._replay(self.spill_path):
                return False
            target = self.spill_path.with_name(f"{self.spill_path.name}.{time.time_ns():020d}-{os.getpid()}.restoring")
            try:
                os.replace(self.spill_path, target)
            except FileNotFoundError:
                target = None  # another process got there first
            if target is not None:
                if not self._replay(target):
                    return False
                target.unlink(missing_ok=True)
        self._spilled = False
        return True

    def _replay(self, f: Path) -> bool:
        """Insert the rows of one spill file. Every complete line goes in; a line that broke off
        mid-write (a full disk, a crash) is set aside on its own in a `.torn` file once no writer
        can still be finishing it (the renamed `.restoring` pass), and named by the health monitor."""
        try:
            data = f.read_bytes()
        except FileNotFoundError:
            return True  # another process restored it
        except OSError:
            log.exception("trace spill file %s unreadable; set aside", f)
            with contextlib.suppress(OSError):
                os.replace(f, f.with_suffix(".unreadable"))
            return True
        final = f != self.spill_path  # renamed: nothing appends to it any more
        lines = data.split(b"\n")
        tail = lines.pop()  # after the last newline: empty when the file ends cleanly
        if tail.strip() and final:
            lines.append(tail)  # complete but for its newline, or torn: parsing tells
        rows, torn = [], []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                r = None
            if isinstance(r, dict):
                rows.append(r)
                continue
            # an older spill appended the next row straight after a torn one: a row starts with
            # {"id": (quotes inside its values are escaped), and only the last piece can be whole
            cut = raw.rfind(b'{"id": ', 1)
            whole = None
            if cut > 0:
                try:
                    whole = json.loads(raw[cut:])
                except ValueError:
                    whole = None
            if isinstance(whole, dict):
                rows.append(whole)
                torn.append(raw[:cut])
            else:
                torn.append(raw)
        if self._genesis is None:
            self._init_chain()
        try:
            with self._tx() as c:
                self._since_cp += self._insert(c, rows)
        except sqlite3.Error as e:
            log.warning("spilled trace rows deferred: %s", e)
            return False
        if torn and final:  # only once the rows are in: a retry must not set the same lines aside twice
            self._set_aside(f, torn)
        return True

    def _set_aside(self, f: Path, torn: list[bytes]) -> None:
        dest = f.with_name(f"{self.spill_path.name}.{time.time_ns():020d}.torn")
        kept = True
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(b"\n".join(torn) + b"\n")
        except OSError:
            kept = False
            log.exception("torn spill lines could not be set aside")
        self.spill_torn = {"lines": len(torn) + int((self.spill_torn or {}).get("lines") or 0),
                           "file": str(dest) if kept else None, "at": self.clock()}
        log.error("trace spill file %s had %d torn line(s): every complete row was restored, the torn part %s",
                  f, len(torn), f"is in {dest}" if kept else "could not be saved")

    def flush(self) -> int:
        """Retry deferred writes, and sign a checkpoint when one is due (the policy tick calls
        this every few seconds, so a quiet node still gets its time checkpoint). Returns how
        many events are still waiting."""
        with self._lock:
            if self._closed:
                return self.pending_count
            self._flush_locked()
            try:
                self._maybe_checkpoint()
            except Exception:
                log.exception("audit checkpoint failed")
            return self.pending_count

    @property
    def pending_count(self) -> int:
        return len(self._pending) + len(self._intake)

    # ---- reading ----
    def query(self, *, viewer_path: str | None = None, principal: str | None = None, grant_id: str | None = None,
              lane: str | None = None, workspace: str | None = None, tool: str | None = None,
              kind: str | None = None, status: str | None = None, effect: str | None = None,
              run_id: str | None = None, since: float | None = None, until: float | None = None,
              before: str | None = None, limit: int = 100) -> tuple[list[TraceEvent], str | None]:
        """Newest first. `viewer_path` limits results to one grant family (a grant id); None
        means everything (owner). `before` is a cursor or a trace id. Returns (events, next)."""
        limit = max(1, min(int(limit), 500))
        where, params = [], []
        if viewer_path is not None:
            where.append("grant_path LIKE ?")
            params.append(f"%/{viewer_path}/%")
        for col, val in (("principal", principal), ("grant_id", grant_id), ("lane", lane),
                         ("workspace", workspace), ("tool", tool), ("kind", kind), ("status", status),
                         ("effect", effect), ("run_id", run_id)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        if since is not None:
            where.append("at >= ?")
            params.append(since)
        if until is not None:
            where.append("at < ?")
            params.append(until)
        if before:
            if before.startswith("tr_"):
                row = self.db.one("SELECT seq FROM traces WHERE id = ?", (before,))
                if row is None:
                    raise InvalidRequest("unknown trace id in before")
                seq = row["seq"]
            else:
                seq = _decode_cursor(before)
            where.append("seq < ?")
            params.append(seq)
        sql = "SELECT * FROM traces"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit + 1)
        rows = self.db.all(sql, params)
        more = len(rows) > limit
        rows = rows[:limit]
        events = [self._event(r) for r in rows]
        nxt = _encode_cursor(rows[-1]["seq"]) if more and rows else None
        return events, nxt

    def cost_since(self, since: float) -> Cost:
        """Tokens and dollars recorded on events since `since` (a Unix time): the panel's cost today."""
        try:
            row = self.db.one("SELECT COALESCE(SUM(cost_tokens), 0) AS t, COALESCE(SUM(cost_usd), 0) AS u "
                              "FROM traces WHERE at >= ?", (since,))
        except sqlite3.Error as e:
            log.warning("trace cost query failed: %s", e)
            return Cost()
        return Cost(tokens=int(row["t"]), usd=float(row["u"])) if row else Cost()

    INTERRUPTED = "interrupted: agentosd stopped before this call finished"

    def end_unfinished(self, *, max_age_days: int | None = None) -> int:
        """Give every tool call that started and never ended (agentosd was killed in the middle
        of it) its end: status error, `INTERRUPTED`. Run once at daemon start, before any call,
        so the action log never shows a call half done without saying so. Returns how many."""
        days = self.retention_days if max_age_days is None else max_age_days
        since = self.clock() - days * 86400
        self.flush()
        rows = self.db.all(
            "SELECT * FROM traces s WHERE s.kind = 'tool_start' AND s.at >= ? AND NOT EXISTS "
            "(SELECT 1 FROM traces e WHERE e.parent_id = s.id AND e.kind = 'tool_end') ORDER BY s.seq", (since,))
        for row in rows:
            start = self._event(row)
            self.write(TraceEvent(
                kind="tool_end", tool=start.tool, lane=start.lane, workspace=start.workspace, effect=start.effect,
                status="error", error=self.INTERRUPTED, principal=start.principal,
                principal_kind=start.principal_kind, grant_id=start.grant_id, grant_path=start.grant_path,
                args=start.args, run_id=start.run_id, parent_id=start.id))
        if rows:
            log.warning("%d tool call(s) were cut by a stop of agentosd; their ends now say so", len(rows))
        return len(rows)

    def get(self, trace_id: str) -> TraceEvent | None:
        row = self.db.one("SELECT * FROM traces WHERE id = ?", (trace_id,))
        return self._event(row) if row else None

    @staticmethod
    def _event(r: sqlite3.Row) -> TraceEvent:
        return TraceEvent(
            id=r["id"], at=r["at"], kind=r["kind"], principal=r["principal"], principal_kind=r["principal_kind"],
            grant_id=r["grant_id"], grant_path=r["grant_path"], tool=r["tool"], lane=r["lane"],
            workspace=r["workspace"], effect=r["effect"], status=r["status"], summary=r["summary"],
            args=json.loads(r["args"]) if r["args"] else None, error=r["error"],
            cost=Cost(tokens=r["cost_tokens"], usd=r["cost_usd"]), duration_ms=r["duration_ms"],
            run_id=r["run_id"], parent_id=r["parent_id"],
        )

    # ---- evidence (workspace memory, docs/design/workspace-memory.md 4.3) ----
    def visible(self, trace_id: str, viewer_path: str | None, workspace: str) -> bool:
        """Does the trace exist, belong to `workspace` and fall under `viewer_path` (the rule
        `query` and traces_query use: a grant id sees its own calls and its narrowed grants'; None
        sees everything)?"""
        row = self.db.one("SELECT grant_path, workspace FROM traces WHERE id = ?", (trace_id,))
        if row is None or row["workspace"] != workspace:
            return False
        return viewer_path is None or f"/{viewer_path}/" in (row["grant_path"] or "")

    def snapshot(self, trace_id: str) -> dict | None:
        """A small redacted record of a trace that outlives retention when copied into memory
        provenance: id, tool, time, status, summary."""
        ev = self.get(trace_id)
        if ev is None:
            return None
        return {"trace": ev.id, "tool": ev.tool, "kind": ev.kind, "at": iso(ev.at), "status": ev.status,
                "lane": ev.lane, "summary": (ev.summary or "")[:300] or None}

    def page_url_at(self, trace_id: str) -> str | None:
        """The page URL the node recorded for the call a trace belongs to: a `page_url` a handler
        traced (ctx.trace(..., page_url=...)), never the agent's own arguments. None when the
        call recorded none."""
        ev = self.get(trace_id)
        if ev is None:
            return None
        call_id = ev.id if ev.kind == "tool_start" or not ev.parent_id else ev.parent_id
        rows = self.db.all("SELECT args FROM traces WHERE (id = ? OR parent_id = ?) AND kind NOT IN (?, ?) "
                           "AND args IS NOT NULL ORDER BY seq DESC", (call_id, call_id, *_CALL_KINDS))
        for r in rows:
            try:
                url = json.loads(r["args"]).get("page_url")
            except (ValueError, AttributeError):
                continue
            if isinstance(url, str) and url:
                return url[:2000]
        return None

    def pin(self, trace_id: str, ref: str) -> bool:
        """Keep a trace through retention while `ref` (a memory entry) needs it. Past max_pins the
        oldest pins are dropped. Returns whether the trace exists."""
        now = self.clock()
        with self.db.tx() as conn:
            conn.execute("INSERT INTO trace_pins (trace_id, ref, at) VALUES (?, ?, ?) "
                         "ON CONFLICT(trace_id, ref) DO UPDATE SET at = excluded.at", (trace_id, ref, now))
            n = conn.execute("SELECT COUNT(*) AS n FROM trace_pins").fetchone()["n"]
            if n > self.max_pins:
                conn.execute("DELETE FROM trace_pins WHERE rowid IN (SELECT rowid FROM trace_pins "
                             "ORDER BY at ASC, rowid ASC LIMIT ?)", (n - self.max_pins,))
            exists = conn.execute("SELECT 1 FROM traces WHERE id = ?", (trace_id,)).fetchone() is not None
        return exists

    def unpin(self, ref: str, trace_id: str | None = None) -> int:
        """Release the pins of `ref` (all of them, or one trace's)."""
        if trace_id is None:
            return self.db.execute("DELETE FROM trace_pins WHERE ref = ?", (ref,)).rowcount
        return self.db.execute("DELETE FROM trace_pins WHERE ref = ? AND trace_id = ?", (ref, trace_id)).rowcount

    def pins(self, ref: str | None = None) -> list[dict]:
        sql, params = "SELECT trace_id, ref, at FROM trace_pins", ()
        if ref is not None:
            sql, params = sql + " WHERE ref = ?", (ref,)
        return [{"trace_id": r["trace_id"], "ref": r["ref"], "at": r["at"]}
                for r in self.db.all(sql + " ORDER BY at, rowid", params)]

    # ---- retention ----
    def purge(self, *, older_than_days: int | None = None) -> int:
        """Delete traces past retention, except pinned ones (memory evidence), and sign a
        "pruned through seq N, hash H" mark that also lists the pinned rows it kept, so a verifier
        can tell lawful retention from a deleted middle. Retention removes a prefix of the log
        (rows up to the first one still inside retention), so an old row written after a newer
        one waits for the prefix to reach it."""
        days = older_than_days if older_than_days is not None else self.retention_days
        cutoff = self.clock() - days * 86400
        body: dict | None = None
        with self._lock:
            try:
                with self._tx() as c:
                    head = c.execute("SELECT MAX(seq) FROM traces").fetchone()[0]
                    if head is None:
                        return 0
                    first_new = c.execute("SELECT MIN(seq) FROM traces WHERE at >= ?", (cutoff,)).fetchone()[0]
                    limit = first_new - 1 if first_new is not None else head
                    row = c.execute("SELECT seq, hash FROM traces WHERE seq <= ? AND id NOT IN "
                                    "(SELECT trace_id FROM trace_pins) ORDER BY seq DESC LIMIT 1", (limit,)).fetchone()
                    if row is None:
                        return 0
                    through, through_hash = int(row[0]), row[1]
                    deleted = c.execute("DELETE FROM traces WHERE seq <= ? AND id NOT IN "
                                        "(SELECT trace_id FROM trace_pins)", (through,)).rowcount
                    floor = self._floor(c)
                    if floor is not None and floor[0] > through:
                        # only rows an earlier mark kept were removed (a pin was released): "pruned
                        # through" never moves back, and the next row still links to that mark's hash
                        through, through_hash = floor
                    if self._genesis is not None:
                        kept = [[int(s), h] for s, h in c.execute("SELECT seq, hash FROM traces WHERE seq <= ? "
                                                                  "ORDER BY seq", (through,)).fetchall()]
                        body = self._add_mark(c, "pruned", {"through_seq": through, "through_hash": through_hash,
                                                            "deleted": deleted, "kept": kept, "cutoff": iso(cutoff)})
            except sqlite3.Error as e:
                log.warning("trace retention skipped: %s", e)
                return 0
        self._mark_written(body)
        return deleted

    def oldest_at(self) -> float | None:
        """When the oldest row still kept was written, or None for an empty log."""
        row = self.db.one("SELECT MIN(at) AS at FROM traces")
        return None if row is None or row["at"] is None else float(row["at"])

    def export_page(self, *, after: int = 0, since: float | None = None, until: float | None = None,
                    workspace: str | None = None, limit: int = 500) -> list[tuple[int, TraceEvent]]:
        """The next rows of the log in order, for an export: up to `limit` rows whose seq is past
        `after`, that fall in [since, until) and in `workspace` if one is named. Oldest first."""
        where, params = ["seq > ?"], [after]
        if since is not None:
            where.append("at >= ?")
            params.append(since)
        if until is not None:
            where.append("at < ?")
            params.append(until)
        if workspace is not None:
            where.append("workspace = ?")
            params.append(workspace)
        rows = self.db.all(f"SELECT * FROM traces WHERE {' AND '.join(where)} ORDER BY seq LIMIT ?",
                           (*params, max(1, min(int(limit), 5000))))
        return [(int(row["seq"]), self._event(row)) for row in rows]
