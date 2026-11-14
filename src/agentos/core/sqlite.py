"""The shared base of agentos's SQLite stores: long-lived connections, WAL, one busy timeout,
and async access that never blocks the event loop.

- **One connection per thread, kept open.** Opening a connection per call cost the daemon a file
  open, a schema read and a PRAGMA on every require, touch and set, on the event loop. A
  connection that stays open in autocommit mode still sees every commit another process (the
  CLI) makes, because each statement starts its own read. Per thread, not one shared behind a
  lock: a write that waits for the lock on the worker thread never holds up a read on the loop.
- **WAL.** Readers never wait for a writer, so a CLI write or a migration holding the write lock
  does not stall a read.
- **`await store.run(store.method, ...)`** runs a call on the store's own worker thread (one per
  store, so its writes keep their order). Use it on the event loop for anything that writes, or
  anything on a hot path: a write waits up to `timeout` for the lock, and on the loop that wait
  froze every lane and the channel keepalive.
- **`store.submit(store.method, ...)`** the same, queued at once and awaited later: for a
  durable write that must be queued before the caller's next step yields the loop.
- **`store.soon(store.method, ...)`** a write nothing waits for (last_used): done at once when
  the lock is free, never waiting for it; else queued on the worker thread, behind what is queued
  already, so writes keep their order. An error of a queued write goes to the log (one of a
  write done at once raises, as a plain call would). `await store.flush()` waits for the queue.

A store that already exists keeps working as it is: the base adds no table and changes no file.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

log = logging.getLogger("agentos.sqlite")

T = TypeVar("T")

DEFAULT_TIMEOUT = 10.0  # seconds a write waits for another process's lock before it fails


class SqliteStore:
    """Subclasses run their SQL in `with self._connect() as conn:` (or `self._q(sql, args)`).
    `row_factory` and `pragmas` (per-connection settings such as synchronous=NORMAL) are set on
    every thread's connection, so a call on the worker reads rows the same way as on the loop."""

    row_factory: Any = None
    pragmas: tuple[str, ...] = ()

    def __init__(self, path: Path | str, *, timeout: float = DEFAULT_TIMEOUT, private: bool = True):
        self.path = Path(path)
        self.timeout = timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        self._local = threading.local()
        self._worker: ThreadPoolExecutor | None = None
        self._worker_lock = threading.Lock()
        self._pending: set[Future] = set()
        self._closed = False
        self._conn().execute("PRAGMA journal_mode=WAL")
        if fresh and private:
            self.path.chmod(0o600)

    # ---- connections ----------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """This thread's connection to the store, opened on first use and kept."""
        local = self._local
        conn = getattr(local, "conn", None)
        if conn is not None and local.pid == os.getpid():
            return conn
        if self._closed:
            raise sqlite3.ProgrammingError(f"the store {self.path.name} is closed")
        # check_same_thread off only so close() may close it; it is only ever used by its thread
        conn = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None, check_same_thread=False)
        conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        for pragma in self.pragmas:
            conn.execute(f"PRAGMA {pragma}")
        if self.row_factory is not None:
            conn.row_factory = self.row_factory
        local.conn, local.pid = conn, os.getpid()
        return conn

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        yield self._conn()

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        return self._conn().execute(sql, args).fetchall()

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """An IMMEDIATE transaction on this thread's connection: the write lock is taken up front,
        so a check-then-write is atomic across threads and processes. Not reentrant."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    # ---- async access ---------------------------------------------------------------------

    def _executor(self) -> ThreadPoolExecutor:
        with self._worker_lock:
            if self._worker is None:
                self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"store-{self.path.stem}")
            return self._worker

    async def run(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """`fn(*args, **kwargs)` on the store's worker thread; the event loop waits without blocking.
        Cancelling the caller never stops the call, even one still queued: it runs and ends on its
        own, so a durable write is never dropped half way through a caller's change."""
        return await self.submit(fn, *args, **kwargs)

    def submit(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> asyncio.Future[T]:
        """Queue `fn(*args, **kwargs)` on the worker thread now, without yielding the loop, and
        return a future to await later: a durable write that must keep its place ahead of what
        the caller does next (a hold, an announcement) is queued first and awaited at the end.
        Must be called on the event loop; await the future (or its error is reported unread)."""
        future = self._executor().submit(functools.partial(fn, *args, **kwargs))
        self._pending.add(future)  # so a soon() after it queues behind it, never ahead
        future.add_done_callback(self._pending.discard)
        # shielded: wrap_future alone would pass a caller's cancel on to a call not yet started
        inner = asyncio.wrap_future(future)
        outer = asyncio.shield(inner)

        def report(done: asyncio.Future) -> None:  # its caller is gone: the log is the only one told
            if outer.cancelled() and not done.cancelled() and done.exception() is not None:
                exc = done.exception()
                log.error("%s: a write whose caller was cancelled failed: %s", self.path.name, exc, exc_info=exc)

        inner.add_done_callback(report)
        return outer

    def later(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
        """Queue `fn(*args, **kwargs)` on the worker thread and do not wait; a failure is logged."""
        future = self._executor().submit(fn, *args, **kwargs)
        self._pending.add(future)
        future.add_done_callback(self._done)

    def soon(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
        """`fn(*args, **kwargs)` now if no other process holds the write lock (no wait at all),
        else queued with later(). Behind earlier queued writes, always: their order holds."""
        if not self._pending:
            conn = self._conn()
            conn.execute("PRAGMA busy_timeout=0")
            try:
                fn(*args, **kwargs)
                return
            except sqlite3.OperationalError as e:
                if not _is_busy(e):
                    raise
            finally:
                conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        self.later(fn, *args, **kwargs)

    def _done(self, future: Future) -> None:
        self._pending.discard(future)
        if not future.cancelled() and future.exception() is not None:
            exc = future.exception()
            log.error("%s: a queued write failed: %s", self.path.name, exc, exc_info=exc)

    @property
    def queued(self) -> bool:
        """Some write sent to the worker has not finished yet (a read may not see it)."""
        return bool(self._pending)

    async def flush(self) -> None:
        """Wait until every write queued with later() has run."""
        await self.run(lambda: None)

    def close(self) -> None:
        """Finish queued writes, then close this thread's and the worker's connections. Other
        threads' connections close when their thread ends or the store is collected."""
        worker = self._worker
        if worker is not None:
            with contextlib.suppress(RuntimeError):
                worker.submit(self._close_local).result(timeout=self.timeout + 5)
            worker.shutdown(wait=False)
        self._close_local()
        self._closed = True

    def _close_local(self) -> None:
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()


def _is_busy(e: sqlite3.OperationalError) -> bool:
    code = getattr(e, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return "locked" in str(e) or "busy" in str(e)
