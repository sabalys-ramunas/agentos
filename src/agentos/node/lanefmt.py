"""The on-disk format of a lane, its version file and its migrations.

A lane is a directory `lanes/<ws>/<name>/` plus its rows in agentos.db. Version 1 (the baseline,
`LAYOUT_V1`) is the layout every lane had before the format was versioned:

    profile/               the Firefox profile (700): cookies, logins, local storage
    fingerprint.json       the pin (600); its own `agentos:pin` version (1 or 2) inside
    egress-trace.jsonl     the egress guard's trace, egress-observe beside it
    identity-trace.jsonl   web bot auth's trace
    files/                 the lane's files (inbox, outbox)
    shots/                 screenshots
    agentos.db             lanes (id, spec, ...), lane_control, lane_events rows for the lane

The version lives in `lane-format.json` (600) beside the profile:

    {"version": 1, "history": [{"version": 1, "name": "...", "applied_at": "..."}]}

A lane without the file is a version 1 lane made before the file existed. Stamping it writes the
file and changes nothing else.

How a migration runs (`migrate_lanes`):

1. Every lane's version is read; a lane written by a newer agentos stops the run before anything
   is changed (the daemon also refuses to warm it).
2. A lane in use is never touched: agentosd holds a shared lock on `.lane-format.lock` while a
   lane is warming or warm, and the migrator takes it exclusively without waiting. A browser
   running on the profile, or a held Firefox profile lock, also counts as in use (a daemon older
   than the lock). A lane in use is reported and left for the next run.
3. Each pending step runs under the exclusive lock. Before it changes a file or a row it copies
   it with `ctx.backup_file` / `ctx.backup_rows` into
   `AGENTOS_HOME/backups/lane-migrations/<UTC stamp>/<ws>/<name>/` (700, files 600).
4. The version file is replaced atomically after each step, so an interrupted run leaves the lane
   at the last finished step. A step must be idempotent: a step that was cut short runs again
   from the start on the next run.

How to add a format change: append `LaneMigration(n, "what changed", up)` to `LANE_MIGRATIONS`.
Versions start at 1 and have no gaps; a released step never changes.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.core.proc import pid_alive
from agentos.ops.layout import LaneDir, adopt_owner, fsync_dir, lane_dirs, write_private_json

FORMAT_NAME = "lane-format.json"
LOCK_NAME = ".lane-format.lock"
BACKUP_ROOT = ("backups", "lane-migrations")
KEEP_BACKUPS = 5
ABSENT_MARK = ".lane-format-absent"  # in a lane's backup: the lane had no version file before the run

# What a version 1 lane holds, for `agentos migrate status --json` and the docs.
LAYOUT_V1: dict[str, str] = {
    "profile/": "the Firefox profile",
    "fingerprint.json": "the pin (agentos:pin version 1 or 2 inside)",
    "egress-trace.jsonl": "the egress guard's trace (egress-observe beside it)",
    "identity-trace.jsonl": "web bot auth's trace",
    "files/": "the lane's files",
    "shots/": "screenshots",
    "agentos.db": "lanes, lane_control and lane_events rows",
}

# Tables in agentos.db with a lane's rows, and the column that names the lane.
LANE_TABLES: dict[str, str] = {"lanes": "id", "lane_control": "lane_id", "lane_events": "lane_id"}


class LaneFormatError(Exception):
    def __init__(self, message: str, backup_dir: Path | None = None):
        super().__init__(message)
        self.backup_dir = backup_dir


class NewerLaneFormat(LaneFormatError):
    """A lane was written by a newer agentos than this one."""


class LaneInUse(LaneFormatError):
    """The lane is warm (or a browser has its profile), so it is not migrated now."""


@dataclass
class LaneMigrationContext:
    """What a step gets: the lane, the node database, and backups of what it changes."""
    lane_id: str
    path: Path
    home: Path
    backup_dir: Callable[[], Path]
    _backed_up: set[str] = field(default_factory=set)

    @property
    def db_path(self) -> Path:
        return self.home / "agentos.db"

    def backup_file(self, rel: str) -> Path | None:
        """Copy `rel` (a file or directory in the lane) aside before changing it. Once per run:
        a second call keeps the first copy, which is the state before the run."""
        src = self.path / rel
        if rel in self._backed_up or not (src.exists() or src.is_symlink()):
            return None
        dest = self.backup_dir() / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest, follow_symlinks=False)
        self._backed_up.add(rel)
        return dest

    def backup_rows(self, table: str) -> Path | None:
        """The lane's rows of one table of agentos.db, as JSON lines, before a step changes them."""
        column = LANE_TABLES.get(table)
        if column is None:
            raise ValueError(f"{table} is not a lane table")
        key = f"agentos.db:{table}"
        if key in self._backed_up or not self.db_path.exists():
            return None
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(f"SELECT * FROM {table} WHERE {column} = ?", (self.lane_id,)).fetchall()
            except sqlite3.OperationalError:  # no such table
                rows = []
        finally:
            conn.close()
        dest = self.backup_dir() / "agentos.db" / f"{table}.jsonl"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("".join(json.dumps(dict(r), default=str) + "\n" for r in rows))
        self._backed_up.add(key)
        return dest

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        """A write transaction on agentos.db; commits when the block ends without an error."""
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()


@dataclass(frozen=True)
class LaneMigration:
    version: int
    name: str
    up: Callable[[LaneMigrationContext], None]


def _baseline(ctx: LaneMigrationContext) -> None:
    # Version 1 is the layout lanes already have (LAYOUT_V1): stamping changes nothing.
    return None


LANE_MIGRATIONS: tuple[LaneMigration, ...] = (
    LaneMigration(1, "baseline: profile, pin, traces, files and store rows as they are", _baseline),
)


def _check(migrations: tuple[LaneMigration, ...]) -> tuple[LaneMigration, ...]:
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError(f"lane migration versions must be 1..n in order, got {versions}")
    return migrations


LATEST = len(_check(LANE_MIGRATIONS))


# ---- reading the version ---------------------------------------------------------------

def format_path(lane_dir: Path) -> Path:
    return Path(lane_dir) / FORMAT_NAME


def read_format(lane_dir: Path) -> dict[str, Any] | None:
    """The version file, or None when the lane has none yet. Raises LaneFormatError when the file
    is there but unreadable (never guess the version of a lane)."""
    path = format_path(lane_dir)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise LaneFormatError(f"{path} is not a regular file")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise LaneFormatError(f"{path} is unreadable: {e}") from None
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise LaneFormatError(f"{path} has no valid version")
    return data


def lane_version(lane_dir: Path) -> tuple[int, bool]:
    """(version, stamped). A lane without the file is version 1, not stamped yet."""
    data = read_format(lane_dir)
    return (1, False) if data is None else (int(data["version"]), True)


def stamp_new(lane_dir: Path, *, now: datetime | None = None) -> None:
    """Give a lane this agentos made (created or imported) the current version."""
    when = (now or datetime.now(UTC)).isoformat()
    write_private_json(format_path(lane_dir), {
        "version": LATEST, "created_at": when,
        "history": [{"version": m.version, "name": m.name, "applied_at": when, "new_lane": True}
                    for m in LANE_MIGRATIONS]})


def check_openable(lane_dir: Path) -> str | None:
    """Why the daemon must not warm this lane, or None. A newer format is refused; so is an older
    one with steps pending (`agentos migrate run` brings it up to date, with a backup)."""
    try:
        version, _ = lane_version(lane_dir)
    except LaneFormatError as e:
        return f"its format file is broken ({e}). Restore lane-format.json from a backup."
    if version > LATEST:
        return (f"its directory is lane format {version}, written by a newer agentos (this one knows "
                f"{LATEST}). Upgrade agentos on this node, or restore the lane from a backup.")
    if version < LATEST:
        return (f"its directory is lane format {version} and this agentos needs {LATEST}. Run "
                "`agentos migrate run` (it backs up what it changes) and open it again.")
    return None


# ---- the in-use lock -------------------------------------------------------------------

class LaneLock:
    """A flock on `.lane-format.lock` in the lane directory: shared while the daemon has the lane
    warming or warm, exclusive while a migration runs."""

    def __init__(self, lane_dir: Path):
        self.path = Path(lane_dir) / LOCK_NAME
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, *, exclusive: bool, wait: float = 0.0, create: bool = True) -> bool:
        """Take the lock, waiting up to `wait` seconds. False when someone else holds it the other
        way (or, with create=False, when there is no lock file, so nobody holds it)."""
        import fcntl
        import time

        if self._fd is not None:
            return True
        if self.path.is_symlink():
            raise LaneFormatError(f"{self.path} is a symlink")
        if self.path.exists():
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        elif create:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            adopt_owner(self.path, self.path.parent)  # root migrating a service user's lane
        else:
            return False
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.05)
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)  # closing drops the flock


def profile_locked(profile: Path) -> bool:
    """A live browser holds this Firefox profile (its `lock` symlink names a live pid, or the
    .parentlock file lock is held)."""
    lock = Path(profile) / "lock"
    if lock.is_symlink():
        match = re.search(r":\+?(\d+)$", os.readlink(lock))
        if match and int(match.group(1)) > 1 and pid_alive(int(match.group(1))):
            return True
    parent = Path(profile) / ".parentlock"
    if parent.is_file() and not parent.is_symlink():
        import fcntl
        try:
            fd = os.open(parent, os.O_RDWR)
        except OSError:
            return False
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            return e.errno in (errno.EACCES, errno.EAGAIN)
        else:
            fcntl.lockf(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return False


def _in_use(lane: LaneDir, browser_pids: Callable[[Path], list[int]] | None) -> str | None:
    pids = browser_pids(lane.profile) if browser_pids else []
    if pids:
        return f"a browser (pid {pids[0]}) runs it"
    if lane.profile.is_dir() and profile_locked(lane.profile):
        return "a browser holds its profile lock"
    return None


# ---- status and running ----------------------------------------------------------------

@dataclass
class LaneStatus:
    lane: str
    current: int
    latest: int
    stamped: bool
    pending: list[str] = field(default_factory=list)
    in_use: str | None = None
    error: str | None = None

    @property
    def newer(self) -> bool:
        return self.current > self.latest

    def to_dict(self) -> dict[str, Any]:
        return {"lane": self.lane, "current": self.current, "latest": self.latest, "stamped": self.stamped,
                "pending": self.pending, "newer": self.newer, "in_use": self.in_use, "error": self.error}


@dataclass
class LaneMigrateResult:
    applied: list[tuple[str, int, str]] = field(default_factory=list)  # (lane, version, name)
    skipped: list[tuple[str, str]] = field(default_factory=list)       # (lane, why)
    backup_dir: Path | None = None
    statuses: list[LaneStatus] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"applied": [{"lane": lane, "version": v, "name": n} for lane, v, n in self.applied],
                "skipped": [{"lane": lane, "why": why} for lane, why in self.skipped],
                "backup_dir": str(self.backup_dir) if self.backup_dir else None,
                "status": [s.to_dict() for s in self.statuses]}


def _pending(current: int, stamped: bool, migrations: tuple[LaneMigration, ...]) -> list[LaneMigration]:
    # An unstamped lane is at version 1 in fact but still needs its stamp (step 1, a no-op).
    return [m for m in migrations if m.version > current or (not stamped and m.version == 1)]


def lane_status(home: Path, *, migrations: tuple[LaneMigration, ...] = LANE_MIGRATIONS,
                browser_pids: Callable[[Path], list[int]] | None = None,
                lanes: Iterable[LaneDir] | None = None) -> list[LaneStatus]:
    latest = len(_check(migrations))
    out: list[LaneStatus] = []
    for lane in (lane_dirs(home) if lanes is None else lanes):
        try:
            current, stamped = lane_version(lane.path)
        except LaneFormatError as e:
            out.append(LaneStatus(lane.id, 0, latest, True, error=str(e)))
            continue
        pending = [m.name for m in _pending(current, stamped, migrations)]
        in_use = None
        if pending:  # only worth asking when something would run
            held = LaneLock(lane.path)
            if lane.path.joinpath(LOCK_NAME).exists() and not held.acquire(exclusive=True):
                in_use = "agentosd has it warm"
            held.release()
            in_use = in_use or _in_use(lane, browser_pids)
        out.append(LaneStatus(lane.id, current, latest, stamped, pending, in_use))
    return out


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")


def backup_root(home: Path) -> Path:
    return Path(home).joinpath(*BACKUP_ROOT)


def _prune(root: Path, keep: int = KEEP_BACKUPS) -> None:
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and p.name[:8].isdigit())
    for old in dirs[:-keep] if keep else dirs:
        shutil.rmtree(old, ignore_errors=True)


def _lock_down(path: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(path):
        os.chmod(dirpath, 0o700)
        for name in filenames:
            p = Path(dirpath, name)
            if not p.is_symlink():
                p.chmod(0o600)
        for name in dirnames:
            p = Path(dirpath, name)
            if not p.is_symlink():
                p.chmod(0o700)


def migrate_lanes(home: Path, *, migrations: tuple[LaneMigration, ...] = LANE_MIGRATIONS,
                  dry_run: bool = False, browser_pids: Callable[[Path], list[int]] | None = None,
                  lanes: Iterable[LaneDir] | None = None, now: datetime | None = None) -> LaneMigrateResult:
    """Bring every cold lane to the latest format. Lanes in use are skipped, never waited for."""
    home = Path(home)
    migrations = _check(migrations)
    lanes = list(lane_dirs(home) if lanes is None else lanes)
    statuses = lane_status(home, migrations=migrations, browser_pids=browser_pids, lanes=lanes)
    broken = [s for s in statuses if s.error]
    if broken:
        raise LaneFormatError(f"lane {broken[0].lane}: {broken[0].error}. Nothing was migrated.")
    newer = [s for s in statuses if s.newer]
    if newer:
        s = newer[0]
        raise NewerLaneFormat(f"lane {s.lane} is at lane format {s.current}, but this agentos knows only "
                              f"{s.latest}. Upgrade agentos, or restore the lane from a backup. Nothing was migrated.")
    result = LaneMigrateResult(statuses=statuses)
    if dry_run:
        return result
    run_dir: Path | None = None

    def backup_dir_for(lane_id: str) -> Callable[[], Path]:
        def get() -> Path:
            nonlocal run_dir
            if run_dir is None:
                root = backup_root(home)
                for d in (root.parent, root):
                    d.mkdir(exist_ok=True)
                    d.chmod(0o700)
                run_dir = root / _stamp(now)
                run_dir.mkdir(mode=0o700)
                result.backup_dir = run_dir
            dest = run_dir / lane_id
            dest.mkdir(parents=True, exist_ok=True, mode=0o700)
            return dest
        return get

    by_id = {lane.id: lane for lane in lanes}
    try:
        for st in statuses:
            if not st.pending:
                continue
            lane = by_id[st.lane]
            lock = LaneLock(lane.path)
            if not lock.acquire(exclusive=True):
                result.skipped.append((lane.id, "agentosd has it warm"))
                continue
            try:
                why = _in_use(lane, browser_pids)  # again, under the lock
                if why:
                    result.skipped.append((lane.id, why))
                    continue
                current, stamped = lane_version(lane.path)  # a second migrator may have run
                data = read_format(lane.path) or {"version": 1, "history": []}
                ctx = LaneMigrationContext(lane.id, lane.path, home, backup_dir_for(lane.id))
                if stamped:  # the version file before this run, so a rollback can put it back
                    ctx.backup_file(FORMAT_NAME)
                else:
                    (ctx.backup_dir() / ABSENT_MARK).touch(mode=0o600)
                for m in _pending(current, stamped, migrations):
                    try:
                        m.up(ctx)
                    except BaseException as e:
                        hint = f" What it changed is backed up in {result.backup_dir}." if result.backup_dir else ""
                        raise LaneFormatError(
                            f"lane {lane.id}: format migration {m.version} ({m.name}) failed: "
                            f"{type(e).__name__}: {e}. The lane stays at format {data['version']}.{hint}",
                            backup_dir=result.backup_dir) from e
                    data = {**data, "version": m.version,
                            "history": [*data.get("history", []), {"version": m.version, "name": m.name,
                                                                   "applied_at": datetime.now(UTC).isoformat()}]}
                    write_private_json(format_path(lane.path), data)
                    fsync_dir(lane.path)
                    adopt_owner(format_path(lane.path), lane.path)
                    result.applied.append((lane.id, m.version, m.name))
            finally:
                lock.release()
    finally:
        if run_dir is not None:
            _lock_down(run_dir)
            adopt_owner(backup_root(home).parent, home)
            adopt_owner(backup_root(home), home, recursive=True)
            _prune(backup_root(home))
    result.statuses = lane_status(home, migrations=migrations, browser_pids=browser_pids, lanes=lanes)
    return result


def restore_lanes(backup_dir: Path, home: Path) -> list[str]:
    """Put a lane migration run's backups back (the upgrade's rollback): the files each step
    changed, the lane's rows it changed, and the version file as it was. Lanes must be cold."""
    backup_dir, home = Path(backup_dir), Path(home)
    restored: list[str] = []
    for ws in sorted(p for p in backup_dir.iterdir() if p.is_dir()):
        for saved in sorted(p for p in ws.iterdir() if p.is_dir()):
            lane_id = f"{ws.name}/{saved.name}"
            lane = home / "lanes" / ws.name / saved.name
            if not lane.is_dir() or lane.is_symlink():
                continue
            for entry in sorted(saved.iterdir()):
                if entry.name == ABSENT_MARK:
                    format_path(lane).unlink(missing_ok=True)
                elif entry.name == "agentos.db" and entry.is_dir():
                    _restore_rows(home / "agentos.db", lane_id, entry)
                else:
                    dest = lane / entry.name
                    if dest.is_dir() and not dest.is_symlink():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink(missing_ok=True)
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.copytree(entry, dest, symlinks=True)
                    else:
                        shutil.copy2(entry, dest, follow_symlinks=False)
                    adopt_owner(dest, lane, recursive=True)
            restored.append(lane_id)
    return restored


def _restore_rows(db: Path, lane_id: str, saved: Path) -> None:
    conn = sqlite3.connect(db, timeout=30, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for dump in sorted(saved.glob("*.jsonl")):
                table = dump.stem
                column = LANE_TABLES.get(table)
                if column is None:
                    continue
                rows = [json.loads(line) for line in dump.read_text().splitlines() if line.strip()]
                conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (lane_id,))
                for row in rows:
                    cols = list(row)
                    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                                 [row[c] for c in cols])
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
