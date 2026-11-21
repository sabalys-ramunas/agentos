"""A tool call's progress and notices, for the MCP layer (openajar.mcp.registry).

An MCP client gives up on a call that sends nothing for a while: Claude Code aborts one that sends
"no response or progress for 300s". So the registry runs a heartbeat beside every call
(HEARTBEAT_S): an MCP progress notification with the seconds so far and what the call does now. A
handler names its step with `step("taking the disk snapshot")`; the heartbeat says it on its next
beat. Nothing waits on a client that asked for no progress (the SDK drops it then).

A handler, or code it calls, may also leave a notice for the agent with `notice(text)`: something
that went right only after openajar fixed it (it closed this caller's own stuck browser). The
registry adds the notices to the result (a dict's "notice", a text's last line).

LONG_CALL_S is how long one call may take before work goes on in the background and the call says
so (`still_working`): well under the 300 s a client waits, with room for the result to arrive.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("openajar.progress")

HEARTBEAT_S = 20.0
LONG_CALL_S = 200.0




@dataclass
class CallProgress:
    """The current call's step and notices (one per call, shared by the tasks it starts)."""
    tool: str = ""
    step: str = ""
    notices: list[str] = field(default_factory=list)


_current: contextvars.ContextVar[CallProgress | None] = contextvars.ContextVar("openajar_progress", default=None)


def begin(tool: str) -> tuple[CallProgress, contextvars.Token]:
    """The registry starts a call's progress; `end(token)` when the call returns."""
    p = CallProgress(tool=tool)
    return p, _current.set(p)


def end(token: contextvars.Token) -> None:
    _current.reset(token)


def current() -> CallProgress | None:
    return _current.get()


def step(text: str) -> None:
    """What the current call does now, for its next progress notification. No call: nothing."""
    p = _current.get()
    if p is not None:
        p.step = text[:200]


def notice(text: str) -> None:
    """A line for the agent, added to the current call's result."""
    p = _current.get()
    if p is not None and text and text not in p.notices:
        p.notices.append(text[:600])


def message(p: CallProgress, elapsed_s: float) -> str:
    what = p.step or f"{p.tool} is still working"
    return f"{what} ({elapsed_s:.0f} s so far)"


async def heartbeat(p: CallProgress, send: Callable[[float, str], Awaitable[None]], *,
                    every_s: float | None = None) -> None:
    """Send a progress notification every `every_s` until cancelled. A send that fails is skipped:
    the call goes on, and the next beat tries again."""
    every = HEARTBEAT_S if every_s is None else every_s
    loop = asyncio.get_running_loop()
    started = loop.time()
    n = 0
    while True:
        await asyncio.sleep(every)
        n += 1
        elapsed = loop.time() - started
        try:
            await asyncio.wait_for(send(float(n), message(p, elapsed)), timeout=10)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("could not send progress for %s", p.tool, exc_info=True)


class StillWorking(Exception):
    """Work that goes on in the background after LONG_CALL_S. `result` is what the call returns."""

    def __init__(self, result: dict[str, Any]):
        super().__init__(result.get("next", "still working"))
        self.result = result


class Background:
    """Work keyed by what it is ("close:<env id>"), that a call waits on for at most LONG_CALL_S and
    leaves running past it. A second call for the same key joins the running work, never a second
    copy. The work is never cancelled by the call that waits on it (a client that goes away)."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def running(self, key: str) -> bool:
        t = self._tasks.get(key)
        return t is not None and not t.done()

    def last_error(self, key: str) -> BaseException | None:
        t = self._tasks.get(key)
        if t is None or not t.done() or t.cancelled():
            return None
        return t.exception()

    async def run[T](self, key: str, start: Callable[[], Awaitable[T]], *, budget_s: float | None = None,
                  still: Callable[[], dict[str, Any]]) -> T:
        """The work's result if it ends within `budget_s` (its error if it fails); else StillWorking
        with `still()`, and the work goes on."""
        task = self._tasks.get(key)
        if task is None or task.done():
            task = asyncio.ensure_future(start())
            self._tasks[key] = task
            task.add_done_callback(_quiet)
        done, _ = await asyncio.wait({task}, timeout=max(0.0, LONG_CALL_S if budget_s is None else budget_s))
        if not done:
            raise StillWorking(still())
        return task.result()


def _quiet(task: asyncio.Task) -> None:
    """A background task's error is read by the next call or shown in its state; never 'never retrieved'."""
    if not task.cancelled():
        task.exception()


async def abandon_after[T](aw: Awaitable[T], timeout_s: float) -> T:
    """`aw` within `timeout_s`, else TimeoutError at once. Unlike asyncio.wait_for, it never waits for
    the work to take its cancellation: a browser call stuck on a dead connection may never end, and
    the caller must not end with it. The work is cancelled and left to end by itself."""
    task = asyncio.ensure_future(aw)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout_s))
    except asyncio.CancelledError:
        task.cancel()
        raise
    if not done:
        task.cancel()
        task.add_done_callback(_quiet)
        raise TimeoutError
    return task.result()
