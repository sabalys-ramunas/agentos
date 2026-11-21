"""Another connection holds a SQLite write lock while a coroutine of the daemon runs: the
event loop must keep turning (openajar.core.sqlite). Shared by the store lock tests.

The bug this catches: a write on the event loop thread that waits for the lock (a plain
`store.method()` instead of `await store.run(...)`). Such a wait lasts until the lock is let go,
so the loop is still stuck when the holder is ready to let go. The holder therefore checks for
exactly that: after `hold` seconds it waits for one more tick of the loop before it commits. A
loop that only paused (a busy runner, the garbage collector) ticks again soon and the test
passes; a loop blocked on the lock never ticks while the lock is held, the holder gives up after
STUCK_S and the test fails. No bound on the loop's wall-clock gaps, which a slow runner inflates
(macOS CI saw a 1.01 s pause with nothing wrong)."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Self

HOLD = 0.6        # seconds the other connection keeps the write lock
STUCK_S = 3.0     # no tick for this long after the hold, the lock still held: the loop waits for it
# STUCK_S stays below the stores' busy timeouts (5 s and more), so a stuck write is still waiting
# for this lock (not failed on its own) when the holder gives up.


def _hold(path: Path, seconds: float, ready: threading.Event, done: threading.Event,
          alive: Any = None, stuck: list[float] | None = None) -> None:
    """Hold the write lock of `path` for `seconds`. With `alive` (a callable returning the loop's
    tick count), keep holding until the loop ticks once more (at most STUCK_S); a loop that never
    did is written into `stuck` (how long it was waited for)."""
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    try:
        conn.execute("BEGIN IMMEDIATE")  # the write lock: other writers wait, WAL readers do not
        ready.set()
        time.sleep(seconds)
        if alive is not None:
            seen, t0 = alive(), time.monotonic()
            while alive() == seen and time.monotonic() - t0 < STUCK_S:
                time.sleep(0.005)
            if alive() == seen and stuck is not None:
                stuck.append(time.monotonic() - t0)
        conn.execute("COMMIT")
    finally:
        conn.close()
        done.set()


async def loop_stays_free(make: Any, path: Path | str, *, hold: float = HOLD) -> Any:
    """Run `make()` (a coroutine function) while another connection holds the write lock of
    `path` for `hold` seconds. Asserts the loop was never blocked waiting for that lock. Returns
    its result; the caller checks that the write landed once the lock was let go."""
    ready, done = threading.Event(), threading.Event()
    ticks = [0]
    stuck: list[float] = []
    holder = threading.Thread(target=_hold, args=(Path(path), hold, ready, done, lambda: ticks[0], stuck),
                              daemon=True)
    holder.start()
    assert ready.wait(5), "the other connection never took the write lock"

    async def tick() -> None:
        while not done.is_set():  # until the lock is let go, even when make() ended long before
            await asyncio.sleep(0.01)
            ticks[0] += 1

    ticker = asyncio.create_task(tick())
    try:
        result = await make()
    finally:
        await ticker
        holder.join(5)
    assert not stuck, (f"the event loop did not turn for {stuck[0]:.1f}s at the end of the hold, the lock still "
                       "held: a call on the loop waits for the write lock (use await store.run(...))")
    assert ticks[0], "the loop never ticked"
    return result


class held_lock:
    """`with held_lock(path) as lock:` another connection holds the write lock of `path` until the
    block ends (at most `max_s`). `lock.still_held` tells whether it does right now: a call that
    returns while it is still true did not wait for the lock, whatever the runner's speed. The
    flag drops before the commit, so a call that waited for the lock always sees it down.
    `insert` runs inside the held transaction (an uncommitted row a reader must not see)."""

    def __init__(self, path: Path | str, *, max_s: float = 8.0, insert: str | None = None):
        self.path, self.max_s, self.insert = Path(path), max_s, insert
        self._ready, self._let_go, self._letting_go = threading.Event(), threading.Event(), threading.Event()
        self._thread = threading.Thread(target=self._hold, daemon=True)

    @property
    def still_held(self) -> bool:
        return not self._letting_go.is_set()

    def _hold(self) -> None:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if self.insert:
                conn.execute(self.insert)
            self._ready.set()
            self._let_go.wait(self.max_s)
            self._letting_go.set()
            conn.execute("COMMIT")
        finally:
            self._letting_go.set()
            conn.close()

    def __enter__(self) -> Self:
        self._thread.start()
        assert self._ready.wait(5), "the other connection never took the write lock"
        return self

    def let_go(self) -> None:
        self._let_go.set()
        self._thread.join(10)

    def __exit__(self, *exc: object) -> None:
        self.let_go()
