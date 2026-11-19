"""Feature services in agentosd: built in order, started after the lanes, stopped before them,
and policy required (fail closed). The last test runs the real policy, vault and runner
services behind real HTTP with the node token."""

import asyncio
import sys
import types

import httpx2
import pytest
from fakes import FakeInteraction, FakeLaneManager, mcp_port_for_tests
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from agentos.core.config import NodeConfig
from agentos.core.models import LaneSpec
from agentos.mcp import providers as mcp_providers
from agentos.node import daemon
from agentos.node.daemon import build_services, node_runtime, serve

TOKEN = "node-token-for-services-test"


@pytest.fixture
def config(tmp_path):
    return NodeConfig(home=tmp_path, mcp_token=TOKEN, mcp_port=mcp_port_for_tests())


class Recorder:
    def __init__(self, name, events, *, fail_start=False, hang_stop=False):
        self.name, self.events, self.fail_start, self.hang_stop = name, events, fail_start, hang_stop

    async def start(self):
        self.events.append(f"start {self.name}")
        if self.fail_start:
            raise RuntimeError("boom")

    async def stop(self):
        self.events.append(f"stop {self.name}")
        if self.hang_stop:
            await asyncio.sleep(60)

    def close(self):
        self.events.append(f"close {self.name}")


def fake_module(monkeypatch, name, build):
    module = types.ModuleType(name)
    module.build = build
    monkeypatch.setitem(sys.modules, name, module)
    return name


class Manager(FakeLaneManager):
    def __init__(self, config, events):
        super().__init__(config)
        self.events = events

    async def start(self):
        self.events.append("start lanes")
        await super().start()

    async def shutdown(self):
        self.events.append("stop lanes")
        await super().shutdown()


async def test_services_start_after_lanes_and_stop_before_them_in_reverse(config, monkeypatch):
    events: list[str] = []
    names = [fake_module(monkeypatch, f"fake_feature_{n}", lambda rt, n=n: Recorder(n, events)) for n in "ab"]
    runtime = node_runtime(config, Manager(config, events), FakeInteraction())
    build_services(runtime, [*names, "agentos.not_a_feature.service"])  # a missing package is skipped
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(serve(config, runtime, stop=stop, providers=[]), 15)
    assert events == ["start lanes", "start a", "start b", "stop b", "close b", "stop a", "close a", "stop lanes"]


async def test_a_failing_optional_service_does_not_stop_the_node(config, monkeypatch, caplog):
    events: list[str] = []

    def broken(rt):
        raise ValueError("no db")

    names = [fake_module(monkeypatch, "fake_broken", broken),
             fake_module(monkeypatch, "fake_flaky", lambda rt: Recorder("flaky", events, fail_start=True)),
             fake_module(monkeypatch, "fake_slow", lambda rt: Recorder("slow", events, hang_stop=True))]
    monkeypatch.setattr(daemon, "SERVICE_STOP_TIMEOUT", 0.2)
    runtime = node_runtime(config, Manager(config, events), FakeInteraction())
    assert [n for n, _ in build_services(runtime, names)] == ["fake_flaky", "fake_slow"]
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(serve(config, runtime, stop=stop, providers=[]), 15)
    assert "stop lanes" in events and "close slow" in events
    assert "fake_broken failed to build" in caplog.text and "fake_slow did not stop" in caplog.text


def test_policy_that_fails_to_build_refuses_the_node(config, monkeypatch):
    def broken(rt):
        raise OSError("policy.db is locked")

    name = fake_module(monkeypatch, "fake_policy", broken)
    monkeypatch.setattr(daemon, "REQUIRED_SERVICES", frozenset({name}))
    runtime = node_runtime(config, FakeLaneManager(config), FakeInteraction())
    with pytest.raises(RuntimeError, match="refusing to start"):
        build_services(runtime, [name])


async def test_policy_that_fails_to_start_refuses_to_serve_but_lanes_close(config, monkeypatch):
    events: list[str] = []
    name = fake_module(monkeypatch, "fake_policy2", lambda rt: Recorder("policy", events, fail_start=True))
    monkeypatch.setattr(daemon, "REQUIRED_SERVICES", frozenset({name}))
    runtime = node_runtime(config, Manager(config, events), FakeInteraction())
    build_services(runtime, [name])
    with pytest.raises(RuntimeError, match="refusing to serve"):
        await asyncio.wait_for(serve(config, runtime, providers=[]), 15)
    assert events[-1] == "stop lanes"


async def test_real_features_are_served_through_the_policy_wrapper(config):
    manager = FakeLaneManager(config)
    await manager.create(LaneSpec(id="personal/reddit"))
    runtime = node_runtime(config, manager, FakeInteraction())
    built = [name for name, _ in build_services(runtime, ["agentos.policy.service", "agentos.vault.service",
                                                          "agentos.runner.service"])]
    assert built == ["agentos.policy.service", "agentos.vault.service", "agentos.runner.service"]
    assert runtime.has("policy") and runtime.has("vault") and runtime.has("runner")

    port: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    task = asyncio.create_task(serve(config, runtime, stop=stop, on_started=port.set_result))
    try:
        url = f"http://localhost:{await asyncio.wait_for(port, 15)}/mcp"
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http,
            Client(streamable_http_client(url, http_client=http), mode="legacy") as client,
        ):
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"browser_snapshot", "tools_more", "tools_call"} <= names and "browsers_list" not in names  # core
            more = await client.call_tool("tools_more", {})
            reachable = {name for row in more.structured_content["toolsets"] for name in row["tools"]}
            assert {"browsers_list", "vault_list", "grant_info", "flows_list"} <= reachable
            lanes = await client.call_tool("browsers_list", {})
            assert not lanes.is_error and "personal/reddit" in lanes.content[0].text
            info = await client.call_tool("grant_info", {})  # not listed by default, still callable
            assert not info.is_error, info.content[0].text
            # the owner's own agent keeps M1's behavior: snapshot, then click by ref
            snap = await client.call_tool("browser_snapshot", {"on": "personal/reddit"})
            assert not snap.is_error and "[ref=e1]" in snap.content[0].text
            act = await client.call_tool("browser_act", {"on": "personal/reddit", "ref": "e1", "action": "click"})
            assert not act.is_error, act.content[0].text
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer forged-token"}) as http:
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "0"}}}
            response = await http.post(url, json=init, headers={
                "Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
            assert response.status_code == 401
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)
    assert manager.shut_down


async def test_services_named_by_module_are_registered_and_shutdown_is_a_stop(config, monkeypatch):
    events: list[str] = []

    class Envs:
        async def start(self):
            events.append("start envs")

        async def shutdown(self):
            events.append("shutdown envs")

    name = fake_module(monkeypatch, "fake_envs", lambda rt: Envs())
    sys.modules[name].SERVICE_NAME = "envs"
    runtime = node_runtime(config, Manager(config, events), FakeInteraction())
    build_services(runtime, [name])
    assert isinstance(runtime.get("envs"), Envs)
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(serve(config, runtime, stop=stop, providers=[]), 15)
    assert events == ["start lanes", "start envs", "shutdown envs", "stop lanes"]


def test_lane_io_tools_are_served_by_default():
    names = {spec.name for provider in mcp_providers.tool_providers()
             for spec in provider(node_runtime(NodeConfig(), FakeLaneManager(), FakeInteraction()))}
    assert {"browser_file_put", "browser_upload", "browser_tabs", "browser_dialog_answer"} <= names


async def test_viewer_and_control_api_are_served_with_their_own_auth(tmp_path):
    """SEC-1: live views go only through signed links on the node port; the control API needs
    the node token. A browser_view link opens the viewer page; a forged one does not."""
    from edge_lanes import make_manager
    from starlette.testclient import TestClient

    from agentos.node.daemon import build_node_app

    manager, _, cfg = make_manager(tmp_path, mcp_token=TOKEN)
    await manager.create(LaneSpec(id="personal/reddit"))
    await manager.open("personal/reddit")
    runtime = node_runtime(cfg, manager, FakeInteraction())
    app = build_node_app(cfg, runtime, providers=[])
    from agentos.node.control_tools import take_over_link

    link = take_over_link(runtime, "personal/reddit")
    with TestClient(app, base_url="http://localhost") as http:
        assert http.get(link["path"]).status_code == 200
        forged = link["path"].rsplit("t=", 1)[0] + "t=forged"
        assert http.get(forged).status_code in (401, 403)
        assert http.post("/api/lanes/personal/reddit/control", json={"action": "take"}).status_code == 401
        took = http.post("/api/lanes/personal/reddit/control", json={"action": "take"},
                         headers={"Authorization": f"Bearer {TOKEN}"})
        assert took.status_code == 200 and took.json()["changed"] is True
        assert http.get(link["path"], headers={"Host": "evil.example"}).status_code == 421
    assert link["url"].startswith(f"http://localhost:{cfg.mcp_port}/view/personal/reddit?t=")
    await manager.shutdown()


def test_build_runtime_registers_the_typist_and_the_flow_driver(config, monkeypatch):
    monkeypatch.setattr(daemon, "_build_lane_manager", lambda cfg: FakeLaneManager(cfg))
    monkeypatch.setattr(daemon, "_build_interaction", lambda cfg: FakeInteraction())
    runtime = daemon.build_runtime(config, services=[])
    assert callable(runtime.get("typist")) and callable(runtime.get("flow_driver"))


async def test_with_the_panel_the_control_routes_live_behind_the_panel_auth(tmp_path):
    from edge_lanes import make_manager
    from starlette.testclient import TestClient

    from agentos.node.daemon import build_node_app

    manager, _, cfg = make_manager(tmp_path, mcp_token=TOKEN)
    await manager.create(LaneSpec(id="personal/reddit"))
    runtime = node_runtime(cfg, manager, FakeInteraction())
    app = build_node_app(cfg, runtime, providers=[], panel=True)
    with TestClient(app, base_url="http://localhost") as http:
        assert http.get("/api/status").status_code == 401
        assert http.post("/api/lanes/personal/reddit/control", json={"action": "take"}).status_code == 401
        auth = {"Authorization": f"Bearer {TOKEN}"}
        assert http.get("/api/status", headers=auth).status_code == 200
        took = http.post("/api/lanes/personal/reddit/control", json={"action": "take"}, headers=auth)
        assert took.status_code == 200 and took.json()["changed"] is True
        link = http.post("/api/lanes/personal/reddit/view-link", json={"mode": "view"}, headers=auth)
        assert link.status_code == 200 and link.json()["url"].startswith("/view/personal/reddit?t=")
    await manager.shutdown()


def test_build_runtime_routes_every_signal_to_the_lane_manager(tmp_path, monkeypatch):
    """Holds need every signal an interaction reads: the daemon wraps the interaction."""
    from edge_lanes import make_manager

    from agentos.node.lanes import SignalWatch

    manager, _, cfg = make_manager(tmp_path, mcp_token=TOKEN)
    monkeypatch.setattr(daemon, "_build_lane_manager", lambda c: manager)
    monkeypatch.setattr(daemon, "_build_interaction", lambda c: FakeInteraction())
    runtime = daemon.build_runtime(cfg, services=[])
    assert isinstance(runtime.get("interaction"), SignalWatch)


def test_policy_build_attaches_the_notifier_to_lane_holds(tmp_path):
    """The lane manager is registered before the services, so policy's notifier hears holds."""
    from edge_lanes import make_manager

    manager, _, cfg = make_manager(tmp_path, mcp_token=TOKEN)
    runtime = node_runtime(cfg, manager, FakeInteraction())
    build_services(runtime, ["agentos.policy.service"])
    notifier = runtime.get("notifier")
    assert notifier.on_lane_control in manager._listeners, "policy's notifier should listen to lane control"


def test_a_flow_upload_resolves_a_files_put_id_through_the_files_service(config, monkeypatch):
    """browser_file_put stores inbox/<id>/<name>; without a files service the driver could not find an id."""
    from agentos.node.files import INBOX, FilesConfig, LaneFiles
    from agentos.node.flow_driver import resolve_inbox_file

    monkeypatch.setattr(daemon, "_build_lane_manager", lambda c: FakeLaneManager(c))
    monkeypatch.setattr(daemon, "_build_interaction", lambda c: FakeInteraction())
    runtime = daemon.build_runtime(config, services=[])
    lane = "biz-a/instagram"
    config.lane_dir(lane).mkdir(parents=True)  # the lane exists, as after lane create
    store = LaneFiles(config.lane_dir(lane) / "files", FilesConfig.from_node(config))
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64
    stored = store.put_bytes("photo.png", png)
    inbox = config.lane_dir(lane) / "files" / INBOX
    path = resolve_inbox_file(inbox, stored.id, files=runtime.get("files"), lane_id=lane)
    assert path.name == "photo.png" and path.read_bytes().startswith(b"\x89PNG")
