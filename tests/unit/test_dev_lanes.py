"""The dev node's lane manager: the LaneManager contract over memory, a display per lane, holds."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from agentos.core.config import NodeConfig
from agentos.core.errors import AgentPaused, HumanInControl, LaneQuarantined
from agentos.core.models import ControlState, LaneSpec, LaneState, Signal
from agentos.dev.lanes import DevLaneManager
from agentos.dev.node import flow_driver_factory
from agentos.dev.web import CHALLENGE_URL, DevInteraction, frame_of_page, new_tab
from agentos.node.flow_driver import LaneAgentPaused
from agentos.node.holds import hold_for
from agentos.runner.fakes import BASE


def manager(home) -> DevLaneManager:
    return DevLaneManager(NodeConfig(home=home), make_page=new_tab, frame=frame_of_page)


async def refused(path: str) -> bool:
    try:
        _, writer = await asyncio.open_unix_connection(path)
    except OSError:
        return True
    writer.close()
    return False


async def test_a_lane_has_a_display_that_the_viewer_is_pointed_at(tmp_path):
    lanes = manager(tmp_path)
    await lanes.start()
    await lanes.create(LaneSpec(id="biz-a/social"))
    server = lanes.servers["biz-a/social"]
    assert server.path is not None
    assert lanes.view("biz-a/social") is None  # cold: no viewer until the lane is warm

    opened = await lanes.open("biz-a/social")
    assert opened.state is LaneState.WARM
    assert opened.viewer.vnc_socket == server.path and opened.viewer.vnc_port is None
    version, picture = server.frame()
    assert (picture.width, picture.height) == (960, 600) and version >= 0
    await lanes.shutdown()


async def test_a_captcha_holds_the_lane_until_a_human_hands_it_back(tmp_path):
    lanes = manager(tmp_path)
    await lanes.create(LaneSpec(id="biz-b/social"))
    interaction = lanes.watch(DevInteraction())
    async with lanes.using("biz-b/social") as page:
        result = await interaction.navigate(page, CHALLENGE_URL)
    assert result.signal.value == "captcha"

    held = lanes.control("biz-b/social")
    assert held.state is ControlState.PAUSED and held.reason == "needs_human:captcha"
    with pytest.raises(AgentPaused):
        async with lanes.using("biz-b/social"):
            pass

    # handed back while the captcha still shows: held again at once, agents never get that page
    again = await lanes.hand_back("biz-b/social", by="owner")
    assert again.control.state is ControlState.PAUSED and again.control.reason == "needs_human:captcha"

    lanes.pages["biz-b/social"].url = BASE + "/"  # the person dealt with it
    await lanes.hand_back("biz-b/social", by="owner")
    assert lanes.control("biz-b/social").state is ControlState.AGENT
    async with lanes.using("biz-b/social"):
        pass
    await lanes.shutdown()


async def test_the_hold_policy_is_the_production_one():
    lanes = DevLaneManager(NodeConfig(home="/nonexistent"))
    decision = lanes.hold_policy
    assert decision is not None
    now = datetime(2026, 11, 4, tzinfo=UTC)
    reason, until = decision(Signal.CAPTCHA, now=now)
    assert (reason, until) == (hold_for(Signal.CAPTCHA, now=now).reason, None)
    assert decision(Signal.OK, now=now) is None


async def test_a_human_in_control_refuses_agents_until_hand_back(tmp_path):
    lanes = manager(tmp_path)
    await lanes.create(LaneSpec(id="biz-a/social"))
    await lanes.take_control("biz-a/social", by="owner")
    with pytest.raises(HumanInControl):
        async with lanes.using("biz-a/social"):
            pass
    async with lanes.using("biz-a/social", principal_kind="human"):
        pass  # the human's own view is never refused
    await lanes.hand_back("biz-a/social", by="owner")
    async with lanes.using("biz-a/social"):
        pass
    await lanes.shutdown()


async def test_a_quarantined_lane_refuses_agents_until_a_human_hands_it_back(tmp_path):
    lanes = manager(tmp_path)
    await lanes.create(LaneSpec(id="biz-d/blog"))
    await lanes.quarantine("biz-d/blog", "exit IP 203.0.113.9 does not match proxy de2")
    with pytest.raises(LaneQuarantined):
        async with lanes.using("biz-d/blog"):
            pass
    await lanes.hand_back("biz-d/blog", by="owner")  # the hand-back clears the quarantine and its hold
    async with lanes.using("biz-d/blog"):
        pass
    await lanes.shutdown()


async def test_deleting_a_lane_closes_its_display_and_shutdown_closes_the_rest(tmp_path):
    lanes = manager(tmp_path)
    await lanes.create(LaneSpec(id="biz-a/social"))
    await lanes.create(LaneSpec(id="biz-a/shop"))
    gone, kept = lanes.servers["biz-a/social"].path, lanes.servers["biz-a/shop"].path

    await lanes.delete("biz-a/social", by="owner")
    assert await refused(gone) and "biz-a/social" not in lanes.servers

    await lanes.shutdown()
    assert await refused(kept) and lanes.servers == {}


async def test_a_flow_replays_on_the_lanes_own_tab_and_waits_for_a_hold(tmp_path):
    lanes = manager(tmp_path)
    await lanes.create(LaneSpec(id="biz-a/social"))
    await lanes.create(LaneSpec(id="biz-b/social"))
    async with flow_driver_factory(lanes)("biz-a/social") as driver:
        await driver.navigate(f"{BASE}/")
    async with lanes.using("biz-a/social") as page:
        snap = await DevInteraction().snapshot(page)
    assert snap.url == f"{BASE}/" and 'heading "Feed"' in snap.text  # the agent sees what the flow did

    await lanes.hold("biz-b/social", reason="needs_human:captcha")
    with pytest.raises(LaneAgentPaused):
        async with flow_driver_factory(lanes)("biz-b/social"):
            pass
    await lanes.shutdown()
