"""A lane's run history in buckets: the run bars on an account's page (web/src/features/fleet/inspector.tsx).

Counts come from the runs the node really has on the lane (`Runner.lane_run_times`). A bucket with no
runs is zero, and nothing is estimated. The node keeps no signal history per bucket, so none is sent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from openajar.runner.util import iso

RANGES: dict[str, tuple[timedelta, timedelta]] = {
    "24h": (timedelta(hours=24), timedelta(hours=1)),
    "7d": (timedelta(days=7), timedelta(hours=6)),
    "30d": (timedelta(days=30), timedelta(days=1)),
}
"""Each range: how far back, and the width of one bar. 7 days is 28 bars, as the page draws them."""

FAILED = "failed"


def lane_metrics(runs: list[tuple[datetime, str]], *, range_name: str, now: datetime) -> dict[str, Any]:
    """`LaneMetrics` (web/src/api/types.ts): runs and failed runs per bar, oldest first, ending now."""
    span, step = RANGES[range_name]
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    start = now - span
    count = int(span / step)
    buckets = [{"at": iso(start + step * index), "runs": 0, "failed": 0} for index in range(count)]
    for created, status in runs:
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if not start <= created <= now:
            continue
        index = min(count - 1, int((created - start) / step))
        buckets[index]["runs"] += 1
        if status == FAILED:
            buckets[index]["failed"] += 1
    return {"range": range_name, "bucket_seconds": int(step.total_seconds()), "buckets": buckets}
