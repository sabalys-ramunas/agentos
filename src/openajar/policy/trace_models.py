"""A trace event (one row of the action log, as callers write it), its cost, and the event bus the
panel API subscribes to for new events and approval changes (served over SSE).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from openajar.policy.util import iso, new_id

log = logging.getLogger("openajar.traces")


class Cost(BaseModel):
    tokens: int = 0
    usd: float = 0.0


class TraceEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tr"))
    at: float | None = None  # set from the store clock at write time
    kind: str = "event"
    principal: str | None = None
    principal_kind: str | None = None
    grant_id: str | None = None
    grant_path: str | None = None
    tool: str | None = None
    lane: str | None = None
    workspace: str | None = None
    effect: str | None = None
    status: str = "ok"
    summary: str | None = None
    args: Any = None
    error: str | None = None
    cost: Cost = Field(default_factory=Cost)
    duration_ms: int | None = None
    run_id: str | None = None
    parent_id: str | None = None

    def public(self) -> dict:
        """The API shape (docs/API.md Activity)."""
        d = self.model_dump(exclude={"grant_path"})
        d["at"] = iso(self.at)
        return d


class EventBus:
    """Callables `fn(event_type, payload)`; sync or async. A failing subscriber is logged
    and never affects the publisher."""

    def __init__(self) -> None:
        self._subs: list[Callable[[str, dict], Any]] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Callable[[str, dict], Any]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)

        return unsubscribe

    def publish(self, event_type: str, payload: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                res = fn(event_type, payload)
                if inspect.isawaitable(res):
                    try:
                        asyncio.get_running_loop().create_task(res)  # type: ignore[arg-type]
                    except RuntimeError:
                        res.close() if hasattr(res, "close") else None
            except Exception:
                log.exception("event subscriber failed")
