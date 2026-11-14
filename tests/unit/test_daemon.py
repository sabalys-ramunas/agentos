"""agentosd lifecycle: start the lanes, serve, close every lane on SIGTERM/SIGINT."""

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import types

import httpx2
import pytest
from fakes import FakeInteraction, FakeLaneManager, mcp_port_for_tests
from starlette.testclient import TestClient

from agentos.core.config import NodeConfig
from agentos.core.errors import PolicyDenied
from agentos.core.models import Effect, Principal
from agentos.core.tools import ToolSpec
from agentos.mcp import providers as mcp_providers
from agentos.mcp.registry import allow_all_wrap
from agentos.node import daemon
from agentos.node.daemon import node_runtime, serve

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def config(tmp_path):
    return NodeConfig(home=tmp_path, mcp_token="tok", mcp_port=mcp_port_for_tests())


async def start(config, manager, **kwargs):
    port: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(serve(config, node_runtime(config, manager, FakeInteraction()), on_started=port.set_result,
                                     **kwargs))
    return task, await asyncio.wait_for(port, 10)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_signal_closes_lanes(config, sig):
    manager = FakeLaneManager(config)
    task, port = await start(config, manager)
    assert manager.started and not manager.shut_down
    async with httpx2.AsyncClient() as client:
        response = await client.get(f"http://127.0.0.1:{port}/mcp")
        assert response.status_code == 401
    os.kill(os.getpid(), sig)
    await asyncio.wait_for(task, 15)
    assert manager.shut_down
    # serve() removed its handlers, so the default ones are back.
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


async def test_stop_event(config):
    manager = FakeLaneManager(config)
    stop = asyncio.Event()
    task, _ = await start(config, manager, stop=stop)
    stop.set()
    await asyncio.wait_for(task, 15)
    assert manager.shut_down


async def test_lanes_close_even_if_start_fails(config):
    class Broken(FakeLaneManager):
        async def start(self):
            raise RuntimeError("store is locked")

    manager = Broken(config)
    with pytest.raises(RuntimeError, match="store is locked"):
        await serve(config, node_runtime(config, manager, FakeInteraction()))
    assert manager.shut_down


async def test_refuses_without_token(tmp_path):
    with pytest.raises(RuntimeError, match="agentos node init"):
        config = NodeConfig(home=tmp_path)
        await serve(config, node_runtime(config, FakeLaneManager(), FakeInteraction()))


def test_build_runtime_imports_lazily():
    """Importing the CLI, the daemon and the MCP server must not pull in a browser stack."""
    code = (
        "import sys, agentos.cli, agentos.node.daemon, agentos.node.tools, agentos.mcp.server\n"
        "bad = [m for m in ('camoufox', 'playwright', 'agentos.node.lanes', 'agentos.node.interaction')"
        " if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_composition_root_is_the_only_place_that_names_classes(monkeypatch, config):
    built = {}
    monkeypatch.setattr(daemon, "_build_lane_manager", lambda cfg: built.setdefault("manager", FakeLaneManager(cfg)))
    monkeypatch.setattr(daemon, "_build_interaction", lambda cfg: built.setdefault("interaction", FakeInteraction()))
    runtime = daemon.build_runtime(config)
    assert runtime.lanes is built["manager"] and runtime.interaction is built["interaction"]
    assert runtime.config is config
    assert daemon.build_lane_manager(config) is not None


# ---- MCP composition: feature providers and the policy seam ----

def feature_module(monkeypatch, name, **attrs):
    package, _, _ = name.rpartition(".")
    monkeypatch.setitem(sys.modules, package, types.ModuleType(package))
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


async def _extra(ctx, *, word: str) -> dict:
    """An extra tool from a feature package."""
    return {"word": word}


def test_feature_providers_skip_what_is_missing(monkeypatch, tmp_path, caplog):
    feature_module(monkeypatch, "featok.tools",
                   tool_specs=lambda runtime: [ToolSpec(name="feat_ok", handler=_extra, lane_arg=None)])
    feature_module(monkeypatch, "featfails.tools", tool_specs=lambda runtime: 1 / 0)
    feature_module(monkeypatch, "featnospecs.tools")
    (tmp_path / "featdep").mkdir()
    (tmp_path / "featdep" / "__init__.py").write_text("")
    (tmp_path / "featdep" / "tools.py").write_text("import a_dependency_nobody_installed\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    modules = ["agentos.nothere.tools", "featok.tools", "featfails.tools", "featnospecs.tools", "featdep.tools"]
    with caplog.at_level(logging.INFO, logger="agentos.node.daemon"):
        providers = mcp_providers.tool_providers(modules)
    levels = {r.getMessage().split()[0]: r.levelno for r in caplog.records}
    assert levels["agentos.nothere.tools"] == logging.INFO
    assert levels["featdep.tools"] == logging.ERROR and levels["featnospecs.tools"] == logging.ERROR
    assert len(providers) == 3  # the node's own tools, featok, featfails
    runtime = node_runtime(NodeConfig(), FakeLaneManager(), FakeInteraction())
    names = [spec.name for provide in providers for spec in provide(runtime) if isinstance(spec, ToolSpec)]
    from agentos.node import tools as node_tools

    own = [s for s in node_tools.tool_specs(runtime) if isinstance(s, ToolSpec)]
    assert "feat_ok" in names and "browser_act" in names and len(names) == len(own) + 1  # the node's own and feat_ok


async def test_the_daemon_serves_feature_tools(monkeypatch, config):
    feature_module(monkeypatch, "featok.tools",
                   tool_specs=lambda runtime: [ToolSpec(name="feat_ok", handler=_extra, lane_arg=None)])
    runtime = node_runtime(config, FakeLaneManager(config), FakeInteraction())
    app = daemon.build_node_app(config, runtime, providers=mcp_providers.tool_providers(["featok.tools"]))
    with TestClient(app, base_url="http://localhost:7400") as client:
        headers = MCP_HEADERS | {"Authorization": "Bearer tok"}
        assert client.post("/mcp", json=INIT, headers=headers).status_code == 200


def test_no_policy_service_means_allow_all(config):
    hooks = daemon.mcp_hooks(node_runtime(config, FakeLaneManager(), FakeInteraction()))
    assert hooks == daemon.McpHooks()


class FakePolicy:
    def resolve(self, token):
        if token != "grant-1":
            raise PolicyDenied("unknown token")
        return Principal(id="agent-1"), object()


def fake_policy_wrap(monkeypatch):
    seen = {}

    def wrap_tool(spec, runtime, *, error_factory=None):
        return allow_all_wrap(spec, runtime)

    @contextlib.contextmanager
    def bind_request_token(token, *, session=None, run_id=None, parent_id=None):
        seen["bound"] = (token, session)
        yield

    def visible_tools(token, specs, runtime):
        seen["visible"] = (token, runtime)
        return [s for s in specs if s.effect is Effect.READ]

    feature_module(monkeypatch, "fakepolicy.wrap", wrap_tool=wrap_tool, bind_request_token=bind_request_token,
                   visible_tools=visible_tools)
    monkeypatch.setattr(daemon, "POLICY_WRAP", "fakepolicy.wrap")
    return seen


def test_a_policy_service_brings_its_wrapper(monkeypatch, config):
    seen = fake_policy_wrap(monkeypatch)
    runtime = node_runtime(config, FakeLaneManager(), FakeInteraction(), policy=FakePolicy())
    hooks = daemon.mcp_hooks(runtime)
    policy = sys.modules["fakepolicy.wrap"]
    assert hooks.wrap is policy.wrap_tool and hooks.bind_token is policy.bind_request_token
    specs = [ToolSpec(name="r", handler=_extra), ToolSpec(name="w", handler=_extra, effect=Effect.COMMIT)]
    assert [s.name for s in hooks.visible("grant-1", specs)] == ["r"]
    assert seen["visible"] == ("grant-1", runtime)
    assert hooks.accept_token("grant-1") and not hooks.accept_token("forged")


def test_policy_tokens_reach_mcp(monkeypatch, config):
    seen = fake_policy_wrap(monkeypatch)
    runtime = node_runtime(config, FakeLaneManager(config), FakeInteraction(), policy=FakePolicy())
    with TestClient(daemon.build_node_app(config, runtime), base_url="http://localhost:7400") as client:
        ok = client.post("/mcp", json=INIT, headers=MCP_HEADERS | {"Authorization": "Bearer grant-1"})
        assert ok.status_code == 200 and seen["bound"] == ("grant-1", None)
        forged = client.post("/mcp", json=INIT, headers=MCP_HEADERS | {"Authorization": "Bearer forged"})
        assert forged.status_code == 401


def test_a_policy_service_without_its_wrapper_refuses_to_start(monkeypatch, config):
    monkeypatch.setattr(daemon, "POLICY_WRAP", "agentos.policy_missing.wrap")
    runtime = node_runtime(config, FakeLaneManager(), FakeInteraction(), policy=FakePolicy())
    with pytest.raises(ModuleNotFoundError):
        daemon.build_node_app(config, runtime)
