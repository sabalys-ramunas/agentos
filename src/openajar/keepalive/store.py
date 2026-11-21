"""keepalive.db: each lane's keepalive seed and status, the visits made, and the node's next free time.

`$OPENAJAR_HOME/keepalive/keepalive.db`, WAL, mode 600 in a 700 directory. The schema comes from
openajar.keepalive.migrations through the node's migrator.

A lane's opt-out lives in the lane's own directory (`lanes/<ws>/<name>/keepalive-off`, OPT_OUT), so it
survives losing keepalive.db; the `opted_out` column only mirrors it. The CLI writes both while
agentosd runs; the service reads the file again every round.

A visit row is written when the visit starts (outcome `started`) and completed when it ends, so a
visit agentosd did not live to finish still counts (`interrupted`, see interrupt_started).

A visit row holds names, times, counts and a plain reason: never a page's text, a URL's query or a
cookie value.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openajar.core import clock, migrate
from openajar.core.errors import ServiceUnavailable
from openajar.core.sqlite import SqliteStore
from openajar.keepalive.migrations import DB, MIGRATIONS

OPT_OUT = "keepalive-off"   # in the lane's directory: the lane opted out of keepalive
STARTED, INTERRUPTED = "started", "interrupted"


def lane_opted_out(lane_dir: Path) -> bool:
    return (Path(lane_dir) / OPT_OUT).exists()


def set_lane_opt_out(lane_dir: Path, opted_out: bool) -> None:
    """Write (or remove) the lane's own opt-out file."""
    path = Path(lane_dir) / OPT_OUT
    if opted_out:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"opted_out": True, "at": clock.iso(timespec="seconds")}) + "\n")
    else:
        path.unlink(missing_ok=True)


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class LaneRow:
    lane_id: str
    seed: str
    opted_out: bool = False
    status: str | None = None        # signed_out | challenged, until the episode ends
    reason: str | None = None
    status_at: datetime | None = None
    notified: bool = False


@dataclass
class Visit:
    lane_id: str
    started_at: datetime
    finished_at: datetime
    trigger: str                     # planned | now
    site: str
    outcome: str                     # started | interrupted | ok | cut | signed_out | challenged | wrong_account
                                     # | yielded | stopped | failed
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    id: int | None = None

    def public(self) -> dict[str, Any]:
        return {"at": self.started_at.isoformat(timespec="seconds"),
                "finished_at": self.finished_at.isoformat(timespec="seconds"), "trigger": self.trigger,
                "site": self.site, "outcome": self.outcome, "reason": self.reason, **self.detail}


class KeepaliveStore(SqliteStore):
    """One kept connection per thread (openajar.core.sqlite). On the event loop, call writes through
    `await store.run(store.method, ...)`: each one is done before the call returns."""

    row_factory = sqlite3.Row

    def __init__(self, home: Path):
        path = Path(home) / DB
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o700)
            migrate.migrate(Path(home), [MIGRATIONS], backup=path.exists())
            path.chmod(0o600)
            super().__init__(path, timeout=30.0, private=False)
        except (OSError, sqlite3.Error, migrate.MigrationError) as e:
            raise ServiceUnavailable(f"cannot open {path}: {e}") from None

    def readable(self) -> bool:
        """The database file is still there and answers (it can be deleted under a running agentosd)."""
        if not self.path.exists():
            return False
        try:
            self._conn().execute("SELECT 1 FROM lanes LIMIT 1").fetchall()
        except sqlite3.Error:
            return False
        return True

    # ---- node-wide values ----
    def get_meta(self, key: str) -> str | None:
        row = self._conn().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self._tx() as conn:
            if value is None:
                conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # ---- lanes ----
    def lane(self, lane_id: str) -> LaneRow:
        """The lane's row, made with a fresh seed the first time it is asked for (only that first
        time takes the write lock)."""
        row = self._conn().execute("SELECT * FROM lanes WHERE lane_id = ?", (lane_id,)).fetchone()
        if row is None:
            row = self._new_lane(lane_id)
            if isinstance(row, LaneRow):
                return row
        return LaneRow(lane_id=row["lane_id"], seed=row["seed"], opted_out=bool(row["opted_out"]),
                       status=row["status"], reason=row["reason"], status_at=_when(row["status_at"]),
                       notified=bool(row["notified"]))

    def _new_lane(self, lane_id: str) -> LaneRow | sqlite3.Row:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM lanes WHERE lane_id = ?", (lane_id,)).fetchone()
            if row is None:
                seed = secrets.token_hex(16)
                conn.execute("INSERT INTO lanes (lane_id, seed, updated_at) VALUES (?, ?, ?)",
                             (lane_id, seed, clock.iso(timespec="seconds")))
                return LaneRow(lane_id, seed)
        return row  # another process made it first

    def set_opt_out(self, lane_id: str, opted_out: bool) -> LaneRow:
        self.lane(lane_id)
        with self._tx() as conn:
            conn.execute("UPDATE lanes SET opted_out = ?, updated_at = ? WHERE lane_id = ?",
                         (int(opted_out), clock.iso(timespec="seconds"), lane_id))
        return self.lane(lane_id)

    def set_status(self, lane_id: str, status: str | None, reason: str | None = None, *,
                   notified: bool | None = None) -> LaneRow:
        """Mark the lane (signed_out, challenged) or clear the mark (None, which also resets notified)."""
        old = self.lane(lane_id)
        if status is None:
            notified = False
        elif notified is None:
            notified = old.notified if old.status == status else False
        at = old.status_at if old.status == status and old.status_at else datetime.now(UTC)
        with self._tx() as conn:
            conn.execute("UPDATE lanes SET status = ?, reason = ?, status_at = ?, notified = ?, updated_at = ? "
                         "WHERE lane_id = ?", (status, reason if status else None,
                                               at.isoformat(timespec="seconds") if status else None,
                                               int(bool(notified)), clock.iso(timespec="seconds"), lane_id))
        return self.lane(lane_id)

    # ---- visits ----
    def add_visit(self, visit: Visit) -> Visit:
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO visits (lane_id, started_at, finished_at, trigger, site, outcome, reason, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (visit.lane_id, visit.started_at.isoformat(), visit.finished_at.isoformat(), visit.trigger,
                 visit.site, visit.outcome, visit.reason, json.dumps(visit.detail, default=str, sort_keys=True)))
            visit.id = cur.lastrowid
        return visit

    def start_visit(self, visit: Visit) -> Visit:
        """The row a visit writes before it starts (outcome STARTED); finish_visit completes it."""
        visit.outcome = STARTED
        return self.add_visit(visit)

    def finish_visit(self, visit: Visit) -> Visit:
        with self._tx() as conn:
            conn.execute("UPDATE visits SET finished_at = ?, outcome = ?, reason = ?, detail = ? WHERE id = ?",
                         (visit.finished_at.isoformat(), visit.outcome, visit.reason,
                          json.dumps(visit.detail, default=str, sort_keys=True), visit.id))
        return visit

    def interrupt_started(self) -> int:
        """Rows still STARTED from an agentosd that stopped mid-visit: INTERRUPTED (they still count as
        visits for the plan). Called once when the service starts, before any visit."""
        with self._tx() as conn:
            cur = conn.execute("UPDATE visits SET outcome = ?, reason = ? WHERE outcome = ?",
                               (INTERRUPTED, "agentosd stopped during the visit", STARTED))
            return cur.rowcount

    def opted_out_lanes(self) -> list[str]:
        rows = self._conn().execute("SELECT lane_id FROM lanes WHERE opted_out = 1").fetchall()
        return [r["lane_id"] for r in rows]

    def visits(self, lane_id: str, limit: int = 10) -> list[Visit]:
        rows = self._conn().execute("SELECT * FROM visits WHERE lane_id = ? ORDER BY id DESC LIMIT ?",
                                    (lane_id, max(1, limit))).fetchall()
        return [Visit(lane_id=r["lane_id"], started_at=datetime.fromisoformat(r["started_at"]),
                      finished_at=datetime.fromisoformat(r["finished_at"]), trigger=r["trigger"], site=r["site"],
                      outcome=r["outcome"], reason=r["reason"], detail=json.loads(r["detail"] or "{}"), id=r["id"])
                for r in rows]

    def last_visit(self, lane_id: str) -> Visit | None:
        found = self.visits(lane_id, 1)
        return found[0] if found else None
