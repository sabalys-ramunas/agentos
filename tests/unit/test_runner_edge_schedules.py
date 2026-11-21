"""Schedule edge cases: cron at DST edges, jitter, impossible cron, a broken schedule that stops the
others, the dead-man check when a fire cannot happen, watch write-backs and clock jumps, and webhook
replay and header parsing.

Each `xfail(strict=True)` is a real bug; its reason names the BUG id used in the QA report.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from openajar.core.errors import InvalidRequest
from openajar.runner.fakes import BASE, El, FakeClock, FakeNotifier, Harness
from openajar.runner.schedules import _jitter, cron_slots, sign_webhook

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
    return h, h.save(read_flow())


def runs(h, s):
    return h.store.list_runs(schedule_id=s.id, oldest_first=True)


def watch_site(h):
    h.site.state["status"] = "Open"

    def page(s, d):
        return El("document", "Shop").add(El("main").add(El("status", "Shop status", text=s.state["status"])))

    h.site.route(f"{BASE}/shop", page)


WATCH = {"url": f"{BASE}/shop", "locator": {"role": "status", "name": "Shop status"}, "every_s": 60}


# ---------------------------------------------------------------- DST edges


def test_a_quarter_hour_cron_has_no_duplicate_slots_across_either_dst_change():
    spring = cron_slots("*/15 * * * *", VILNIUS, at("2027-03-27T23:00:00"), at("2027-03-28T03:00:00"))
    # the local 03:00-03:45 gap maps forward onto 04:00-04:45 EEST, so each real quarter-hour fires once
    assert spring == [at("2027-03-27T23:15:00") + timedelta(minutes=15 * i) for i in range(16)]
    fall = cron_slots("*/15 * * * *", VILNIUS, at("2027-10-30T23:00:00"), at("2027-10-31T03:00:00"))
    # 03:00-03:45 happens twice; only the first pass fires, then 04:00 EET (02:00 UTC) continues
    assert [s.strftime("%H:%M") for s in fall] == [
        "23:15", "23:30", "23:45", "00:00", "00:15", "00:30", "00:45", "02:00", "02:15", "02:30", "02:45", "03:00"]
    assert all(s.tzinfo is not None for s in spring + fall)
    assert len(set(spring)) == len(spring) and len(set(fall)) == len(fall)


# ---------------------------------------------------------------- cron validity and jitter


def test_a_cron_that_can_never_match_is_rejected_up_front(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T09:00:00")
    with pytest.raises(InvalidRequest):
        h.scheduler.add(flow=fv.id, cron="0 0 30 2 *")


def _pick_schedule_with_a_long_jitter(h, flow_id):
    """A schedule from before jitter_s was checked against the cron's gap, whose 10:00 slot waits
    out a jitter that outlasts the 10:05 slot's jitter."""
    early, late = at("2026-11-04T10:00:00"), at("2026-11-04T10:05:00")
    for _ in range(500):
        s, _ = h.scheduler.add(flow=flow_id, cron="*/5 * * * *", timezone="UTC")
        s.cron = s.cron.model_copy(update={"jitter_s": 600})
        h.store.put_schedule(s)
        j_early, j_late = _jitter(s.id, early, 600), _jitter(s.id, late, 600)
        if j_early - j_late > timedelta(seconds=300):
            return s, j_early, j_late
        h.store.delete_schedule(s.id)
    raise AssertionError("no schedule id with a long jitter in 500 tries")


def test_a_jitter_as_long_as_the_gap_between_slots_is_refused(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T09:59:00")
    with pytest.raises(InvalidRequest, match="jitter_s"):
        h.scheduler.add(flow=fv.id, cron="*/5 * * * *", timezone="UTC", jitter_s=300)
    s, _ = h.scheduler.add(flow=fv.id, cron="*/5 * * * *", timezone="UTC", jitter_s=299)
    assert s.cron.jitter_s == 299


async def test_a_slot_a_later_one_overtook_is_dropped_on_record_not_in_silence(tmp_path):
    """BUG-14: when a later slot comes due first, the cursor jumps past a jittered slot that has
    not come due yet. New schedules cannot be made that way; an old one says what it dropped."""
    h, fv = harness(tmp_path, "2026-11-04T09:59:00")
    s, j_early, j_late = _pick_schedule_with_a_long_jitter(h, fv.id)
    early, late = at("2026-11-04T10:00:00"), at("2026-11-04T10:05:00")
    h.clock.set(late + j_late)  # 10:05 is due; 10:00 is still waiting out its jitter
    await h.scheduler.tick()
    h.clock.set(early + j_early + timedelta(seconds=1))
    await h.scheduler.tick()
    assert [r.trigger["slot"] for r in runs(h, s)] == [late.isoformat()]
    state = h.store.get_schedule(s.id).state
    assert state["last_dropped"]["slots"] == [early.isoformat()] and state["dropped_total"] == 1
    assert state["last_dropped"]["fired_instead"] == late.isoformat() and "—" not in state["last_dropped"]["reason"]


async def test_a_schedule_whose_flow_was_deleted_does_not_stop_the_others(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T08:59:00")
    other = h.save(read_flow("biz-a.social.other"))
    broken, _ = h.scheduler.add(flow=fv.id, cron="0 9 * * *", timezone="UTC")
    good, _ = h.scheduler.add(flow=other.id, cron="0 9 * * *", timezone="UTC")
    h.store.delete_flow(fv.id)
    h.clock.advance(120)
    await h.scheduler.tick()
    assert len(runs(h, good)) == 1 and runs(h, broken) == []


async def test_one_unreadable_schedule_row_does_not_stop_the_others(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T08:59:00")
    good, _ = h.scheduler.add(flow=fv.id, cron="0 9 * * *", timezone="UTC")
    bad, _ = h.scheduler.add(flow=fv.id, cron="0 9 * * *", timezone="UTC")
    h.store._exec("UPDATE schedules SET doc=? WHERE id=?", ("{}", bad.id))
    h.clock.advance(120)
    await h.scheduler.tick()
    assert len(runs(h, good)) == 1


def test_schedule_params_are_validated_even_when_one_value_is_a_template(tmp_path):
    h = Harness(tmp_path, clock=FakeClock(at("2026-11-04T09:00:00")))
    flow = read_flow("biz-a.social.note")
    flow["params"] = {"type": "object", "required": ["must"],
                      "properties": {"must": {"type": "string"}, "note": {"type": "string"}}}
    fv = h.save(flow)
    with pytest.raises(InvalidRequest):
        h.scheduler.add(flow=fv.id, cron="0 9 * * *", params={"note": "from ${trigger.kind}"})


# ---------------------------------------------------------------- dead-man


async def test_deadman_still_alerts_when_a_schedule_cannot_fire(tmp_path):
    notifier = FakeNotifier()
    h, fv = harness(tmp_path, "2026-11-04T09:00:00", services={"notifier": notifier})
    h.scheduler.add(flow=fv.id, cron="* * * * *", deadman_minutes=30)
    h.store.delete_flow(fv.id)  # every fire now raises NotFound
    for _ in range(40):
        h.clock.advance(60)
        await h.scheduler.tick()
    await asyncio.sleep(0)
    assert [n["title"] for n in notifier.sent] == ["A scheduled run is missing"]


async def test_deadman_alerts_once_per_incident_and_again_for_the_next(tmp_path):
    notifier = FakeNotifier()
    h, fv = harness(tmp_path, "2026-11-04T09:00:00", services={"notifier": notifier})
    s, _ = h.scheduler.add(flow=fv.id, cron="0 9 * * *", deadman_minutes=60)
    h.clock.advance(61 * 60)
    await h.scheduler.tick()
    await h.scheduler.tick()
    h.runner.submit(flow_id=fv.id, idempotency_key="ok-1", schedule_id=s.id, authorized=True)
    await h.runner.run_until_idle()
    await h.scheduler.tick()  # a success clears the incident
    h.clock.advance(61 * 60)
    await h.scheduler.tick()
    await h.scheduler.tick()
    await asyncio.sleep(0)
    assert [n["title"] for n in notifier.sent] == ["A scheduled run is missing", "A scheduled run is missing"]


# ---------------------------------------------------------------- watches


@pytest.mark.parametrize("edit", ["remove", "disable"])
async def test_a_watch_tick_does_not_undo_an_edit_made_while_it_was_reading(tmp_path, monkeypatch, edit):
    """The tick loads the schedule, awaits the page, then writes its copy back."""
    h, fv = harness(tmp_path, "2026-11-04T09:00:00")
    watch_site(h)
    s, _ = h.scheduler.add(flow=fv.id, watch=WATCH)
    real_read = h.scheduler._read_watch

    async def read_while_the_owner_edits(sched, cfg):
        value = await real_read(sched, cfg)
        if edit == "remove":
            h.scheduler.remove(sched.id)
        else:
            h.scheduler.set_enabled(sched.id, False)
        return value

    monkeypatch.setattr(h.scheduler, "_read_watch", read_while_the_owner_edits)
    await h.scheduler.tick()
    stored = h.store.get_schedule(s.id)
    if edit == "remove":
        assert stored is None
    else:
        assert stored is not None and stored.enabled is False


async def test_a_flip_while_the_previous_run_is_pending_waits_for_it_instead_of_stacking(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T09:00:00")
    watch_site(h)
    s, _ = h.scheduler.add(flow=fv.id, watch=WATCH)  # debounce defaults to 0
    await h.scheduler.tick()  # baseline "Open"
    h.site.state["status"] = "Closed"
    h.clock.advance(60)
    await h.scheduler.tick()  # fires a run for the change
    h.site.state["status"] = "Open"
    h.clock.advance(60)
    await h.scheduler.tick()  # flips back while that run is still queued: no second run
    assert [r.trigger["value"] for r in runs(h, s)] == ["Closed"]
    await h.runner.run_until_idle()
    h.clock.advance(60)
    await h.scheduler.tick()  # the owner's run finished, so the pending change now fires
    assert [r.trigger["value"] for r in runs(h, s)] == ["Closed", "Open"]


async def test_a_backward_clock_jump_does_not_stall_a_page_watch(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T09:00:00")
    watch_site(h)
    h.scheduler.add(flow=fv.id, watch=WATCH)
    await h.scheduler.tick()  # baseline; the next check is due in 60 seconds
    h.clock.set(at("2026-11-04T08:00:00"))  # NTP stepped the clock back an hour
    stats = await h.scheduler.tick()
    assert stats["watched"] == 1


# ---------------------------------------------------------------- webhooks


async def test_a_webhook_refuses_missing_stale_future_wrong_and_tampered_requests(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T09:00:00")
    s, extras = h.scheduler.add(flow=fv.id, webhook=True)
    token, secret = extras["url_path"].rsplit("/", 1)[-1], extras["secret"]
    body = b'{"order": 17}'
    now = int(h.clock.now().timestamp())
    ts = str(now)
    good = sign_webhook(secret, ts, body)
    refused = [
        (body, {"X-Agentos-Timestamp": ts}),                                             # no signature
        (body, {"X-Agentos-Signature": good}),                                           # no timestamp
        (body, {"X-Agentos-Timestamp": ts, "X-Agentos-Signature": sign_webhook("x" * 64, ts, body)}),
        (body, {"X-Agentos-Timestamp": str(now + 3600),
                "X-Agentos-Signature": sign_webhook(secret, str(now + 3600), body)}),    # too far in the future
        (b'{"order": 18}', {"X-Agentos-Timestamp": ts, "X-Agentos-Signature": good}),   # body changed
    ]
    for sent, headers in refused:
        status, _ = await h.scheduler.handle_webhook(token, sent, headers)
        assert status == 401, headers
    assert runs(h, s) == []
    status, _ = await h.scheduler.handle_webhook(token, body, {"X-Agentos-Timestamp": ts,
                                                              "X-Agentos-Signature": good})
    assert status == 202


async def test_a_signed_webhook_replayed_with_a_new_delivery_id_is_still_a_duplicate(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T09:00:00")
    s, extras = h.scheduler.add(flow=fv.id, webhook=True)
    token, secret = extras["url_path"].rsplit("/", 1)[-1], extras["secret"]
    body = b'{"order": 17}'
    ts = str(int(h.clock.now().timestamp()))
    sig = sign_webhook(secret, ts, body)
    first, _ = await h.scheduler.handle_webhook(token, body, {"X-Agentos-Timestamp": ts,
                                                             "X-Agentos-Signature": sig,
                                                             "X-Agentos-Delivery": "d-1"})
    h.clock.advance(60)
    replay, out = await h.scheduler.handle_webhook(token, body, {"X-Agentos-Timestamp": ts,
                                                                "X-Agentos-Signature": sig,
                                                                "X-Agentos-Delivery": "d-2"})
    assert first == 202
    assert replay == 200 and out["duplicate"], out
    assert len(runs(h, s)) == 1


async def test_a_nan_timestamp_is_refused(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T09:00:00")
    s, extras = h.scheduler.add(flow=fv.id, webhook=True)
    token, secret = extras["url_path"].rsplit("/", 1)[-1], extras["secret"]
    body = b'{"order": 17}'
    status, _ = await h.scheduler.handle_webhook(token, body, {"X-Agentos-Timestamp": "nan",
                                                              "X-Agentos-Signature": sign_webhook(secret, "nan", body)})
    assert status == 401
    assert runs(h, s) == []


async def test_a_non_ascii_signature_header_is_refused(tmp_path):
    h, fv = harness(tmp_path, "2026-11-04T09:00:00")
    _, extras = h.scheduler.add(flow=fv.id, webhook=True)
    token = extras["url_path"].rsplit("/", 1)[-1]
    ts = str(int(h.clock.now().timestamp()))
    status, _ = await h.scheduler.handle_webhook(token, b"{}", {"X-Agentos-Timestamp": ts,
                                                               "X-Agentos-Signature": "sha256=é"})
    assert status == 401


def test_leap_day_and_day_31_crons_skip_the_months_that_lack_them():
    leap = cron_slots("0 9 29 2 *", VILNIUS, at("2027-12-31T00:00:00"), at("2028-03-01T00:00:00"))
    assert leap == [at("2028-02-29T07:00:00")]  # 09:00 local in February 2028 is UTC+2
    thirty_one = cron_slots("0 9 31 * *", VILNIUS, at("2027-04-01T00:00:00"), at("2027-06-30T00:00:00"))
    assert thirty_one == [at("2027-05-31T06:00:00")]  # April has no 31st; May 31 is UTC+3


def test_next_slot_for_the_31st_skips_short_months(tmp_path):
    h, fv = harness(tmp_path, "2027-04-30T10:00:00")
    s, _ = h.scheduler.add(flow=fv.id, cron="0 9 31 * *")
    assert h.scheduler.next_slot(s) == at("2027-05-31T06:00:00")


class _FrozenLane:
    def __init__(self, lane):
        self.lane = lane

    def check_call(self, lane, *, tool):
        from openajar.core.errors import Frozen

        if lane == self.lane:
            raise Frozen(f"{lane} is frozen")


async def test_a_cron_fire_takes_the_freeze_and_the_fire_checks_an_interval_takes(tmp_path):
    from openajar.core.errors import PolicyDenied

    h = Harness(tmp_path, clock=FakeClock(at("2026-11-04T08:59:00")), services={"freeze": _FrozenLane("biz-a/social")})
    fv = h.save(read_flow())
    s, _ = h.scheduler.add(flow=fv.id, cron="0 9 * * *", timezone="UTC")
    h.clock.advance(120)
    await h.scheduler.tick()
    state = h.store.get_schedule(s.id).state
    assert runs(h, s) == [] and state["last_error"]["code"] == "frozen"

    h2 = Harness(tmp_path / "b", clock=FakeClock(at("2026-11-04T08:59:00")))
    fv2 = h2.save(read_flow())
    s2, _ = h2.scheduler.add(flow=fv2.id, cron="0 9 * * *", timezone="UTC")
    checked = []

    def refuse(schedule):
        checked.append(schedule.id)
        raise PolicyDenied("it is a write now")

    h2.scheduler.fire_checks.append(refuse)
    h2.clock.advance(120)
    await h2.scheduler.tick()
    assert checked == [s2.id] and runs(h2, s2) == []
    assert h2.store.get_schedule(s2.id).state["last_error"]["code"] == "denied"
