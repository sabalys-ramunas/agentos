"""browser_scroll: the wheel moves the page, through human.py, at the read level."""

from __future__ import annotations

from typing import Any

import pytest
from test_policy_support import Clock

from agentos.core.errors import HumanInControl, PolicyDenied
from agentos.core.models import AccountRef, Effect, LaneSpec
from agentos.core.runtime import Runtime
from agentos.dev.lanes import DevLaneManager
from agentos.dev.web import DevInteraction, new_tab
from agentos.node import human
from agentos.node import tools as node_tools
from agentos.policy.service import PolicyService
from agentos.runner.fakes import BASE

LANE = "biz-a/social"


@pytest.fixture
async def node(tmp_path: Any, monkeypatch: Any) -> Any:
    wheeled: list[Any] = []

    async def wheel_down(page: Any) -> None:
        wheeled.append(page)

    monkeypatch.setattr(human, "wheel_down", wheel_down)
    lanes = DevLaneManager(make_page=new_tab)
    runtime = Runtime(interaction=DevInteraction())
    runtime.register("lanes", lanes)
    policy = PolicyService(tmp_path / "policy", runtime=runtime, clock=Clock())
    runtime.register("policy", policy)
    await lanes.create(LaneSpec(id=LANE, account=AccountRef(site="social", handle="@biza.coffee")))
    _, token = policy.issue(subject={"id": "reader"}, workspaces=["biz-a"], ceiling=Effect.READ)
    yield lanes, policy, token, wheeled
    await lanes.shutdown()


async def test_it_wheels_at_the_read_level_and_says_where_it_is(node: Any) -> None:
    _, policy, token, wheeled = node
    ctx = policy.context_for_token(token, tool="browser_scroll", lane_id=LANE)
    await node_tools.browser_navigate(ctx, on=LANE, url=f"{BASE}/compose")
    out = await node_tools.browser_scroll(ctx, on=LANE)
    assert len(wheeled) == 1 and out["ok"] is True and out["url"].endswith("/compose") and out["signal"] == "ok"


async def test_it_can_show_what_the_wheel_brought_in(node: Any) -> None:
    _, policy, token, _ = node
    ctx = policy.context_for_token(token, tool="browser_scroll", lane_id=LANE)
    await node_tools.browser_navigate(ctx, on=LANE, url=f"{BASE}/compose")
    assert isinstance(await node_tools.browser_scroll(ctx, on=LANE, snapshot="full"), str)


async def test_a_lane_outside_the_grant_is_refused_and_nothing_wheels(node: Any) -> None:
    _, policy, token, wheeled = node
    with pytest.raises(PolicyDenied):
        await node_tools.browser_scroll(policy.context_for_token(token, tool="browser_scroll"), on="other/lane")
    assert wheeled == []


async def test_a_person_in_control_stops_it(node: Any) -> None:
    lanes, policy, token, wheeled = node
    await lanes.take_control(LANE, by="owner")
    with pytest.raises(HumanInControl):
        await node_tools.browser_scroll(policy.context_for_token(token, tool="browser_scroll"), on=LANE)
    assert wheeled == []


def test_it_is_a_read_and_served_with_the_browser_tools() -> None:
    spec = next(s for s in node_tools.tool_specs(Runtime()) if getattr(s, "name", "") == "browser_scroll")
    assert spec.effect is Effect.READ and "—" not in (spec.description or "")
