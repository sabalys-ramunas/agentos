"""MCP tools over the real LaneManagerImpl: concurrent calls, busy and quarantine, and take-over.

These use edge_lanes (fake engine and displays), so the lane locks and eviction are the real ones.
"""

import asyncio
import json

import pytest
from edge_lanes import make_manager
from mcp import Client

from agentos.core.errors import HumanInControl
from agentos.core.models import ActionKind, ActionResult, LaneSpec, ProxyConfig, Signal
from agentos.mcp.server import build_server


class TrackingInteraction:
    """Counts calls inside the interaction layer, and can hold every call until `gate` is set."""

    def __init__(self, gate: asyncio.Event | None = None):
        self.inside = 0
        self.max_inside = 0
        self.gate = gate

    async def _enter(self):
        self.inside += 1
        self.max_inside = max(self.max_inside, self.inside)
        if self.gate is not None:
            await self.gate.wait()
        await asyncio.sleep(0.01)
        self.inside -= 1

    async def act(self, page, ref, action: ActionKind, text=None):
        await self._enter()
        return ActionResult(ok=True, action=action, ref=ref, url=page.url, signal=Signal.OK)


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


def act_args(lane: str) -> dict:
    return {"on": lane, "ref": "e1", "action": "click"}


async def test_parallel_calls_on_one_lane_never_overlap(tmp_path):
    mgr, _, cfg = make_manager(tmp_path, busy_timeout=5.0)
    await mgr.create(LaneSpec(id="personal/reddit"))
    interaction = TrackingInteraction()
    async with Client(build_server(mgr, interaction, cfg), mode="auto") as client:
        results = await asyncio.gather(*(client.call_tool("browser_act", act_args("personal/reddit"))
                                         for _ in range(25)))
    assert all(not r.is_error for r in results)
    assert interaction.max_inside == 1
    assert mgr.get("personal/reddit").state.value == "warm"
    await mgr.shutdown()


async def test_a_third_lane_is_busy_when_every_warm_slot_is_taken(tmp_path):
    gate = asyncio.Event()
    mgr, _, cfg = make_manager(tmp_path, max_warm_lanes=2, busy_timeout=0.3)
    for name in ("a/one", "a/two", "a/three"):
        await mgr.create(LaneSpec(id=name))
    async with Client(build_server(mgr, TrackingInteraction(gate), cfg), mode="auto") as client:
        holders = [asyncio.create_task(client.call_tool("browser_act", act_args(lane)))
                   for lane in ("a/one", "a/two")]
        await asyncio.sleep(0.2)
        blocked = await client.call_tool("browser_act", act_args("a/three"))
        gate.set()
        done = await asyncio.gather(*holders)
    assert blocked.is_error and text_of(blocked).startswith("busy: all 2 warm browsers are busy")
    assert all(not r.is_error for r in done)
    await mgr.shutdown()


async def test_quarantine_is_reported_through_mcp_and_cleared_by_reopening(tmp_path):
    proxy = ProxyConfig(label="lt1", host="198.51.100.10", port=12323, username="u", password="p")
    mgr, engine, cfg = make_manager(tmp_path, proxies={"lt1": proxy}, self_check=True)
    await mgr.create(LaneSpec(id="personal/reddit", proxy_label="lt1"))
    engine.exit_ip = "203.0.113.7"  # not the proxy's address: the self-check must quarantine the lane
    async with Client(build_server(mgr, TrackingInteraction(), cfg), mode="auto") as client:
        blocked = await client.call_tool("browser_navigate", {"on": "personal/reddit", "url": "https://a.test"})
        assert blocked.is_error and text_of(blocked).startswith("quarantined: browser personal/reddit")
        assert "does not match proxy lt1" in text_of(blocked)

        engine.exit_ip = "198.51.100.10"  # the cause is fixed; browser_open tries again
        reopened = await client.call_tool("browser_open", {"on": "personal/reddit"})
    assert not reopened.is_error
    assert (reopened.structured_content or json.loads(text_of(reopened)))["state"] == "warm"
    await mgr.shutdown()


async def test_a_human_takeover_blocks_agent_calls(tmp_path):
    mgr, _, _ = make_manager(tmp_path)
    await mgr.create(LaneSpec(id="personal/reddit"))
    assert hasattr(mgr, "take_control"), "LaneManagerImpl has no take_control yet"
    await mgr.take_control("personal/reddit", by="human:owner")
    with pytest.raises(HumanInControl):
        async with mgr.using("personal/reddit"):
            pass
    await mgr.shutdown()
