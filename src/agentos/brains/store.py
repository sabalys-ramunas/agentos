"""SQLite state for brains: `$AGENTOS_HOME/brains/brains.db` (WAL).

Brains, workspace defaults, tasks, attempts, seat state (cooldowns, backoff, learned window
capacity) and usage events. Seat occupancy is derived from task rows, so it survives a
restart for free. Credentials never live here (see credentials.py).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agentos.brains.models import BrainSpec, Privacy, TaskRecord, TaskStatus
from agentos.core.sqlite import SqliteStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS brains (
  name TEXT PRIMARY KEY, spec TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS brain_state (
  name TEXT PRIMARY KEY, state TEXT, reason TEXT, cooldown_until REAL,
  backoff_level INTEGER NOT NULL DEFAULT 0, window_capacity REAL, window_utilization REAL,
  last_rate_limit_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, brain TEXT NOT NULL, task_id TEXT, at REAL NOT NULL,
  tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS usage_brain_at ON usage(brain, at);
CREATE INDEX IF NOT EXISTS usage_task ON usage(task_id);
CREATE TABLE IF NOT EXISTS workspaces (
  workspace TEXT PRIMARY KEY, default_brain TEXT, privacy TEXT);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, status TEXT NOT NULL, brain TEXT, workspace TEXT NOT NULL,
  data TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS attempts (
  task_id TEXT NOT NULL, n INTEGER NOT NULL, brain TEXT NOT NULL, runner TEXT NOT NULL,
  shim_pid INTEGER, unit TEXT, dir TEXT NOT NULL, started_at REAL, ended_at REAL,
  outcome TEXT, reason TEXT, tokens INTEGER, cost_usd REAL, turns INTEGER,
  PRIMARY KEY (task_id, n));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

ACTIVE = (TaskStatus.STARTING.value, TaskStatus.RUNNING.value)


class Store(SqliteStore):
    """One kept connection per thread (agentos.core.sqlite). Every write is durable: on the event
    loop, call it through `await store.run(...)`; it is done before the call returns."""

    row_factory = sqlite3.Row

    def __init__(self, path: Path):
        super().__init__(path, timeout=10.0, private=False)
        self._conn().executescript(SCHEMA)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def tx(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        """An IMMEDIATE transaction: serializes seat checks across processes (not reentrant)."""
        return self._tx()

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn().execute(sql, tuple(args)))

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        return self._conn().execute(sql, tuple(args)).rowcount

    # ---- brains ----
    def put_brain(self, spec: BrainSpec) -> None:
        self.x("INSERT INTO brains(name, spec, created_at) VALUES (?,?,?) "
               "ON CONFLICT(name) DO UPDATE SET spec=excluded.spec",
               (spec.name, spec.model_dump_json(), time.time()))

    def get_brain(self, name: str) -> BrainSpec | None:
        rows = self.q("SELECT spec FROM brains WHERE name=?", (name,))
        return BrainSpec.model_validate_json(rows[0]["spec"]) if rows else None

    def list_brains(self) -> list[BrainSpec]:
        specs = [BrainSpec.model_validate_json(r["spec"]) for r in self.q("SELECT spec FROM brains")]
        return sorted(specs, key=lambda s: (s.order, s.name))

    def remove_brain(self, name: str) -> bool:
        with self.tx() as db:
            n = db.execute("DELETE FROM brains WHERE name=?", (name,)).rowcount
            db.execute("DELETE FROM brain_state WHERE name=?", (name,))
            db.execute("UPDATE workspaces SET default_brain=NULL WHERE default_brain=?", (name,))
        return n > 0

    # ---- brain state (seats) ----
    def brain_state(self, name: str) -> dict[str, Any]:
        rows = self.q("SELECT * FROM brain_state WHERE name=?", (name,))
        return dict(rows[0]) if rows else {"name": name, "state": None, "reason": None, "cooldown_until": None,
                                           "backoff_level": 0, "window_capacity": None,
                                           "window_utilization": None, "last_rate_limit_at": None}

    def set_brain_state(self, name: str, **fields: Any) -> None:
        cur = self.brain_state(name)
        cur.update(fields)
        cur["updated_at"] = time.time()
        cols = ["name", "state", "reason", "cooldown_until", "backoff_level", "window_capacity",
                "window_utilization", "last_rate_limit_at", "updated_at"]
        self.x(f"INSERT OR REPLACE INTO brain_state({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
               [cur.get(c) for c in cols])

    def add_usage(self, brain: str, task_id: str | None, tokens: int, cost_usd: float, at: float | None = None):
        self.x("INSERT INTO usage(brain, task_id, at, tokens, cost_usd) VALUES (?,?,?,?,?)",
               (brain, task_id, at or time.time(), int(tokens), float(cost_usd)))

    def usage_since(self, brain: str, since: float) -> tuple[int, float]:
        rows = self.q("SELECT COALESCE(SUM(tokens),0) t, COALESCE(SUM(cost_usd),0) c FROM usage "
                      "WHERE brain=? AND at>=?", (brain, since))
        return int(rows[0]["t"]), float(rows[0]["c"])

    def usage_for_task(self, task_id: str) -> tuple[int, float]:
        rows = self.q("SELECT COALESCE(SUM(tokens),0) t, COALESCE(SUM(cost_usd),0) c FROM usage WHERE task_id=?",
                      (task_id,))
        return int(rows[0]["t"]), float(rows[0]["c"])

    def spend_since(self, since: float) -> tuple[int, float]:
        rows = self.q("SELECT COALESCE(SUM(tokens),0) t, COALESCE(SUM(cost_usd),0) c FROM usage WHERE at>=?",
                      (since,))
        return int(rows[0]["t"]), float(rows[0]["c"])

    def active_count(self, brain: str, db: sqlite3.Connection | None = None) -> int:
        conn = db or self._conn()
        row = conn.execute(f"SELECT COUNT(*) n FROM tasks WHERE brain=? AND status IN ({','.join('?' * 2)})",
                           (brain, *ACTIVE)).fetchone()
        return int(row["n"])

    # ---- workspaces ----
    def workspace(self, ws: str) -> dict[str, Any]:
        rows = self.q("SELECT * FROM workspaces WHERE workspace=?", (ws,))
        return dict(rows[0]) if rows else {"workspace": ws, "default_brain": None, "privacy": None}

    def set_workspace(self, ws: str, *, default_brain: str | None | type = ..., privacy: Privacy | None | type = ...):
        cur = self.workspace(ws)
        if default_brain is not ...:
            cur["default_brain"] = default_brain
        if privacy is not ...:
            cur["privacy"] = privacy.value if isinstance(privacy, Privacy) else privacy
        self.x("INSERT OR REPLACE INTO workspaces(workspace, default_brain, privacy) VALUES (?,?,?)",
               (ws, cur["default_brain"], cur["privacy"]))

    def list_workspaces(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.q("SELECT * FROM workspaces ORDER BY workspace")]

    # ---- tasks ----
    def insert_task(self, rec: TaskRecord) -> None:
        now = time.time()
        self.x("INSERT INTO tasks(id, status, brain, workspace, data, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
               (rec.id, rec.status.value, rec.brain, rec.workspace, rec.model_dump_json(), now, now))

    def get_task(self, task_id: str) -> TaskRecord | None:
        rows = self.q("SELECT data FROM tasks WHERE id=?", (task_id,))
        return TaskRecord.model_validate_json(rows[0]["data"]) if rows else None

    def save_task(self, rec: TaskRecord, *, expect: Iterable[TaskStatus] | None = None,
                  db: sqlite3.Connection | None = None) -> bool:
        """Write the task; with `expect`, only if its stored status is one of those (so two
        processes finalizing the same task cannot both win)."""
        sql = "UPDATE tasks SET status=?, brain=?, data=?, updated_at=? WHERE id=?"
        args: list[Any] = [rec.status.value, rec.brain, rec.model_dump_json(), time.time(), rec.id]
        if expect is not None:
            exp = [s.value for s in expect]
            sql += f" AND status IN ({','.join('?' * len(exp))})"
            args += exp
        if db is not None:
            return db.execute(sql, args).rowcount > 0
        return self.x(sql, args) > 0

    def list_tasks(self, *, statuses: Iterable[TaskStatus] | None = None, workspace: str | None = None,
                   limit: int = 100) -> list[TaskRecord]:
        sql, args = "SELECT data FROM tasks WHERE 1=1", []
        if statuses is not None:
            st = [s.value for s in statuses]
            sql += f" AND status IN ({','.join('?' * len(st))})"
            args += st
        if workspace:
            sql += " AND workspace=?"
            args.append(workspace)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [TaskRecord.model_validate_json(r["data"]) for r in self.q(sql, args)]

    # ---- attempts ----
    def add_attempt(self, task_id: str, n: int, brain: str, runner: str, directory: Path,
                    db: sqlite3.Connection | None = None) -> None:
        sql = ("INSERT OR REPLACE INTO attempts(task_id, n, brain, runner, dir, started_at) VALUES (?,?,?,?,?,?)")
        args = (task_id, n, brain, runner, str(directory), time.time())
        if db is not None:
            db.execute(sql, args)
        else:
            self.x(sql, args)

    def update_attempt(self, task_id: str, n: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE attempts SET {cols} WHERE task_id=? AND n=?", (*fields.values(), task_id, n))

    def claim_attempt(self, task_id: str, n: int, **fields: Any) -> bool:
        """Record an attempt's end exactly once (first writer wins, across processes)."""
        cols = ", ".join(f"{k}=?" for k in fields)
        return self.x(f"UPDATE attempts SET {cols} WHERE task_id=? AND n=? AND outcome IS NULL",
                      (*fields.values(), task_id, n)) > 0

    def attempts(self, task_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.q("SELECT * FROM attempts WHERE task_id=? ORDER BY n", (task_id,))]

    def attempt(self, task_id: str, n: int) -> dict[str, Any] | None:
        rows = self.q("SELECT * FROM attempts WHERE task_id=? AND n=?", (task_id, n))
        return dict(rows[0]) if rows else None

    # ---- kv ----
    def get_kv(self, key: str, default: Any = None) -> Any:
        rows = self.q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_kv(self, key: str, value: Any) -> None:
        self.x("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (key, json.dumps(value)))
