"""End to end on a real HTTP server: the node app with FakeLaneManager and FakeInteraction plus
the real policy, vault, runner, brains and env services, the panel mounted at /api and /.

Sign in with a one-time code, use the panel with the cookie and the CSRF header, drive MCP with
the node token, then give an agent a grant and check it sees only its lanes in MCP and /api."""

from __future__ import annotations

import asyncio
import json

import httpx2
from fakes import FakeInteraction, FakeLaneManager, mcp_port_for_tests
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_vault_support import make_vault, private_home

from agentos.api import create_login_code
from agentos.core.config import NodeConfig
from agentos.core.models import LaneSpec
from agentos.node.daemon import FEATURE_SERVICES, build_services, node_runtime, serve

TOKEN = "node-token-for-the-e2e-test"
LANES = ["personal/reddit", "biz-a/instagram", "biz-b/facebook"]


async def mcp_lanes(url: str, token: str) -> tuple[set[str], set[str]]:
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
        Client(streamable_http_client(url, http_client=http), mode="legacy") as client,
    ):
        tools = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("lanes_list", {})
        assert not result.is_error, result.content[0].text
        return tools, {lane["id"] for lane in json.loads(result.content[0].text)["lanes"]}


async def test_panel_and_mcp_on_one_node(tmp_path):
    home = private_home(tmp_path)
    make_vault(home)
    config = NodeConfig(home=home, mcp_token=TOKEN, mcp_port=mcp_port_for_tests())
    manager = FakeLaneManager(config)
    for lane in LANES:
        await manager.create(LaneSpec(id=lane))
    runtime = node_runtime(config, manager, FakeInteraction())
    built = [name for name, _ in build_services(runtime, [m for m in FEATURE_SERVICES if "control" not in m])]
    assert built[:4] == ["agentos.api.events", "agentos.policy.service", "agentos.freeze.service",
                         "agentos.vault.service"]
    assert {"agentos.workspaces.service", "agentos.runner.service", "agentos.brains.service",
            "agentos.env.service", "agentos.ops.service"} <= set(built)

    port: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    task = asyncio.create_task(serve(config, runtime, stop=stop, on_started=port.set_result))
    try:
        base = f"http://localhost:{await asyncio.wait_for(port, 15)}"
        link = create_login_code(home, base_url=base)
        assert link.url.startswith(f"{base}/login#code=")

        async with httpx2.AsyncClient(base_url=base) as panel:
            # signed out: 401, and the page shell is public
            assert (await panel.get("/api/status")).status_code == 401
            assert (await panel.get("/")).status_code == 200
            signed = await panel.post("/api/session", json={"code": link.code})
            assert signed.status_code == 200, signed.text
            assert "agentos_session" in panel.cookies and "httponly" in signed.headers["set-cookie"].lower()
            again = await panel.post("/api/session", json={"code": link.code})
            assert again.status_code == 401  # a code works once

            status = (await panel.get("/api/status")).json()
            csrf = {"X-Agentos-CSRF": status["csrf"]}
            assert status["principal"] == {"kind": "human", "id": "owner", "owner": True}
            assert {"policy", "vault", "runner", "brains", "envs", "events"} <= set(status["services"])
            assert {lane["id"] for lane in (await panel.get("/api/lanes")).json()} == set(LANES)
            assert (await panel.get("/api/approvals")).json() == []
            assert (await panel.get("/api/vault")).json() == []
            assert (await panel.get("/api/vault/status")).json()["state"] == "unsealed"
            assert (await panel.get("/api/brains")).json() == []
            assert (await panel.get("/api/activity")).status_code == 200

            # a write needs the CSRF header
            assert (await panel.post("/api/lanes/personal/reddit/open")).status_code == 403
            opened = await panel.post("/api/lanes/personal/reddit/open", headers=csrf)
            assert opened.status_code == 200 and opened.json()["state"] == "warm"

            # the owner's agent on MCP with the node token sees every lane, and every tool when it asks
            tools, lanes = await mcp_lanes(f"{base}/mcp", TOKEN)
            assert lanes == set(LANES) and {"lanes_list", "tools_more"} <= tools and "vault_list" not in tools
            tools, _ = await mcp_lanes(f"{base}/mcp?toolsets=full", TOKEN)
            assert {"lanes_list", "grant_info", "vault_list"} <= tools and "tools_more" not in tools

            # the owner gives an agent biz-a, read only; the token is shown once
            issued = await panel.post("/api/grants", json={"subject": "worker", "workspaces": ["biz-a"],
                                                            "ceiling": "read", "ttl": "1h"}, headers=csrf)
            assert issued.status_code == 200, issued.text
            grant = issued.json()["token"]
            listed = (await panel.get("/api/grants")).json()
            assert [g["subject"] for g in listed] == ["worker"] and grant not in json.dumps(listed)

        # the agent: only its lanes, in MCP and in /api
        tools, lanes = await mcp_lanes(f"{base}/mcp", grant)
        assert lanes == {"biz-a/instagram"} and "lanes_list" in tools and "lane_exec" not in tools  # no exec special
        async with httpx2.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {grant}"}) as agent:
            assert [lane["id"] for lane in (await agent.get("/api/lanes")).json()] == ["biz-a/instagram"]
            assert (await agent.get("/api/lanes/personal/reddit")).status_code == 403
            assert (await agent.get("/api/vault")).status_code == 403
            assert (await agent.post("/api/grants", json={"subject": "x", "workspaces": ["biz-a"]})).status_code == 403
            assert (await agent.get("/api/approvals")).json() == []
            assert (await agent.get("/api/status")).json()["principal"]["owner"] is False

        # forged, revoked and missing tokens
        async with httpx2.AsyncClient(base_url=base) as anyone:
            assert (await anyone.get("/api/lanes", headers={"Authorization": "Bearer agt1_forged"})).status_code == 401
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "0"}}}
            assert (await anyone.post("/mcp", json=init, headers={
                "Accept": "application/json, text/event-stream"})).status_code == 401
            grant_id = runtime.policy.resolve(grant)[1].id
            runtime.policy.revoke(grant_id)
            assert (await anyone.get("/api/lanes", headers={"Authorization": f"Bearer {grant}"})).status_code == 401
            assert (await anyone.get("/api/status", headers={"Host": "evil.example"})).status_code == 421

        # the panel's own actions are in the action log
        traced = {e.tool for e in runtime.policy.traces.query(kind="panel", limit=200)[0]}
        assert {"panel.lane.open", "panel.grant.issue"} <= traced
        assert TOKEN not in json.dumps([e.public() for e in runtime.policy.traces.query(limit=500)[0]])
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)
    assert manager.shut_down
