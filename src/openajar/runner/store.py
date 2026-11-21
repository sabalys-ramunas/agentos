"""SQLite storage for flows, runs, the step journal, schedules, recordings, and the connector
layer's surfaces (AUD-01) and per-operation health.

One file (`$OPENAJAR_HOME/runner.db`), WAL mode, `synchronous=FULL`: every journal write
is on disk before the step it describes runs, so a crash or power cut never loses the
fact that a step started. Status transitions are compare-and-set, so the daemon and the
CLI can share the file safely.

The step journal (`steps`) holds what may be shown: param values a flow does not mark
`show` are already replaced by `${name}`. What a resume or a re-check needs verbatim (the
page URL, saved values, the resolved readback) sits in `step_state`, which is never shown,
like a run's own params. A row that cannot be read is skipped (and logged once), so one
bad row never stops the queue or the schedules.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from openajar.core.models import RunKind, RunStatus, Surface
from openajar.runner.flowdoc import FlowDefinition, FlowVersion
from openajar.runner.records import Run, Schedule, StepRecord, StepStatus
from openajar.runner.util import iso, parse_dt

log = logging.getLogger("openajar.runner")

SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
  id TEXT NOT NULL, version TEXT NOT NULL, definition TEXT NOT NULL,
  created_at TEXT NOT NULL, created_by TEXT, PRIMARY KEY (id, version));
CREATE TABLE IF NOT EXISTS flow_heads (id TEXT PRIMARY KEY, version TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, status TEXT NOT NULL, kind TEXT NOT NULL, lane TEXT, workspace TEXT,
  flow_id TEXT, schedule_id TEXT, idempotency_key TEXT UNIQUE, approval_id TEXT,
  created_at TEXT NOT NULL, not_before TEXT, recheck_at TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status, not_before);
CREATE INDEX IF NOT EXISTS runs_schedule ON runs(schedule_id, created_at);
CREATE INDEX IF NOT EXISTS runs_flow ON runs(flow_id, created_at);
CREATE INDEX IF NOT EXISTS runs_approval ON runs(approval_id);
CREATE TABLE IF NOT EXISTS steps (
  run_id TEXT NOT NULL, path TEXT NOT NULL, idx INTEGER NOT NULL, type TEXT NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, started_at TEXT, finished_at TEXT,
  result TEXT, error TEXT, PRIMARY KEY (run_id, path));
CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, flow_id TEXT NOT NULL, enabled INTEGER NOT NULL,
  token TEXT UNIQUE, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recordings (
  lane TEXT PRIMARY KEY, started_at TEXT NOT NULL, principal TEXT NOT NULL, events TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS step_state (
  run_id TEXT NOT NULL, path TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY (run_id, path));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS surfaces (id TEXT PRIMARY KEY, site TEXT NOT NULL, lane TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connector_health (
  operation TEXT NOT NULL, surface TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY (operation, surface));
"""

# columns added after the first release: (table, column, type)
COLUMNS = [("flows", "saved_by", "TEXT")]


def _j(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)


def _uj(value: str | None) -> Any:
    return None if value is None else json.loads(value)


class RunnerStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._bad_rows: set[str] = set()
        if str(path) != ":memory:":
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def _migrate(self) -> None:
        for table, column, type_ in COLUMNS:
            have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _skip_bad(self, what: str, key: str, e: Exception) -> None:
        if f"{what}:{key}" not in self._bad_rows:
            self._bad_rows.add(f"{what}:{key}")
            log.error("skipping unreadable %s row %s: %s", what, key, str(e)[:300])

    def meta(self, key: str, default: str) -> str:
        """A node-local value set once (the first caller's `default` wins)."""
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (key, default))
            return self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()["value"]

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(args))

    def _rows(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    # ------------------------------------------------------------ flows

    def put_flow(self, fv: FlowVersion) -> bool:
        """Insert a version (idempotent) and point the flow's head at it. True if new."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO flows (id, version, definition, created_at, created_by, saved_by) "
                "VALUES (?,?,?,?,?,?)",
                (fv.id, fv.version, json.dumps(fv.definition.canonical()), iso(fv.created_at), fv.created_by,
                 _j(fv.saved_by)))
            self._db.execute(
                "INSERT INTO flow_heads (id, version, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version, updated_at=excluded.updated_at",
                (fv.id, fv.version, iso(fv.created_at)))
            return cur.rowcount == 1

    def stage_flow(self, fv: FlowVersion) -> bool:
        """Insert a version without pointing the head at it: a pinned run can use it, nothing lists it."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO flows (id, version, definition, created_at, created_by, saved_by) "
                "VALUES (?,?,?,?,?,?)",
                (fv.id, fv.version, json.dumps(fv.definition.canonical()), iso(fv.created_at), fv.created_by,
                 _j(fv.saved_by)))
            return cur.rowcount == 1

    def _flow_from_row(self, row: sqlite3.Row) -> FlowVersion:
        return FlowVersion(definition=FlowDefinition.model_validate(json.loads(row["definition"])),
                           version=row["version"], created_at=parse_dt(row["created_at"]),
                           created_by=row["created_by"], saved_by=_uj(row["saved_by"]))

    def set_flow_saved_by(self, flow_id: str, version: str, saved_by: dict[str, Any]) -> None:
        self._exec("UPDATE flows SET saved_by=? WHERE id=? AND version=?", (_j(saved_by), flow_id, version))

    def get_flow(self, flow_id: str, version: str | None = None) -> FlowVersion | None:
        if version is None:
            rows = self._rows("SELECT f.* FROM flows f JOIN flow_heads h ON f.id=h.id AND f.version=h.version "
                              "WHERE f.id=?", (flow_id,))
        else:
            rows = self._rows("SELECT * FROM flows WHERE id=? AND version=?", (flow_id, version))
        return self._flow_from_row(rows[0]) if rows else None

    def flow_heads(self) -> list[FlowVersion]:
        rows = self._rows("SELECT f.* FROM flows f JOIN flow_heads h ON f.id=h.id AND f.version=h.version "
                          "ORDER BY f.id")
        return [self._flow_from_row(r) for r in rows]

    def flow_heads_stamp(self) -> tuple[tuple[str, str], ...]:
        """(id, version) of every flow head, without parsing a definition: cheap enough for
        agentosd to poll, so a flow or connector saved by another process shows up as tools."""
        return tuple((r["id"], r["version"]) for r in self._rows("SELECT id, version FROM flow_heads ORDER BY id"))

    def flow_versions(self, flow_id: str) -> list[FlowVersion]:
        rows = self._rows("SELECT * FROM flows WHERE id=? ORDER BY created_at, rowid", (flow_id,))
        return [self._flow_from_row(r) for r in rows]

    def delete_flow(self, flow_id: str) -> bool:
        with self._lock:
            self._db.execute("DELETE FROM flow_heads WHERE id=?", (flow_id,))
            cur = self._db.execute("DELETE FROM flows WHERE id=?", (flow_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------ runs

    def _run_args(self, run: Run) -> tuple:
        return (run.status.value, run.kind.value, run.lane, run.workspace, run.flow_id, run.schedule_id,
                run.idempotency_key, run.approval_id, iso(run.created_at), iso(run.not_before),
                iso(run.recheck_at), int(run.cancel_requested), run.model_dump_json())

    def insert_run(self, run: Run) -> tuple[Run, bool]:
        """Insert, or return the existing run with the same idempotency key."""
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO runs (status, kind, lane, workspace, flow_id, schedule_id, idempotency_key, "
                    "approval_id, created_at, not_before, recheck_at, cancel_requested, doc, id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*self._run_args(run), run.id))
                return run, True
            except sqlite3.IntegrityError:
                if run.idempotency_key:
                    existing = self.run_by_key(run.idempotency_key)
                    if existing:
                        return existing, False
                raise

    def save_run(self, run: Run, *, expect: Iterable[RunStatus] | None = None) -> bool:
        """Write the run. With `expect`, only if the stored status is one of those (CAS)."""
        sql = ("UPDATE runs SET status=?, kind=?, lane=?, workspace=?, flow_id=?, schedule_id=?, "
               "idempotency_key=?, approval_id=?, created_at=?, not_before=?, recheck_at=?, "
               "cancel_requested=?, doc=? WHERE id=?")
        args: list[Any] = [*self._run_args(run), run.id]
        if expect is not None:
            statuses = [s.value for s in expect]
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        with self._lock:
            return self._db.execute(sql, args).rowcount == 1

    def _run_from_row(self, row: sqlite3.Row) -> Run:
        run = Run.model_validate_json(row["doc"])
        # status and the cancel flag are authoritative in their columns (CLI writes them)
        run.status = RunStatus(row["status"])
        run.cancel_requested = bool(row["cancel_requested"])
        return run

    def get_run(self, run_id: str) -> Run | None:
        rows = self._rows("SELECT * FROM runs WHERE id=?", (run_id,))
        return self._run_from_row(rows[0]) if rows else None

    def run_by_key(self, key: str) -> Run | None:
        rows = self._rows("SELECT * FROM runs WHERE idempotency_key=?", (key,))
        return self._run_from_row(rows[0]) if rows else None

    def lane_run_times(self, lane: str, since: datetime) -> list[tuple[datetime, str]]:
        """(created_at, status) of each run on a lane created at or after `since`: what a lane's history counts."""
        rows = self._rows("SELECT created_at, status FROM runs WHERE lane = ? AND created_at >= ?",
                          (lane, iso(since)))
        return [(parse_dt(row["created_at"]), row["status"]) for row in rows]

    def list_runs(self, *, status: Iterable[RunStatus] | RunStatus | None = None, lane: str | None = None,
                  flow_id: str | None = None, schedule_id: str | None = None, workspace: str | None = None,
                  approval_id: str | None = None, due_before: datetime | None = None,
                  recheck_before: datetime | None = None, limit: int = 100, oldest_first: bool = False,
                  exclude_lanes: Iterable[str] = (), kind: RunKind | str | None = None) -> list[Run]:
        where, args = [], []
        skip = sorted(set(exclude_lanes))
        if skip:
            where.append(f"(lane IS NULL OR lane NOT IN ({','.join('?' * len(skip))}))")
            args += skip
        if status is not None:
            statuses = [status] if isinstance(status, RunStatus | str) else list(status)
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args += [RunStatus(s).value for s in statuses]
        for col, val in (("lane", lane), ("flow_id", flow_id), ("schedule_id", schedule_id),
                         ("workspace", workspace), ("approval_id", approval_id),
                         ("kind", RunKind(kind).value if kind is not None else None)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        if due_before is not None:
            where.append("(not_before IS NULL OR not_before <= ?)")
            args.append(iso(due_before))
        if recheck_before is not None:
            where.append("recheck_at IS NOT NULL AND recheck_at <= ?")
            args.append(iso(recheck_before))
        sql = "SELECT * FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY created_at {'ASC' if oldest_first else 'DESC'}, rowid {'ASC' if oldest_first else 'DESC'}"
        sql += " LIMIT ?"
        args.append(limit)
        out = []
        for r in self._rows(sql, args):
            try:
                out.append(self._run_from_row(r))
            except ValueError as e:  # pydantic's ValidationError, bad JSON, an unknown status
                self._skip_bad("run", r["id"], e)
        return out

    def queued_workspaces(self, due_before: datetime, exclude_lanes: Iterable[str] = ()) -> list[str]:
        """Workspaces with a queued run that is due and not on one of `exclude_lanes`."""
        skip = sorted(set(exclude_lanes))
        sql = "SELECT DISTINCT workspace FROM runs WHERE status=? AND (not_before IS NULL OR not_before <= ?)"
        args: list[Any] = [RunStatus.QUEUED.value, iso(due_before)]
        if skip:
            sql += f" AND (lane IS NULL OR lane NOT IN ({','.join('?' * len(skip))}))"
            args += skip
        return sorted(r["workspace"] or "_" for r in self._rows(sql, args))

    def set_cancel_requested(self, run_id: str) -> None:
        self._exec("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))

    def cancel_requested(self, run_id: str) -> bool:
        rows = self._rows("SELECT cancel_requested FROM runs WHERE id=?", (run_id,))
        return bool(rows and rows[0]["cancel_requested"])

    # ------------------------------------------------------------ step journal

    def _step_from_row(self, row: sqlite3.Row) -> StepRecord:
        return StepRecord(run_id=row["run_id"], path=row["path"], idx=row["idx"], type=row["type"],
                          status=StepStatus(row["status"]), attempts=row["attempts"],
                          started_at=parse_dt(row["started_at"]), finished_at=parse_dt(row["finished_at"]),
                          result=_uj(row["result"]), error=_uj(row["error"]))

    def steps(self, run_id: str) -> list[StepRecord]:
        rows = self._rows("SELECT * FROM steps WHERE run_id=? ORDER BY started_at, rowid", (run_id,))
        return [self._step_from_row(r) for r in rows]

    def step_start(self, run_id: str, path: str, idx: int, type_: str, at: datetime) -> None:
        self._exec(
            "INSERT INTO steps (run_id, path, idx, type, status, attempts, started_at) VALUES (?,?,?,?,?,1,?) "
            "ON CONFLICT(run_id, path) DO UPDATE SET status=excluded.status, attempts=steps.attempts+1, "
            "started_at=excluded.started_at, finished_at=NULL, error=NULL",
            (run_id, path, idx, type_, StepStatus.STARTED.value, iso(at)))

    def step_end(self, run_id: str, path: str, status: StepStatus, at: datetime, *,
                 result: Any = None, error: Any = None) -> None:
        self._exec("UPDATE steps SET status=?, finished_at=?, result=?, error=? WHERE run_id=? AND path=?",
                   (status.value, iso(at), _j(result), _j(error), run_id, path))

    def put_step_state(self, run_id: str, path: str, doc: dict[str, Any]) -> None:
        self._exec("INSERT INTO step_state (run_id, path, doc) VALUES (?,?,?) "
                   "ON CONFLICT(run_id, path) DO UPDATE SET doc=excluded.doc", (run_id, path, _j(doc)))

    def step_states(self, run_id: str) -> dict[str, dict[str, Any]]:
        return {r["path"]: json.loads(r["doc"])
                for r in self._rows("SELECT path, doc FROM step_state WHERE run_id=?", (run_id,))}

    def step_set(self, run_id: str, path: str, status: StepStatus, *, result: Any = None) -> None:
        with self._lock:
            cur = self._db.execute("UPDATE steps SET status=?, result=COALESCE(?, result) WHERE run_id=? AND path=?",
                                   (status.value, _j(result), run_id, path))
            if cur.rowcount == 0:
                self._db.execute("INSERT INTO steps (run_id, path, idx, type, status, result) VALUES (?,?,?,?,?,?)",
                                 (run_id, path, 0, "", status.value, _j(result)))

    # ------------------------------------------------------------ schedules

    def put_schedule(self, s: Schedule) -> None:
        token = s.webhook.token if s.webhook else None
        self._exec("INSERT INTO schedules (id, kind, flow_id, enabled, token, doc) VALUES (?,?,?,?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, flow_id=excluded.flow_id, "
                   "enabled=excluded.enabled, token=excluded.token, doc=excluded.doc",
                   (s.id, s.kind.value, s.flow_id or "", int(s.enabled), token, s.model_dump_json()))

    def _schedule_from_row(self, row: sqlite3.Row) -> Schedule | None:
        try:
            return Schedule.model_validate_json(row["doc"])
        except ValueError as e:
            self._skip_bad("schedule", row["id"], e)
            return None

    def get_schedule(self, sid: str) -> Schedule | None:
        rows = self._rows("SELECT id, doc FROM schedules WHERE id=?", (sid,))
        return self._schedule_from_row(rows[0]) if rows else None

    def has_schedule(self, sid: str) -> bool:
        """Whether the id is taken, even by a row that no longer parses."""
        return bool(self._rows("SELECT 1 FROM schedules WHERE id=?", (sid,)))

    def schedule_by_token(self, token: str) -> Schedule | None:
        rows = self._rows("SELECT id, doc FROM schedules WHERE token=?", (token,))
        return self._schedule_from_row(rows[0]) if rows else None

    def list_schedules(self, kind: str | None = None, enabled_only: bool = False) -> list[Schedule]:
        sql, args = "SELECT id, doc FROM schedules", []
        where = []
        if kind:
            where.append("kind=?")
            args.append(kind)
        if enabled_only:
            where.append("enabled=1")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        return [s for s in (self._schedule_from_row(r) for r in self._rows(sql, args)) if s is not None]

    def delete_schedule(self, sid: str) -> bool:
        return self._exec("DELETE FROM schedules WHERE id=?", (sid,)).rowcount > 0

    # ------------------------------------------------------------ surfaces (AUD-01)

    def put_surface(self, surface: Surface) -> None:
        self._exec("INSERT INTO surfaces (id, site, lane, doc) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                   "site=excluded.site, lane=excluded.lane, doc=excluded.doc",
                   (surface.id, surface.site, surface.lane, surface.model_dump_json()))

    def _surface_from_row(self, row: sqlite3.Row) -> Surface | None:
        try:
            return Surface.model_validate_json(row["doc"])
        except ValueError as e:
            self._skip_bad("surface", row["id"], e)
            return None

    def get_surface(self, surface_id: str) -> Surface | None:
        rows = self._rows("SELECT id, doc FROM surfaces WHERE id=?", (surface_id,))
        return self._surface_from_row(rows[0]) if rows else None

    def list_surfaces(self, *, site: str | None = None) -> list[Surface]:
        sql, args = "SELECT id, doc FROM surfaces", []
        if site:
            sql, args = sql + " WHERE site=?", [site]
        return [s for s in (self._surface_from_row(r) for r in self._rows(sql + " ORDER BY id", args)) if s]

    def delete_surface(self, surface_id: str) -> bool:
        return self._exec("DELETE FROM surfaces WHERE id=?", (surface_id,)).rowcount > 0

    # ------------------------------------------------------------ connector health

    def put_health(self, operation: str, surface: str, doc: dict[str, Any]) -> None:
        self._exec("INSERT INTO connector_health (operation, surface, doc) VALUES (?,?,?) "
                   "ON CONFLICT(operation, surface) DO UPDATE SET doc=excluded.doc",
                   (operation, surface, json.dumps(doc, default=str)))

    def get_health(self, operation: str, surface: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT doc FROM connector_health WHERE operation=? AND surface=?", (operation, surface))
        return json.loads(rows[0]["doc"]) if rows else None

    def list_health(self, operation: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT operation, surface, doc FROM connector_health", []
        if operation:
            sql, args = sql + " WHERE operation=?", [operation]
        return [{"operation": r["operation"], "surface": r["surface"], **json.loads(r["doc"])}
                for r in self._rows(sql + " ORDER BY operation, surface", args)]

    def delete_health(self, operation: str, surface: str | None = None) -> int:
        if surface is None:
            return self._exec("DELETE FROM connector_health WHERE operation=?", (operation,)).rowcount
        return self._exec("DELETE FROM connector_health WHERE operation=? AND surface=?",
                          (operation, surface)).rowcount

    def delete_health_surface(self, surface: str) -> int:
        return self._exec("DELETE FROM connector_health WHERE surface=?", (surface,)).rowcount

    # ------------------------------------------------------------ recordings

    def put_recording(self, lane: str, started_at: datetime, principal: dict, events: list[dict]) -> None:
        self._exec("INSERT INTO recordings (lane, started_at, principal, events) VALUES (?,?,?,?) "
                   "ON CONFLICT(lane) DO UPDATE SET started_at=excluded.started_at, "
                   "principal=excluded.principal, events=excluded.events",
                   (lane, iso(started_at), json.dumps(principal), json.dumps(events, default=str)))

    def get_recording(self, lane: str) -> dict | None:
        rows = self._rows("SELECT * FROM recordings WHERE lane=?", (lane,))
        if not rows:
            return None
        r = rows[0]
        return {"lane": r["lane"], "started_at": parse_dt(r["started_at"]),
                "principal": json.loads(r["principal"]), "events": json.loads(r["events"])}

    def delete_recording(self, lane: str) -> None:
        self._exec("DELETE FROM recordings WHERE lane=?", (lane,))
