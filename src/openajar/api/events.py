"""In-process event bus behind `GET /api/events` (Server-Sent Events).

Any service publishes with `bus.publish(kind, payload)`; the payload is the same object
shape as the matching resource (a Lane, an Approval, a Run, a trace event, a Node).
publish() never blocks and is safe to call from any thread. Subscribers that fall too
far behind get a single `resync` event and should refetch.

The composition root registers one bus as the runtime service "events" before building
other services (`build(runtime)` below, first in openajar.node.daemon.FEATURE_SERVICES);
build_api() creates and registers one if none exists.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from openajar.api.jsonable import jsonable
from openajar.core.models import utcnow

log = logging.getLogger("openajar.api.events")

# the event kinds the panel API sends (docs/API.md, Live updates)
KINDS = ("lane", "control", "approval", "question", "code", "login", "run", "trace", "node", "workspace", "schedule", "brain",
         "task", "meter", "pointer", "resync")
_KIND_RE = re.compile(r"^[a-z][a-z_]{0,31}$")


@dataclass(frozen=True)
class Event:
    id: int
    kind: str
    data: Any
    at: datetime

    def sse(self) -> str:
        body = json.dumps(self.data, separators=(",", ":"), default=str)
        # id 0 marks a synthetic resync: it must not move the client's Last-Event-ID back to 0
        head = f"id: {self.id}\n" if self.id else ""
        return f"{head}event: {self.kind}\ndata: {body}\n\n"


class _Subscriber:
    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int):
        self.loop = loop
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.overflowed = False

    def offer(self, event: Event) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self._put(event)
        else:
            try:
                self.loop.call_soon_threadsafe(self._put, event)
            except RuntimeError:  # loop closed: the subscriber is gone
                pass

    def _put(self, event: Event) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.overflowed = True

    async def get(self) -> Event:
        if self.overflowed and self.queue.empty():
            self.overflowed = False
            return Event(id=0, kind="resync", data={"reason": "behind"}, at=utcnow())
        return await self.queue.get()


class EventBus:
    def __init__(self, *, history: int = 500, queue_size: int = 1000):
        self._lock = threading.Lock()
        self._seq = 0
        self._history: deque[Event] = deque(maxlen=history)
        self._subs: set[_Subscriber] = set()
        self._queue_size = queue_size
        self._listeners: list[Callable[[Event], None]] = []

    @property
    def last_id(self) -> int:
        return self._seq

    def listen(self, fn: Callable[[Event], None]) -> Callable[[], None]:
        """Call `fn(event)` for every event as it is published, on the publishing thread. For a
        service that keeps its own record of what happened (openajar.webhooks). `fn` must not block;
        an error in it is logged and never reaches the publisher. Returns how to stop listening."""
        with self._lock:
            self._listeners.append(fn)

        def stop() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)

        return stop

    def publish(self, kind: str, payload: Any) -> Event:
        if not _KIND_RE.match(kind):
            raise ValueError(f"invalid event kind {kind!r}")
        with self._lock:
            self._seq += 1
            event = Event(id=self._seq, kind=kind, data=jsonable(payload), at=utcnow())
            self._history.append(event)
            subs = list(self._subs)
            listeners = list(self._listeners)
        for sub in subs:
            sub.offer(event)
        for fn in listeners:
            try:
                fn(event)
            except Exception:
                log.exception("an event listener failed on %s", kind)
        return event

    def flash(self, kind: str, payload: Any) -> None:
        """Send an event that is only news this moment (where the agent's pointer is): to the streams open now,
        with no id, never kept for a replay and never given to listeners. A stream that reconnects does not get
        it again, and it does not push older events out of the replay."""
        if not _KIND_RE.match(kind):
            raise ValueError(f"invalid event kind {kind!r}")
        event = Event(id=0, kind=kind, data=jsonable(payload), at=utcnow())
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub.offer(event)

    def since(self, last_id: int) -> list[Event] | None:
        """Events after last_id, or None when the history no longer reaches back that far."""
        with self._lock:
            return self._since_locked(last_id)

    def _since_locked(self, last_id: int) -> list[Event] | None:
        if last_id > self._seq:  # from a bus that restarted: nothing to replay, the client must refetch
            return None
        if last_id == self._seq:
            return []
        if not self._history or self._history[0].id > last_id + 1:
            return None
        return [e for e in self._history if e.id > last_id]

    @asynccontextmanager
    async def subscribe(self, last_id: int | None = None) -> AsyncIterator[_Subscriber]:
        sub = _Subscriber(asyncio.get_running_loop(), self._queue_size)
        with self._lock:
            self._subs.add(sub)
            backlog = [] if last_id is None else self._since_locked(last_id)
        if backlog is None:
            sub._put(Event(id=0, kind="resync", data={"reason": "gap"}, at=utcnow()))
        else:
            for e in backlog:
                sub._put(e)
        try:
            yield sub
        finally:
            with self._lock:
                self._subs.discard(sub)


SERVICE_NAME = "events"


def build(runtime: Any) -> EventBus:
    """Composition-root hook: the node's one event bus, registered as "events"."""
    bus = runtime.get("events") if runtime is not None else None
    if bus is None:
        bus = EventBus()
        if runtime is not None:
            runtime.register(SERVICE_NAME, bus)
    return bus
