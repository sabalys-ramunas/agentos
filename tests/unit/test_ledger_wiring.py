"""The action ledger wired to a real lane manager. A like that leaves a lane's browser is counted once,
as the actor that drove it: the node's browser_act (agent), a flow run (flow), or a pilot run's hands
(agent, through the node's tools). Reads, a listener that raises and a flow that has let go of the
lane are checked too.

Real: the lane manager and its binding of the egress guard, the guard's request handling, the node's
browser_act, the pilot's NodeTools with the policy wrapper and a child grant, and the ledger.
Fake: the browser engine (no browser starts), the browser context (FakeContext, which reports
requests as finished) and the page's clicks (FakeInteraction): a click makes the context report the
request the site would answer.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fakes import FakeInteraction
from test_egress import FakeContext, FakeRequest
from test_egress_effect_rules import meta_form
from test_lanes import FakeEngine, FakeSession, make
from test_policy_support import Clock

from openajar.core.models import ActionKind, Effect, LaneSpec
from openajar.core.runtime import Runtime
from openajar.node import egress
from openajar.node import tools as node_tools
from openajar.node.egress import LaneGuard
from openajar.node.routes import RouteDispatcher
from openajar.pilot import grant as pilot_grant
from openajar.pilot.hands import NodeTools
from openajar.pilot.modes import GOAL
from openajar.policy.ledger import build
from openajar.policy.service import PolicyService
from openajar.policy.wrap import bind_request_token, wrap_tool

LANE = "biz-a/instagram"
IG_GQL = "https://www.instagram.com/graphql/query"
# FakeInteraction's upvote button (a like here) and its comments link (a read)
LIKE_REF = "e2"
READ_REF = "e1"


def like_request(frame: Any = None) -> FakeRequest:
    headers, body = meta_form("usePolarisLikeMediaXIGLikeMutation", {"input": {"media_id": "1"}})
    return FakeRequest(IG_GQL, "POST", headers=headers, body=body, frame=frame)


def feed_read_request(frame: Any = None) -> FakeRequest:
    headers, body = meta_form("PolarisFeedTimelineQuery", {})
    return FakeRequest(IG_GQL, "POST", headers=headers, body=body, frame=frame)


class GuardedEngine(FakeEngine):
    """The part of the engine the manager sees. Each session gets an egress guard and its route
    dispatcher attached to a browser context, as CamoufoxEngine does, and its page lives there."""

    async def launch(self, spec, **kw):
        session: FakeSession = await super().launch(spec, **kw)
        context = FakeContext()
        session.guard = LaneGuard(spec.id, sites=spec.sites, strict=spec.egress_strict).attach(
            context, RouteDispatcher(context)
        )
        session._page.context = context
        return session


class SiteInteraction(FakeInteraction):
    """A click makes the browser send the request the site answers: the like button a like, the
    comments link a feed read. The context reports it finished, as Camoufox does."""

    async def act(self, page, ref, action, text=None):
        result = await super().act(page, ref, action, text)
        page.context.emit("requestfinished", like_request() if ref == LIKE_REF else feed_read_request())
        return result


async def lane_node(tmp_path, *, listeners=()) -> SimpleNamespace:
    """A real lane manager with LANE warm (the manager bound its guard to the session), a runtime
    with the ledger built on it, and a policy with a token for the caller. Listeners the test adds
    go first, so they run before the ledger does."""
    mgr, _, _, _ = make(tmp_path, engine=GuardedEngine(), self_check=False)
    await mgr.create(LaneSpec(id=LANE, sites=["*.instagram.com"]))
    for listener in listeners:
        mgr.add_effect_listener(listener)
    rt = Runtime(config=mgr.config, lanes=mgr, interaction=SiteInteraction())
    ledger = build(rt)  # the node's own composition: registers "ledger" and listens to every lane
    policy = PolicyService(tmp_path / "policy", runtime=rt, clock=Clock())
    rt.register("policy", policy)
    _, token = policy.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    async with mgr.using(LANE) as page:
        pass
    return SimpleNamespace(mgr=mgr, rt=rt, ledger=ledger, policy=policy, token=token, page=page)


async def test_a_like_clicked_through_browser_act_is_counted_as_the_agent(tmp_path):
    # The real browser_act tool drives the warm lane. No flow runs and no person has control, so
    # the guard names the like "agent".
    node = await lane_node(tmp_path)
    ctx = node.policy.context_for_token(node.token, tool="browser_act", lane_id=LANE)
    out = await node_tools.browser_act(ctx, on=LANE, ref=LIKE_REF, action=ActionKind.CLICK)
    assert out["ok"]
    assert node.ledger.count(LANE, "like", since=0) == 1
    [row] = node.ledger.recent(LANE)
    assert row["actor"] == "agent" and row["rule"] == "instagram.like"


async def test_a_like_sent_during_a_flow_run_is_counted_as_the_flow(tmp_path):
    node = await lane_node(tmp_path)
    guard = egress.guard_for(node.page)
    # flow() is what the flow driver enters for a run (flow_driver.py); it holds the lane through
    # routine(page), so requests from this page are the flow's.
    async with guard.flow(node.page):
        node.page.context.emit("requestfinished", like_request(frame=SimpleNamespace(page=node.page)))
    assert node.ledger.count(LANE, "like", since=0, actors=("flow",)) == 1
    # The flow has let go of the lane, so the same page's like is the agent's again.
    node.page.context.emit("requestfinished", like_request(frame=SimpleNamespace(page=node.page)))
    assert node.ledger.count(LANE, "like", since=0, actors=("flow",)) == 1
    assert node.ledger.count(LANE, "like", since=0, actors=("agent",)) == 1


async def test_a_like_by_a_pilot_run_is_counted_as_the_agent_on_its_lane(tmp_path):
    # A pilot run acts through NodeTools: the node's tools, wrapped by the policy, under a child
    # grant the pilot mints for the run. Its decider and loop are not run here; only its hands.
    node = await lane_node(tmp_path)
    specs = {s.name: s for s in node_tools.tool_specs(node.rt) if hasattr(s, "handler")}
    principal, _ = node.policy.resolve(node.token)
    child = pilot_grant.mint(
        node.policy, principal, run_id="run-1", lane=LANE, mode=GOAL, specs=specs, ttl_s=600, max_usd=0.5
    )
    hands = NodeTools(
        node.rt,
        lane=LANE,
        token=child.token,
        grant_id=child.grant_id,
        run_id="run-1",
        allowed=GOAL.tools,
        specs=specs,
        wrap=wrap_tool,
        bind=bind_request_token,
        redact=lambda value: value,
    )
    out = await hands.call("browser_act", on=LANE, ref=LIKE_REF, action=ActionKind.CLICK)
    assert out["ok"]
    assert node.ledger.count(LANE, "like", since=0) == 1
    [row] = node.ledger.recent(LANE)
    assert row["actor"] == "agent" and row["rule"] == "instagram.like"


async def test_a_read_adds_nothing_on_any_path(tmp_path):
    node = await lane_node(tmp_path)
    ctx = node.policy.context_for_token(node.token, tool="browser_act", lane_id=LANE)
    await node_tools.browser_act(ctx, on=LANE, ref=READ_REF, action=ActionKind.CLICK)  # the feed loads
    guard = egress.guard_for(node.page)
    async with guard.flow(node.page):
        node.page.context.emit("requestfinished", feed_read_request(frame=SimpleNamespace(page=node.page)))
    assert node.ledger.counts(LANE, since=0) == {}
    assert node.ledger.recent(LANE) == []


async def test_a_listener_that_raises_does_not_stop_the_ledger_counting(tmp_path):
    def boom(lane_id: str, kind: str, rule: str | None, actor: str) -> None:
        raise RuntimeError("disk full")

    node = await lane_node(tmp_path, listeners=[boom])  # boom is registered before the ledger
    ctx = node.policy.context_for_token(node.token, tool="browser_act", lane_id=LANE)
    await node_tools.browser_act(ctx, on=LANE, ref=LIKE_REF, action=ActionKind.CLICK)
    assert node.ledger.count(LANE, "like", since=0) == 1
