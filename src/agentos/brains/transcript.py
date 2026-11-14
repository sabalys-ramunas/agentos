"""Task transcripts: normalized events, one JSON object per line, rotated by size.

Each attempt writes `transcript.<first_seq>.jsonl` segments in its own directory; the
sequence number continues across attempts, so `brain_tail(id, since=N)` works the same
whether a task ran once or moved between brains. Old segments are dropped once there are
more than `max_segments` (the tail is what a reattaching agent needs).

Every line is redacted before it is written: the shim knows the secret values it injected
and replaces them, so a brain that prints its own environment cannot leak a credential
into a transcript, a trace or a tool result.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SEGMENT_RE = re.compile(r"^transcript\.(\d+)\.jsonl$")

# patterns for the redacted-cloud privacy level and for defense in depth in transcripts
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"agtg_[A-Za-z0-9_\-]{20,}"),  # a git gateway token (agentos.git)
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),
]


def redact_patterns(text: str) -> str:
    """Replace things that look like credentials. Used for redacted-cloud prompts."""
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            text = pat.sub(lambda m: m.group(1) + "[redacted]", text)
        else:
            text = pat.sub("[redacted]", text)
    return text


class Redactor:
    """Replaces exact secret values (and simple encodings of them) with [redacted]."""

    def __init__(self, values: Iterable[str] = ()):
        vals = {v for v in values if isinstance(v, str) and len(v) >= 8}
        expanded = set(vals)
        for v in vals:
            expanded.add(json.dumps(v)[1:-1])  # JSON-escaped form
        self._values = sorted(expanded, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for v in self._values:
            if v in text:
                text = text.replace(v, "[redacted]")
        return redact_patterns(text)

    def __bool__(self) -> bool:
        return bool(self._values)


class TranscriptWriter:
    def __init__(self, directory: Path, *, seq_start: int = 1, segment_bytes: int = 8_000_000,
                 max_segments: int = 6, redactor: Redactor | None = None):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.seq = seq_start - 1
        self.segment_bytes = segment_bytes
        self.max_segments = max_segments
        self.redact = redactor or Redactor()
        self.bytes_written = 0
        self._fh = None
        self._cur_size = 0
        self._open_segment(seq_start)

    def _open_segment(self, first_seq: int) -> None:
        if self._fh:
            self._fh.close()
        path = self.dir / f"transcript.{first_seq:012d}.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        self._fh = os.fdopen(fd, "a", encoding="utf-8")
        self._cur_size = path.stat().st_size
        segs = sorted(p for p in self.dir.iterdir() if SEGMENT_RE.match(p.name))
        for old in segs[:-self.max_segments] if len(segs) > self.max_segments else []:
            old.unlink(missing_ok=True)

    def write(self, event: dict[str, Any]) -> int:
        self.seq += 1
        event = {"seq": self.seq, "at": round(time.time(), 3), **event}
        line = self.redact(json.dumps(event, ensure_ascii=False, default=str)) + "\n"
        if self._cur_size and self._cur_size + len(line) > self.segment_bytes:
            self._open_segment(self.seq)
        assert self._fh is not None
        self._fh.write(line)
        self._fh.flush()
        self._cur_size += len(line)
        self.bytes_written += len(line)
        return self.seq

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def segments(directory: Path) -> list[tuple[int, Path]]:
    if not directory.exists():
        return []
    out = []
    for p in directory.iterdir():
        m = SEGMENT_RE.match(p.name)
        if m:
            out.append((int(m.group(1)), p))
    return sorted(out)


def read_events(dirs: Iterable[Path], *, since: int | None = None, limit: int = 200) -> dict[str, Any]:
    """Events with seq > since (or the last `limit` events when since is None), oldest first."""
    segs: list[tuple[int, Path]] = []
    for d in dirs:
        segs.extend(segments(d))
    segs.sort()
    if not segs:
        return {"events": [], "next": since or 0, "first_available": None, "gap": False}
    first_available = segs[0][0]
    if since is None:
        chosen: list[tuple[int, Path]] = []
        # read from the newest segment backwards until we have `limit` events
        events: list[dict[str, Any]] = []
        for start, path in reversed(segs):
            chosen.insert(0, (start, path))
            events = _load(p for _, p in chosen)
            if len(events) >= limit:
                break
        events = events[-limit:]
        gap = False
    else:
        idx = 0
        for i, (start, _) in enumerate(segs):
            if start <= since + 1:
                idx = i
        events = [e for e in _load(p for _, p in segs[idx:]) if e.get("seq", 0) > since][:limit]
        gap = since + 1 < first_available
    nxt = events[-1]["seq"] if events else (since or 0)
    return {"events": events, "next": nxt, "first_available": first_available, "gap": gap}


def last_seq(dirs: Iterable[Path]) -> int:
    segs: list[tuple[int, Path]] = []
    for d in dirs:
        segs.extend(segments(d))
    if not segs:
        return 0
    segs.sort()
    events = _load([segs[-1][1]])
    return events[-1]["seq"] if events else segs[-1][0] - 1


def _load(paths: Iterable[Path]) -> list[dict[str, Any]]:
    out = []
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except FileNotFoundError:
            continue
    return out
