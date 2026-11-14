"""The self-hosted controller passes the owner's standing approvals through to a node: the node
applies them, an agent's matching post runs without asking, the use comes back as an event, and a
mesh freeze ends them (docs/PROTOCOL.md `standing`)."""

from __future__ import annotations

import json
import time

import pytest
from channel_support import agent, owner, start_controller, wait_for
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_freeze_mesh import start

from agentos.core import signing
from agentos.core.config import NodeConfig
from agentos.core.runtime import Runtime
from agentos.node.channel import service, wire
from agentos.node.channel.enroll import enroll
from agentos.policy.approval_binding import content_digest
from agentos.policy.errors import Denied
from agentos.policy.issuers import TrustedIssuers

LANE = "personal/reddit"


def item(**kw) -> dict:
    return {"id": "sa_posts1", "workspace": "personal", "effect": "commit", "lanes": [LANE], "count": 2,
            "until": wire.iso(time.time() + 86400), "targets": ["tool:post_note"],
            "contents": [content_digest({"text": "hello"})], **kw}


@pytest.fixture
async def mesh(tmp_path):
    ctl = await start_controller(tmp_path / "controller")
    node = await start(tmp_path / "a", "node-a", ctl, [LANE])
    hub = ctl.controller.hub
    await wait_for(lambda: len(hub.sessions) == 1 and all(s.tools for s in hub.sessions.values()))
    yield ctl, node
    await node.close()
    await ctl.close()


def standing_events(ctl) -> list[dict]:
    return [e["payload"] for e in ctl.controller.store.events(ctl.org) if e["kind"] == "standing"]


async def test_the_controller_passes_them_through_and_the_node_reports_each_use(mesh):
    ctl, node = mesh
    node_id = node.config.node_id
    async with owner(ctl) as api:
        r = await api.post(f"/api/nodes/{node_id}/standing", json={"upsert": [item(), item(id="sa_pay", effect="pay")]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] == [{"id": "sa_posts1", "used": 0, "remaining": 2, "state": "active"}]
        assert [(x["id"], x["code"]) for x in body["refused"]] == [("sa_pay", "effect_not_allowed")]
        assert (await api.post(f"/api/nodes/{node_id}/standing", json={"upsert": "x"})).status_code == 400

        async with agent(ctl) as client:
            done = await client.call_tool("post_note", {"on": LANE, "text": "hello"})
            assert not done.is_error and json.loads(done.content[0].text) == {"posted": "hello", "on": LANE}
            await wait_for(lambda: {"id": "sa_posts1", "state": "active", "used": 1, "remaining": 1}
                           in standing_events(ctl))
            other = await client.call_tool("post_note", {"on": LANE, "text": "something else"})
            assert other.structured_content["status"] == "approval_pending"

        # a mesh freeze revokes it on the node, and it never comes back
        assert (await api.post("/api/freeze", json={"reason": "stop"})).status_code == 200
        await wait_for(lambda: any(e["state"] == "revoked" for e in standing_events(ctl)))
        assert (await api.post("/api/unfreeze", json={})).status_code == 200
        again = (await api.post(f"/api/nodes/{node_id}/standing", json={"set": [item()]})).json()
        assert again["refused"][0]["code"] == "revoked"


async def test_nothing_is_added_during_a_freeze_so_nothing_goes_live_at_the_unfreeze(mesh):
    ctl, node = mesh
    node_id, freeze = node.config.node_id, node.runtime.get("freeze")
    url = f"/api/nodes/{node_id}/standing"
    async with owner(ctl) as api:
        assert (await api.post("/api/freeze", json={"reason": "stop"})).status_code == 200
        await wait_for(lambda: freeze.is_frozen())
        during = (await api.post(url, json={"upsert": [item(id="sa_during")]})).json()
        assert [(x["id"], x["code"]) for x in during["refused"]] == [("sa_during", "frozen")]
        whole = (await api.post(url, json={"set": [item(id="sa_set")]})).json()
        assert [(x["id"], x["code"]) for x in whole["refused"]] == [("sa_set", "frozen")]
        # revokes still apply
        assert (await api.post(url, json={"revoke": ["sa_gone"]})).json()["revoked"] == ["sa_gone"]
        assert (await api.post("/api/unfreeze", json={})).status_code == 200
        await wait_for(lambda: not freeze.is_frozen())
        assert node.runtime.get("approvals").list_standing() == []

        # the node's own freeze, and a workspace freeze for the items over it only
        await freeze.freeze("node", None, reason="local", by="owner")
        assert (await api.post(url, json={"upsert": [item()]})).json()["refused"][0]["code"] == "frozen"
        await freeze.unfreeze("node", None, by="owner")
        await freeze.freeze("workspace", "personal", reason="local", by="owner")
        other = item(id="sa_work", workspace="work", lanes=["work/linkedin"])
        mixed = (await api.post(url, json={"upsert": [item(), other]})).json()
        assert [x["id"] for x in mixed["applied"]] == ["sa_work"]
        assert [(x["id"], x["code"]) for x in mixed["refused"]] == [("sa_posts1", "frozen")]


async def test_the_controller_takes_as_many_as_the_node_does(mesh):
    ctl, node = mesh
    url = f"/api/nodes/{node.config.node_id}/standing"
    many = [item(id=f"sa_n{i}", count=1, note="x" * 200) for i in range(300)]  # well over 64 KiB
    async with owner(ctl) as api:
        r = await api.post(url, json={"upsert": many})
        assert r.status_code == 200, r.text
        assert len(r.json()["applied"]) + len(r.json()["refused"]) == 300
        too_many = await api.post(url, json={"revoke": [f"sa_n{i}" for i in range(501)]})
        assert too_many.status_code == 400 and "500" in too_many.text


async def test_joining_another_org_leaves_the_old_ones_grants_and_standing_approvals(mesh):
    ctl, node = mesh
    old = f"org:{ctl.org}"
    async with owner(ctl) as api:
        r = await api.post(f"/api/nodes/{node.config.node_id}/standing", json={"upsert": [item()]})
        assert r.json()["applied"][0]["id"] == "sa_posts1"
    grant, _ = ctl.controller.grants.create(ctl.org, subject="agent-x")
    token = ctl.controller.grants.mint(grant, deadline=time.time() + 60, node_id=node.config.node_id)
    assert node.runtime.get("grant_issuers").open(token)[0] == old

    other = ctl.controller.store.create_org("other", "Other Org")
    _, join = ctl.controller.create_join_token(other["id"], name="node-a")
    left: dict = {}
    config = await enroll(node.config, ctl.node_url, join, left=left)
    assert config.org_id == other["id"] and left == {"issuer": old, "standing": ["sa_posts1"]}
    # its standing approvals cover nothing from now, even in the daemon still running
    approvals = node.runtime.get("approvals")
    assert approvals.issued_standing(old)[0].revoked_at is not None
    assert approvals.consume_standing("commit", LANE, [], tool="post_note", content={"text": "hello"}) is None
    # its grants stop verifying once the node trusts what is on disk (the restart the join asks for)
    fresh = TrustedIssuers(config.home)
    try:
        assert fresh.issuers == [] and not fresh.live(old)
        with pytest.raises(Denied) as refused:
            fresh.open(token)
        assert refused.value.reason == "unknown_issuer"
    finally:
        fresh.close()


def test_the_channel_forgets_an_org_the_node_left_when_it_starts(tmp_path):
    issuers = TrustedIssuers(tmp_path)
    issuers.trust("org:old", signing.public_b64(Ed25519PrivateKey.generate()))
    runtime = Runtime()
    runtime.register("config", NodeConfig(home=tmp_path, org_id="new", controller_url="ws://127.0.0.1:1", node_id="nd_x", controller_pubkey=signing.public_b64(Ed25519PrivateKey.generate())))
    runtime.register("grant_issuers", issuers)
    service.build(runtime)
    assert not issuers.live("org:old")
    issuers.close()


async def test_a_node_offline_gets_its_set_and_the_revokes_it_missed_when_it_connects(mesh, tmp_path):
    ctl, node = mesh
    node_id, store, hub = node.config.node_id, ctl.controller.store, ctl.controller.hub
    url = f"/api/nodes/{node_id}/standing"
    async with owner(ctl) as api:
        await api.post(url, json={"upsert": [item(), item(id="sa_keep", contents=[content_digest({"text": "yo"})])]})
        await node.close()
        await wait_for(lambda: not hub.sessions)
        queued = (await api.post(url, json={"revoke": ["sa_posts1"], "upsert": [item(id="sa_new")]})).json()
        assert queued["queued"] is True and store.standing_revokes(ctl.org, node_id) == ["sa_posts1"]
        assert [i["id"] for i in store.standing(ctl.org, node_id)] == ["sa_keep", "sa_new"]

    back = await start(tmp_path / "a", "node-a", ctl, [LANE], config=node.config)
    try:
        await wait_for(lambda: store.standing_revokes(ctl.org, node_id) == [])
        held = {sa.id: sa.state(time.time()) for sa in back.runtime.get("approvals").issued_standing(f"org:{ctl.org}")}
        assert held == {"sa_posts1": "revoked", "sa_keep": "active", "sa_new": "active"}
        assert back.channel._standing_synced  # its controller sent the whole set: the session confirms it
        # an item the node refuses is no longer one it should hold, so a later welcome cannot slip it in
        async with owner(ctl) as api:
            refused = (await api.post(url, json={"upsert": [item(id="sa_long", count=1000)]})).json()
        assert refused["refused"][0]["code"] == "over_limit"
        assert [i["id"] for i in store.standing(ctl.org, node_id)] == ["sa_keep", "sa_new"]
    finally:
        await back.close()
