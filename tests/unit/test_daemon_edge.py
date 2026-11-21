"""agentosd edge cases: signals during startup and in-flight calls, startup failures, bind address.

The browser side is the real LaneManagerImpl over edge_lanes (fake engine), so "the browser was
closed" is something the test can check. Grace periods are shortened with monkeypatch.
"""

import asyncio
import json
import logging
import os
import signal
import socket
import time

import httpx2
import pytest
from edge_lanes import make_manager
from fakes import FakeInteraction

from openajar.core.config import NodeConfig
from openajar.core.errors import OpenajarError
from openajar.core.models import LaneSpec, NavigateResult, Signal
from openajar.node import daemon
from openajar.node.daemon import node_runtime, serve

TOKEN = "tok"
URL = "http://127.0.0.1:{port}/mcp"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {TOKEN}",
}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}


class SlowNavigate(FakeInteraction):
    """A navigate that takes `delay` seconds, and records whether it finished or was cancelled."""

    def __init__(self, delay: float):
        super().__init__()
        self.delay = delay
        self.started = asyncio.Event()
        self.finished = False
        self.cancelled = False

    async def navigate(self, page, url):
        self.started.set()
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished = True
        return NavigateResult(url=url, title="slow", status=200, signal=Signal.OK)


async def start(config, manager, interaction=None, **kwargs):
    port: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(serve(config, node_runtime(config, manager, interaction or FakeInteraction()),
                                     on_started=port.set_result, **kwargs))
    return task, await asyncio.wait_for(port, 10)


async def open_session(http: httpx2.AsyncClient, url: str) -> dict[str, str]:
    init = await http.post(url, json=INIT, headers=MCP_HEADERS)
    headers = MCP_HEADERS | {"mcp-session-id": init.headers["mcp-session-id"],
                             "mcp-protocol-version": "2025-06-18"}
    await http.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
    return headers


async def call_tool(http: httpx2.AsyncClient, url: str, headers: dict[str, str], name: str, args: dict) -> dict:
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}}
    response = await http.post(url, json=body, headers=headers)
    events = [json.loads(line[len("data:"):]) for line in response.text.splitlines() if line.startswith("data:")]
    return events[-1] if events else json.loads(response.text)


def record_shutdown(manager) -> list[str]:
    calls: list[str] = []
    original = manager.shutdown

    async def shutdown():
        calls.append("shutdown")
        await original()

    manager.shutdown = shutdown
    return calls


@pytest.fixture
def config(tmp_path):
    return NodeConfig(home=tmp_path, mcp_token=TOKEN, mcp_port=0, self_check=False)


async def test_signal_during_lane_start_skips_serving_but_closes_lanes(config, tmp_path):
    manager, _, _ = make_manager(tmp_path, mcp_token=TOKEN, mcp_port=0)
    started = asyncio.Event()
    real_start = manager.start

    async def slow_start():
        started.set()
        await asyncio.sleep(0.3)
        await real_start()

    manager.start = slow_start
    shutdowns = record_shutdown(manager)
    port: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(serve(config, node_runtime(config, manager, FakeInteraction()), on_started=port.set_result))
    await started.wait()
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 15)
    assert not port.done(), "the server must not start once a stop was requested"
    assert shutdowns == ["shutdown"]


async def test_call_in_flight_inside_the_grace_window_gets_its_result(tmp_path, config, monkeypatch):
    monkeypatch.setattr(daemon, "SHUTDOWN_GRACE", 5.0)
    manager, engine, _ = make_manager(tmp_path, mcp_token=TOKEN, mcp_port=0, self_check=False)
    await manager.create(LaneSpec(id="personal/reddit"))
    interaction = SlowNavigate(delay=0.8)
    task, port = await start(config, manager, interaction)
    url = URL.format(port=port)
    async with httpx2.AsyncClient(timeout=30) as http:
        headers = await open_session(http, url)
        pending = asyncio.create_task(call_tool(http, url, headers, "browser_navigate",
                                                {"on": "personal/reddit", "url": "https://example.com"}))
        await interaction.started.wait()
        os.kill(os.getpid(), signal.SIGTERM)
        reply = await asyncio.wait_for(pending, 30)
    await asyncio.wait_for(task, 30)

    assert reply["result"]["isError"] is False, reply
    assert json.loads(reply["result"]["content"][0]["text"])["url"] == "https://example.com"
    assert interaction.finished and not interaction.cancelled
    assert engine.running == set() and all(s.closed for s in engine.sessions)


async def test_call_still_running_past_the_grace_window_is_cut_off_and_its_browser_closed(
        tmp_path, config, monkeypatch):
    monkeypatch.setattr(daemon, "SHUTDOWN_GRACE", 0.3)
    manager, engine, _ = make_manager(tmp_path, mcp_token=TOKEN, mcp_port=0, self_check=False)
    await manager.create(LaneSpec(id="personal/reddit"))
    interaction = SlowNavigate(delay=30)
    task, port = await start(config, manager, interaction)
    url = URL.format(port=port)
    async with httpx2.AsyncClient(timeout=30) as http:
        headers = await open_session(http, url)
        pending = asyncio.create_task(call_tool(http, url, headers, "browser_navigate",
                                                {"on": "personal/reddit", "url": "https://example.com"}))
        await interaction.started.wait()
        os.kill(os.getpid(), signal.SIGTERM)
        with pytest.raises(httpx2.RemoteProtocolError):  # the connection closes before the reply
            await asyncio.wait_for(pending, 30)
    await asyncio.wait_for(task, 30)

    assert interaction.cancelled and not interaction.finished
    assert engine.running == set() and all(s.closed for s in engine.sessions)


async def test_stop_requested_before_serve_binds_nothing_but_still_closes_lanes(tmp_path, config):
    manager, _, _ = make_manager(tmp_path, mcp_token=TOKEN, mcp_port=0)
    shutdowns = record_shutdown(manager)
    bound = []
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(serve(config, node_runtime(config, manager, FakeInteraction()), stop=stop,
                                 on_started=bound.append), 15)
    assert bound == [] and shutdowns == ["shutdown"]


async def test_lanes_still_close_when_the_port_is_already_taken(tmp_path):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        config = NodeConfig(home=tmp_path, mcp_token=TOKEN, mcp_port=blocker.getsockname()[1], self_check=False)
        manager, _, _ = make_manager(tmp_path, mcp_token=TOKEN, mcp_port=config.mcp_port)
        shutdowns = record_shutdown(manager)
        with pytest.raises(SystemExit):
            await asyncio.wait_for(serve(config, node_runtime(config, manager, FakeInteraction())), 15)
        assert shutdowns == ["shutdown"]
    finally:
        blocker.close()


async def test_a_lane_shutdown_that_hangs_is_abandoned_after_the_timeout(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(daemon, "LANES_SHUTDOWN_TIMEOUT", 0.2)
    manager, _, _ = make_manager(tmp_path, mcp_token=TOKEN, mcp_port=0)

    async def hangs():
        await asyncio.sleep(30)

    manager.shutdown = hangs
    stop = asyncio.Event()
    stop.set()
    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger="openajar.node.daemon"):
        config = NodeConfig(home=tmp_path, mcp_token=TOKEN)
        await asyncio.wait_for(serve(config, node_runtime(config, manager, FakeInteraction()), stop=stop), 15)
    assert time.monotonic() - started < 5
    assert "lanes did not close within" in caplog.text


async def test_no_browser_starts_after_the_lanes_have_shut_down(tmp_path):
    manager, engine, _ = make_manager(tmp_path)
    await manager.create(LaneSpec(id="personal/reddit"))
    await manager.open("personal/reddit")
    await manager.shutdown()
    with pytest.raises(OpenajarError):
        async with manager.using("personal/reddit"):
            pass
    assert engine.launches == ["personal/reddit"], engine.launches


async def test_mcp_host_must_be_loopback(tmp_path):
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(RuntimeError, match="loopback"):
        config = NodeConfig(home=tmp_path, mcp_token=TOKEN, mcp_host="0.0.0.0", mcp_port=0)
        await serve(config, node_runtime(config, make_manager(tmp_path)[0], FakeInteraction()), stop=stop)


def test_mcp_bind_any_is_an_explicit_opt_in():
    from openajar.core.config import check_mcp_bind

    for host in ("127.0.0.1", "::1", "localhost", "127.0.0.2"):
        check_mcp_bind(NodeConfig(mcp_host=host))
    with pytest.raises(RuntimeError, match="loopback"):
        check_mcp_bind(NodeConfig(mcp_host="10.0.0.5"))
    check_mcp_bind(NodeConfig(mcp_host="0.0.0.0", mcp_bind_any=True))
