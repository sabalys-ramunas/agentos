"""Versioned SQLite migrations, one ordered set per package and database file.

Each database carries a `schema_version` table with one row per applied migration:

    schema_version(package TEXT, version INTEGER, name TEXT, applied_at TEXT,
                   PRIMARY KEY (package, version))

A package's current version is its highest row. `migrate()`:

1. reads every set's current version and refuses a database written by a newer agentos;
2. if anything is pending, snapshots each database it will touch with the SQLite backup API
   into `AGENTOS_HOME/backups/migrations/<UTC stamp>/` (700, files 600; the last 5 are kept);
3. applies each pending migration in its own `BEGIN IMMEDIATE` transaction together with its
   schema_version row, re-reading the version inside the transaction, so a second migrator
   (or a rerun) is a no-op and a failed migration leaves the database exactly as it was.

How a package adds migrations: a module `agentos.<package>.migrations` exposing
`MIGRATIONS = MigrationSet(package=..., db=..., migrations=(Migration(1, "...", up), ...))`
(or a list of sets). `discover()` finds them without importing anything else of the package.
Versions start at 1 and have no gaps; a migration never changes once released.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import shutil
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agentos.ops.layout import adopt_owner, connect_ro, fsync_dir

KEEP_PRE_MIGRATION = 5

VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
  package    TEXT NOT NULL,
  version    INTEGER NOT NULL,
  name       TEXT NOT NULL,
  applied_at TEXT NOT NULL,
  PRIMARY KEY (package, version)
)
"""


class MigrationError(Exception):
    def __init__(self, message: str, backup_dir: Path | None = None):
        super().__init__(message)
        self.backup_dir = backup_dir


class NewerSchema(MigrationError):
    """The database was written by a newer agentos than this one."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class MigrationSet:
    package: str
    db: str                      # file name relative to AGENTOS_HOME, e.g. "agentos.db"
    migrations: tuple[Migration, ...]
    create: bool = False         # create the database when it does not exist yet

    def __post_init__(self) -> None:
        versions = [m.version for m in self.migrations]
        if versions != list(range(1, len(versions) + 1)):
            raise ValueError(f"{self.package}: migration versions must be 1..n in order, got {versions}")
        names = [m.name for m in self.migrations]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.package}: migration names must be unique")
        if Path(self.db).is_absolute() or ".." in Path(self.db).parts:
            raise ValueError(f"{self.package}: db must be a path inside AGENTOS_HOME")

    @property
    def latest(self) -> int:
        return len(self.migrations)


@dataclass
class SetStatus:
    package: str
    db: str
    exists: bool
    current: int
    latest: int
    pending: list[str] = field(default_factory=list)

    @property
    def newer(self) -> bool:
        return self.current > self.latest

    def to_dict(self) -> dict:
        return {"package": self.package, "db": self.db, "exists": self.exists, "current": self.current,
                "latest": self.latest, "pending": self.pending, "newer": self.newer}


@dataclass
class MigrateResult:
    applied: list[tuple[str, int, str]] = field(default_factory=list)  # (package, version, name)
    backup_dir: Path | None = None
    statuses: list[SetStatus] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"applied": [{"package": p, "version": v, "name": n} for p, v, n in self.applied],
                "backup_dir": str(self.backup_dir) if self.backup_dir else None,
                "status": [s.to_dict() for s in self.statuses]}


# ---- the node's own database -----------------------------------------------------

def _node_v1(conn: sqlite3.Connection) -> None:
    # The M1 schema exactly (agentos.node.store); a database made before migrations existed is
    # stamped at version 1 without change.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lanes (
            id         TEXT PRIMARY KEY,
            spec       TEXT NOT NULL,
            last_used  TEXT,
            exit_ip    TEXT
        )""")


NODE = MigrationSet(package="node", db="agentos.db", migrations=(Migration(1, "lanes table", _node_v1),))


def discover() -> list[MigrationSet]:
    """The node's set plus every `agentos.<package>.migrations` module's MIGRATIONS."""
    import agentos

    sets: list[MigrationSet] = [NODE]
    for info in sorted(pkgutil.iter_modules(agentos.__path__), key=lambda i: i.name):
        if not info.ispkg or info.name == "ops":
            continue
        name = f"agentos.{info.name}.migrations"
        try:
            if importlib.util.find_spec(name) is None:
                continue
        except (ImportError, ValueError):
            continue
        module = importlib.import_module(name)
        found = getattr(module, "MIGRATIONS", None)
        if isinstance(found, MigrationSet):
            sets.append(found)
        elif isinstance(found, list | tuple):
            sets.extend(s for s in found if isinstance(s, MigrationSet))
    packages = [s.package for s in sets]
    if len(set(packages)) != len(packages):
        raise MigrationError(f"two migration sets claim the same package: {packages}")
    return sets


# ---- reading versions --------------------------------------------------------------

def _connect(path: Path, timeout: float = 30.0) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn


def current_version(conn: sqlite3.Connection, package: str) -> int:
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
    if not has_table:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version WHERE package = ?", (package,)).fetchone()
    return int(row[0] or 0)


def status(home: Path, sets: Iterable[MigrationSet] | None = None) -> list[SetStatus]:
    out: list[SetStatus] = []
    for s in (discover() if sets is None else sets):
        path = Path(home) / s.db
        if not path.exists():
            pending = [m.name for m in s.migrations] if s.create else []
            out.append(SetStatus(s.package, s.db, False, 0, s.latest, pending))
            continue
        conn = connect_ro(path, timeout=10)
        try:
            cur = current_version(conn, s.package)
        finally:
            conn.close()
        out.append(SetStatus(s.package, s.db, True, cur, s.latest,
                             [m.name for m in s.migrations if m.version > cur]))
    return out


# ---- backups -------------------------------------------------------------------------

def snapshot_db(src: Path, dest: Path) -> None:
    """A consistent copy of a live SQLite database (the backup API), written 600."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.touch(mode=0o600)
    tmp.chmod(0o600)
    source = sqlite3.connect(src, timeout=30)  # a real connection, so the backup API takes proper locks
    try:
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
    finally:
        source.close()
    tmp.replace(dest)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")


def pre_migration_dir(home: Path) -> Path:
    return Path(home) / "backups" / "migrations"


def backup_databases(home: Path, dbs: Iterable[str], *, now: datetime | None = None) -> Path:
    root = pre_migration_dir(home)
    for d in (root.parent, root):
        d.mkdir(exist_ok=True)
        d.chmod(0o700)
    dest = root / _stamp(now)
    dest.mkdir(mode=0o700)
    for db in sorted(set(dbs)):
        src = Path(home) / db
        if src.exists():
            snapshot_db(src, dest / db)
    fsync_dir(dest)
    adopt_owner(root.parent, home)
    adopt_owner(root, home, recursive=True)
    _prune_pre_migration(root)
    return dest


def _prune_pre_migration(root: Path, keep: int = KEEP_PRE_MIGRATION) -> None:
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and p.name[:8].isdigit())
    for old in dirs[:-keep] if keep else dirs:
        shutil.rmtree(old, ignore_errors=True)


def restore_databases(backup_dir: Path, home: Path) -> list[str]:
    """Put pre-migration snapshots back (the upgrade's rollback). Stale WAL files are removed
    first so SQLite does not replay them over the restored file."""
    restored = []
    for src in sorted(Path(backup_dir).rglob("*")):
        if not src.is_file() or src.name.startswith("."):
            continue
        rel = src.relative_to(backup_dir)
        dest = Path(home) / rel
        for side in ("-wal", "-shm", "-journal"):
            Path(f"{dest}{side}").unlink(missing_ok=True)
        tmp = dest.with_name(f".{dest.name}.restore")
        shutil.copyfile(src, tmp)
        tmp.chmod(0o600)
        adopt_owner(tmp, dest if dest.exists() else home)
        tmp.replace(dest)
        restored.append(str(rel))
    return restored


# ---- applying ---------------------------------------------------------------------

def migrate(home: Path, sets: Iterable[MigrationSet] | None = None, *, backup: bool = True,
            dry_run: bool = False, now: datetime | None = None) -> MigrateResult:
    sets = list(discover() if sets is None else sets)
    statuses = status(home, sets)
    newer = [s for s in statuses if s.newer]
    if newer:
        s = newer[0]
        raise NewerSchema(f"{s.db} is at {s.package} schema {s.current}, but this agentos knows only "
                          f"{s.latest}. Upgrade agentos, or restore the database from a backup.")
    result = MigrateResult(statuses=statuses)
    pending = [(s, st) for s, st in zip(sets, statuses, strict=True) if st.pending]
    if not pending or dry_run:
        return result
    existing = [s.db for s, st in pending if st.exists]
    if backup and existing:
        result.backup_dir = backup_databases(home, existing, now=now)
    for s, _ in pending:
        path = Path(home) / s.db
        if not path.exists():
            made = [d for d in (path.parent, *path.parent.parents) if not d.exists()]
            path.parent.mkdir(parents=True, exist_ok=True)
            for d in made:
                adopt_owner(d, home)
            path.touch(mode=0o600)
            path.chmod(0o600)
            adopt_owner(path, home)
        conn = _connect(path)
        try:
            conn.execute(VERSION_TABLE)
            for m in s.migrations:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if current_version(conn, s.package) >= m.version:
                        conn.execute("ROLLBACK")
                        continue
                    m.up(conn)
                    conn.execute(
                        "INSERT INTO schema_version (package, version, name, applied_at) VALUES (?, ?, ?, ?)",
                        (s.package, m.version, m.name, datetime.now(UTC).isoformat()))
                    conn.execute("COMMIT")
                except BaseException as e:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    hint = f" The databases before this run are in {result.backup_dir}." if result.backup_dir else ""
                    raise MigrationError(f"{s.package} migration {m.version} ({m.name}) failed: "
                                         f"{type(e).__name__}: {e}. Nothing of it was applied.{hint}",
                                         backup_dir=result.backup_dir) from e
                result.applied.append((s.package, m.version, m.name))
        finally:
            conn.close()
    result.statuses = status(home, sets)
    return result
