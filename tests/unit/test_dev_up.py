"""`agentos dev up`, in process: the daemon's composition over the dev fakes, served over real HTTP.

MCP is called over HTTP with the printed token, the panel is signed in with the printed code, and
the node shuts down with every port closed and a temporary home removed."""

from __future__ import annotations

import asyncio
import json
import re
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from websockets.asyncio.client import connect

from agentos.core.config import NodeConfig
from agentos.dev.cli import banner
from agentos.dev.node import (
    LANES,
    SEED_QUESTION,
    SEED_SUMMARY,
    DevNode,
    build_runtime,
    prepare_home,
    run,
    seed,
)
from agentos.vault.service import keyfile_path


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def refused(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return True
    writer.close()
    return False


@asynccontextmanager
async def dev_node(*, home: Path | None = None, seed: bool = True):
    """`agentos dev up` in this process; yields its DevNode and stops it on exit."""
    announced: asyncio.Future[DevNode] = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    def announce(node: DevNode) -> None:
        if not announced.done():
            announced.set_result(node)

    task = asyncio.create_task(run(home=home, port=free_port(), seed_lanes=seed, announce=announce, stop=stop))
    try:
        node = await asyncio.wait_for(announced, 30)
        yield node
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


def ref_for(text: str, name: str) -> str:
    line = next(line for line in text.splitlines() if f'"{name}"' in line and "[ref=" in line)
    return re.search(r"\[ref=(e\d+)\]", line).group(1)


async def test_dev_node_serves_mcp_and_the_panel_on_one_port():
    async with dev_node() as node:
        assert node.mcp_url.endswith("/mcp") and node.agent_token in node.claude_add
        assert "agentos-dev" in node.claude_add and node.mcp_url in node.claude_add

        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {node.agent_token}"}) as http,
            Client(streamable_http_client(node.mcp_url, http_client=http), mode="legacy") as client,
        ):
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert {"browser_snapshot", "browser_act", "tools_more"} <= tools  # the account tools: tools_more("accounts")

            listed = await client.call_tool("browsers_list", {})
            assert not listed.is_error
            assert {lane["id"] for lane in json.loads(listed.content[0].text)["browsers"]} == set(LANES)

            opened = await client.call_tool("browser_open", {"on": "biz-a/social"})
            assert not opened.is_error, opened.content[0].text
            snap = (await client.call_tool("browser_snapshot", {"on": "biz-a/social"})).content[0].text
            assert "[ref=" in snap and 'heading "Feed"' in snap
            acted = await client.call_tool("browser_act", {
                "on": "biz-a/social", "ref": ref_for(snap, "New post"), "action": "click"})
            assert not acted.is_error, acted.content[0].text
            result = json.loads(acted.content[0].text)
            assert result["changed"] is True and result["url"].endswith("/compose")

            # the seed met a captcha on biz-b: the lane is held for every agent until a person looks
            held = await client.call_tool("browser_snapshot", {"on": "biz-b/social"})
            assert held.is_error and "agent_paused" in held.content[0].text and "captcha" in held.content[0].text

        panel_code = parse_qs(urlsplit(node.panel_link).fragment)["code"][0]
        async with httpx2.AsyncClient(base_url=f"http://localhost:{node.port}") as panel:
            assert (await panel.get("/api/status")).status_code == 401
            signed = await panel.post("/api/session", json={"code": panel_code})
            assert signed.status_code == 200, signed.text
            assert (await panel.post("/api/session", json={"code": panel_code})).status_code == 401  # once

            lanes = {lane["id"]: lane for lane in (await panel.get("/api/lanes")).json()}
            assert set(lanes) == set(LANES)
            assert lanes["biz-b/social"]["signal"] == "captcha"
            pending = [a["summary"] for a in (await panel.get("/api/approvals")).json()]
            assert SEED_SUMMARY in pending and SEED_QUESTION in pending
            assert "biz-a.social.post" in {f["id"] for f in (await panel.get("/api/flows")).json()}

            csrf = {"X-Agentos-CSRF": (await panel.get("/api/status")).json()["csrf"]}
            view = await panel.post("/api/lanes/biz-a/social/view-link", headers=csrf)
            assert view.status_code == 200, view.text
            page = await panel.get(view.json()["url"])
            assert page.status_code == 200 and "biz-a/social" in page.text

            # the live view: the viewer's WebSocket relays to the lane's fake display (RFB)
            token = parse_qs(urlsplit(view.json()["url"]).query)["t"][0]
            async with connect(f"ws://localhost:{node.port}/viewer-ws/biz-a/social?t={token}") as viewer:
                assert await asyncio.wait_for(viewer.recv(), 10) == b"RFB 003.008\n"


async def test_the_printed_line_carries_an_agent_grant_and_never_the_node_token():
    async with dev_node() as node:
        shown = banner(node)
        assert node.token not in shown and node.token not in node.claude_add
        assert node.agent_token in node.claude_add and node.agent_token != node.token
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {node.agent_token}"}) as http,
            Client(streamable_http_client(node.mcp_url, http_client=http), mode="legacy") as client,
        ):
            who = json.loads((await client.call_tool("grant_info", {})).content[0].text)
            assert "claude-code@dev" in json.dumps(who), who
            assert node.token not in json.dumps(who)


async def test_a_post_after_reading_a_page_waits_for_a_person():
    """The quickstart's own path: read the feed, open the composer, type, Share. The post waits."""
    async with dev_node() as node:
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {node.agent_token}"}) as http,
            Client(streamable_http_client(node.mcp_url, http_client=http), mode="legacy") as client,
        ):
            on = {"on": "biz-a/social"}

            async def snap() -> str:
                return (await client.call_tool("browser_snapshot", on)).content[0].text

            await client.call_tool("browser_navigate", {**on, "url": "https://social.example/"})
            await client.call_tool("browser_act", {**on, "ref": ref_for(await snap(), "New post"), "action": "click"})
            await client.call_tool("browser_act", {**on, "ref": ref_for(await snap(), "Write a caption..."),
                                                   "action": "type", "text": "hello from the quickstart"})
            shared = await client.call_tool("browser_act", {**on, "ref": ref_for(await snap(), "Share"),
                                                            "action": "click"})
            result = json.loads(shared.content[0].text)
            assert result["status"] == "approval_pending", result
            assert "untrusted" in result["why"]

        panel_code = parse_qs(urlsplit(node.panel_link).fragment)["code"][0]
        async with httpx2.AsyncClient(base_url=f"http://localhost:{node.port}") as panel:
            assert (await panel.post("/api/session", json={"code": panel_code})).status_code == 200
            pending = (await panel.get("/api/approvals")).json()
            assert any("hello from the quickstart" in (a.get("summary") or "") for a in pending)


async def test_dev_node_stops_cleanly_and_removes_a_temporary_home():
    async with dev_node(seed=False) as node:
        home, port = node.home, node.port
        assert node.temporary and home.is_dir()
        assert not await refused(port)
    assert not home.exists()
    assert await refused(port)


async def test_a_persistent_home_keeps_the_vault_key_and_the_brains_wiring(tmp_path):
    home = tmp_path / "node"
    async with dev_node(home=home, seed=False) as node:
        assert not node.temporary and node.home == home
    assert (home / "vault.db").is_file() and keyfile_path(home).is_file()
    assert (home / "brains" / "settings.toml").is_file()
    key = keyfile_path(home).read_bytes()
    async with dev_node(home=home, seed=False):
        pass
    assert keyfile_path(home).read_bytes() == key  # a second start keeps the vault it made


async def test_a_kept_home_ends_the_grant_an_earlier_start_printed(tmp_path):
    home = tmp_path / "node"
    async with dev_node(home=home, seed=False) as first:
        old = first.agent_token
    async with dev_node(home=home, seed=False) as second:
        assert second.agent_token != old
        for token, works in ((old, False), (second.agent_token, True)):
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
                answer = await http.post(second.mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                         headers={"Accept": "application/json, text/event-stream"})
            assert (answer.status_code != 401) is works, (works, answer.status_code, answer.text[:200])


async def test_seeding_a_persistent_home_twice_adds_nothing_twice(tmp_path):
    home = tmp_path / "node"
    prepare_home(home)
    runtime, lanes = build_runtime(NodeConfig(name="dev", home=home, mcp_token="seed-test-token"))
    await seed(runtime, lanes)
    await seed(runtime, lanes)  # the lanes are new in memory; the workspaces, flow and approvals are kept
    pending = [a.summary for a in runtime.approvals.list(state="pending")]
    assert pending.count(SEED_SUMMARY) == 1 and pending.count(SEED_QUESTION) == 1
    assert {ws.id for ws in runtime.workspaces.list_workspaces()} == {"biz-a", "biz-b"}
    await lanes.shutdown()
