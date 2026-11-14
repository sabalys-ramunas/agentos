"""engage.db: programs and their versions, personas, day plans, counters, owner caps, notices,
(schema 2, the executor) sessions, operations, holds and the per-surface history, (schema 3,
the items inbox) the owner's items, and (schema 4, the draft provider) drafts.

`$AGENTOS_HOME/engage/engage.db`, WAL, mode 600 in a 700 directory. The schema comes from
agentos.engage.migrations through the node's migrator, so `agentos migrate` and the store agree.

Times the executor writes are UTC ISO 8601 strings at whole seconds (`iso()`), so they compare
as text. Rows come back as plain dicts.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from agentos.core.errors import ServiceUnavailable
from agentos.engage.caps import Kind
from agentos.engage.errors import ProgramInvalid
from agentos.engage.migrations import DB, MIGRATIONS
from agentos.engage.models import DayPlan, Program, ProgramState, ProgramVersion
from agentos.engage.persona import Persona
from agentos.ops import migrate


def _now() -> str:
    return datetime.now(UTC).isoformat()


def iso(t: datetime) -> str:
    """A UTC timestamp as the executor tables keep it (whole seconds, comparable as text)."""
    return t.astimezone(UTC).isoformat(timespec="seconds")


def _history(conn: sqlite3.Connection, surface: str, kind: str, payload: dict[str, Any] | None, *, at: datetime,
             by: str | None = None) -> int:
    """One history row, inside the caller's transaction."""
    cur = conn.execute("INSERT INTO history (surface, at, kind, by, payload) VALUES (?, ?, ?, ?, ?)",
                       (surface, iso(at), kind, by, json.dumps(payload or {}, sort_keys=True, default=str)))
    return int(cur.lastrowid or 0)


# Operation statuses (ops.status). `planned` is waiting for its window; `skipped` never reached a
# connector (the reason says why); `attempted` is written right before the connector call, so a
# crash leaves a trace; the rest are the connector's outcome.
OP_STATUSES = ("planned", "skipped", "attempted", "verified", "unverified", "failed", "held", "awaiting_approval")
# What counts against a cap: every operation that reached a connector, whatever came of it.
SPENT = ("attempted", "verified", "unverified", "failed", "awaiting_approval")
# Session statuses (sessions.status).
SESSION_STATUSES = ("scheduled", "running", "done", "ended_early", "aborted", "refused", "missed")
SESSION_OPEN = ("scheduled",)
# Item statuses (items.status, schema 3).
ITEM_STATUSES = ("queued", "reserved", "awaiting", "used", "failed", "expired", "withdrawn")
ITEM_JSON = ("targets", "attachments", "files", "requested_by")


class EngageStore:
    def __init__(self, home: Path):
        self.path = Path(home) / DB
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.parent.chmod(0o700)
            migrate.migrate(Path(home), [MIGRATIONS], backup=self.path.exists())
            self.path.chmod(0o600)
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        except (OSError, sqlite3.Error, migrate.MigrationError) as e:
            raise ServiceUnavailable(f"cannot open {self.path}: {e}") from None
        self._lock = threading.Lock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        # agentos.profiles.service.ProfilesService once the node builds it: an account attached to a
        # profile takes its wanted profile and its voice from there (agentos.engage.profile.effective)
        self.profiles: Any = None

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _dicts(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows(sql, args)]

    # ---- programs ----
    @staticmethod
    def _version(row: sqlite3.Row) -> ProgramVersion:
        return ProgramVersion(
            definition=Program.model_validate(json.loads(row["body"])), version=row["version"], hash=row["hash"],
            state=ProgramState(row["state"]), created_at=row["created_at"],
            created_by=json.loads(row["created_by"]) if row["created_by"] else None,
            approved_at=row["approved_at"], approved_by=row["approved_by"],
            starts_on=date.fromisoformat(row["starts_on"]) if row["starts_on"] else None)

    def put_program(self, program: Program, *, created_by: dict[str, Any] | None) -> tuple[ProgramVersion, bool]:
        """Save `program` as the next version of its id; a program equal to a saved version
        returns that version. Returns (version, created). A program id belongs to one surface."""
        h = program.content_hash()
        with self._tx() as conn:
            same = conn.execute("SELECT * FROM programs WHERE id = ? AND hash = ?", (program.id, h)).fetchone()
            if same is not None:
                return self._version(same), False
            other = conn.execute("SELECT surface FROM programs WHERE id = ? LIMIT 1", (program.id,)).fetchone()
            if other is not None and other["surface"] != program.surface:
                raise ProgramInvalid(f"program {program.id} belongs to {other['surface']}; pick another name")
            n = conn.execute("SELECT COALESCE(MAX(version), 0) FROM programs WHERE id = ?", (program.id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO programs (id, version, hash, surface, body, state, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (program.id, n + 1, h, program.surface, json.dumps(program.canonical(), sort_keys=True),
                 ProgramState.DRAFT.value, _now(), json.dumps(created_by) if created_by else None))
            row = conn.execute("SELECT * FROM programs WHERE id = ? AND version = ?", (program.id, n + 1)).fetchone()
        return self._version(row), True

    def versions(self, program_id: str) -> list[ProgramVersion]:
        return [self._version(r) for r in self._rows("SELECT * FROM programs WHERE id = ? ORDER BY version",
                                                     (program_id,))]

    def programs(self, surface: str | None = None) -> list[ProgramVersion]:
        """The latest version of every program (of one surface)."""
        sql = ("SELECT p.* FROM programs p JOIN (SELECT id, MAX(version) v FROM programs GROUP BY id) m "
               "ON p.id = m.id AND p.version = m.v")
        rows = self._rows(sql + (" WHERE p.surface = ?" if surface else "") + " ORDER BY p.surface, p.id",
                          (surface,) if surface else ())
        return [self._version(r) for r in rows]

    def active(self, surface: str) -> ProgramVersion | None:
        rows = self._rows("SELECT * FROM programs WHERE surface = ? AND state = ? ORDER BY approved_at DESC LIMIT 1",
                          (surface, ProgramState.APPROVED.value))
        return self._version(rows[0]) if rows else None

    def approved(self) -> list[ProgramVersion]:
        return [self._version(r) for r in self._rows("SELECT * FROM programs WHERE state = ? ORDER BY surface",
                                                     (ProgramState.APPROVED.value,))]

    def approve(self, program_id: str, version: int, *, by: str, starts_on: date) -> ProgramVersion:
        """Mark one version approved. Any other approved program on the surface is superseded, so a
        surface runs one program. A program already running keeps its day 1."""
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM programs WHERE id = ? AND version = ?", (program_id, version)).fetchone()
            surface = row["surface"]
            running = conn.execute("SELECT starts_on FROM programs WHERE id = ? AND state = ? LIMIT 1",
                                   (program_id, ProgramState.APPROVED.value)).fetchone()
            first = running["starts_on"] if running is not None and running["starts_on"] else starts_on.isoformat()
            conn.execute("UPDATE programs SET state = ? WHERE surface = ? AND state = ?",
                         (ProgramState.SUPERSEDED.value, surface, ProgramState.APPROVED.value))
            conn.execute("UPDATE programs SET state = ?, approved_at = ?, approved_by = ?, starts_on = ? "
                         "WHERE id = ? AND version = ?",
                         (ProgramState.APPROVED.value, _now(), by, first, program_id, version))
            row = conn.execute("SELECT * FROM programs WHERE id = ? AND version = ?", (program_id, version)).fetchone()
        return self._version(row)

    # ---- personas ----
    def persona(self, surface: str) -> Persona | None:
        rows = self._rows("SELECT body FROM personas WHERE surface = ?", (surface,))
        return Persona.model_validate(json.loads(rows[0]["body"])) if rows else None

    def put_persona(self, persona: Persona) -> None:
        now = _now()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO personas (surface, seed, redraws, body, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(surface) DO UPDATE SET seed = excluded.seed, redraws = excluded.redraws, "
                "body = excluded.body, updated_at = excluded.updated_at",
                (persona.surface, persona.seed, persona.redraws, persona.model_dump_json(), now, now))

    # ---- plans and counters ----
    def put_plan(self, plan: DayPlan) -> None:
        day = plan.day.isoformat()
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO plans (surface, day, program_hash, body, created_at) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (plan.surface, day, plan.program_hash, plan.model_dump_json(), _now()))
            conn.execute("UPDATE counters SET planned = 0 WHERE surface = ? AND day = ?", (plan.surface, day))
            for kind, n in plan.totals().items():
                conn.execute("INSERT INTO counters (surface, day, kind, planned) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(surface, day, kind) DO UPDATE SET planned = excluded.planned",
                             (plan.surface, day, kind.value, n))

    def plans(self, surface: str, since: date, until: date) -> list[DayPlan]:
        rows = self._rows("SELECT body FROM plans WHERE surface = ? AND day >= ? AND day <= ? ORDER BY day",
                          (surface, since.isoformat(), until.isoformat()))
        return [DayPlan.model_validate_json(r["body"]) for r in rows]

    def counters(self, surface: str, day: date) -> dict[Kind, dict[str, int]]:
        rows = self._rows("SELECT kind, planned, done FROM counters WHERE surface = ? AND day = ?",
                          (surface, day.isoformat()))
        return {Kind(r["kind"]): {"planned": r["planned"], "done": r["done"]} for r in rows}

    # ---- owner caps ----
    def caps(self, surface: str) -> dict[Kind, int]:
        return {Kind(r["kind"]): r["value"] for r in self._rows("SELECT kind, value FROM caps WHERE surface = ?",
                                                                (surface,))}

    def set_cap(self, surface: str, kind: Kind, value: int | None, *, by: str) -> None:
        with self._tx() as conn:
            if value is None:
                conn.execute("DELETE FROM caps WHERE surface = ? AND kind = ?", (surface, kind.value))
            else:
                conn.execute("INSERT OR REPLACE INTO caps (surface, kind, value, set_by, set_at) VALUES (?, ?, ?, ?, ?)",
                             (surface, kind.value, int(value), by, _now()))

    # ---- notices and meta ----
    def notice(self, kind: str, text: str, surface: str | None = None) -> None:
        with self._tx() as conn:
            conn.execute("INSERT INTO notices (at, surface, kind, text) VALUES (?, ?, ?, ?)",
                         (_now(), surface, kind, text))

    def notices(self, surface: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql = "SELECT at, surface, kind, text FROM notices"
        args: tuple = ()
        if surface:
            sql += " WHERE surface = ? OR surface IS NULL"
            args = (surface,)
        rows = self._rows(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [dict(r) for r in rows]

    def meta(self, key: str) -> str | None:
        rows = self._rows("SELECT v FROM meta WHERE k = ?", (key,))
        return rows[0]["v"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, value))

    # ---- sessions (schema 2) ----
    def session(self, surface: str, day: date, idx: int) -> dict[str, Any] | None:
        rows = self._dicts("SELECT * FROM sessions WHERE surface = ? AND day = ? AND idx = ?",
                           (surface, day.isoformat(), idx))
        return rows[0] if rows else None

    def session_by_id(self, session_id: int) -> dict[str, Any] | None:
        rows = self._dicts("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return rows[0] if rows else None

    def sessions(self, surface: str, day: date | None = None, *, origin: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM sessions WHERE surface = ?", [surface]
        if day is not None:
            sql, args = sql + " AND day = ?", [*args, day.isoformat()]
        if origin is not None:
            sql, args = sql + " AND origin = ?", [*args, origin]
        return self._dicts(sql + " ORDER BY day, idx", tuple(args))

    def running_sessions(self) -> list[dict[str, Any]]:
        """Every surface's sessions still marked running (one the node may have left behind)."""
        return self._dicts("SELECT * FROM sessions WHERE status = 'running' ORDER BY day, surface, idx")

    def put_session(self, surface: str, day: date, idx: int, *, planned_start: datetime, minutes: float,
                    origin: str = "plan") -> dict[str, Any]:
        """The window's row, created as `scheduled` when it has none. An existing row keeps its
        status; a `scheduled` one takes the new start (the owner's run --now)."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO sessions (surface, day, idx, origin, planned_start, minutes) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(surface, day, idx) DO UPDATE SET planned_start = excluded.planned_start, "
                "minutes = excluded.minutes WHERE sessions.status = 'scheduled'",
                (surface, day.isoformat(), idx, origin, iso(planned_start), float(minutes)))
            row = conn.execute("SELECT * FROM sessions WHERE surface = ? AND day = ? AND idx = ?",
                               (surface, day.isoformat(), idx)).fetchone()
        return dict(row)

    def set_session(self, session_id: int, **fields: Any) -> None:
        allowed = {"status", "end_reason", "started_at", "ended_at", "planned_start", "minutes"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown session fields {sorted(bad)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as conn:
            conn.execute(f"UPDATE sessions SET {cols} WHERE id = ?", (*fields.values(), session_id))

    # ---- operations (schema 2) ----
    def add_ops(self, session_id: int, surface: str, day: date, kinds: list[str], *,
                moved_from: list[int | None] | None = None) -> list[dict[str, Any]]:
        """Planned rows for a window's operations, in order (seq continues after existing rows)."""
        with self._tx() as conn:
            start = conn.execute("SELECT COALESCE(MAX(seq), -1) FROM ops WHERE session_id = ?",
                                 (session_id,)).fetchone()[0] + 1
            for i, kind in enumerate(kinds):
                conn.execute("INSERT INTO ops (session_id, seq, surface, day, kind, moved_from) VALUES (?, ?, ?, ?, ?, ?)",
                             (session_id, start + i, surface, day.isoformat(), str(kind),
                              (moved_from or [None] * len(kinds))[i]))
        return self.ops(session_id=session_id)

    def op_by_content(self, origin: str, content_id: str) -> dict[str, Any] | None:
        """The newest operation row that carried this content (an item or a draft)."""
        rows = self._dicts("SELECT * FROM ops WHERE content_origin = ? AND content_id = ? ORDER BY id DESC LIMIT 1",
                           (str(origin), content_id))
        return rows[0] if rows else None

    def op_by_run(self, run_id: str) -> dict[str, Any] | None:
        """The newest operation row of a connector run."""
        rows = self._dicts("SELECT * FROM ops WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,))
        return rows[0] if rows else None

    def ops(self, surface: str | None = None, day: date | None = None, *, session_id: int | None = None,
            status: str | tuple[str, ...] | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM ops WHERE 1 = 1", []
        for col, val in (("surface", surface), ("day", day.isoformat() if day else None),
                         ("session_id", session_id), ("kind", str(kind) if kind else None)):
            if val is not None:
                sql, args = sql + f" AND {col} = ?", [*args, val]
        if status is not None:
            statuses = (status,) if isinstance(status, str) else tuple(status)
            sql += f" AND status IN ({', '.join('?' * len(statuses))})"
            args.extend(statuses)
        return self._dicts(sql + " ORDER BY day, session_id, seq", tuple(args))

    def set_op(self, op_id: int, **fields: Any) -> None:
        allowed = {"status", "reason", "run_id", "rule_id", "target", "content_origin", "content_id", "at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown op fields {sorted(bad)}")
        if "status" in fields and fields["status"] not in OP_STATUSES:
            raise ValueError(f"unknown op status {fields['status']!r}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as conn:
            conn.execute(f"UPDATE ops SET {cols} WHERE id = ?", (*fields.values(), op_id))

    def count_ops(self, surface: str, kind: str, *, day: date | None = None, since: datetime | None = None,
                  status: tuple[str, ...] = SPENT) -> int:
        """Operations of a kind in `status` (by default: every one that reached a connector), on a
        local `day` or done at or after `since`."""
        sql = f"SELECT COUNT(*) FROM ops WHERE surface = ? AND kind = ? AND status IN ({', '.join('?' * len(status))})"
        args: list[Any] = [surface, str(kind), *status]
        if day is not None:
            sql, args = sql + " AND day = ?", [*args, day.isoformat()]
        if since is not None:
            sql, args = sql + " AND at >= ?", [*args, iso(since)]
        return int(self._rows(sql, tuple(args))[0][0])

    def add_done(self, surface: str, day: date, kind: str, n: int = 1) -> None:
        """counters.done for a verified operation."""
        with self._tx() as conn:
            conn.execute("INSERT INTO counters (surface, day, kind, done) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(surface, day, kind) DO UPDATE SET done = done + excluded.done",
                         (surface, day.isoformat(), str(kind), n))

    # ---- holds (schema 2) ----
    def add_hold(self, surface: str, *, reason: str, since: datetime, signal: str | None = None,
                 kind: str | None = None, until: datetime | None = None, detail: str | None = None) -> int:
        with self._tx() as conn:
            cur = conn.execute("INSERT INTO holds (surface, kind, signal, reason, since, until, detail) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (surface, str(kind) if kind else None, signal, reason, iso(since),
                                iso(until) if until else None, detail))
            return int(cur.lastrowid or 0)

    def holds(self, surface: str | None = None, *, at: datetime | None = None,
              include_cleared: bool = False) -> list[dict[str, Any]]:
        """Holds of a surface (every surface when None). With `at`, only those in force then:
        not cleared, and with no `until` or one after `at`."""
        sql, args = "SELECT * FROM holds WHERE 1 = 1", []
        if surface is not None:
            sql, args = sql + " AND surface = ?", [surface]
        if not include_cleared:
            sql += " AND cleared_at IS NULL"
        if at is not None:
            sql, args = sql + " AND (until IS NULL OR until > ?)", [*args, iso(at)]
        return self._dicts(sql + " ORDER BY id", tuple(args))

    def clear_holds(self, surface: str, *, by: str, at: datetime, kind: str | None = None,
                    hold_id: int | None = None) -> int:
        sql, args = "UPDATE holds SET cleared_at = ?, cleared_by = ? WHERE surface = ? AND cleared_at IS NULL", \
            [iso(at), by, surface]
        if kind is not None:
            sql, args = sql + " AND kind = ?", [*args, str(kind)]
        if hold_id is not None:
            sql, args = sql + " AND id = ?", [*args, hold_id]
        with self._tx() as conn:
            return conn.execute(sql, tuple(args)).rowcount

    # ---- history (schema 2) ----
    def add_history(self, surface: str, kind: str, payload: dict[str, Any] | None = None, *, at: datetime,
                    by: str | None = None) -> int:
        """One entry of the surface's history: what happened, as a small JSON payload."""
        with self._tx() as conn:
            return _history(conn, surface, kind, payload, at=at, by=by)

    def history(self, surface: str, *, kinds: tuple[str, ...] | None = None, since: datetime | None = None,
                limit: int = 100) -> list[dict[str, Any]]:
        """Newest first."""
        sql, args = "SELECT * FROM history WHERE surface = ?", [surface]
        if kinds:
            sql += f" AND kind IN ({', '.join('?' * len(kinds))})"
            args.extend(kinds)
        if since is not None:
            sql, args = sql + " AND at >= ?", [*args, iso(since)]
        rows = self._dicts(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        for r in rows:
            r["payload"] = json.loads(r["payload"] or "{}")
        return rows

    # ---- items (schema 3) ----
    @staticmethod
    def _item(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for k in ITEM_JSON:
            out[k] = json.loads(out[k]) if out.get(k) else (None if k == "requested_by" else [])
        return out

    def add_item(self, item: dict[str, Any], *, history: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Queue one item and write `item_submitted` (with `history`) in the same transaction.
        `item` has id, surface, kind, text, title, targets, attachments, files, expires_at,
        submitted_by, submitted_at, and optionally requested_by and approval_id. None when an item
        already holds that approval id (an approval queues one item, once)."""
        with self._tx() as conn:
            if item.get("approval_id") and conn.execute("SELECT 1 FROM items WHERE approval_id = ?",
                                                        (item["approval_id"],)).fetchone():
                return None
            conn.execute(
                "INSERT INTO items (id, surface, kind, text, title, targets, attachments, files, expires_at, status, "
                "submitted_by, submitted_at, requested_by, approval_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                (item["id"], item["surface"], str(item["kind"]), item["text"], item.get("title"),
                 json.dumps(list(item.get("targets") or [])), json.dumps(list(item.get("attachments") or [])),
                 json.dumps(list(item.get("files") or []), sort_keys=True),
                 item["expires_at"], item["submitted_by"], item["submitted_at"],
                 json.dumps(item["requested_by"], sort_keys=True) if item.get("requested_by") else None,
                 item.get("approval_id")))
            _history(conn, item["surface"], "item_submitted", {"id": item["id"], **(history or {})},
                     at=datetime.fromisoformat(item["submitted_at"]), by=item["submitted_by"])
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item["id"],)).fetchone()
        return self._item(row)

    def item(self, item_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM items WHERE id = ?", (item_id,))
        return self._item(rows[0]) if rows else None

    def item_surfaces(self) -> list[str]:
        return [r["surface"] for r in self._rows("SELECT DISTINCT surface FROM items ORDER BY surface")]

    def items(self, surface: str | None = None, *, surfaces: Iterable[str] | None = None,
              status: str | tuple[str, ...] | None = None, kind: str | None = None,
              limit: int = 200) -> list[dict[str, Any]]:
        """Oldest first. `surfaces` keeps only items of those surfaces (none when it is empty)."""
        sql, args = "SELECT * FROM items WHERE 1 = 1", []
        for col, val in (("surface", surface), ("kind", str(kind) if kind else None)):
            if val is not None:
                sql, args = sql + f" AND {col} = ?", [*args, val]
        if surfaces is not None:
            allowed = sorted(set(surfaces))
            if not allowed:
                return []
            sql += f" AND surface IN ({', '.join('?' * len(allowed))})"
            args.extend(allowed)
        if status is not None:
            statuses = (status,) if isinstance(status, str) else tuple(status)
            sql += f" AND status IN ({', '.join('?' * len(statuses))})"
            args.extend(statuses)
        return [self._item(r) for r in self._rows(sql + " ORDER BY submitted_at, rowid LIMIT ?", (*args, limit))]

    def reserve_items(self, surface: str, kind: str, n: int, *, now: datetime,
                      session_id: int | None = None) -> list[dict[str, Any]]:
        """Reserve up to `n` of the oldest queued, unexpired items of (surface, kind) for the
        session `session_id` (the window that takes them; None when no window is named), in one
        IMMEDIATE transaction: two ticks (or two processes) never reserve the same item."""
        if n <= 0:
            return []
        at = iso(now)
        with self._tx() as conn:
            rows = conn.execute("SELECT id FROM items WHERE surface = ? AND kind = ? AND status = 'queued' "
                                "AND expires_at > ? ORDER BY submitted_at, rowid LIMIT ?",
                                (surface, str(kind), at, n)).fetchall()
            got = []
            for r in rows:
                cur = conn.execute("UPDATE items SET status = 'reserved', reserved_by_window = ?, reserved_at = ? "
                                   "WHERE id = ? AND status = 'queued'", (session_id, at, r["id"]))
                if cur.rowcount == 1:
                    got.append(r["id"])
            out = [conn.execute("SELECT * FROM items WHERE id = ?", (i,)).fetchone() for i in got]
        return [self._item(r) for r in out]

    def settle_item(self, item_id: str, to: str, *, now: datetime, run_id: str | None = None,
                    history: tuple[str, dict[str, Any]] | None = None) -> bool:
        """Move a taken item to `to`, writing `history` (kind, payload) in the same transaction:
        `queued` (handed back: only from reserved or awaiting), `awaiting` (its run waits for an
        approval card), `used` or `failed` (the run may have acted). A run that went out is
        recorded whatever the item's state (a sweep may have released it meanwhile), except over
        `used`. False when nothing changed."""
        sources = {"queued": ("reserved", "awaiting"),
                   "awaiting": ("reserved", "queued", "expired", "withdrawn"),
                   "used": ("reserved", "awaiting", "queued", "expired", "withdrawn", "failed"),
                   "failed": ("reserved", "awaiting", "queued", "expired", "withdrawn")}[to]
        sets = {"queued": "reserved_by_window = NULL, reserved_at = NULL, used_run_id = NULL, settled_at = NULL",
                "awaiting": "used_run_id = ?", "used": "used_run_id = COALESCE(?, used_run_id), settled_at = ?",
                "failed": "used_run_id = COALESCE(?, used_run_id), settled_at = ?"}[to]
        args: tuple = {"queued": (), "awaiting": (run_id,), "used": (run_id, iso(now)),
                       "failed": (run_id, iso(now))}[to]
        with self._tx() as conn:
            row = conn.execute("SELECT surface FROM items WHERE id = ?", (item_id,)).fetchone()
            cur = conn.execute(f"UPDATE items SET status = ?, {sets} WHERE id = ? AND status IN "
                               f"({', '.join('?' * len(sources))})", (to, *args, item_id, *sources))
            if cur.rowcount != 1:
                return False
            if history is not None:
                _history(conn, row["surface"], history[0], history[1], at=now)
            return True

    def expire_items(self, *, now: datetime) -> list[dict[str, Any]]:
        """Queued items past their expiry become `expired`, each with `item_expired` in the
        history; returns them."""
        at = iso(now)
        with self._tx() as conn:
            rows = conn.execute("SELECT * FROM items WHERE status = 'queued' AND expires_at <= ?", (at,)).fetchall()
            for r in rows:
                conn.execute("UPDATE items SET status = 'expired', settled_at = ? WHERE id = ? AND status = 'queued'",
                             (at, r["id"]))
                _history(conn, r["surface"], "item_expired", {"id": r["id"], "kind": r["kind"],
                                                              "expires_at": r["expires_at"]}, at=now)
        return [self._item(r) for r in rows]

    def release_items(self, *, now: datetime, stale_before: datetime, grace: timedelta) -> list[dict[str, Any]]:
        """Reserved items whose window is over without settling them: its session is no longer
        running (or is gone), its planned end (from when it started, if later) plus `grace` has
        passed (a cancelled or crashed window that still reads `running`), or no session was
        named and they were reserved before `stale_before`. Each goes back to `queued`
        (`item_released`), or to `expired` (`item_expired`) when it is past its expiry, in one
        transaction with its history. Returns them as they were (still `reserved`), each with
        `released_to`."""
        at = iso(now)
        out: list[dict[str, Any]] = []
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT i.*, s.id AS s_id, s.status AS s_status, s.planned_start AS s_start, "
                "s.started_at AS s_started, s.minutes AS s_minutes FROM items i "
                "LEFT JOIN sessions s ON s.id = i.reserved_by_window WHERE i.status = 'reserved'").fetchall()
            for r in rows:
                if r["reserved_by_window"] is None:
                    over = (r["reserved_at"] or "") < iso(stale_before)
                elif r["s_id"] is None or r["s_status"] != "running":
                    over = True
                else:
                    start = max(datetime.fromisoformat(r["s_start"]),
                                datetime.fromisoformat(r["s_started"] or r["s_start"]))
                    over = start + timedelta(minutes=float(r["s_minutes"] or 0)) + grace <= now
                if not over:
                    continue
                # an operation already tried it (its newest has a run, or is still `attempted`: the
                # window stopped mid-run): it may have gone out, so it is `failed`, never queued again
                op = conn.execute("SELECT status, run_id FROM ops WHERE content_origin = 'item' AND content_id = ? "
                                  "ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
                if op is not None and (op["run_id"] is not None or op["status"] == "attempted"):
                    conn.execute("UPDATE items SET status = 'failed', used_run_id = COALESCE(?, used_run_id), "
                                 "settled_at = ? WHERE id = ? AND status = 'reserved'", (op["run_id"], at, r["id"]))
                    _history(conn, r["surface"], "item_failed", {"id": r["id"], "run_id": op["run_id"],
                                                                 "reason": "its window stopped while an operation "
                                                                           "was trying it"}, at=now)
                    item = self._item({k: v for k, v in dict(r).items() if not k.startswith("s_")})
                    out.append(item | {"released_to": "failed"})
                    continue
                to = "expired" if r["expires_at"] <= at else "queued"
                conn.execute("UPDATE items SET status = ?, reserved_by_window = NULL, reserved_at = NULL, "
                             "settled_at = ? WHERE id = ? AND status = 'reserved'",
                             (to, at if to == "expired" else None, r["id"]))
                payload = {"id": r["id"], "kind": r["kind"], "window": r["reserved_by_window"]}
                if to == "expired":
                    payload |= {"expires_at": r["expires_at"], "reason": "its window ended without using it"}
                _history(conn, r["surface"], "item_expired" if to == "expired" else "item_released", payload, at=now)
                item = self._item({k: v for k, v in dict(r).items() if not k.startswith("s_")})
                out.append(item | {"released_to": to})
        return out

    def awaiting_items(self) -> list[dict[str, Any]]:
        """Items whose run waits for an approval card, each with its operation's status and
        reason now (`op_status`, `op_reason`; None when no operation has the run)."""
        rows = self._rows("SELECT i.*, o.status AS op_status, o.reason AS op_reason FROM items i "
                          "LEFT JOIN ops o ON o.run_id = i.used_run_id WHERE i.status = 'awaiting'")
        return [self._item({k: v for k, v in dict(r).items() if not k.startswith("op_")})
                | {"op_status": r["op_status"], "op_reason": r["op_reason"]} for r in rows]

    def withdraw_item(self, item_id: str, *, now: datetime, by: str, history: dict[str, Any]) -> bool:
        """A queued item becomes `withdrawn`, with `item_withdrawn` in the same transaction.
        False when it is not queued any more."""
        with self._tx() as conn:
            row = conn.execute("SELECT surface FROM items WHERE id = ?", (item_id,)).fetchone()
            cur = conn.execute("UPDATE items SET status = 'withdrawn', settled_at = ? WHERE id = ? AND status = 'queued'",
                               (iso(now), item_id))
            if cur.rowcount != 1:
                return False
            _history(conn, row["surface"], "item_withdrawn", history, at=now, by=by)
            return True

    def item_counts(self, surface: str) -> dict[str, int]:
        rows = self._rows("SELECT status, COUNT(*) n FROM items WHERE surface = ? GROUP BY status", (surface,))
        return {r["status"]: r["n"] for r in rows}
