"""The browser's own output, kept out of the journal and the agents' errors unscrubbed.

Two ways Camoufox, Firefox and Playwright write text nobody chose to show:

- The Playwright driver (a Node process per lane) inherits the daemon's stderr, so its
  warnings, crash stacks and, with DEBUG=pw:browser, the browser's own stdout and stderr (page
  console lines included) went straight to the journal. `capture_driver_stderr` gives every driver
  a pipe instead; each line is logged through "agentos.browser", so it passes the log scrubbers
  (vault values, proxy credentials, token shapes) after `mask_urls`.
- The browser's own stdout and stderr (Firefox, its content processes, Camoufox) go to the
  driver over sockets, never to a file we hold, and the driver drops them unless its DEBUG names
  pw:browser. So every driver gets DEBUG=pw:browser in its own environment (never the daemon's:
  agentos.node.typist refuses to type a secret while protocol logging is on, and pw:browser is not
  protocol logging), and its `[pid=N][err] ...` lines come through the same pipe. The engine
  names each browser's pid (`watch_browser`), so a line carries its lane's id. The reader is
  bounded: at most MAX_LINE_BYTES of a line, and MAX_LINES_PER_MINUTE lines per browser, the rest
  counted and reported as skipped.
- Playwright errors carry a tail: "Browser logs:" (the browser's recent stdout and stderr, with
  every URL it loaded or complained about) and "Call log:" or a "==== logs ====" block (the
  steps of the call). `browser_error` keeps the message and moves the tail to the log, masked.

`mask_urls` shows each URL the way a log line may (agentos.core.logs.url_for_log): scheme, host
and path, without the query, the fragment or a user:pass@. OAuth codes, reset tokens, session ids
and prefilled emails ride there, and the token shapes the log scrubbers know miss a short
`code=4/0AY...`.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import threading
import time

from agentos.core.logs import url_for_log

log = logging.getLogger("agentos.browser")

MAX_HEAD = 500
MAX_LINE_BYTES = 8 * 1024  # bytes of a stderr line held; the log filter scrubs it whole, then cuts it
MAX_LINES_PER_MINUTE = 120  # per browser (and for the drivers' own lines): a chatty browser cannot flood the log
BROWSER_DEBUG = "pw:browser"  # the driver channel that carries the browser's stdout and stderr
_READ_BUFFER = 64 * 1024

_TAIL_RE = re.compile(r"\n[ \t]*(?:Browser logs:|Call log:|=+ logs =+)", re.IGNORECASE)
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>`]*[^\s\"'<>`.,;:)\]]", re.IGNORECASE)  # not the comma after it


def mask_urls(text: str) -> str:
    """Every URL in `text` without its query, fragment or user:pass@."""
    return _URL_RE.sub(lambda m: url_for_log(m.group(0)), text) if "://" in text else text


def split_error(e: BaseException | str) -> tuple[str, str]:
    """(the message, the browser log or call log tail after it)."""
    text = (e if isinstance(e, str) else str(e)).strip() or type(e).__name__
    m = _TAIL_RE.search(text)
    if m is None:
        return text, ""
    return text[:m.start()].rstrip(), text[m.start():].strip()


def browser_error(e: BaseException | str, where: str) -> str:
    """The message of a Playwright error, for an EngineError an agent or the panel reads. The
    browser's output that came with it goes to the log (scrubbed there), masked, under `where`."""
    head, tail = split_error(e)
    if tail:
        log.warning("%s: browser output with the error: %s", where, mask_urls(tail))
    head = mask_urls(head)
    return head if len(head) <= MAX_HEAD else head[:MAX_HEAD] + "..."


# ---- the driver's stderr ----

_lock = threading.Lock()
_write_fd: int | None = None
_browsers: dict[int, str] = {}  # browser pid -> lane id
_profiles: dict[str, str] = {}  # profile dir -> lane id, for a pid the engine has not named yet
_clock = time.monotonic

# The driver's debug lines: "2026-11-04T10:00:00.000Z pw:browser [pid=812][err] text" on a pipe,
# "  pw:browser [pid=812][err] text +2ms" on a terminal.
_DEBUG_RE = re.compile(r"^\s*(?:\d{4}-\d\d-\d\dT[\d:.]+Z\s+)?pw:browser\s?(?P<text>.*?)(?:\s\+\d+m?s)?$")
_PID_RE = re.compile(r"\[pid=(\d+)\]")
_EMPTY_RE = re.compile(r"^\[pid=\d+\]\[(?:out|err)\]\s*$")


def watch_profile(profile_dir: os.PathLike | str, lane_id: str) -> None:
    """A browser about to start on this profile is `lane_id`'s: its lines before the engine knows
    its pid (the launch) carry the lane's id too."""
    with _lock:
        _profiles[os.fspath(profile_dir)] = lane_id


def watch_browser(pid: int | None, lane_id: str) -> None:
    """Lines of browser `pid` belong to `lane_id` from now on (the engine calls this once it knows
    the pid)."""
    if pid:
        with _lock:
            _browsers[pid] = lane_id


def forget_browser(pid: int | None, profile_dir: os.PathLike | str | None = None) -> None:
    with _lock:
        if pid:
            _browsers.pop(pid, None)
        if profile_dir is not None:
            _profiles.pop(os.fspath(profile_dir), None)
    if pid:
        _budget.flush(pid)


def _lane_launching(line: str) -> str | None:
    """The lane of a `<launching> ... -profile <dir> ...` line."""
    if "-profile " not in line:
        return None
    with _lock:
        return next((lane for path, lane in _profiles.items() if f"-profile {path} " in line + " "), None)


def _lane_of(pid: int | None) -> str | None:
    """The lane of browser `pid`: named by the engine, else found by its -profile argument."""
    if not pid:
        return None
    with _lock:
        lane = _browsers.get(pid)
        if lane is not None or not _profiles:
            return lane
        profiles = dict(_profiles)
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().decode(errors="replace").split("\0")
    except OSError:
        return None
    for flag, value in itertools.pairwise(argv):
        if flag == "-profile" and value in profiles:
            with _lock:
                _browsers.setdefault(pid, profiles[value])
            return profiles[value]
    return None


class _Budget:
    """At most `limit` lines per key in each minute; what is over is counted, and reported once
    when the key's next minute starts (or when its browser is forgotten)."""

    def __init__(self, limit: int, window: float = 60.0):
        self.limit, self.window = limit, window
        self._state: dict[int | str, list] = {}  # key -> [window start, lines logged, lines skipped]
        self._mutex = threading.Lock()

    def allow(self, key: int | str, lane: str | None) -> bool:
        now = _clock()
        with self._mutex:
            state = self._state.get(key)
            if state is None or now - state[0] >= self.window:
                skipped = state[2] if state else 0
                state = self._state[key] = [now, 0, 0]
            else:
                skipped = 0
            ok = state[1] < self.limit
            if ok:
                state[1] += 1
            else:
                state[2] += 1
        if skipped:
            _skipped(key, lane, skipped)
        return ok

    def flush(self, key: int | str) -> None:
        with self._mutex:
            state = self._state.pop(key, None)
        if state and state[2]:
            _skipped(key, None, state[2])


def _skipped(key: int | str, lane: str | None, count: int) -> None:
    who = f"browser pid {key}" if isinstance(key, int) else "the browser drivers"
    log.warning("browser stderr: %d lines of %s skipped (at most %d a minute are logged)", count, who,
                MAX_LINES_PER_MINUTE, extra={"fields": {"lane": lane}} if lane else None)


_budget = _Budget(MAX_LINES_PER_MINUTE)


def _drain(read_fd: int) -> None:
    """Log the pipe line by line. Buffered reads, and at most MAX_LINE_BYTES of a line held: a
    longer line is logged up to its last space before the cap (so the cut is unlikely to keep half
    of a value the scrubbers would catch whole) and the rest of it is skipped. The driver never
    waits on a full pipe for long, however much it writes."""
    with os.fdopen(read_fd, "rb", buffering=_READ_BUFFER) as pipe:
        skipping = False
        while raw := pipe.readline(MAX_LINE_BYTES):
            whole = raw.endswith(b"\n")
            if not skipping:
                _log_line(raw if whole else _cut_at_space(raw), cut=not whole)
            skipping = not whole


def _cut_at_space(raw: bytes) -> bytes:
    at = max(raw.rfind(b" "), raw.rfind(b"\t"))
    return raw[:at] if at > 0 else b""


def _log_line(raw: bytes, *, cut: bool) -> None:
    line = raw.decode("utf-8", "replace").rstrip()
    if not line and not cut:
        return
    m = _DEBUG_RE.match(line)
    level = logging.WARNING  # the driver's own lines: warnings and crash stacks
    if m:
        line, level = m.group("text"), logging.INFO  # the browser's output, through the driver
        if _EMPTY_RE.match(line):
            return
    pid_m = _PID_RE.search(line)
    pid = int(pid_m.group(1)) if pid_m else None
    lane = _lane_of(pid) if pid else _lane_launching(line)
    if not _budget.allow(pid or "driver", lane):
        return
    log.log(level, "browser stderr: %s%s", mask_urls(line), " [line cut]" if cut else "",
            extra={"fields": {"lane": lane}} if lane else None)


def _driver_env(get_env):
    """The driver's environment with pw:browser added to its DEBUG, so it passes the browser's
    stdout and stderr on (see the module docstring)."""
    def env() -> dict[str, str]:
        out = dict(get_env())
        channels = [c for c in re.split(r"[\s,]+", out.get("DEBUG", "")) if c]
        if BROWSER_DEBUG not in channels:
            out["DEBUG"] = ",".join([*channels, BROWSER_DEBUG])
        return out
    env.agentos = True  # type: ignore[attr-defined]
    return env


def capture_driver_stderr() -> bool:
    """From now on, every Playwright driver this process starts writes its stderr into a pipe that
    is logged line by line. Patches the one function Playwright reads the stderr fd from; if a
    Playwright release renames it, says so and leaves stderr as it is. Idempotent."""
    global _write_fd
    with _lock:
        if _write_fd is not None:
            return True
        try:
            from playwright._impl import _transport
        except ImportError:
            return False
        if not callable(getattr(_transport, "_get_stderr_fileno", None)):
            log.warning("this Playwright has no _get_stderr_fileno; the driver's stderr goes to the journal "
                        "unscrubbed")
            return False
        read_fd, write_fd = os.pipe()
        os.set_inheritable(write_fd, True)
        threading.Thread(target=_drain, args=(read_fd,), name="browser-stderr", daemon=True).start()
        _write_fd = write_fd  # kept open for the life of the process: drivers start and stop
        _transport._get_stderr_fileno = lambda: write_fd
        get_env = getattr(_transport, "get_driver_env", None)
        if not callable(get_env):
            log.warning("this Playwright has no get_driver_env; the browser's own output is not logged")
        elif not getattr(get_env, "agentos", False):
            _transport.get_driver_env = _driver_env(get_env)
        return True
