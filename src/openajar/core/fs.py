"""Writing private files the one way: never readable by anyone else, never half written.

Every file that holds a secret, a pin or a store goes through `write_private`:

- **Mode rule**: the temp file is created with `mode` (600 by default) and `fchmod`ed to it, so the
  umask cannot widen it and the final file is never wider, not even for a moment.
- **Symlink rule**: the temp file is opened with `O_NOFOLLOW | O_EXCL` under a fresh random name,
  and the final step is a rename (or a hard link), which replaces a symlink at `path` itself and
  never writes through it.
- **Durability**: the data is fsynced before the rename and the directory is fsynced after it, so
  after a power cut the file is either the old one or the new one, never zero length.
- **No leftovers**: a temp file is `.<name>.<pid>.<hex>.tmp`; a write removes the ones a dead
  writer left (a kill or a power cut mid write) once they are an hour old, and `is_temp` tells
  them apart from real files.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

_TEMP = re.compile(r"\.(?P<name>.+)\.(?P<pid>\d+)\.[0-9a-f]{8}\.tmp")


def is_temp(path: Path | str) -> bool:
    """A temp file write_private makes (and removes), not a file of its own."""
    return _TEMP.fullmatch(Path(path).name) is not None


STALE_TEMP_S = 3600.0  # a write takes milliseconds: a temp this old was left by a writer that died


def _sweep_temps(path: Path) -> None:
    """Remove the temp files of `path` that a writer which is gone left behind: an hour old, and
    its pid not running here (a pid in another PID namespace can look dead, so the age decides
    first). Older versions used `.<name>.tmp`, which only a killed write leaves, so it goes too.
    Never fails the write that called it."""
    try:
        from openajar.core.proc import pid_alive

        now = time.time()
        for name in os.listdir(path.parent):
            if not name.startswith(f".{path.name}.") or not name.endswith(".tmp"):
                continue
            m = _TEMP.fullmatch(name)
            if m is not None:
                if m["name"] != path.name or int(m["pid"]) == os.getpid():
                    continue
            elif name != f".{path.name}.tmp":
                continue
            temp = path.parent / name
            with contextlib.suppress(OSError, ValueError, OverflowError):
                st = os.lstat(temp)
                if now - st.st_mtime < STALE_TEMP_S or (m is not None and pid_alive(int(m["pid"]))):
                    continue
                os.unlink(temp)
    except Exception:  # noqa: BLE001,S110 - tidying up: the write itself is done
        pass


def fsync_dir(path: Path) -> None:
    """Flush a directory entry (a rename or a link in it) to disk. Best effort: some filesystems
    refuse to open or fsync a directory."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_private(path: Path | str, data: bytes | str, *, mode: int = 0o600, exclusive: bool = False,
                  owner: tuple[int, int] | None = None) -> None:
    """Write `data` to `path` atomically and durably, readable only as `mode` allows.

    `exclusive`: only when no file is at `path` yet (FileExistsError otherwise, and nothing is
    written), so two writers racing for a first pin never overwrite each other.
    `owner`: (uid, gid) the file is given before it appears (root writing a service user's file).
    The parent directory is created when missing."""
    path = Path(path)
    raw = data.encode() if isinstance(data, str) else data
    path.parent.mkdir(parents=True, exist_ok=True)
    # A temp name of its own, so two writers of the same file never share one.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        try:
            os.fchmod(fd, mode)
            if owner is not None:
                os.fchown(fd, *owner)
            view = memoryview(raw)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        if exclusive:
            os.link(tmp, path)  # atomic, and never over a file another writer made meanwhile
        else:
            os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    fsync_dir(path.parent)
    _sweep_temps(path)


def write_private_json(path: Path | str, data: Any, *, exclusive: bool = False, mode: int = 0o600,
                       owner: tuple[int, int] | None = None, newline: bool = False) -> None:
    text = json.dumps(data, indent=1) + ("\n" if newline else "")
    write_private(path, text, mode=mode, exclusive=exclusive, owner=owner)


# ---- files someone else controls: never follow a link -----------------------------------------
#
# A task's work and home dirs belong to the brain (and hold whatever a cloned repo or an archive
# brought), so any name in them can be a symlink or a hard link to a host file. agentosd still
# makes, writes, reads and deletes files there, as its own user (root when it drops to
# the brain user). The rule for those paths:
#
# - **Anchor**: start from a directory the brain cannot replace (the brain user's home itself,
#   or a directory of agentosd's own), and resolve the rest one component at a time with
#   `O_NOFOLLOW` against the directory fd before it (openat). A link anywhere below the anchor
#   refuses the step (LinkRefused), so a link swapped in after a check is refused too: there is
#   no check separate from the open.
# - **Writes** go to a fresh `O_EXCL | O_NOFOLLOW` temp name in the resolved directory and are
#   renamed over the final name, which replaces a link or a hard link there and never writes
#   through it.
# - **Reads** open the final name with `O_NOFOLLOW | O_NONBLOCK` and take only a regular file
#   with one link (a hard link to a host file has two), up to a size limit.
# - **Deletes** unlink the final name in the resolved directory (unlink never follows).
# - **Owners** are set with fchown on what was just created (`owner=`), never by walking a tree
#   afterwards: a walk would chown a hard link to a host file along with the task's own files.

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW


class LinkRefused(OSError):
    """A name under the anchor is a link (or is not what it must be): agentosd will not follow it."""

    def __init__(self, path: str, what: str = "is a link"):
        super().__init__(errno.ELOOP, f"{path} {what}; agentosd never follows a link in files it does not own")
        self.path = path


def _parts(rel: PurePosixPath | str) -> list[str]:
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts:
        raise LinkRefused(str(rel), "leaves its anchor")
    return [part for part in p.parts if part not in ("", ".")]


def _refuse_if_link(dir_fd: int, name: str, shown: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        if stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode):
            raise LinkRefused(shown)


def open_dir(anchor: Path | str, rel: PurePosixPath | str = ".", *, make: bool = False, mode: int = 0o700,
             owner: tuple[int, int] | None = None) -> int:
    """An fd for the directory `anchor/rel`, every component below `anchor` opened without
    following a link. `make`: create missing components with `mode` (and `owner`). The caller
    closes the fd."""
    fd = os.open(anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    shown = Path(anchor)
    try:
        for part in _parts(rel):
            shown = shown / part
            made = False
            if make:
                try:
                    os.mkdir(part, mode, dir_fd=fd)
                    made = True
                except FileExistsError:
                    pass
            try:
                nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                    _refuse_if_link(fd, part, str(shown))
                    raise LinkRefused(str(shown), "is not a directory") from exc
                raise
            os.close(fd)
            fd = nfd
            if made:
                os.fchmod(fd, mode)
                if owner is not None:
                    os.fchown(fd, *owner)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _split(rel: PurePosixPath | str) -> tuple[PurePosixPath, str]:
    parts = _parts(rel)
    if not parts:
        raise LinkRefused(str(rel), "names no file")
    return PurePosixPath(*parts[:-1]) if parts[:-1] else PurePosixPath("."), parts[-1]


def _write_at(dir_fd: int, name: str, chunks: Iterator[bytes], *, mode: int, owner: tuple[int, int] | None) -> None:
    tmp = f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    try:
        try:
            os.fchmod(fd, mode)
            if owner is not None:
                os.fchown(fd, *owner)
            for chunk in chunks:
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)  # replaces a link, never writes through it
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dir_fd)


def write_beneath(anchor: Path | str, rel: PurePosixPath | str, data: bytes | str, *, mode: int = 0o600,
                  owner: tuple[int, int] | None = None, dir_mode: int = 0o700, refuse_link: bool = False) -> None:
    """Write `anchor/rel` atomically, never through a link: directories on the way are made
    (`dir_mode`, `owner`) and must not be links; a link at the final name is replaced, or with
    `refuse_link` refused (LinkRefused)."""
    parent, name = _split(rel)
    raw = data.encode() if isinstance(data, str) else data
    dfd = open_dir(anchor, parent, make=True, mode=dir_mode, owner=owner)
    try:
        if refuse_link:
            _refuse_if_link(dfd, name, str(Path(anchor) / rel))
        _write_at(dfd, name, iter([raw]), mode=mode, owner=owner)
    finally:
        os.close(dfd)


def read_beneath(anchor: Path | str, rel: PurePosixPath | str, *, limit: int = 1_000_000) -> bytes | None:
    """The bytes of the regular file `anchor/rel`, or None when there is none. A link on the way
    or at the name, a hard link (more than one link), a FIFO or device, or a file over `limit`
    bytes is refused (LinkRefused)."""
    parent, name = _split(rel)
    try:
        dfd = open_dir(anchor, parent)
    except FileNotFoundError:
        return None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise LinkRefused(str(Path(anchor) / rel)) from exc
            raise
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise LinkRefused(str(Path(anchor) / rel), "is not a regular file")
            if st.st_nlink != 1:
                raise LinkRefused(str(Path(anchor) / rel), "is a hard link")
            out = bytearray()
            while len(out) <= limit:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                out.extend(chunk)
            if len(out) > limit:
                raise LinkRefused(str(Path(anchor) / rel), f"is over {limit} bytes")
            return bytes(out)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def unlink_beneath(anchor: Path | str, rel: PurePosixPath | str) -> None:
    """Remove the name `anchor/rel` (a file or a link; a link is removed, never its target).
    Missing is fine; a link on the way is refused (LinkRefused)."""
    parent, name = _split(rel)
    try:
        dfd = open_dir(anchor, parent)
    except FileNotFoundError:
        return
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=dfd)
    finally:
        os.close(dfd)


def copy_tree_beneath(src: Path | str, anchor: Path | str, rel: PurePosixPath | str, *,
                      owner: tuple[int, int] | None = None) -> None:
    """Copy the tree at `src` (agentosd's own, nobody else writes it) into `anchor/rel`, never
    following a link on the destination side. Links in `src` are copied as links (their
    targets are never read), regular files and directories keep their permission bits (never
    setuid), anything else is skipped. A hard link becomes a file of its own."""
    src = Path(src)
    top = open_dir(anchor, rel, make=True, mode=0o700, owner=owner)
    try:
        for root, dirs, files, root_fd in os.fwalk(src, follow_symlinks=False):
            sub = PurePosixPath(os.path.relpath(root, src))
            dfd = top if sub == PurePosixPath(".") else open_dir(anchor, PurePosixPath(rel) / sub)
            try:
                for n in sorted((*dirs, *files)):
                    st = os.lstat(n, dir_fd=root_fd)
                    _refuse_if_link(dfd, n, str(Path(anchor) / rel / sub / n))
                    if stat.S_ISDIR(st.st_mode):
                        with contextlib.suppress(FileExistsError):
                            os.mkdir(n, 0o700, dir_fd=dfd)
                        cfd = os.open(n, _DIR_FLAGS, dir_fd=dfd)
                        try:
                            os.fchmod(cfd, stat.S_IMODE(st.st_mode) & 0o777)
                            if owner is not None:
                                os.fchown(cfd, *owner)
                        finally:
                            os.close(cfd)
                    elif stat.S_ISLNK(st.st_mode):
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(n, dir_fd=dfd)
                        os.symlink(os.readlink(n, dir_fd=root_fd), n, dir_fd=dfd)
                        if owner is not None:
                            os.chown(n, *owner, dir_fd=dfd, follow_symlinks=False)
                    elif stat.S_ISREG(st.st_mode):
                        sfd = os.open(n, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
                        with os.fdopen(sfd, "rb") as fh:
                            _write_at(dfd, n, iter(functools.partial(fh.read, 1 << 20), b""),
                                      mode=stat.S_IMODE(st.st_mode) & 0o777, owner=owner)
            finally:
                if dfd != top:
                    os.close(dfd)
    finally:
        os.close(top)
