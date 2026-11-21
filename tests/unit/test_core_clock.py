"""openajar.core.clock: one set of time helpers, pinned byte for byte against the private copies it
replaced (each copy is kept here as it was, next to the call that took its place)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from openajar.core import clock

MOMENTS = [
    datetime(2026, 11, 14, 9, 30, 0, tzinfo=UTC),
    datetime(2026, 11, 14, 9, 30, 0, 123456, tzinfo=UTC),
    datetime(2026, 11, 14, 9, 30, 0, 1000, tzinfo=UTC),
    datetime(2026, 11, 14, 11, 30, 5, 500, tzinfo=timezone(timedelta(hours=2))),
    datetime(1999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
]
STAMPS = [0.0, 1.0, 1763112600.0, 1763112600.123456, 1763112600.5, 1763112600.000001, 1763112600]


# ---- the copies, as they were -------------------------------------------------------------------

def plain_from_ts(ts):  # api/auth, api/members, api/routes_core, api/routes_members, agenda/service._ts
    return datetime.fromtimestamp(ts, UTC).isoformat()


def z_from_ts(ts):  # policy/util.iso, api/web/session.iso, vault/relay, node/login_assist
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def z_from_dt(dt):  # api/web/home, api/web/topology, environments/manager, dev/tuidemo, node/channel/reports
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def seconds_from_ts(ts):  # node/rulehealth, studio/store, textreview/review
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def seconds_from_dt(dt):  # keepalive/service
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def seconds_z_from_ts(ts):  # memory/models.iso, runner/siteapis/keys.iso
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def seconds_z_strftime(moment):  # node/channel/wire.iso, node/camoufox/launch
    if isinstance(moment, int | float):
        moment = datetime.fromtimestamp(moment, UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def millis_z_from_ts(ts):  # node/devtools
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def micros_fixed(dt):  # runner/util.iso: fixed width, so SQLite string order is time order
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def viewer_iso(value):  # node/viewer_tokens.iso
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def compact(now=None):  # core/migrate._stamp, node/lanefmt._stamp
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")


def parse_dt(value):  # runner/util.parse_dt
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---- pins ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("ts", STAMPS)
def test_timestamps_format_as_every_copy_did(ts):
    assert clock.iso(ts) == plain_from_ts(ts)
    assert clock.iso(ts, z=True) == z_from_ts(ts)
    assert clock.iso(ts, timespec="seconds") == seconds_from_ts(ts)
    assert clock.iso(ts, z=True, timespec="seconds") == seconds_z_from_ts(ts) == seconds_z_strftime(ts)
    assert clock.iso(ts, z=True, timespec="milliseconds") == millis_z_from_ts(ts)
    assert clock.iso(ts, z=True) == viewer_iso(ts)


@pytest.mark.parametrize("dt", MOMENTS)
def test_datetimes_format_as_every_copy_did(dt):
    assert clock.iso(dt, z=True) == z_from_dt(dt) == viewer_iso(dt)
    assert clock.iso(dt, timespec="seconds") == seconds_from_dt(dt)
    assert clock.iso(dt, z=True, timespec="seconds") == seconds_z_strftime(dt)
    assert clock.iso(dt, timespec="microseconds") == micros_fixed(dt)
    assert clock.stamp(dt) == compact(dt)


def test_formats_are_what_the_stores_write():
    dt = datetime(2026, 11, 14, 9, 30, 0, 123456, tzinfo=UTC)
    assert clock.iso(dt) == "2026-11-14T09:30:00.123456+00:00"
    assert clock.iso(dt, z=True) == "2026-11-14T09:30:00.123456Z"
    assert clock.iso(dt, timespec="seconds") == "2026-11-14T09:30:00+00:00"
    assert clock.iso(dt, z=True, timespec="seconds") == "2026-11-14T09:30:00Z"
    assert clock.iso(dt.replace(microsecond=0), timespec="microseconds") == "2026-11-14T09:30:00.000000+00:00"
    assert clock.stamp(dt) == "20261114T093000123456Z"


def test_none_is_now_and_missing_stays_missing():
    before = datetime.now(UTC)
    got = datetime.fromisoformat(clock.iso())
    assert before <= got <= datetime.now(UTC)
    assert clock.iso_or_none(None) is None
    assert clock.iso_or_none(0.0, z=True) == "1970-01-01T00:00:00Z"  # 0 is the epoch, not missing
    assert clock.now().tzinfo is UTC
    assert len(clock.stamp()) == len("20261114T093000123456Z")


@pytest.mark.parametrize("text", [None, "", "2026-11-14T09:30:00Z", "2026-11-14T09:30:00+02:00",
                                  "2026-11-14T09:30:00.5", "2026-11-14"])
def test_parse_matches_the_runner_copy(text):
    assert clock.parse(text) == parse_dt(text)
    if text:
        assert clock.parse(text).tzinfo is not None


def test_parse_refuses_what_is_not_a_date():
    with pytest.raises(ValueError):
        clock.parse("yesterday")


def test_the_clock_service():
    c = clock.Clock()
    assert c.now().tzinfo is UTC
    t0 = c.monotonic()
    asyncio.run(c.sleep(-1))  # never negative
    assert c.monotonic() >= t0
    assert abs(time.time() - c.now().timestamp()) < 5
