"""Pace per surface and operation (docs/design/connectors.md 4.2, AUD-13).

The operation's `pace` (min_gap_minutes, per_hour, per_day, per_week) is counted over the runs of that
operation on that surface: queued, waiting for approval, running, succeeded or partial (a run
that may have posted), and a cancelled run that had already done a write step (a cancel that
lands after the post still posted). A failed run that wrote is partial, so it counts too; failed
and cancelled runs that wrote nothing do not count.

A site API (saved_by kind local) counts every run that reached the lane, whatever came of it: its
pace protects the account from navigation, and a key whose runs keep failing still loads the site.
A run that never reached it (cancelled while queued, refused at its gate) has no proof, and does not
count. The catalog posts, so what it counts is what may have posted.

It is checked when a run is queued (the tool, the panel, the CLI, a schedule's fire), and again
right before a run starts, before any approval routing: a standing approval never overrides
pace. Queued, a run that is not started yet counts as now; at the start, only runs that
started count, and a run over its pace waits until the gap has passed.

Not yet: the lane's own limits (AUD-13, `LaneSpec.limits`, so the stricter of the two applies)
and `active_hours`, which needs the workspace timezone; both are recorded on the operation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from agentos.core.models import RunStatus
from agentos.runner.records import ACTIVE, Run
from agentos.runner.store import RunnerStore

COUNTED = ACTIVE | {RunStatus.SUCCEEDED_VERIFIED, RunStatus.SUCCEEDED_UNVERIFIED, RunStatus.PARTIAL}
WINDOWS = (("per_hour", timedelta(hours=1), "an hour"), ("per_day", timedelta(days=1), "a day"),
           ("per_week", timedelta(days=7), "a week"))
SCAN = 1000  # recent runs of one operation looked at; far above any pace a connector declares


def counts(r: Run, *, local: bool = False) -> bool:
    """Does this run use up pace: it may post, it did post before it was cancelled, or, `local`, it
    was on the lane at all."""
    if r.status in COUNTED or (local and r.started_at is not None and r.proof is not None):
        return True
    wrote = (r.error or {}).get("writes_done") or (r.proof or {}).get("writes")
    return r.status == RunStatus.CANCELLED and isinstance(wrote, int) and wrote > 0


def pace_problem(store: RunnerStore, flow_id: str, surface: str, pace: dict[str, Any] | None, now: datetime, *,
                 exclude: str | None = None, started_only: bool = False,
                 local: bool = False) -> tuple[str, datetime] | None:
    """(why, when it is allowed again) if one more run of `flow_id` on `surface` breaks the
    pace, else None. `local`: a site API's operation (see `counts`)."""
    pace = pace or {}
    gap = pace.get("min_gap_minutes")
    windows = [(pace.get(k), span, words) for k, span, words in WINDOWS if pace.get(k)]
    if not gap and not windows:
        return None

    def at(r: Run) -> datetime | None:
        if started_only:
            return r.started_at
        return now if r.status in ACTIVE and r.started_at is None else (r.started_at or r.created_at)

    # a site API's retries visit the site too, so in an outage each attempt counts
    times = sorted(t for r in store.list_runs(flow_id=flow_id, limit=SCAN)
                   if r.surface == surface and r.id != exclude and counts(r, local=local)
                   and (t := at(r)) is not None for _ in range(max(1, r.attempts) if local else 1))
    op = flow_id.removeprefix("connector.")
    if gap:
        span = timedelta(minutes=int(gap))
        recent = [t for t in times if t > now - span]
        if recent:
            when = max(recent) + span
            return (f"{op} runs at most once every {gap} minutes on {surface}; the next run may start at "
                    f"{when.isoformat(timespec='minutes')}"), when
    for limit, span, words in windows:
        inside = [t for t in times if t > now - span]
        if len(inside) >= int(limit):
            when = inside[-int(limit)] + span
            return (f"{op} runs at most {limit} times in {words} on {surface}; the next run may start at "
                    f"{when.isoformat(timespec='minutes')}"), when
    return None
