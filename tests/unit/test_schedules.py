"""Schedules and triggers: cron with timezones and DST, missed runs, overlap, clock jumps,
page watches, webhooks with HMAC, and the dead-man check."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentos.core.errors import InvalidRequest
from agentos.core.models import RunStatus
from agentos.runner.fakes import BASE, El, FakeClock, FakeNotifier, Harness
from agentos.runner.schedules import _jitter, cron_slots, sign_webhook

VILNIUS = ZoneInfo("Europe/Vilnius")


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def read_flow(fid="biz-a.social.check"):
    return {"id": fid, "lane": "biz-a/social",
            "params": {"properties": {"note": {"type": "string"}}},
            "steps": [{"type": "navigate", "url": f"{BASE}/"},
                      {"type": "readback", "locator": {"role": "heading", "name": "Feed"},
                       "condition": {"kind": "exists"}}]}


def harness(tmp_path, start: str, **kw):
    h = Harness(tmp_path, clock=FakeClock(at(start)), **kw)
    fv = h.save(read_flow())
    return h, fv


def runs(h, s):
    return h.store.list_runs(schedule_id=s.id, oldest_first=True)


async def walk(h, until: str, step_s: int = 60):
    end = at(until)
    while h.clock.now() < end:
        h.clock.advance(step_s)
        await h.scheduler.tick()


async def test_cron_fires_at_local_time_with_vilnius_default(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T06:58:00")  # 08:58 in Vilnius (UTC+2 in November)
    s, _ = h.scheduler.add(flow=fv.id, cron="0 9 * * *")
    assert s.cron.timezone == "Europe/Vilnius"
    assert h.scheduler.describe(s)["next_run"] == "2026-11-04T07:00:00+00:00"
    await h.scheduler.tick()
    assert runs(h, s) == []
    h.clock.advance(125)
    stats = await h.scheduler.tick()
    assert stats["fired"] == 1
    fired = runs(h, s)
    assert len(fired) == 1 and fired[0].trigger["slot"] == "2026-11-04T07:00:00+00:00"
    assert fired[0].idempotency_key == f"cron:{s.id}:2026-11-04T07:00:00+00:00"
    assert fired[0].source == f"cron:{s.id}" and not fired[0].authorized
    await h.scheduler.tick()
    await h.scheduler.tick()
    assert len(runs(h, s)) == 1  # duplicate ticks never fire twice
    await h.runner.run_until_idle()
    assert h.runner.get(fired[0].id).status == RunStatus.SUCCEEDED_VERIFIED


async def test_timezone_from_a_param(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T08:59:00")
    s, _ = h.scheduler.add(flow=fv.id, cron="0 9 * * *", timezone="UTC")
    h.clock.advance(61)
    await h.scheduler.tick()
    assert runs(h, s)[0].trigger["slot"] == "2026-11-04T09:00:00+00:00"
    with pytest.raises(InvalidRequest, match="timezone"):
        h.scheduler.add(flow=fv.id, cron="0 9 * * *", timezone="Mars/Olympus")
    with pytest.raises(InvalidRequest, match="cron"):
        h.scheduler.add(flow=fv.id, cron="every day at nine")
    with pytest.raises(InvalidRequest, match="exactly one"):
        h.scheduler.add(flow=fv.id)


async def test_missed_runs_skip_or_catch_up_once(tmp_path):
    for policy, expected in (("skip", 0), ("catch_up_once", 1)):
        h, fv = harness(tmp_path / policy, "2026-11-04T06:00:30")
        s, _ = h.scheduler.add(flow=fv.id, cron="0 * * * *", missed=policy)
        h.clock.set(at("2026-11-04T12:30:00"))  # the node was down for six hours
        stats = await h.scheduler.tick()
        fired = runs(h, s)
        assert len(fired) == expected, policy
        assert stats["skipped"] == 6 - expected
        if expected:
            assert fired[0].trigger == {"kind": "cron", "slot": "2026-11-04T12:00:00+00:00", "late": True}
        await h.scheduler.tick()
        assert len(runs(h, s)) == expected


async def test_long_outage_with_a_minutely_cron_catches_up_once(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T06:00:30")
    s, _ = h.scheduler.add(flow=fv.id, cron="* * * * *", missed="catch_up_once")
    h.clock.set(at("2026-11-14T06:00:40"))  # ten days later
    await h.scheduler.tick()
    fired = runs(h, s)
    assert [r.trigger["slot"] for r in fired] == ["2026-11-14T06:00:00+00:00"]


async def test_on_time_slot_fires_even_after_downtime(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T06:00:30")
    s, _ = h.scheduler.add(flow=fv.id, cron="0 * * * *", missed="skip")
    h.clock.set(at("2026-11-04T12:00:20"))  # back up just after a slot
    await h.scheduler.tick()
    fired = runs(h, s)
    assert len(fired) == 1 and fired[0].trigger["late"] is False


async def test_jitter_is_deterministic_and_delays_the_fire(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T06:59:00")
    s, _ = h.scheduler.add(flow=fv.id, cron="0 9 * * *", jitter_s=900)
    slot = at("2026-11-04T07:00:00")
    j = _jitter(s.id, slot, 900)
    assert j == _jitter(s.id, slot, 900) and timedelta(0) <= j <= timedelta(seconds=900)
    h.clock.set(slot + j - timedelta(seconds=1))
    await h.scheduler.tick()
    assert runs(h, s) == [] or j == timedelta(0)
    h.clock.set(slot + j + timedelta(seconds=1))
    await h.scheduler.tick()
    assert len(runs(h, s)) == 1


async def test_no_overlapping_runs_of_the_same_schedule(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T07:00:30")
    s, _ = h.scheduler.add(flow=fv.id, cron="* * * * *")
    h.clock.advance(60)
    await h.scheduler.tick()
    assert len(runs(h, s)) == 1  # queued, not executed yet
    h.clock.advance(60)
    stats = await h.scheduler.tick()
    assert stats["fired"] == 0 and stats["skipped"] == 1
    assert h.scheduler.get(s.id).state["last_skip"]["reason"] == "previous run still active"
    await h.runner.run_until_idle()
    h.clock.advance(60)
    await h.scheduler.tick()
    assert len(runs(h, s)) == 2


async def test_restart_between_fire_and_cursor_save_does_not_double_fire(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T06:59:30")
    s, _ = h.scheduler.add(flow=fv.id, cron="0 9 * * *")
    saved = h.scheduler.get(s.id)
    h.clock.advance(60)
    await h.scheduler.tick()
    assert len(runs(h, s)) == 1
    h.store.put_schedule(saved)  # the cursor write was lost in a crash
    h2 = Harness(tmp_path, site=h.site, clock=h.clock)
    stats = await h2.scheduler.tick()
    assert stats["fired"] == 0 and len(runs(h2, s)) == 1


def test_cron_slots_across_spring_forward():
    # 2027-03-28: Vilnius jumps from 03:00 EET to 04:00 EEST; 03:30 does not exist
    daily = cron_slots("30 3 * * *", VILNIUS, at("2027-03-27T20:00:00"), at("2027-03-28T06:00:00"))
    assert daily == [at("2027-03-28T01:30:00")]  # once, shifted to 04:30 local
    hourly = cron_slots("30 * * * *", VILNIUS, at("2027-03-27T23:00:00"), at("2027-03-28T03:00:00"))
    assert hourly == [at("2027-03-27T23:30:00"), at("2027-03-28T00:30:00"),
                      at("2027-03-28T01:30:00"), at("2027-03-28T02:30:00")]  # no duplicate instant


def test_cron_slots_across_fall_back():
    # 2027-10-31: Vilnius goes from 04:00 EEST back to 03:00 EET; 03:30 happens twice
    daily = cron_slots("30 3 * * *", VILNIUS, at("2027-10-30T20:00:00"), at("2027-10-31T06:00:00"))
    assert daily == [at("2027-10-31T00:30:00")]  # the first 03:30 only
    hourly = cron_slots("30 * * * *", VILNIUS, at("2027-10-30T23:00:00"), at("2027-10-31T03:00:00"))
    assert hourly == [at("2027-10-30T23:30:00"), at("2027-10-31T00:30:00"), at("2027-10-31T02:30:00")]


@pytest.mark.parametrize("start,until,expected", [
    ("2027-03-27T22:00:00", "2027-03-28T04:00:00", ["2027-03-28T01:30:00+00:00"]),
    ("2027-10-30T22:00:00", "2027-10-31T04:00:00", ["2027-10-31T00:30:00+00:00"]),
])
async def test_daily_schedule_fires_exactly_once_on_dst_days(tmp_path, start, until, expected):
    h, fv = harness(tmp_path, start)
    s, _ = h.scheduler.add(flow=fv.id, cron="30 3 * * *")
    await walk(h, until, step_s=30)
    assert [r.trigger["slot"] for r in runs(h, s)] == expected
    assert all(not r.trigger["late"] for r in runs(h, s))


async def test_hourly_schedule_through_fall_back_never_duplicates(tmp_path):
    h, fv = harness(tmp_path, "2027-10-30T22:00:30")
    s, _ = h.scheduler.add(flow=fv.id, cron="30 * * * *")

    async def tick_and_finish():
        await h.scheduler.tick()
        await h.runner.run_until_idle()

    end = at("2027-10-31T04:00:00")
    while h.clock.now() < end:
        h.clock.advance(60)
        await tick_and_finish()
    slots = [r.trigger["slot"] for r in runs(h, s)]
    assert len(slots) == len(set(slots))
    assert "2027-10-31T00:30:00+00:00" in slots and "2027-10-31T01:30:00+00:00" not in slots


async def test_clock_jumps_forward_and_back(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T09:00:30")
    s, _ = h.scheduler.add(flow=fv.id, cron="0 * * * *", missed="catch_up_once")
    h.clock.set(at("2026-11-04T14:20:00"))  # NTP corrected a slow clock by five hours
    await h.scheduler.tick()
    assert [r.trigger["slot"] for r in runs(h, s)] == ["2026-11-04T14:00:00+00:00"]
    await h.runner.run_until_idle()

    h.clock.set(at("2026-11-04T13:58:00"))  # and back again
    await h.scheduler.tick()
    assert "clock_moved_back_at" in h.scheduler.get(s.id).state
    h.clock.set(at("2026-11-04T14:00:30"))
    await h.scheduler.tick()
    assert len(runs(h, s)) == 1  # the 14:00 slot already ran: deduplicated
    h.clock.set(at("2026-11-04T15:00:30"))
    await h.scheduler.tick()
    assert len(runs(h, s)) == 2


async def test_disabled_schedule_does_not_replay_when_enabled(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T06:00:30")
    s, _ = h.scheduler.add(flow=fv.id, cron="0 * * * *", missed="catch_up_once")
    h.scheduler.set_enabled(s.id, False)
    h.clock.set(at("2026-11-04T10:30:00"))
    await h.scheduler.tick()
    h.scheduler.set_enabled(s.id, True)
    await h.scheduler.tick()
    assert runs(h, s) == []


# ---------------------------------------------------------------- page watches


def watch_site(h):
    h.site.state["status"] = "Open"
    h.site.state["items"] = 2

    def page(s, d):
        main = El("main").add(El("status", "Shop status", text=s.state["status"]))
        for i in range(s.state["items"]):
            main.add(El("listitem", f"Item {i}"))
        return El("document", "Shop").add(main)

    h.site.route(f"{BASE}/shop", page)


async def test_page_watch_baseline_debounce_and_fire(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T09:00:00")
    watch_site(h)
    s, _ = h.scheduler.add(flow=fv.id, params={"note": "status is now ${trigger.value}"},
                           watch={"url": f"{BASE}/shop", "locator": {"role": "status", "name": "Shop status"},
                                  "every_s": 60, "debounce_s": 120})
    await h.scheduler.tick()
    assert h.scheduler.get(s.id).state["last_value"] == "Open" and runs(h, s) == []

    h.site.state["status"] = "Closed"
    h.clock.advance(60)
    await h.scheduler.tick()  # first sight of the change: pending, not fired
    assert runs(h, s) == [] and h.scheduler.get(s.id).state["pending_value"] == "Closed"

    h.site.state["status"] = "Open"  # a flicker
    h.clock.advance(120)
    await h.scheduler.tick()
    assert runs(h, s) == [] and "pending_value" not in h.scheduler.get(s.id).state

    h.site.state["status"] = "Sold out"
    h.clock.advance(60)
    await h.scheduler.tick()
    h.clock.advance(60)
    await h.scheduler.tick()
    assert runs(h, s) == []  # seen for 60s, debounce is 120s
    h.clock.advance(60)
    await h.scheduler.tick()
    fired = runs(h, s)
    assert len(fired) == 1
    assert fired[0].trigger == {"kind": "watch", "value": "Sold out", "previous": "Open", "seq": 1}
    assert fired[0].params == {"note": "status is now Sold out"}
    h.clock.advance(60)
    await h.scheduler.tick()
    assert len(runs(h, s)) == 1  # no new change, no new run


async def test_page_watch_count_and_busy_lane(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T09:00:00")
    watch_site(h)
    s, _ = h.scheduler.add(flow=fv.id, watch={"url": f"{BASE}/shop", "read": "count",
                                              "locator": {"role": "listitem", "within": {"role": "main"}},
                                              "every_s": 60})
    await h.scheduler.tick()
    assert h.scheduler.get(s.id).state["last_value"] == 2
    h.site.state["items"] = 3
    h.runner._lanes.add("biz-a/social")  # a run holds the lane
    h.clock.advance(60)
    stats = await h.scheduler.tick()
    assert stats["watched"] == 0 and runs(h, s) == []
    h.runner._lanes.discard("biz-a/social")
    await h.scheduler.tick()
    assert [r.trigger["value"] for r in runs(h, s)] == [3]


async def test_failing_watch_notifies(tmp_path):
    notifier = FakeNotifier()
    h, fv = harness(tmp_path, "2026-11-04T09:00:00", services={"notifier": notifier})
    watch_site(h)
    h.scheduler.add(flow=fv.id, watch={"url": f"{BASE}/shop", "every_s": 60,
                                              "locator": {"role": "status", "name": "Gone"}})
    for _ in range(3):
        await h.scheduler.tick()
        h.clock.advance(60)
    await asyncio.sleep(0)
    assert [n["title"] for n in notifier.sent] == ["A page watch keeps failing"]


# ---------------------------------------------------------------- webhooks


async def test_webhook_hmac_replay_and_duplicates(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T09:00:00")
    s, extras = h.scheduler.add(flow=fv.id, webhook={"accept_params": True})
    token = extras["url_path"].rsplit("/", 1)[-1]
    secret = extras["secret"]
    assert secret not in json.dumps(h.scheduler.describe(s)) and token not in json.dumps(h.scheduler.describe(s))

    def headers(body: bytes, ts: float | None = None, delivery: str | None = "d-1", sig: str | None = None):
        ts_s = str(int(ts if ts is not None else h.clock.now().timestamp()))
        out = {"X-Agentos-Timestamp": ts_s, "X-Agentos-Signature": sig or sign_webhook(secret, ts_s, body)}
        if delivery:
            out["X-Agentos-Delivery"] = delivery
        return out

    body = json.dumps({"params": {"note": "from the shop"}}).encode()
    status, out = await h.scheduler.handle_webhook(token, body, headers(body))
    assert status == 202 and not out["duplicate"]
    assert h.runner.get(out["run_id"]).params == {"note": "from the shop"}
    status, again = await h.scheduler.handle_webhook(token, body, headers(body))
    assert status == 200 and again["duplicate"] and again["run_id"] == out["run_id"]

    assert (await h.scheduler.handle_webhook(token, body, headers(body, sig="sha256=00")))[0] == 401
    old = h.clock.now().timestamp() - 3600
    assert (await h.scheduler.handle_webhook(token, body, headers(body, ts=old)))[0] == 401
    assert (await h.scheduler.handle_webhook("nope", body, headers(body)))[0] == 404
    bad = json.dumps({"params": {"note": 5}}).encode()
    assert (await h.scheduler.handle_webhook(token, bad, headers(bad, delivery="d-2")))[0] == 400
    assert len(runs(h, s)) == 1


# ---------------------------------------------------------------- dead-man


async def test_deadman_alerts_when_runs_go_missing_or_keep_failing(tmp_path):
    notifier = FakeNotifier()
    h, fv = harness(tmp_path, "2026-11-04T09:00:00", services={"notifier": notifier})
    s, _ = h.scheduler.add(flow=fv.id, cron="0 9 * * *", deadman_minutes=60, max_failures=3)
    h.clock.advance(61 * 60)
    await h.scheduler.tick()
    await h.scheduler.tick()
    await asyncio.sleep(0)
    assert [n["title"] for n in notifier.sent] == ["A scheduled run is missing"]

    for i in range(3):
        run, _ = h.runner.submit(flow_id=fv.id, idempotency_key=f"x{i}", schedule_id=s.id, authorized=True)
        run.status, run.finished_at = RunStatus.FAILED, h.clock.now()
        run.error = {"code": "element_not_found", "message": "no element matches heading"}
        h.store.save_run(run)
    await h.scheduler.tick()
    await h.scheduler.tick()
    await asyncio.sleep(0)
    titles = [n["title"] for n in notifier.sent]
    assert titles == ["A scheduled run is missing", "A schedule keeps failing"]
    assert "no element matches heading" in notifier.sent[-1]["body"]

    h.runner.submit(flow_id=fv.id, idempotency_key="ok", schedule_id=s.id, authorized=True)
    await h.runner.run_until_idle()
    await h.scheduler.tick()
    state = h.scheduler.get(s.id).state
    assert state["deadman_alerted"] is False and state["failing_alerted"] is False
