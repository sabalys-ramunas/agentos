"""A lane's posting pace in the runner (AUD-13): a commit run outside the lane's waking hours or
inside its gap moves to the next allowed time (with jitter) instead of failing, the move is on the
run, and an agent's flow_run learns when the lane may post."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_policy_support import make_service

from agentos.core.models import Effect, RunStatus
from agentos.policy.meters import MeterPolicy
from agentos.policy.pace import LanePaced
from agentos.policy.routing import PolicyConfig, RoutingTable, Rule
from agentos.runner.fakes import FakeClock, Harness, post_flow
from agentos.runner.tools import flow_run

NY = ZoneInfo("America/New_York")
PARAMS = {"caption": "hi", "image": "x.png"}
FLOW = "biz-a.social.post"
LANE = "biz-a/social"


def ny(d: int, h: int, mi: int = 0) -> datetime:
    return datetime(2026, 11, d, h, mi, tzinfo=NY).astimezone(UTC)


@pytest.fixture
def env(tmp_path):
    clock = FakeClock(ny(5, 3))
    cfg = PolicyConfig(routing=RoutingTable(rules=[Rule(effect="*", then="auto")]),
                       meters=MeterPolicy(timezone="Europe/Vilnius"))
    svc, _ = make_service(tmp_path / "policy", clock=lambda: clock.now().timestamp(), config=cfg)
    h = Harness(tmp_path / "runner", clock=clock, services={"policy": svc, "approvals": svc.approvals,
                                                            "traces": svc.traces, "notifier": svc.notifier})
    svc.runtime = h.runtime
    h.save(post_flow())
    svc.set_lane_pace(LANE, {"active_hours": ["08:00-22:00"], "min_gap_minutes": 45,
                             "timezone": "America/New_York"})
    yield svc, h, clock
    svc.close()


def agent(svc):
    _, tok = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE)
    p, g = svc.resolve(tok)
    return svc.context(g, principal=p, tool="flow_run", token=tok)


async def test_a_scheduled_slot_at_night_moves_to_the_lanes_morning_and_says_so(env):
    svc, h, clock = env
    run = h.submit(FLOW, PARAMS, authorized=False)  # what a schedule's slot submits
    await h.runner.run_until_idle()
    run = h.runner.get(run.id)
    assert run.status == RunStatus.QUEUED and h.site.state["posts"] == []
    assert run.error["code"] == "deferred" and run.error["control"] == "lane_pace"
    paced = run.error["paced"]
    assert paced["rule"] == "active_hours" and paced["timezone"] == "America/New_York"
    assert paced["timezone_source"] == "lane" and paced["next_at"] == "2026-11-05T13:00:00Z"
    moved = run.not_before - ny(5, 8)
    assert timedelta(seconds=30) <= moved <= timedelta(seconds=240)  # a little after, never on the minute
    assert "may next post at 08:00 today lane time (America/New_York), 2026-11-05 13:00 UTC" in run.error["message"]
    assert run.held == 1  # a hold: never counts toward giving up
    clock.set(run.not_before)
    await h.runner.run_until_idle()
    assert len(h.site.state["posts"]) == 1
    assert svc.lane_pace(LANE)["posts_today"] == 1


async def test_a_second_slot_inside_the_gap_waits_for_the_gap(env):
    _svc, h, clock = env
    clock.set(ny(5, 9))
    await h.run(FLOW, PARAMS, authorized=False)
    clock.set(ny(5, 9, 10))
    run = h.submit(FLOW, {"caption": "two", "image": "x.png"}, authorized=False)
    await h.runner.run_until_idle()
    run = h.runner.get(run.id)
    assert run.error["paced"]["rule"] == "min_gap" and run.not_before > ny(5, 9, 45)


async def test_an_agents_flow_run_learns_when_the_lane_may_post_and_can_queue_for_it(env):
    svc, h, clock = env
    with pytest.raises(LanePaced) as e:
        await flow_run(agent(svc), name=FLOW, params=PARAMS, wait_seconds=0)
    assert e.value.hit["next_at"] == "2026-11-05T13:00:00Z"
    assert "it may next post at 08:00 today lane time" in e.value.message
    out = await flow_run(agent(svc), name=FLOW, params=PARAMS, wait_seconds=0,
                         not_before=ny(5, 9).isoformat())
    assert out["status"] == "queued"
    clock.set(ny(5, 9))
    await h.runner.run_until_idle()
    assert len(h.site.state["posts"]) == 1


async def test_a_run_that_holds_its_row_and_waited_into_the_night_waits_for_the_morning(env):
    svc, h, clock = env
    clock.set(ny(5, 21, 50))
    out = await flow_run(agent(svc), name=FLOW, params=PARAMS, wait_seconds=0)  # authorized, on the ledger at 21:50
    run = h.runner.get(out["id"] if "id" in out else out["run_id"])
    assert run.pace_id is not None and svc.lane_pace(LANE)["posts_today"] == 1
    clock.set(ny(5, 22, 30))  # the lane was busy until now
    await h.runner.run_until_idle()
    run = h.runner.get(run.id)
    assert run.error["control"] == "lane_pace" and run.error["paced"]["rule"] == "active_hours"
    assert run.not_before > ny(6, 8) and h.site.state["posts"] == []
    clock.set(run.not_before)
    await h.runner.run_until_idle()
    assert len(h.site.state["posts"]) == 1
    assert svc.lane_pace(LANE)["posts_today"] == 0  # a new lane day; the 21:50 row is yesterday's, and only one


async def test_a_run_the_owner_starts_is_paced_and_recorded_when_it_goes(env):
    svc, h, clock = env
    clock.set(ny(5, 9))
    await h.run(FLOW, PARAMS, authorized=False)  # 09:00 on the ledger
    clock.set(ny(5, 9, 10))
    run = h.submit(FLOW, {"caption": "two", "image": "x.png"}, authorized=True)  # `agentos flows run`: no row
    await h.runner.run_until_idle()
    run = h.runner.get(run.id)
    assert run.error["paced"]["rule"] == "min_gap" and run.not_before > ny(5, 9, 45)
    assert run.pace_id is None and len(h.site.state["posts"]) == 1
    clock.set(run.not_before)
    await h.runner.run_until_idle()
    run = h.runner.get(run.id)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED and run.pace_id is not None
    assert svc.lane_pace(LANE)["posts_today"] == 2


async def test_a_human_taking_the_lane_back_does_not_wake_a_paced_run(env):
    _svc, h, _clock = env
    run = h.submit(FLOW, PARAMS, authorized=False)
    await h.runner.run_until_idle()
    before = h.runner.get(run.id).not_before

    class Control:
        def __init__(self, state):
            self.state = state

    h.runner.on_lane_control(LANE, Control("human"), Control("agent"))
    assert h.runner.get(run.id).not_before == before


async def test_a_schedule_shows_where_its_next_slot_moves(env):
    _svc, h, _clock = env
    s, _ = h.scheduler.add(flow=FLOW, params=PARAMS, cron={"cron": "0 2 * * *", "timezone": "America/New_York"})
    out = h.scheduler.describe(s)
    assert out["next_run"].startswith("2026-11-06T07:00")
    assert out["paced"]["rule"] == "active_hours" and out["paced"]["moved_to"] == "2026-11-06T13:00:00Z"


@pytest.fixture
def asking(tmp_path):
    clock = FakeClock(ny(5, 8))
    cfg = PolicyConfig(routing=RoutingTable(rules=[Rule(effect="commit", then="approval"),
                                                   Rule(effect="*", then="auto")]),
                       meters=MeterPolicy(timezone="Europe/Vilnius"))
    svc, _ = make_service(tmp_path / "policy", clock=lambda: clock.now().timestamp(), config=cfg)
    h = Harness(tmp_path / "runner", clock=clock, services={"policy": svc, "approvals": svc.approvals,
                                                            "traces": svc.traces, "notifier": svc.notifier})
    svc.runtime = h.runtime
    h.save(post_flow())
    svc.set_lane_pace(LANE, {"active_hours": ["08:00-22:00"], "min_gap_minutes": 45, "commits_per_day": 2,
                             "timezone": "America/New_York"})
    yield svc, h, clock
    svc.close()


async def test_a_run_approved_after_it_parked_is_paced_and_counted_when_it_goes(asking):
    svc, h, clock = asking
    ctx = agent(svc)
    svc.reserve(ctx.grant, effect=Effect.COMMIT, lane_id=LANE, action=False)  # 08:00: a publish
    clock.set(ny(5, 9))
    run = h.submit(FLOW, PARAMS, authorized=False)
    await h.runner.run_until_idle()
    assert h.runner.get(run.id).status == RunStatus.WAITING_APPROVAL
    assert svc.lane_pace(LANE)["posts_today"] == 1  # a run waiting for a person holds no row
    clock.set(ny(5, 9, 20))
    svc.reserve(ctx.grant, effect=Effect.COMMIT, lane_id=LANE, action=False)  # 09:20: the day's second
    clock.set(ny(5, 9, 30))
    svc.approvals.decide(h.runner.get(run.id).approval_id, "approve", by="owner")
    await h.runner.poll_approvals()
    await h.runner.run_until_idle()
    run = h.runner.get(run.id)
    assert h.site.state["posts"] == [] and run.status == RunStatus.QUEUED  # approved, but the day is used up
    assert run.error["paced"]["rule"] == "commits_per_day" and run.not_before > ny(6, 8)
    assert svc.lane_pace(LANE)["posts_today"] == 2
    clock.set(run.not_before)
    await h.runner.run_until_idle()
    assert h.runner.get(run.id).status == RunStatus.SUCCEEDED_VERIFIED and len(h.site.state["posts"]) == 1
    assert svc.lane_pace(LANE)["posts_today"] == 1  # the new day's first, on the ledger


async def test_a_run_queued_for_a_slot_holds_off_a_publish_just_before_it(env):
    svc, h, clock = env
    clock.set(ny(5, 9))
    out = await flow_run(agent(svc), name=FLOW, params=PARAMS, wait_seconds=0, not_before=ny(5, 10).isoformat())
    assert out["status"] == "queued"
    clock.set(ny(5, 9, 50))
    with pytest.raises(LanePaced) as e:
        svc.reserve(agent(svc).grant, effect=Effect.COMMIT, lane_id=LANE, action=False)
    assert "has a post queued for 10:00 today lane time" in e.value.message
    clock.set(ny(5, 10))
    await h.runner.run_until_idle()
    assert len(h.site.state["posts"]) == 1 and svc.lane_pace(LANE)["posts_today"] == 1


async def test_the_owners_own_run_at_night_waits_for_the_lanes_morning(env):
    _svc, h, clock = env
    clock.set(ny(5, 23))
    run = h.submit(FLOW, PARAMS, authorized=True)  # `agentos flows run` or Run in the panel: a run, so paced
    await h.runner.run_until_idle()
    run = h.runner.get(run.id)
    assert h.site.state["posts"] == [] and run.error["paced"]["rule"] == "active_hours"
    assert run.not_before > ny(6, 8)


async def test_a_connector_schedule_shows_its_surfaces_lane_pace(env, monkeypatch):
    from types import SimpleNamespace

    svc, h, _clock = env
    monkeypatch.setattr(svc, "pace_lane", lambda target: LANE if target == "biz-a/page" else target)
    s = SimpleNamespace(surface="biz-a/page", lane=None, flow_id=FLOW, flow_version=None)
    paced = h.scheduler.paced_slot(s, ny(6, 2))  # 02:00 lane time: the surface's lane is asleep
    assert paced is not None and paced["rule"] == "active_hours" and paced["moved_to"] == "2026-11-06T13:00:00Z"
