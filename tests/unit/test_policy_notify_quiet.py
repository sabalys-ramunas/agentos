"""Quiet hours, digest times, the batch window and stale-aware urgency (AUD-34), as pure rules."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from openajar.policy.quiet_hours import (
    MEMBER_DEFAULTS,
    OWNER_DEFAULTS,
    Timing,
    in_quiet,
    next_digest,
    plan,
    quiet_end,
)

VILNIUS = ZoneInfo("Europe/Vilnius")
NEW_YORK = ZoneInfo("America/New_York")


def at(s: str, tz: ZoneInfo = VILNIUS) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=tz).timestamp()


def wall(t: float, tz: ZoneInfo = VILNIUS) -> str:
    return datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d %H:%M")


VA = Timing(timezone="Europe/Vilnius", quiet_hours=["21:00", "08:00"], batch_window_s=600,
            urgent_breaks_quiet=False)


def test_defaults_follow_the_design():
    assert MEMBER_DEFAULTS.quiet_hours == ["21:00", "08:00"] and MEMBER_DEFAULTS.batch_window_s == 600
    assert MEMBER_DEFAULTS.urgent_breaks_quiet is False
    assert OWNER_DEFAULTS.quiet_hours is None and OWNER_DEFAULTS.urgent_breaks_quiet is True  # solo: unchanged


@pytest.mark.parametrize(("when", "quiet"), [
    ("2026-06-10 20:59", False), ("2026-06-10 21:00", True), ("2026-06-10 23:59", True),
    ("2026-06-11 00:00", True), ("2026-06-11 07:59", True), ("2026-06-11 08:00", False), ("2026-06-11 13:00", False)])
def test_quiet_hours_across_midnight(when, quiet):
    assert in_quiet(at(when), VA) is quiet


def test_quiet_hours_within_one_day():
    nap = Timing(quiet_hours=["13:00", "14:30"])
    assert not in_quiet(at("2026-06-10 12:59", ZoneInfo("UTC")), nap)
    assert in_quiet(at("2026-06-10 14:29", ZoneInfo("UTC")), nap)
    assert quiet_end(at("2026-06-10 13:10", ZoneInfo("UTC")), nap) == at("2026-06-10 14:30", ZoneInfo("UTC"))


def test_quiet_end_is_the_next_morning():
    assert wall(quiet_end(at("2026-06-10 22:15"), VA)) == "2026-06-11 08:00"
    assert wall(quiet_end(at("2026-06-11 03:00"), VA)) == "2026-06-11 08:00"
    assert quiet_end(at("2026-06-11 09:00"), VA) is None


def test_spring_forward_night_is_an_hour_shorter():
    start = at("2026-03-28 22:00")  # Vilnius jumps from 03:00 to 04:00 on 2026-03-29
    end = quiet_end(start, VA)
    assert wall(end) == "2026-03-29 08:00" and end - start == 9 * 3600


def test_fall_back_night_is_an_hour_longer():
    start = at("2026-10-24 22:00")  # Vilnius goes from 04:00 back to 03:00 on 2026-10-25
    end = quiet_end(start, VA)
    assert wall(end) == "2026-10-25 08:00" and end - start == 11 * 3600
    # 03:30 happens twice that night; both are quiet
    first = datetime(2026, 10, 25, 3, 30, tzinfo=VILNIUS, fold=0).timestamp()
    second = datetime(2026, 10, 25, 3, 30, tzinfo=VILNIUS, fold=1).timestamp()
    assert second - first == 3600 and in_quiet(first, VA) and in_quiet(second, VA)


def test_an_end_inside_the_spring_gap_lands_after_it():
    t = Timing(timezone="Europe/Vilnius", quiet_hours=["23:00", "03:30"])  # 03:30 does not exist on 2026-03-29
    end = quiet_end(at("2026-03-29 01:00"), t)
    assert wall(end) == "2026-03-29 04:30" and not in_quiet(end, t)


def test_other_zones_change_on_their_own_dates():
    ny = Timing(timezone="America/New_York", quiet_hours=["22:00", "07:00"])
    start = at("2026-03-07 23:00", NEW_YORK)  # US clocks spring forward on 2026-03-08
    assert quiet_end(start, ny) - start == 7 * 3600
    assert in_quiet(at("2026-03-08 06:30", NEW_YORK), ny) and not in_quiet(at("2026-03-08 07:00", NEW_YORK), ny)


def test_digest_times_skip_quiet_hours():
    t = VA.model_copy(update={"digest_at": ["08:00", "13:00", "18:00"]})
    assert wall(next_digest(at("2026-06-10 10:00"), t)) == "2026-06-10 13:00"
    assert wall(next_digest(at("2026-06-10 19:00"), t)) == "2026-06-11 08:00"
    early = Timing.model_validate({**VA.model_dump(), "digest_at": ["07:00", "12:00"]})
    assert wall(next_digest(at("2026-06-10 13:00"), early)) == "2026-06-11 12:00"  # 07:00 is quiet


@pytest.mark.parametrize("bad", [
    {"timezone": "Mars/Base"}, {"quiet_hours": ["08:00", "08:00"]}, {"quiet_hours": ["8am", "9pm"]},
    {"quiet_hours": ["21:00"]}, {"digest_at": ["25:00"]},
    {"quiet_hours": ["21:00", "08:00"], "digest_at": ["22:00", "07:00"]}])
def test_bad_timing_is_refused(bad):
    with pytest.raises(ValueError):
        Timing.model_validate(bad)


def test_digest_times_are_sorted_and_unique():
    assert Timing(digest_at=["18:00", "08:00", "18:00"]).digest_at == ["08:00", "18:00"]


def test_awake_and_nothing_waiting_goes_now():
    now = at("2026-06-10 10:00")
    assert plan(now, VA) == plan(now, VA, last_push=now - 3600)
    p = plan(now, VA)
    assert p.due == now and p.held is None and not p.urgent


def test_the_batch_window_collapses_what_follows_the_first_push():
    now = at("2026-06-10 10:00")
    p = plan(now, VA, last_push=now - 120)
    assert p.held == "batch" and p.due == now - 120 + 600
    joined = plan(now + 60, VA, last_push=now - 120, open_batch=p.due)
    assert joined.due == p.due  # joins the batch already waiting
    assert plan(now, VA.model_copy(update={"batch_window_s": 0}), last_push=now - 1).held is None


def test_quiet_hours_hold_until_the_morning():
    now = at("2026-06-10 22:00")
    p = plan(now, VA)
    assert p.held == "quiet" and wall(p.due) == "2026-06-11 08:00" and not p.urgent


def test_digest_times_hold_outside_quiet_hours():
    t = VA.model_copy(update={"digest_at": ["13:00"]})
    p = plan(at("2026-06-10 10:00"), t)
    assert p.held == "digest" and wall(p.due) == "2026-06-10 13:00"


def test_urgent_only_when_the_run_would_go_stale():
    now = at("2026-06-10 22:00")
    later = plan(now, VA, not_after=at("2026-06-11 09:00"))
    assert later.held == "quiet" and not later.urgent  # it keeps until the morning
    stale = plan(now, VA, not_after=at("2026-06-11 06:00"))
    assert stale.urgent and stale.held == "quiet" and wall(stale.due) == "2026-06-11 08:00"  # VA: not woken
    breaks = plan(now, VA.model_copy(update={"urgent_breaks_quiet": True}), not_after=at("2026-06-11 06:00"))
    assert breaks.urgent and breaks.held is None and breaks.due == now


def test_urgent_skips_digests_and_batches_but_not_forbidden_quiet():
    t = VA.model_copy(update={"digest_at": ["13:00"]})
    now = at("2026-06-10 10:00")
    assert plan(now, t, urgent=True).due == now
    assert plan(now, t, not_after=now + 600).due == now  # stale before the 13:00 digest
    assert plan(now, VA, last_push=now - 10, not_after=now + 60).due == now  # stale before the batch ends
    assert plan(at("2026-06-10 23:00"), VA, urgent=True).held == "quiet"  # urgent_breaks_quiet is off
