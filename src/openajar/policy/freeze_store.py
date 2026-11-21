"""Where a freeze lives: one small file per frozen scope, outside every database.

    $OPENAJAR_HOME/freeze/
      mesh.frozen                  the controller froze every node (source "mesh")
      node.frozen                  this node
      workspace.<ws>.frozen        a workspace
      lane.<ws>.<name>.frozen      a lane (lane ids have no dots, so the name is unambiguous)
      journal.jsonl                every freeze and unfreeze, appended (best effort)
      link.key                     the key of the one-tap freeze links in pushes (600)
      .reserve                     64 KiB kept free for a freeze on a full disk

Rules, all toward frozen:
- A marker's presence is the freeze. Its JSON says who, why and when; a marker that cannot be read
  (empty, cut short, garbage) is still a freeze ("unreadable"), and a `*.frozen` name that does not
  parse freezes the whole node.
- A directory that cannot be listed (permissions, an I/O error) freezes the whole node.
- Writing: a temporary file renamed over the marker. On a full disk the reserve is given up and the
  write is tried again; if the record still cannot be written, an empty marker is created (it needs
  no data blocks); only when even that fails is the freeze held in memory alone (`durable` false).
- Unfreezing deletes the marker. A delete that fails leaves the scope frozen.
The policy database is never involved, so a policy store that can no longer write cannot stop a
freeze from being set or kept.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openajar.core.models import parse_lane_id

log = logging.getLogger("openajar.freeze")

SCOPES = ("lane", "workspace", "node", "mesh")
SUFFIX = ".frozen"
RESERVE_BYTES = 64 * 1024
JOURNAL_MAX_BYTES = 4 * 1024 * 1024  # rotated once (journal.jsonl.1) past this size
MAX_REASON = 300


def scope_key(scope: str, target: str) -> str:
    """The file stem for a scope: "node", "mesh", "workspace.<ws>" or "lane.<ws>.<name>"."""
    if scope in ("node", "mesh"):
        return scope
    if scope == "workspace":
        return f"workspace.{target}"
    if scope == "lane":
        ws, name = parse_lane_id(target)
        return f"lane.{ws}.{name}"
    raise ValueError(f"scope is one of {', '.join(SCOPES)}")


def parse_key(stem: str) -> tuple[str, str] | None:
    """(scope, target) for a file stem, or None when it does not parse."""
    if stem in ("node", "mesh"):
        return stem, stem
    parts = stem.split(".")
    try:
        if len(parts) == 2 and parts[0] == "workspace":
            parse_lane_id(f"{parts[1]}/x")
            return "workspace", parts[1]
        if len(parts) == 3 and parts[0] == "lane":
            lane = f"{parts[1]}/{parts[2]}"
            parse_lane_id(lane)
            return "lane", lane
    except ValueError:
        return None
    return None


@dataclass
class FreezeRecord:
    scope: str                    # lane | workspace | node | mesh
    target: str                   # the lane id, the workspace, the node name, "mesh"
    reason: str
    by: str                       # who froze it: owner, member:<id>, agent:<id>, controller, push
    at: float
    id: str = field(default_factory=lambda: "fz_" + secrets.token_hex(6))
    via: str | None = None        # cli, panel, mcp, push_link, controller, file
    source: str = "local"         # local, or mesh (the controller's freeze)
    readable: bool = True         # False: the marker could not be read; still frozen
    durable: bool = True          # False: only in memory (the disk refused every write)
    detail_saved: bool = True     # False: the marker exists, but without its record
    applied_at: float | None = None  # when the sweep (stop, revoke, cancel, pause) last ran
    revoked: list[str] = field(default_factory=list)  # grant ids this freeze revoked

    @property
    def key(self) -> str:
        return scope_key(self.scope, self.target)

    def public(self) -> dict[str, Any]:
        out = asdict(self)
        out["at"] = _iso(self.at)
        out["applied_at"] = _iso(self.applied_at)
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any], scope: str, target: str) -> FreezeRecord:
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        known.update(scope=scope, target=target)  # the file name is the truth for what is frozen
        rec = cls(**{"reason": "", "by": "unknown", "at": 0.0, **known})
        rec.reason = str(rec.reason or "")[:MAX_REASON]
        rec.revoked = [str(g) for g in rec.revoked or []][:1000]
        return rec


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class FreezeStore:
    def __init__(self, home: Path):
        self.dir = Path(home) / "freeze"
        self.ensure()

    # ---- setup ----
    def ensure(self) -> None:
        """Make the directory and the full-disk reserve (best effort: a freeze still works without)."""
        try:
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                self.dir.chmod(0o700)
        except OSError as exc:
            log.error("cannot make %s (%s); freezes are held in memory only", self.dir, exc.strerror or exc)
            return
        self._reserve()

    def _reserve(self) -> None:
        path = self.dir / ".reserve"
        try:
            if path.exists() and path.stat().st_size >= RESERVE_BYTES:
                return
            with open(path, "wb") as f:
                f.write(b"\0" * RESERVE_BYTES)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            with contextlib.suppress(OSError):
                path.unlink()

    def _release_reserve(self) -> bool:
        try:
            (self.dir / ".reserve").unlink()
            return True
        except OSError:
            return False

    def path(self, key: str) -> Path:
        return self.dir / f"{key}{SUFFIX}"

    # ---- read ----
    def load(self) -> tuple[dict[str, FreezeRecord], str | None]:
        """Every freeze on disk, and a problem (the whole node is then frozen) or None."""
        out: dict[str, FreezeRecord] = {}
        try:
            names = sorted(p.name for p in self.dir.iterdir())
        except FileNotFoundError:
            return out, None
        except OSError as exc:
            problem = f"the freeze directory cannot be read ({exc.strerror or type(exc).__name__})"
            out["node"] = FreezeRecord("node", "node", problem, "openajar", time.time(), via="file", readable=False)
            return out, problem
        problem = None
        for name in names:
            if not name.endswith(SUFFIX) or name.startswith("."):
                continue
            stem = name[:-len(SUFFIX)]
            parsed = parse_key(stem)
            if parsed is None:
                problem = f"a freeze file this version does not understand ({name[:80]})"
                out["node"] = FreezeRecord("node", "node", problem, "openajar", time.time(), via="file",
                                           readable=False)
                continue
            scope, target = parsed
            out[scope_key(scope, target)] = self._read(self.dir / name, scope, target)
        return out, problem

    def _read(self, path: Path, scope: str, target: str) -> FreezeRecord:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("not an object")
            return FreezeRecord.from_json(data, scope, target)
        except (OSError, ValueError, TypeError) as exc:
            when = 0.0
            with contextlib.suppress(OSError):
                when = path.stat().st_mtime
            why = "an empty marker" if isinstance(exc, ValueError) and _empty(path) else "an unreadable record"
            return FreezeRecord(scope, target, f"frozen ({why}; details were lost)", "unknown", when, via="file",
                                readable=False, detail_saved=False)

    # ---- write ----
    def put(self, rec: FreezeRecord) -> tuple[bool, bool]:
        """Write the marker. Returns (durable, detail_saved): the scope survives a restart when
        durable; detail_saved is false when only an empty marker could be made."""
        body = (json.dumps(asdict(rec), sort_keys=True) + "\n").encode()
        for attempt in range(2):
            try:
                self._atomic_write(self.path(rec.key), body)
                if attempt:
                    log.warning("freeze %s: written after giving up the disk reserve", rec.key)
                return True, True
            except OSError as exc:
                log.error("freeze %s: could not write its record (%s)", rec.key, exc.strerror or exc)
                if attempt or not self._release_reserve():
                    break
        try:
            fd = os.open(self.path(rec.key), os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            self._sync_dir()
            log.error("freeze %s: only an empty marker could be written; it is frozen, the details are lost",
                      rec.key)
            return True, False
        except OSError as exc:
            log.critical("freeze %s: the disk refused every write (%s); frozen in memory only, a restart "
                         "would lose it", rec.key, exc.strerror or exc)
            return False, False

    def update(self, rec: FreezeRecord) -> bool:
        """Rewrite the record of a marker that is still there (after a sweep). A marker someone
        deleted meanwhile (`openajar unfreeze` on the node) is never written back: the file is opened
        without O_CREAT, so a delete at any moment wins. A write cut short leaves an unreadable
        marker, which is still a freeze."""
        body = (json.dumps(asdict(rec), sort_keys=True) + "\n").encode()
        try:
            fd = os.open(self.path(rec.key), os.O_WRONLY | os.O_TRUNC)
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.warning("freeze %s: could not update its record (%s)", rec.key, exc.strerror or exc)
            return False
        try:
            os.write(fd, body)
            os.fsync(fd)
        except OSError as exc:
            log.warning("freeze %s: could not update its record (%s)", rec.key, exc.strerror or exc)
            return False
        finally:
            os.close(fd)
        return True

    def _atomic_write(self, path: Path, body: bytes) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                os.write(fd, body)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        self._sync_dir()

    def _sync_dir(self) -> None:
        with contextlib.suppress(OSError):
            fd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def remove(self, key: str) -> None:
        """Delete a marker (raises OSError when it cannot be deleted: the scope stays frozen)."""
        try:
            self.path(key).unlink()
        except FileNotFoundError:
            return
        self._sync_dir()
        self._reserve()  # a freeze may have spent it

    # ---- journal ----
    def journal(self, entry: dict[str, Any]) -> bool:
        line = json.dumps({"at": _iso(time.time()), **entry}, sort_keys=True, default=str) + "\n"
        path = self.dir / "journal.jsonl"
        try:
            with contextlib.suppress(OSError):
                if path.stat().st_size > JOURNAL_MAX_BYTES:
                    os.replace(path, path.with_name("journal.jsonl.1"))
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode())
            finally:
                os.close(fd)
            return True
        except OSError as exc:
            log.warning("freeze journal: could not append (%s)", exc.strerror or exc)
            return False

    def read_journal(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            lines = (self.dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in lines:
            with contextlib.suppress(ValueError):
                item = json.loads(line)
                if isinstance(item, dict):
                    out.append(item)
        return out

    # ---- push links ----
    def link_key(self) -> bytes:
        path = self.dir / "link.key"
        with contextlib.suppress(OSError):
            key = path.read_bytes()
            if len(key) >= 32:
                return key
        key = secrets.token_bytes(32)
        try:
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
        except OSError:
            log.warning("freeze: could not save the link key; push links last until the next restart")
        return key

    def used_links(self) -> set[str]:
        try:
            return set((self.dir / "links.used").read_text(encoding="utf-8").split())
        except OSError:
            return set()

    def use_link(self, nonce: str) -> None:
        with contextlib.suppress(OSError):
            fd = os.open(self.dir / "links.used", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.write(fd, (nonce + "\n").encode())
            finally:
                os.close(fd)


def _empty(path: Path) -> bool:
    try:
        return path.stat().st_size == 0
    except OSError:
        return False
