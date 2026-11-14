"""Structured logs for agentosd: ids on every line, JSON out, secrets scrubbed before writing.

The ids that apply to what the code is doing (lane, grant, run, workspace, request, tool) live
in a context variable. They are set once where a request or task starts and every log line
below picks them up; nobody passes them by hand:

    with log_context(request=new_request_id(), lane="personal/reddit"):
        ...                                 # every log line in here carries both
    update(grant="g_123")                   # adds to the innermost log_context, until it ends

asyncio copies the context into each task it creates, so a task started inside a request keeps
its ids, and a task that sets its own (a run) never changes another's.

Output (setup()):
- under systemd: one JSON object per line on stderr, which journald stores (the line starts
  with a `<N>` syslog priority, so `journalctl -p warning` works). When the daemon runs as a
  service user (not root), the same lines also go to the file below: that user cannot read the
  system journal, and `agentos logs` run as it reads the file instead;
- otherwise: JSON lines to AGENTOS_HOME/logs/agentosd.jsonl (rotated by size: 20 MB x 5), and a
  human-readable line on stderr (AGENTOS_LOG_FORMAT=json makes stderr JSON too).

Before anything is written, the message, the traceback and every field go through the
scrubbers the daemon registers (proxy credentials, vault values, grant tokens, the request's
bearer token). A scrubber that fails withholds the line instead of writing it unscrubbed. The
scrubbers see the whole text, and only then is it cut to length, so a cut never splits a secret
the scrubbers would have caught. Text that is not valid UTF-8 (a lone surrogate) is escaped, so
every line can be written. A human-readable line escapes control characters and indents its
continuation lines, so a message can neither forge another line nor repaint the terminal.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any, TextIO

FIELDS = ("lane", "workspace", "grant", "run", "parent", "request", "tool")  # parent: the run's Run.parent
LOG_FILE = "agentosd.jsonl"  # not *.log: deploy/agentos.logrotate rotates those by copytruncate
MAX_BYTES = 20 * 1024 * 1024
BACKUPS = 4
MAX_MESSAGE = 8000  # characters of message, and of traceback, per line
MAX_LINE_BYTES = 32 * 1024  # one encoded line, well under journald's LineMax (48 KiB by default)
MAX_FIELD = 200
WITHHELD = "[log line withheld: redaction failed]"
LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "warning": logging.WARNING,
          "error": logging.ERROR, "critical": logging.CRITICAL}
SYSLOG = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3, logging.CRITICAL: 2}

_EMPTY: Mapping[str, str] = MappingProxyType({})
_fields: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar("agentos_log_fields", default=_EMPTY)


# ---- context ----

def _clean(fields: Mapping[str, Any]) -> dict[str, str]:
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown log fields: {', '.join(sorted(unknown))}")
    out = {k: str(v)[:MAX_FIELD] for k, v in fields.items() if v is not None and v != ""}
    lane = out.get("lane")
    if lane and "workspace" not in out and "/" in lane:
        out["workspace"] = lane.split("/", 1)[0]
    return out


@contextmanager
def log_context(*, drop: tuple[str, ...] = (), **fields: Any) -> Iterator[None]:
    """Add ids for the duration of the block (None and "" are left out, so an outer value stays).
    `drop` names fields an outer context must not lend the block (a run's `parent`, which must be
    the run's own or none). The block's end puts back whatever was there before, including
    anything update() added inside it."""
    _clean(dict.fromkeys(drop, ""))   # unknown names fail loudly
    outer = {k: v for k, v in _fields.get().items() if k not in drop}
    token = _fields.set(MappingProxyType({**outer, **_clean(fields)}))
    try:
        yield
    finally:
        _fields.reset(token)


def update(**fields: Any) -> None:
    """Add ids to the current context, until the enclosing log_context ends (or, outside one,
    for the rest of the current task)."""
    _fields.set(MappingProxyType({**_fields.get(), **_clean(fields)}))


def current() -> dict[str, str]:
    return dict(_fields.get())


def new_request_id() -> str:
    return "rq_" + secrets.token_hex(6)


# ---- scrubbing ----

Scrubber = Callable[[str], str]
_scrubbers: list[Scrubber] = []
_guard = threading.local()


def add_scrubber(fn: Scrubber) -> None:
    """Register a function that masks secrets in a string. It must not log (a line it logs
    while scrubbing is dropped)."""
    if fn not in _scrubbers:
        _scrubbers.append(fn)


def clear_scrubbers() -> None:
    _scrubbers.clear()


def scrub(text: str) -> str:
    for fn in _scrubbers:
        text = fn(text)
    return text


class ContextFilter(logging.Filter):
    """Puts the context's ids on the record, then scrubs the message, the traceback and the ids.
    Installed on the handlers, so every line agentosd writes passes through it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(_guard, "busy", False):
            return False  # logged from inside a scrubber: there is no safe way to write it
        if getattr(record, "agentos_scrubbed", False):
            return True  # a second handler: already done
        _guard.busy = True
        try:
            fields = {**current(), **{k: str(v) for k, v in (getattr(record, "fields", None) or {}).items()
                                      if k in FIELDS}}
            try:
                # Scrub the whole text, then cut: a cut first could split a secret and keep half of it.
                message = _cut(_encodable(scrub(record.getMessage())))
                exc = (_cut(_encodable(scrub(_traceback(record))), tail=True)
                       if record.exc_info or record.exc_text else None)
                fields = {k: _encodable(scrub(v))[:MAX_FIELD] for k, v in fields.items()}
            except Exception:  # noqa: BLE001  (fail closed)
                message, exc, fields = WITHHELD, None, {}
        finally:
            _guard.busy = False
        record.msg, record.args = message, None
        record.exc_info, record.exc_text, record.stack_info = None, exc, None
        record.agentos_fields = fields
        record.agentos_scrubbed = True
        return True


def _cut(text: str, *, limit: int = MAX_MESSAGE, tail: bool = False) -> str:
    """At most `limit` characters. `tail` keeps the head and the end (a traceback's last lines hold the
    exception), else the head only. Only ever applied to text that is already scrubbed."""
    if len(text) <= limit:
        return text
    if not tail:
        return text[:limit] + f"...[{len(text) - limit} more]"
    head = limit // 4
    end = limit - head
    return text[:head] + f"\n...[{len(text) - limit} characters left out]...\n" + text[len(text) - end:]


def _encodable(text: str) -> str:
    """`text` as something UTF-8 can write: a lone surrogate (a title cut inside an emoji) is escaped."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")
    return text


def _traceback(record: logging.LogRecord) -> str:
    if record.exc_text:
        return record.exc_text
    return logging.Formatter().formatException(record.exc_info) if record.exc_info else ""


def url_for_log(url: Any) -> str:
    """A page URL as a log line may show it: scheme, host and path. The query and fragment (where
    sessions, signatures and reset tokens ride) and any user:pass@ are left out."""
    from urllib.parse import urlsplit

    text = str(url or "")
    try:
        parts = urlsplit(text)
    except ValueError:
        return "[url not shown]"
    if not parts.scheme or not parts.netloc:
        return text.split("?", 1)[0].split("#", 1)[0]
    host = parts.netloc.rsplit("@", 1)[-1]
    return f"{parts.scheme}://{host}{parts.path}" + ("?..." if parts.query else "")


# ---- formats ----

class JsonFormatter(logging.Formatter):
    """`{"ts", "level", "logger", "msg", <ids>..., "exc"?}` on one line."""

    def __init__(self, *, syslog_prefix: bool = False):
        super().__init__()
        self.syslog_prefix = syslog_prefix

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": _iso(record.created), "level": _level(record.levelno), "logger": record.name,
            "msg": record.getMessage(), **getattr(record, "agentos_fields", {})}
        if record.exc_text:
            line["exc"] = record.exc_text
        text = _fit(line)
        return f"<{SYSLOG.get(record.levelno, 6)}>{text}" if self.syslog_prefix else text


def _dumps(line: Mapping[str, Any] | str) -> str:
    return json.dumps(line, ensure_ascii=False, separators=(",", ":"))


def _fit(line: dict[str, Any]) -> str:
    """The line as JSON in at most MAX_LINE_BYTES of UTF-8. Escapes can make the JSON several times
    longer than the text (a control character becomes six), so the message and the traceback are cut
    further, the longer one first, until it fits. They are scrubbed already."""
    text = _dumps(line)
    for _ in range(40):
        size = len(text.encode("utf-8"))
        if size <= MAX_LINE_BYTES:
            return text
        key = max(("msg", "exc"), key=lambda k: len(_dumps(line.get(k) or "")))
        value = str(line.get(key) or "")
        if not value:
            break
        line[key] = _cut(value, limit=max(0, int(len(value) * MAX_LINE_BYTES / size * 0.9) - 64),
                         tail=key == "exc")
        text = _dumps(line)
    return _dumps({k: line[k] for k in ("ts", "level", "logger") if k in line} | {"msg": "[log line too long]"})


class HumanFormatter(logging.Formatter):
    """`08:51:35 INFO  agentos.node.lanes [lane=personal/reddit run=r_1] message`."""

    def format(self, record: logging.LogRecord) -> str:
        return human_line({"ts": _iso(record.created), "level": _level(record.levelno), "logger": record.name,
                           "msg": record.getMessage(), **getattr(record, "agentos_fields", {}),
                           **({"exc": record.exc_text} if record.exc_text else {})})


# C0 and C1 controls (but tab and newline), DEL, lone surrogates, bidi overrides and line separators: what
# can repaint a terminal, move the cursor or make one line look like two.
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff\u2028\u2029\u202a-\u202e\u2066-\u2069]")
CONTINUATION = "    | "


def _safe(value: Any) -> str:
    return _UNSAFE.sub(lambda m: f"\\x{ord(m.group()):02x}" if ord(m.group()) < 0x100
                       else f"\\u{ord(m.group()):04x}", str(value))


def _one_line(value: Any) -> str:
    return _safe(value).replace("\n", "\\n")


def human_line(line: Mapping[str, Any]) -> str:
    """One line for a person. Control characters are escaped; a message's or a traceback's further
    lines are indented under CONTINUATION, so none of them can pass for a log line of its own."""
    ts = _one_line(line.get("ts", ""))
    clock = ts[11:19] if len(ts) >= 19 else ts
    ids = " ".join(f"{k}={_one_line(line[k])}" for k in FIELDS
                   if line.get(k) and not (k == "workspace" and line.get("lane")))
    text = f"{ts[:10]} {clock} {_one_line(line.get('level', '')).upper():<5} {_one_line(line.get('logger', ''))}"
    text += f" [{ids}]" if ids else ""
    msg = line.get("msg")
    rest = [_safe("" if msg is None else msg)]
    if line.get("exc"):
        rest.append(_safe(line["exc"]))
    body = "\n".join(rest).replace("\t", "    ").split("\n")
    text += f" {body[0]}"
    return "\n".join([text, *(CONTINUATION + part for part in body[1:])])


def _iso(created: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created)) + f".{int(created % 1 * 1000):03d}Z"


def _level(levelno: int) -> str:
    return {logging.WARNING: "warn", logging.CRITICAL: "error"}.get(levelno, logging.getLevelName(levelno).lower())


# ---- setup ----

def under_systemd(stream: TextIO | None = None) -> bool:
    """True when stderr is connected to the journal (systemd sets JOURNAL_STREAM to its device
    and inode)."""
    value = os.environ.get("JOURNAL_STREAM", "")
    if ":" not in value:
        return False
    try:
        st = os.fstat((stream or sys.stderr).fileno())
    except (OSError, ValueError, AttributeError):
        return False
    return value == f"{st.st_dev}:{st.st_ino}"


def log_dir(home: Path) -> Path:
    return home / "logs"


def _file_handler(home: Path) -> logging.Handler:
    folder = log_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    file = logging.handlers.RotatingFileHandler(folder / LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                                encoding="utf-8", errors="backslashreplace")
    file.setFormatter(JsonFormatter())
    return file


def setup(home: Path | None, *, verbose: bool = False, stream: TextIO | None = None,
          journal: bool | None = None, file_too: bool | None = None) -> list[logging.Handler]:
    """Send every log line of this process through the context filter to its destinations
    (see the module docstring). Replaces the root logger's handlers; returns the new ones."""
    stream = stream or sys.stderr
    journal = under_systemd(stream) if journal is None else journal
    handlers: list[logging.Handler] = []
    if journal:
        out = logging.StreamHandler(stream)
        out.setFormatter(JsonFormatter(syslog_prefix=True))
        handlers.append(out)
        # a service user cannot read the system journal: keep its own copy where it can
        if file_too is None:
            file_too = hasattr(os, "geteuid") and os.geteuid() != 0
        if file_too and home is not None:
            try:
                handlers.append(_file_handler(home))
            except OSError:
                pass  # the journal still has every line
    else:
        if home is not None:
            handlers.append(_file_handler(home))
        out = logging.StreamHandler(stream)
        as_json = os.environ.get("AGENTOS_LOG_FORMAT", "").lower() == "json"
        out.setFormatter(JsonFormatter() if as_json else HumanFormatter())
        handlers.append(out)
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    for handler in handlers:
        handler.addFilter(ContextFilter())
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for noisy in ("httpx", "httpcore"):  # one INFO line per request, with its URL
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.captureWarnings(True)  # warnings go through the same filter, not straight to stderr
    return handlers
