"""QA for `openajar up` (openajar.controller.up.run_up) across restarts: a node that keeps its identity, and
a node the owner removed in the panel. Bugs are strict xfails pinned to their reason."""

from __future__ import annotations

import asyncio

import pytest
from channel_support import wait_for
from fakes import FakeInteraction, FakeLaneManager

from openajar.controller.config import ControllerConfig, save_controller_config
from openajar.controller.store import ControllerStore
from openajar.controller.up import run_up
from openajar.core.config import NodeConfig, load_config, save_config
from openajar.core.models import LaneSpec
from openajar.node.daemon import build_services, node_runtime


def builder(built: dict):
    def build(config):
        manager = FakeLaneManager(config)
        asyncio.get_event_loop().create_task(manager.create(LaneSpec(id="personal/reddit")))
        runtime = node_runtime(config, manager, FakeInteraction())
        build_services(runtime, ["openajar.policy.service", "openajar.node.channel.service"])
        built["runtime"] = runtime
        return runtime

    return build


def start(node_home, ctl_cfg, built: dict) -> tuple[asyncio.Task, asyncio.Event]:
    stop = asyncio.Event()
    task = asyncio.create_task(run_up(load_config(node_home), ctl_cfg, stop=stop, build_runtime=builder(built)))
    return task, stop


async def until_connected(built: dict, timeout: float = 20) -> None:
    try:
        await wait_for(lambda: "runtime" in built and built["runtime"].get("channel") is not None
                       and built["runtime"].channel.connected, timeout=timeout)
    except TimeoutError:
        channel = built["runtime"].get("channel") if "runtime" in built else None
        raise AssertionError(f"the node never came online; its channel says: {getattr(channel, 'last_error', None)}"
                             ) from None


@pytest.fixture
async def home(tmp_path):
    node_home = tmp_path / "node"
    node_home.mkdir()
    save_config(NodeConfig(home=node_home, name="My Box", mcp_token="node-owner-token-restart", mcp_port=0))
    ctl_cfg = ControllerConfig(home=node_home / "controller", port=0, agent_token="aga_up_restart_agent_token",
                               owner_token="owner-restart")
    save_controller_config(ctl_cfg)
    return node_home, ctl_cfg


async def test_a_restart_keeps_the_node_identity_and_it_comes_back_online(home):
    node_home, ctl_cfg = home
    built: dict = {}
    task, stop = start(node_home, ctl_cfg, built)
    await until_connected(built)
    first_id = load_config(node_home).node_id
    stop.set()
    await asyncio.wait_for(task, 20)

    built2: dict = {}
    task, stop = start(node_home, ctl_cfg, built2)
    try:
        await until_connected(built2)
        assert load_config(node_home).node_id == first_id
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)


async def test_openajar_up_enrolls_again_a_node_the_owner_removed(home):
    node_home, ctl_cfg = home
    built: dict = {}
    task, stop = start(node_home, ctl_cfg, built)
    await until_connected(built)
    old_id = load_config(node_home).node_id
    stop.set()
    await asyncio.wait_for(task, 20)

    store = ControllerStore(ctl_cfg.db_path)
    try:
        store.remove_node(store.default_org()["id"], old_id)  # the owner removes the node in the panel
    finally:
        store.close()

    built2: dict = {}
    task, stop = start(node_home, ctl_cfg, built2)
    try:
        await until_connected(built2, timeout=10)
        assert load_config(node_home).node_id != old_id
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)
