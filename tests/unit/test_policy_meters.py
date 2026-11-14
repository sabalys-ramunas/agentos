"""Meters: daily and hourly counters per grant and per lane, resets at the owner's midnight (DST
included), warnings once per threshold per day, and the cutoff's reason and sentence."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_policy_support import Clock, make_service

from agentos.core.errors import ApprovalPending, InvalidRequest
from agentos.core.models import Effect
from agentos.policy.errors import Denied
from agentos.policy.meters import LimitReached, MeterPolicy, Meters, Scope
from agentos.policy.routing import PolicyConfig, RoutingTable, Rule
from agentos.policy.store import DB
from agentos.policy.tools import grant_info

TZ = "Europe/Vilnius"
VILNIUS = ZoneInfo(TZ)
EM_DASH = "—"


def at(y, mo, d, h=12, mi=0, tz=VILNIUS) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


def auto_config(**meters) -> PolicyConfig:
    return PolicyConfig(routing=RoutingTable(rules=[Rule(effect="*", then="auto")]),
                        meters=MeterPolicy(timezone=TZ, **meters))


@pytest.fixture
def env(tmp_path):
    clock = Clock(at(2026, 11, 4, 12))
    svc, _rt = make_service(tmp_path, clock=clock, config=auto_config())
    events: list[dict] = []
    svc.bus.subscribe(lambda kind, payload: events.append(payload) if kind == "meter" else None)
    yield svc, clock, events
    svc.close()


def ctx_for(svc, budget=None, **kw):
    g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE,
                       budget=budget, **kw)
    p, g = svc.resolve(tok)
    return svc.context(g, principal=p, tool="t", token=tok)


async def limit_reached(coro) -> LimitReached:
    with pytest.raises(LimitReached) as e:
        await coro
    assert e.value.reason == "limit_reached"
    assert EM_DASH not in e.value.message
    return e.value


# ------------------------------------------------------------------ periods

def meters(tmp_path, tz: str) -> Meters:
    return Meters(DB(tmp_path / "m.db", "CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, "
                     "used INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);"),
                  cfg=MeterPolicy(timezone=tz), clock=lambda: 0.0)


def test_the_day_resets_at_local_midnight_and_dst_days_have_23_or_25_hours(tmp_path):
    m = meters(tmp_path, TZ)
    noon = at(2026, 11, 4, 12)
    assert m.day_reset(noon) == at(2026, 11, 5, 0)
    assert m.day_reset(at(2026, 11, 4, 23, 59)) == at(2026, 11, 5, 0)
    assert m.day(at(2026, 11, 4, 23, 59)) != m.day(at(2026, 11, 5, 0, 1))
    # spring forward (2026-03-29, 03:00 -> 04:00): the day has 23 hours; fall back (2026-10-25): 25
    spring = m.day_reset(at(2026, 3, 29, 12)) - m.day_start(datetime(2026, 3, 29).date())
    fall = m.day_reset(at(2026, 10, 25, 12)) - m.day_start(datetime(2026, 10, 25).date())
    assert (spring, fall) == (23 * 3600, 25 * 3600)
    # midnight stays midnight on the local clock either side of a change
    for t in (at(2026, 3, 29, 12), at(2026, 10, 25, 12)):
        assert datetime.fromtimestamp(m.day_reset(t), VILNIUS).strftime("%H:%M") == "00:00"


def test_where_the_clocks_jump_over_midnight_the_day_starts_after_the_jump(tmp_path):
    m = meters(tmp_path, "America/Santiago")
    zone = ZoneInfo("America/Santiago")
    day = datetime(2026, 9, 6).date()  # Chile moves 00:00 to 01:00 on the first Sunday of September
    start = m.day_start(day)
    assert m.day(start) == day and m.day(start - 1) == day - timedelta(days=1)
    assert datetime.fromtimestamp(start, zone).strftime("%H:%M") in ("00:00", "01:00")


def test_hours_are_clock_hours_in_utc(tmp_path):
    m = meters(tmp_path, TZ)
    t = at(2026, 11, 4, 12, 30)
    assert m.hour_reset(t) == at(2026, 11, 4, 13, 0)
    assert m.bucket("hour", t) != m.bucket("hour", m.hour_reset(t))


# ------------------------------------------------------------------ lane limits

async def test_a_lane_commit_limit_cuts_off_commits_with_a_plain_sentence_and_reads_go_on(env):
    svc, clock, _ = env
    svc.set_lane_limits("biz-a/ig", {"commits_per_day": 2}, by="owner")
    ctx = ctx_for(svc)
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    e = await limit_reached(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    assert e.hit["code"] == "lane.commits_per_day" and e.hit["value"] == 2 and e.hit["used"] == 2
    assert e.message == ("limit_reached: Lane biz-a/ig reached its limit of 2 commits a day (2 used today), so "
                         "posting, paying and irreversible steps are paused until it resets at 00:00 "
                         "(Europe/Vilnius), in 12 h.")
    await ctx.authorize(Effect.READ, lane_id="biz-a/ig")
    await ctx.authorize(Effect.DRAFT, lane_id="biz-a/ig")
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/other")  # another lane has its own meter
    state = svc.lane_meter("biz-a/ig")
    assert state["state"] == "cut_off" and state["limits_from"] == "owner"
    assert state["usage"]["actions"] == {"commit": 2, "read": 1, "draft": 1}
    assert state["explain"].startswith("Lane biz-a/ig reached its limit of 2 commits a day")
    clock.t = at(2026, 11, 5, 0, 0) + 1  # past the owner's midnight: a new day
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    assert svc.lane_meter("biz-a/ig")["usage"]["commits_today"] == 1


async def test_an_hourly_limit_resets_on_the_hour(env):
    svc, clock, _ = env
    svc.set_lane_limits("biz-a/ig", {"commits_per_hour": 1}, by="owner")
    ctx = ctx_for(svc)
    clock.t = at(2026, 11, 4, 12, 50)
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    e = await limit_reached(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    assert e.hit["period"] == "hour" and "1 commit an hour (1 used this hour)" in e.message
    assert "until it resets at 13:00 (Europe/Vilnius), in 10 min." in e.message
    clock.t = at(2026, 11, 4, 13, 0)
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")


async def test_policy_toml_patterns_set_lane_limits_and_the_owner_overrides_them(tmp_path):
    clock = Clock(at(2026, 11, 4, 12))
    svc, _ = make_service(tmp_path, clock=clock, config=auto_config(lanes={"biz-a/*": {"commits_per_day": 1}}))
    try:
        ctx = ctx_for(svc)
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
        await limit_reached(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
        assert svc.lane_meter("biz-a/ig")["limits_from"] == "policy.toml biz-a/*"
        svc.set_lane_limits("biz-a/ig", {}, by="owner")  # the owner lifts it for this lane
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
        svc.set_lane_limits("biz-a/ig", None, by="owner")  # back to the pattern
        await limit_reached(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    finally:
        svc.close()


async def test_a_commit_waiting_for_approval_is_not_counted(tmp_path):
    clock = Clock(at(2026, 11, 4, 12))
    svc, _ = make_service(tmp_path, clock=clock, config=PolicyConfig(meters=MeterPolicy(timezone=TZ)))
    try:
        svc.set_lane_limits("biz-a/ig", {"commits_per_day": 1}, by="owner")
        svc.ask_first.set("lane", "biz-a/ig", True, by="owner", person=True)
        ctx = ctx_for(svc)
        with pytest.raises(ApprovalPending):  # Ask me first holds the commit for a person
            await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig", content={"text": "hi"})
        assert svc.lane_meter("biz-a/ig")["usage"]["commits_today"] == 0
    finally:
        svc.close()


# ------------------------------------------------------------------ grant limits

async def test_a_grant_daily_dollar_limit_refuses_a_cost_that_would_pass_it_then_pauses_all_but_reads(env):
    svc, _, _ = env
    ctx = ctx_for(svc, budget={"usd_per_day": 1.0})
    await ctx.authorize(Effect.DRAFT, lane_id="biz-a/ig", cost={"usd": 0.6})
    e = await limit_reached(ctx.authorize(Effect.DRAFT, lane_id="biz-a/ig", cost={"usd": 0.5}))
    assert e.hit["code"] == "grant.usd_per_day"
    assert e.message.startswith("limit_reached: This call's cost ($0.50) would pass the limit of $1.00 a day "
                                "on Grant agent agent-1")
    await ctx.authorize(Effect.DRAFT, lane_id="biz-a/ig", cost={"usd": 0.4})
    e = await limit_reached(ctx.authorize(Effect.DRAFT, lane_id="biz-a/ig"))
    assert "reached its limit of $1.00 a day ($1.00 used today), so everything but reads is paused" in e.message
    await ctx.authorize(Effect.READ, lane_id="biz-a/ig")
    meter = svc.grant_meter(ctx.grant)
    assert meter["state"] == "cut_off" and meter["usage"]["usd"] == pytest.approx(1.0)
    assert meter["limits"][0]["left"] == 0


async def test_a_parent_limit_holds_for_its_children_and_a_child_cannot_raise_it(env):
    svc, _, _ = env
    parent, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE,
                            budget={"commits_per_day": 1})
    with pytest.raises(InvalidRequest, match="budget.commits_per_day"):
        svc.attenuate(tok, budget={"commits_per_day": 5})
    _child, ctok = svc.attenuate(tok, workspaces=["biz-a"])
    p, g = svc.resolve(ctok)
    ctx = svc.context(g, principal=p, tool="t", token=ctok)
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    e = await limit_reached(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    assert e.hit["id"] == parent.id
    assert svc.grant_meter(g)["limits"][0]["set_on"] == parent.id


async def test_grant_info_shows_the_meters_and_says_what_tripped(env):
    svc, _, _ = env
    svc.set_lane_limits("biz-a/ig", {"commits_per_day": 1}, by="owner")
    ctx = ctx_for(svc, budget={"tokens_per_day": 1000})
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    info = await grant_info(ctx)
    assert info["meters"]["grant"]["limits"][0]["name"] == "tokens_per_day"
    assert info["meters"]["grant"]["limits"][0]["left"] == 1000
    lane = info["meters"]["lanes"][0]
    assert lane["id"] == "biz-a/ig" and lane["state"] == "cut_off"
    assert any(line.startswith("Lane biz-a/ig reached its limit") for line in info["explain"])


# ------------------------------------------------------------------ thresholds

async def test_each_threshold_notifies_once_a_day(env):
    svc, clock, events = env
    svc.set_lane_limits("biz-a/ig", {"commits_per_day": 5}, by="owner")
    events.clear()
    ctx = ctx_for(svc)
    for _ in range(3):
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    assert events == []
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")  # 4 of 5: 80%
    assert [e["kind"] for e in events] == ["warning"]
    assert events[0]["explain"] == ("Lane biz-a/ig used 80% of its limit of 5 commits a day (4 used today). "
                                    "It resets at 00:00 (Europe/Vilnius).")
    await ctx.authorize(Effect.READ, lane_id="biz-a/ig")
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")  # 5 of 5: the cutoff
    assert [e["kind"] for e in events] == ["warning", "cutoff"]
    await limit_reached(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    await ctx.authorize(Effect.READ, lane_id="biz-a/ig")
    assert len(events) == 2  # nothing more today
    traces = [t for t in svc.traces.query(kind="meter")] if hasattr(svc.traces, "query") else []
    assert len(traces) in (0, 2)
    clock.t = at(2026, 11, 5, 9)
    for _ in range(4):
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    assert [e["kind"] for e in events] == ["warning", "cutoff", "warning"]  # a new day warns again


async def test_a_jump_past_several_thresholds_sends_only_the_highest(env):
    svc, _, events = env
    svc.set_lane_limits("biz-a/ig", {"tokens_per_day": 100}, by="owner")
    events.clear()
    ctx = ctx_for(svc)
    await ctx.authorize(Effect.DRAFT, lane_id="biz-a/ig", cost={"tokens": 100})
    assert [(e["kind"], e["threshold"]) for e in events] == [("cutoff", 1.0)]


# ------------------------------------------------------------------ concurrency

def test_concurrent_commits_never_pass_the_limit(env):
    svc, _, _ = env
    svc.set_lane_limits("biz-a/ig", {"commits_per_day": 10}, by="owner")
    g, _tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE)
    ok, refused = [], []
    lock = threading.Lock()

    def worker():
        for _ in range(5):
            try:
                svc.reserve(g, effect=Effect.COMMIT, lane_id="biz-a/ig")
                with lock:
                    ok.append(1)
            except Denied:
                with lock:
                    refused.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert (len(ok), len(refused)) == (10, 30)
    assert svc.lane_meter("biz-a/ig")["usage"]["commits_today"] == 10


def test_counters_are_kept_for_keep_days_then_purged(env):
    svc, clock, _ = env
    g, _tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE)
    svc.reserve(g, effect=Effect.READ, lane_id="biz-a/ig")
    clock.advance(9 * 86400)
    assert svc.meters.purge() >= 2
    assert svc.meters.used([svc.meters.key(Scope("lane", "biz-a/ig"), "act.read", "d2026-11-04")]) == {}


# ------------------------------------------------------------------ the CLI

def test_the_cli_shows_and_sets_a_lanes_limits(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from agentos.policy.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    runner = CliRunner()
    res = runner.invoke(app, ["grants", "meters", "--lane", "biz-a/ig", "--limit", "commits_per_day=6",
                              "--limit", "usd_per_day=2.5"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert [(i["name"], i["value"]) for i in out["limits"]] == [("usd_per_day", 2.5), ("commits_per_day", 6)]
    bad = runner.invoke(app, ["grants", "meters", "--lane", "biz-a/ig", "--limit", "posts=1"])
    assert bad.exit_code == 1
    res = runner.invoke(app, ["grants", "meters", "--lane", "biz-a/ig", "--unset"])
    assert res.exit_code == 0 and json.loads(res.output)["limits"] == []
    assert runner.invoke(app, ["grants", "meters"]).exit_code == 1


# ------------------------------------------------------------------ review fixes

def test_a_zone_change_at_restart_keeps_the_stored_day_until_it_ends(tmp_path):
    now = at(2026, 11, 4, 19)  # 19:00 in Vilnius is 02:00 the next day in Tokyo
    schema = ("CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0, "
              "updated_at REAL NOT NULL);")
    clock = Clock(now)
    vilnius = Meters(DB(tmp_path / "m.db", schema), cfg=MeterPolicy(timezone=TZ), clock=clock)
    vilnius.entries([Scope("lane", "biz-a/ig")], "commit", 0, 0, now)
    midnight = at(2026, 11, 5, 0)
    tokyo = Meters(DB(tmp_path / "m.db", schema), cfg=MeterPolicy(timezone="Asia/Tokyo"), clock=clock)
    assert tokyo.bucket("day", now) == "d2026-11-04" and tokyo.day_reset(now) == midnight
    assert tokyo.bucket("day", midnight) == "d2026-11-05"  # Tokyo's own day from the stored day's end
    # back the other way: Tokyo is ahead, so its day ends while it is still that date in Vilnius,
    # and the day runs on to Vilnius's midnight rather than start the same date over
    clock.t = t = datetime(2026, 11, 6, 20, 0, tzinfo=ZoneInfo("Asia/Tokyo")).timestamp()
    tokyo.entries([Scope("lane", "biz-a/ig")], "commit", 0, 0, t)
    back = Meters(DB(tmp_path / "m.db", schema), cfg=MeterPolicy(timezone=TZ), clock=clock)
    assert back.bucket("day", t) == "d2026-11-06" and back.day_reset(t) == at(2026, 11, 7, 0)


async def test_a_parent_limit_shows_as_the_parents_and_names_the_parent(env):
    svc, _, _ = env
    parent, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE,
                            budget={"usd_per_day": 5})
    _c1, t1 = svc.attenuate(tok, workspaces=["biz-a"])
    _c2, t2 = svc.attenuate(tok, workspaces=["biz-a"])
    p1, g1 = svc.resolve(t1)
    await svc.context(g1, principal=p1, tool="t", token=t1).authorize(Effect.DRAFT, lane_id="biz-a/ig",
                                                                       cost={"usd": 4.5})
    _p2, g2 = svc.resolve(t2)
    m = svc.grant_meter(g2)
    assert m["usage"]["usd"] == 0  # its own use
    [lim] = m["limits"]
    assert lim["set_on"] == parent.id and lim["used"] == 4.5
    assert lim["scope_note"].startswith(f"Set on grant {parent.id}, which this grant was narrowed from")
    assert m["explain"].startswith(f"Grant {parent.id} (which this grant was narrowed from) used 90%")
    assert g2.id not in m["explain"]


async def test_lane_taint_includes_turn_scoped_taint_and_clear_clears_it(env):
    from agentos.policy.service import family_key, turn_key

    svc, _, _ = env
    _g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE)
    g_turn, _ttok = svc.mint_turn_token(tok)
    svc.taint.add_origin(turn_key(g_turn.id), family_key(g_turn), kind="web_page", host="evil.example")
    lt = svc.lane_taint("biz-a/ig")
    assert lt["tainted"] and lt["families"][0]["follows"][0]["host"] == "evil.example"
    assert svc.clear_taint(g_turn.id)["origins_cleared"] == 1
    assert svc.lane_taint("biz-a/ig")["tainted"] is False


async def test_a_refused_call_sends_the_cutoff_once(env):
    svc, _, events = env
    ctx = ctx_for(svc, budget={"usd_per_day": 5})
    await ctx.authorize(Effect.DRAFT, lane_id="biz-a/ig", cost={"usd": 4.9})
    for _ in range(2):
        await limit_reached(ctx.authorize(Effect.DRAFT, lane_id="biz-a/ig", cost={"usd": 0.2}))
    assert [e["kind"] for e in events] == ["warning", "cutoff"]
    assert events[1]["explain"].startswith("This call's cost ($0.20) would pass the limit")


async def test_lowering_a_lane_limit_below_todays_use_sends_the_cutoff_once(env):
    svc, _, events = env
    ctx = ctx_for(svc)
    for _ in range(3):
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    svc.set_lane_limits("biz-a/ig", {"commits_per_day": 2}, by="owner")
    assert [e["kind"] for e in events if "threshold" in e] == ["cutoff"]
    await limit_reached(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    svc.set_lane_limits("biz-a/ig", {"commits_per_day": 2}, by="owner")
    assert [e["kind"] for e in events if "threshold" in e] == ["cutoff"]


async def test_an_hourly_limit_notifies_once_per_hour(env):
    svc, clock, events = env
    svc.set_lane_limits("biz-a/ig", {"commits_per_hour": 1}, by="owner")
    ctx = ctx_for(svc)
    clock.t = at(2026, 11, 4, 10, 30)
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    await limit_reached(ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig"))
    clock.t = at(2026, 11, 4, 11, 30)
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    assert [e["kind"] for e in events if "threshold" in e] == ["cutoff", "cutoff"]


async def test_a_notice_that_could_not_be_sent_is_sent_on_the_next_call(env, monkeypatch):
    svc, _, events = env
    svc.set_lane_limits("biz-a/ig", {"commits_per_day": 5}, by="owner")
    ctx = ctx_for(svc)
    for _ in range(3):
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")
    write = svc.traces.write

    def broken(ev, **kw):
        if ev.kind == "meter":
            raise OSError("disk full")
        return write(ev, **kw)

    monkeypatch.setattr(svc.traces, "write", broken)
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/ig")  # 4 of 5: the warning cannot be traced
    assert [e.get("kind") for e in events if "threshold" in e] == []
    monkeypatch.setattr(svc.traces, "write", write)
    await ctx.authorize(Effect.READ, lane_id="biz-a/ig")
    assert [e["kind"] for e in events if "threshold" in e] == ["warning"]


async def test_meter_notices_go_to_the_grants_owner_and_the_node_owner_only(env, monkeypatch):
    from test_policy_notify_members import Directory
    from test_policy_notify_recipients import human

    svc, _, _ = env
    svc.notifier.update_config({"ntfy": {"topic": "agentos-owner-H5j8Kd2Sf6Gq9Wz3Yx7Pm1"}})
    svc.notifier.directory = Directory({"m_ops": [human("m_ops", ["biz-a"], approve=["commit"])],
                                        "m_mine": [human("m_mine", ["biz-b"])]})
    svc.notifier.update_member("m_ops", {"topic": "agentos-ops-Q8w2Lm4Zp7Rt1Xc9Vb3Nk6"})
    svc.notifier.update_member("m_mine", {"topic": "agentos-mine-Z8w2Lm4Zp7Rt1Xc9Vb3Nk6"})
    sent: list[dict] = []
    monkeypatch.setattr(svc.notifier, "send", lambda ev: sent.append(ev) or [])
    svc.set_lane_limits("biz-a/ig", {"commits_per_day": 1}, by="owner")
    g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE, owner="m_mine")
    await svc.context(g, tool="t", token=tok).authorize(Effect.COMMIT, lane_id="biz-a/ig")
    [ev] = sent
    assert svc.notifier.recipients(ev) == ["owner", "m_mine"]  # m_ops controls the lane: not theirs to hear
    await svc.context(g, tool="t", token=tok).authorize(Effect.COMMIT, lane_id="biz-a/fb")
    svc.set_lane_limits("biz-a/fb", {"commits_per_day": 1}, by="owner")  # the owner's own change: no grant
    assert len(sent) == 2 and svc.notifier.recipients(sent[-1]) == ["owner"]


async def test_a_grant_for_one_lane_counts_its_lane_less_calls_against_that_lane(env):
    svc, _, _ = env
    svc.set_lane_limits("biz-a/ig", {"commits_per_day": 1}, by="owner")
    g, tok = svc.issue(subject={"id": "agent-1"}, lanes=["biz-a/ig"], ceiling=Effect.IRREVERSIBLE)
    ctx = svc.context(g, tool="t", token=tok)
    await ctx.authorize(Effect.COMMIT)  # names no lane, but this grant reaches only biz-a/ig
    await limit_reached(ctx.authorize(Effect.COMMIT))
    assert svc.lane_meter("biz-a/ig")["usage"]["commits_today"] == 1
    wide = ctx_for(svc)  # the whole workspace: no lane implied, grant-metered only
    await wide.authorize(Effect.COMMIT)
    await wide.authorize(Effect.COMMIT)
    assert svc.lane_meter("biz-a/ig")["usage"]["commits_today"] == 1


async def test_brain_spend_is_charged_to_the_grants_meters_and_its_lane(env):
    svc, _, events = env
    svc.set_lane_limits("biz-a/ig", {"usd_per_day": 1}, by="owner")
    parent, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.PAY,
                            budget={"tokens_per_day": 1000})
    p, _g = svc.resolve(tok)
    child = svc.mint_child_token(p, purpose="brain task t1", workspace="biz-a", lanes=["biz-a/ig"])
    child_id = child["grant_id"] if isinstance(child, dict) else child.grant_id
    svc.revoke(child_id)  # a brain's child grant ends with its task, before or after the spend comes in
    assert svc.charge_spend(child_id, tokens=900, usd=1.0)
    assert svc.grant_meter(svc.grant_by_id(parent.id))["usage"]["tokens"] == 900
    lane = svc.lane_meter("biz-a/ig")
    assert lane["usage"]["usd"] == 1.0 and lane["state"] == "cut_off"
    assert {e["kind"] for e in events if "threshold" in e} == {"warning", "cutoff"}
    assert svc.charge_spend("nope", tokens=1) is False


async def test_a_publish_the_guard_saw_counts_as_one_commit_and_past_the_limit_is_refused(env):
    from agentos.node.tools import ActMeter

    svc, _, events = env
    svc.set_lane_limits("biz-a/ig", {"commits_per_day": 1}, by="owner")
    ctx = ctx_for(svc)
    await ctx.authorize(Effect.DRAFT, lane_id="biz-a/ig")  # browser_act's own authorize
    meter = ActMeter.of(ctx, "biz-a/ig")
    assert meter is not None and not meter.capped()
    rule = SimpleNamespace(id="instagram.configure")
    assert meter.charge(rule, sent=True) is None
    usage = svc.lane_meter("biz-a/ig")["usage"]
    assert usage["commits_today"] == 1 and usage["actions"] == {"draft": 1}  # a commit, not a second action
    assert meter.capped()
    why = meter.charge(rule, sent=False)  # the route holds it: refused, nothing debited
    assert why.startswith("Lane biz-a/ig reached its limit of 1 commit a day")
    assert svc.lane_meter("biz-a/ig")["usage"]["commits_today"] == 1
    assert meter.charge(rule, sent=True) is not None  # it left anyway: counted, because it happened
    assert svc.lane_meter("biz-a/ig")["usage"]["commits_today"] == 2
    assert [e["kind"] for e in events if "threshold" in e] == ["cutoff"]
    traces = svc.traces.query(kind="commit_observed", limit=10)[0]
    assert sorted(t.status for t in traces) == ["denied", "denied", "ok"]
