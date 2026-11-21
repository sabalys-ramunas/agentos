"""The local service keeps idle connections open, so a client never reuses one the node closed
(Bun's fetch in Claude Code got bare ECONNRESET errors when uvicorn closed them after 5 s), and
several chats' long and short calls run at once on it."""

from __future__ import annotations

import asyncio
import socket

import pytest
from fakes import mcp_port_for_tests

from openajar.core.config import NodeConfig
from openajar.core.models import Effect
from openajar.core.runtime import Runtime
from openajar.core.tools import ToolSpec
from openajar.mcp.http import build_app
from openajar.mcp.registry import build_mcp
from openajar.node import daemon

TOKEN = "k" * 40


def test_idle_connections_outlive_an_agent_thinking_between_calls(tmp_path):
    server = daemon._Uvicorn.server(lambda *a: None, NodeConfig(home=tmp_path, mcp_port=0))
    assert server.config.timeout_keep_alive >= 300


async def wait_a_bit(ctx, *, seconds: float = 0.0) -> dict:
    """Sleeps."""
    await asyncio.sleep(seconds)
    return {"slept": seconds}


@pytest.fixture
async def served(tmp_path):
    config = NodeConfig(home=tmp_path, mcp_token=TOKEN, mcp_port=mcp_port_for_tests())
    runtime = Runtime(config=config)
    specs = [ToolSpec(name="wait_a_bit", handler=wait_a_bit, effect=Effect.READ)]
    server = daemon._Uvicorn.server(build_app(runtime, build_mcp(runtime, [lambda rt: specs])), config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        yield daemon.bound_port(server)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)


def _read_response(s: socket.socket) -> bytes:
    """One whole HTTP response off the socket (headers, then Content-Length bytes of body), so the next
    read starts at the next response, however the body was split into packets."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = s.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = next((int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                   if line.lower().startswith(b"content-length:")), 0)
    while len(body) < length:
        chunk = s.recv(65536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


def _get_twice(port: int, idle_s: float) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    ask = b"GET /mcp HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n"
    s.sendall(ask)
    _read_response(s)
    import time
    time.sleep(idle_s)
    s.sendall(ask)
    return _read_response(s)


async def test_a_pooled_connection_still_answers_after_an_idle_pause(served):
    # a pause and the same socket again (scratchpad repro: at 6 s uvicorn's 5 s default had closed it)
    got = await asyncio.to_thread(_get_twice, served, 1.2)
    assert got.startswith(b"HTTP/1.1 ")


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_three_chats_with_long_and_short_calls_at_once(served, mode):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    url = f"http://localhost:{served}/mcp"

    async def chat(n: int) -> list[bool]:
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http,
            Client(streamable_http_client(url, http_client=http), mode=mode) as client,
        ):
            calls = [client.call_tool("wait_a_bit", {"seconds": 0.8 if i == 0 else 0.01}) for i in range(6)]
            results = await asyncio.gather(*calls)
            await asyncio.sleep(0.3)  # a pause, then the pooled connections again
            results += [await client.call_tool("wait_a_bit", {"seconds": 0.01})]
            return [not r.is_error for r in results]

    outcomes = await asyncio.gather(*(chat(n) for n in range(3)))
    assert all(all(o) for o in outcomes)
