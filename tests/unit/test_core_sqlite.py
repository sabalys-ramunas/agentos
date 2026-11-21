"""openajar.core.sqlite: long-lived connections that still see other processes' commits, and
async access that never blocks the event loop on another process's lock."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

from openajar.core.sqlite import SqliteStore
from openajar.node.store import LaneStore


class Notes(SqliteStore):
    def __init__(self, path, **kw):
        super().__init__(path, **kw)
        self._q("CREATE TABLE IF NOT EXISTS notes (k TEXT PRIMARY KEY, v TEXT)")

    def put(self, k, v):
        self._q("INSERT OR REPLACE INTO notes VALUES (?, ?)", (k, v))

    def get(self, k):
        rows = self._q("SELECT v FROM notes WHERE k = ?", (k,))
        return rows[0][0] if rows else None


def _hold_write_lock(path, seconds, ready: threading.Event):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT OR REPLACE INTO notes VALUES ('other', 'x')")
    ready.set()
    time.sleep(seconds)
    conn.execute("COMMIT")
    conn.close()


def test_wal_and_private_on_create(tmp_path):
    store = Notes(tmp_path / "n.db")
    assert store._q("PRAGMA journal_mode")[0][0] == "wal"
    assert (tmp_path / "n.db").stat().st_mode & 0o777 == 0o600


def test_a_kept_connection_sees_what_another_process_commits(tmp_path):
    store = Notes(tmp_path / "n.db")
    assert store.get("a") is None
    other = sqlite3.connect(tmp_path / "n.db", isolation_level=None)
    other.execute("INSERT INTO notes VALUES ('a', '1')")
    other.close()
    assert store.get("a") == "1"


def test_each_thread_has_its_own_connection(tmp_path):
    store = Notes(tmp_path / "n.db")
    mine = store._conn()
    theirs = []
    t = threading.Thread(target=lambda: theirs.append(store._conn()))
    t.start()
    t.join()
    assert theirs[0] is not mine and store._conn() is mine


def test_run_keeps_the_loop_free_while_another_process_holds_the_lock(tmp_path):
    store = Notes(tmp_path / "n.db", timeout=5.0)
    ready = threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(tmp_path / "n.db", 0.6, ready))
    holder.start()
    ready.wait(5)

    async def main():
        ticks = 0

        async def tick():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        ticker = asyncio.create_task(tick())
        await store.run(store.put, "a", "1")  # waits for the lock, off the loop
        assert store.get("a") == "1"  # a read on the loop: WAL, never waits for the writer
        ticker.cancel()
        return ticks

    assert asyncio.run(main()) >= 10  # the loop kept running the whole time
    holder.join()


def test_soon_writes_at_once_when_free_and_queues_in_order_when_locked(tmp_path):
    store = Notes(tmp_path / "n.db", timeout=5.0)
    store.soon(store.put, "a", "now")
    assert store.get("a") == "now" and not store._pending

    ready = threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(tmp_path / "n.db", 0.5, ready))
    holder.start()
    ready.wait(5)
    started = time.monotonic()
    store.soon(store.put, "a", "first")   # locked: queued, and the caller does not wait
    store.soon(store.put, "a", "second")  # behind it, even if the lock is free by now
    assert time.monotonic() - started < 0.2
    asyncio.run(store.flush())
    holder.join()
    assert store.get("a") == "second"


def test_soon_raises_what_is_not_a_lock(tmp_path):
    store = Notes(tmp_path / "n.db")
    with pytest.raises(sqlite3.OperationalError):
        store.soon(store._q, "SELECT * FROM missing")


def test_a_queued_write_that_fails_is_logged(tmp_path, caplog):
    store = Notes(tmp_path / "n.db")
    store.later(store._q, "SELECT * FROM missing")
    asyncio.run(store.flush())
    assert "a queued write failed" in caplog.text


def test_close_finishes_queued_writes(tmp_path):
    store = Notes(tmp_path / "n.db")
    store.later(store.put, "a", "1")
    store.close()
    assert Notes(tmp_path / "n.db").get("a") == "1"


def test_an_existing_lane_store_opens_as_it_is(tmp_path):
    """A lanes.db made by the old per-call code (same schema, WAL) opens with no migration."""
    path = tmp_path / "lanes.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE lanes (id TEXT PRIMARY KEY, spec TEXT NOT NULL, last_used TEXT, exit_ip TEXT)")
    conn.execute("INSERT INTO lanes (id, spec) VALUES ('a/b', '{\"id\": \"a/b\"}')")
    conn.close()
    store = LaneStore(path)
    assert [r.spec.id for r in store.list()] == ["a/b"]
    store.touch("a/b")
    assert store.require("a/b").last_used is not None


def test_a_read_never_waits_for_a_write_lock(tmp_path):
    store = Notes(tmp_path / "n.db", timeout=5.0)
    store.put("a", "1")
    ready = threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(tmp_path / "n.db", 0.8, ready))
    holder.start()
    ready.wait(5)
    started = time.monotonic()
    assert store.get("a") == "1" and store.get("other") is None  # the uncommitted row is not seen
    assert time.monotonic() - started < 0.2
    holder.join()
    assert store.get("other") == "x"


def test_soon_never_runs_ahead_of_a_run_in_flight(tmp_path):
    store = Notes(tmp_path / "n.db")
    gate = threading.Event()

    def slow_put(k, v):
        gate.wait(5)
        store.put(k, v)

    async def main():
        first = asyncio.ensure_future(store.run(slow_put, "a", "first"))
        await asyncio.sleep(0.05)
        store.soon(store.put, "a", "second")  # must queue behind the run still going
        gate.set()
        await first
        await store.flush()

    asyncio.run(main())
    assert store.get("a") == "second"


async def test_a_cancelled_caller_never_drops_a_queued_write(tmp_path):
    store = Notes(tmp_path / "n.db", timeout=5.0)
    ready = threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(tmp_path / "n.db", 0.4, ready))
    holder.start()
    ready.wait(5)
    first = asyncio.ensure_future(store.run(store.put, "first", "1"))  # waits for the lock on the worker
    queued = asyncio.ensure_future(store.run(store.put, "second", "2"))  # not started yet
    await asyncio.sleep(0.05)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    await first
    await store.flush()
    holder.join(5)
    assert store.get("second") == "2"
