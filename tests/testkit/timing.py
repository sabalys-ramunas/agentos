"""Timing in tests that holds on a slow or busy runner (docs/DEVELOPING.md, "Tests and time").

- **Waiting for something to happen:** `wait_for(check)` (sync) or `await until(check)` (async)
  return as soon as `check()` is true and give up after a generous deadline (10 s), so a quick
  machine does not wait and a slow CI runner does not fail. Never a fixed sleep and an assert,
  never a loop of a fixed short count.
- **Bounding how fast something is:** prefer measuring what our code did (calls made, time spent
  inside our code). Where wall-clock is the only measure, bound it with `budget(seconds)`: the
  bound times OPENAJAR_TEST_SLOW (unset is 1; CI sets 3 on macOS runners). A bound that tells
  "returned at once" from "waited for a timeout" keeps the timeout well above the budget.
- **Ports:** `free_port()`, never a fixed number.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import Callable
from typing import Any

DEADLINE_S = 10.0  # the longest a test waits for an event before it fails


def slow_factor() -> float:
    """OPENAJAR_TEST_SLOW: how much slower this runner is than a quiet laptop (at least 1)."""
    try:
        return max(1.0, float(os.environ.get("OPENAJAR_TEST_SLOW", "1") or 1))
    except ValueError:
        return 1.0


def budget(seconds: float) -> float:
    """A wall-clock bound for a speed check, scaled for a slow runner."""
    return seconds * slow_factor()


def wait_for(check: Callable[[], Any], timeout: float = DEADLINE_S, every: float = 0.02) -> Any:
    """Poll `check()` until it returns something true and return that; after `timeout` seconds
    return its last (false) value, so the caller's assert says what was missing."""
    deadline = time.monotonic() + timeout
    while True:
        got = check()
        if got or time.monotonic() >= deadline:
            return got
        time.sleep(every)


async def until(check: Callable[[], Any], timeout: float = DEADLINE_S, every: float = 0.02) -> Any:
    """`wait_for` on the event loop: sleeps with asyncio, so the code under test keeps running."""
    deadline = time.monotonic() + timeout
    while True:
        got = check()
        if got or time.monotonic() >= deadline:
            return got
        await asyncio.sleep(every)


def free_port() -> int:
    """A TCP port on 127.0.0.1 nobody listens on right now (the kernel picks it)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
