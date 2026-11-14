"""Encrypted, consistent backups of AGENTOS_HOME: create, verify, restore, list and prune.

One backup is one file, `agentos-<node>-<UTC stamp>.tar.gz.age`: a gzipped tar encrypted with
a passphrase in the age v1 format (see agentos.core.age; `age -d` opens it too). Inside:

  agentos-backup/format.json     format name and version, node, agentos version, created_at
  home/...                       the files, at their paths relative to AGENTOS_HOME
  agentos-backup/manifest.json   last: every file with its size and SHA-256, the lanes and how
                                 each was copied (cold, paused, live), and what was skipped

What is in a backup:
  node.toml (with the MCP token, so the restored node keeps working with Claude Code)
  secrets/ (proxies.json, vault.key, anything else there)
  every SQLite database outside lanes/ (agentos.db, vault.db, other packages' .db files),
    copied with the SQLite backup API, so a database in use is still copied consistently
  lanes/<ws>/<name>/: profile/ (cookies, logins, local storage, IndexedDB), fingerprint.json,
    and the other files of the lane
  every other file under AGENTOS_HOME

What is not:
  run/ (sockets), logs/, backups/ (this directory, and the pre-migration snapshots), tmp/
  top-level dot entries (.cache with the service user's Camoufox browser, .mozilla, ...)
  lanes/*/*/shots/ (screenshots)
  browser caches inside profiles: cache2, startupCache, thumbnails, shader-cache,
    jumpListCache, OfflineCache, safebrowsing, crashes, minidumps, datareporting,
    saved-telemetry-pings (rebuilt by the browser; pass include_caches to keep them)
  profile lock files (lock, .parentlock, parent.lock) and symlinks of any kind
  the code (/opt/agentos), the Camoufox browser, apt packages and the systemd unit: the
    installer recreates those
  the vault passphrase of a passphrase-mode vault (it is never stored on the node)

Consistency: a browser writes its profile while it runs, so each lane is copied into a private
staging directory while its browser is down. A warm lane is paused through the daemon's ops
socket (sent cold, copied, and warmed again right away: a pause of seconds); a browser that
starts during the copy makes the copy start over. With no daemon to ask, a warm lane fails
the backup unless allow_live is set (then it is copied live and marked "live").

Restore writes into a staging directory beside the target and swaps it in only after every
checksum matched, so a damaged archive, a wrong passphrase or an interrupted restore never
leaves a half-restored home. It refuses a non-empty home unless forced (the old home is then
moved aside, never deleted), and leaves every directory 700 and every file 600.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets as pysecrets
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Protocol

from agentos import __version__
from agentos.core import age
from agentos.core.errors import AgentosError
from agentos.ops.layout import (
    PROFILE_LOCKS,
    LaneDir,
    adopt_owner,
    default_backup_dir,
    fsync_dir,
    is_sqlite,
    lane_dirs,
    ops_socket,
    vault_socket,
)
from agentos.ops.migrate import snapshot_db
from agentos.ops.system import Host

FORMAT = "agentos-backup"
FORMAT_VERSION = 1
SUFFIX = ".tar.gz.age"
META = "agentos-backup"
DATA = "home"
NAME_RE = re.compile(r"^agentos-(?P<node>[A-Za-z0-9][A-Za-z0-9._-]*)-(?P<stamp>\d{8}T\d{6}Z)\.tar\.gz\.age$")
PARTIAL_PREFIX = ".partial-"
MIN_PASSPHRASE = 12

EXCLUDE_TOP = {"run", "logs", "backups", "tmp"}
EXCLUDE_LANE = {"shots"}
EXCLUDE_PROFILE = {"cache2", "startupCache", "thumbnails", "shader-cache", "jumpListCache", "OfflineCache",
                   "safebrowsing", "crashes", "minidumps", "datareporting", "saved-telemetry-pings"}
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SQLITE_SIDE = ("-wal", "-shm", "-journal")
SMALL_FILE = 8 * 1024 * 1024
PAUSE_WAIT = 30.0
COPY_ATTEMPTS = 3


class BackupError(AgentosError):
    code = "backup_failed"


class BackupRefused(AgentosError):
    code = "refused"


class ArchiveError(AgentosError):
    """The archive cannot be read: wrong passphrase, damaged, truncated, or not a backup."""
    code = "bad_archive"


# ---- names and retention --------------------------------------------------------------

@dataclass(frozen=True)
class Archive:
    path: Path
    node: str
    created: datetime
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "node": self.node, "created_at": self.created.isoformat(),
                "size": self.size}


def safe_node(node: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", node).strip(".-") or "node"


def archive_name(node: str, now: datetime) -> str:
    return f"agentos-{safe_node(node)}-{now.strftime('%Y%m%dT%H%M%SZ')}{SUFFIX}"


def parse_name(name: str) -> tuple[str, datetime] | None:
    m = NAME_RE.match(name)
    if not m:
        return None
    return m.group("node"), datetime.strptime(m.group("stamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def list_archives(directory: Path, node: str | None = None) -> list[Archive]:
    """Finished backups in a directory, oldest first (partial files never count)."""
    out = []
    try:
        entries = list(Path(directory).iterdir())
    except OSError:
        return out
    for p in entries:
        parsed = parse_name(p.name)
        if parsed is None or not p.is_file() or p.is_symlink():
            continue
        if node is not None and parsed[0] != safe_node(node):
            continue
        out.append(Archive(p, parsed[0], parsed[1], p.stat().st_size))
    return sorted(out, key=lambda a: (a.created, a.path.name))


def prune(directory: Path, keep: int, node: str | None = None, *, now: float | None = None) -> list[Path]:
    """Delete all but the newest `keep` backups of this node, and partial files over a day old.
    Touches only files that match the backup name pattern."""
    if keep < 1:
        raise BackupRefused("keep must be at least 1")
    removed = []
    archives = list_archives(directory, node)
    for a in archives[:-keep]:
        a.path.unlink(missing_ok=True)
        removed.append(a.path)
    cutoff = (now or time.time()) - 86400
    for p in Path(directory).glob(f"{PARTIAL_PREFIX}*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed.append(p)
        except OSError:
            pass
    return removed


# ---- talking to the daemon ------------------------------------------------------------

class LaneCoordinator(Protocol):
    def available(self) -> bool: ...

    def warm(self) -> set[str]: ...

    def close(self, lane_id: str) -> None: ...

    def open(self, lane_id: str) -> None: ...


class NoDaemon:
    def available(self) -> bool:
        return False

    def warm(self) -> set[str]:
        return set()

    def close(self, lane_id: str) -> None:
        raise BackupError("no daemon to pause the lane")

    def open(self, lane_id: str) -> None:
        pass


class DaemonCoordinator:
    """Pauses lanes through the health service's ops socket (agentos.ops.control)."""

    def __init__(self, home: Path, host: Host | None = None, timeout: float = 120.0):
        self.path = ops_socket(home)
        self.host = host or Host()
        self.timeout = timeout

    def _call(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return self.host.unix_request(self.path, payload, timeout=self.timeout)

    def available(self) -> bool:
        resp = self._call({"op": "ping"})
        return bool(resp and resp.get("ok"))

    def warm(self) -> set[str]:
        resp = self._call({"op": "lanes"}) or {}
        return {lane["id"] for lane in resp.get("lanes", []) if lane.get("state") in ("warming", "warm", "active")}

    def close(self, lane_id: str) -> None:
        resp = self._call({"op": "close", "lane": lane_id})
        if not resp or not resp.get("ok"):
            raise BackupError(f"the daemon could not pause {lane_id}: "
                              f"{(resp or {}).get('error', {}).get('message', 'no answer')}")

    def open(self, lane_id: str) -> None:
        self._call({"op": "open", "lane": lane_id})


# ---- writing ------------------------------------------------------------------------------

@dataclass
class BackupResult:
    path: Path
    size: int
    files: int
    bytes: int
    lanes: list[dict[str, Any]]
    skipped: list[str]
    removed: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    location: str | None = None   # where it ended up (a path or an s3:// URL)

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "location": self.location or str(self.path), "size": self.size,
                "files": self.files, "bytes": self.bytes, "lanes": self.lanes, "skipped": self.skipped,
                "removed": [str(p) for p in self.removed], "warnings": self.warnings}


class _Writer:
    def __init__(self, tar: tarfile.TarFile, now: float):
        self.tar = tar
        self.now = now
        self.files: list[dict[str, Any]] = []
        self.dirs: list[str] = []
        self.total = 0

    def _info(self, name: str, size: int, *, directory: bool = False, mtime: float | None = None) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.size = 0 if directory else size
        info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
        info.mode = 0o700 if directory else 0o600
        info.mtime = int(mtime if mtime is not None else self.now)
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    def add_dir(self, rel: str) -> None:
        self.tar.addfile(self._info(f"{DATA}/{rel}", 0, directory=True))
        self.dirs.append(rel)

    def add_bytes(self, rel: str, data: bytes, kind: str = "file", mtime: float | None = None) -> None:
        self.tar.addfile(self._info(f"{DATA}/{rel}", len(data), mtime=mtime), io.BytesIO(data))
        self.files.append({"path": rel, "kind": kind, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.total += len(data)

    def add_file(self, rel: str, path: Path, kind: str = "file") -> None:
        st = os.stat(path, follow_symlinks=False)
        if st.st_size <= SMALL_FILE:
            with open(path, "rb") as f:
                self.add_bytes(rel, f.read(), kind, st.st_mtime)
            return
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            reader = _ExactReader(f, st.st_size, digest, rel)
            self.tar.addfile(self._info(f"{DATA}/{rel}", st.st_size, mtime=st.st_mtime), reader)
        self.files.append({"path": rel, "kind": kind, "size": st.st_size, "sha256": digest.hexdigest()})
        self.total += st.st_size

    def add_meta(self, name: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, indent=1, sort_keys=True).encode()
        info = self._info(f"{META}/{name}", len(data))
        self.tar.addfile(info, io.BytesIO(data))


class _ExactReader:
    """Feeds tarfile exactly `size` bytes of a file, hashing them; a file that shrinks fails."""

    def __init__(self, f: BinaryIO, size: int, digest: Any, rel: str):
        self.f, self.left, self.digest, self.rel = f, size, digest, rel

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        n = self.left if n is None or n < 0 else min(n, self.left)
        data = self.f.read(n)
        if not data:
            raise BackupError(f"{self.rel} shrank while it was being copied; run the backup again")
        self.left -= len(data)
        self.digest.update(data)
        return data


def _copy_tree(src: Path, dest: Path, *, profile_excludes: set[str], skipped: list[str], rel_base: str) -> None:
    """Copy a lane directory into staging: no symlinks, no excluded caches, no lock files."""
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        here = Path(dirpath)
        rel = here.relative_to(src)
        parts = rel.parts
        keep_dirs = []
        for d in sorted(dirnames):
            p = here / d
            if p.is_symlink():
                skipped.append(f"{rel_base}/{(rel / d).as_posix()} (symlink)")
                continue
            if not parts and d in EXCLUDE_LANE:
                continue
            if parts[:1] == ("profile",) and len(parts) == 1 and d in profile_excludes:
                continue
            keep_dirs.append(d)
            (dest / rel / d).mkdir(mode=0o700, exist_ok=True)
        dirnames[:] = keep_dirs
        for name in sorted(filenames):
            p = here / name
            if parts[:1] == ("profile",) and len(parts) == 1 and name in PROFILE_LOCKS:
                continue
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                if name not in PROFILE_LOCKS:
                    skipped.append(f"{rel_base}/{(rel / name).as_posix()} (symlink)")
                continue
            if not stat.S_ISREG(st.st_mode):
                skipped.append(f"{rel_base}/{(rel / name).as_posix()} (not a regular file)")
                continue
            shutil.copy2(p, dest / rel / name, follow_symlinks=False)


def _wait_no_browser(host: Host, profile: Path, timeout: float = PAUSE_WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while host.browser_pids(profile):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


def _stage_lane(lane: LaneDir, staging: Path, *, host: Host, coordinator: LaneCoordinator, warm: set[str],
                allow_live: bool, include_caches: bool, skipped: list[str], warnings: list[str]) -> dict[str, Any]:
    excludes = set() if include_caches else EXCLUDE_PROFILE
    was_warm = lane.id in warm
    running = bool(host.browser_pids(lane.profile)) or was_warm
    method = "cold"
    paused = False
    daemon = coordinator.available() if running else False

    def pause() -> None:
        coordinator.close(lane.id)
        if not _wait_no_browser(host, lane.profile):
            raise BackupError(f"{lane.id}: its browser did not stop within {PAUSE_WAIT:.0f}s after the pause")

    if running:
        if daemon:
            pause()
            method, paused = "paused", True
        elif allow_live:
            method = "live"
            warnings.append(f"{lane.id} was copied while its browser ran (live): its newest changes may be missing")
        else:
            raise BackupRefused(
                f"lane {lane.id} is warm and no daemon answers on the ops socket to pause it. Close it "
                "(lane_close), stop agentosd, or pass --allow-live to copy it while it runs.")
    dest = staging / lane.id
    try:
        for attempt in range(COPY_ATTEMPTS):
            shutil.rmtree(dest, ignore_errors=True)
            _copy_tree(lane.path, dest, profile_excludes=excludes, skipped=skipped, rel_base=f"lanes/{lane.id}")
            if method == "live" or not host.browser_pids(lane.profile):
                break
            # a browser started while we copied (an agent warmed the lane): copy again
            if coordinator.available():
                pause()
                method, paused = "paused", True
            elif allow_live:
                method = "live"
                warnings.append(f"{lane.id} started during the copy and was kept as a live copy")
                break
            else:
                raise BackupRefused(f"lane {lane.id} started while it was being copied; run the backup again")
            if attempt == COPY_ATTEMPTS - 1:
                raise BackupError(f"{lane.id}: agents kept reopening the lane; no quiet copy after "
                                  f"{COPY_ATTEMPTS} tries")
    finally:
        if paused and was_warm:
            try:
                coordinator.open(lane.id)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{lane.id} was paused for the backup and did not warm again: {type(e).__name__}")
    return {"id": lane.id, "copied": method, "fingerprint": (lane.fingerprint).exists()}


def _is_db(path: Path) -> bool:
    return path.suffix in SQLITE_SUFFIXES and is_sqlite(path)


def _sqlite_side_file(path: Path) -> bool:
    """A -wal, -shm or -journal file next to a database we copy with the backup API."""
    for side in SQLITE_SIDE:
        if path.name.endswith(side):
            return _is_db(path.with_name(path.name[: -len(side)]))
    return False


def _walk_home(home: Path, lanes: set[Path], skipped: list[str]) -> Iterator[tuple[str, Path, bool]]:
    """(relative path, path, is_dir) for everything outside the lane directories, sorted."""
    for dirpath, dirnames, filenames in os.walk(home, followlinks=False):
        here = Path(dirpath)
        rel = here.relative_to(home)
        keep = []
        for d in sorted(dirnames):
            p = here / d
            r = (rel / d).as_posix()
            if not rel.parts and (d in EXCLUDE_TOP or d.startswith(".")):
                continue
            if p in lanes:
                continue
            if p.is_symlink():
                skipped.append(f"{r} (symlink)")
                continue
            keep.append(d)
            yield r, p, True
        dirnames[:] = keep
        for name in sorted(filenames):
            p = here / name
            r = (rel / name).as_posix()
            if not rel.parts and name.startswith("."):
                continue
            try:
                st = os.lstat(p)
            except FileNotFoundError:  # the snapshot of a database closes it, and that removes its -wal and -shm
                if not _sqlite_side_file(p):
                    skipped.append(f"{r} (gone before it was read)")
                continue
            if stat.S_ISLNK(st.st_mode):
                skipped.append(f"{r} (symlink)")
                continue
            if not stat.S_ISREG(st.st_mode):
                if not stat.S_ISSOCK(st.st_mode):
                    skipped.append(f"{r} (not a regular file)")
                continue
            yield r, p, False


def _estimate(home: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(home, followlinks=False):
        rel = Path(dirpath).relative_to(home)
        if not rel.parts:
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_TOP and not d.startswith(".")]
        if len(rel.parts) == 4 and rel.parts[0] == "lanes" and rel.parts[3] == "profile":
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_PROFILE]
        for name in filenames:
            try:
                total += os.lstat(Path(dirpath, name)).st_size
            except OSError:
                pass
    return total


def create_backup(home: Path, dest: Path, passphrase: str, *, node: str, keep: int | None = 7,
                  host: Host | None = None, coordinator: LaneCoordinator | None = None,
                  allow_live: bool = False, include_caches: bool = False, log_n: int = age.DEFAULT_LOG_N,
                  now: datetime | None = None, fail_after: Callable[[str], None] | None = None) -> BackupResult:
    """Write one encrypted backup of `home` into directory `dest`. `fail_after` is a test hook
    called after each member is written (raise from it to simulate an interruption)."""
    home, dest = Path(home), Path(dest)
    host = host or Host()
    coordinator = coordinator or NoDaemon()
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupRefused(f"the backup passphrase must be at least {MIN_PASSPHRASE} characters")
    if not (home / "node.toml").exists():
        raise BackupRefused(f"{home} has no node.toml; is AGENTOS_HOME right?")
    inside = dest.resolve().is_relative_to(home.resolve())
    if inside and not dest.resolve().is_relative_to(default_backup_dir(home.resolve())):
        raise BackupRefused(f"{dest} is inside AGENTOS_HOME; use {default_backup_dir(home)} or a path outside it")
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    if inside:
        dest.chmod(0o700)
        for d in (default_backup_dir(home), dest):
            adopt_owner(d, home)
    need = _estimate(home)
    usage = host.disk_usage(dest)
    if usage is not None and usage[2] < need * 1.1 + 64 * 1024**2:
        raise BackupRefused(f"not enough space in {dest}: about {need // 1024**2} MiB needed for staging and "
                            f"the archive, {usage[2] // 1024**2} MiB free")
    now = now or datetime.now(UTC)
    name = archive_name(node, now)
    final = dest / name
    if final.exists():
        raise BackupRefused(f"{final} already exists")
    partial = dest / f"{PARTIAL_PREFIX}{name}"
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=dest))
    staging.chmod(0o700)
    skipped: list[str] = []
    warnings: list[str] = []
    lanes_out: list[dict[str, Any]] = []
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as raw:
            enc = age.Encryptor(raw, passphrase, log_n=log_n)
            with tarfile.open(fileobj=enc, mode="w|gz", compresslevel=6) as tar:
                w = _Writer(tar, now.timestamp())
                header = {"format": FORMAT, "version": FORMAT_VERSION, "node": node, "agentos_version": __version__,
                          "created_at": now.isoformat(timespec="seconds"), "hostname": host.hostname()}
                w.add_meta("format.json", header)
                lanes = lane_dirs(home)
                lane_paths = {lane.path for lane in lanes}
                for rel, path, is_dir in _walk_home(home, lane_paths, skipped):
                    if is_dir:
                        w.add_dir(rel)
                    elif _sqlite_side_file(path):
                        continue  # covered by the backup API copy of its database
                    elif _is_db(path):
                        snap = staging / "db" / rel
                        snapshot_db(path, snap)
                        w.add_file(rel, snap, kind="sqlite")
                        snap.unlink()
                    else:
                        w.add_file(rel, path)
                    if fail_after:
                        fail_after(rel)
                warm = coordinator.warm() if coordinator.available() else set()
                for lane in lanes:
                    info = _stage_lane(lane, staging / "lanes", host=host, coordinator=coordinator, warm=warm,
                                       allow_live=allow_live, include_caches=include_caches, skipped=skipped,
                                       warnings=warnings)
                    staged = staging / "lanes" / lane.id
                    count_before, bytes_before = len(w.files), w.total
                    for dirpath, dirnames, filenames in os.walk(staged):
                        dirnames.sort()
                        here = Path(dirpath)
                        rel_dir = (Path("lanes") / lane.id / here.relative_to(staged)).as_posix()
                        w.add_dir(rel_dir)
                        for fname in sorted(filenames):
                            w.add_file(f"{rel_dir}/{fname}", here / fname)
                            if fail_after:
                                fail_after(f"{rel_dir}/{fname}")
                    shutil.rmtree(staged, ignore_errors=True)
                    info.update(files=len(w.files) - count_before, bytes=w.total - bytes_before)
                    lanes_out.append(info)
                manifest = {**header, "files": w.files, "dirs": w.dirs, "lanes": lanes_out, "skipped": skipped,
                            "excluded": {"top": sorted(EXCLUDE_TOP) + [".*"], "lane": sorted(EXCLUDE_LANE),
                                         "profile": [] if include_caches else sorted(EXCLUDE_PROFILE)},
                            "total_bytes": w.total}
                w.add_meta("manifest.json", manifest)
            enc.close()
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(partial, final)
        fsync_dir(dest)
        files, total = len(w.files), w.total
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _chown_like(final, home, host)
    result = BackupResult(path=final, size=final.stat().st_size, files=files, bytes=total, lanes=lanes_out,
                          skipped=skipped, warnings=warnings)
    if keep:
        result.removed = prune(dest, keep, node)
    return result


def _chown_like(path: Path, home: Path, host: Host) -> None:
    """When root backs up a home owned by the service user, give the archive to that user."""
    if host.euid() != 0:
        return
    try:
        st = os.stat(home)
        if st.st_uid != 0:
            os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass


# ---- reading --------------------------------------------------------------------------

def _safe_rel(name: str) -> str:
    if not name.startswith(f"{DATA}/"):
        raise ArchiveError(f"unexpected member {name!r} in the archive")
    rel = PurePosixPath(name[len(DATA) + 1:])
    if rel.is_absolute() or not rel.parts or any(p in ("", ".", "..") for p in rel.parts) or "\\" in name:
        raise ArchiveError(f"unsafe path {name!r} in the archive")
    return rel.as_posix()


@dataclass
class ArchiveContents:
    header: dict[str, Any]
    manifest: dict[str, Any]
    files: int
    bytes: int
    sqlite_checked: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"node": self.header.get("node"), "created_at": self.header.get("created_at"),
                "agentos_version": self.header.get("agentos_version"), "files": self.files, "bytes": self.bytes,
                "lanes": self.manifest.get("lanes", []), "skipped": self.manifest.get("skipped", []),
                "sqlite_checked": self.sqlite_checked}


def _read_json_member(tar: tarfile.TarFile, member: tarfile.TarInfo) -> dict[str, Any]:
    if not member.isreg() or member.size > 64 * 1024 * 1024:
        raise ArchiveError(f"{member.name} is malformed")
    f = tar.extractfile(member)
    assert f is not None
    try:
        data = json.loads(f.read())
    except ValueError:
        raise ArchiveError(f"{member.name} is not valid JSON") from None
    if not isinstance(data, dict):
        raise ArchiveError(f"{member.name} is malformed")
    return data


def _walk_archive(path: Path, passphrase: str, on_file: Callable[[str, tarfile.TarInfo, BinaryIO], str],
                  on_dir: Callable[[str], None]) -> ArchiveContents:
    """Stream an archive, calling on_file for each file (it returns the SHA-256 it computed)
    and on_dir for each directory, then check everything against the manifest."""
    try:
        raw = path.open("rb")
    except OSError as e:
        raise ArchiveError(f"cannot open {path}: {e.strerror or e}") from None
    with raw:
        try:
            dec = age.Decryptor(raw, passphrase)
            header: dict[str, Any] | None = None
            manifest: dict[str, Any] | None = None
            seen: dict[str, tuple[int, str]] = {}
            dirs: set[str] = set()
            with tarfile.open(fileobj=dec, mode="r|gz") as tar:
                for member in tar:
                    if manifest is not None:
                        raise ArchiveError("the archive has data after its manifest")
                    if header is None:
                        if member.name != f"{META}/format.json":
                            raise ArchiveError("this is not an agentos backup (no format.json first)")
                        header = _read_json_member(tar, member)
                        if header.get("format") != FORMAT:
                            raise ArchiveError("this is not an agentos backup")
                        if header.get("version") != FORMAT_VERSION:
                            raise ArchiveError(f"backup format {header.get('version')} is not supported by "
                                               f"agentos {__version__}; restore it with a newer agentos")
                        continue
                    if member.name == f"{META}/manifest.json":
                        manifest = _read_json_member(tar, member)
                        continue
                    rel = _safe_rel(member.name)
                    if member.isdir():
                        dirs.add(rel)
                        on_dir(rel)
                    elif member.isreg():
                        if rel in seen:
                            raise ArchiveError(f"{rel} appears twice in the archive")
                        f = tar.extractfile(member)
                        assert f is not None
                        seen[rel] = (member.size, on_file(rel, member, f))
                    else:
                        raise ArchiveError(f"{member.name} is not a regular file or directory")
        except age.WrongPassphrase:
            raise ArchiveError("wrong passphrase") from None
        except age.TruncatedArchive:
            raise ArchiveError(f"{path.name} is truncated (an interrupted backup or copy); use another backup") from None
        except age.NotAgeFile:
            raise ArchiveError(f"{path.name} is not an encrypted agentos backup") from None
        except age.CorruptArchive as e:
            raise ArchiveError(f"{path.name}: {e}; use another backup") from None
        except (tarfile.TarError, EOFError, OSError) as e:
            raise ArchiveError(f"{path.name} is damaged ({type(e).__name__}); use another backup") from None
    if header is None or manifest is None:
        raise ArchiveError(f"{path.name} is incomplete (no manifest); use another backup")
    listed = {f["path"]: (f["size"], f["sha256"]) for f in manifest.get("files", [])}
    if set(listed) != set(seen):
        missing = sorted(set(listed) - set(seen))[:3]
        extra = sorted(set(seen) - set(listed))[:3]
        raise ArchiveError(f"the manifest does not match the archive (missing {missing}, unlisted {extra})")
    bad = [rel for rel, got in seen.items() if got != listed[rel]]
    if bad:
        raise ArchiveError(f"checksum mismatch for {', '.join(bad[:3])}")
    return ArchiveContents(header=header, manifest=manifest, files=len(seen),
                           bytes=sum(size for size, _ in seen.values()))


def _hash_stream(f: BinaryIO, sink: BinaryIO | None = None) -> str:
    digest = hashlib.sha256()
    while chunk := f.read(1024 * 1024):
        digest.update(chunk)
        if sink is not None:
            sink.write(chunk)
    return digest.hexdigest()


def verify_backup(path: Path, passphrase: str, *, check_sqlite: bool = True) -> ArchiveContents:
    """Decrypt and read the whole archive without writing it anywhere: every checksum, the
    manifest, and (check_sqlite) an integrity check of each SQLite database in it."""
    checked: list[str] = []
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="agentos-verify-") as tmp:
        os.chmod(tmp, 0o700)

        def on_file(rel: str, member: tarfile.TarInfo, f: BinaryIO) -> str:
            if not check_sqlite or not rel.endswith(SQLITE_SUFFIXES) or rel.startswith("lanes/"):
                return _hash_stream(f)
            spool = Path(tmp) / "db"
            with open(spool, "wb") as out:
                digest = _hash_stream(f, out)
            if is_sqlite(spool):
                try:
                    conn = sqlite3.connect(spool)
                    try:
                        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    finally:
                        conn.close()
                except sqlite3.Error as e:
                    ok = type(e).__name__
                (checked if ok == "ok" else problems).append(rel)
            spool.unlink(missing_ok=True)
            return digest

        contents = _walk_archive(Path(path), passphrase, on_file, lambda rel: None)
    if problems:
        raise ArchiveError(f"database(s) in the backup fail their integrity check: {', '.join(problems)}")
    contents.sqlite_checked = checked
    return contents


# ---- restore ----------------------------------------------------------------------------

@dataclass
class RestoreResult:
    home: Path
    files: int
    bytes: int
    lanes: list[str]
    moved_aside: Path | None
    node: str | None
    created_at: str | None
    permission_problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"home": str(self.home), "files": self.files, "bytes": self.bytes, "lanes": self.lanes,
                "moved_aside": str(self.moved_aside) if self.moved_aside else None, "node": self.node,
                "created_at": self.created_at, "permission_problems": self.permission_problems}


def daemon_running_on(home: Path, host: Host) -> str | None:
    """Why we think agentosd (or a browser) is using this home, or None."""
    for sock in (ops_socket(home), vault_socket(home)):
        if host.unix_request(sock, {"op": "ping" if sock.name == "ops.sock" else "status"}, timeout=2) is not None:
            return f"a daemon answers on {sock}"
    lanes_root = str(Path(home).resolve() / "lanes") + "/"
    for pid, argv in host.processes():
        if argv and os.path.basename(argv[0]) == "camoufox-bin" and any(a.startswith(lanes_root) for a in argv[1:]):
            return f"a browser (pid {pid}) runs a lane under {home}"
    return None


def _lock_down(root: Path, uid: int | None = None, gid: int | None = None) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            p = Path(dirpath, name)
            os.chmod(p, 0o700, follow_symlinks=False)
            if uid is not None:
                os.chown(p, uid, gid if gid is not None else -1, follow_symlinks=False)
        for name in filenames:
            p = Path(dirpath, name)
            os.chmod(p, 0o600, follow_symlinks=False)
            if uid is not None:
                os.chown(p, uid, gid if gid is not None else -1, follow_symlinks=False)
    os.chmod(root, 0o700)
    if uid is not None:
        os.chown(root, uid, gid if gid is not None else -1)


UNIT_FILE = Path("/etc/systemd/system/agentosd.service")


def unit_user(home: Path, unit: Path | None = None) -> str | None:
    """The user the installed agentosd unit runs as, when that unit serves this home, else None."""
    try:
        text = (unit or UNIT_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    if str(Path(home)) not in text:
        return None
    for line in text.splitlines():
        key, _, value = line.strip().partition("=")
        if key == "User" and value.strip() and value.strip() != "root":
            return value.strip()
    return None


def restore_backup(path: Path, passphrase: str, home: Path, *, force: bool = False, owner: str | None = None,
                   host: Host | None = None, now: datetime | None = None) -> RestoreResult:
    host = host or Host()
    home = Path(home).absolute()
    uid = gid = None
    if owner is not None:
        if host.euid() != 0:
            raise BackupRefused("--owner needs root")
        import pwd

        try:
            pw = pwd.getpwnam(owner)
        except KeyError:
            raise BackupRefused(f"no user named {owner}") from None
        uid, gid = pw.pw_uid, pw.pw_gid
    elif home.exists() and host.euid() == 0 and os.stat(home).st_uid != 0:
        st = os.stat(home)  # root restoring over a service user's home keeps it the user's
        uid, gid = st.st_uid, st.st_gid
    elif host.euid() == 0 and (service := unit_user(home)) is not None:
        import pwd  # a node whose agentosd runs as a service user: the restored home is that user's

        try:
            pw = pwd.getpwnam(service)
            uid, gid = pw.pw_uid, pw.pw_gid
        except KeyError:
            pass
    if home.exists() and not home.is_dir():
        raise BackupRefused(f"{home} exists and is not a directory")
    occupied = home.exists() and any(home.iterdir())
    if occupied and not force:
        raise BackupRefused(f"{home} is not empty; restore into an empty directory, or pass --force to move the "
                            "current home aside first")
    if home.exists():
        why = daemon_running_on(home, host)
        if why:
            raise BackupRefused(f"{why}; stop agentosd before restoring (sudo systemctl stop agentosd)")
    # the restore is staged next to the home and renamed over it, so it needs to write the parent
    try:
        home.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(home.parent, os.W_OK | os.X_OK):
            raise PermissionError(home.parent)
        staging = home.parent / f".{home.name}.restore-{pysecrets.token_hex(4)}"
        staging.mkdir(mode=0o700)
    except OSError:
        raise BackupRefused(
            f"a restore replaces {home} as a whole and stages next to it, and this user cannot write "
            f"{home.parent}. Run it as root (on a node: `sudo agentos backup restore ARCHIVE --force`); the "
            "restored home goes to the user agentosd runs as") from None
    try:
        def ensure_parent(target: Path) -> None:
            parent = target.parent
            if not parent.exists():
                parent.mkdir(mode=0o700, parents=True)

        def on_dir(rel: str) -> None:
            target = staging / rel
            target.mkdir(mode=0o700, parents=True, exist_ok=True)

        def on_file(rel: str, member: tarfile.TarInfo, f: BinaryIO) -> str:
            target = staging / rel
            ensure_parent(target)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as out:
                digest = _hash_stream(f, out)
                out.flush()
                os.fsync(out.fileno())
            os.utime(target, (member.mtime, member.mtime))
            return digest

        contents = _walk_archive(Path(path), passphrase, on_file, on_dir)
        _lock_down(staging, uid, gid)
        moved = None
        if home.exists():
            if occupied:
                stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
                moved = home.with_name(f"{home.name}.before-restore-{stamp}")
                os.replace(home, moved)
            else:
                home.rmdir()
        try:
            os.replace(staging, home)
        except BaseException:
            if moved is not None and not home.exists():
                os.replace(moved, home)  # put the old home back
            raise
        fsync_dir(home.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    from agentos.ops.doctor import DoctorContext, check_permissions

    perms = check_permissions(DoctorContext(home=home, host=host))
    problems = perms[0].data.get("problems", []) if perms and perms[0].status != "pass" else []
    return RestoreResult(home=home, files=contents.files, bytes=contents.bytes,
                         lanes=[lane["id"] for lane in contents.manifest.get("lanes", [])], moved_aside=moved,
                         node=contents.header.get("node"), created_at=contents.header.get("created_at"),
                         permission_problems=problems)
