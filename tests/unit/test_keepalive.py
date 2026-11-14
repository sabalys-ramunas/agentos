"""Session keepalive (agentos.keepalive): the planner's rules, the visit on the dev fake lanes and site,
read-only enforcement (the visit's own action list and the egress guard), signed-out and challenged
lanes, coordination with recorded activity, and the owner's switches."""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_commit_gate_guard import Gate
from test_egress import COMMITTER, FailingDispatcher, FakeRequest, FakeRoute, make_guard, settle
from typer.testing import CliRunner

from agentos.core.config import NodeConfig, load_config, save_config
from agentos.core.errors import EgressRefused, InvalidRequest
from agentos.core.models import (
    AccountRef,
    ActionKind,
    ControlState,
    LaneControl,
    LaneInfo,
    LaneSpec,
    LaneState,
    NavigateResult,
    Signal,
)
from agentos.core.runtime import Runtime
from agentos.dev.lanes import DevLaneManager
from agentos.dev.web import CHALLENGE_URL, DevInteraction, new_tab
from agentos.engage.persona import draw_persona
from agentos.keepalive import planner, visit
from agentos.keepalive.service import KeepaliveService
from agentos.keepalive.sites import TABLE, Entry, entry_for
from agentos.keepalive.store import KeepaliveStore
from agentos.node import egress
from agentos.ops import control
from agentos.runner.fakes import BASE
from agentos.stealth.lanecheck import Check, LaneReport

UTC_TZ = ZoneInfo("UTC")
VILNIUS = ZoneInfo("Europe/Vilnius")
NEW_YORK = ZoneInfo("America/New_York")
SEED = "0123456789abcdef"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
FAST = visit.Pace(first_look=(0, 0), between=(0, 0), item_look=(0, 0), last_look=(0, 0), max_seconds=30)
LANE = "biz-a/blog"


def spec(lane_id: str = LANE, **kw) -> LaneSpec:
    kw.setdefault("sites", ["social.example"])
    return LaneSpec(id=lane_id, **kw)


def info(state: LaneState = LaneState.COLD, **kw) -> LaneInfo:
    return LaneInfo(spec=spec(), state=state, **kw)


# ---- the site table -------------------------------------------------------------------------------

def test_a_lane_visits_its_own_site_or_its_first_site():
    assert entry_for(LaneSpec(id="biz-a/instagram")).key == "instagram"
    assert entry_for(LaneSpec(id="acme/x", account=AccountRef(site="reddit", handle="u/a"))).key == "reddit"
    own = entry_for(LaneSpec(id="biz-a/blog", sites=["frame:captcha.example", "*.myblog.example"]))
    assert (own.key, own.home, own.items) == ("site", "https://myblog.example/", None)
    assert entry_for(LaneSpec(id="biz-a/blog", sites=["medium.com"])).key == "medium"
    assert entry_for(LaneSpec(id="biz-a/nothing")) is None
    for key in ("instagram", "facebook", "linkedin", "reddit", "medium", "wordpress"):
        lo, hi = TABLE[key].cadence
        assert 2 <= lo < hi <= 9


def test_item_links_stay_on_the_site_and_point_at_one_post():
    ig = TABLE["instagram"]
    assert ig.is_item("/p/Cx1y2Z3/", "https://www.instagram.com/")
    assert ig.is_item("https://www.instagram.com/reel/abc_1/")
    assert not ig.is_item("https://evilinstagram.com/p/abc/")
    assert not ig.is_item("/accounts/edit/", "https://www.instagram.com/")
    assert not ig.is_item("javascript:like()", "https://www.instagram.com/")
    assert TABLE["reddit"].is_item("/r/python/comments/1abc/some_title/", "https://www.reddit.com/")


# ---- the planner ----------------------------------------------------------------------------------

def test_cadence_is_drawn_inside_the_sites_range_and_differs_per_lane():
    ig = TABLE["instagram"]
    seen = {planner.cadence(ig, f"seed-{i}", NOW) for i in range(30)}
    assert len(seen) == 30
    assert all(timedelta(days=2) <= c <= timedelta(days=4) for c in seen)
    assert planner.cadence(ig, SEED, NOW) == planner.cadence(ig, SEED, NOW)  # stable between rounds
    assert planner.cadence(ig, SEED, NOW) != planner.cadence(ig, SEED, NOW + timedelta(hours=1))


def test_due_counts_from_the_last_activity_and_a_day_before_the_cookie_expires():
    ig = TABLE["instagram"]
    used = NOW - timedelta(days=1)
    due = planner.due_at(ig, SEED, used, None, NOW)
    assert used + timedelta(days=2) <= due <= used + timedelta(days=4)
    soon = NOW + timedelta(hours=30)
    assert planner.due_at(ig, SEED, used, soon, NOW) == soon - planner.EXPIRY_MARGIN
    assert planner.due_at(ig, SEED, None, None, NOW) == NOW  # never used: due now


@pytest.mark.parametrize("tz", [UTC_TZ, VILNIUS, NEW_YORK, ZoneInfo("Asia/Tokyo")])
def test_visits_fall_inside_the_waking_hours_in_the_lanes_own_time_zone(tz):
    for i in range(40):
        seed = f"lane-{i}"
        persona = draw_persona("x/y", seed)
        due = NOW + timedelta(hours=i * 7)
        at = planner.next_visit(seed, persona, tz, due, NOW)
        assert at is not None and at >= due
        local = at.astimezone(tz)
        first, last = persona.for_day(local.date()).window(local.date())
        hour = local.hour + local.minute / 60 + local.second / 3600
        assert first <= hour <= last
        assert local.second != 0  # never on the minute


@pytest.mark.parametrize(("day", "tz"), [(date(2026, 3, 29), VILNIUS), (date(2026, 10, 25), VILNIUS),
                                         (date(2026, 3, 8), NEW_YORK), (date(2026, 11, 1), NEW_YORK)])
def test_daylight_saving_days_keep_the_local_window(day, tz):
    for i in range(60):
        seed = f"dst-{i}"
        persona = draw_persona("x/y", seed)
        start = datetime.combine(day, datetime.min.time(), tzinfo=tz).astimezone(UTC)
        at = planner.next_visit(seed, persona, tz, start, start)
        local = at.astimezone(tz)
        first, last = persona.for_day(local.date()).window(local.date())
        assert local.date() == day
        assert first <= local.hour + local.minute / 60 + local.second / 3600 <= last
    first, last = planner.window(draw_persona("x/y", "w"), day, tz)
    wall = (last.astimezone(tz).replace(tzinfo=None) - first.astimezone(tz).replace(tzinfo=None))
    lo, hi = draw_persona("x/y", "w").for_day(day).window(day)
    assert abs(wall.total_seconds() / 3600 - (hi - lo)) < 1 / 60  # wall-clock hours, whatever the offset did


def test_a_missed_slot_goes_at_once_while_its_window_lasts_then_the_next_day():
    persona = draw_persona("x/y", SEED)
    due = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    slot = planner.next_visit(SEED, persona, UTC_TZ, due, due)
    assert planner.next_visit(SEED, persona, UTC_TZ, due, slot) == slot
    later = slot + timedelta(minutes=5)
    _first, end = planner.window(persona, slot.date(), UTC_TZ)
    if later < end - planner.VISIT_ROOM:
        assert planner.next_visit(SEED, persona, UTC_TZ, due, later) == later  # missed: now
    after = end + timedelta(minutes=1)
    tomorrow = planner.next_visit(SEED, persona, UTC_TZ, due, after)
    assert tomorrow.date() == slot.date() + timedelta(days=1)


def test_a_slot_moves_away_from_the_last_visits_time_of_day():
    moved = 0
    for i in range(40):
        seed = f"repeat-{i}"
        persona = draw_persona("x/y", seed)
        due = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
        plain = planner.next_visit(seed, persona, UTC_TZ, due, due)
        yesterday = plain - timedelta(days=1, minutes=3)
        at = planner.next_visit(seed, persona, UTC_TZ, due, due, yesterday)
        gap = abs((at - yesterday) - timedelta(days=1))
        if gap >= planner.REPEAT_MIN:
            moved += 1
    assert moved >= 38  # all but a window too short to move inside


def test_thirty_lanes_never_share_a_clock():
    times = set()
    for i in range(30):
        seed = f"node-{i}"
        at = planner.next_visit(seed, draw_persona(f"w/l{i}", seed), UTC_TZ, NOW, NOW)
        times.add(at.strftime("%H:%M"))
    assert len(times) >= 28


def _block(lane: LaneInfo, **kw):
    args = {"enabled": True, "opted_out": False, "frozen": False, "entry": TABLE["instagram"], "signed_in": True,
            "now": NOW, **kw}
    return planner.blocked(lane, **args)


def test_skip_rules():
    assert _block(info()) is None
    assert _block(info(), enabled=False).code == "off"
    assert _block(info(), opted_out=True).code == "opted_out"
    assert _block(info(), entry=None).code == "no_site"
    assert _block(info(LaneState.QUARANTINED, quarantine_reason="exit IP changed")).code == "quarantined"
    assert _block(info(), frozen=True).code == "frozen"
    human = _block(info(control=LaneControl(state=ControlState.HUMAN, by="owner")))
    assert human.code == "human" and human.waits
    timed = LaneControl(state=ControlState.PAUSED, reason="backoff:rate_limited", hold_until=NOW + timedelta(hours=1))
    assert _block(info(control=timed)).code == "held"
    checkpoint = _block(info(control=LaneControl(state=ControlState.PAUSED, reason="needs_human:checkpoint"),
                             signal=Signal.CHECKPOINT))
    assert checkpoint.code == "challenged" and checkpoint.notify
    assert _block(info(control=LaneControl(state=ControlState.PAUSED, by="owner"))).code == "held"
    assert _block(info(claim={"holder": "claude"})).code == "claimed"
    assert _block(info(queue_length=1)).code == "queued"
    for state in (LaneState.WARM, LaneState.ACTIVE, LaneState.WARMING):
        assert _block(info(state)).code == "in_use"
    assert _block(info(signal=Signal.CAPTCHA)).code == "challenged"
    out = _block(info(), signed_in=False)
    assert out.code == "signed_out" and out.notify
    assert _block(info(), signed_in=None) is None  # unknown is not signed out


# ---- the visit, read only -------------------------------------------------------------------------

def test_outline_nodes_read_refs_and_link_addresses():
    text = """\
- main
  - button "Like 12" [pressed] [ref=e1] [cursor=pointer]
  - link "A post by someone" [ref=e2] [cursor=pointer]
    - /url: /p/Cx1y2Z3/
    - img "photo"
  - link "Edit profile" [ref=e3]
    - /url: /accounts/edit/
  - textbox "Search" [ref=e4]"""
    nodes = visit.outline_nodes(text)
    assert [(n.ref, n.role, n.url) for n in nodes] == [
        ("e1", "button", None), ("e2", "link", "/p/Cx1y2Z3/"), ("e3", "link", "/accounts/edit/"),
        ("e4", "textbox", None)]
    ro = visit.ReadOnly(None, None, TABLE["instagram"])
    ro.nodes, ro.url = {n.ref: n for n in nodes}, "https://www.instagram.com/"
    assert [n.ref for n in ro.item_links()] == ["e2"]
    # after the page navigated, refs carry the frame (agentos.node.snapshot REF_RE): a lane's page always has
    framed = visit.outline_nodes(text.replace("[ref=e", "[ref=f3e"))
    assert [(n.ref, n.url) for n in framed][:2] == [("f3e1", None), ("f3e2", "/p/Cx1y2Z3/")]


@pytest.fixture
async def dev():
    lanes = DevLaneManager(NodeConfig(home="/nonexistent"), make_page=new_tab)
    await lanes.create(spec())
    return lanes, lanes.watch(DevInteraction())


async def test_a_visit_on_the_dev_lane_scrolls_and_changes_nothing(dev):
    lanes, interaction = dev
    entry = entry_for(spec())
    async with visit.lane_session(lanes, LANE) as page:
        site = page.driver.site
        before = (list(site.state["posts"]), set(site.state["liked"]))
        out = await visit.run(page, interaction, entry, pace=FAST, r=random.Random(1), sleep=_no_sleep)
    assert out.outcome == "ok" and out.reason is None
    assert out.url_host == "social.example" and out.scrolls >= 1
    assert (site.state["posts"], site.state["liked"]) == before
    assert lanes.principals == ["node"]
    assert out.session == "unknown"  # a fake tab has no cookies to read


async def _no_sleep(seconds: float) -> None:
    return None


async def test_a_scripted_like_is_refused_before_anything_is_pressed(dev):
    lanes, interaction = dev
    async with visit.lane_session(lanes, LANE) as page:
        ro = visit.ReadOnly(interaction, page, entry_for(spec()))
        await ro.open(f"{BASE}/p/1")
        nodes = await ro.look()
        like = next(n for n in nodes if n.role == "button" and n.name == "Like")
        with pytest.raises(EgressRefused, match="read-only"):
            await ro.open_item(like.ref)
        with pytest.raises(EgressRefused, match="stays on"):
            await ro.open("https://elsewhere.example/")
        assert page.driver.site.state["liked"] == set()
    home = await _fresh_dev_lane()
    async with visit.lane_session(home, LANE) as page:
        ro = visit.ReadOnly(home.watch(DevInteraction()), page, entry_for(spec()))
        await ro.open(f"{BASE}/")
        post = next(n for n in await ro.look() if n.name == "New post")
        with pytest.raises(EgressRefused):
            await ro.open_item(post.ref)  # a button that opens the composer: never pressed
        assert page.driver.url == f"{BASE}/"


async def _fresh_dev_lane() -> DevLaneManager:
    lanes = DevLaneManager(NodeConfig(home="/nonexistent"), make_page=new_tab)
    await lanes.create(spec())
    return lanes


async def test_the_guard_refuses_a_like_under_the_keepalive_grant(tmp_path):
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "X-FB-Friendly-Name": "usePolarisLikeMediaXIGLikeMutation"}
    body = b"fb_api_req_friendly_name=usePolarisLikeMediaXIGLikeMutation&variables=%7B%7D"
    rule = egress.classify("https://www.instagram.com/api/graphql", "POST", headers, body=body)
    assert rule is not None and rule.id == "instagram.like"
    refusal = egress.judge("https://www.instagram.com/api/graphql", "POST", navigation=False,
                           sites=(), driver=visit.DRIVER, human=False, effect=rule)
    assert refusal is not None and refusal.rule == "write" and "read" in refusal.reason
    guard, ctx, _events, _ = make_guard(tmp_path, driver=visit.DRIVER)
    ctx.emit("request", FakeRequest("https://www.instagram.com/api/graphql", "POST", headers=headers, body=body))
    ctx.emit("request", FakeRequest("https://i.instagram.com/api/v1/media/configure/", "POST"))
    await settle()
    assert [b.rule for b in guard.blocks_since(0)] == ["write", "commit"]
    reads = make_guard(tmp_path / "r", driver=visit.DRIVER)
    reads[1].emit("request", FakeRequest("https://www.instagram.com/api/graphql", "POST",
                                         headers={**headers, "X-FB-Friendly-Name": "PolarisFeedQuery"}))
    assert reads[0].blocks_since(0) == []  # reading the feed through POST is still allowed


class GuardPage:
    """A tab with a guard on its context, the way a real lane's tab has one."""

    def __init__(self, guard, ctx, driver):
        self.context = ctx
        self.inner = driver

    @property
    def url(self):
        return self.inner.url


async def test_the_visit_drives_the_guard_read_only_and_puts_the_old_driver_back(tmp_path, dev):
    """On a lane that is not strict, a write the page sends during the visit is aborted before it
    leaves (the gate's route in its effects mode), not only recorded; a feed read goes as it would for
    a person, and nothing is gated."""
    lanes, interaction = dev
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=egress.Driver(kind="agent", id="claude"))
    assert not guard.strict
    seen, routed = [], []

    class Watching:
        def __getattr__(self, name):
            return getattr(interaction, name)

        async def navigate(self, page, url):
            seen.append(guard.driver)
            if routed:
                return await interaction.navigate(page.inner, url)
            [(pattern, handler)] = ctx.routes  # the route the visit installed: the gate's, in its effects mode
            assert pattern == guard.gate_route()
            for request in (FakeRequest("https://i.instagram.com/api/v1/media/configure/", "POST"),
                            FakeRequest("https://www.instagram.com/api/v1/seen/", "POST"),
                            FakeRequest("https://www.instagram.com/p/x/")):
                route = FakeRoute()
                await handler(route, request)
                ctx.emit("request", request)  # the listener hears it too, and leaves it to the route
                routed.append((request.url, route.aborted, route.continued))
            return await interaction.navigate(page.inner, url)

        async def snapshot(self, page):
            return await interaction.snapshot(page.inner)

        async def act(self, page, ref, action, text=None):
            return await interaction.act(page.inner, ref, action, text)

    async with visit.lane_session(lanes, LANE) as tab:
        page = GuardPage(guard, ctx, tab)
        out = await visit.run(page, Watching(), entry_for(spec()), pace=FAST, r=random.Random(2), sleep=_no_sleep)
    await settle()
    assert seen == [visit.DRIVER]
    assert guard.driver == egress.Driver(kind="agent", id="claude")
    assert routed == [("https://i.instagram.com/api/v1/media/configure/", "blockedbyclient", 0),
                      ("https://www.instagram.com/api/v1/seen/", "blockedbyclient", 0),  # a POST no rule names
                      ("https://www.instagram.com/p/x/", None, 1)]
    assert [(b["rule"], b["prevented"]) for b in out.blocks] == [("commit", True), ("commit", True)]
    assert ctx.routes == []  # the route lasted only as long as the visit


async def test_a_challenge_or_a_login_page_ends_the_visit(dev):
    lanes, interaction = dev
    challenge = Entry("site", "Social", CHALLENGE_URL, ("social.example",), (3, 5))
    async with visit.lane_session(lanes, LANE) as page:
        out = await visit.run(page, interaction, challenge, pace=FAST, r=random.Random(3), sleep=_no_sleep)
    assert (out.outcome, out.signal, out.scrolls) == ("challenged", "captcha", 0)
    assert lanes.get(LANE).control.state == ControlState.PAUSED  # the watched interaction held it

    class LoggedOut:
        async def navigate(self, page, url):
            return NavigateResult(url="https://www.instagram.com/accounts/login/", title="Login",
                                  signal=Signal.LOGGED_OUT, signal_detail="login URL")

    out = await visit.run(object(), LoggedOut(), TABLE["instagram"], pace=FAST, sleep=_no_sleep)
    assert out.outcome == "signed_out" and "login page" in out.reason


async def test_another_caller_ends_the_visit_early_and_that_counts(dev):
    lanes, interaction = dev
    async with visit.lane_session(lanes, LANE) as page:
        out = await visit.run(page, interaction, entry_for(spec()), pace=FAST, r=random.Random(4),
                              sleep=_no_sleep, waiting=lambda: True)
    assert out.outcome == "yielded" and out.scrolls == 0


class CookieContext:
    def __init__(self, jar):
        self.jar = jar

    async def cookies(self, urls=None):
        return list(self.jar)


async def test_the_session_cookies_are_read_before_and_after():
    soon, later = NOW.timestamp() + 86400, NOW.timestamp() + 90 * 86400
    jar = [{"name": "sessionid", "value": "s", "domain": ".instagram.com", "expires": soon},
           {"name": "ds_user_id", "value": "1", "domain": ".instagram.com", "expires": later}]
    page = SimpleNamespace(context=CookieContext(jar))
    state = await visit.session_state(page, TABLE["instagram"])
    assert state == {"signed_in": True, "expires_at": datetime.fromtimestamp(soon, UTC).isoformat(timespec="seconds")}
    out = visit.Outcome(session_before=state, session_after={**state, "expires_at": "2027-01-01T00:00:00+00:00"})
    assert out.session == "refreshed"
    assert visit.Outcome(session_before=state, session_after=state).session == "kept"
    assert visit.Outcome(session_after={"signed_in": False}).session == "signed_out"
    gone = await visit.session_state(SimpleNamespace(context=CookieContext([])), TABLE["instagram"])
    assert gone == {"signed_in": False, "expires_at": None}


# ---- the service: rounds, records, coordination ---------------------------------------------------

def afternoon(now: datetime) -> str:
    """A fixed-offset zone where `now` is mid-afternoon: always inside a drawn persona's waking hours."""
    offset = 15 - now.hour
    offset = offset - 24 if offset > 14 else offset + 24 if offset < -12 else offset
    return "Etc/GMT" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


class Notes:
    def __init__(self):
        self.sent = []

    def send(self, event):
        self.sent.append(event)


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> float:
        return self.at.timestamp()


@pytest.fixture
async def node(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    cfg = NodeConfig(home=tmp_path, keepalive=True, max_warm_lanes=2)
    save_config(cfg)
    lanes = DevLaneManager(cfg, make_page=new_tab)
    for lane_id in (LANE, "biz-a/other"):
        await lanes.create(spec(lane_id))
        path = cfg.fingerprint_path(lane_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"timezone": afternoon(now)}))
    notes = Notes()
    runtime = Runtime(config=cfg, lanes=lanes, interaction=lanes.watch(DevInteraction()), notifier=notes)
    clock = Clock(now)
    service = KeepaliveService(runtime, home=tmp_path, clock=clock, pace=FAST, rng=random.Random(5), sleep=_no_sleep)
    yield SimpleNamespace(service=service, lanes=lanes, notes=notes, clock=clock, cfg=cfg, home=tmp_path)
    service.store.close()


async def due_round(n, lane=LANE):
    """Move the clock to the lane's slot and run a round."""
    plan = n.service.plan(n.lanes.get(lane), n.service.now())
    assert plan["blocked"] is None, plan["blocked"]
    n.clock.at = max(plan["_at"], n.clock.at)
    return await n.service.run_round()


async def test_a_round_visits_a_due_lane_records_it_and_lets_it_sleep(node):
    out = await due_round(node)
    assert out["visited"]["outcome"] == "ok", out
    assert node.lanes.get(LANE).state == LaneState.COLD  # woken for the visit, asleep again
    assert ("close", LANE) in node.lanes.calls
    last = node.service.store.last_visit(LANE)
    assert last.trigger == "planned" and last.detail["scrolls"] >= 1 and last.detail["host"] == "social.example"
    status = node.service.status(LANE)
    assert status["last_visit"]["outcome"] == "ok"
    assert status["next_at"] > status["last_visit"]["at"]  # the next one is days away
    days = (datetime.fromisoformat(status["due_at"]) - datetime.fromisoformat(status["activity_at"])).days
    assert 3 <= days <= 7
    assert not node.notes.sent
    again = await node.service.run_round()  # the gap between visits holds the other lane back
    assert "visited" not in again


async def test_one_lane_at_a_time_with_a_gap(node):
    node.service.plan(node.lanes.get("biz-a/other"), node.service.now())
    first = await due_round(node)
    assert "visited" in first
    other = node.service.plan(node.lanes.get("biz-a/other"), node.service.now())
    node.clock.at = max(other["_at"], node.clock.at) + timedelta(seconds=1)
    if node.clock.at < node.service._not_before:
        assert "visited" not in await node.service.run_round()
        node.clock.at = node.service._not_before + timedelta(seconds=1)
    out = await node.service.run_round()
    assert out.get("visited", {}).get("outcome") == "ok" or out.get("due") == []


async def test_recent_activity_and_an_engagement_session_push_the_visit_back(node):
    lane = node.lanes.get(LANE)
    lane.spec = lane.spec.model_copy(update={"created_at": node.service.now() - timedelta(days=30)})
    before = node.service.plan(lane, node.service.now())
    assert before["activity_at"] is None and datetime.fromisoformat(before["due_at"]) < node.service.now()
    async with node.lanes.using(LANE, principal_kind="agent"):  # an engagement session, a flow, an agent call
        pass
    await node.lanes.close(LANE)
    after = node.service.plan(node.lanes.get(LANE), node.service.now())
    assert after["activity_at"] is not None and after["due_at"] > before["due_at"]
    assert datetime.fromisoformat(after["next_at"]) - node.service.now() > timedelta(days=1)
    human = node.lanes.get(LANE)
    human.last_used = None
    human.control = LaneControl(last_human_input=node.service.now() - timedelta(hours=2))
    planned = node.service.plan(human, node.service.now())
    assert datetime.fromisoformat(planned["due_at"]) > node.service.now() + timedelta(days=1)


async def test_skips_follow_the_switches_and_the_gates(node):
    save_config(node.cfg.model_copy(update={"keepalive": False}))
    assert await node.service.run_round() == {"enabled": False}
    save_config(node.cfg)
    node.service.set_opt_out(LANE, True)
    node.service.set_opt_out("biz-a/other", True)
    assert node.service.status(LANE)["blocked"]["code"] == "opted_out"
    node.clock.at += timedelta(days=10)
    assert "visited" not in await node.service.run_round()
    node.service.set_opt_out(LANE, False)
    await node.lanes.take_control(LANE, by="owner")
    assert node.service.status(LANE)["blocked"]["code"] == "human"
    assert "visited" not in await node.service.run_round()
    await node.lanes.hand_back(LANE, by="owner")
    await node.lanes.quarantine(LANE, "exit IP changed")
    assert node.service.status(LANE)["blocked"]["code"] == "quarantined"
    assert node.lanes.principals == []  # nothing ever touched the lane


async def test_memory_pressure_and_a_full_warm_cap_wait_and_never_close_a_lane(node):
    node.lanes.memory_status = lambda: {"available_mb": 1500, "reserved_mb": 0, "floor_mb": 800}
    out = await due_round(node)
    assert "visited" not in out and "MB of memory" in out["waiting"]
    node.lanes.memory_status = lambda: {"available_mb": 8000, "reserved_mb": 0, "floor_mb": 800}
    for lane_id in ("biz-a/other", "biz-a/third"):
        if lane_id not in node.lanes.lanes:
            await node.lanes.create(spec(lane_id))
        await node.lanes.open(lane_id)
    out = await node.service.run_round()
    assert "visited" not in out and "warm slots" in out["waiting"]
    assert [c for c in node.lanes.calls if c[0] == "close"] == []
    assert node.service.status(LANE)["waiting"] == out["waiting"]
    await node.lanes.close("biz-a/third")
    out = await node.service.run_round()
    assert out["visited"]["outcome"] == "ok"
    assert node.lanes.get("biz-a/other").state == LaneState.WARM  # left exactly as it was


async def test_a_challenged_visit_marks_the_lane_and_tells_the_owner_once(node, monkeypatch):
    from agentos.keepalive import service as service_mod

    challenge = Entry("site", "Social", CHALLENGE_URL, ("social.example",), (3, 5))
    monkeypatch.setattr(service_mod, "entry_for", lambda s: challenge)
    node.service.set_opt_out("biz-a/other", True)
    out = await due_round(node)
    assert out["visited"]["outcome"] == "challenged"
    row = node.service.store.lane(LANE)
    assert row.status == "challenged" and row.notified
    assert [e["type"] for e in node.notes.sent] == ["keepalive.challenged"]
    assert "—" not in node.notes.sent[0]["body"] and LANE in node.notes.sent[0]["title"]
    assert node.lanes.get(LANE).control.state == ControlState.PAUSED
    assert node.lanes.get(LANE).state == LaneState.WARM  # held: left for the human, then the idle reaper
    node.clock.at += timedelta(days=5)
    await node.service.run_round()
    assert len(node.notes.sent) == 1  # once per episode
    assert node.service.status(LANE)["blocked"]["code"] == "challenged"
    # a human solved it in the live view: the page has left the challenge, so the hand-back (which reads
    # the page first and holds the lane again while a challenge shows) gives the lane back
    node.lanes._page_for(LANE).url = "https://social.example/"
    await node.lanes.hand_back(LANE, by="owner")
    assert node.lanes.get(LANE).control.state == ControlState.AGENT
    await node.lanes.close(LANE)
    node.lanes.get(LANE).signal = Signal.OK
    monkeypatch.undo()  # the next visit sees the feed again
    node.clock.at = node.service._not_before + timedelta(seconds=1)
    out = await due_round(node)
    assert out["visited"]["outcome"] == "ok"
    assert node.service.store.lane(LANE).status is None


async def test_a_lane_the_lane_check_finds_signed_out_is_not_visited_and_the_owner_hears(node):
    checker = SimpleNamespace(last={})
    node.service.runtime.register("lane_check", checker)
    for lane_id in (LANE, "biz-a/other"):
        checker.last[lane_id] = LaneReport(lane_id, "cold", NOW, [Check("session", "fail", "Signed out.", None, {
            "sites": {"site": {"signed_in": False, "missing_cookie": "sid"}}})])
    node.clock.at += timedelta(days=10)
    out = await node.service.run_round()
    assert "visited" not in out and node.lanes.principals == []
    assert sorted(e["lane"] for e in node.notes.sent) == ["biz-a/blog", "biz-a/other"]
    assert {e["type"] for e in node.notes.sent} == {"keepalive.signed_out"}
    await node.service.run_round()
    assert len(node.notes.sent) == 2
    checker.last[LANE] = LaneReport(LANE, "cold", NOW, [Check("session", "ok", "Signed in.", None, {
        "sites": {"site": {"signed_in": True, "expires_at": "2027-01-01T00:00:00+00:00"}}})])
    await node.service.run_round()
    assert node.service.store.lane(LANE).status is None
    assert node.service.status(LANE)["session_expires_at"] == "2027-01-01T00:00:00+00:00"


async def test_a_visit_now_runs_even_when_keepalive_is_off_but_never_past_a_gate(node):
    save_config(node.cfg.model_copy(update={"keepalive": False}))
    node.service.set_opt_out(LANE, True)
    done = await node.service.visit_now(LANE)
    assert done["trigger"] == "now" and done["outcome"] == "ok"
    await node.lanes.stop_agent(LANE, by="owner")
    with pytest.raises(InvalidRequest, match="held off"):
        await node.service.visit_now(LANE)
    node.lanes.memory_status = lambda: {"available_mb": 100, "reserved_mb": 0, "floor_mb": 800}
    with pytest.raises(InvalidRequest, match="memory"):
        await node.service.visit_now("biz-a/other", wait_s=0)


async def test_the_ops_socket_serves_the_plan_and_a_visit_now(node):
    health = SimpleNamespace(runtime=node.service.runtime)
    node.service.runtime.register("keepalive", node.service)
    plan = await control.dispatch(health, {"op": "keepalive", "lane": LANE})
    assert plan["ok"] and plan["keepalive"]["lane"] == LANE and plan["keepalive"]["enabled"]
    done = await control.dispatch(health, {"op": "keepalive", "lane": LANE, "now": True})
    assert done["visit"]["outcome"] == "ok" and done["keepalive"]["last_visit"]["trigger"] == "now"
    assert (await control.dispatch(health, {"op": "keepalive"}))["ok"] is False


# ---- the owner's switches -------------------------------------------------------------------------

def test_keepalive_is_off_by_default_and_the_cli_switches_it(tmp_path, monkeypatch):
    from agentos.cli import app

    assert NodeConfig().keepalive is False
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    save_config(NodeConfig(home=tmp_path, mcp_token="t"))
    runner = CliRunner()
    result = runner.invoke(app, ["lane", "keepalive", "--node", "on"])
    assert result.exit_code == 0, result.output
    assert load_config(tmp_path).keepalive is True
    result = runner.invoke(app, ["lane", "keepalive", LANE, "--off"])
    assert result.exit_code == 1 and "no lane" in result.output  # a typo makes no lane directory
    lane_dir = load_config(tmp_path).lane_dir(LANE)
    lane_dir.mkdir(parents=True)
    result = runner.invoke(app, ["lane", "keepalive", LANE, "--off"])
    assert result.exit_code == 0 and "opted out" in result.output
    assert (lane_dir / "keepalive-off").exists()  # the lane's own record, which outlives keepalive.db
    store = KeepaliveStore(tmp_path)
    assert store.lane(LANE).opted_out
    store.close()
    result = runner.invoke(app, ["lane", "keepalive", LANE])  # agentosd is not running here
    assert result.exit_code == 0 and "opted out" in result.output and "not running" in result.output
    result = runner.invoke(app, ["lane", "keepalive", LANE, "--now"])
    assert result.exit_code == 1 and "not running" in result.output
    assert "—" not in result.output


def test_the_doctor_says_whether_keepalive_is_on(tmp_path):
    from agentos.ops import doctor

    off = doctor.check_keepalive(doctor.DoctorContext(home=tmp_path, config=NodeConfig(home=tmp_path)))
    assert off[0].status == doctor.PASS and "off (the default)" in off[0].detail
    on = doctor.check_keepalive(doctor.DoctorContext(home=tmp_path, config=NodeConfig(home=tmp_path,
                                                                                      keepalive=True)))
    assert on[0].detail.startswith("on:")


def test_status_text_reads_plainly():
    from agentos.keepalive.cli import format_status

    lines = format_status({"lane": LANE, "enabled": True, "opted_out": False, "site": "Instagram",
                           "home": "https://www.instagram.com/", "window_today": ["07:40", "23:10"],
                           "timezone": "Europe/Vilnius", "timezone_source": "pin", "blocked": None,
                           "next_at": "2026-11-06T13:22:41+00:00", "activity_at": "2026-11-03T09:00:00+00:00",
                           "visits": [{"at": "2026-11-01T10:00:00+00:00", "trigger": "planned", "outcome": "ok",
                                       "scrolls": 5, "opened_item": True, "session": "refreshed"}]})
    text = "\n".join(lines)
    assert "next visit:  2026-11-06 13:22 UTC (Fri 15:22 Europe/Vilnius)" in text
    assert "ok, 5 scrolls, opened one item, session refreshed" in text
    assert "—" not in text


def test_act_kinds_the_visit_uses_are_reads():
    assert visit.DRIVER.ceiling == "read" and not visit.DRIVER.human
    assert not visit.DRIVER.allows("draft") and visit.DRIVER.allows("read")
    assert ActionKind.SCROLL.value == "scroll"


class FeedInteraction:
    """An Instagram-shaped feed in the outline format the node's snapshot writes (agentos.node.snapshot)."""

    OUTLINE = "\n".join([
        '- main',
        '  - link "Home" [ref=e1]',
        '    - /url: /',
        *[line for i in range(2, 12) for line in (
            f'  - article "Post {i}"',
            f'    - link "Post {i}" [ref=e{i}]',
            f'      - /url: /p/Post{i}x/',
            f'    - button "Like" [ref=e{i + 20}]')],
    ])

    def __init__(self):
        self.calls = []
        self.url = "about:blank"

    async def navigate(self, page, url):
        self.calls.append(("navigate", url))
        self.url = url
        return NavigateResult(url=url, title="Instagram", signal=Signal.OK)

    async def snapshot(self, page):
        from agentos.core.models import SnapshotResult

        return SnapshotResult(url=self.url, title="Instagram", text=self.OUTLINE, ref_count=31)

    async def act(self, page, ref, action, text=None):
        from agentos.core.models import ActionResult

        self.calls.append((action.value, ref))
        if action is ActionKind.CLICK:
            self.url = "https://www.instagram.com/p/x/"
        return ActionResult(ok=True, action=action, ref=ref, url=self.url, signal=Signal.OK)


async def test_a_visit_may_open_one_post_and_come_back_and_never_presses_like():
    feed = FeedInteraction()
    pace = visit.Pace(first_look=(0, 0), between=(0, 0), item_look=(0, 0), last_look=(0, 0), open_item=1.0,
                      item_scrolls=(1, 1))
    out = await visit.run(object(), feed, TABLE["instagram"], pace=pace, r=random.Random(7), sleep=_no_sleep)
    assert out.outcome == "ok" and out.opened_item and out.scrolls >= 2
    opened = [url for kind, url in feed.calls if kind == "navigate"][1:-1]
    assert len(opened) == 1 and re.fullmatch(r"https://www\.instagram\.com/p/Post\d+x/", opened[0])
    assert feed.calls[0] == ("navigate", "https://www.instagram.com/")
    assert feed.calls[-1] == ("navigate", "https://www.instagram.com/")  # back to the feed (no history here)
    assert {kind for kind, _ in feed.calls} <= {"navigate", "scroll"}  # nothing is ever pressed


# ---- review findings: each test failed before its fix ---------------------------------------------

async def test_the_effect_route_lasts_one_visit_and_judges_only_writes_and_commits(tmp_path):
    """1. On a lane that is not strict, a refused write is aborted before it leaves while the visit
    runs; other requests pass the route, and outside the visit there is no route at all."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=visit.DRIVER)
    assert ctx.routes == []
    async with guard.acting(route=True):
        assert ctx.routes == []  # an agent's act on a lane that is not strict: unchanged
    headers = {"X-FB-Friendly-Name": "usePolarisLikeMediaXIGLikeMutation"}
    async with guard.acting(route=True, effects_route=True):
        [(pattern, handler)] = ctx.routes  # the commit gate's route, in its effects mode
        assert pattern == guard.gate_route()
        like, feed, page = FakeRoute(), FakeRoute(), FakeRoute()
        await handler(like, FakeRequest("https://www.instagram.com/api/graphql", "POST", headers=headers))
        await handler(feed, FakeRequest("https://www.instagram.com/api/graphql", "POST",
                                        headers={"X-FB-Friendly-Name": "PolarisFeedQuery"}))
        await handler(page, FakeRequest("https://www.instagram.com/", nav=True))
    assert (like.aborted, feed.continued, page.continued) == ("blockedbyclient", 1, 1)
    assert [(b.rule, b.prevented) for b in guard.blocks_since(0)] == [("write", True)]
    assert ctx.routes == []


async def test_a_visit_ends_a_drive_and_never_asks_the_commit_gate(tmp_path):
    """A drive left on by an agent's act ends when a visit begins: a publish the page sends during
    the visit is refused by the read-only grant and aborted, and no gate is asked (no approval)."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=visit.DRIVER)
    asked = []

    class Gate:
        submit_like = False

        async def hold(self, rule, request, url, method):
            asked.append(rule.id)

    async with guard.acting(route=True, gate=Gate()):
        assert guard.driving()
    async with guard.acting(route=True, effects_route=True):
        assert not guard.driving()
        [(_, handler)] = ctx.routes
        post = FakeRoute()
        await handler(post, FakeRequest("https://i.instagram.com/api/v1/media/configure/", "POST"))
    assert post.aborted == "blockedbyclient"
    assert asked == []
    assert [(b.rule, b.prevented) for b in guard.blocks_since(0)] == [("commit", True)]
    assert ctx.routes == []


async def test_a_post_link_is_opened_by_its_address_never_pressed():
    """2. A link with a Like nested inside it is opened by loading its address through the guard's URL
    check: no press can land on the nested control, and there is no click whose pre-click read the
    guard could skip."""
    assert not hasattr(visit.ReadOnly, "click")
    text = """\
- main
  - link "A post" [ref=e2]
    - /url: /p/Cx1y2Z3/
    - button "Like" [ref=e3]"""
    calls, checked = [], []

    class Recording:
        async def navigate(self, page, url):
            calls.append(("navigate", url))
            return NavigateResult(url=url, title="Instagram", signal=Signal.OK)

        async def act(self, page, ref, action, text=None):
            calls.append((action.value, ref))

    guard = SimpleNamespace(check_url=checked.append)
    ro = visit.ReadOnly(Recording(), None, TABLE["instagram"], guard)
    ro.nodes, ro.url = {n.ref: n for n in visit.outline_nodes(text)}, "https://www.instagram.com/"
    await ro.open_item("e2")
    with pytest.raises(EgressRefused, match="read-only"):
        await ro.open_item("e3")
    assert calls == [("navigate", "https://www.instagram.com/p/Cx1y2Z3/")]
    assert checked == ["https://www.instagram.com/p/Cx1y2Z3/"]


class WrongAccount(FeedInteraction):
    def __init__(self, status="wrong_account"):
        super().__init__()
        self.status = status

    async def navigate(self, page, url):
        await super().navigate(page, url)
        return NavigateResult(url=url, title="Feed", signal=Signal.OK, account={
            "site": "social", "user_id": "2", "status": self.status, "expected": {"user_id": "1", "handle": "alice"}})


async def test_a_wrong_account_holds_the_lane_stays_marked_and_tells_the_owner_once(node):
    """3. The lane is held with a plain reason; later rounds keep the mark and send nothing more."""
    node.service.runtime.register("interaction", WrongAccount())
    node.service.set_opt_out("biz-a/other", True)
    out = await due_round(node)
    assert out["visited"]["outcome"] == "wrong_account"
    assert "signed in as user 2" in out["visited"]["reason"] and "alice (1)" in out["visited"]["reason"]
    control = node.lanes.get(LANE).control
    assert control.state == ControlState.PAUSED and control.reason.startswith("needs_human:wrong_account")
    assert [n["type"] for n in node.notes.sent] == ["keepalive.wrong_account"]
    assert "wrong account" in node.notes.sent[0]["title"] and "—" not in node.notes.sent[0]["body"]
    for minutes in (2, 60 * 24 * 5):
        node.clock.at += timedelta(minutes=minutes)
        await node.service.run_round()
        assert node.service.store.lane(LANE).status == "wrong_account"
    assert len(node.notes.sent) == 1
    assert node.service.status(LANE)["blocked"]["code"] == "wrong_account"
    await node.lanes.hand_back(LANE, by="owner")  # a human looked; the next visit checks the identity
    await node.lanes.close(LANE)
    node.service._set_not_before(node.clock.at + timedelta(hours=1))  # this round plans, and visits nothing
    out = await node.service.run_round()
    assert "visited" not in out and node.service.status(LANE)["blocked"] is None
    assert node.service.store.lane(LANE).status == "wrong_account"  # a round with no block never clears it
    node.service.runtime.register("interaction", WrongAccount("match"))
    done = await node.service.visit_now(LANE)
    assert done["outcome"] == "ok" and done["identity"] == "match"
    assert node.service.store.lane(LANE).status is None


class ViewerOpens(FeedInteraction):
    """Someone opens a live view (view only) while the visit runs."""

    def __init__(self, lanes):
        super().__init__()
        self.lanes = lanes

    async def navigate(self, page, url):
        if not self.calls:
            self.lanes.view_opened(LANE)
        return await super().navigate(page, url)


async def test_a_lane_someone_looks_at_is_never_closed_after_the_visit(node):
    """4. A live view opened during the visit (and one open now) keeps the lane warm."""
    node.service.runtime.register("interaction", ViewerOpens(node.lanes))
    done = await node.service.visit_now(LANE)
    assert done["outcome"] == "ok"
    assert node.lanes.get(LANE).state == LaneState.WARM and ("close", LANE) not in node.lanes.calls
    node.lanes.view_closed(LANE)
    node.lanes.viewer_attached("biz-a/other")  # a person on the live view of a cold lane, as the visit starts
    node.service.runtime.register("interaction", FeedInteraction())
    node.clock.at = node.service._not_before + timedelta(seconds=1)
    done = await node.service.visit_now("biz-a/other")
    assert done["outcome"] == "ok" and node.lanes.get("biz-a/other").state == LaneState.WARM


async def test_a_take_over_stops_the_visit_at_once_and_nothing_acts_after_it(node):
    """4. A human takes the lane over in the middle of a long look: the visit ends at once (the
    rest is interrupted) and no step follows."""
    feed = FeedInteraction()
    node.service.runtime.register("interaction", feed)
    node.service.sleep = asyncio.sleep
    node.service.pace = visit.Pace(first_look=(3600, 3600), max_seconds=7200)
    task = asyncio.create_task(node.service.visit_now(LANE))
    for _ in range(200):
        if feed.calls:
            break
        await asyncio.sleep(0.01)
    assert feed.calls == [("navigate", visit_home(node))]
    await node.lanes.take_control(LANE, by="owner")
    done = await asyncio.wait_for(task, 2)
    assert done["outcome"] == "stopped" and "human took over" in done["reason"]
    assert len(feed.calls) == 1
    assert node.lanes.get(LANE).state == LaneState.WARM  # left to the human who has it

    class Gate:
        state = ControlState.AGENT

    gate = Gate()
    ro = visit.ReadOnly(feed, None, TABLE["instagram"], may_act=lambda: gate.state == ControlState.AGENT)
    gate.state = ControlState.PAUSED
    with pytest.raises(visit._Stop):
        await ro.open("https://www.instagram.com/")
    assert len(feed.calls) == 1


def visit_home(n) -> str:
    return entry_for(n.lanes.get(LANE).spec).home


async def test_a_failed_attempt_backs_off_briefly_and_is_not_activity(node, monkeypatch):
    """5. An attempt that failed (the lane was busy; the feed did not load, though the lane manager
    stamped last_used for it) leaves the plan's activity as it was and retries in 30 to 90 minutes."""
    from contextlib import asynccontextmanager

    from agentos.core.errors import EngineError

    before = node.service.status(LANE)

    @asynccontextmanager
    async def busy(lanes, lane_id):
        raise EngineError("lane busy: a claim holds it")
        yield  # pragma: no cover

    monkeypatch.setattr(visit, "lane_session", busy)
    node.service.set_opt_out("biz-a/other", True)
    out = await due_round(node)
    assert out["visited"]["outcome"] == "failed"
    plan = node.service.status(LANE)
    assert plan["activity_at"] == before["activity_at"]
    finished = datetime.fromisoformat(out["visited"]["finished_at"])
    retry = datetime.fromisoformat(plan["retry_at"])
    assert timedelta(minutes=30) <= retry - finished <= timedelta(minutes=90)
    assert datetime.fromisoformat(plan["next_at"]) >= retry
    assert datetime.fromisoformat(plan["next_at"]) - finished < timedelta(hours=24)  # not a cadence of days
    monkeypatch.undo()

    class NoLoad(FeedInteraction):
        async def navigate(self, page, url):
            return NavigateResult(url=url, title="", signal=Signal.UNKNOWN)

    import agentos.dev.lanes as dev_lanes

    monkeypatch.setattr(dev_lanes, "utcnow", lambda: datetime.fromtimestamp(node.clock(), UTC))
    node.service.runtime.register("interaction", NoLoad())
    node.clock.at = max(datetime.fromisoformat(plan["next_at"]), node.service._not_before) + timedelta(seconds=1)
    out = await node.service.run_round()
    assert out["visited"]["outcome"] == "failed"
    assert node.lanes.get(LANE).last_used is not None  # stamped by the lane manager for the attempt
    assert node.service.status(LANE)["activity_at"] == before["activity_at"]


async def test_a_visit_cut_by_a_restart_counts_and_the_node_gap_survives_it(node):
    """6. A started row agentosd did not finish counts as a visit (hours before another), and the
    node-wide gap is read back after a restart."""
    from agentos.keepalive.store import Visit as Row

    now = node.service.now()
    node.service.store.start_visit(Row(lane_id=LANE, started_at=now, finished_at=now, trigger="planned",
                                       site="social.example", outcome="started"))
    node.service._set_not_before(now + timedelta(minutes=17))
    node.service.store.close()
    again = KeepaliveService(node.service.runtime, home=node.home, clock=node.clock, pace=FAST,
                             rng=random.Random(6), sleep=_no_sleep)
    try:
        assert again._not_before == (now + timedelta(minutes=17)).replace(microsecond=0)
        assert again.store.last_visit(LANE).outcome == "interrupted"
        plan = again.status(LANE)
        assert plan["activity_at"] == now.isoformat(timespec="seconds")
        assert datetime.fromisoformat(plan["next_at"]) >= now + planner.RESTART_GAP
        node.clock.at = now + timedelta(hours=1)
        assert "visited" not in await again.run_round()
    finally:
        again.store.close()
        node.service.store = KeepaliveStore(node.home)


class LoggedOutPage(FeedInteraction):
    async def navigate(self, page, url):
        return NavigateResult(url="https://social.example/login", title="Login", signal=Signal.LOGGED_OUT,
                              signal_detail="login URL")


async def test_a_signed_out_visit_tells_the_owner_once_with_the_right_reason(node):
    """7. The visit's notice is the only one: the next round calls the held lane signed out too."""
    node.service.runtime.register("interaction", node.lanes.watch(LoggedOutPage()))
    node.service.set_opt_out("biz-a/other", True)
    out = await due_round(node)
    assert out["visited"]["outcome"] == "signed_out"
    for minutes in (2, 60):
        node.clock.at += timedelta(minutes=minutes)
        await node.service.run_round()
    assert [n["type"] for n in node.notes.sent] == ["keepalive.signed_out"]
    assert node.service.store.lane(LANE).status == "signed_out"
    assert node.service.status(LANE)["blocked"]["code"] == "signed_out"


async def test_a_visit_cut_by_its_time_limit_is_cut_and_clears_no_mark(node):
    """8. Cut before the feed loaded: retried soon. Cut after it loaded: a visit. Neither clears a mark."""

    class Slow(FeedInteraction):
        async def navigate(self, page, url):
            await asyncio.sleep(0.5)
            return await super().navigate(page, url)

    node.service.store.set_status(LANE, "challenged", "a test mark", notified=True)
    node.service.runtime.register("interaction", Slow())
    node.service.sleep = asyncio.sleep
    node.service.pace = visit.Pace(first_look=(1, 1), max_seconds=0.2)
    done = await node.service.visit_now(LANE)
    assert done["outcome"] == "cut" and not done["loaded"]
    assert node.service.status(LANE)["retry_at"] is not None
    node.service.runtime.register("interaction", FeedInteraction())
    node.clock.at = node.service._not_before + timedelta(seconds=1)
    done = await node.service.visit_now(LANE)
    assert done["outcome"] == "cut" and done["loaded"]
    assert node.service.status(LANE)["retry_at"] is None  # it counts
    assert node.service.store.lane(LANE).status == "challenged"


async def test_a_visit_now_checks_for_room_again_under_the_lock_and_never_evicts(node):
    """9. Room was free when --now asked, then gone by the time the planned visit's lock came free."""
    async with node.service._lock:
        task = asyncio.create_task(node.service.visit_now(LANE, wait_s=0))
        await settle()
        node.lanes.memory_status = lambda: {"available_mb": 100, "reserved_mb": 0, "floor_mb": 800}
    with pytest.raises(InvalidRequest, match="memory"):
        await task
    assert node.lanes.principals == [] and not [c for c in node.lanes.calls if c[0] == "close"]


def test_an_overdue_lane_goes_at_once_while_awake_else_soon_after_it_wakes():
    """10. A lane due yesterday is not left for today's random slot."""
    now = datetime(2026, 11, 4, 9, 30, tzinfo=VILNIUS).astimezone(UTC)
    due = datetime(2026, 11, 3, 9, 0, tzinfo=VILNIUS).astimezone(UTC)
    for i in range(300):
        seed = f"{i:032x}"
        persona = draw_persona(f"lane{i}", seed)
        at = planner.next_visit(seed, persona, VILNIUS, due, now)
        start, end = planner.window(persona, now.astimezone(VILNIUS).date(), VILNIUS)
        if start <= now < end - planner.VISIT_ROOM:
            assert at == now
        else:
            awake = next(s for _d, s, e in planner._windows(persona, VILNIUS, now) if now < e)
            assert timedelta(minutes=2) <= at - max(awake, now) <= timedelta(minutes=12)


async def test_the_opt_out_lives_with_the_lane_and_a_lost_database_plans_nothing_until_rebuilt(node):
    """11. keepalive.db goes missing: a round plans nothing, rebuilds it, keeps the lane's opt-out,
    and waits a gap before the first visit."""
    node.service.set_opt_out("biz-a/other", True)
    assert (node.cfg.lane_dir("biz-a/other") / "keepalive-off").exists()
    node.clock.at += timedelta(days=10)
    db = node.service.store.path
    for path in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        path.unlink(missing_ok=True)
    out = await node.service.run_round()
    assert "visited" not in out and "rebuilt" in out["store"]
    assert node.lanes.principals == []
    assert node.service.status("biz-a/other")["opted_out"] is True
    assert node.service._not_before > node.service.now()
    node.clock.at = node.service._not_before + timedelta(seconds=1)
    out = await node.service.run_round()
    assert out["visited"]["outcome"] == "ok" and out["due"] == [LANE]  # the lane that did not opt out
    assert node.lanes.principals == ["node"]


def test_the_planner_skips_a_lane_an_agent_drives():
    driven = _block(info(), driven=True)
    assert driven.code == "driven" and driven.waits and not driven.notify
    assert _block(info()) is None


async def test_a_lane_an_agent_drives_is_not_visited_by_a_round_or_now(node):
    node.lanes.driven = lambda lane_id: lane_id == LANE  # an agent acted and control is still its own
    plan = node.service.plan(node.lanes.get(LANE), node.service.now())
    assert plan["blocked"]["code"] == "driven" and plan["_at"] is not None
    node.clock.at = max(plan["_at"], node.clock.at)
    await node.service.run_round()
    assert node.service.store.visits(LANE, 5) == []
    with pytest.raises(InvalidRequest, match="is driving"):
        await node.service.visit_now(LANE)
    assert node.service.store.visits(LANE, 5) == []


# ---- review findings: a visit and an agent's drive -------------------------------------------------

CONFIGURE = "https://i.instagram.com/api/v1/media/configure/"
UNKNOWN_PUT = "https://i.instagram.com/api/v1/media/123/unknown_mutation/"


async def _visit(ctx, *, interaction=None, may_act=lambda: True):
    """One visit on a context whose guard may hold an agent's drive (the visit's page is the context)."""
    return await visit.run(SimpleNamespace(context=ctx), interaction or FeedInteraction(), TABLE["instagram"],
                           pace=FAST, r=random.Random(3), sleep=_no_sleep, may_act=may_act)


async def test_a_visit_that_ends_an_agents_drive_puts_it_back_before_control_returns(tmp_path):
    """Defence in depth: the visit ends the drive (acting(effects_route=True)), and its end binds the
    drive's gate route again, so a publish the page sends next meets the gate, not the listener alone."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    gate = Gate()
    async with guard.acting(gate=gate):
        pass
    await _visit(ctx)
    assert guard.driving() and guard.driver == COMMITTER
    [(pattern, handler)] = ctx.routes
    assert pattern == guard.gate_route()
    route = FakeRoute()
    await handler(route, FakeRequest(CONFIGURE, "POST"))
    assert route.aborted == "blockedbyclient" and gate.seen == ["instagram.post"]


async def test_a_visit_does_not_put_a_drive_back_while_control_is_not_the_agents(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    async with guard.acting(gate=Gate()):
        pass
    await _visit(ctx, may_act=lambda: False)  # a take over or a hold: the visit stops before a step
    assert not guard.driving() and ctx.routes == []


async def test_a_publish_held_between_an_agents_acts_is_listed_by_its_next_act_after_a_visit(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    async with guard.acting(gate=Gate()):
        pass
    [(_, handler)] = ctx.routes
    await handler(FakeRoute(), FakeRequest(CONFIGURE, "POST"))  # a page's own timer, between the agent's acts
    await _visit(ctx)
    async with guard.acting(gate=Gate()) as mark:
        reported = [b.rule for b in guard.blocks_since(mark)]
    assert reported == ["commit_gate"]


async def test_a_visit_aborts_each_write_the_gate_would_hold_and_lets_reads_go(tmp_path):
    """The visit reads a lane with the gate's own classification: a PUT, a PATCH or a POST that no rule
    names is a possible publish and is aborted, as a named write is; a feed read goes."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=visit.DRIVER)
    async with guard.acting(route=True, effects_route=True):
        [(_, handler)] = ctx.routes
        put, patch, seen, feed = FakeRoute(), FakeRoute(), FakeRoute(), FakeRoute()
        await handler(put, FakeRequest(UNKNOWN_PUT, "PUT"))
        await handler(patch, FakeRequest("https://www.instagram.com/api/v1/media/123/", "PATCH"))
        await handler(seen, FakeRequest("https://www.instagram.com/api/v1/seen/", "POST"))
        await handler(feed, FakeRequest("https://www.instagram.com/api/graphql", "POST",
                                        headers={"X-FB-Friendly-Name": "PolarisFeedQuery"}))
    assert (put.aborted, patch.aborted, seen.aborted) == ("blockedbyclient",) * 3
    assert feed.continued == 1
    assert [(b.rule, b.effect_rule) for b in guard.blocks_since(0)] == [("commit", "instagram.unknown")] * 3


async def test_a_visit_whose_effect_route_cannot_be_bound_does_not_run(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=visit.DRIVER)
    guard.dispatcher = FailingDispatcher()
    interaction = FeedInteraction()
    out = await _visit(ctx, interaction=interaction)
    assert out.outcome == "failed" and "route" in out.reason
    assert interaction.calls == [] and ctx.routes == []



async def test_a_lane_with_a_posting_pace_is_visited_only_in_its_active_hours(node, tmp_path):
    """AUD-13: a session at 3 am lane time is a signal too, so a visit keeps to the pace's hours."""
    from test_policy_support import make_service

    from agentos.policy import pace as pacing

    svc, _ = make_service(tmp_path / "policy", clock=node.clock)
    node.service.runtime.register("policy", svc)
    try:
        lane = node.lanes.get(LANE)
        plain = node.service.plan(lane, node.service.now())
        assert plain["active_hours"] is None and plain["timezone_source"] == "pin"
        svc.set_lane_pace(LANE, {"active_hours": ["03:00-04:00"], "timezone": "Asia/Tokyo"})
        plan = node.service.plan(lane, node.service.now())
        assert plan["timezone"] == "Asia/Tokyo" and plan["timezone_source"] == "lane"
        assert plan["active_hours"] == ["03:00-04:00"]
        tokyo = plan["_at"].astimezone(ZoneInfo("Asia/Tokyo"))
        assert tokyo.hour == 3 and pacing.is_open(pacing.Pace(active_hours=["03:00-04:00"]).windows(),
                                                   ZoneInfo("Asia/Tokyo"), plan["_at"].timestamp())
    finally:
        svc.close()
