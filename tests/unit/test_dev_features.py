"""What the dev node must run of today's features: browser_file_put into the lane's inbox, browser_upload
and browser_wait on the composer (the post_image recipe), the action ledger fed by the dev site's
publish, and browser_task served or refused with a plain reason. Over MCP with the printed agent grant."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_dev_up import dev_node, ref_for

from agentos.core.config import NodeConfig
from agentos.core.errors import NotFound
from agentos.core.models import LaneSpec
from agentos.dev.node import build_runtime, prepare_home
from agentos.node.files import LaneFiles
from agentos.policy import ledger as ledger_mod
from agentos.runner.fakes import BASE

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"


def find_key(root, key: str):
    stack = [root]
    while stack:
        el = stack.pop()
        if el.key == key:
            return el
        stack.extend(el.children)
    raise LookupError(key)


@asynccontextmanager
async def connected():
    """A dev node, and an MCP client on the agent grant its banner printed."""
    async with dev_node() as node, \
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {node.agent_token}"}) as http, \
            Client(streamable_http_client(node.mcp_url, http_client=http), mode="legacy") as client:
        yield node, client


async def call(client, name: str, args: dict) -> tuple[bool, str]:
    result = await client.call_tool(name, args)
    return result.is_error, result.content[0].text if result.content else ""


async def test_an_image_post_follows_the_recipe_on_the_dev_node():
    async with connected() as (node, client):
        on = {"on": "biz-a/social"}
        err, text = await call(client, "browser_file_put", {**on, "name": "photo.png", "content_base64": PNG})
        assert not err, text
        assert str(node.home) not in text
        file_id = json.loads(text)["file"]["id"]

        await call(client, "browser_navigate", {**on, "url": f"{BASE}/"})
        _, snap = await call(client, "browser_snapshot", on)
        await call(client, "browser_act", {**on, "ref": ref_for(snap, "New post"), "action": "click"})
        _, snap = await call(client, "browser_snapshot", on)
        err, text = await call(client, "browser_upload", {**on, "ref": ref_for(snap, "Select from computer"),
                                                          "file_id": file_id})
        assert not err, text
        uploaded = json.loads(text)
        assert uploaded["ok"] is True and str(node.home) not in text

        err, text = await call(client, "browser_wait", {**on, "for_text": "photo.png", "wait_s": 5,
                                                        "snapshot": "delta"})
        assert not err, text
        waited, _ = json.JSONDecoder().raw_decode(text)  # the delta outline follows the result
        assert waited["met"] is True and "photo.png attached" in text, text
        err, text = await call(client, "browser_wait", {**on, "network_quiet_ms": 500, "wait_s": 5})
        assert not err and json.loads(text)["met"] is True, text

        _, snap = await call(client, "browser_snapshot", on)
        await call(client, "browser_act", {**on, "ref": ref_for(snap, "Write a caption..."), "action": "type",
                                           "text": "Autumn menu"})
        _, snap = await call(client, "browser_snapshot", on)
        share = {**on, "ref": ref_for(snap, "Share"), "action": "click"}
        err, text = await call(client, "browser_act", share)
        assert not err, text
        held = json.loads(text)
        assert held["status"] == "approval_pending", text

        # the owner approves in the panel; the same call then publishes, with the photo bound
        code = parse_qs(urlsplit(node.panel_link).fragment)["code"][0]
        async with httpx2.AsyncClient(base_url=f"http://localhost:{node.port}") as panel:
            assert (await panel.post("/api/session", json={"code": code})).status_code == 200
            csrf = {"X-Agentos-CSRF": (await panel.get("/api/status")).json()["csrf"]}
            approved = await panel.post(f"/api/approvals/{held['approval_id']}/approve", headers=csrf, json={})
            assert approved.status_code == 200, approved.text
        err, text = await call(client, "browser_act", share)
        assert not err, text
        assert "approval_pending" not in text and "/u/" in json.loads(text)["url"], text
        _, profile = await call(client, "browser_snapshot", on)
        assert "1 posts" in profile, profile


async def test_lane_do_says_what_it_needs():
    async with connected() as (_, client):
        err, text = await call(client, "browser_task", {"on": "biz-a/social", "goal": "like the first post"})
        assert err and text.startswith("unavailable:"), text  # not a bare not_found
        assert "browser-worker" in text and "decide job" in text and "browser_act" in text
        err, text = await call(client, "no_such_tool", {})
        assert text.startswith("not_found:"), text


def test_a_missing_lane_folder_names_no_server_path(tmp_path):
    files = LaneFiles(tmp_path / "lanes" / "biz-a" / "social" / "files")
    with pytest.raises(NotFound) as caught:
        files.ensure()
    assert str(tmp_path) not in caught.value.message


async def test_a_dev_lane_has_its_folder(tmp_path):
    home = tmp_path / "node"
    prepare_home(home)
    config = NodeConfig(name="dev", home=home, mcp_token="dev-features-token")
    _, lanes = build_runtime(config)
    try:
        await lanes.create(LaneSpec(id="biz-a/social"))
        assert config.lane_dir("biz-a/social").is_dir()
    finally:
        await lanes.shutdown()


async def test_the_ledger_hears_the_dev_sites_publish(tmp_path):
    home = tmp_path / "node"
    prepare_home(home)
    runtime, lanes = build_runtime(NodeConfig(name="dev", home=home, mcp_token="dev-features-token"))
    try:
        await lanes.create(LaneSpec(id="biz-a/social"))
        await lanes.take_control("biz-a/social", by="owner")  # a person posts by hand: nothing to ask
        async with lanes.using("biz-a/social", principal_kind="human") as page:
            driver = page.driver
            await driver.navigate(f"{BASE}/compose")
            driver.values["caption"] = "by hand"
            share = find_key(driver.root, "share")
            await driver.click(share)
        ledger = runtime.require("ledger")
        assert ledger.count("biz-a/social", "post", since=0, actors=("person", "agent", "flow")) == 1
    finally:
        await lanes.shutdown()


def test_a_ledger_that_cannot_hear_the_lanes_says_so(caplog):
    runtime = SimpleNamespace(register=lambda *a: None, get=lambda name: object())
    with caplog.at_level(logging.WARNING, logger=ledger_mod.__name__):
        ledger_mod.build(runtime)
    assert any("ledger" in r.getMessage() for r in caplog.records)


def test_a_workers_tool_names_are_the_ones_its_modules_serve():
    from agentos.mcp.providers import feature_providers
    from agentos.mcp.registry import collect_specs
    from agentos.plugins.entries import PLUGINS

    for plugin in PLUGINS:
        served = {spec.name for spec in collect_specs(SimpleNamespace(has=lambda name: True, get=lambda n: None),
                                                      feature_providers(plugin.tools))} if plugin.tools else set()
        assert set(plugin.tool_names) == served, plugin.id


# ---- shared lanes and hand back on the dev node, as on a node (howto shared_lane, hand_to_human) ----


def holder(name: str):
    from agentos.node.claims import Holder

    return Holder(key=f"agent:{name}", label=f"agent {name}", identity=f"agent:{name}")


async def dev_lanes(tmp_path, **config):
    from agentos.dev.lanes import DevLaneManager

    lanes = DevLaneManager(NodeConfig(home=tmp_path, **config))
    await lanes.create(LaneSpec(id="biz-a/social"))
    return lanes


async def test_another_callers_act_waits_for_a_claim_then_is_busy_and_reads_pass(tmp_path):
    from agentos.core.errors import LaneBusy
    from agentos.node.claims import READ

    lanes = await dev_lanes(tmp_path, queue_timeout=0.3)
    a, b = holder("a"), holder("b")
    await lanes.claim("biz-a/social", a, purpose="posting", ttl_s=60)
    with pytest.raises(LaneBusy) as busy:
        async with lanes.using("biz-a/social", holder=b):
            pass
    assert "agent a has claimed it" in busy.value.message and "(posting)" in busy.value.message
    async with lanes.using("biz-a/social", holder=b, mode=READ):  # reads share the lane
        pass
    async with lanes.using("biz-a/social", holder=a):  # the holder's own acts go on
        pass
    with pytest.raises(LaneBusy):
        await lanes.claim("biz-a/social", b, wait_s=0.2)
    assert lanes.queue_info("biz-a/social", viewer=a)["claim"]["purpose"] == "posting"
    assert (await lanes.release("biz-a/social", a))["released"] is True
    async with lanes.using("biz-a/social", holder=b):
        pass


async def test_a_claim_that_ended_without_its_holder_fails_its_next_call_once(tmp_path):
    from agentos.core.errors import ClaimLost

    lanes = await dev_lanes(tmp_path)
    a = holder("a")
    await lanes.claim("biz-a/social", a, ttl_s=30)
    lanes._access("biz-a/social").claim.expires = 0  # its ttl ran out
    with pytest.raises(ClaimLost) as lost:
        async with lanes.using("biz-a/social", holder=a):
            pass
    assert "no call from you" in lost.value.message
    async with lanes.using("biz-a/social", holder=a):  # once
        pass
    await lanes.claim("biz-a/social", a, ttl_s=30)
    await lanes.take_control("biz-a/social", by="owner")  # a take over ends every claim
    await lanes.hand_back("biz-a/social", by="owner")
    with pytest.raises(ClaimLost):
        async with lanes.using("biz-a/social", holder=a):
            pass


async def test_lane_wait_control_blocks_until_the_hand_back(tmp_path):
    import asyncio

    lanes = await dev_lanes(tmp_path)
    await lanes.take_control("biz-a/social", by="owner")
    loop = asyncio.get_running_loop()
    started = loop.time()
    timed_out = await lanes.wait_for_agent_control("biz-a/social", 0.3)
    assert timed_out.control.state.value == "human" and loop.time() - started >= 0.25

    async def hand_back_soon():
        await asyncio.sleep(0.2)
        await lanes.hand_back("biz-a/social", by="owner")

    task = asyncio.create_task(hand_back_soon())
    info = await lanes.wait_for_agent_control("biz-a/social", 10)
    await task
    assert info.control.state.value == "agent"


async def test_a_flow_run_posts_once_on_its_own_approval_and_reads_it_back():
    """The seeded flow, run by the agent after it read a page: it waits for a person (schedule_post),
    and once approved the guard knows a flow drives the tab, so the Share is not held a second
    time, and the run's readback sees the post on the profile."""
    import asyncio

    async with connected() as (node, client):
        await call(client, "browser_navigate", {"on": "biz-a/social", "url": f"{BASE}/"})
        flow = {"name": "flow_run", "arguments": {"name": "biz-a.social.post",
                                                   "params": {"caption": "From the flow", "image": "in_x"}}}
        err, text = await call(client, "tools_call", flow)
        assert not err, text
        started = json.loads(text)
        assert started["status"] == "waiting_approval", text
        code = parse_qs(urlsplit(node.panel_link).fragment)["code"][0]
        async with httpx2.AsyncClient(base_url=f"http://localhost:{node.port}") as panel:
            assert (await panel.post("/api/session", json={"code": code})).status_code == 200
            csrf = {"X-Agentos-CSRF": (await panel.get("/api/status")).json()["csrf"]}
            approved = await panel.post(f"/api/approvals/{started['approval_id']}/approve", headers=csrf,
                                        json={})
            assert approved.status_code == 200, approved.text
        err, text = await call(client, "tools_call", flow)
        assert not err, text
        run_id = json.loads(text)["id"]
        for _ in range(100):
            err, text = await call(client, "run_status", {"id": run_id})
            status = json.loads(text)["status"]
            if status not in ("queued", "running", "waiting_approval"):
                break
            await asyncio.sleep(0.1)
        assert status == "succeeded_verified", text


async def test_tools_the_grant_lacks_say_what_it_needs_and_the_seeded_lanes_can_be_viewed():
    async with connected() as (_, client):
        # the agent's secret special covers its environments' bound logins (env/*), never an account lane
        err, text = await call(client, "login", {"on": "biz-a/social"})
        assert err and text.startswith("denied: special: secret is not granted for biz-a/social"), text
        for lane in ("biz-b/forum", "biz-b/social"):  # a person holds both: the link is there to give them
            err, text = await call(client, "browser_view", {"on": lane})
            assert not err and json.loads(text)["viewer"]["url"], text
        await call(client, "browser_navigate", {"on": "biz-a/social", "url": f"{BASE}/p/1"})
        _, snap = await call(client, "browser_snapshot", {"on": "biz-a/social"})
        assert 'text: "Lovely day at the lake"' in snap  # a read-back finds a post by its words
