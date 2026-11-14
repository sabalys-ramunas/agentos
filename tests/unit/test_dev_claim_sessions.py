"""A claim belongs to the agent, not to its MCP session (QA 2026-11-07 integration 2, finding 7):
the same agent on a new session passes, renews and releases its own claim, and another agent
still waits behind it."""

from __future__ import annotations

import json

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_dev_up import dev_node

from agentos.node.claims import Holder

ON = {"on": "biz-a/social"}


def session(node, token: str | None = None):
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token or node.agent_token}"})
    return http, Client(streamable_http_client(node.mcp_url, http_client=http), mode="legacy")


def body(result) -> dict:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


async def test_the_same_agent_on_a_new_session_keeps_its_own_claim():
    async with dev_node() as node:
        http, first = session(node)
        async with http, first:
            claimed = body(await first.call_tool("lane_claim", {**ON, "purpose": "a post"}))
            assert claimed["lease"]
        http, second = session(node)  # reconnected: a new MCP session, the same grant
        async with http, second:
            went = await second.call_tool("browser_navigate", {**ON, "url": "https://social.example/"})
            assert not went.is_error, went.content[0].text  # never waits behind its own claim
            queue = body(await second.call_tool("lane_queue", ON))
            assert queue["claim"]["yours"] is True
            released = body(await second.call_tool("lane_release", ON))
            assert released["released"] is True and "note" not in released


async def test_another_agent_never_passes_the_claim():
    async with dev_node() as node:
        lanes = node.lanes
        http, first = session(node)
        async with http, first:
            body(await first.call_tool("lane_claim", {**ON, "purpose": "a post"}))
            other = Holder(key="other|g2||s9", label="agent other", identity="other|g2|")
            queue = lanes.queue_info("biz-a/social", viewer=other)
            assert queue["claim"]["yours"] is False
            released = await lanes.release("biz-a/social", other)
            assert released["released"] is False
            assert lanes.queue_info("biz-a/social")["claim"] is not None
