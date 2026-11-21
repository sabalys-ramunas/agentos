"""The action log: append-only, redacted at write time, in its own SQLite file (WAL).

Writing never fails a tool call. If the database is locked or broken, the event waits in
memory (and, past a bound, in a private spill file) and is written on the next success.
Updates are blocked by a trigger; only retention deletes old rows.

The log is tamper-evident (AUDIT-LOG.md, openajar.policy.auditlog): each row is hashed as
stored, after redaction, and linked to the previous row when it is inserted, inside the same
transaction that reads the chain head, so events waiting in memory or in the spill file get
their place in the chain only when they reach the database and two writers can never fork
it. Signed marks (genesis, checkpoints every N rows or M minutes and at shutdown, retention
anchors, key rotations) sit in `audit_marks`.

The event bus is a list of callables. The panel API subscribes to stream new events
(`trace`) and approval changes (`approval`) over SSE.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from openajar.core.errors import InvalidRequest
from openajar.policy import auditlog
from openajar.policy.auditlog import AuditKeys
from openajar.policy.store import DB
from openajar.policy.trace_chain import TraceChainMixin
from openajar.policy.trace_models import Cost, EventBus, TraceEvent
from openajar.policy.trace_writes import TraceWritesMixin
from openajar.policy.util import b64u, b64u_decode, iso

log = logging.getLogger("openajar.traces")

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
CHECKPOINT_ROWS = 1000   # a signed checkpoint at least every this many rows
CHECKPOINT_S = 600       # and every this many seconds while rows keep coming
MAX_PINS = 10_000   # pins per node; past it the oldest pins stop protecting their traces
# events a handler wrote (ctx.trace): their args are node-observed, unlike the agent's own
# arguments on tool_start and tool_end
_CALL_KINDS = ("tool_start", "tool_end")


def _encode_cursor(seq: int) -> str:
    return "c_" + b64u(str(seq).encode())


def _decode_cursor(cursor: str) -> int:
    try:
        if not cursor.startswith("c_"):
            raise ValueError
        return int(b64u_decode(cursor[2:]).decode())
    except (ValueError, UnicodeDecodeError):
        raise InvalidRequest("bad cursor") from None


class TraceStore(TraceChainMixin, TraceWritesMixin):
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
        # On the node, the vault's values (openajar.policy.scrub.runtime_scrub): a trace's summary or
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
        # The write path's health (openajar.policy.audit_health): since when the database has refused
        # rows, and since when rows sit only in memory (neither the database nor the spill file took
        # them). None while all is well.
        self.db_failing_since: float | None = None
        self.unwritten_since: float | None = None
        self.last_write_error: str | None = None
        # Called with each new checkpoint body, after it commits. Never raises.
        self.on_checkpoint: Callable[[dict], Any] | None = None
        # Called with each new checkpoint and retention mark body, after it commits
        # (openajar.node.anchors anchors both). Never raises.
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
    def subscribe(self, fn: Callable[[str, dict], Any]) -> Callable[[], None]:
        return self.bus.subscribe(fn)

    async def settled(self) -> None:
        """Wait, off the loop, until rows this loop wrote have been tried with the full lock wait
        (written, or kept and spilled as a failed write is): an MCP call's rows before it returns."""
        if self.deferred:
            await self.db.flush()

    async def aflush(self) -> int:
        """flush() on the worker thread (the policy tick)."""
        return await self.db.run(self.flush)

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
