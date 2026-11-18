"""The files of an environment's replay (agentos.environments.replay): its frames, their index and the
calls made on the environment, under AGENTOS_HOME/environments/<id>/replay/, kept after the VM is gone.

    replay/f<10 chars>.jpg    one frame: a JPEG of the browser's viewport
    replay/index.jsonl        one line per frame, oldest first: id, at, cause (agent with tool and
                              summary, person, change, save, close), page_url, page_title, width,
                              height, bytes
    replay/calls.jsonl        one line per call on the environment's lanes: at, tool, summary (scrubbed as
                              the audit log's are), ok, error, exit_code, lane (terminal or browser)

Limits, so no agent fills the disk the account lanes and the audit log share: FRAMES_MAX frames and
ENV_BYTES_MAX bytes for one environment (past them its oldest `change` frames go first; when none is
left, its oldest frames of any other cause but the save and close ones, so the replay always reaches
the end; a new `change` frame never pushes out another kind), and NODE_BYTES_MAX for every
environment's frames together (the oldest frames of closed environments go first; when only open
ones' remain, the new frame is skipped). Kept `replay_days` after the environment closed, then the
reaper removes the folder (prune). Plain files and no service: `agentos environments replay` reads
them with the node stopped too.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("agentos.environments")

FRAMES_MAX = 1500                # frames one environment keeps
ENV_BYTES_MAX = 150 << 20        # their bytes together
NODE_BYTES_MAX = 1 << 30         # every environment's frames on this node together
FRAME_BYTES_MAX = 4 << 20        # one frame (a viewport JPEG is far smaller)
CALLS_MAX = 5000                 # calls one environment's log keeps (the oldest go first)
CAUSES = ("agent", "person", "change", "save", "close")
KEPT_CAUSES = ("save", "close")  # the end of the replay: never dropped to make room
SUMMARY_MAX = 200
TEXT_MAX = 300                   # a page's url or title, as the index keeps it
_FRAME_RE = re.compile(r"^f[a-z0-9]{10}$")
_ENV_RE = re.compile(r"^e[a-z0-9]{3,40}$")
_ID_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"
PUBLIC_KEYS = ("id", "at", "cause", "tool", "summary", "page_url", "page_title", "width", "height", "bytes")
CALL_KEYS = ("at", "tool", "summary", "ok", "error", "exit_code", "lane")


def iso_ms(moment: datetime) -> str:
    """A fixed-width UTC time (milliseconds, Z), so the strings sort by time."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_frame_id(frame_id: Any) -> bool:
    return isinstance(frame_id, str) and bool(_FRAME_RE.match(frame_id))


def _read_lines(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        with contextlib.suppress(ValueError):
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _write_lines(path: Path, records: list[dict]) -> None:
    """Replace a .jsonl file at once (a reader sees the old one or the new one, never half)."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    os.replace(tmp, path)


def _append(path: Path, record: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


class ReplayStore:
    """The replay folders of every environment under `home` (AGENTOS_HOME)."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self._node_bytes: int | None = None  # every replay's bytes together, read once, then kept up to date
        self._call_lines: dict[str, int] = {}  # lines in each calls log, so a trim reads it only when due

    # ---------------------------------------------------------------- reads
    def folder(self, env_id: str) -> Path:
        if not _ENV_RE.match(str(env_id)):
            raise ValueError(f"not an environment id: {env_id!r}")
        return self.home / "environments" / env_id / "replay"

    def frames(self, env_id: str) -> list[dict]:
        """Every frame's record, oldest first (with the private fields: fp, ms)."""
        try:
            folder = self.folder(env_id)
        except ValueError:
            return []
        return [r for r in _read_lines(folder / "index.jsonl") if is_frame_id(r.get("id"))]

    def calls(self, env_id: str) -> list[dict]:
        try:
            folder = self.folder(env_id)
        except ValueError:
            return []
        return [{k: r.get(k) for k in CALL_KEYS} for r in _read_lines(folder / "calls.jsonl")]

    def frame_bytes(self, env_id: str, frame_id: str) -> bytes | None:
        if not is_frame_id(frame_id):
            return None
        try:
            return (self.folder(env_id) / f"{frame_id}.jpg").read_bytes()
        except (OSError, ValueError):
            return None

    def frame_path(self, env_id: str, frame_id: str) -> Path | None:
        if not is_frame_id(frame_id):
            return None
        try:
            path = self.folder(env_id) / f"{frame_id}.jpg"
        except ValueError:
            return None
        return path if path.is_file() else None

    def summary(self, env_id: str) -> dict:
        """{frames, since, bytes}: what environment() tells the agent, and nothing more."""
        frames = self.frames(env_id)
        return {"frames": len(frames), "since": frames[0].get("at") if frames else None,
                "bytes": sum(int(r.get("bytes") or 0) for r in frames)}

    def env_ids(self) -> list[str]:
        root = self.home / "environments"
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if _ENV_RE.match(p.name) and (p / "replay").is_dir())

    def node_bytes(self) -> int:
        if self._node_bytes is None:
            self._node_bytes = sum(int(r.get("bytes") or 0) for e in self.env_ids() for r in self.frames(e))
        return self._node_bytes

    # ---------------------------------------------------------------- writes
    def add_call(self, env_id: str, record: dict) -> None:
        """One call on the environment's lanes, for the timeline. The log keeps the newest CALLS_MAX."""
        folder = self.folder(env_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "calls.jsonl"
        _append(path, {k: record.get(k) for k in CALL_KEYS})
        count = self._call_lines.get(env_id)
        if count is None:
            count = len(_read_lines(path))
        else:
            count += 1
        if count > CALLS_MAX * 5 // 4:  # trim in steps, not on every call
            _write_lines(path, _read_lines(path)[-CALLS_MAX:])
            count = CALLS_MAX
        self._call_lines[env_id] = count

    def add_frame(self, env_id: str, jpeg: bytes, record: dict, *, closed: Callable[[str], bool]) -> dict | None:
        """Keep a frame (record: at, cause, tool, summary, page_url, page_title, width, height, fp, ms).
        Returns its record with its id, or None when the limits leave no room for it. `closed(env_id)`:
        is that environment closed (its frames may go to make room on the node)?"""
        if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > FRAME_BYTES_MAX:
            return None
        cause = str(record.get("cause"))
        if cause not in CAUSES:
            raise ValueError(f"not a frame cause: {cause!r}")
        folder = self.folder(env_id)
        frames = self.frames(env_id)
        gone = _over_env(frames, len(jpeg), cause)
        if gone is None:
            return None
        if not self._node_room(len(jpeg) - sum(int(r.get("bytes") or 0) for r in gone), env_id, closed):
            log.info("replay: no room on this node for a frame of environment %s (only open ones hold it)", env_id)
            return None
        if gone:
            self._drop(env_id, frames, {r["id"] for r in gone})
        folder.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            folder.chmod(0o700)  # a frame shows whatever the page showed
        rec = {"id": "f" + "".join(secrets.choice(_ID_CHARS) for _ in range(10)), **_clean(record), "bytes": len(jpeg)}
        (folder / f"{rec['id']}.jpg").write_bytes(jpeg)
        _append(folder / "index.jsonl", rec)
        self._node_bytes = self.node_bytes() + len(jpeg)
        return rec

    def _drop(self, env_id: str, frames: list[dict], ids: set[str]) -> None:
        folder = self.folder(env_id)
        for frame_id in ids:
            (folder / f"{frame_id}.jpg").unlink(missing_ok=True)
        _write_lines(folder / "index.jsonl", [r for r in frames if r.get("id") not in ids])
        if self._node_bytes is not None:
            self._node_bytes -= sum(int(r.get("bytes") or 0) for r in frames if r.get("id") in ids)

    def _node_room(self, need: int, env_id: str, closed: Callable[[str], bool]) -> bool:
        """Room for `need` more bytes under NODE_BYTES_MAX: the oldest frames of closed environments go
        first. False when only open environments' frames remain."""
        total = self.node_bytes()
        if total + need <= NODE_BYTES_MAX:
            return True
        old: list[tuple[str, str, dict]] = []
        for other in self.env_ids():
            if other != env_id and closed(other):
                old.extend((str(r.get("at") or ""), other, r) for r in self.frames(other))
        old.sort(key=lambda o: o[0])
        drop: dict[str, set[str]] = {}
        for _, other, r in old:
            if total + need <= NODE_BYTES_MAX:
                break
            drop.setdefault(other, set()).add(r["id"])
            total -= int(r.get("bytes") or 0)
        for other, ids in drop.items():
            self._drop(other, self.frames(other), ids)
            log.info("replay: removed %d frames of closed environment %s to stay under the node's cap", len(ids), other)
        self._node_bytes = None  # read again: drops may have raced a reader
        return self.node_bytes() + need <= NODE_BYTES_MAX

    def remove(self, env_id: str) -> None:
        shutil.rmtree(self.folder(env_id), ignore_errors=True)
        self._node_bytes = None
        self._call_lines.pop(env_id, None)

    def prune(self, now: datetime, days: int, ended: Callable[[str], datetime | None | bool]) -> int:
        """Remove the replays of environments closed more than `days` ago. `ended(env_id)`: when it
        closed, None while it is open, or False when the node no longer knows it (then the folder's
        own age counts). Returns how many went."""
        cutoff = now - timedelta(days=days)
        gone = 0
        for env_id in self.env_ids():
            when = ended(env_id)
            if when is None:
                continue
            if when is False:
                try:
                    when = datetime.fromtimestamp(self.folder(env_id).stat().st_mtime, UTC)
                except OSError:
                    continue
            if isinstance(when, datetime) and when <= cutoff:
                self.remove(env_id)
                gone += 1
        return gone


def _over_env(frames: list[dict], need: int, cause: str) -> list[dict] | None:
    """The frames to drop so a new one fits the environment's limits, oldest `change` frames first; None
    when it does not fit (a new `change` frame never pushes out another kind)."""
    count = len(frames) + 1
    total = sum(int(r.get("bytes") or 0) for r in frames) + need
    gone: list[dict] = []
    ids: set[str] = set()

    def over() -> bool:
        return count > FRAMES_MAX or total > ENV_BYTES_MAX

    for kinds in (("change",), () if cause == "change" else ("agent", "person")):
        for r in frames:
            if not over():
                return gone
            if r.get("cause") in kinds and r["id"] not in ids:
                gone.append(r)
                ids.add(r["id"])
                count -= 1
                total -= int(r.get("bytes") or 0)
    return None if over() else gone


def _clean(record: dict) -> dict:
    out: dict[str, Any] = {}
    for key in ("at", "cause", "tool", "summary", "page_url", "page_title", "width", "height", "fp", "ms"):
        value = record.get(key)
        if isinstance(value, str):
            value = " ".join(value.split())[: SUMMARY_MAX if key == "summary" else TEXT_MAX]
        out[key] = value
    return out


def public(record: dict) -> dict:
    """A frame as the owner's timeline lists it (no fingerprint, no timing)."""
    return {k: record.get(k) for k in PUBLIC_KEYS}
