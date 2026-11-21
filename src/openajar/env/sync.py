"""Working-tree sync into a lane (`openajar run`): content-addressed chunks, then one extraction.

A client sends its files as chunks named by their sha256 (`terminal_upload`), so a second run sends only
what changed, and then a manifest: every path with its mode, mtime and chunk list (itself uploaded as
chunks). Applying it (terminal_job_start's `sync`) builds a tar archive on the node from the chunks and
extracts it *inside* the lane with the lane's own `tar`, as a workload like any command: the lane's
filesystem is only ever written from inside its sandbox, so nothing the lane planted (a symlink to
/etc) can steer a write on the node. Paths the previous manifest had and this one does not are
deleted the same way. Files the command made (a .venv, node_modules, caches) are not in any manifest,
so they stay between runs; `clean` wipes the destination first.

Chunks live outside the lane in `<lane dir>/sync/chunks`, capped per lane (`upload_max_mb`), and
whatever the applied manifests no longer use is dropped after each apply.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import time
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import BaseModel, Field, ValidationError, field_validator

from openajar.core.errors import InvalidRequest, NotFound

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# one path segment: no "." or "..", no leading dash (so it never reads as an option), no NUL or slash
SEGMENT_RE = re.compile(r"^[^\x00/]{1,255}$")
DEST_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
MAX_PATH = 4096
MAX_FILES = 200_000
MAX_MANIFEST_CHUNKS = 256
MANIFEST_VERSION = 1
GC_GRACE_S = 3600.0  # an unused chunk younger than this may belong to a sync about to start


def check_sha(value: Any) -> str:
    if not isinstance(value, str) or not SHA_RE.match(value):
        raise InvalidRequest(f"not a sha256 chunk name: {str(value)[:80]!r}")
    return value


def check_rel_path(value: Any) -> str:
    """A path relative to the synced tree: no absolute path, no `.`, `..` or empty segment."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH:
        raise InvalidRequest("a synced path is a non-empty relative path")
    parts = value.split("/")
    if any(p in ("", ".", "..") or not SEGMENT_RE.match(p) for p in parts):
        raise InvalidRequest(f"not a plain relative path: {value[:200]!r}")
    return value


def check_dir(value: Any, what: str) -> str:
    """A directory under the lane's home (the default working directory of every command): plain
    segments of letters, digits, dots, dashes and underscores, so it is safe in a shell word too."""
    if not isinstance(value, str) or not value or len(value) > 400:
        raise InvalidRequest(f"{what} is a relative directory such as src/myrepo")
    if not all(DEST_SEGMENT_RE.match(p) and p not in (".", "..") for p in value.split("/")):
        raise InvalidRequest(f"{what} must be a relative path of plain names (letters, digits, . _ -): {value[:200]!r}")
    return value


def check_cwd(value: Any, *, remote: bool) -> str:
    """A command's working directory: on a lane of the node, a directory under the lane's home
    (check_dir); on a VM lane, also an absolute path of plain names (/work/shop), since the whole
    machine is the agent's. Safe as one shell word either way."""
    if remote and isinstance(value, str) and value.startswith("/") and value != "/":
        check_dir(value[1:].rstrip("/"), "cwd")
        return value.rstrip("/")
    return check_dir(value, "cwd")


class Entry(BaseModel):
    """One synced path: a regular file (its chunks, in order) or a symlink (`link`)."""
    path: str
    mode: int = 0o644
    mtime: int | None = None
    size: int = Field(default=0, ge=0)
    chunks: list[str] = Field(default_factory=list)
    link: str | None = None

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return check_rel_path(v)

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: int) -> int:
        return 0o755 if v & 0o111 else 0o644  # the exec bit only: no setuid, no world-writable files

    @field_validator("chunks")
    @classmethod
    def _chunks(cls, v: list[str]) -> list[str]:
        return [check_sha(c) for c in v]

    @field_validator("link")
    @classmethod
    def _link(cls, v: str | None) -> str | None:
        if v is not None and (not v or "\x00" in v or len(v) > MAX_PATH):
            raise ValueError("a symlink target is a non-empty path")
        return v

    def signature(self) -> str:
        """What decides whether the lane's copy changed (the mtime alone does not)."""
        body = json.dumps([self.mode, self.link, self.chunks], separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()[:32]


class Manifest(BaseModel):
    v: int = MANIFEST_VERSION
    files: list[Entry] = Field(default_factory=list, max_length=MAX_FILES)

    def check(self) -> None:
        """Refuse what tar could not extract as listed: a path twice, or a path under a symlink."""
        if self.v != MANIFEST_VERSION:
            raise InvalidRequest(f"manifest version {self.v} is not supported (this node reads {MANIFEST_VERSION})")
        seen: set[str] = set()
        links = set()
        for e in self.files:
            if e.path in seen:
                raise InvalidRequest(f"the manifest lists {e.path} twice")
            seen.add(e.path)
            if e.link is not None:
                links.add(e.path)
        for e in self.files:
            parts = e.path.split("/")
            for i in range(1, len(parts)):
                parent = "/".join(parts[:i])
                if parent in links:
                    raise InvalidRequest(f"{e.path} is under the symlink {parent}")
                if parent in seen:
                    raise InvalidRequest(f"{e.path} is under the file {parent}")

    def chunk_set(self) -> set[str]:
        return {c for e in self.files for c in e.chunks}

    def total_bytes(self) -> int:
        return sum(e.size for e in self.files)


class ChunkStore:
    """Chunks by sha256 under `root`, written atomically after their hash is checked. Blocking:
    call it through asyncio.to_thread."""

    def __init__(self, root: Path, *, max_bytes: int, chunk_max_bytes: int):
        self.root = root
        self.dir = root / "chunks"
        self.max_bytes = max_bytes
        self.chunk_max_bytes = chunk_max_bytes
        self._size: int | None = None

    def path(self, sha: str) -> Path:
        return self.dir / sha[:2] / sha

    def has(self, sha: str) -> bool:
        return self.path(check_sha(sha)).is_file()

    def missing(self, shas: list[str]) -> list[str]:
        return [s for s in dict.fromkeys(shas) if not self.has(s)]

    def size(self) -> int:
        if self._size is None:
            total = 0
            if self.dir.is_dir():
                for dirpath, _dirs, files in os.walk(self.dir):
                    for f in files:
                        try:
                            total += os.stat(os.path.join(dirpath, f)).st_size
                        except OSError:
                            pass
            self._size = total
        return self._size

    def put(self, sha: str, data: bytes) -> int:
        """Store one chunk; returns the bytes added (0 when it was there already)."""
        check_sha(sha)
        if len(data) > self.chunk_max_bytes:
            raise InvalidRequest(f"a chunk is at most {self.chunk_max_bytes} bytes")
        if hashlib.sha256(data).hexdigest() != sha:
            raise InvalidRequest(f"chunk {sha[:12]} does not match its sha256 (damaged in transit?)")
        target = self.path(sha)
        if target.is_file():
            return 0
        if self.size() + len(data) > self.max_bytes:
            raise InvalidRequest(f"this lane's upload store is full ({self.max_bytes // 1048576} MB, upload_max_mb in "
                                 "env.toml); sync fewer files, or run with --fresh to drop what older syncs left")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{sha}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        self._size = self.size() + len(data)
        return len(data)

    def read(self, sha: str) -> bytes:
        try:
            return self.path(check_sha(sha)).read_bytes()
        except FileNotFoundError:
            raise NotFound(f"chunk {sha[:12]} is not on the node; upload it first (terminal_upload)") from None

    def gc(self, keep: set[str], *, grace_s: float = GC_GRACE_S) -> int:
        """Drop every chunk not in `keep` and older than `grace_s` (one uploaded for a sync that has
        not started yet, another run's, stays); returns how many."""
        dropped = 0
        if not self.dir.is_dir():
            return 0
        cutoff = time.time() - grace_s
        for sub in self.dir.iterdir():
            if not sub.is_dir():
                continue
            for f in sub.iterdir():
                if f.name in keep:
                    continue
                try:
                    if f.stat().st_mtime > cutoff:
                        continue
                except OSError:
                    continue
                f.unlink(missing_ok=True)
                dropped += 1
        self._size = None
        return dropped


def load_manifest(store: ChunkStore, shas: list[str]) -> Manifest:
    if not isinstance(shas, list) or not shas or len(shas) > MAX_MANIFEST_CHUNKS:
        raise InvalidRequest(f"sync.manifest is a list of 1 to {MAX_MANIFEST_CHUNKS} chunk names")
    missing = store.missing([check_sha(s) for s in shas])
    if missing:
        raise NotFound(f"{len(missing)} manifest chunk(s) are not on the node; upload them first (terminal_upload)")
    raw = b"".join(store.read(s) for s in shas)
    try:
        manifest = Manifest.model_validate_json(raw)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(x) for x in first.get("loc", ()))
        raise InvalidRequest(f"the sync manifest is not valid ({where}: {first.get('msg')})") from None
    manifest.check()
    missing = store.missing(sorted(manifest.chunk_set()))
    if missing:
        raise NotFound(f"{len(missing)} file chunk(s) of the manifest are not on the node; upload them first "
                       "(terminal_upload_check says which)")
    return manifest


class SyncState:
    """The last manifest applied to each destination of one lane (`<sync dir>/state.json`)."""

    def __init__(self, root: Path):
        self.file = root / "state.json"

    def load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.file.read_text())
        except (FileNotFoundError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, dest: str) -> dict:
        return self.load().get(dest) or {}

    def set(self, dest: str, manifest_shas: list[str], files: dict[str, str]) -> None:
        data = self.load()
        data[dest] = {"manifest": manifest_shas, "files": files}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")))
        os.replace(tmp, self.file)

    def drop(self, dest: str) -> None:
        data = self.load()
        if data.pop(dest, None) is not None:
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")))
            os.replace(tmp, self.file)


def plan(manifest: Manifest, previous: dict[str, str]) -> tuple[dict[str, str], list[str], list[str]]:
    """(signatures by path, changed paths, deleted paths) against the previous apply."""
    sigs = {e.path: e.signature() for e in manifest.files}
    changed = [p for p, s in sigs.items() if previous.get(p) != s]
    deleted = sorted(p for p in previous if p not in sigs)
    return sigs, changed, deleted


def empty_dirs_after(deleted: list[str], kept: set[str]) -> list[str]:
    """Parent directories of deleted paths that no kept path lives under, deepest first: `rmdir`
    removes the ones nothing else (a file the command made) still holds."""
    kept_dirs = set()
    for p in kept:
        parts = p.split("/")
        for i in range(1, len(parts)):
            kept_dirs.add("/".join(parts[:i]))
    out = set()
    for p in deleted:
        parts = p.split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            if d not in kept_dirs:
                out.add(d)
    return sorted(out, key=lambda d: (-d.count("/"), d))


def write_tar(store: ChunkStore, manifest: Manifest, out: BinaryIO) -> int:
    """Every manifest entry as an uncompressed tar archive (owned by root, with its mode and mtime).
    Blocking. Returns the bytes of file content written."""
    total = 0
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for e in manifest.files:
            info = tarfile.TarInfo(e.path)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = e.mtime if e.mtime is not None else 0
            if e.link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = e.link
                info.mode = 0o777
                tar.addfile(info)
                continue
            info.mode = e.mode
            info.size = sum(store.path(c).stat().st_size for c in e.chunks)
            tar.addfile(info, _Chunks(store, e.chunks))
            total += info.size
    return total


class _Chunks:
    """A file object over a file's chunks, read in order (what tarfile.addfile reads from)."""

    def __init__(self, store: ChunkStore, chunks: list[str]):
        self._paths = [store.path(c) for c in chunks]
        self._cur: BinaryIO | None = None

    def read(self, n: int = -1) -> bytes:
        out = bytearray()
        while n < 0 or len(out) < n:
            if self._cur is None:
                if not self._paths:
                    break
                self._cur = open(self._paths.pop(0), "rb")  # noqa: SIM115 (closed when it is read out)
            data = self._cur.read(-1 if n < 0 else n - len(out))
            if not data:
                self._cur.close()
                self._cur = None
                continue
            out += data
        return bytes(out)

