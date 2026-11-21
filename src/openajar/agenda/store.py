"""SQLite storage for agenda entries.

One file (`$OPENAJAR_HOME/agenda.db`), WAL mode, `synchronous=FULL`, versioned like the other
node stores (a `schema_version` table: a file written by a newer openajar is refused with a
message, never misread). An entry is one JSON document plus the columns the timeline queries
by. State changes are compare-and-set on the state column (`put(..., expect=...)`), so the
daemon's tick and a CLI in another process never both move the same entry. A row that cannot
be read is skipped and logged once, so one bad row never stops the agenda.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from openajar.agenda.models import Entry, EntryState
from openajar.core.errors import ServiceUnavailable
from openajar.runner.util import iso

log = logging.getLogger("openajar.agenda")

DB_NAME = "agenda.db"
SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
  id TEXT PRIMARY KEY, workspace TEXT NOT NULL, state TEXT NOT NULL, kind TEXT NOT NULL,
  lane TEXT, not_before TEXT, not_after TEXT, run_id TEXT, approval_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS entries_ws ON entries(workspace, not_before);
CREATE INDEX IF NOT EXISTS entries_state ON entries(state);
CREATE INDEX IF NOT EXISTS entries_run ON entries(run_id);
"""
# columns added after the first release: (table, column, type)
COLUMNS: list[tuple[str, str, str]] = []


class AgendaStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        memory = str(path) == ":memory:"
        if not memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        except sqlite3.DatabaseError as exc:
            raise self._unreadable(exc) from None
        self._db.row_factory = sqlite3.Row
        with self._guard():
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.executescript(SCHEMA)
            self._check_version()
            self._migrate()
        self._bad: set[str] = set()
        if not memory:
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

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
            f"{self.path.name} could not be read or written ({exc}). If the file is damaged, restore it from a "
            "backup or move it aside and restart. Runs, schedules and lanes are not affected.")

    def _check_version(self) -> None:
        row = self._db.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            self._db.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif row["version"] > SCHEMA_VERSION:
            raise ServiceUnavailable(
                f"{self.path.name} was written by a newer openajar (schema {row['version']}, this build reads "
                f"{SCHEMA_VERSION}). Upgrade openajar, or restore an older copy of the file.")

    def _migrate(self) -> None:
        for table, column, type_ in COLUMNS:
            have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")

    # ------------------------------------------------------------ rows

    @staticmethod
    def _args(e: Entry) -> dict:
        return {"id": e.id, "workspace": e.workspace, "state": e.state.value, "kind": e.action.kind.value,
                "lane": e.action.acts_on, "not_before": iso(e.when.not_before), "not_after": iso(e.when.not_after),
                "run_id": e.run_id, "approval_id": (e.approval or {}).get("id"), "created_at": iso(e.created_at),
                "updated_at": iso(e.updated_at), "doc": e.model_dump_json()}

    def insert(self, e: Entry) -> None:
        with self._lock, self._guard():
            self._db.execute(
                "INSERT INTO entries (id, workspace, state, kind, lane, not_before, not_after, run_id, approval_id, "
                "created_at, updated_at, doc) VALUES (:id, :workspace, :state, :kind, :lane, :not_before, :not_after, "
                ":run_id, :approval_id, :created_at, :updated_at, :doc)", self._args(e))

    def put(self, e: Entry, *, expect: Iterable[EntryState] | None = None) -> bool:
        """Write the entry. With `expect`, only while its stored state is one of them (compare and
        set); False when another writer moved it first."""
        args = self._args(e)
        cols = ("workspace", "state", "kind", "lane", "not_before", "not_after", "run_id", "approval_id",
                "updated_at", "doc")
        sql = f"UPDATE entries SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?"
        values = [args[c] for c in cols] + [e.id]
        if expect is not None:
            states = [s.value for s in expect]
            sql += f" AND state IN ({', '.join('?' * len(states))})"
            values += states
        with self._lock, self._guard():
            return self._db.execute(sql, values).rowcount == 1

    def _entry(self, row: sqlite3.Row) -> Entry | None:
        try:
            return Entry.model_validate(json.loads(row["doc"]))
        except (ValueError, ValidationError) as exc:
            if row["id"] not in self._bad:
                self._bad.add(row["id"])
                log.error("skipping unreadable agenda entry %s: %s", row["id"], str(exc)[:300])
            return None

    def get(self, entry_id: str) -> Entry | None:
        with self._lock, self._guard():
            row = self._db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return self._entry(row) if row else None

    def by_run(self, run_id: str) -> Entry | None:
        with self._lock, self._guard():
            row = self._db.execute("SELECT * FROM entries WHERE run_id = ?", (run_id,)).fetchone()
        return self._entry(row) if row else None

    def has(self, entry_id: str) -> bool:
        with self._lock, self._guard():
            return self._db.execute("SELECT 1 FROM entries WHERE id = ?", (entry_id,)).fetchone() is not None

    def list(self, *, workspace: str | None = None, states: Iterable[EntryState] | None = None,
             start: datetime | None = None, end: datetime | None = None, limit: int = 1000) -> list[Entry]:
        """Entries, by planned time. With a range, those whose window meets [start, end), plus
        those with no time of their own (as soon as approved) that are still open, plus those
        that finished inside the range."""
        sql, args = "SELECT * FROM entries WHERE 1=1", []
        if workspace is not None:
            sql += " AND workspace = ?"
            args.append(workspace)
        if states is not None:
            vals = [s.value for s in states]
            if not vals:
                return []
            sql += f" AND state IN ({', '.join('?' * len(vals))})"
            args += vals
        if start is not None and end is not None:
            open_ = [s.value for s in (EntryState.DRAFT, EntryState.PROPOSED, EntryState.SCHEDULED,
                                       EntryState.RUNNING)]
            sql += (" AND ((not_before IS NOT NULL AND not_before < ? AND (not_after IS NULL OR not_after >= ?))"
                    " OR (not_before IS NULL AND (not_after IS NULL OR not_after >= ?)"
                    f" AND state IN ({', '.join('?' * len(open_))}))"
                    " OR (not_before IS NULL AND updated_at >= ? AND updated_at < ?))")
            args += [iso(end), iso(start), iso(start), *open_, iso(start), iso(end)]
        sql += " ORDER BY COALESCE(not_before, created_at), id LIMIT ?"
        args.append(int(limit))
        with self._lock, self._guard():
            rows = self._db.execute(sql, args).fetchall()
        return [e for e in (self._entry(r) for r in rows) if e is not None]

    def delete(self, entry_id: str) -> bool:
        with self._lock, self._guard():
            return self._db.execute("DELETE FROM entries WHERE id = ?", (entry_id,)).rowcount == 1
