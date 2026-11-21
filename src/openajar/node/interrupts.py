"""Interrupting agent work at its next safe point.

A freeze must stop work that is already running, not only refuse new work. The freeze service
cancels the asyncio task of each call in its scope through `interrupt(task, reason)`, which
marks the task first. The cancellation lands at the task's next await (a safe point: atomic
input steps such as a mouse press and release run shielded, so a click is never cut in half).

The code that owns the call (LaneManager.using(), the policy wrapper's freeze guard) catches
the CancelledError, calls `take(task)`, and when the mark was there raises its own error (the
agent sees "frozen") instead of a bare cancellation. `take` uncancels the task once for the
cancel it made, so a task that was also cancelled for another reason (a client that went away,
a shutdown) still ends as cancelled.

Besides a freeze's reasons (free text), the node interrupts a call for three reasons of its own,
which LaneManagerImpl.using() and the MCP registry turn into their own errors: DEADLINE (the call
ran past its overall deadline, openajar.node.deadlines), CLOSED (browser_close or the memory watchdog
closed a lane a stuck call was on) and CLIENT_GONE (the MCP client that made the call went away).
"""

from __future__ import annotations

import asyncio
import weakref

DEADLINE = "openajar:deadline"
CLOSED = "openajar:closed"
CLIENT_GONE = "openajar:client_gone"
OWN = frozenset({DEADLINE, CLOSED, CLIENT_GONE})

_marks: weakref.WeakKeyDictionary[asyncio.Task, str] = weakref.WeakKeyDictionary()


def interrupt(task: asyncio.Task, reason: str) -> bool:
    """Cancel `task` and remember why. False when it already finished (or is the caller itself,
    which a freeze never interrupts)."""
    if task.done() or task is asyncio.current_task():
        return False
    if task in _marks:
        return True  # already interrupted; one cancel is enough
    _marks[task] = reason
    task.cancel()
    return True


def take(task: asyncio.Task | None = None) -> str | None:
    """The reason when `task` (default: the current task) was interrupted and nothing else
    cancelled it, after uncancelling it; None otherwise (re-raise the CancelledError then)."""
    task = task or asyncio.current_task()
    if task is None:
        return None
    reason = _marks.pop(task, None)
    if reason is None:
        return None
    if task.uncancel() > 0:
        return None  # cancelled for another reason too: let that cancellation win
    return reason


def pending(task: asyncio.Task) -> bool:
    """Whether the task carries an interrupt nobody has taken yet."""
    return task in _marks
