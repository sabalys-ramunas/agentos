"""SQLite state of this node's plugins: `$OPENAJAR_HOME/plugins.db`, WAL mode.

Three tables: what is on, which brain does each job, and the lanes a worker stays off. Reads fail
closed: a missing file is the default (everything off), and a file that cannot be read, has the wrong
shape or holds a value it should not reads as everything off with `problem` set. Reads never raise
and never change the file. `lane_switches` came after version 1 and is only ever added to, so a file
without it is a good file with every lane on, and the next write creates it.
Writes refuse a broken file instead of replacing it, because a person may want to look at it. A
write that changes nothing does nothing, so a `note` is only ever about a real change.

A connection lives for one operation only, because the CLI writes while the daemon reads.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from openajar.core.errors import ServiceUnavailable
from openajar.core.models import utcnow
from openajar.plugins.errors import StateBroken

Note = Callable[..., None]  # called inside the write's transaction, so if it raises nothing changes

FILE_NAME = "plugins.db"
SCHEMA_VERSION = 1
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS plugins (id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, updated_at TEXT NOT NULL)",
    ("CREATE TABLE IF NOT EXISTS assignments (plugin TEXT NOT NULL, job TEXT NOT NULL, brain TEXT NOT NULL, "
     "updated_at TEXT NOT NULL, PRIMARY KEY (plugin, job))"),
    ("CREATE TABLE IF NOT EXISTS lane_switches (plugin TEXT NOT NULL, lane TEXT NOT NULL, enabled INTEGER NOT NULL, "
     "updated_at TEXT NOT NULL, PRIMARY KEY (plugin, lane))"),
)


@dataclass(frozen=True)
class PluginState:
    enabled: frozenset[str] = frozenset()
    assignments: dict[str, dict[str, str]] = field(default_factory=dict)  # plugin -> job -> brain
    problem: str | None = None  # why the file could not be trusted; nothing is on when this is set
    lanes_off: dict[str, frozenset[str]] = field(default_factory=dict)  # plugin -> lanes a person turned it off on

    def brains_of(self, plugin: str) -> dict[str, str]:
        return self.assignments.get(plugin, {})

    def off_on(self, plugin: str) -> frozenset[str]:
        return self.lanes_off.get(plugin, frozenset())


class PluginStore:
    def __init__(self, home: Path | str):
        self.path = Path(home) / FILE_NAME

    def read(self) -> PluginState:
        try:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return PluginState()
            con = sqlite3.connect(f"file:{quote(str(self.path))}?mode=ro", uri=True, timeout=5)
            try:
                return self._load(con)
            finally:
                con.close()
        except (sqlite3.Error, OSError, ValueError) as exc:
            return PluginState(problem=f"{self.path.name} could not be read ({exc})")

    @staticmethod
    def _load(con: sqlite3.Connection) -> PluginState:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("the integrity check failed")
        versions = con.execute("SELECT version FROM schema_version").fetchall()
        if versions != [(SCHEMA_VERSION,)]:
            raise ValueError(f"schema version {versions}, and this build reads {SCHEMA_VERSION}")
        enabled: set[str] = set()
        for plugin, value in con.execute("SELECT id, enabled FROM plugins"):
            if not isinstance(plugin, str) or type(value) is not int or value not in (0, 1):
                raise ValueError("a plugin row holds something other than on or off")
            if value == 1:
                enabled.add(plugin)
        assignments: dict[str, dict[str, str]] = {}
        for plugin, job, brain in con.execute("SELECT plugin, job, brain FROM assignments"):
            if not all(isinstance(v, str) and v for v in (plugin, job, brain)):
                raise ValueError("an assignment row is not a plugin, a job and a brain name")
            assignments.setdefault(plugin, {})[job] = brain
        lanes_off: dict[str, set[str]] = {}
        if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lane_switches'").fetchone():
            for plugin, lane, value in con.execute("SELECT plugin, lane, enabled FROM lane_switches"):
                if not all(isinstance(v, str) and v for v in (plugin, lane)) or type(value) is not int \
                        or value not in (0, 1):
                    raise ValueError("a lane switch holds something other than a plugin, a lane and on or off")
                if value == 0:
                    lanes_off.setdefault(plugin, set()).add(lane)
        return PluginState(frozenset(enabled), assignments,
                           lanes_off={plugin: frozenset(lanes) for plugin, lanes in lanes_off.items()})

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        problem = self.read().problem
        if problem:
            raise StateBroken(f"{problem}. Nothing is on while it is broken, and it was not changed. Restore "
                              f"{self.path} from a backup, or move it aside to start again with every plugin off.")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))  # never readable by anyone else
            self._match_home_owner()
            con = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA synchronous=FULL")
                con.execute("BEGIN IMMEDIATE")
                for statement in SCHEMA:
                    con.execute(statement)
                if con.execute("SELECT count(*) FROM schema_version").fetchone()[0] == 0:
                    con.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
                con.execute("COMMIT")  # on its own: a change undone must not leave a file with no tables
                con.execute("BEGIN IMMEDIATE")
                yield con
                con.execute("COMMIT")
            finally:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                con.close()
        except (sqlite3.Error, OSError) as exc:
            raise ServiceUnavailable(f"{self.path.name} could not be written ({exc}). Nothing was changed.") from None

    def _match_home_owner(self) -> None:
        """`sudo openajar plugins enable` on a node whose daemon runs as its own user must leave a
        file that daemon can read, or the plugin would read as off with nobody told why."""
        if os.geteuid() == 0:
            home = self.path.parent.stat()
            os.chown(self.path, home.st_uid, home.st_gid)

    def set_enabled(self, plugin: str, enabled: bool, *, note: Note | None = None) -> None:
        with self._write() as con:
            row = con.execute("SELECT enabled FROM plugins WHERE id = ?", (plugin,)).fetchone()
            if bool(row and row[0]) == enabled:
                return
            con.execute("INSERT INTO plugins (id, enabled, updated_at) VALUES (?, ?, ?) ON CONFLICT (id) DO UPDATE "
                        "SET enabled = excluded.enabled, updated_at = excluded.updated_at",
                        (plugin, int(enabled), utcnow().isoformat()))
            if note:
                note(what="enabled" if enabled else "disabled")

    def set_lane(self, plugin: str, lane: str, on: bool, *, note: Note | None = None) -> None:
        """A lane with no row is on."""
        with self._write() as con:
            row = con.execute("SELECT enabled FROM lane_switches WHERE plugin = ? AND lane = ?", (plugin, lane)).fetchone()
            if (bool(row[0]) if row else True) == on:
                return
            con.execute("INSERT INTO lane_switches (plugin, lane, enabled, updated_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT (plugin, lane) DO UPDATE SET enabled = excluded.enabled, "
                        "updated_at = excluded.updated_at", (plugin, lane, int(on), utcnow().isoformat()))
            if note:
                note(what="lane_on" if on else "lane_off", lane=lane)

    def assign(self, plugin: str, job: str, brain: str, *, note: Note | None = None) -> None:
        with self._write() as con:
            before = self._brain(con, plugin, job)
            if before == brain:
                return
            con.execute("INSERT INTO assignments (plugin, job, brain, updated_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT (plugin, job) DO UPDATE SET brain = excluded.brain, "
                        "updated_at = excluded.updated_at", (plugin, job, brain, utcnow().isoformat()))
            if note:
                note(what="assigned", job=job, before=before, after=brain)

    def unassign(self, plugin: str, job: str, *, note: Note | None = None) -> str | None:
        """Nobody does this job any more. Returns the brain that did, or None when nobody did."""
        with self._write() as con:
            before = self._brain(con, plugin, job)
            if before is None:
                return None
            con.execute("DELETE FROM assignments WHERE plugin = ? AND job = ?", (plugin, job))
            if note:
                note(what="unassigned", job=job, before=before)
            return before

    @staticmethod
    def _brain(con: sqlite3.Connection, plugin: str, job: str) -> str | None:
        row = con.execute("SELECT brain FROM assignments WHERE plugin = ? AND job = ?", (plugin, job)).fetchone()
        return row[0] if row else None
