"""The plugin gate in agentosd: a plugin's services and tools load only when it is on, the fixed
feature lists stay as they are, and nothing a plugin does stops the node."""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from typing import Annotated

import pytest
from fakes import FakeInteraction, FakeLaneManager
from mcp import Client
from pydantic import Field

from agentos.core.config import NodeConfig
from agentos.core.errors import ServiceUnavailable
from agentos.core.tools import ToolSpec
from agentos.mcp import providers as mcp_providers
from agentos.mcp.registry import allow_all_wrap, build_mcp, collect_specs, tool_params
from agentos.node import daemon
from agentos.node.daemon import build_plugins, build_services, node_runtime, start_services, stop_services
from agentos.plugins.errors import PluginDisabled
from agentos.plugins.models import Plugin, Stage
from agentos.plugins.service import SERVICE_NAME, PluginService
from agentos.plugins.store import FILE_NAME, PluginStore

SERVICE = "agentos.fakeworker.service"
TOOLS = "agentos.fakeworker.tools"
WORKER = Plugin(id="fake-worker", name="Fake", description="d", stage=Stage.WORKS, lane_kinds=(),
                services=(SERVICE,), tools=(TOOLS,))


class Worker:
    def __init__(self, events, *, start_fails=False, start_hangs=False):
        self.events, self.start_fails, self.start_hangs = events, start_fails, start_hangs

    async def start(self):
        self.events.append("start")
        if self.start_fails:
            raise RuntimeError("no engine")
        if self.start_hangs:
            await asyncio.sleep(60)

    async def stop(self):
        self.events.append("stop")


async def worker_do(ctx, *, thing: Annotated[str, Field(description="What to do.")], times: int = 1) -> dict:
    """Do a thing."""
    return {"did": thing, "times": times}


def module(monkeypatch, name, **attrs):
    fake = types.ModuleType(name)
    fake.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, fake)


@pytest.fixture
def config(tmp_path):
    return NodeConfig(home=tmp_path)


@pytest.fixture
def runtime(config):
    return node_runtime(config, FakeLaneManager(config), FakeInteraction())


@pytest.fixture
def registry(monkeypatch, config):
    """The real service and the real gate, with one test plugin in place of the shipped ones."""
    def build(runtime):
        service = PluginService(runtime, home=config.home, plugins=(WORKER,))
        runtime.register(SERVICE_NAME, service)
        return service

    module(monkeypatch, "agentos.fakeplugins", build=build)
    monkeypatch.setattr(daemon, "PLUGINS_SERVICE", "agentos.fakeplugins")


@pytest.fixture
def worker(monkeypatch):
    events: list[str] = []
    module(monkeypatch, "agentos.fakeworker")
    module(monkeypatch, SERVICE, build=lambda runtime: runtime.register("fakeworker", Worker(events)) or
           runtime.get("fakeworker"))
    module(monkeypatch, TOOLS, tool_specs=lambda runtime: [ToolSpec(name="worker_do", handler=worker_do, lane_arg=None)])
    return events


def turn_on(config):
    PluginStore(config.home).set_enabled(WORKER.id, True)


def service_names(runtime):
    return [name for name, _ in runtime.get(daemon.SERVICES_KEY) or []]


def served(runtime):
    return {spec.name: spec for spec in collect_specs(runtime, mcp_providers.tool_providers())}


def test_the_fixed_lists_hold_no_plugin_module():
    fixed = {*daemon.FEATURE_SERVICES, *mcp_providers.FEATURE_TOOLS}
    assert not {name for name in fixed if name.startswith("agentos.plugins")}
    assert daemon.REQUIRED_SERVICES == {"agentos.policy.service", "agentos.freeze.service"}


def test_a_real_node_with_nothing_on_loads_only_the_registry(runtime):
    build_plugins(runtime)
    assert isinstance(runtime.get("plugins"), PluginService)
    assert service_names(runtime) == []
    assert "plugins_list" in served(runtime)


def test_off_means_nothing_loads(runtime, registry, worker):
    build_plugins(runtime)
    assert service_names(runtime) == [] and worker == []
    assert "worker_do" not in served(runtime)


async def test_on_loads_its_services_and_tools_and_they_run(runtime, registry, worker, config):
    turn_on(config)
    build_plugins(runtime)
    assert service_names(runtime) == [SERVICE]
    tools = served(runtime)
    assert "worker_do" in tools
    await start_services(runtime)
    assert worker == ["start"]
    assert await tools["worker_do"].handler(None, thing="x") == {"did": "x", "times": 1}
    await stop_services(runtime)
    assert worker == ["start", "stop"]


def test_a_gated_tool_keeps_its_schema(runtime, registry, worker, config):
    turn_on(config)
    build_plugins(runtime)
    gated = tool_params(served(runtime)["worker_do"])
    plain = tool_params(ToolSpec(name="worker_do", handler=worker_do, lane_arg=None))
    assert [p.name for p in gated] == ["thing", "times"]
    assert [(p.name, p.required, p.description) for p in gated] == [(p.name, p.required, p.description) for p in plain]


async def test_a_disabled_plugin_stops_its_tools_without_a_restart(runtime, registry, worker, config):
    turn_on(config)
    build_plugins(runtime)
    tool = served(runtime)["worker_do"]
    PluginStore(config.home).set_enabled(WORKER.id, False)
    with pytest.raises(PluginDisabled, match="Only a person"):
        await tool.handler(None, thing="x")


async def test_over_real_mcp_an_agent_is_told_a_person_must_act(runtime, registry, worker, config):
    """The whole path: the MCP client, the registry, the gate, and the catalog's next step."""
    turn_on(config)
    build_plugins(runtime)
    server = build_mcp(runtime, mcp_providers.tool_providers(), allow_all_wrap)
    async with Client(server) as client:
        done = await client.call_tool("worker_do", {"thing": "x"})
        assert not done.is_error
        PluginStore(config.home).set_enabled(WORKER.id, False)
        refused = await client.call_tool("worker_do", {"thing": "x"})
        asked = await client.call_tool("plugins_list", {"plugin": WORKER.id})
        listed = await client.call_tool("plugins_list", {})
    for result in (refused, asked):
        text = result.content[0].text
        assert result.is_error and text.startswith("plugin_disabled: fake-worker is off on this node.")
        assert "Only a person can turn it on" in text and "fix: Do not retry" in text
    assert not listed.is_error and "fake-worker" in listed.content[0].text


def test_a_broken_state_file_loads_nothing_and_the_node_goes_on(runtime, registry, worker, config, caplog):
    turn_on(config)
    (config.home / FILE_NAME).write_bytes(b"x" * 4096)
    with caplog.at_level(logging.ERROR):
        build_plugins(runtime)
    assert service_names(runtime) == [] and "worker_do" not in served(runtime)
    assert "no plugin is loaded" in caplog.text


def test_a_registry_that_cannot_build_stops_nothing(runtime, monkeypatch, caplog):
    module(monkeypatch, "agentos.fakeplugins", build=lambda runtime: 1 / 0)
    monkeypatch.setattr(daemon, "PLUGINS_SERVICE", "agentos.fakeplugins")
    with caplog.at_level(logging.ERROR):
        build_plugins(runtime)
    assert "no plugin is loaded" in caplog.text


def test_a_plugin_that_fails_to_build_does_not_stop_the_node(runtime, registry, worker, config, monkeypatch):
    module(monkeypatch, SERVICE, build=lambda runtime: 1 / 0)
    turn_on(config)
    build_plugins(runtime)
    assert service_names(runtime) == []
    assert "worker_do" not in served(runtime)
    view = runtime.get("plugins").view(WORKER.id)
    assert view.status == "failed" and SERVICE in view.failure


def test_a_plugin_whose_module_is_missing_is_failed_not_fatal(runtime, registry, config, monkeypatch):
    module(monkeypatch, "agentos.fakeworker")
    monkeypatch.delitem(sys.modules, SERVICE, raising=False)
    turn_on(config)
    build_plugins(runtime)
    assert runtime.get("plugins").view(WORKER.id).status == "failed"


def test_a_build_that_registers_no_service_is_failed(runtime, config, monkeypatch, worker):
    from agentos.plugins.requirements import NodeFacts, ServicePresent

    plugin = Plugin(id=WORKER.id, name="F", description="d", stage=Stage.WORKS, lane_kinds=(), services=(SERVICE,),
                    needs=(ServicePresent("fakeworker", SERVICE),))

    def build(runtime):
        service = PluginService(runtime, home=runtime.config.home, plugins=(plugin,),
                                facts=NodeFacts(runtime=runtime, installed=lambda name: True))
        runtime.register(SERVICE_NAME, service)
        return service

    module(monkeypatch, "agentos.fakeplugins", build=build)
    monkeypatch.setattr(daemon, "PLUGINS_SERVICE", "agentos.fakeplugins")
    module(monkeypatch, SERVICE, build=lambda runtime: Worker([]))  # builds, and never registers its name
    turn_on(config)
    build_plugins(runtime)
    view = runtime.get("plugins").view(WORKER.id)
    assert view.status == "failed" and "not running" in view.failure


async def test_a_plugin_that_fails_to_start_does_not_stop_the_node(runtime, registry, worker, config, monkeypatch):
    events: list[str] = []
    module(monkeypatch, SERVICE, build=lambda runtime: Worker(events, start_fails=True))
    turn_on(config)
    build_plugins(runtime)
    tool = served(runtime)["worker_do"]
    await start_services(runtime)
    assert events == ["start"]
    plugins = runtime.get("plugins")
    assert plugins.view(WORKER.id).status == "failed"
    with pytest.raises(ServiceUnavailable, match="failed to start"):
        await tool.handler(None, thing="x")


async def test_a_plugin_that_hangs_on_start_does_not_stop_the_node(runtime, registry, worker, config, monkeypatch):
    module(monkeypatch, SERVICE, build=lambda runtime: Worker([], start_hangs=True))
    monkeypatch.setattr(daemon, "PLUGIN_START_TIMEOUT", 0.01)
    turn_on(config)
    build_plugins(runtime)
    await asyncio.wait_for(start_services(runtime), 5)
    assert runtime.get("plugins").view(WORKER.id).status == "failed"


async def test_a_required_service_still_refuses_to_start(runtime, registry, monkeypatch):
    name = next(iter(daemon.REQUIRED_SERVICES))
    module(monkeypatch, name, build=lambda runtime: Worker([], start_fails=True))
    build_services(runtime, [name])
    with pytest.raises(RuntimeError, match="refusing to serve"):
        await start_services(runtime)


def test_a_tool_name_that_clashes_fails_the_plugin_not_the_server(runtime, registry, worker, config, monkeypatch):
    module(monkeypatch, TOOLS, tool_specs=lambda runtime: [ToolSpec(name="lane_open", handler=worker_do, lane_arg=None)])
    turn_on(config)
    build_plugins(runtime)
    collect_specs(runtime, mcp_providers.tool_providers())  # would raise on a duplicate
    view = runtime.get("plugins").view(WORKER.id)
    assert view.status == "failed" and "lane_open" in view.failure


async def test_a_clashing_plugin_starts_nothing(runtime, registry, worker, config, monkeypatch):
    module(monkeypatch, TOOLS, tool_specs=lambda runtime: [ToolSpec(name="lane_open", handler=worker_do, lane_arg=None)])
    turn_on(config)
    build_plugins(runtime)
    served(runtime)
    await start_services(runtime)
    assert worker == []


def test_tools_that_will_not_load_fail_the_plugin(runtime, registry, worker, config, monkeypatch):
    module(monkeypatch, TOOLS, tool_specs=lambda runtime: 1 / 0)
    turn_on(config)
    build_plugins(runtime)
    served(runtime)
    assert runtime.get("plugins").view(WORKER.id).failure == "its tools did not load"


def test_a_module_already_in_the_fixed_lists_is_not_loaded_twice(runtime, registry, worker, config, monkeypatch):
    """When a feature is also listed as always on, the fixed list wins and nothing breaks."""
    monkeypatch.setattr(daemon, "FEATURE_SERVICES", (*daemon.FEATURE_SERVICES, SERVICE))
    monkeypatch.setattr(mcp_providers, "FEATURE_TOOLS", (*mcp_providers.FEATURE_TOOLS, TOOLS))
    build_services(runtime, [SERVICE])
    turn_on(config)
    build_plugins(runtime)
    assert service_names(runtime).count(SERVICE) == 1
    names = [spec.name for spec in collect_specs(runtime, mcp_providers.tool_providers(mcp_providers.FEATURE_TOOLS))]
    assert names.count("worker_do") == 1


def test_the_controller_channel_stays_the_last_service(runtime, registry, worker, config, monkeypatch):
    module(monkeypatch, daemon.LAST_SERVICE, build=lambda runtime: Worker([]))
    module(monkeypatch, "agentos.fakefirst", build=lambda runtime: Worker([]))
    build_services(runtime, ["agentos.fakefirst", daemon.LAST_SERVICE])
    turn_on(config)
    build_plugins(runtime)
    assert service_names(runtime) == ["agentos.fakefirst", SERVICE, daemon.LAST_SERVICE]


def test_a_caller_that_names_its_modules_gets_only_those(runtime):
    assert len(mcp_providers.tool_providers(["agentos.plugins.tools"])) == 2  # the node's own, and that one


def test_build_runtime_goes_through_the_gate(config, monkeypatch, registry, worker):
    monkeypatch.setattr(daemon, "_build_lane_manager", lambda config: FakeLaneManager(config))
    monkeypatch.setattr(daemon, "_build_interaction", lambda config: FakeInteraction())
    turn_on(config)
    runtime = daemon.build_runtime(config, services=())
    assert service_names(runtime) == [SERVICE]
