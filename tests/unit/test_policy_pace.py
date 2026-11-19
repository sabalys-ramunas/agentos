"""Posting pace and waking hours per lane (AUD-13): the window math (DST, overnight windows,
weekdays), whose clock a lane wakes on, and the policy refusing a publish before approval routing."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_policy_support import Clock, make_service

from agentos.core.errors import ApprovalPending
from agentos.core.models import Effect
from agentos.policy import pace as pacing
from agentos.policy.meters import MeterPolicy
from agentos.policy.pace import LanePaced, Pace, Window
from agentos.policy.routing import PolicyConfig, RoutingTable, Rule

NODE_TZ = "Europe/Vilnius"
NY = ZoneInfo("America/New_York")
VILNIUS = ZoneInfo(NODE_TZ)
EM_DASH = "—"


def at(y, mo, d, h=12, mi=0, tz=NY) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


def local(t: float, tz=NY) -> str:
    return datetime.fromtimestamp(t, tz).strftime("%a %Y-%m-%d %H:%M")


# ------------------------------------------------------------------ windows

def test_windows_parse_days_overnight_and_midnight_ends():
    w = Window.parse("mon-fri 08:00-22:00")
    assert w.days == frozenset(range(5)) and (w.start, w.end) == (480, 1320) and not w.overnight
    assert Window.parse("22:00-02:00").overnight
    assert Window.parse("sat,sun 10:00-24:00").end == 1440
    assert Window.parse("fri-mon 9:00-17:00").days == frozenset({4, 5, 6, 0})
    assert Window.parse("Mon-Fri  08:00 - 22:00").text() == "mon-fri 08:00-22:00"
    for bad in ("8-22", "mon-xyz 08:00-22:00", "08:00-08:00", "25:00-26:00", "08:61-09:00", "24:00-02:00"):
        with pytest.raises(ValueError):
            Window.parse(bad)
    assert Pace(active_hours="08:00-12:00; 14:00-22:00").active_hours == ["08:00-12:00", "14:00-22:00"]
    typed = Pace(active_hours="mon-fri 09:00-18:00; sat,sun 10:00-14:00")  # what the panel sends, as typed
    assert typed.active_hours == ["mon-fri 09:00-18:00", "sat,sun 10:00-14:00"]


def test_an_ordinary_day_window_and_the_next_opening():
    ws = [Window.parse("08:00-22:00")]
    assert pacing.is_open(ws, NY, at(2026, 11, 5, 8, 0))
    assert not pacing.is_open(ws, NY, at(2026, 11, 5, 22, 0))  # the end is not inside
    assert pacing.next_open(ws, NY, at(2026, 11, 5, 3, 0)) == at(2026, 11, 5, 8, 0)
    assert pacing.next_open(ws, NY, at(2026, 11, 5, 23, 0)) == at(2026, 11, 6, 8, 0)
    assert pacing.next_open([], NY, 5.0) == 5.0 and pacing.is_open([], NY, 5.0)


def test_an_overnight_window_belongs_to_the_day_it_starts():
    ws = [Window.parse("fri 22:00-02:00")]  # Friday 22:00 to Saturday 02:00
    assert pacing.is_open(ws, NY, at(2026, 11, 6, 23, 0))   # Friday night
    assert pacing.is_open(ws, NY, at(2026, 11, 7, 1, 30))   # Saturday small hours
    assert not pacing.is_open(ws, NY, at(2026, 11, 7, 2, 0))
    assert not pacing.is_open(ws, NY, at(2026, 11, 5, 23, 0))  # Thursday night: not a Friday start
    assert not pacing.is_open(ws, NY, at(2026, 11, 6, 1, 0))   # Friday small hours: Thursday's window
    assert local(pacing.next_open(ws, NY, at(2026, 11, 7, 3, 0))) == "Fri 2026-11-13 22:00"
    every = [Window.parse("22:00-02:00")]
    assert pacing.is_open(every, NY, at(2026, 11, 6, 1, 0)) and not pacing.is_open(every, NY, at(2026, 11, 6, 12))


def test_weekday_rules_pick_the_window_of_each_day():
    ws = [Window.parse("mon-fri 08:00-18:00"), Window.parse("sat,sun 11:00-23:00")]
    assert not pacing.is_open(ws, NY, at(2026, 11, 7, 9, 0))   # Saturday 09:00
    assert local(pacing.next_open(ws, NY, at(2026, 11, 7, 9, 0))) == "Sat 2026-11-07 11:00"
    assert local(pacing.next_open(ws, NY, at(2026, 11, 8, 23, 30))) == "Mon 2026-11-09 08:00"
    assert pacing.is_open(ws, NY, at(2026, 11, 9, 17, 59))


def test_dst_windows_stay_on_the_local_clock():
    ws = [Window.parse("08:00-22:00")]
    # New York falls back on Sun 1 Nov 2026 and springs forward on Sun 8 Mar 2026
    for day in ((2026, 10, 31), (2026, 11, 1), (2026, 3, 7), (2026, 3, 8)):
        nxt = pacing.next_open(ws, NY, at(*day, 3, 0))
        assert datetime.fromtimestamp(nxt, NY).strftime("%H:%M") == "08:00"
    fall = pacing.intervals(ws, NY, at(2026, 11, 1, 12), days=1)
    spring = pacing.intervals(ws, NY, at(2026, 3, 8, 12), days=1)
    assert all(b - a == 14 * 3600 for a, b in fall + spring)  # 14 local hours either side of a change
    # a window across the change: 00:00-04:00 is 5 hours when the clocks fall back, 3 when they spring
    night = [Window.parse("00:00-04:00")]
    lengths = {d: [b - a for a, b in pacing.intervals(night, NY, at(2026, *d, 12), days=0)
                   if datetime.fromtimestamp(a, NY).day == d[1]] for d in ((11, 1), (3, 8))}
    assert lengths == {(11, 1): [5 * 3600], (3, 8): [3 * 3600]}
    # a start the clocks skip (02:30 on 8 March) opens at the first instant after the jump
    skipped = [Window.parse("sun 02:30-05:00")]
    first = pacing.next_open(skipped, NY, at(2026, 3, 8, 0, 0))
    assert datetime.fromtimestamp(first, NY).strftime("%H:%M") == "03:30"


def test_a_day_count_resets_at_lane_local_midnight_over_a_dst_change():
    assert local(pacing.next_midnight(NY, at(2026, 11, 1, 12))) == "Mon 2026-11-02 00:00"
    assert pacing.next_midnight(NY, at(2026, 11, 1, 12)) - pacing.local_instant(
        datetime(2026, 11, 1).date(), 0, NY) == 25 * 3600


def test_the_verdict_walks_every_rule_to_the_next_allowed_time():
    pace = Pace(commits_per_day=2, min_gap_minutes=60, active_hours=["08:00-22:00"])
    posts = [at(2026, 11, 5, 9, 0)]

    def v(t):
        return pacing.verdict(pace, NY, t, last_at=lambda x: max((p for p in posts if p <= x), default=None),
                              count_day=lambda a, b: sum(a <= p < b for p in posts))

    assert v(at(2026, 11, 5, 9, 30)).rule == "min_gap" and v(at(2026, 11, 5, 9, 30)).next_ts == at(2026, 11, 5, 10)
    posts.append(at(2026, 11, 5, 21, 30))
    over = v(at(2026, 11, 5, 21, 45))  # two today: tomorrow, and then the waking hours
    assert over.rule == "commits_per_day" and over.next_ts == at(2026, 11, 6, 8, 0)
    assert v(at(2026, 11, 6, 3, 0)).rule == "active_hours"
    assert v(at(2026, 11, 6, 8, 0)).rule is None


def test_the_gap_holds_before_a_queued_post_as_well_as_after_one():
    pace = Pace(min_gap_minutes=45)
    posts = [at(2026, 11, 5, 10, 0)]  # a run queued for 10:00

    def v(t):
        return pacing.verdict(pace, NY, t, last_at=lambda x: max((p for p in posts if p <= x), default=None),
                              next_at=lambda x: min((p for p in posts if p > x), default=None),
                              count_day=lambda a, b: sum(a <= p < b for p in posts))

    before = v(at(2026, 11, 5, 9, 50))
    assert before.rule == "min_gap" and before.next_ts == at(2026, 11, 5, 10, 45)
    assert before.near_at == at(2026, 11, 5, 10, 0)
    assert v(at(2026, 11, 5, 9, 15)).rule is None  # exactly the gap before it
    assert v(at(2026, 11, 5, 10, 5)).rule == "min_gap"


def test_an_overnight_window_counts_toward_the_day_it_starts():
    pace = Pace(commits_per_day=1, active_hours=["22:00-02:00"])
    posts = [at(2026, 11, 5, 22, 30)]

    def v(t):
        return pacing.verdict(pace, NY, t, last_at=lambda x: max((p for p in posts if p <= x), default=None),
                              count_day=lambda a, b: sum(a <= p < b for p in posts))

    after_midnight = v(at(2026, 11, 6, 0, 5))
    assert after_midnight.rule == "commits_per_day"
    assert after_midnight.next_ts == at(2026, 11, 6, 22, 0)  # the next window, the next lane day
    assert v(at(2026, 11, 6, 22, 0)).rule is None
    ws = pace.windows()
    assert pacing.lane_day(ws, NY, at(2026, 11, 6, 1, 0)) == (at(2026, 11, 5, 2, 0), at(2026, 11, 6, 2, 0))
    assert pacing.lane_day([], NY, at(2026, 11, 6, 1, 0)) == (at(2026, 11, 6, 0, 0), at(2026, 11, 7, 0, 0))


def test_no_posts_a_day_is_refused_in_favour_of_a_pause():
    with pytest.raises(ValueError, match="pause the lane"):
        Pace(commits_per_day=0)
    assert Pace(commits_per_day=1).commits_per_day == 1


def test_jitter_stays_inside_a_short_window():
    opens = at(2026, 11, 5, 9, 0)
    for seed in ("a", "b", "c", "d", "e"):
        moved = pacing.jitter(seed, opens, until=opens + 120)  # a two-minute window
        assert opens <= moved < opens + 120
        free = pacing.jitter(seed, opens)
        assert opens + 30 <= free <= opens + 240
    ws = [Window.parse("09:00-09:02")]
    assert pacing.open_until(ws, NY, opens) == opens + 120


def test_the_preset_is_a_new_accounts_pace():
    assert pacing.PRESET.given() == {"commits_per_day": 3, "min_gap_minutes": 45, "active_hours": ["08:00-22:00"]}
    assert not Pace(timezone="America/New_York").paces() and Pace(min_gap_minutes=5).paces()
    with pytest.raises(ValueError):
        Pace(timezone="Mars/Olympus")


# ------------------------------------------------------------------ the policy

def auto_config(**kw) -> PolicyConfig:
    return PolicyConfig(routing=RoutingTable(rules=[Rule(effect="*", then="auto")]),
                        meters=MeterPolicy(timezone=NODE_TZ), **kw)


@pytest.fixture
def env(tmp_path):
    clock = Clock(at(2026, 11, 5, 12))
    svc, rt = make_service(tmp_path, clock=clock, config=auto_config())
    yield svc, clock, rt
    svc.close()


def ctx_for(svc, **kw):
    g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE, **kw)
    p, g = svc.resolve(tok)
    return svc.context(g, principal=p, tool="t", token=tok)


def pin(svc, lane: str, tz: str) -> None:
    path = svc.node_config.fingerprint_path(lane)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"timezone": tz, "navigator.platform": "Win32"}))


async def paced(coro) -> LanePaced:
    with pytest.raises(LanePaced) as e:
        await coro
    assert e.value.code == "lane_paced" and EM_DASH not in e.value.message
    return e.value


async def test_a_lane_with_no_pace_is_never_paced(env):
    svc, clock, _ = env
    ctx = ctx_for(svc)
    clock.t = at(2026, 11, 5, 3, 0)
    for _ in range(5):
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    state = svc.lane_pace("biz-a/ig")
    assert state["paced"] is False and state["may_post_now"] and state["posts_today"] == 5


async def test_the_pinned_timezone_wakes_the_lane_on_its_proxys_time(env):
    svc, clock, _ = env
    pin(svc, "biz-a/ig", "America/New_York")
    svc.set_lane_pace("biz-a/ig", {"active_hours": ["08:00-22:00"]})
    ctx = ctx_for(svc)
    clock.t = at(2026, 11, 5, 7, 0)  # 14:00 in Vilnius, 07:00 on the lane's clock
    e = await paced(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    assert e.hit["rule"] == "active_hours" and e.hit["timezone_source"] == "pin"
    assert e.hit["next_at"] == "2026-11-05T13:00:00Z" and e.hit["next_at_local"] == "2026-11-05T08:00-05:00"
    assert e.message == ("Browser biz-a/ig is outside its posting hours (08:00-22:00 its time, America/New_York, from "
                         "its proxy's region (the pinned fingerprint)), so it may not publish now: it may next post "
                         "at 08:00 today its time (America/New_York), 2026-11-05 13:00 UTC, in 1 h.")
    await ctx.authorize(Effect.READ, lane_id="biz-a/ig")  # reads and drafts go on
    await ctx.authorize(Effect.DRAFT, lane_id="biz-a/ig")
    clock.t = at(2026, 11, 5, 8, 0)
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")


async def test_zone_sources_lane_then_pin_then_workspace_then_node(env):
    svc, _, _rt = env
    assert svc.pace_zone("biz-a/x").public()["source"] == "node" and svc.pace_zone("biz-a/x").name == NODE_TZ
    svc.set_workspace_zone("biz-a", "Asia/Tokyo")
    assert (svc.pace_zone("biz-a/x").name, svc.pace_zone("biz-a/x").source) == ("Asia/Tokyo", "workspace")
    pin(svc, "biz-a/x", "America/Chicago")
    assert (svc.pace_zone("biz-a/x").name, svc.pace_zone("biz-a/x").source) == ("America/Chicago", "pin")
    svc.set_lane_pace("biz-a/x", {"timezone": "Europe/London"})
    assert (svc.pace_zone("biz-a/x").name, svc.pace_zone("biz-a/x").source) == ("Europe/London", "lane")
    assert svc.lane_pace("biz-a/x")["paced"] is False  # a zone alone paces nothing


async def test_when_the_exit_moved_the_pin_still_decides_and_says_so(env):
    svc, clock, rt = env
    pin(svc, "biz-a/ig", "America/New_York")
    check = SimpleNamespace(id="location", data={"exit_timezone": "Europe/Berlin"})
    rt.register("lane_check", SimpleNamespace(last={"biz-a/ig": SimpleNamespace(checks=[check])}))
    svc.set_lane_pace("biz-a/ig", {"active_hours": ["08:00-22:00"]})
    zone = svc.lane_pace("biz-a/ig")["zone"]
    assert zone["timezone"] == "America/New_York" and zone["exit_timezone"] == "Europe/Berlin"
    assert "not the pinned America/New_York" in zone["note"]
    clock.t = at(2026, 11, 5, 3, 0)  # 09:00 in Berlin, 03:00 pinned
    e = await paced(ctx_for(svc).authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    assert "The pinned zone is used, because that is what the site sees" in e.message


async def test_min_gap_and_the_daily_count_say_when_the_lane_may_post_again(env):
    svc, clock, _ = env
    pin(svc, "biz-a/ig", "America/New_York")
    svc.set_lane_pace("biz-a/ig", pacing.PRESET)
    ctx = ctx_for(svc)
    clock.t = at(2026, 11, 5, 9, 0)
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    clock.t = at(2026, 11, 5, 9, 20)
    e = await paced(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    assert e.hit["rule"] == "min_gap" and e.hit["next_at_local"] == "2026-11-05T09:45-05:00"
    assert "posted 20 min ago and keeps at least 45 minutes between posts" in e.message
    for hm in ((9, 45), (11, 0)):
        clock.t = at(2026, 11, 5, *hm)
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    clock.t = at(2026, 11, 5, 15, 0)
    e = await paced(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    assert e.hit["rule"] == "commits_per_day" and e.hit["next_at_local"] == "2026-11-06T08:00-05:00"
    state = svc.lane_pace("biz-a/ig")
    assert state["posts_today"] == 3 and state["blocked_by"] == "commits_per_day" and not state["may_post_now"]
    assert state["next_allowed_at"] == "2026-11-06T13:00:00Z" and state["explain"] == e.message


async def test_an_approval_cannot_get_past_the_pace(tmp_path):
    clock = Clock(at(2026, 11, 5, 12))
    svc, _ = make_service(tmp_path, clock=clock, config=PolicyConfig(meters=MeterPolicy(timezone=NODE_TZ)))
    pin(svc, "biz-a/ig", "America/New_York")
    svc.set_lane_pace("biz-a/ig", {"active_hours": ["08:00-22:00"]})
    svc.ask_first.set("lane", "biz-a/ig", True, by="owner", person=True)
    ctx = ctx_for(svc)
    clock.t = at(2026, 11, 5, 21, 50)
    with pytest.raises(ApprovalPending) as pending:
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig", content={"text": "hi"})
    assert svc.lane_pace("biz-a/ig")["posts_today"] == 0  # a commit waiting for a person is not on the ledger
    svc.decide(pending.value.approval_id, "approve")
    clock.t = at(2026, 11, 5, 22, 10)  # approved, but the lane is asleep now
    e = await paced(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig", content={"text": "hi"}))
    assert e.hit["rule"] == "active_hours"
    clock.t = at(2026, 11, 6, 8, 5)  # the approval was never used: it still covers the publish
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig", content={"text": "hi"})
    svc.close()


async def test_the_owner_in_person_is_never_paced(env):
    svc, clock, _ = env
    svc.set_lane_pace("biz-a/ig", {"active_hours": ["08:00-09:00"]})
    clock.t = at(2026, 11, 5, 23, 0)
    await svc.owner_context(tool="panel").authorize(Effect.COMMIT, lane_id="biz-a/ig")


async def test_a_member_in_person_is_paced_like_any_grant(env):
    svc, clock, _ = env
    svc.set_lane_pace("biz-a/ig", {"active_hours": ["08:00-22:00"], "timezone": "America/New_York"})
    op = svc.issue_member_grant("m_op", "biz-a", "operator")
    ctx = svc.member_context(op, tool="panel")
    assert ctx.in_person
    clock.t = at(2026, 11, 5, 3, 0)
    e = await paced(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig", summary="post", content="x"))
    assert e.hit["rule"] == "active_hours"
    await svc.owner_context(tool="panel").authorize(Effect.COMMIT, lane_id="biz-a/ig")  # the owner is not


async def test_a_vault_or_memory_change_on_a_lane_is_not_a_post(env):
    svc, clock, _ = env
    svc.set_lane_pace("biz-a/ig", {"active_hours": ["08:00-22:00"], "timezone": "America/New_York"})
    op = svc.issue_member_grant("m_op", "biz-a", "operator")
    clock.t = at(2026, 11, 5, 3, 0)  # the lane is asleep
    await svc.member_context(op, tool="panel.vault.delete").authorize(Effect.COMMIT, lane_id="biz-a/ig")
    await svc.member_context(op, tool="memory_forget").authorize(Effect.COMMIT, lane_id="biz-a/ig")
    assert svc.lane_pace("biz-a/ig")["posts_today"] == 0  # and neither is a row
    e = await paced(svc.member_context(op, tool="panel").authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    assert e.hit["rule"] == "active_hours"  # anything else on the lane still is


async def test_a_commit_queued_for_a_slot_is_read_at_that_slot(env):
    svc, clock, _ = env
    pin(svc, "biz-a/ig", "America/New_York")
    svc.set_lane_pace("biz-a/ig", {"active_hours": ["08:00-22:00"], "min_gap_minutes": 30})
    ctx = ctx_for(svc)
    clock.t = at(2026, 11, 5, 3, 0)
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig", at=at(2026, 11, 5, 9, 0))
    await paced(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig", at=at(2026, 11, 5, 23, 0)))
    clock.t = at(2026, 11, 5, 9, 10)
    e = await paced(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    assert e.hit["rule"] == "min_gap"


async def test_a_publish_is_held_by_a_run_queued_after_it_and_slots_keep_the_gap_in_any_order(env):
    svc, clock, _ = env
    svc.set_lane_pace("biz-a/ig", {"min_gap_minutes": 45, "timezone": "America/New_York"})
    ctx = ctx_for(svc)
    clock.t = at(2026, 11, 5, 9, 0)
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig", at=at(2026, 11, 5, 10, 0))  # a run queued for 10:00
    clock.t = at(2026, 11, 5, 9, 50)
    e = await paced(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    assert e.hit["rule"] == "min_gap" and e.hit["next_at_local"] == "2026-11-05T10:45-05:00"
    assert "has a post queued for 10:00 today its time" in e.message
    clock.t = at(2026, 11, 5, 8, 0)
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig", at=at(2026, 11, 5, 12, 5))  # the later slot first
    await paced(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig", at=at(2026, 11, 5, 12, 0)))
    res = svc.reserve(ctx.grant, effect=Effect.COMMIT, lane_id="biz-a/ig", action=False, at=at(2026, 11, 5, 14))
    svc.pace_check("biz-a/ig", at(2026, 11, 5, 14), exclude=tuple(res.pace_ids))  # its own row never holds it


async def test_a_publish_the_commit_gate_holds_is_refused_and_an_unheld_one_is_counted(env):
    svc, _clock, _ = env
    svc.set_lane_pace("biz-a/ig", {"min_gap_minutes": 30})
    ctx = ctx_for(svc)
    assert ctx.charge_commit("biz-a/ig", sent=False) is None   # the guard's charge for a publish
    assert ctx.commits_capped("biz-a/ig")                      # within the gap: the act runs strict
    why = ctx.charge_commit("biz-a/ig", sent=False)
    assert why is not None and "keeps at least 30 minutes between posts" in why
    assert svc.lane_pace("biz-a/ig")["posts_today"] == 1
    assert ctx.charge_commit("biz-a/ig", sent=True) is not None  # it left anyway: counted, it happened
    assert svc.lane_pace("biz-a/ig")["posts_today"] == 2


async def test_setting_and_removing_a_pace_is_traced_and_the_workspace_zone_is_checked(env):
    svc, _, _ = env
    out = svc.set_lane_pace("biz-a/ig", {"commits_per_day": 1})
    assert out["pace"] == {"commits_per_day": 1} and out["set"]["by"] == "owner"
    assert svc.set_lane_pace("biz-a/ig", None)["pace"] is None
    with pytest.raises(ValueError):
        svc.set_workspace_zone("biz-a", "Nowhere/Here")
    assert svc.set_workspace_zone("biz-a", None) == {"workspace": "biz-a", "timezone": None}


async def test_a_persons_own_publish_holds_an_agents_post_for_the_gap(env):
    svc, clock, _ = env
    svc.set_lane_pace("biz-a/ig", {"min_gap_minutes": 45})
    listeners: list = []
    assert svc.attach_lanes(SimpleNamespace(get=lambda name: SimpleNamespace(add_commit_listener=listeners.append)))
    [record] = listeners
    record("biz-a/ig")  # a person posted by hand (the egress guard's on_human_commit, via the lane manager)
    clock.t += 10 * 60
    ctx = ctx_for(svc)
    e = await paced(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    assert e.hit["rule"] == "min_gap" and "posted 10 min ago" in e.message
    assert ctx.charge_commit("biz-a/ig", sent=False) is not None and ctx.commit_code == "lane_paced"


async def test_an_unreadable_pin_keeps_the_last_zone_and_with_none_refuses_a_paced_publish(env):
    svc, clock, _ = env
    svc.set_lane_pace("biz-a/ig", {"active_hours": ["08:00-22:00"]})
    svc.set_lane_pace("biz-a/fb", {"active_hours": ["08:00-22:00"]})
    pin(svc, "biz-a/ig", "America/New_York")
    clock.t = at(2026, 11, 5, 9, 0)  # 09:00 New York, 16:00 Vilnius
    assert svc.lane_pace("biz-a/ig")["zone"]["timezone"] == "America/New_York"
    svc.node_config.fingerprint_path("biz-a/ig").write_text("{not json")
    clock.t = at(2026, 11, 5, 7, 0)  # 07:00 New York, 14:00 on the node: the kept zone still says asleep
    e = await paced(ctx_for(svc).authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    assert e.hit["rule"] == "active_hours" and e.hit["timezone"] == "America/New_York"
    broken = svc.node_config.fingerprint_path("biz-a/fb")
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not json")  # no earlier read: refused, never read on the node's clock
    e = await paced(ctx_for(svc).authorize(Effect.COMMIT, lane_id="biz-a/fb"))
    assert e.hit["rule"] == "timezone" and "timezone could not be read" in e.message
    state = svc.lane_pace("biz-a/fb")
    assert state["zone"]["unreadable"] and not state["may_post_now"] and state["blocked_by"] == "timezone"
    svc.set_lane_pace("biz-a/fb", {"active_hours": ["08:00-22:00"], "timezone": "Europe/Vilnius"})
    await ctx_for(svc).authorize(Effect.COMMIT, lane_id="biz-a/fb")  # a zone set on the lane needs no pin


def test_a_surface_is_paced_on_the_lane_it_posts_through(env):
    svc, _, rt = env
    rt.register("surfaces", SimpleNamespace(find=lambda sid: SimpleNamespace(lane="personal/social")
                                            if sid == "biz-a/social-page" else None))
    assert svc.pace_lane("biz-a/social-page") == "personal/social" and svc.pace_lane("biz-a/ig") == "biz-a/ig"
