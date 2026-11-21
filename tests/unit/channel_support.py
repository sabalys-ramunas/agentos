"""Helpers for the controller and channel tests: a real controller on a free port, nodes with
fake lanes and the real policy and channel services, and an MCP client for agents."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
from fakes import FakeInteraction, FakeLaneManager
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from openajar.controller.app import Controller, serve_controller
from openajar.controller.config import ControllerConfig
from openajar.core.config import NodeConfig
from openajar.core.models import Effect, LaneSpec
from openajar.core.tools import ToolSpec
from openajar.mcp import providers as mcp_providers
from openajar.node.channel.enroll import enroll
from openajar.node.channel.node import Timings
from openajar.node.daemon import build_services, node_runtime, serve
from openajar.policy.ceiling import ControllerCeiling

AGENT_TOKEN = "aga_test_agent_token_for_the_controller"
OWNER_TOKEN = "owner-token-for-controller-tests"
FAST = Timings(ping_interval=1.0, dead_after=5.0, heartbeat=0.5, backoff_min=0.05, backoff_max=0.3,
               handshake=5.0, lane_poll=0.2, trace_flush=0.1, housekeeping=5.0)


async def post_note(ctx, *, on: str, text: str) -> dict:
    """Post a short note on a lane (a COMMIT, so it waits for the owner's approval)."""
    await ctx.authorize(Effect.COMMIT, lane_id=on, summary=f"Post a note on {on}: {text}", content={"text": text})
    return {"posted": text, "on": on}


async def slow_tool(ctx, *, seconds: float = 5.0) -> dict:
    """Sleep, then answer (for cancel and deadline tests)."""
    await ctx.authorize(Effect.READ)
    await asyncio.sleep(seconds)
    return {"slept": seconds}


def extra_tools(runtime: Any) -> list[ToolSpec]:
    return [ToolSpec(name="post_note", handler=post_note, effect=Effect.COMMIT),
            ToolSpec(name="slow_tool", handler=slow_tool, lane_arg=None)]


@dataclass
class RunningController:
    controller: Controller
    config: ControllerConfig
    port: int
    stop: asyncio.Event
    task: asyncio.Task

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"

    @property
    def node_url(self) -> str:
        return f"ws://localhost:{self.port}/v1/node/connect"

    @property
    def org(self) -> str:
        return self.controller.org["id"]

    async def close(self) -> None:
        self.stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.task, 15)


async def start_controller(home: Path, **overrides: Any) -> RunningController:
    config = ControllerConfig(home=home, port=0, agent_token=AGENT_TOKEN, owner_token=OWNER_TOKEN, **overrides)
    stop = asyncio.Event()
    started: asyncio.Future = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(serve_controller(config, stop=stop,
                                                on_started=lambda port, c: started.set_result((port, c))))
    port, controller = await asyncio.wait_for(started, 15)
    return RunningController(controller, config.model_copy(update={"port": port}), port, stop, task)


@dataclass
class RunningNode:
    name: str
    config: NodeConfig
    runtime: Any
    lanes: FakeLaneManager
    interaction: FakeInteraction
    stop: asyncio.Event
    task: asyncio.Task

    @property
    def channel(self) -> Any:
        return self.runtime.get("channel")

    async def close(self) -> None:
        self.stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.task, 15)


async def join_node(home: Path, name: str, ctl: RunningController, *, org_id: str | None = None) -> NodeConfig:
    config = NodeConfig(home=home, name=name, mcp_token=f"node-token-{name}", mcp_port=0)
    home.mkdir(parents=True, exist_ok=True)
    _, token = ctl.controller.create_join_token(org_id or ctl.org, name=name)
    # the owner's own controller, as `openajar up` joins it: the full ceiling (test_controller_ceiling.py
    # covers the hosted default)
    return await enroll(config, ctl.node_url, token, ceiling=ControllerCeiling.full())


async def start_node(home: Path, name: str, ctl: RunningController, lanes: list[str], *,
                     org_id: str | None = None, config: NodeConfig | None = None) -> RunningNode:
    config = config or await join_node(home, name, ctl, org_id=org_id)
    manager = FakeLaneManager(config)
    for lane in lanes:
        await manager.create(LaneSpec(id=lane))
    interaction = FakeInteraction()
    runtime = node_runtime(config, manager, interaction)
    build_services(runtime, ["openajar.policy.service", "openajar.node.channel.service"])
    runtime.get("channel").timings = FAST
    runtime.get("channel")._backoff.low, runtime.get("channel")._backoff.high = FAST.backoff_min, FAST.backoff_max
    stop = asyncio.Event()
    providers = [*mcp_providers.tool_providers(), extra_tools]
    task = asyncio.create_task(serve(config, runtime, stop=stop, providers=providers))
    channel = runtime.get("channel")
    await asyncio.wait_for(channel.welcomed.wait(), 15)
    return RunningNode(name, config, runtime, manager, interaction, stop, task)


async def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def agent(ctl: RunningController, token: str = AGENT_TOKEN):
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60) as http,
        Client(streamable_http_client(f"{ctl.url}/mcp", http_client=http), mode="legacy") as client,
    ):
        yield client


def owner(ctl: RunningController) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(base_url=ctl.url, headers={"Authorization": f"Bearer {OWNER_TOKEN}"}, timeout=30)
