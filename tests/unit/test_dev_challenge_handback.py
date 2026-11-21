"""A challenge on screen holds the lane whichever page read sees it, and a hand-back with the
challenge still showing holds it again at once (QA 2026-11-07 integration 2, finding 1).

The QA steps word for word, on the dev node over MCP (agent grant) and the panel API (owner)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_dev_up import dev_node

from openajar.dev.web import CHALLENGE_URL

ON = {"on": "biz-a/shop"}


@asynccontextmanager
async def owner(node):
    """The panel, signed in with the printed code (a person: hand-backs need one)."""
    async with httpx2.AsyncClient(base_url=f"http://localhost:{node.port}") as panel:
        code = parse_qs(urlsplit(node.panel_link).fragment)["code"][0]
        assert (await panel.post("/api/session", json={"code": code})).status_code == 200
        panel.headers["X-Agentos-CSRF"] = (await panel.get("/api/status")).json()["csrf"]
        yield panel


async def test_a_hand_back_with_the_captcha_still_showing_holds_the_lane_again():
    async with (
        dev_node() as node,
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {node.agent_token}"}) as http,
        Client(streamable_http_client(node.mcp_url, http_client=http), mode="legacy") as agent,
        owner(node) as panel,
    ):
        assert not (await agent.call_tool("browser_open", ON)).is_error
        went = json.loads((await agent.call_tool("browser_navigate", {**ON, "url": CHALLENGE_URL})).content[0].text)
        assert went["signal"] == "captcha"
        control = json.loads((await agent.call_tool("browser_control", ON)).content[0].text)
        assert control["agent_may_act"] is False and "needs_human:captcha" in json.dumps(control)

        back = await panel.post("/api/lanes/biz-a/shop/control", json={"action": "hand_back"})
        assert back.status_code == 200, back.text
        body = back.json()
        assert body["control"]["state"] == "paused" and body["control"]["reason"].startswith("needs_human:captcha")
        assert "captcha" in body["warning"] and "—" not in body["warning"]

        snap = await agent.call_tool("browser_snapshot", ON)
        assert snap.is_error and "agent_paused" in snap.content[0].text, snap.content[0].text
        control = json.loads((await agent.call_tool("browser_control", ON)).content[0].text)
        assert control["agent_may_act"] is False
        clicked = await agent.call_tool("browser_act", {**ON, "ref": "e1", "action": "click"})
        assert clicked.is_error and "agent_paused" in clicked.content[0].text


async def test_after_the_person_leaves_the_challenge_the_hand_back_gives_the_lane_back():
    async with (
        dev_node() as node,
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {node.agent_token}"}) as http,
        Client(streamable_http_client(node.mcp_url, http_client=http), mode="legacy") as agent,
        owner(node) as panel,
    ):
        await agent.call_tool("browser_open", ON)
        await agent.call_tool("browser_navigate", {**ON, "url": CHALLENGE_URL})
        node.lanes._page_for("biz-a/shop").url = "https://social.example/"  # the person solved it
        back = (await panel.post("/api/lanes/biz-a/shop/control", json={"action": "hand_back"})).json()
        assert back["control"]["state"] == "agent" and "warning" not in back
        snap = await agent.call_tool("browser_snapshot", ON)
        assert not snap.is_error and "signal: ok" in snap.content[0].text


async def test_every_page_read_of_a_challenge_holds_the_lane():
    """A lane given back while it shows a challenge (an older node, a hand-back with no browser read):
    the first read of any kind holds it before the agent can act."""
    for tool, args in (("browser_snapshot", {}), ("browser_screenshot", {}), ("browser_tabs", {}),
                       ("browser_dialog", {}), ("browser_wait", {"for_text": "robot", "wait_s": 0})):
        async with (
            dev_node() as node,
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {node.agent_token}"}) as http,
            Client(streamable_http_client(node.mcp_url, http_client=http), mode="legacy") as agent,
        ):
            lanes = node.lanes
            await agent.call_tool("browser_open", ON)
            lanes._page_for("biz-a/shop").url = CHALLENGE_URL  # on screen, never read by a watched call
            read = await agent.call_tool(tool, {**ON, **args})
            # the dev engine tracks no tabs and queues no dialogs: those two answer unavailable,
            # after the read of the page they still made
            assert not read.is_error or "unavailable" in read.content[0].text, (tool, read.content[0].text)
            control = json.loads((await agent.call_tool("browser_control", ON)).content[0].text)
            assert control["agent_may_act"] is False, tool
            snap = await agent.call_tool("browser_snapshot", ON)
            assert snap.is_error and "agent_paused" in snap.content[0].text, tool
