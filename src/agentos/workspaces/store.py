"""SQLite storage for workspace records, lane readiness and proxy-rule overrides.

One file (`$AGENTOS_HOME/workspaces.db`), WAL mode, `synchronous=FULL`. Lanes stay in the node's own
lane store. This file only adds what the workspace layer needs to know about a lane: its platform,
the template surface it came from, its login URL and its readiness. A lane with no row here is still
a lane: it reads as needs_login, with its own name as its platform.

A file that cannot be read raises ServiceUnavailable, with a message that says the lanes are fine.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agentos.core.errors import ServiceUnavailable
from agentos.core.sqlite import SqliteStore

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT, default_proxy TEXT, notes TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lanes (
  id TEXT PRIMARY KEY, workspace TEXT NOT NULL, surface TEXT, platform TEXT NOT NULL, template TEXT,
  home_url TEXT, login_url TEXT, readiness TEXT NOT NULL, note TEXT, updated_at TEXT, updated_by TEXT,
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS lanes_workspace ON lanes(workspace);
CREATE TABLE IF NOT EXISTS overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, by TEXT NOT NULL, lane TEXT NOT NULL,
  proxy TEXT, rules TEXT NOT NULL, reason TEXT NOT NULL);
"""
_WORKSPACE_FIELDS = {"name", "color", "default_proxy", "notes"}


class WorkspaceStore(SqliteStore):
    """One kept connection per thread (agentos.core.sqlite). Every write is durable: on the event
    loop call it through `await store.run(...)`; it is done before the call returns."""

    row_factory = sqlite3.Row
    pragmas = ("synchronous=FULL",)

    def __init__(self, path: Path | str):
        path = Path(path)
        with self._guard_for(path):
            super().__init__(path, timeout=10.0, private=False)
            db = self._conn()
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("the integrity check failed")
            db.executescript(SCHEMA)
            self._check_version()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _guard_for(self, path: Path) -> Iterator[None]:
        self.path = path
        with self._guard():
            yield

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
            f"{self.path.name} could not be read or written ({exc}). If the file is damaged, restore it "
            "from a backup or move it aside and restart. Lanes are not affected.")

    def _check_version(self) -> None:
        row = self._conn().execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            self._conn().execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif row["version"] > SCHEMA_VERSION:
            raise ServiceUnavailable(
                f"{self.path.name} was written by a newer agentos (schema {row['version']}, this build reads "
                f"{SCHEMA_VERSION}). Upgrade agentos, or restore an older copy of the file.")

    # ---- workspaces -----------------------------------------------------------

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        with self._guard():
            row = self._conn().execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
            return dict(row) if row else None

    def list_workspaces(self) -> list[dict[str, Any]]:
        with self._guard():
            return [dict(r) for r in self._conn().execute("SELECT * FROM workspaces ORDER BY id")]

    def insert_workspace(self, row: dict[str, Any]) -> None:
        with self._guard():
            self._conn().execute(
                "INSERT INTO workspaces (id, name, color, default_proxy, notes, created_at, updated_at) "
                "VALUES (:id, :name, :color, :default_proxy, :notes, :created_at, :updated_at)", row)

    def update_workspace(self, workspace_id: str, fields: dict[str, Any], now: str) -> None:
        unknown = set(fields) - _WORKSPACE_FIELDS
        if unknown:
            raise ValueError(f"not workspace columns: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        with self._guard():
            self._conn().execute(f"UPDATE workspaces SET {assignments}, updated_at = :now WHERE id = :id",
                             {**fields, "now": now, "id": workspace_id})

    def delete_workspace(self, workspace_id: str) -> None:
        with self._guard(), self._tx() as db:
            db.execute("DELETE FROM lanes WHERE workspace = ?", (workspace_id,))
            db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))

    # ---- lanes: platform, template surface, login URL and readiness -------------

    def lane_rows(self) -> dict[str, dict[str, Any]]:
        with self._guard():
            return {r["id"]: dict(r) for r in self._conn().execute("SELECT * FROM lanes")}

    def lane_row(self, lane_id: str) -> dict[str, Any] | None:
        with self._guard():
            row = self._conn().execute("SELECT * FROM lanes WHERE id = ?", (lane_id,)).fetchone()
            return dict(row) if row else None

    def record_lane(self, row: dict[str, Any]) -> None:
        """A lane apply just created: its template details, starting as needs_login."""
        with self._guard():
            self._conn().execute(
                "INSERT OR REPLACE INTO lanes (id, workspace, surface, platform, template, home_url, login_url, "
                "readiness, note, updated_at, updated_by, created_at) VALUES (:id, :workspace, :surface, "
                ":platform, :template, :home_url, :login_url, 'needs_login', NULL, NULL, NULL, :created_at)", row)

    def adopt_lane(self, row: dict[str, Any]) -> None:
        """A lane that already exists and matches a plan: fill in template details it lacks. Never
        touches its readiness or note, and never overwrites a value it already has."""
        with self._guard(), self._tx():
            self._conn().execute(
                "INSERT OR IGNORE INTO lanes (id, workspace, surface, platform, template, home_url, login_url, "
                "readiness, note, created_at) VALUES (:id, :workspace, :surface, :platform, :template, "
                ":home_url, :login_url, 'needs_login', NULL, :created_at)", row)
            self._conn().execute(
                "UPDATE lanes SET surface = COALESCE(surface, :surface), template = COALESCE(template, :template), "
                "home_url = COALESCE(home_url, :home_url), login_url = COALESCE(login_url, :login_url) "
                "WHERE id = :id", row)

    def set_readiness(self, lane_id: str, *, workspace: str, platform: str, readiness: str,
                      note: str | None, by: str, now: str) -> None:
        with self._guard(), self._tx():
            self._conn().execute(
                "INSERT OR IGNORE INTO lanes (id, workspace, platform, readiness, created_at) "
                "VALUES (?, ?, ?, 'needs_login', ?)", (lane_id, workspace, platform, now))
            self._conn().execute(
                "UPDATE lanes SET readiness = ?, note = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                (readiness, note, now, by, lane_id))

    # ---- proxy-rule overrides ---------------------------------------------------

    def add_override(self, *, at: str, by: str, lane: str, proxy: str | None, rules: list[str],
                     reason: str) -> int:
        with self._guard():
            cur = self._conn().execute(
                "INSERT INTO overrides (at, by, lane, proxy, rules, reason) VALUES (?, ?, ?, ?, ?, ?)",
                (at, by, lane, proxy, json.dumps(rules), reason))
            return int(cur.lastrowid or 0)

    def list_overrides(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._guard():
            rows = self._conn().execute("SELECT * FROM overrides ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "rules": json.loads(r["rules"])} for r in rows]
