"""Where things live under OPENAJAR_HOME, as ops sees them.

Ops reads node data without importing the node implementation: lanes are directories
`lanes/<workspace>/<name>/` (profile/, fingerprint.json), lane specs are rows in openajar.db
(read-only here), and a browser "runs a lane" when a camoufox-bin process has that profile on
its command line.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from openajar.core import fs
from openajar.core.models import LANE_ID_RE, DeviceSpec, LaneSpec

BROWSER_BIN = "camoufox-bin"
PROFILE_LOCKS = ("lock", ".parentlock", "parent.lock")
SQLITE_MAGIC = b"SQLite format 3\x00"


@dataclass(frozen=True)
class LaneDir:
    id: str
    path: Path

    @property
    def profile(self) -> Path:
        return self.path / "profile"

    @property
    def fingerprint(self) -> Path:
        return self.path / "fingerprint.json"


def run_dir(home: Path) -> Path:
    return Path(home) / "run"


# The longest unix socket path the kernel takes (sun_path less its NUL): 104 bytes on macOS, 108 on Linux.
SUN_PATH_MAX = 103 if sys.platform == "darwin" else 107
SHORT_RUN_ROOT = Path("/tmp")
_log = logging.getLogger("openajar.ops")


class SocketDirUnsafe(OSError):
    """The short socket directory for a long OPENAJAR_HOME is not this user's private directory."""


def short_run_dir(home: Path) -> Path:
    """Where a home too long for a unix socket keeps its sockets: /tmp/openajar-<16 hex of the home>,
    the same for the daemon and every CLI that asks it, mode 700 and owned by the daemon's user."""
    digest = hashlib.sha256(os.fsencode(os.path.abspath(home))).hexdigest()[:16]
    return SHORT_RUN_ROOT / f"openajar-{digest}"


def socket_path(home: Path, name: str) -> Path:
    """OPENAJAR_HOME/run/NAME, or under short_run_dir when that path is longer than a unix socket may be
    (a deep --home, a test's temp dir): a long path would make the socket fail to bind."""
    path = run_dir(home) / name
    if len(os.fsencode(str(path))) <= SUN_PATH_MAX:
        return path
    return short_run_dir(home) / name


def check_socket_dir(directory: Path, *, create: bool) -> None:
    """Refuse a socket directory that is a symlink, another user's, or open to group or others, so a
    socket there is this user's own; `create` makes it (mode 700) when it is missing."""
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        st = directory.lstat()
    except FileNotFoundError:
        return
    fix = f"remove {directory} and start agentosd again, or use a shorter OPENAJAR_HOME"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise SocketDirUnsafe(f"{directory} is not a plain directory; {fix}")
    if st.st_uid != os.geteuid():
        raise SocketDirUnsafe(f"{directory} belongs to another user (uid {st.st_uid}); {fix}")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        os.chmod(directory, 0o700)


def trusted_socket(path: Path) -> None:
    """Before a client sends to `path`: a socket in the short directory must sit in one that this user
    or root owns, never one another user made first in /tmp (it could listen in their place)."""
    path = Path(path)
    if path.parent.parent != SHORT_RUN_ROOT:
        return
    try:
        st = path.parent.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or st.st_uid not in (os.geteuid(), 0) or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SocketDirUnsafe(f"{path.parent} is not this user's private directory; remove it and start "
                              "agentosd again, or use a shorter OPENAJAR_HOME")


def prepare_socket(path: Path) -> None:
    """Before a server binds `path`: its directory exists and is private, and the path fits. Raises an
    OSError that says what to do, never a bare "path too long"."""
    path = Path(path)
    if len(os.fsencode(str(path))) > SUN_PATH_MAX:
        raise OSError(f"the socket path {path} is {len(os.fsencode(str(path)))} bytes, over the {SUN_PATH_MAX} a "
                      "unix socket allows; use a shorter OPENAJAR_HOME")
    check_socket_dir(path.parent, create=True)
    if path.parent.parent == SHORT_RUN_ROOT:
        _log.info("sockets: the home's path is too long for a unix socket, so %s is in %s", path.name, path.parent)


def ops_socket(home: Path) -> Path:
    return socket_path(home, "ops.sock")


def vault_socket(home: Path) -> Path:
    return socket_path(home, "vault.sock")


def default_backup_dir(home: Path) -> Path:
    return Path(home) / "backups"


def lane_dirs(home: Path) -> list[LaneDir]:
    """Every `lanes/<ws>/<name>` directory with a valid lane id, sorted."""
    root = Path(home) / "lanes"
    out: list[LaneDir] = []
    if not root.is_dir():
        return out
    for ws in sorted(root.iterdir()):
        if not ws.is_dir() or ws.is_symlink():
            continue
        for lane in sorted(ws.iterdir()):
            lane_id = f"{ws.name}/{lane.name}"
            if lane.is_dir() and not lane.is_symlink() and LANE_ID_RE.match(lane_id):
                out.append(LaneDir(lane_id, lane))
    return out


def connect_ro(path: Path, timeout: float = 5.0) -> sqlite3.Connection:
    """A read-only connection that creates nothing next to the database.

    A plain mode=ro open of a WAL database creates -wal and -shm files and leaves them behind.
    With no -wal file nobody has the database open, so it is opened immutable (no locks, no
    side files); otherwise the writer's -wal and -shm already exist and mode=ro reads through them.
    """
    path = Path(path).resolve()
    if not path.exists():
        raise sqlite3.OperationalError(f"no database at {path}")
    wal = path.with_name(path.name + "-wal")
    mode = "mode=ro" if wal.exists() else "mode=ro&immutable=1"
    return sqlite3.connect(f"{path.as_uri()}?{mode}", uri=True, timeout=timeout)


def lane_specs(home: Path) -> dict[str, LaneSpec]:
    """Lane specs from openajar.db (empty when the database or its table is missing)."""
    db = Path(home) / "openajar.db"
    if not db.exists():
        return {}
    try:
        conn = connect_ro(db)
        try:
            rows = conn.execute("SELECT spec FROM lanes").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    out: dict[str, LaneSpec] = {}
    for (raw,) in rows:
        try:
            spec = LaneSpec.model_validate_json(raw)
        except ValueError:
            continue
        out[spec.id] = spec
    return out


def device_specs(home: Path) -> dict[str, DeviceSpec]:
    """Devices from openajar.db (AUD-02; empty when the database or its table is missing)."""
    db = Path(home) / "openajar.db"
    if not db.exists():
        return {}
    try:
        conn = connect_ro(db)
        try:
            rows = conn.execute("SELECT spec FROM devices").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    out: dict[str, DeviceSpec] = {}
    for (raw,) in rows:
        try:
            device = DeviceSpec.model_validate_json(raw)
        except ValueError:
            continue
        out[device.name] = device
    return out


def lane_pin_path(home: Path, lane_id: str, specs: dict[str, LaneSpec] | None = None) -> Path:
    """The pin a lane launches with: its device's (devices/<name>/fingerprint.json) when it is on a
    device, else its own. The lane's own file is a copy of the device's for a device lane."""
    spec = (specs if specs is not None else lane_specs(home)).get(lane_id)
    if spec is not None and spec.device:
        return Path(home) / "devices" / spec.device / "fingerprint.json"
    ws, name = lane_id.split("/", 1)
    return Path(home) / "lanes" / ws / name / "fingerprint.json"


def is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def read_json(path: Path) -> dict:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise TypeError("not a JSON object")
    return data


def owner_of(path: Path) -> tuple[int, int] | None:
    """(uid, gid) of path when root is acting on a home a service user owns; else None."""
    if os.geteuid() != 0:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_uid, st.st_gid) if st.st_uid != 0 else None


def adopt_owner(path: Path, like: Path, *, recursive: bool = False) -> None:
    """When root writes into a service user's home, give what it wrote to that user, so the
    service can still read and write it. A no-op for everyone else."""
    owner = owner_of(like)
    if owner is None:
        return
    paths = [Path(path)]
    if recursive and Path(path).is_dir():
        paths += list(Path(path).rglob("*"))
    for p in paths:
        try:
            os.chown(p, *owner, follow_symlinks=False)
        except OSError:
            pass


def write_private(path: Path, data: bytes) -> None:
    """Write a file readable only by its owner (600), replaced atomically, never through a symlink.
    A replaced file keeps its owner (root editing a service user's file must not take it over)."""
    path = Path(path)
    owner = owner_of(path) if path.exists() else owner_of(path.parent)
    fs.write_private(path, data, owner=owner)


def write_private_json(path: Path, data: object) -> None:
    write_private(path, (json.dumps(data, indent=1) + "\n").encode())


fsync_dir = fs.fsync_dir


def proxy_users(home: Path, label: str) -> list[str]:
    """What reaches its sites through proxy `label`: lanes of their own, and devices with their lanes.
    Said before a label is pointed elsewhere, which is a new IP for every one of them."""
    specs = lane_specs(home)
    users = sorted(i for i, s in specs.items() if s.proxy_label == label and not s.device)
    for name, device in sorted(device_specs(home).items()):
        if device.proxy_label == label:
            on = sorted(i for i, s in specs.items() if s.device == name)
            users.append(f"device {name} with {', '.join(on) or 'no lanes'}")
    return users
