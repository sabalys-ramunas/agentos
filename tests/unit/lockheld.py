"""Another connection holds a SQLite write lock while a coroutine of the daemon runs: the
event loop must keep turning (agentos.core.sqlite). Shared by the store lock tests."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

HOLD = 0.6        # seconds the other connection keeps the write lock
MAX_GAP = 0.25    # the longest the loop may stall between two ticks of a 10 ms ticker


def _hold(path: Path, seconds: float, ready: threading.Event, done: threading.Event) -> None:
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    try:
        conn.execute("BEGIN IMMEDIATE")  # the write lock: other writers wait, WAL readers do not
        ready.set()
        time.sleep(seconds)
        conn.execute("COMMIT")
    finally:
        conn.close()
        done.set()


async def loop_stays_free(make: Any, path: Path | str, *, hold: float = HOLD) -> Any:
    """Run `make()` (a coroutine function) while another connection holds the write lock of
    `path` for `hold` seconds. Asserts the loop never stalled. Returns its result; the caller
    checks that the write landed once the lock was let go."""
    ready, done = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold, args=(Path(path), hold, ready, done), daemon=True)
    holder.start()
    assert ready.wait(5), "the other connection never took the write lock"
    gaps: list[float] = []
    stop = False

    async def tick() -> None:
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(tick())
    try:
        result = await make()
    finally:
        stop = True
        await ticker
        holder.join(5)
    assert gaps, "the loop never ticked"
    assert max(gaps) < MAX_GAP, f"the event loop stalled for {max(gaps):.2f}s while the lock was held"
    return result

