"""How a trace event reaches the action log: redacted at write time, queued in memory, flushed on
the database's worker, and kept in a private spill file while the database refuses rows, then
replayed in order. The part of TraceStore that makes writing never fail a tool call. A mixin, so
the store stays one object with one database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from openajar.core.sqlite import _is_busy
from openajar.policy import auditlog
from openajar.policy.redact import redact, redact_text
from openajar.policy.scrub import walk
from openajar.policy.trace_models import TraceEvent
from openajar.policy.util import new_id

log = logging.getLogger("openajar.traces")  # the store's own logger: one log for the whole action log

INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1
STATUSES = ("started", "ok", "denied", "pending", "error")
SPILL_AFTER_S = 2.0      # rows the database refuses this long go to the spill file, not just memory


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _text(value: Any) -> str | None:
    """A string SQLite and the canonical form can both store: each lone surrogate (U+D800 to
    U+DFFF) becomes U+FFFD (AUDIT-LOG.md "Write path")."""
    if value is None:
        return None
    value = value if isinstance(value, str) else str(value)
    try:
        value.encode()
    except UnicodeEncodeError:
        return "".join("\ufffd" if "\ud800" <= ch <= "\udfff" else ch for ch in value)
    return value


def _int(value: Any) -> int | None:
    """An integer SQLite can bind: clamped to the signed 64-bit range."""
    if value is None:
        return None
    return max(INT64_MIN, min(INT64_MAX, int(value)))


def _real(value: Any, default: float) -> float:
    """A finite REAL, with -0.0 as 0.0 (SQLite stores it so); anything else is `default`."""
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return v + 0.0 if math.isfinite(v) else default


class TraceWritesMixin:
    def _redacted(self, ev: TraceEvent, extra: Iterable[str]) -> TraceEvent:
        scrub = [*self._scrub, *extra]
        if self.value_scrub is not None:
            ev.args = walk(ev.args, self.value_scrub) if ev.args is not None else None
            ev.summary = self.value_scrub(ev.summary) if ev.summary else ev.summary
            ev.error = self.value_scrub(ev.error) if ev.error else ev.error
        ev.args = redact(ev.args, extra=scrub) if ev.args is not None else None
        ev.summary = redact_text(ev.summary, extra=scrub, max_len=1000) if ev.summary else ev.summary
        ev.error = redact_text(ev.error, extra=scrub, max_len=1000) if ev.error else ev.error
        if ev.lane and not ev.workspace and "/" in ev.lane:
            ev.workspace = ev.lane.split("/", 1)[0]
        if ev.status not in STATUSES:
            ev.status = "error"
        if ev.at is None:
            ev.at = self.clock()
        return ev

    def write(self, ev: TraceEvent, *, extra_scrub: Iterable[str] = ()) -> TraceEvent:
        """Redact and append. Never raises."""
        try:
            ev = self._redacted(ev, extra_scrub)
            row = self._row(ev)
        except Exception:
            log.exception("trace redaction failed; event dropped to a stub")
            ev = TraceEvent(kind=ev.kind, tool=ev.tool, status="error", summary="[trace could not be redacted]")
            row = self._row(ev)
        self._intake.append(row)
        if _on_loop():
            self._write_nowait()
        else:  # the CLI, a thread: waits for the lock as it always did
            with self._lock:
                self._flush_locked()
                try:
                    self._maybe_checkpoint()
                except Exception:
                    log.exception("audit checkpoint failed")
        try:
            self.bus.publish("trace", ev.public())
        except Exception:
            log.exception("trace publish failed")
        return ev

    def _write_nowait(self) -> None:
        """On the event loop: write at once when nothing holds traces.db's write lock, else hand
        the rows to the worker thread, which waits for the lock (busy_timeout) and, past it, keeps
        or spills them as a failed write always did (the fail-closed clock starts there). The
        loop never waits: not for SQLite's lock, nor for a flush the worker has running."""
        if self._closed:
            return
        if self._flush_queued or self._spilled or not self._lock.acquire(blocking=False):
            self._queue_flush()
            return
        if not self.db.lock.acquire(blocking=False):  # a reader holds a snapshot (the anchors, a verify)
            self._lock.release()
            self._queue_flush()
            return
        try:
            if self._closed:
                return
            if self._genesis is None or self._spill_waits():
                self._queue_flush()  # the slow paths (chain set up, spill replay) run on the worker
                return
            conn = self.db.conn
            conn.execute("PRAGMA busy_timeout=0")
            self._nowait = True
            try:
                self._flush_locked()
                if not self._intake and not self._pending:
                    with contextlib.suppress(Exception):
                        self._maybe_checkpoint()  # a locked one is deferred to the tick
            finally:
                self._nowait = False
                conn.execute(f"PRAGMA busy_timeout={int(self.db.timeout * 1000)}")
            if self._intake or self._pending:
                self._queue_flush()
        finally:
            self.db.lock.release()
            self._lock.release()

    def _queue_flush(self) -> None:
        if self._flush_queued:
            return
        self._flush_queued = True
        try:
            self.db.later(self._flush_on_worker)
        except RuntimeError:  # the worker is shut down (closing): the close flush takes the rows
            self._flush_queued = False

    def _flush_on_worker(self) -> None:
        with self._lock:
            self._flush_queued = False
            if self._closed:
                return
            self._flush_locked()
            try:
                self._maybe_checkpoint()
            except Exception:
                log.exception("audit checkpoint failed")

    @property
    def deferred(self) -> bool:
        """Rows written on the event loop still wait for the worker thread."""
        return self._flush_queued or bool(self._intake)

    def _row(self, ev: TraceEvent) -> dict:
        """The row as stored. Every value is one SQLite binds and the canonical form encodes, so
        no value an agent sends can fail the write (AUDIT-LOG.md "Write path")."""
        now = self.clock()
        return {
            "id": _text(ev.id), "at": _real(ev.at, now) if ev.at is not None else now, "kind": _text(ev.kind) or "event",
            "principal": _text(ev.principal), "principal_kind": _text(ev.principal_kind),
            "grant_id": _text(ev.grant_id), "grant_path": _text(ev.grant_path), "tool": _text(ev.tool),
            "lane": _text(ev.lane), "workspace": _text(ev.workspace), "effect": _text(ev.effect),
            "status": _text(ev.status) or "error", "summary": _text(ev.summary),
            "args": json.dumps(ev.args, default=str) if ev.args is not None else None,
            "error": _text(ev.error), "cost_tokens": _int(ev.cost.tokens) or 0, "cost_usd": _real(ev.cost.usd, 0.0),
            "duration_ms": _int(ev.duration_ms), "run_id": _text(ev.run_id), "parent_id": _text(ev.parent_id),
        }

    def _stub_row(self, row: dict) -> dict:
        """What is stored in place of a row SQLite refused: its id, time and kind, nothing else."""
        at = row.get("at")
        return {**{c: None for c in auditlog.ROW_COLUMNS if c != "seq"},
                "id": _text(row.get("id")) or new_id("tr"), "at": _real(at, self.clock()),
                "kind": _text(row.get("kind")) or "event", "status": "error", "cost_tokens": 0, "cost_usd": 0.0,
                "summary": "[trace could not be stored]"}

    def _spill_waits(self) -> bool:
        """A spill file is there, or cannot be looked for (the worker's flush then finds out why)."""
        try:
            return self.spill_path.exists()
        except OSError:
            return True

    def _flush_locked(self) -> None:
        """Write what waits. Any error that keeps rows out is a failed write (_write_failed), so the
        fail-closed clock starts for it, not only for an error SQLite raised."""
        try:
            self._flush_rows()
        except Exception as e:  # noqa: BLE001  (every error that keeps rows out starts the clock)
            log.warning("trace write failed: %s: %s", type(e).__name__, e)
            self._write_failed(f"{type(e).__name__}: {e}")

    def _flush_rows(self) -> None:
        while self._intake:  # in the order written
            self._pending.append(self._intake.popleft())
        if (self._spilled or self.spill_path.exists()) and not self._spill_restore():
            self._write_failed("the database still refuses the spilled rows")
            return
        if self._genesis is None:
            self._init_chain()
        if not self._pending:
            self.db_failing_since = self.unwritten_since = None
            return
        rows = list(self._pending)
        try:
            with self._tx() as c:
                n = self._insert(c, rows)
        except sqlite3.Error as e:
            if self._nowait and _is_busy(e):
                return  # another connection holds the lock: the worker waits for it (not a failure yet)
            log.warning("trace write deferred: %s", e)
            self._write_failed(str(e))
            return
        self._pending.clear()
        self._since_cp += n
        self.db_failing_since = self.unwritten_since = None

    def _write_failed(self, why: str) -> None:
        """The database refused rows: keep them in memory for a blip, then in the spill file. Rows
        neither takes start the clock the fail-closed rule reads (openajar.policy.audit_health)."""
        now = self.clock()
        self.last_write_error = why
        if self.db_failing_since is None:
            self.db_failing_since = now
        if not self._pending:
            return
        if len(self._pending) > self.MAX_PENDING or self._spilled or now - self.db_failing_since >= SPILL_AFTER_S:
            try:
                self._spill()
            except OSError as e:
                log.error("trace spill failed: %s", e)
                self.last_write_error = f"{why}; the spill file also failed: {e}"
            else:
                self.unwritten_since = None
                return
        if self.unwritten_since is None:
            self.unwritten_since = now

    def _spill(self) -> None:
        """Move what waits in memory to the spill file, unchained: rows get their seq and hash
        only when they reach the database, in the order they were written."""
        rows = list(self._pending)
        fd = os.open(self.spill_path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            size = os.fstat(fd).st_size
            # a write that broke off mid-line leaves no newline: start on a line of our own, so the
            # torn line stays alone and the rows after it are read back
            lead = "\n" if size and os.pread(fd, 1, size - 1) != b"\n" else ""
            f.write(lead + "".join(json.dumps(r) + "\n" for r in rows))
            f.flush()
            os.fsync(f.fileno())
        self._spilled = True
        for _ in rows:  # only once they are on disk: a failed spill keeps them waiting in memory
            self._pending.popleft()

    def _restoring_files(self) -> list[Path]:
        return sorted(self.spill_path.parent.glob(self.spill_path.name + ".*.restoring"))

    def _spill_restore(self) -> bool:
        """Insert spilled rows, oldest first, before anything waiting in memory. A spill file is
        deleted only after its rows are committed, so a crash in between replays it: rows already
        in the log are skipped by id, so the chain never forks or repeats a row. Once committed,
        the file is renamed and read once more, for rows another process appended meanwhile.
        False while the database still refuses writes."""
        for f in self._restoring_files():
            if not self._replay(f):
                return False
            f.unlink(missing_ok=True)
        if self.spill_path.exists():
            if not self._replay(self.spill_path):
                return False
            target = self.spill_path.with_name(f"{self.spill_path.name}.{time.time_ns():020d}-{os.getpid()}.restoring")
            try:
                os.replace(self.spill_path, target)
            except FileNotFoundError:
                target = None  # another process got there first
            if target is not None:
                if not self._replay(target):
                    return False
                target.unlink(missing_ok=True)
        self._spilled = False
        return True

    def _replay(self, f: Path) -> bool:
        """Insert the rows of one spill file. Every complete line goes in; a line that broke off
        mid-write (a full disk, a crash) is set aside on its own in a `.torn` file once no writer
        can still be finishing it (the renamed `.restoring` pass), and named by the health monitor."""
        try:
            data = f.read_bytes()
        except FileNotFoundError:
            return True  # another process restored it
        except OSError:
            log.exception("trace spill file %s unreadable; set aside", f)
            with contextlib.suppress(OSError):
                os.replace(f, f.with_suffix(".unreadable"))
            return True
        final = f != self.spill_path  # renamed: nothing appends to it any more
        lines = data.split(b"\n")
        tail = lines.pop()  # after the last newline: empty when the file ends cleanly
        if tail.strip() and final:
            lines.append(tail)  # complete but for its newline, or torn: parsing tells
        rows, torn = [], []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                r = None
            if isinstance(r, dict):
                rows.append(r)
                continue
            # an older spill appended the next row straight after a torn one: a row starts with
            # {"id": (quotes inside its values are escaped), and only the last piece can be whole
            cut = raw.rfind(b'{"id": ', 1)
            whole = None
            if cut > 0:
                try:
                    whole = json.loads(raw[cut:])
                except ValueError:
                    whole = None
            if isinstance(whole, dict):
                rows.append(whole)
                torn.append(raw[:cut])
            else:
                torn.append(raw)
        if self._genesis is None:
            self._init_chain()
        try:
            with self._tx() as c:
                self._since_cp += self._insert(c, rows)
        except sqlite3.Error as e:
            log.warning("spilled trace rows deferred: %s", e)
            return False
        if torn and final:  # only once the rows are in: a retry must not set the same lines aside twice
            self._set_aside(f, torn)
        return True

    def _set_aside(self, f: Path, torn: list[bytes]) -> None:
        dest = f.with_name(f"{self.spill_path.name}.{time.time_ns():020d}.torn")
        kept = True
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(b"\n".join(torn) + b"\n")
        except OSError:
            kept = False
            log.exception("torn spill lines could not be set aside")
        self.spill_torn = {"lines": len(torn) + int((self.spill_torn or {}).get("lines") or 0),
                           "file": str(dest) if kept else None, "at": self.clock()}
        log.error("trace spill file %s had %d torn line(s): every complete row was restored, the torn part %s",
                  f, len(torn), f"is in {dest}" if kept else "could not be saved")

    def flush(self) -> int:
        """Retry deferred writes, and sign a checkpoint when one is due (the policy tick calls
        this every few seconds, so a quiet node still gets its time checkpoint). Returns how
        many events are still waiting."""
        with self._lock:
            if self._closed:
                return self.pending_count
            self._flush_locked()
            try:
                self._maybe_checkpoint()
            except Exception:
                log.exception("audit checkpoint failed")
            return self.pending_count

    @property
    def pending_count(self) -> int:
        return len(self._pending) + len(self._intake)
