"""What other packages call on the events layer (runtime service "event_queue").

Sources (the runner's scheduler, the lane control listener, policy approvals, agent lanes)
call `emit`. A target that finishes later (an agent turn, brief B2) calls
`delivery_finished`. Both are safe to call from any task in the daemon's loop; the store is
the only writer of events.db.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from agentos.events.models import Event

SERVICE_NAME = "event_queue"  # "events" is the panel's SSE bus (agentos.api.events)


@runtime_checkable
class EventsPort(Protocol):
    def emit(self, event: Event | dict[str, Any], *, route_id: str | None = None) -> tuple[Event, bool]:
        """Store the event (idempotent on source and dedupe_key) and fan it out to the routes
        that match it. Returns (event, created); a duplicate returns the stored event.
        `route_id` also offers it to that one route (a Schedule that names its route)."""
        ...

    def delivery_finished(self, delivery_id: str, outcome: Literal["done", "failed"], *,
                          status: str | None = None, detail: str | None = None) -> None:
        """A target that runs on its own (an agent turn) reports how the delivery ended.
        `status` is the target's honest status (done_unverified, waiting_approval, failed)."""
        ...
