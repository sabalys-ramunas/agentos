"""`logs` streams: the node's own log lines, redacted here before any of them leaves.

`stream_open { kind: "logs", params: { since, follow, source, level } }` (additive under
docs/PROTOCOL.md v1). The lines are what agentosd writes (openajar.core.logs): journald when it
runs under systemd, else OPENAJAR_HOME/logs/agentosd.jsonl and its rotated copies, read with
openajar.ops.logs. Each goes out as one JSON object per line, `{ at, source, level, logger,
message, lane }`, in binary stream frames under the usual credit.

Every message passes the scrubbers agentosd registered (the node token, vault values, proxy
credentials, the policy's secret shapes), the vault's own scrub, and the redaction traces use,
over its whole text; then IP addresses are masked (a lane logs its exit IP) and URLs lose their
query and fragment, and only then is it cut to MAX_MESSAGE characters. `at` goes out only when it
reads as a time. A line whose redaction fails is withheld. Lines from before the stream opened are
at most MAX_BACKLOG of the newest, found in at most MAX_SCAN bytes of the newest files (a line
older than `since` is skipped on its time prefix, unparsed); a followed stream sends at most RATE
lines a second (with bursts to BURST) and says how many it left out. Nothing is kept: a controller
that wants logs again opens a new stream.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openajar.core import logs as core_logs
from openajar.core.errors import InvalidRequest
from openajar.core.models import LANE_ID_RE
from openajar.node.channel import wire
from openajar.node.channel.streams import StreamPipe
from openajar.ops import logs as node_logs
from openajar.policy.redact import mask_addresses, redact_text

log = logging.getLogger("openajar.channel.streams")

SOURCES = ("agentosd", "scheduler", "channel", "lane", "service")
LEVELS = ("debug", "info", "warn", "error")
MAX_MESSAGE = 2000      # characters of a message, after redaction
MAX_INPUT = 16 * 1024   # characters of a raw line the scrubbers see (far more than is ever sent)
MAX_RAW = 1 << 20       # a longer line in a log file is read as plain text (and the rest of it skipped)
MAX_BACKLOG = 2000      # lines from before the stream opened
MAX_SCAN = 64 << 20     # bytes of log files read for the backlog, the newest first
DEFAULT_SINCE = 15 * 60
RATE = 100.0            # lines a second while following
BURST = 500
BATCH_BYTES = 32 * 1024
WITHHELD = "[log line withheld: redaction failed]"
POLL = 0.5
LIVE: dict = {}  # marks the end of the backlog in the reader's queue
_URL_TAIL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s?#\"'<>]*)([?#][^\s\"'<>]*)")
# agentosd's own lines start `{"ts":"<time>"`: enough to skip an old one without parsing it
_TS_PREFIX = re.compile(rb'^\s*\{"ts": ?"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)')

Worker = Callable[[StreamPipe], Awaitable[str]]


def source_of(line: dict) -> str:
    logger = str(line.get("logger") or "")
    if logger.startswith(("openajar.channel", "openajar.node.channel")):
        return "channel"
    if logger.startswith(("openajar.runner.schedules", "openajar.runner.scheduler")):
        return "scheduler"
    if logger.startswith("openajar.env.services"):
        return "service"
    if line.get("lane") or logger.startswith(("openajar.node.lanes", "openajar.env.lanes")):
        return "lane"
    return "agentosd"


def level_of(line: dict) -> str:
    level = str(line.get("level") or "info").strip().lower()
    return {"warning": "warn", "critical": "error", "fatal": "error"}.get(level, level if level in LEVELS else "info")


class Scrubber:
    """Redaction for one stream: agentosd's own scrubbers, the vault's, then the trace redaction."""

    def __init__(self, runtime: Any, config: Any):
        vault = runtime.get("vault") if runtime is not None else None
        self.vault = vault.scrub if vault is not None and callable(getattr(vault, "scrub", None)) else None
        self.exact = tuple(v for v in (getattr(config, "mcp_token", None),) if isinstance(v, str) and len(v) >= 6)

    def __call__(self, text: str, limit: int = MAX_MESSAGE) -> str:
        try:
            if len(text) > MAX_INPUT:
                text = _cut_input(text)
            text = core_logs.scrub(text)
            if self.vault is not None:
                text = self.vault(text)
            text = _URL_TAIL.sub(lambda m: m.group(1) + ("?..." if m.group(2).startswith("?") else ""), text)
            return redact_text(mask_addresses(text), extra=self.exact, max_len=limit)
        except Exception:  # noqa: BLE001  (fail closed: a line that cannot be redacted is not sent)
            return WITHHELD


def _cut_input(text: str) -> str:
    """The first MAX_INPUT characters, less a word the cut splits (it may be half an address or a token)."""
    head = text[:MAX_INPUT]
    if head[-1].isspace() or text[MAX_INPUT].isspace():
        return head
    split = re.search(r"\s\S*\Z", head)
    return head[:split.start()] if split else ""


def shape(line: dict, scrub: Scrubber) -> dict:
    message = line.get("msg")
    text = "" if message is None else str(message)
    if line.get("exc"):
        text += "\n" + str(line["exc"])
    lane = line.get("lane")
    return {"at": _at(line.get("ts")), "source": source_of(line), "level": level_of(line),
            "logger": scrub(str(line.get("logger") or ""), 80), "message": scrub(text),
            "lane": lane if isinstance(lane, str) and LANE_ID_RE.fullmatch(lane) else None}


def _at(ts: Any) -> str | None:
    """The line's time, in agentosd's own form, when it is one; anything else is not sent."""
    if not isinstance(ts, str) or not 10 <= len(ts) <= 40:
        return None
    try:
        moment = datetime.fromisoformat(ts)
    except ValueError:
        return None
    moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class Query:
    def __init__(self, params: dict, now: float | None = None):
        now = time.time() if now is None else now
        since, follow = params.get("since"), params.get("follow", False)
        source, level = params.get("source"), params.get("level") or "info"
        if not isinstance(follow, bool):
            raise InvalidRequest("params.follow must be true or false")
        if source is not None and source not in SOURCES:
            raise InvalidRequest(f"params.source must be one of {', '.join(SOURCES)}")
        if level not in LEVELS:
            raise InvalidRequest(f"params.level must be one of {', '.join(LEVELS)}")
        if since is None:
            self.since = now - DEFAULT_SINCE
        elif isinstance(since, str) and len(since) <= 40:
            try:
                self.since = node_logs.parse_since(since, now)
            except ValueError:
                raise InvalidRequest("params.since must be an ISO 8601 time or 10m, 2h, 1d") from None
        else:
            raise InvalidRequest("params.since must be an ISO 8601 time or 10m, 2h, 1d")
        self.follow = follow
        self.source = source
        self.level = level

    def matches(self, line: dict) -> bool:
        if node_logs.LEVEL_ORDER.get(level_of(line), 20) < node_logs.LEVEL_ORDER[self.level]:
            return False
        return self.source is None or source_of(line) == self.source


class Reader:
    """The lines, read in a thread: first the newest MAX_BACKLOG since `since`, then (following)
    what is written from now on. `stop()` ends it, a journalctl it started included."""

    def __init__(self, home: Path, query: Query, *, journal: bool | None = None, unit: str = node_logs.UNIT):
        self.home = home
        self.query = query
        self.journal = core_logs.under_systemd() if journal is None else journal
        self.unit = unit
        self.lines: queue.Queue[dict | None] = queue.Queue(maxsize=4 * BURST)
        self._stop = threading.Event()
        self._procs: list[Any] = []
        self.thread = threading.Thread(target=self._run, name="channel-logs", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        for proc in list(self._procs):
            with contextlib.suppress(Exception):
                proc.terminate()

    def _popen(self, *args: Any, **kwargs: Any) -> Any:
        proc = subprocess.Popen(*args, **kwargs)  # journalctl with fixed arguments
        self._procs.append(proc)
        return proc

    def _put(self, line: dict | None) -> bool:
        while not self._stop.is_set():
            try:
                self.lines.put(line, timeout=POLL)
                return True
            except queue.Full:
                continue
        return False

    def _run(self) -> None:
        try:
            start = time.time()
            follower = None
            if self.query.follow and not self.journal:
                # opened before the backlog is read: a line written meanwhile is followed, not lost
                follower = Tail(node_logs.log_dir(self.home) / core_logs.LOG_FILE)
                follower.prime()
            backlog: collections.deque[dict] = collections.deque(maxlen=MAX_BACKLOG)
            for line in self._backlog(follower):
                if self._stop.is_set():
                    return
                when = node_logs.line_time(line)  # 0 when the line has no time of its own (not ours)
                if (not when or when >= self.query.since) and self.query.matches(line):
                    backlog.append(line)
            for line in backlog:
                if not self._put(line):
                    return
            if not self._put(LIVE):
                return
            if self.query.follow:
                follow = (node_logs.journal_lines(self.unit, start, True, popen=self._popen) if follower is None
                          else follower.lines(self._stop.is_set, self._stop.wait))  # stop() wakes its wait
                for line in follow:
                    if self._stop.is_set():
                        return
                    if self.query.matches(line) and not self._put(line):
                        return
        except Exception:
            log.exception("logs stream: reading the node's logs failed")
        finally:
            self._put(None)

    def _backlog(self, follower: Tail | None = None) -> Iterator[dict]:
        if self.journal:
            yield from node_logs.journal_lines(self.unit, self.query.since, False, popen=self._popen)
            return
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.query.since)).encode()
        # the newest MAX_SCAN bytes, newest file first, then read oldest first
        budget, plan = MAX_SCAN, []
        for path in reversed(node_logs.log_files(self.home)):
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < self.query.since:
                continue  # a rotated file last written before `since` holds nothing newer
            end = follower.offset if follower is not None and follower.inode == st.st_ino else st.st_size
            begin = max(0, end - budget)
            budget -= end - begin
            plan.append((path, begin, end))
            if budget <= 0:
                break
        for path, begin, end in reversed(plan):
            try:
                with path.open("rb") as handle:
                    handle.seek(begin)
                    if begin:
                        _read_line(handle)  # the rest of a line cut by the budget
                    while handle.tell() < end:
                        raw = _read_line(handle)
                        if not raw:
                            break
                        stamp = _TS_PREFIX.match(raw)
                        if stamp is not None and stamp.group(1) < since:
                            continue
                        parsed = _parse(raw.decode("utf-8", "replace"))
                        if parsed is not None:
                            yield parsed
            except OSError:
                continue


class Tail:
    """What is written to `path` from `prime()` on, across rotation (a new file is read from its start)."""

    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None
        self.inode: int | None = None
        self.offset = 0
        self.from_start = False
        self.partial = False  # primed in the middle of a line: its rest is not a line

    def prime(self) -> None:
        try:
            self.handle = self.path.open("rb")
        except FileNotFoundError:
            self.from_start = True  # a file that appears later holds only new lines
            return
        self.inode = os.fstat(self.handle.fileno()).st_ino
        self.offset = self.handle.seek(0, 2)
        if self.offset:
            self.handle.seek(self.offset - 1)
            self.partial = self.handle.read(1) != b"\n"

    def lines(self, stopped: Callable[[], bool], sleep: Callable[[float], None] = time.sleep) -> Iterator[dict]:
        path = self.path
        try:
            while not stopped():
                if self.handle is None:
                    try:
                        self.handle = path.open("rb")
                    except FileNotFoundError:
                        self.from_start = True
                        sleep(POLL)
                        continue
                    self.inode = os.fstat(self.handle.fileno()).st_ino
                    if not self.from_start:
                        self.handle.seek(0, 2)
                handle = self.handle
                pos = handle.tell()
                raw = _read_line(handle)
                if raw.endswith(b"\n") or len(raw) > MAX_RAW:
                    if self.partial:
                        self.partial = False
                        continue
                    parsed = _parse(raw.decode("utf-8", "replace"))
                    if parsed is not None:
                        yield parsed
                    continue
                handle.seek(pos)  # nothing new, or half a line
                sleep(POLL)
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                if st.st_ino != self.inode or st.st_size < pos:  # rotated: the rest of the old file, then the new one
                    while rest := _read_line(handle):
                        parsed = _parse(rest.decode("utf-8", "replace"))
                        if parsed is not None:
                            yield parsed
                    handle.close()
                    self.handle = path.open("rb")
                    self.inode = os.fstat(self.handle.fileno()).st_ino
                    self.partial = False
        finally:
            if self.handle is not None:
                self.handle.close()
                self.handle = None


def tail(path: Path, stopped: Callable[[], bool], sleep: Callable[[float], None] = time.sleep) -> Iterator[dict]:
    """What is written to `path` from now on, across rotation."""
    follower = Tail(path)
    follower.prime()
    return follower.lines(stopped, sleep)


def _read_line(handle: Any) -> bytes:
    """One line, at most MAX_RAW bytes and its newline: the rest of a longer one is read past in bounded
    pieces (a damaged file cannot make one read unbounded). A cut line keeps no newline of its own."""
    raw = handle.readline(MAX_RAW + 1)
    if len(raw) > MAX_RAW and not raw.endswith(b"\n"):
        while (more := handle.readline(MAX_RAW + 1)) and not more.endswith(b"\n"):
            pass
    return raw


def _parse(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        line = json.loads(raw) if len(raw) <= MAX_RAW else None
    except ValueError:
        line = None
    if line is None:
        return {"ts": "", "level": "info", "logger": "", "msg": raw[:MAX_INPUT]}
    return line if isinstance(line, dict) else None


class Rate:
    """A token bucket over lines."""

    def __init__(self, rate: float = RATE, burst: int = BURST, clock: Callable[[], float] = time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens = float(burst)
        self.at = clock()
        self.dropped = 0

    def allow(self) -> bool:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        self.dropped += 1
        return False


def prepare_logs(runtime: Any, config: Any, params: dict, *, journal: bool | None = None) -> Worker:
    query = Query(params)
    scrub = Scrubber(runtime, config)

    async def run(pipe: StreamPipe) -> str:
        reader = Reader(config.home, query, journal=journal)
        reader.start()
        rate = Rate()
        backlog = True  # lines from before the stream opened are capped by count, not by rate
        try:
            while True:
                if pipe.closed:
                    return pipe.close_reason or "closed"
                batch: list[bytes] = []
                size = 0
                try:
                    item = await asyncio.to_thread(reader.lines.get, True, POLL)
                except queue.Empty:
                    if rate.dropped and rate.allow():
                        await pipe.write(_note(rate))
                    continue
                while True:
                    if item is LIVE:
                        backlog = False
                    elif item is None:
                        if batch:
                            await pipe.write(b"".join(batch))
                        return "eof"
                    elif backlog or rate.allow():
                        if rate.dropped and not backlog:
                            batch.append(_note(rate))
                        encoded = json.dumps(shape(item, scrub), ensure_ascii=False).encode() + b"\n"
                        batch.append(encoded)
                        size += len(encoded)
                    if size >= BATCH_BYTES:
                        break
                    try:
                        item = reader.lines.get_nowait()
                    except queue.Empty:
                        break
                if batch:
                    await pipe.write(b"".join(batch))
        finally:
            reader.stop()

    return run


def _note(rate: Rate) -> bytes:
    count, rate.dropped = rate.dropped, 0
    note = {"at": wire.iso(), "source": "agentosd", "level": "warn", "logger": "openajar.channel",
            "message": f"{count} log lines left out: more than {int(RATE)} a second", "lane": None}
    return json.dumps(note).encode() + b"\n"
