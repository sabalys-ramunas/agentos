"""Usage of site APIs (docs/SITE-APIS.md, Usage): one row per call, and an hourly rollup.

A row is `site_api_usage`: key id, API, endpoint, surface, HTTP status, code, duration, cached, time.
Never params, never data, never the source address. Rows and rollups are kept for 30 days.

The rollup keeps one row per hour for each endpoint and one with `endpoint` '' for the whole API,
because a percentile does not add up across endpoints. It is written once an hour is over, from the
raw rows, so its p50 and p95 are exact; the hour still running is computed on read.

`chart` is what GET /api/site-apis/{api}/usage returns: one point per hour, with no gaps, ready to
plot: calls, cached, fresh, errors by code, p50 and p95 duration.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from openajar.runner.siteapis.http_store import HttpStore
from openajar.runner.siteapis.keys import iso
from openajar.runner.util import Clock

RETENTION_S = 30 * 86400
HOUR = 3600
DEFAULT_HOURS = 48
MAX_HOURS = RETENTION_S // HOUR
ROLL_EVERY_S = 300.0  # at most this often from the write path; the chart rolls up before it reads


def percentile(sorted_values: list[int], q: float) -> int:
    """Nearest rank."""
    if not sorted_values:
        return 0
    return sorted_values[max(0, math.ceil(q * len(sorted_values)) - 1)]


def _point(hour: int, rows: Iterable[Any]) -> dict[str, Any]:
    durations: list[int] = []
    cached = fresh = 0
    errors: Counter[str] = Counter()
    for r in rows:
        durations.append(int(r["duration_ms"]))
        if r["status"] >= 400:
            errors[r["code"] or "error"] += 1
        elif r["cached"]:
            cached += 1
        else:
            fresh += 1
    durations.sort()
    return {"hour": hour, "calls": len(durations), "cached": cached, "fresh": fresh, "errors": dict(errors),
            "p50_ms": percentile(durations, 0.5), "p95_ms": percentile(durations, 0.95)}


class Usage:
    def __init__(self, store: HttpStore, *, clock: Clock | None = None):
        self.store = store
        self.clock = clock or Clock()
        self._rolled_at = 0.0

    def _now(self) -> float:
        return self.clock.now().timestamp()

    def record(self, *, key_id: str, api: str, endpoint: str, surface: str | None, status: int, code: str | None,
               duration_ms: int, cached: bool) -> None:
        now = self._now()
        self.store.soon(self.store.usage_add, {"key_id": key_id, "api": api, "endpoint": endpoint, "surface": surface, "status": status,
                              "code": code, "duration_ms": max(0, duration_ms), "cached": int(cached), "at": now})
        if now - self._rolled_at >= ROLL_EVERY_S:
            self.roll()

    def roll(self) -> None:
        """Roll up the hours that are over, then drop what is past retention. Not while usage rows
        are still queued (another process holds the lock): an hour rolled without them would stay
        short, so the next record or chart rolls it instead."""
        if self.store.queued:
            return
        now = self._now()
        self._rolled_at = now
        floor = int(now - RETENTION_S) // HOUR * HOUR
        first = self.store.usage_first()
        start = max(self.store.rolled_until(), floor, int(first) // HOUR * HOUR if first is not None else floor)
        end = int(now) // HOUR * HOUR
        if first is not None and start < end:
            groups: dict[tuple[str, str, int], list[Any]] = defaultdict(list)
            for r in self.store.usage_rows(start, end):
                hour = int(r["at"]) // HOUR * HOUR
                groups[(r["api"], r["endpoint"], hour)].append(r)
                groups[(r["api"], "", hour)].append(r)
            if groups:
                self.store.soon(self.store.rollup_put, [{"api": api, "endpoint": endpoint, **_stored(_point(hour, rows))}
                                       for (api, endpoint, hour), rows in groups.items()])
        self.store.soon(self.store.usage_purge, now - RETENTION_S)

    def chart(self, api: str, *, hours: int = DEFAULT_HOURS, endpoint: str | None = None) -> dict[str, Any]:
        self.roll()
        now = self._now()
        current = int(now) // HOUR * HOUR
        since = current - (hours - 1) * HOUR
        have = {r["hour"]: _read(r) for r in self.store.rollup_rows(api, endpoint or "", since)}
        rows = [r for r in self.store.usage_rows(current, now + 1)
                if r["api"] == api and (endpoint is None or r["endpoint"] == endpoint)]
        have[current] = _point(current, rows)
        points = [{**have.get(h, _point(h, ())), "hour": iso(h)} for h in range(since, current + HOUR, HOUR)]
        return {"api": api, "endpoint": endpoint, "hours": hours, "points": points,
                "totals": {k: sum(p[k] for p in points) for k in ("calls", "cached", "fresh")}
                | {"errors": sum(sum(p["errors"].values()) for p in points)}}


def _stored(point: dict[str, Any]) -> dict[str, Any]:
    return {**point, "errors": json.dumps(point["errors"], sort_keys=True)}


def _read(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in ("hour", "calls", "cached", "fresh", "p50_ms", "p95_ms")} | {"errors": json.loads(row["errors"])}
