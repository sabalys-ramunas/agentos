"""Runs: what one ui_check or system_check found, kept so a later call can say what changed
(`since`), show one finding in full (finding_get) and crop its place (ui_image).

A run is one JSON file in `<studio state dir>/runs/` (files 600 in a 700 folder), next to the
store's captures it names. Its id is new for every call, so two calls that found the same things
are still two runs. The newest MAX_RUNS are kept; a run whose capture was pruned still answers
`since` and finding_get, and says its images are gone.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentos.core.errors import InvalidRequest, NotFound
from agentos.studio.contracts import Adherence, CheckRun, Finding, Rect

MAX_RUNS = 500
RUN_RE = re.compile(r"^run_[0-9a-z]{6,40}$")
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def new_run_id() -> str:
    return "run_" + "".join(secrets.choice(_ALPHABET) for _ in range(10))


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str = "check"                 # check (ui_check) or system (system_check)
    url: str
    profile: str | None = None
    capture: str                        # capture id in the store
    created_at: str
    run: CheckRun
    adherence: dict[str, Adherence] = Field(default_factory=dict)
    provenance: dict[str, int] = Field(default_factory=dict)
    failed: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    reports: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rects: dict[str, dict[str, Rect]] = Field(default_factory=dict)   # finding id -> screen -> its place

    def finding(self, fid: str) -> Finding | None:
        return next((f for f in self.run.findings if f.id == fid), None)


class Runs:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir) / "runs"
        self._lock = threading.Lock()

    def _path(self, run_id: str) -> Path:
        if not RUN_RE.match(run_id or ""):
            raise InvalidRequest(f"{str(run_id)[:60]!r} is not a run id: one looks like run_k3x9q2m4ab, from an "
                                 "earlier ui_check")
        return self.dir / f"{run_id}.json"

    def save(self, record: RunRecord) -> RunRecord:
        path = self._path(record.id)
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            files = self._files()
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(record.model_dump_json())
            # The newest run is the one with the latest mtime. Linux stamps files from a clock that
            # ticks every few milliseconds, so two quick runs could share one and sort by their random
            # ids instead: each run is stamped at least 1 ns after the newest one before it.
            stamp = max(time.time_ns(), files[0].stat().st_mtime_ns + 1 if files else 0)
            os.utime(tmp, ns=(stamp, stamp))
            os.replace(tmp, path)
            self._prune()
        return record

    def get(self, run_id: str) -> RunRecord:
        path = self._path(run_id)
        try:
            return RunRecord.model_validate_json(path.read_text("utf-8"))
        except FileNotFoundError:
            raise NotFound(f"no run {run_id}: it is older than the newest {MAX_RUNS} runs, or the id is wrong. "
                           "Call ui_check again for a new run") from None

    def _files(self) -> list[Path]:
        try:
            files = [p for p in self.dir.iterdir() if p.suffix == ".json" and RUN_RE.match(p.stem)]
        except FileNotFoundError:
            return []
        return sorted(files, key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)

    def _prune(self) -> None:
        for old in self._files()[MAX_RUNS:]:
            old.unlink(missing_ok=True)

    def records(self, *, limit: int = MAX_RUNS) -> list[RunRecord]:
        """Newest first."""
        out = []
        for p in self._files()[:limit]:
            try:
                out.append(RunRecord.model_validate_json(p.read_text("utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def latest(self, url: str, *, profile: str | None = None, kind: str | None = None,
               exclude: str | None = None) -> RunRecord | None:
        for r in self.records():
            if r.url == url and r.id != exclude and (profile is None or r.profile == profile) \
                    and (kind is None or r.kind == kind):
                return r
        return None

    def with_finding(self, fid: str) -> RunRecord | None:
        return next((r for r in self.records() if r.finding(fid) is not None), None)

    def for_capture(self, capture_id: str) -> RunRecord | None:
        return next((r for r in self.records() if r.capture == capture_id), None)
