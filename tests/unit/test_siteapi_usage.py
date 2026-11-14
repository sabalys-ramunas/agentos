"""Usage rows hold names and numbers only, and the rollup feeds a chart."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from siteapi_http_support import API, READ_PATH, SURFACE, TOKEN, Node

from agentos.runner.fakes import FakeClock
from agentos.runner.siteapis.http_migrations import DB
from agentos.runner.siteapis.http_store import HttpStore
from agentos.runner.siteapis.usage import RETENTION_S, Usage, percentile

START = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)


@pytest.fixture
async def node(tmp_path):
    async with Node(tmp_path) as n:
        yield n


@pytest.fixture
def usage(tmp_path):
    return Usage(HttpStore(tmp_path), clock=FakeClock(START))


def row(u, *, status=200, code=None, ms=10, cached=False, endpoint="open_orders", api=API):
    u.record(key_id="key_1", api=api, endpoint=endpoint, surface=SURFACE, status=status, code=code, duration_ms=ms,
             cached=cached)


async def test_a_usage_row_holds_names_and_numbers_only(node):
    row_key, key = node.key_row(surfaces=[SURFACE])
    async with node.client(key) as c:
        await c.get(READ_PATH, params={"status": "late"})
        await c.get(READ_PATH, params={"status": "late"})
        await c.get(READ_PATH, params={"status": "nope"})
    db = sqlite3.connect(node.config.home / DB)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM site_api_usage ORDER BY id")]
    assert set(rows[0]) == {"id", "key_id", "api", "endpoint", "surface", "status", "code", "duration_ms", "cached", "at"}
    assert [(r["status"], r["code"], r["cached"]) for r in rows] == [(200, None, 0), (200, None, 1), (400, "invalid_params", 0)]
    assert all(r["key_id"] == row_key.id and r["api"] == API and r["surface"] == SURFACE for r in rows)
    text = json.dumps(rows)
    assert "late" not in text and "127.0.0.1" not in text and "A-1042" not in text


async def test_a_call_without_a_key_or_on_nothing_real_writes_no_row(node):
    async with node.client() as c:
        await c.get(READ_PATH)
    async with node.client(node.key()) as c:
        await c.get("/v1/sites/made-up/name")
    assert node.site_http.store.usage_first() is None


async def test_the_usage_route_is_shaped_for_a_chart_and_is_the_owners(node):
    key = node.key(surfaces=[SURFACE], rate_per_min=3)
    async with node.client(key) as c:
        for _ in range(4):
            await c.get(READ_PATH)
    async with node.client(TOKEN) as c:
        r = await c.get(f"/api/site-apis/{API}/usage", params={"hours": 3})
        assert r.status_code == 200 and len(r.json()["points"]) == 3
        assert (await c.get("/api/site-apis/nope/usage")).status_code == 404
    last = r.json()["points"][-1]
    assert set(last) == {"hour", "calls", "cached", "fresh", "errors", "p50_ms", "p95_ms"}
    assert (last["calls"], last["fresh"], last["cached"], last["errors"]) == (4, 1, 2, {"rate_limited_key": 1})
    assert r.json()["totals"]["calls"] == 4 and last["hour"].endswith(":00:00Z")
    async with node.client(node.grant()) as c:
        assert (await c.get(f"/api/site-apis/{API}/usage")).status_code == 403
    async with node.client(key) as c:
        assert (await c.get(f"/api/site-apis/{API}/usage")).status_code == 401


def test_percentiles_are_nearest_rank():
    values = list(range(1, 101))
    assert percentile(values, 0.5) == 50 and percentile(values, 0.95) == 95 and percentile([7], 0.95) == 7
    assert percentile([], 0.5) == 0


def test_an_hour_that_is_over_is_rolled_up_with_exact_percentiles(usage):
    for ms in range(1, 101):
        row(usage, ms=ms, cached=ms % 2 == 0)
    row(usage, status=429, code="pace_limited", ms=1000)
    row(usage, status=503, code="needs_human", ms=1000, endpoint="other")
    usage.clock.advance(3600)
    row(usage, ms=7)
    chart = usage.chart(API, hours=2)
    past, now = chart["points"]
    assert (past["calls"], past["cached"], past["fresh"]) == (102, 50, 50)
    assert past["errors"] == {"pace_limited": 1, "needs_human": 1} and past["p95_ms"] >= 95
    assert (now["calls"], now["p50_ms"]) == (1, 7)
    one = usage.chart(API, hours=2, endpoint="other")["points"][0]
    assert one["calls"] == 1 and one["errors"] == {"needs_human": 1} and one["p50_ms"] == 1000
    assert usage.chart("another", hours=2)["totals"]["calls"] == 0


def test_rolling_up_twice_counts_once(usage):
    row(usage)
    usage.clock.advance(3600)
    usage.roll()
    usage.roll()
    assert usage.chart(API, hours=2)["points"][0]["calls"] == 1


def test_hours_with_no_calls_are_points_too(usage):
    row(usage)
    usage.clock.advance(5 * 3600)
    points = usage.chart(API, hours=6)["points"]
    assert [p["calls"] for p in points] == [1, 0, 0, 0, 0, 0]
    assert points[1] == {"hour": points[1]["hour"], "calls": 0, "cached": 0, "fresh": 0, "errors": {}, "p50_ms": 0,
                         "p95_ms": 0}


def test_rows_and_rollups_are_kept_for_30_days(usage):
    row(usage)
    usage.clock.advance(3600)
    usage.roll()
    usage.clock.set(START + timedelta(seconds=RETENTION_S + 7200))
    usage.roll()
    assert usage.store.usage_first() is None
    assert usage.chart(API, hours=720)["totals"]["calls"] == 0
