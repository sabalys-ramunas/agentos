"""The egress guard wired in: the lane manager (site lists in the store, control, holds),
workspace templates, and the browser tools (refusals before acting, blocks in results)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fakes import FakeInteraction, FakeLaneManager
from test_egress import FakeContext
from test_lanes import FakeEngine, FakeSession, make

from agentos.core.config import NodeConfig
from agentos.core.errors import EgressRefused, EngineError, InvalidRequest
from agentos.core.models import ActionKind, ControlState, Effect, LaneSpec
from agentos.core.runtime import Runtime
from agentos.node import egress
from agentos.node import tools as node_tools
from agentos.node.egress import Driver, EgressTrace, LaneGuard
from agentos.node.routes import RouteDispatcher
from agentos.workspaces.planner import make_plan
from agentos.workspaces.templates import parse_template

LANE = "biz-a/instagram"


# ---- the lane manager ------------------------------------------------------------------------

class GuardedEngine(FakeEngine):
    """FakeEngine whose sessions carry an egress guard, as CamoufoxEngine's do."""

    async def launch(self, spec, **kw):
        session: FakeSession = await super().launch(spec, **kw)
        session.guard = LaneGuard(spec.id, sites=spec.sites, strict=spec.egress_strict)
        return session


async def test_a_new_lane_has_no_site_list_and_set_sites_persists(tmp_path):
    mgr, *_ = make(tmp_path)
    info = await mgr.create(LaneSpec(id=LANE))
    assert info.spec.sites == [] and not info.spec.egress_strict
    info = await mgr.set_sites(LANE, ["Instagram.com", "*.instagram.com", "instagram.com"], strict=True)
    assert info.spec.sites == ["instagram.com", "*.instagram.com"] and info.spec.egress_strict
    assert mgr.store.require(LANE).spec.sites == ["instagram.com", "*.instagram.com"]
    [event] = [e for e in mgr.events(LANE) if e.kind == "sites_changed"]
    assert event.detail["to"] == ["instagram.com", "*.instagram.com"] and event.detail["strict"] is True
    with pytest.raises(InvalidRequest, match="empty list already means"):
        await mgr.set_sites(LANE, ["*"])
    info = await mgr.set_sites(LANE, [])
    assert info.spec.sites == [] and info.spec.egress_strict  # strict left as it was


async def test_a_running_lane_guard_follows_the_store_control_and_holds(tmp_path):
    mgr, engine, _, _ = make(tmp_path, engine=GuardedEngine(), self_check=False)
    await mgr.create(LaneSpec(id=LANE, sites=["*.instagram.com"]))
    async with mgr.using(LANE):
        pass
    guard: LaneGuard = engine.sessions[-1].guard
    assert guard.sites == ("*.instagram.com",) and not guard.human_in_control()

    await mgr.set_sites(LANE, ["*.instagram.com", "*.fbcdn.net"])
    assert guard.sites == ("*.instagram.com", "*.fbcdn.net")
    mgr.store.update_spec(mgr.store.require(LANE).spec.model_copy(update={"sites": ["instagram.com"]}))
    guard._settings_at = -1e9  # the CLI wrote the store; the guard rereads it after a moment
    assert guard.sites == ("instagram.com",)

    await mgr.take_control(LANE, by="owner")
    assert guard.human_in_control()
    await mgr.hand_back(LANE, by="owner")
    assert not guard.human_in_control()

    block = SimpleNamespace(rule="commit", host="i.instagram.com")
    await guard.on_violation(block)
    control = mgr.control(LANE)
    assert control.state == ControlState.PAUSED and control.reason == "egress:commit i.instagram.com"
    guard.events(LANE, "egress_blocked", {"rule": "lane_sites", "host": "evil.example"})
    assert any(e.kind == "egress_blocked" for e in mgr.events(LANE))


# ---- templates -------------------------------------------------------------------------------

TEMPLATE = """
title = "Shop"

[[surfaces]]
name = "store"
platform = "shopify"
sites = ["shop.example.com", "*.shopifycdn.com", "shop.example.com"]

[[surfaces]]
name = "blog"
platform = "blog"
"""


def test_a_template_surface_can_set_the_lane_site_list():
    tpl = parse_template("shop", TEMPLATE)
    assert tpl.surfaces[0].sites == ["shop.example.com", "*.shopifycdn.com"] and tpl.surfaces[1].sites is None
    plan = make_plan(tpl, ["biz-a"], proxies={"biz-a/store": None, "biz-a/blog": None}, existing={},
                     override=False, reason=None)
    assert [s.sites for s in plan.steps] == [["shop.example.com", "*.shopifycdn.com"], None]
    from agentos.workspaces.templates import TemplateInvalid

    with pytest.raises(TemplateInvalid, match="sites"):
        parse_template("bad", TEMPLATE.replace('"*.shopifycdn.com"', '"*"'))


# ---- the browser tools -----------------------------------------------------------------------

class Ctx:
    def __init__(self, runtime, *, grant=None, kind: str = "agent"):
        self.runtime = runtime
        self.principal = SimpleNamespace(kind=kind, id="claude")
        self.grant = grant
        self.traces: list[tuple[str, dict]] = []

    async def authorize(self, effect, **kw):
        return None

    def trace(self, kind, **fields):
        self.traces.append((kind, fields))


class StoppedInteraction(FakeInteraction):
    """navigate and act as a page whose own navigation the guard stopped (a redirect hop off-list)."""

    def __init__(self, guard: LaneGuard, *, fail: bool):
        super().__init__()
        self.guard, self.fail, self.navigated = guard, fail, []

    async def navigate(self, page, url):
        self.navigated.append(url)
        self.guard._record("https://evil.example/landing", "GET",
                           egress.Refusal("lane_sites", "evil.example is not on this lane's site list"),
                           kind="redirect", prevented=False)
        if self.fail:
            raise EngineError("navigation failed: NS_BINDING_ABORTED")
        return await super().navigate(page, url)


async def setup(tmp_path, *, sites=("*.instagram.com",), fail=True):
    manager = FakeLaneManager(NodeConfig(name="test-node", home=tmp_path))
    await manager.create(LaneSpec(id=LANE))
    async with manager.using(LANE) as page:
        pass
    guard = LaneGuard(LANE, sites=sites, trace=EgressTrace(tmp_path / "t.jsonl"))
    guard.dispatcher = RouteDispatcher(FakeContext())  # every real lane has its route dispatcher
    egress._GUARDS[page] = guard
    inter = StoppedInteraction(guard, fail=fail)
    runtime = Runtime(config=manager.config, lanes=manager, interaction=inter)
    grant = SimpleNamespace(id="g1", ceiling=Effect.DRAFT, blocks=[SimpleNamespace(egress=None, domains=None)])
    return Ctx(runtime, grant=grant), guard, inter, page


async def test_navigate_refuses_an_off_list_host_before_loading_it(tmp_path):
    ctx, guard, inter, _ = await setup(tmp_path)
    with pytest.raises(EgressRefused, match="evil.example is not on this lane's site list.*nothing was sent"):
        await node_tools.browser_navigate(ctx, on=LANE, url="https://evil.example/?q=dm")
    assert inter.navigated == []
    assert guard.driver == Driver(kind="agent", id="claude", grant_id="g1", lists=None, ceiling="draft")
    assert [k for k, _ in ctx.traces] == ["egress_blocked"]
    assert ctx.traces[0][1]["args"]["host"] == "evil.example"


async def test_a_redirect_the_guard_stopped_fails_the_navigation_with_the_host(tmp_path):
    for fail in (True, False):
        ctx, _, inter, _ = await setup(tmp_path / str(fail), fail=fail)
        with pytest.raises(EgressRefused, match="reached the host and was stopped"):
            await node_tools.browser_navigate(ctx, on=LANE, url="https://www.instagram.com/out")
        assert inter.navigated == ["https://www.instagram.com/out"]


async def test_act_checks_the_target_first_and_lists_blocks_in_its_result(tmp_path):
    ctx, guard, inter, _page = await setup(tmp_path)
    targets = {"e1": {"link": {"url": "https://evil.example/x"}}, "e2": {}}

    async def check_action(pg, ref, action, text):
        target = targets[ref].get("link")
        if target:
            guard.check_url(target["url"])

    guard.check_action = check_action
    with pytest.raises(EgressRefused):
        await node_tools.browser_act(ctx, on=LANE, ref="e1", action=ActionKind.CLICK)
    assert inter.acts == []

    async def act_and_frame(pg, ref, action, text=None):
        guard._record("https://ads.example/frame", "GET", egress.Refusal("lane_sites", "ads.example is not on "
                      "this lane's site list"), kind="subframe", prevented=False)
        return await FakeInteraction.act(inter, pg, ref, action, text)

    inter.act = act_and_frame
    out = await node_tools.browser_act(ctx, on=LANE, ref="e2", action=ActionKind.CLICK)
    assert out["ok"] and [b["host"] for b in out["egress_blocked"]] == ["ads.example"]


async def test_a_lane_without_a_list_and_an_open_grant_behaves_as_before(tmp_path):
    ctx, guard, inter, _ = await setup(tmp_path, sites=(), fail=False)
    inter.navigate = FakeInteraction.navigate.__get__(inter)
    out = await node_tools.browser_navigate(ctx, on=LANE, url="https://anywhere.example/")
    assert "egress_blocked" not in out and guard.blocks_since(0) == []
