"""`agentos logs`: read agentosd's structured log lines and filter them on their ids.

Two sources, the same lines (agentos.core.logs writes one JSON object per line):
- journald, when agentosd runs under systemd: `journalctl -u agentosd -o json`, whose MESSAGE
  holds the line. Lines that are not ours (systemd's own, a browser's stderr) are shown as plain
  messages with the journal's priority, and never match an id filter.
- the log files otherwise: AGENTOS_HOME/logs/agentosd.jsonl and its rotated copies (.1 to .4),
  oldest first.

Filters: --lane (a glob: "biz-a/*"), --grant, --run, --workspace, --request, --since ("10m",
"2h", "1d", "90s" or an ISO time), and --level (the lowest level to show).
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.core.logs import BACKUPS, LEVELS, LOG_FILE, log_dir

UNIT = "agentosd"
LEVEL_ORDER = {"debug": 10, "info": 20, "warn": 30, "warning": 30, "error": 40, "critical": 40, "fatal": 40}
PRIORITY_LEVEL = {0: "error", 1: "error", 2: "error", 3: "error", 4: "warn", 5: "info", 6: "info", 7: "debug"}
SINCE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
FOLLOW_POLL_S = 0.5


@dataclass
class Query:
    lane: str | None = None
    grant: str | None = None
    run: str | None = None
    workspace: str | None = None
    request: str | None = None
    since: float | None = None  # unix seconds
    level: str = "debug"

    def matches(self, line: dict[str, Any]) -> bool:
        if LEVEL_ORDER.get(str(line.get("level")).strip().lower(), 20) < LEVEL_ORDER[self.level]:
            return False
        if self.since is not None and line_time(line) < self.since:
            return False
        lane = str(line.get("lane") or "")
        if self.lane and not (lane and fnmatch.fnmatchcase(lane, self.lane)):  # a line with no lane never matches
            return False
        for key in ("grant", "run", "workspace", "request"):
            wanted = getattr(self, key)
            if wanted and line.get(key) != wanted:
                return False
        return True


def parse_level(value: str) -> str:
    key = value.lower()
    if key not in LEVELS:
        raise ValueError(f"unknown level {value!r}; use debug, info, warn or error")
    return {"warning": "warn", "critical": "error"}.get(key, key)


def parse_since(value: str, now: float | None = None) -> float:
    """"10m", "2h", "1d", "90s" before now, or an ISO 8601 time (UTC when it has no zone)."""
    now = time.time() if now is None else now
    text = value.strip()
    if re.fullmatch(r"[0-9]+", text[:-1]) and text[-1:] in SINCE_UNITS:
        return now - int(text[:-1]) * SINCE_UNITS[text[-1]]
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"cannot read --since {value!r}; use 10m, 2h, 1d or an ISO time") from None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()


def line_time(line: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(line.get("ts", ""))).timestamp()
    except ValueError:
        return 0.0


# ---- journald ----

def journal_available(unit: str = UNIT, run: Callable[..., Any] = subprocess.run) -> bool:
    """journalctl exists and holds at least one entry for the unit (readable by this user)."""
    if shutil.which("journalctl") is None:
        return False
    try:
        out = run(["journalctl", "-u", unit, "-n", "1", "-o", "json", "-q", "--no-pager"],
                  capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and bool(out.stdout.strip())


def journal_command(unit: str, since: float | None, follow: bool) -> list[str]:
    cmd = ["journalctl", "-u", unit, "-o", "json", "--no-pager", "-q"]
    if since is not None:
        cmd += ["--since", f"@{int(since)}"]
    if follow:
        cmd.append("-f")
    return cmd


def from_journal(entry: dict[str, Any]) -> dict[str, Any]:
    """One journal entry as a log line: our JSON when MESSAGE holds it, else a plain line."""
    message = entry.get("MESSAGE", "")
    if isinstance(message, list):  # journald sends bytes that are not UTF-8 as a list of ints
        message = bytes(int(b) & 0xFF for b in message).decode("utf-8", "replace")
    message = str(message)
    if message.startswith("{"):
        try:
            line = json.loads(message)
        except ValueError:
            line = None
        if isinstance(line, dict) and "msg" in line:
            return line
    try:
        stamp = int(entry.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000
    except (TypeError, ValueError):
        stamp = 0.0
    try:
        level = PRIORITY_LEVEL.get(int(entry.get("PRIORITY", 6)), "info")
    except (TypeError, ValueError):
        level = "info"
    when = datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return {"ts": when, "level": level, "logger": str(entry.get("SYSLOG_IDENTIFIER") or entry.get("_COMM") or UNIT),
            "msg": message}


def journal_lines(unit: str, since: float | None, follow: bool,
                  popen: Callable[..., Any] = subprocess.Popen) -> Iterator[dict[str, Any]]:
    proc = popen(journal_command(unit, since, follow), stdout=subprocess.PIPE, text=True)
    try:
        for raw in proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield from_journal(entry)
    finally:
        proc.terminate()
        proc.wait()


# ---- files ----

def log_files(home: Path) -> list[Path]:
    """The current file and its rotated copies, oldest first."""
    base = log_dir(home) / LOG_FILE
    rotated = [base.with_name(f"{LOG_FILE}.{i}") for i in range(BACKUPS + 5, 0, -1)]
    return [p for p in [*rotated, base] if p.is_file()]


def _parse(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        line = json.loads(raw)
    except ValueError:
        return {"ts": "", "level": "info", "logger": "", "msg": raw}
    return line if isinstance(line, dict) else None


def file_lines(home: Path, follow: bool, sleep: Callable[[float], None] = time.sleep,
               stop: Callable[[], bool] = lambda: False) -> Iterator[dict[str, Any]]:
    for path in log_files(home)[:-1] if (log_dir(home) / LOG_FILE).is_file() else log_files(home):
        with path.open(encoding="utf-8", errors="replace") as f:
            yield from filter(None, map(_parse, f))
    current = log_dir(home) / LOG_FILE
    while not current.is_file():
        if not follow or stop():
            return
        sleep(FOLLOW_POLL_S)
    f = current.open("rb")
    inode = current.stat().st_ino
    try:
        while True:
            pos = f.tell()
            raw = f.readline()
            if raw.endswith(b"\n") or (raw and not follow):
                parsed = _parse(raw.decode("utf-8", "replace"))
                if parsed is not None:
                    yield parsed
                continue
            if not follow or stop():
                return
            f.seek(pos)  # nothing new, or half a line: wait for the rest
            sleep(FOLLOW_POLL_S)
            try:
                st = current.stat()
            except FileNotFoundError:
                continue
            if st.st_ino != inode or st.st_size < pos:  # rotated: read the new file from the start
                if st.st_ino != inode:   # the old file was renamed: first the lines written to it since the last read
                    for raw in f:
                        parsed = _parse(raw.decode("utf-8", "replace"))
                        if parsed is not None:
                            yield parsed
                f.close()
                f = current.open("rb")
                inode = st.st_ino
    finally:
        f.close()


def select(lines: Iterable[dict[str, Any]], query: Query) -> Iterator[dict[str, Any]]:
    return (line for line in lines if query.matches(line))
