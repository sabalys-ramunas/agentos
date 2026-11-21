""""Ask me first" at the egress guard: browser_act is a draft call, so the guard is what holds a click
on Post to the switch. It covers requests EFFECT_RULES recognise; a lane with the switch off behaves
exactly as before; a person in control, a routine and an approved dialog are not held by it."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_egress import AGENT, OPEN_AGENT, FakeMeter, FakeRequest, FakeRoute, TargetPage, make_guard, settle
from test_policy_support import Clock, make_service

from openajar.core.errors import EgressRefused
from openajar.core.models import ActionKind, Effect
from openajar.core.runtime import Runtime
from openajar.node import egress, flow_driver
from openajar.node import tools as node_tools
from openajar.node.egress import Driver, classify, judge
from openajar.node.tools import session as tool_session

PUBLISH = "https://i.instagram.com/api/v1/media/configure/"
LANE = "biz-a/instagram"
POSTER = Driver(kind="agent", id="a1", grant_id="g1", ceiling="commit")  # may publish, by its grant
FORM = {"targets": [{"kind": "form", "url": PUBLISH, "method": "post"}]}


def guarded(tmp_path, *, on=True, sites=None, strict=False, driver=POSTER):
    kw = {} if sites is None else {"sites": sites}
    guard, ctx, events, clock = make_guard(tmp_path, driver=driver, strict=strict, **kw)
    guard.asks_first = lambda: on
    held: list = []
    told: list = []
    guard.on_violation = held.append
    guard.on_ask_first = told.append
    return SimpleNamespace(guard=guard, ctx=ctx, events=events, clock=clock, held=held, told=told)


def verdict(driver=POSTER, *, on=True, url=PUBLISH, method="POST", human=False):
    return judge(url, method, navigation=False, sites=(), driver=driver, human=human,
                 effect=classify(url, method), asks_first=lambda: on)


def test_the_rule_refuses_an_agents_commit_only_when_the_switch_is_on():
    refusal = verdict()
    assert refusal is not None and refusal.rule == "ask_first"
    assert "asks a person first" in refusal.reason and "routine" in refusal.reason and "human_ask" in refusal.reason
    assert "—" not in refusal.reason
    assert verdict(on=False) is None
    assert verdict(url="https://www.instagram.com/graphql/query", method="GET") is None  # a read


def test_a_person_is_not_held_and_a_low_ceiling_keeps_its_own_rule():
    assert verdict(human=True) is None  # a taken-over lane
    assert verdict(Driver(kind="panel", id="owner")) is None
    assert verdict(Driver(kind="human", id="owner")) is None
    low = verdict(AGENT)
    assert low is not None and low.rule == "commit"


async def test_the_listener_records_a_publish_that_left_and_holds_the_lane(tmp_path):
    g = guarded(tmp_path)
    g.ctx.emit("request", FakeRequest(PUBLISH, "POST"))
    await settle()
    [block] = g.guard.blocks_since(0)
    assert block.rule == "ask_first" and block.prevented is False and block.effect_rule
    assert [b.rule for b in g.held] == ["ask_first"]
    assert [b.seq for b in g.told] == [block.seq]
    assert g.guard.stats["observed_commits"] == 0


async def test_check_action_refuses_before_the_click_with_or_without_a_host_list(tmp_path):
    for sites in (None, ()):
        g = guarded(tmp_path, sites=sites)
        page = TargetPage(FORM)
        with pytest.raises(EgressRefused, match="asks a person first.*nothing was sent") as refused:
            await g.guard.check_action(page, "e3", ActionKind.CLICK, None)
        assert "—" not in refused.value.message and "Do not retry" not in refused.value.message
        [block] = g.guard.blocks_since(0)
        assert block.rule == "ask_first" and block.prevented and block.kind == "action"
        assert g.held == [] and len(g.told) == 1  # nothing left: no hold, and a person still hears


async def test_a_lane_that_only_asks_first_refuses_nothing_else_before_the_click(tmp_path):
    g = guarded(tmp_path, sites=(), driver=POSTER)
    for target in ({"kind": "unreadable", "why": "a frame"}, {"kind": "link", "url": "javascript:void(0)"},
                   {"kind": "link", "url": "https://evil.example/"},
                   {"kind": "form", "url": "https://www.instagram.com/search", "method": "get"}):
        await g.guard.check_action(TargetPage({"targets": [target]}), "e1", ActionKind.CLICK, None)
    assert g.guard.blocks_since(0) == []


async def test_an_action_runs_under_the_route_so_a_script_publish_never_leaves(tmp_path):
    g = guarded(tmp_path)
    assert not g.guard.strict and g.ctx.routes == []
    async with g.guard.acting() as mark:
        [(pattern, handler)] = g.ctx.routes
        assert pattern == "**/*"
        publish, read = FakeRoute(), FakeRoute()
        await handler(publish, FakeRequest(PUBLISH, "POST"))
        await handler(read, FakeRequest("https://www.instagram.com/feed", "GET"))
    assert publish.aborted == "blockedbyclient" and read.continued == 1 and read.aborted is None
    assert g.ctx.routes == []
    [block] = g.guard.blocks_since(mark)
    assert block.rule == "ask_first" and block.prevented


async def test_a_publish_the_switch_refuses_on_the_route_is_never_charged(tmp_path):
    """Both guard the same request: the switch answers first, so a refused publish debits nothing."""
    g = guarded(tmp_path)
    meter = FakeMeter(over="Lane biz-a/instagram reached its limit of 1 commit a day.")
    async with g.guard.acting(meter=meter) as mark:
        [(_, handler)] = g.ctx.routes
        route = FakeRoute()
        await handler(route, FakeRequest(PUBLISH, "POST"))
    assert route.aborted == "blockedbyclient" and meter.charged == []
    assert [b.rule for b in g.guard.blocks_since(mark)] == ["ask_first"]


async def test_a_publish_the_switch_refuses_after_it_left_is_never_charged(tmp_path):
    g = guarded(tmp_path)
    meter = FakeMeter()
    async with g.guard.acting(route=False, meter=meter) as mark:
        g.ctx.emit("request", FakeRequest(PUBLISH, "POST"))
        await settle()
    assert meter.charged == [] and [b.rule for b in g.held] == ["ask_first"]
    assert [b.rule for b in g.guard.blocks_since(mark)] == ["ask_first"]


async def test_with_the_switch_off_the_meter_still_stops_a_publish_past_a_limit(tmp_path):
    g = guarded(tmp_path, on=False)
    meter = FakeMeter(over="Lane biz-a/instagram reached its limit of 1 commit a day.", capped=True)
    async with g.guard.acting(meter=meter) as mark:
        [(_, handler)] = g.ctx.routes
        route = FakeRoute()
        await handler(route, FakeRequest(PUBLISH, "POST"))
    assert route.aborted == "blockedbyclient"
    assert [b.rule for b in g.guard.blocks_since(mark)] == ["limit_reached"]


async def test_a_dialog_a_person_approved_is_still_charged_and_still_stops_at_a_limit(tmp_path):
    """A person's approval answers the switch. It does not lift a limit (ROUTING.md, step 5)."""
    g = guarded(tmp_path)
    within, over = FakeMeter(), FakeMeter(over="Lane biz-a/instagram reached its limit of 1 commit a day.")
    async with g.guard.acting(meter=within):
        g.guard.person_approved()
        [(_, handler)] = g.ctx.routes
        route = FakeRoute()
        await handler(route, FakeRequest(PUBLISH, "POST"))
    assert route.continued == 1 and route.aborted is None and len(within.charged) == 1
    async with g.guard.acting(meter=over) as mark:
        g.guard.person_approved()
        [(_, handler)] = g.ctx.routes
        route = FakeRoute()
        await handler(route, FakeRequest(PUBLISH, "POST"))
    assert route.aborted == "blockedbyclient"
    assert [b.rule for b in g.guard.blocks_since(mark)] == ["limit_reached"]


async def test_a_lane_with_the_switch_off_behaves_exactly_as_before(tmp_path):
    g = guarded(tmp_path, on=False)
    page = TargetPage(FORM)
    await g.guard.check_action(page, "e3", ActionKind.CLICK, None)  # the lane's list reads the target, as before
    async with g.guard.acting():
        assert g.ctx.routes == []  # not strict: no route
        g.ctx.emit("request", FakeRequest(PUBLISH, "POST"))
    await settle()
    assert g.guard.blocks_since(0) == [] and g.held == [] and g.told == []
    assert g.guard.stats["observed_commits"] == 1
    open_lane = guarded(tmp_path, on=False, sites=(), driver=OPEN_AGENT)
    unread = TargetPage(FORM)
    await open_lane.guard.check_action(unread, "e3", ActionKind.CLICK, None)
    assert unread.read == 0  # nothing to enforce: nothing read
    low = guarded(tmp_path, on=False, driver=AGENT)
    low.ctx.emit("request", FakeRequest(PUBLISH, "POST"))
    await settle()
    assert [b.rule for b in low.held] == ["commit"]  # a low ceiling is refused as it was


async def test_a_publish_no_rule_recognises_still_goes_through(tmp_path):
    g = guarded(tmp_path, sites=())
    unknown = "https://www.example.com/api/create_post"
    assert classify(unknown, "POST") is None
    await g.guard.check_action(TargetPage({"targets": [{"kind": "form", "url": unknown, "method": "post"}]}), "e1",
                               ActionKind.CLICK, None)
    async with g.guard.acting():
        [(_, handler)] = g.ctx.routes
        route = FakeRoute()
        await handler(route, FakeRequest(unknown, "POST"))
    assert route.aborted is None and route.continued == 1  # the limit ROUTING.md states
    assert g.guard.blocks_since(0) == []


async def test_a_person_in_control_of_the_lane_is_not_affected(tmp_path):
    g = guarded(tmp_path)
    g.guard.human_in_control = lambda: True
    await g.guard.check_action(TargetPage(FORM), "e3", ActionKind.CLICK, None)
    async with g.guard.acting():
        assert g.ctx.routes == []
    g.ctx.emit("request", FakeRequest(PUBLISH, "POST"))
    await settle()
    assert g.guard.blocks_since(0) == [] and g.held == [] and g.told == []


async def test_a_routine_on_the_lane_publishes(tmp_path):
    """It asked at its own effect; the last agent stays the driver, so this must be said out loud."""
    g = guarded(tmp_path)
    with g.guard.routine():
        g.ctx.emit("request", FakeRequest(PUBLISH, "POST"))
    await settle()
    assert g.guard.blocks_since(0) == [] and g.guard.stats["observed_commits"] == 1
    g.ctx.emit("request", FakeRequest(PUBLISH, "POST"))  # and only while it runs
    await settle()
    assert [b.rule for b in g.guard.blocks_since(0)] == ["ask_first"]


async def test_the_flow_driver_opens_the_lane_as_a_routine(tmp_path, monkeypatch):
    g = guarded(tmp_path)
    seen: list[int] = []

    class Lanes:
        @asynccontextmanager
        async def using(self, lane_id, principal_kind=None):
            yield SimpleNamespace()

    monkeypatch.setattr(flow_driver.egress, "guard_for", lambda page: g.guard)
    monkeypatch.setattr(flow_driver, "control_gate", lambda lanes, lane_id: lambda: None)
    monkeypatch.setattr(flow_driver, "PlaywrightFlowDriver", lambda *a, **k: seen.append(g.guard._routines))
    async with flow_driver.driver_factory(Runtime(lanes=Lanes()), interaction=object())(LANE):
        pass
    assert seen == [1] and g.guard._routines == 0


async def test_a_dialog_a_person_approved_sends_and_the_next_action_does_not_inherit_it(tmp_path):
    g = guarded(tmp_path)
    g.guard.person_approved()
    g.ctx.emit("request", FakeRequest(PUBLISH, "POST"))
    await settle()
    assert g.guard.blocks_since(0) == []
    async with g.guard.acting():
        assert g.ctx.routes  # the approval ended with the agent's next action
    g.guard.person_approved()
    g.clock.now += egress.APPROVED_WINDOW_S + 1
    g.ctx.emit("request", FakeRequest(PUBLISH, "POST"))
    await settle()
    assert [b.rule for b in g.guard.blocks_since(0)] == ["ask_first"]


async def test_a_switch_that_cannot_be_read_refuses_the_publish(tmp_path):
    g = guarded(tmp_path)

    def broken() -> bool:
        raise RuntimeError("database is locked")

    g.guard.asks_first = broken
    g.ctx.emit("request", FakeRequest(PUBLISH, "POST"))
    await settle()
    assert [b.rule for b in g.guard.blocks_since(0)] == ["ask_first"]


# ---- the real switch, and Needs you --------------------------------------------------------

@pytest.fixture
def svc(tmp_path):
    s, _ = make_service(tmp_path, clock=Clock())
    yield s
    s.close()


def test_the_guard_reads_the_lane_and_the_surfaces_that_post_through_it(svc):
    surfaces = SimpleNamespace(list=lambda: [SimpleNamespace(id="biz-a/page", lane=LANE)])
    runtime = Runtime(ask_first=svc.ask_first, surfaces=surfaces)
    assert not node_tools.lane_asks_first(runtime, LANE)
    svc.ask_first.set("lane", "biz-a/page", True, by="owner", person=True)
    assert node_tools.lane_asks_first(runtime, LANE)
    svc.ask_first.set("lane", "biz-a/page", False, by="owner", person=True)
    svc.ask_first.set("lane", LANE, True, by="agent:a1", person=False)
    assert node_tools.lane_asks_first(runtime, LANE)
    assert not node_tools.lane_asks_first(Runtime(), LANE)  # a node with no switches


async def test_driven_session_binds_the_switch_and_a_refusal_reaches_needs_you(tmp_path, svc, monkeypatch):
    g = guarded(tmp_path)
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    ctx = svc.context_for_token(tok, tool="browser_act")
    ctx.runtime = Runtime(ask_first=svc.ask_first, policy=svc, lanes=object())

    @asynccontextmanager
    async def session(*a, **k):
        yield object()

    monkeypatch.setattr(tool_session, "ctx_session", session)
    monkeypatch.setattr(egress, "guard_for", lambda page: g.guard)
    async with node_tools.driven_session(ctx, LANE):
        pass
    assert g.guard.driver.grant_id == ctx.grant.id
    await g.guard.check_action(TargetPage(FORM), "e3", ActionKind.CLICK, None)  # the real switch is off
    svc.ask_first.set("lane", LANE, True, by="owner", person=True)
    for _ in range(3):
        with pytest.raises(EgressRefused):
            await g.guard.check_action(TargetPage(FORM), "e3", ActionKind.CLICK, None)
    [card] = [a for a in svc.approvals.list(state="pending") if a.kind == "question"]  # one card, not three
    assert card.summary.startswith(f"An agent tried to publish on {LANE} by clicking")
    assert "stopped before it left" in card.summary and "asks you first" in card.summary
    assert "—" not in card.summary
