"""A mesh freeze through the controller: every online node at once, offline nodes in their next
welcome before anything else runs there, agent grants revoked, and only the controller's own freeze
lifted by its unfreeze (openajar.freeze, docs/PROTOCOL.md mesh_freeze)."""

from __future__ import annotations

import asyncio

import pytest
from channel_support import (
    AGENT_TOKEN,
    FAST,
    RunningNode,
    agent,
    extra_tools,
    join_node,
    owner,
    start_controller,
    wait_for,
)
from fakes import FakeInteraction, FakeLaneManager
from testkit.timing import until

from openajar.core import signing
from openajar.core.errors import PolicyDenied
from openajar.core.models import LaneSpec
from openajar.mcp import providers as mcp_providers
from openajar.node.daemon import build_services, node_runtime, serve

SERVICES = ["openajar.policy.service", "openajar.freeze.service", "openajar.node.channel.service"]


async def start(home, name, ctl, lanes, *, config=None) -> RunningNode:
    config = config or await join_node(home, name, ctl)
    manager = FakeLaneManager(config)
    for lane in lanes:
        await manager.create(LaneSpec(id=lane))
    interaction = FakeInteraction()
    runtime = node_runtime(config, manager, interaction)
    build_services(runtime, SERVICES)
    channel = runtime.get("channel")
    channel.timings = FAST
    channel._backoff.low, channel._backoff.high = FAST.backoff_min, FAST.backoff_max
    stop = asyncio.Event()
    task = asyncio.create_task(serve(config, runtime, stop=stop, providers=[*mcp_providers.tool_providers(), extra_tools]))
    await asyncio.wait_for(channel.welcomed.wait(), 15)
    return RunningNode(name, config, runtime, manager, interaction, stop, task)


@pytest.fixture
async def mesh(tmp_path):
    ctl = await start_controller(tmp_path / "controller")
    a = await start(tmp_path / "a", "node-a", ctl, ["personal/reddit"])
    b = await start(tmp_path / "b", "node-b", ctl, ["work/linkedin"])
    hub = ctl.controller.hub
    await wait_for(lambda: len(hub.sessions) == 2 and all(s.tools for s in hub.sessions.values()))
    nodes = {"a": a, "b": b}
    yield ctl, nodes, tmp_path
    for n in nodes.values():
        await n.close()
    await ctl.close()


async def test_a_mesh_freeze_reaches_every_node_and_offline_ones_when_they_dial_in(mesh):
    ctl, nodes, tmp = mesh
    a, b = nodes["a"], nodes["b"]
    b_config = b.config
    await b.close()
    await wait_for(lambda: len(ctl.controller.hub.sessions) == 1)

    async with owner(ctl) as api:
        r = await api.post("/api/freeze", json={"reason": "an agent went rogue"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["frozen"] and body["pending"] == ["node-b"] and body["revoked_grants"]
        assert body["sent"] == {a.config.node_id: "sent"}
        await wait_for(lambda: a.runtime.get("freeze").is_frozen())
        rec = a.runtime.get("freeze").records()[0]
        assert (rec.scope, rec.source, rec.reason) == ("mesh", "mesh", "an agent went rogue")
        assert rec.by == "controller:owner"

        # the agent token was revoked with every org grant (the controller refuses it at the door)
        with pytest.raises(BaseException):  # noqa: B017  (the MCP client fails to connect: 401)
            async with agent(ctl, AGENT_TOKEN) as client:
                await client.call_tool("browsers_list", {"node": "node-a"})
        assert ctl.controller.store.grant_by_token(AGENT_TOKEN)["revoked_at"] is not None
        _, fresh = ctl.controller.grants.create(ctl.org, subject="granted-during-the-freeze")
        async with agent(ctl, fresh) as client:
            res = await client.call_tool("browsers_list", {"node": "node-a"})
        assert res.is_error and res.content[0].text.startswith("frozen:")

        # node-b comes back: the freeze is in its welcome, applied before anything else runs
        nodes["b"] = b2 = await start(tmp / "b", "node-b", ctl, ["work/linkedin"], config=b_config)
        assert b2.runtime.get("freeze").is_frozen("work/linkedin")
        status = (await api.get("/api/freeze")).json()
        assert status["pending"] == []

        # lifting it: only the mesh freeze goes, a node's own freeze stays
        await a.runtime.get("freeze").freeze("lane", "personal/reddit", reason="mine", by="owner")
        r = await api.post("/api/unfreeze", json={})
        assert r.status_code == 200 and r.json()["unfrozen"]
        await wait_for(lambda: not b2.runtime.get("freeze").is_frozen("work/linkedin"))
        await wait_for(lambda: a.runtime.get("freeze").records()[0].scope == "lane")
        assert a.runtime.get("freeze").is_frozen("personal/reddit")
        assert not (await api.get("/api/freeze")).json()["frozen"]


async def test_a_forged_or_stale_mesh_freeze_is_refused(mesh):
    _, nodes, _ = mesh
    channel = nodes["a"].channel
    forged = signing.sign_controller(_key(), "mesh_freeze", channel.node_id,
                                  {"frozen": True, "reason": "x", "by": "x", "freeze_id": "fz_x"})
    with pytest.raises(PolicyDenied):
        channel._apply_mesh_freeze(forged)
    assert not nodes["a"].runtime.get("freeze").is_frozen()


def _key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.generate()


async def test_a_controller_unfreeze_never_lifts_a_nodes_own_node_freeze(mesh):
    ctl, nodes, _ = mesh
    a = nodes["a"]
    await a.runtime.get("freeze").freeze("node", None, reason="local", by="owner")
    sent = await ctl.controller.hub.push_mesh_freeze(ctl.org, None)
    assert sent[a.config.node_id] == "sent"
    assert await until(lambda: a.runtime.get("freeze").is_frozen("personal/reddit"))
