"""`openajar controller ...`, `openajar node join` and `openajar up`."""

import asyncio
import threading

import pytest
from channel_support import agent, wait_for
from fakes import FakeInteraction, FakeLaneManager
from typer.testing import CliRunner

from openajar.cli import app
from openajar.client.remote import parse_reply
from openajar.controller.app import serve_controller
from openajar.controller.config import (
    ControllerConfig,
    controller_home,
    load_controller_config,
    save_controller_config,
)
from openajar.controller.up import node_name, run_up
from openajar.core.config import NodeConfig, load_config, save_config
from openajar.core.models import LaneSpec
from openajar.node.daemon import build_services, node_runtime
from openajar.policy.ceiling import ControllerCeiling

runner = CliRunner()


@pytest.fixture
def homes(tmp_path, monkeypatch):
    node = tmp_path / "node"
    monkeypatch.setenv("OPENAJAR_HOME", str(node))
    monkeypatch.delenv("OPENAJAR_CONTROLLER_HOME", raising=False)
    result = runner.invoke(app, ["node", "init", "--name", "box-1", "--mcp-port", "7680"])
    assert result.exit_code == 0, result.output
    return node, controller_home(node)


def test_join_token_login_and_agent_token(homes):
    _, ctl_home = homes
    out = runner.invoke(app, ["controller", "join-token", "--name", "box-2", "--ttl-minutes", "10"])
    assert out.exit_code == 0, out.output
    assert "openajar node join --self-hosted ws://localhost:7401/v1/node/connect ajt_" in out.output
    assert "—" not in out.output
    bad = runner.invoke(app, ["controller", "join-token", "--name", "Box Two"])
    assert bad.exit_code == 1 and "lowercase" in bad.output
    login = runner.invoke(app, ["controller", "login"])
    assert login.exit_code == 0 and "http://localhost:7401/login#code=" in login.output
    cfg = load_controller_config(ctl_home)
    shown = runner.invoke(app, ["controller", "agent-token", "--ssh-host", "mynode"])
    assert f"openajar connect mynode --remote-home {ctl_home}" in shown.output
    assert cfg.agent_token in shown.output
    rotated = runner.invoke(app, ["controller", "agent-token", "--new"])
    assert rotated.exit_code == 0 and load_controller_config(ctl_home).agent_token != cfg.agent_token
    assert "No nodes yet" in runner.invoke(app, ["controller", "nodes"]).output


def test_openajar_connect_reads_the_controller_like_a_node(homes):
    _, ctl_home = homes
    runner.invoke(app, ["controller", "init", "--port", "7681"])
    cfg = load_controller_config(ctl_home)
    stub = (ctl_home / "node.toml").read_text()
    assert (ctl_home / "node.toml").stat().st_mode & 0o777 == 0o600
    settings = parse_reply("mynode", str(ctl_home), stub)
    assert settings.mcp_port == 7681 and settings.token == cfg.agent_token
    assert settings.kind == "controller" and settings.controller  # connect keeps it: it is an agent token
    assert (ctl_home / "controller.toml").stat().st_mode & 0o777 == 0o600


def test_a_controller_refuses_a_public_bind_without_tls(homes):
    from openajar.controller.config import check_bind

    _, ctl_home = homes
    with pytest.raises(RuntimeError, match="TLS"):
        check_bind(ControllerConfig(home=ctl_home, host="0.0.0.0"))
    check_bind(ControllerConfig(home=ctl_home, host="0.0.0.0", behind_tls_proxy=True))
    with pytest.raises(ValueError, match="localhost"):
        ControllerConfig(home=ctl_home, public_url="ws://controller.example.com")


class ThreadedController:
    """A real controller on its own loop, so a CLI command can run asyncio.run() against it."""

    def __init__(self, home):
        self.config = ControllerConfig(home=home, port=0, agent_token="aga_cli_test_token_value", owner_token="o")
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        async def main():
            self.loop = asyncio.get_running_loop()
            self.stop = asyncio.Event()

            def started(port, controller):
                self.port, self.controller = port, controller
                self.ready.set()

            await serve_controller(self.config, stop=self.stop, on_started=started)

        asyncio.run(main())

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(15)
        return self

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(15)


def test_node_join_enrolls_and_pins_the_controller(homes, tmp_path):
    node_home, _ = homes
    with ThreadedController(tmp_path / "remote-controller") as ctl:
        _, token = ctl.controller.create_join_token(ctl.controller.org["id"])
        url = f"ws://localhost:{ctl.port}"
        joined = runner.invoke(app, ["node", "join", url, token])
        assert joined.exit_code == 0, joined.output
        cfg = load_config(node_home)
        assert cfg.enrolled and cfg.controller_url == f"{url}/v1/node/connect"
        assert cfg.controller_pubkey == ctl.controller.keys.controller_pubkey
        assert cfg.grant_pubkey == ctl.controller.keys.grant_pubkey(ctl.controller.org["id"])
        assert ctl.controller.store.node(cfg.node_id)["name"] == "box-1"
        assert "Controller ceiling: effects up to commit, specials none, streams view." in joined.output
        assert (cfg.controller_max_effect, cfg.controller_specials, cfg.controller_streams) == ("commit", [], ["view"])
        _, own = ctl.controller.create_join_token(ctl.controller.org["id"], name="box-own")
        mine = runner.invoke(app, ["node", "join", "--self-hosted", "--name", "box-own", url, own])
        assert mine.exit_code == 0, mine.output
        assert ControllerCeiling.from_config(load_config(node_home)) == ControllerCeiling.full()
        assert "streams view, control, terminal" in mine.output and "—" not in mine.output
        again = runner.invoke(app, ["node", "join", url, token])
        assert again.exit_code == 1 and "already_used" in again.output and "—" not in again.output
    insecure = runner.invoke(app, ["node", "join", "ws://controller.example.com", "ajt_x"])
    assert insecure.exit_code == 2 and "wss://" in insecure.output
    unreachable = runner.invoke(app, ["node", "join", "ws://localhost:1", "ajt_x"])
    assert unreachable.exit_code == 1 and "could not reach" in unreachable.output


def test_node_names_for_up():
    assert node_name("Box One!") == "box-one" and node_name("___") == "node-1" and len(node_name("a" * 50)) == 31


async def test_openajar_up_runs_a_controller_and_this_node_joined(tmp_path):
    node_cfg = NodeConfig(home=tmp_path / "node", name="My Box", mcp_token="node-owner-token-up", mcp_port=0)
    (tmp_path / "node").mkdir()
    save_config(node_cfg)
    ctl_cfg = ControllerConfig(home=tmp_path / "node" / "controller", port=0, agent_token="aga_up_agent_token_val",
                               owner_token="owner-up")
    save_controller_config(ctl_cfg)
    built = {}

    def build(config):
        manager = FakeLaneManager(config)
        asyncio.get_event_loop().create_task(manager.create(LaneSpec(id="personal/reddit")))
        runtime = node_runtime(config, manager, FakeInteraction())
        build_services(runtime, ["openajar.policy.service", "openajar.node.channel.service"])
        built["runtime"] = runtime
        return runtime

    ready = {}
    stop = asyncio.Event()
    task = asyncio.create_task(run_up(node_cfg, ctl_cfg, stop=stop, build_runtime=build,
                                      on_ready=lambda cfg, name: ready.update(cfg=cfg, name=name)))
    await wait_for(lambda: "runtime" in built and built["runtime"].get("channel") is not None
                   and built["runtime"].channel.connected, timeout=20)
    cfg = ready["cfg"]
    assert ready["name"] == "My Box" and cfg.mcp_url.endswith("/mcp")
    joined = load_config(tmp_path / "node")
    assert joined.enrolled and joined.controller_url.startswith("ws://localhost:")

    class Ctl:
        url = cfg.local_url

    async with agent(Ctl, "aga_up_agent_token_val") as client:
        await wait_for(lambda: True)
        listed = None
        for _ in range(100):
            listed = await client.call_tool("nodes_list", {})
            if listed.structured_content["nodes"] and listed.structured_content["nodes"][0]["online"]:
                break
            await asyncio.sleep(0.1)
        assert listed.structured_content["nodes"][0]["name"] == "my-box"
    stop.set()
    await asyncio.wait_for(task, 20)
    node_id = joined.node_id
    # a second `up` keeps the node's identity
    built.clear()
    stop2 = asyncio.Event()
    task2 = asyncio.create_task(run_up(load_config(tmp_path / "node"), ctl_cfg, stop=stop2, build_runtime=build))
    await wait_for(lambda: "runtime" in built and built["runtime"].get("channel") is not None
                   and built["runtime"].channel.connected, timeout=20)
    assert load_config(tmp_path / "node").node_id == node_id
    stop2.set()
    await asyncio.wait_for(task2, 20)


def test_openajar_up_logs_through_the_same_scrubbers_as_agentosd(homes, monkeypatch):
    """`openajar up` runs agentosd in its own process: its log lines need agentosd's scrub chain,
    not a plain handler that writes the node token and a proxy password as they come."""
    import json
    import logging

    from openajar.controller import up as up_mod
    from openajar.core import logs
    from openajar.core.config import save_proxies
    from openajar.core.models import ProxyConfig

    node_home, _ = homes
    node_token = load_config(node_home).mcp_token
    save_proxies(load_config(node_home), {"lt1": ProxyConfig(label="lt1", host="203.0.113.7", port=1,
                                                             username="proxyuser", password="hunter2-proxy")})
    seen = {}

    async def fake_run_up(node_cfg, controller_cfg, *, on_ready=None, **kw):
        seen["agent_token"] = controller_cfg.agent_token
        logging.getLogger("openajar.up").warning("node %s agent %s proxy %s", node_token,
                                                controller_cfg.agent_token, "hunter2-proxy")

    monkeypatch.setattr(up_mod, "run_up", fake_run_up)
    try:
        result = runner.invoke(app, ["up"])
        assert result.exit_code == 0, result.output
        lines = [json.loads(line) for line in (logs.log_dir(node_home) / logs.LOG_FILE).read_text().splitlines()]
    finally:
        logs.clear_scrubbers()
        logging.getLogger().handlers.clear()
    [line] = [line for line in lines if line.get("logger") == "openajar.up"]
    text = json.dumps(line)
    for secret in (node_token, seen["agent_token"], "hunter2-proxy"):
        assert secret and secret not in text and secret not in result.output
    assert "[redacted]" in text


def test_the_controllers_uvicorn_lines_go_through_the_scrubbers(tmp_path):
    """uvicorn's default log config gives its loggers a handler of their own, which skips the root
    handler and so the scrubbers. The controller passes log_config=None, as agentosd does."""
    import io
    import logging

    from openajar.controller.app import _server
    from openajar.core import logs

    async def app(scope, receive, send):  # never called: only the server's logging is under test
        pass

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):  # as a fresh process has them
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate, logger.level = True, logging.NOTSET
    stream = io.StringIO()
    logs.setup(None, stream=stream)
    logs.add_scrubber(lambda text: text.replace("ctl-secret-77", "[redacted]"))
    try:
        _server(app, ControllerConfig(home=tmp_path, port=0))
        logging.getLogger("uvicorn.error").info("refused ctl-secret-77")
    finally:
        logs.clear_scrubbers()
        logging.getLogger().handlers.clear()
    assert "refused [redacted]" in stream.getvalue()
    assert "ctl-secret-77" not in stream.getvalue()


def test_openajar_up_an_unexpected_error_is_logged_scrubbed_and_exits_non_zero(homes, monkeypatch):
    """Not a raw traceback on the terminal: the error can quote the node token, so it goes through
    the scrubbed log like 's does."""
    import json
    import logging

    from openajar.controller import up as up_mod
    from openajar.core import logs

    node_home, _ = homes
    node_token = load_config(node_home).mcp_token

    async def fake_run_up(node_cfg, controller_cfg, *, on_ready=None, **kw):
        raise KeyError(f"node {node_token} refused")

    monkeypatch.setattr(up_mod, "run_up", fake_run_up)
    try:
        result = runner.invoke(app, ["up"])
        text = (logs.log_dir(node_home) / logs.LOG_FILE).read_text()
    finally:
        logs.clear_scrubbers()
        logging.getLogger().handlers.clear()
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit), repr(result.exception)
    assert node_token not in text and node_token not in result.output
    [line] = [json.loads(raw) for raw in text.splitlines() if '"openajar.up"' in raw]
    assert line["msg"] == "openajar up stopped on an unexpected error" and "[redacted]" in line["exc"]


@pytest.fixture
def root_logging():
    import logging

    from openajar.core import logs

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)
    logs.clear_scrubbers()


def test_openajar_up_logs_like_node_serve(homes, monkeypatch, root_logging):
    """`openajar up` used logging.basicConfig: plain lines, no ids, nothing scrubbed. Now it gets
    node serve's setup (the log file, the context filter, the scrubbers), the controller's tokens
    stay masked after the node installs its scrubbers again, and a crash goes through the log."""
    import json
    import logging

    from openajar.controller import up
    from openajar.core import logs
    from openajar.core.logs import ContextFilter
    from openajar.node.daemon import install_log_scrubbers

    node_home, ctl_home = homes
    runner.invoke(app, ["controller", "init", "--port", "7682"])
    ctl = load_controller_config(ctl_home)
    node_token = load_config(node_home).mcp_token
    seen = {}

    async def fake_run_up(node_cfg, controller_cfg, *, on_ready=None, stop=None):
        install_log_scrubbers(node_cfg)  # what daemon.serve does once the node starts
        seen["filters"] = [type(f) for h in logging.getLogger().handlers for f in h.filters]
        with logs.log_context(lane="biz-a/x"):
            logging.getLogger("openajar.controller").warning(
                "joined with %s, agents use %s, owner %s", node_token, ctl.agent_token, ctl.owner_token)
        raise ValueError(f"boom {ctl.owner_token}")

    monkeypatch.setattr(up, "run_up", fake_run_up)
    result = runner.invoke(app, ["up"])
    assert result.exit_code == 1
    assert ContextFilter in seen["filters"]
    lines = [json.loads(raw) for raw in (node_home / "logs" / logs.LOG_FILE).read_text().splitlines()]
    joined = next(line for line in lines if "joined with" in line["msg"])
    assert joined["lane"] == "biz-a/x" and joined["msg"].count("[redacted]") == 3
    crash = next(line for line in lines if "openajar up stopped" in line["msg"])
    assert "ValueError" in crash["exc"] and ctl.owner_token not in crash["exc"]
    text = (node_home / "logs" / logs.LOG_FILE).read_text() + result.output
    for secret in (node_token, ctl.agent_token, ctl.owner_token):
        assert secret not in text


def test_openajar_up_on_a_new_box_sets_it_up_and_prints_one_link(tmp_path, monkeypatch, root_logging):
    """No node.toml yet: `up` writes it (as node init) and controller.toml, then prints the panel,
    one sign-in link and one connect line. The tunnel and `claude mcp add` lines are
    `openajar connect --by-hand` now. A rerun keeps both files and their tokens."""
    from openajar.controller import up as up_mod
    from openajar.controller.config import config_file

    node_home = tmp_path / "node"
    monkeypatch.setenv("OPENAJAR_HOME", str(node_home))
    monkeypatch.delenv("OPENAJAR_CONTROLLER_HOME", raising=False)
    runs = []

    async def fake_run_up(node_cfg, controller_cfg, *, on_ready=None, **kw):
        runs.append(node_cfg.mcp_token)
        on_ready(controller_cfg, "box-1")

    monkeypatch.setattr(up_mod, "run_up", fake_run_up)
    first = runner.invoke(app, ["up", "--ssh-host", "mybox"])
    assert first.exit_code == 0, first.output
    ctl_home = controller_home(node_home)
    assert (node_home / "node.toml").exists() and config_file(ctl_home).exists()
    assert f"First run: wrote {node_home / 'node.toml'} and {config_file(ctl_home)}." in first.output
    lines = first.output.splitlines()
    labels = [line.split(":")[0].strip() for line in lines if line.startswith("  ") and not line.startswith("    ")]
    assert labels == ["Panel", "Sign in", "Connect"]
    assert sum("http://" in line for line in lines) == 2  # the panel, and the link into it
    assert "http://localhost:7401/login#code=" in first.output
    assert f"openajar connect mybox --remote-home {ctl_home} --name controller" in first.output
    for gone in ("claude mcp add", "ssh -N", "Bearer", load_controller_config(ctl_home).agent_token):
        assert gone not in first.output
    assert "—" not in first.output
    agent_token = load_controller_config(ctl_home).agent_token

    again = runner.invoke(app, ["up"])
    assert again.exit_code == 0, again.output
    assert "First run" not in again.output and "Sign in:" in again.output
    assert runs[0] == runs[1] and load_controller_config(ctl_home).agent_token == agent_token


def test_openajar_up_with_a_broken_node_toml_says_so_in_one_line(tmp_path, monkeypatch):
    node_home = tmp_path / "node"
    node_home.mkdir()
    (node_home / "node.toml").write_text("mcp_port = \n")
    monkeypatch.setenv("OPENAJAR_HOME", str(node_home))
    monkeypatch.delenv("OPENAJAR_CONTROLLER_HOME", raising=False)
    result = runner.invoke(app, ["up"])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert "is not valid TOML" in result.output and "Traceback" not in result.output
