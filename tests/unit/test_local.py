"""agentos local: the home it makes, the composition it serves (over real HTTP, on a free port), the
grant and the clients it registers, and the service it installs. Nothing here touches this
computer's real setup: HOME is a temporary folder, launchd and systemd are fakes (no command ever
runs), port probes are fakes, and the one real server binds a port the OS picks."""

from __future__ import annotations

import asyncio
import json
import logging
import plistlib
import socket
import stat
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest
from mcp import Client as McpClient
from mcp.client.streamable_http import streamable_http_client
from typer.testing import CliRunner

from agentos.client.choices import Tools
from agentos.client.ops import Client
from agentos.client.state import Paths
from agentos.client.tokens import FileStore
from agentos.local import home as local_home
from agentos.local import service, wake
from agentos.local.home import LocalError
from agentos.local.node import LOCAL_SERVICES, build_runtime, run
from agentos.local.paths import AVOIDED_PORTS, DEFAULT_PORT, NAME, default_home, log_path
from agentos.local.register import register_clients
from agentos.policy.service import PolicyService

NEVER = {7400, 7450, 5392}  # in use on the computers this runs on: never probed, bound or killed


class Ports:
    """A fake port probe: `taken` are busy. Records every port it was asked about."""

    def __init__(self, *taken: int):
        self.taken = set(taken)
        self.asked: list[int] = []

    def __call__(self, port: int) -> bool:
        self.asked.append(port)
        assert port not in NEVER
        return port in self.taken


@pytest.fixture(autouse=True)
def _logging_put_back():
    """`agentos local serve` sets up logging for its whole process, with a handler on the stream it was given.
    Run in a CliRunner, that stream closes with the test: put the root logger back, so a later test's output
    (test_ops_cli, test_wsexport_cli: JSON) gets no log lines and no "I/O operation on closed file"."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in [h for h in root.handlers if h not in handlers]:
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    """A temporary HOME: the client's files, Claude Code's, Codex's and the LaunchAgents go there."""
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # set, not deleted, so it is put back after the test: the local node sets it to its own home
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path / "not-this-one"))
    return home


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ------------------------------------------------------------------ the home and its port


def test_the_default_home_and_port(user_home):
    assert default_home() == user_home / ".agentos"
    assert DEFAULT_PORT == 7410 and {7400, 7450} <= AVOIDED_PORTS


def test_the_first_start_takes_7410_and_remembers_it(tmp_path):
    home, ports = tmp_path / "agentos", Ports()
    cfg = local_home.ensure(home, local_home.choose_port(home, None, busy=ports))
    assert cfg.mcp_port == 7410 and cfg.mcp_host == "127.0.0.1" and cfg.name == NAME and len(cfg.mcp_token) > 30
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "secrets" / "vault.key").stat().st_mode) == 0o600
    again = local_home.ensure(home, local_home.choose_port(home, None, busy=ports))
    assert again.mcp_port == 7410 and again.mcp_token == cfg.mcp_token  # same address, same token


def test_a_taken_port_moves_to_the_next_free_one_and_that_one_is_remembered(tmp_path):
    home = tmp_path / "agentos"
    assert local_home.ensure(home, local_home.choose_port(home, None, busy=Ports(7410, 7411))).mcp_port == 7412
    assert local_home.choose_port(home, None, busy=Ports(7410)) == 7412  # remembered, though 7410 is free again
    assert local_home.choose_port(home, None, busy=Ports(7412)) == 7413  # the remembered one is taken now


def test_the_ports_of_connect_and_dev_up_are_never_taken_or_probed(tmp_path):
    ports = Ports(*range(7395, 7400))
    assert local_home.free_port(7395, busy=ports) == 7401
    assert 7400 not in ports.asked
    with pytest.raises(LocalError, match="kept for agentos connect"):
        local_home.choose_port(tmp_path, 7400, busy=ports)
    with pytest.raises(LocalError, match="kept for agentos dev up"):
        local_home.choose_port(tmp_path, 7450, busy=ports)
    with pytest.raises(LocalError, match="already in use"):
        local_home.choose_port(tmp_path, 7420, busy=Ports(7420))


# ------------------------------------------------------------------ the composition


def test_the_runtime_has_the_environment_parts_and_the_browser_routed_to_the_vm(tmp_path):
    from agentos.environments.vmbrowser import RoutedDisplays, RoutedEngine
    from agentos.local.lanes import LocalLanes, NoLocalDisplays, NoLocalEngine

    cfg = local_home.ensure(tmp_path / "agentos", 7419)
    runtime = build_runtime(cfg)
    built = [name for name, _ in runtime.get("feature_services")]
    assert built == [*LOCAL_SERVICES, "agentos.local.wake"]
    for name in ("policy", "traces", "freeze", "vault", "git", "envs", "vms", "environments", "interaction"):
        assert runtime.get(name) is not None, name
    for name in ("runner", "brains", "profiles", "keepalive", "studio_browser", "workspaces"):
        assert runtime.get(name) is None, name
    lanes = runtime.lanes
    assert isinstance(lanes, LocalLanes) and lanes.list() == [] and callable(lanes.take_control)
    # the routers are in front from the start, and behind them nothing that starts a browser here
    assert isinstance(lanes.engine, RoutedEngine) and isinstance(lanes.engine.inner, NoLocalEngine)
    assert isinstance(lanes.displays, RoutedDisplays) and isinstance(lanes.displays.inner, NoLocalDisplays)
    assert lanes.engine.vm is runtime.environments.vm_browsers()  # the one the environments use
    assert runtime.environments.settings.browser == "vm"  # the default here
    assert runtime.vms.follow_ip is True  # a laptop: SSH from wherever it is now
    assert runtime.vms.home == runtime.environments.home == cfg.home


def test_browser_node_in_environments_toml_is_refused_with_what_to_do(tmp_path):
    cfg = local_home.ensure(tmp_path / "agentos", 7419)
    (cfg.home / "environments.toml").write_text('browser = "node"\n')
    with pytest.raises(LocalError, match="cannot run an environment's browser itself") as err:
        build_runtime(cfg)
    assert 'browser = "vm"' in err.value.hint and "—" not in err.value.message + err.value.hint
    (cfg.home / "environments.toml").write_text('browser = "vm"\n')
    assert build_runtime(cfg).environments.settings.browser == "vm"


def test_environments_setup_says_the_browser_runs_on_the_vm_on_a_laptop(tmp_path):
    from agentos.environments.cli import _local_browser

    cfg = local_home.ensure(tmp_path / "agentos", 7419)
    assert _local_browser(cfg.home, "node") == (None, "vm")  # nothing in environments.toml: the VM
    (cfg.home / "environments.toml").write_text('browser = "node"\n')
    refused, where = _local_browser(cfg.home, "node")
    assert refused[0] == "missing" and 'browser = "vm"' in refused[2] and where == "vm"
    server = tmp_path / "server"
    server.mkdir()
    assert _local_browser(server, "node") == (None, "node")  # a server node's home: as it says


def test_serve_says_why_it_did_not_start(tmp_path, user_home):
    from agentos.cli import app

    root = tmp_path / "agentos"
    local_home.ensure(root, free_port())
    (root / "environments.toml").write_text('browser = "node"\n')
    out = CliRunner().invoke(app, ["local", "serve", "--home", str(root)])
    assert out.exit_code == 1 and "cannot run an environment's browser itself" in out.output


# The base install (scripts/install.sh, the `local` extra) on a Mac: no Camoufox, no browser binary, no Xvfb.
# Both packages are made unimportable, PATH has no Xvfb, and the platform is darwin. On darwin, urllib's
# proxy lookup imports _scproxy, which only a real macOS Python has: off a Mac (CI's Linux) a stub answers
# "no proxies", as a Mac with none set would.
MAC_WITHOUT_A_BROWSER = """\
import sys, tempfile, os, types, importlib.util
sys.platform = "darwin"
if importlib.util.find_spec("_scproxy") is None:
    scproxy = types.ModuleType("_scproxy")
    scproxy._get_proxy_settings = lambda: {}
    scproxy._get_proxies = lambda: {}
    sys.modules["_scproxy"] = scproxy
sys.modules["camoufox"] = None  # an import of it fails, as on an install without the node extra
os.environ["PATH"] = tempfile.mkdtemp()  # no Xvfb, no x11vnc, no browser on PATH
import asyncio, shutil, subprocess
from pathlib import Path
ran = []
real_popen = subprocess.Popen.__init__
def popen(self, args, *a, **kw):
    ran.append(args)
    return real_popen(self, args, *a, **kw)
subprocess.Popen.__init__ = popen
from agentos.local import home
from agentos.local.node import run
cfg = home.ensure(Path(tempfile.mkdtemp()) / "a", int(sys.argv[1]))
async def main():
    stop, started = asyncio.Event(), asyncio.get_running_loop().create_future()
    task = asyncio.create_task(run(cfg, stop=stop, on_started=started.set_result))
    await asyncio.wait_for(started, 60)
    stop.set()
    await asyncio.wait_for(task, 30)
asyncio.run(main())
print(sorted({m.split(".")[0] for m in sys.modules if sys.modules[m] is not None} & {"camoufox", "playwright"}))
print(shutil.which("Xvfb"), [a for a in ran if any(w in str(a) for w in ("Xvfb", "x11vnc", "camoufox", "firefox"))])
"""


def test_it_starts_on_a_mac_with_no_browser_stack_and_imports_none(tmp_path):
    env = {k: v for k, v in __import__("os").environ.items() if k not in ("AGENTOS_HOME",)}
    env["HOME"] = str(tmp_path)
    out = subprocess.run([sys.executable, "-c", MAC_WITHOUT_A_BROWSER, str(free_port())], capture_output=True,
                         text=True, timeout=180, check=False, env=env)
    assert out.returncode == 0, out.stderr[-3000:]
    assert out.stdout.split("\n")[:2] == ["[]", "None []"], out.stdout + out.stderr[-2000:]


async def test_it_serves_mcp_and_the_web_app_and_the_registered_grant_works(tmp_path, user_home):
    home = tmp_path / "agentos"
    cfg = local_home.ensure(home, free_port())
    stop, started = asyncio.Event(), asyncio.get_running_loop().create_future()
    task = asyncio.create_task(run(cfg, stop=stop, on_started=started.set_result))
    try:
        port = await asyncio.wait_for(started, 30)
        client = Client(paths=Paths(user_home), platform="linux", tools=Tools(), out=lambda _: None)
        lines = register_clients(home, port, client=client)
        assert any(line.startswith("Agent grant gr_") and "issued" in line for line in lines), lines
        token = FileStore(Paths(user_home)).get(NAME)
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            McpClient(streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http), mode="legacy") as mcp,
        ):
            tools = {tool.name for tool in (await mcp.list_tools()).tools}
            assert {"environment_open", "environment", "environment_close", "lane_exec", "lane_file_read"} <= tools
            # the environment's browser (on its VM), and nothing built on account lanes
            assert {"browser_navigate", "browser_snapshot", "browser_act", "browser_wait", "browser_screenshot",
                    "lanes_list", "lane_view"} <= tools
            assert not tools & {"flow_run", "login", "lane_create", "profile_use"}
            who = await mcp.call_tool("environment", {})
            assert not who.is_error and json.loads(who.content[0].text)["environment"] is None
            up = await mcp.call_tool("tools_call", {"name": "environment_upload", "arguments": {}})
            assert "no tool named" not in up.content[0].text, up.content[0].text  # a laptop's checkout goes up too
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{port}") as web:
            assert (await web.get("/")).status_code == 200
            assert (await web.get("/api/status")).status_code == 401
            assert (await web.post("/mcp", json={})).status_code == 401  # no token, no MCP
            # the live view of an environment's browser: the node's viewer, behind its signed link
            assert (await web.get("/view/env/e1234567890-browser")).status_code in (401, 403)
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)
    assert wake.read_last(home)["reason"] == "started"  # the catch-up ran at the start


# ------------------------------------------------------------------ the grant and the clients


def fake_claude(tmp_path: Path) -> tuple[str, Path]:
    calls = tmp_path / "claude-calls.txt"
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f"#!/bin/sh\necho \"$@\" >> {calls}\n")
    exe.chmod(0o755)
    return str(exe), calls


def test_register_issues_the_connect_grant_stores_its_token_and_registers_agentos_local(tmp_path, user_home):
    home = tmp_path / "agentos"
    local_home.ensure(home, 7410)
    exe, calls = fake_claude(tmp_path)
    (user_home / ".codex").mkdir()
    client = Client(paths=Paths(user_home), platform="linux", tools=Tools(claude=exe), out=lambda _: None)
    lines = register_clients(home, 7410, client=client)
    text = "\n".join(lines)
    assert "Claude Code: registered agentos-local (user scope)" in text
    assert "Codex: registered agentos-local" in text and "Skills: installed" in text and "—" not in text
    token_file = Paths(user_home).tokens_dir / NAME
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    helper = Paths(user_home).headers_helper
    assert helper.exists() and str(Paths(user_home).tokens_dir) in helper.read_text()
    added = calls.read_text()
    assert "add-json --scope user agentos-local" in added and "http://127.0.0.1:7410/mcp" in added
    assert token_file.read_text().strip() not in added  # the token never goes to Claude Code
    codex = (user_home / ".codex" / "config.toml").read_text()
    assert "agentos-local" in codex and token_file.read_text().strip() not in codex

    policy = PolicyService(home, node_config=local_home.read(home))
    try:
        _, grant = policy.resolve(token_file.read_text().strip())
        record = policy.grant_record(grant.id)
        assert sorted(record["scope"]["specials"]) == ["exec", "memory", "vm"]
        assert record["scope"]["ceiling"] == "commit"
        conn = client.records.get(NAME)
        assert conn.agent_grant_id == grant.id and conn.url == "http://127.0.0.1:7410/mcp"
        assert conn.claude_name == "agentos-local" and conn.service == "none"
    finally:
        policy.close()


def test_register_again_keeps_a_working_grant_and_replaces_a_revoked_one(tmp_path, user_home):
    home = tmp_path / "agentos"
    local_home.ensure(home, 7410)
    client = Client(paths=Paths(user_home), platform="linux", tools=Tools(), out=lambda _: None)
    register_clients(home, 7410, client=client, codex=False)
    first = client.records.get(NAME).agent_grant_id
    lines = register_clients(home, 7411, client=client, codex=False)  # the port moved: the same grant
    assert lines[0] == f"Agent grant {first} kept"
    assert client.records.get(NAME).url == "http://127.0.0.1:7411/mcp"
    policy = PolicyService(home, node_config=local_home.read(home))
    policy.revoke(first, by="owner")
    policy.close()
    lines = register_clients(home, 7411, client=client, codex=False)
    second = client.records.get(NAME).agent_grant_id
    assert second != first and "issued" in lines[0]


def test_register_before_the_home_exists_says_to_start_first(tmp_path, user_home):
    client = Client(paths=Paths(user_home), platform="linux", tools=Tools(), out=lambda _: None)
    with pytest.raises(LocalError, match="no local node"):
        register_clients(tmp_path / "none", 7410, client=client)


# ------------------------------------------------------------------ the service


class FakeLaunchctl:
    """launchctl as a fake Run: bootstrap loads the label, bootout unloads it, print reports it."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.loaded = False

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        verb = cmd[1]
        if verb == "bootstrap":
            self.loaded = True
        elif verb == "bootout":
            ok, self.loaded = self.loaded, False
            return subprocess.CompletedProcess(cmd, 0 if ok else 3, "", "")
        elif verb == "print":
            if not self.loaded:
                return subprocess.CompletedProcess(cmd, 113, "", "Could not find service")
            return subprocess.CompletedProcess(cmd, 0, "\tstate = running\n\tpid = 4242\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def launchd_deps(user_home: Path, *, taken: tuple[int, ...] = ()) -> tuple[service.Deps, FakeLaunchctl]:
    fake = FakeLaunchctl()
    mgr = service.Launchd(Paths(user_home), fake, uid=501)
    return service.Deps(manager=mgr, busy=Ports(*taken), answers=lambda port: fake.loaded, sleep=lambda s: None,
                        wait_s=1), fake


def test_start_installs_and_loads_a_launch_agent_that_runs_serve(tmp_path, user_home):
    home = tmp_path / "agentos"
    deps, fake = launchd_deps(user_home)
    state = service.start(home, deps=deps)
    assert state.running and state.pid == 4242 and state.port == 7410
    assert state.url == "http://127.0.0.1:7410/mcp" and state.log_path == log_path(home)
    assert state.note.startswith("Running on port 7410.")
    plist_path = user_home / "Library" / "LaunchAgents" / "dev.agentos.local.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["Label"] == "dev.agentos.local" and plist["KeepAlive"] and plist["RunAtLoad"]
    assert plist["ProgramArguments"][-4:] == ["local", "serve", "--home", str(home)]
    assert plist["ProgramArguments"][0] == sys.executable
    assert plist["EnvironmentVariables"]["AGENTOS_HOME"] == str(home)
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(log_path(home))
    assert ["launchctl", "bootstrap", "gui/501", str(plist_path)] in fake.calls
    assert local_home.read(home).mcp_port == 7410


def test_start_when_running_leaves_it_and_stop_keeps_it_stopped_past_a_login(tmp_path, user_home):
    home = tmp_path / "agentos"
    deps, fake = launchd_deps(user_home)
    service.start(home, deps=deps)
    calls = len(fake.calls)
    again = service.start(home, deps=deps)
    assert again.running and again.note.startswith("Already running on port 7410.")
    assert not any(c[1] in ("bootstrap", "bootout") for c in fake.calls[calls:])
    stopped = service.stop(home, deps=deps)
    assert not stopped.running and stopped.note.startswith("Stopped. It stays stopped")
    assert not (user_home / "Library" / "LaunchAgents" / "dev.agentos.local.plist").exists()
    assert service.status(home, deps=deps).note.startswith("Not running.")


def test_start_on_a_taken_port_picks_the_next_and_status_says_what_the_catch_up_did(tmp_path, user_home):
    home = tmp_path / "agentos"
    deps, _ = launchd_deps(user_home, taken=(7410,))
    assert service.start(home, deps=deps).port == 7411
    wake_record = {"at": "2026-11-15T10:00:00+00:00", "reason": "woke", "slept_s": 5400,
                   "closed": [{"id": "eabc", "title": "Shop page", "reason": "time limit"}], "vms_deleted": [],
                   "errors": []}
    wake.catch_up_path(home).write_text(json.dumps(wake_record))
    note = service.status(home, deps=deps).note
    assert "Running on port 7411." in note and "after 1 h 30 min asleep" in note
    assert "closed 1 environment at once: eabc (Shop page: time limit)." in note and "—" not in note


def test_status_before_anything_was_set_up(tmp_path, user_home):
    deps, fake = launchd_deps(user_home)
    state = service.status(tmp_path / "none", deps=deps)
    assert not state.running and state.port is None and "agentos local start" in state.note
    assert fake.calls == []


def test_a_service_that_does_not_answer_says_so(tmp_path, user_home):
    home = tmp_path / "agentos"
    deps, _ = launchd_deps(user_home)
    deps.answers = lambda port: False
    state = service.start(home, deps=deps)
    assert not state.running and "did not answer on port 7410" in state.note


def test_the_systemd_unit_runs_serve_with_the_home_and_logs_to_its_file(tmp_path, user_home):
    calls: list[list[str]] = []

    def run(cmd):
        calls.append(cmd)
        out = "active\n" if "is-active" in cmd else "777\n" if "show" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    home = tmp_path / "agentos"
    mgr = service.Systemd(Paths(user_home), run)
    deps = service.Deps(manager=mgr, busy=Ports(), answers=lambda port: True, sleep=lambda s: None, wait_s=1)
    state = service.start(home, deps=deps)
    assert state.running and state.pid == 777
    unit = (user_home / ".config" / "systemd" / "user" / "agentos-local.service").read_text()
    assert f"local serve --home {home}" in unit and f"Environment=AGENTOS_HOME={home}" in unit
    assert f"StandardOutput=append:{log_path(home)}" in unit and "Restart=always" in unit
    assert ["systemctl", "--user", "enable", "agentos-local.service"] in calls
    service.stop(home, deps=deps)
    assert calls[-3][:4] == ["systemctl", "--user", "disable", "--now"]


def test_the_manager_is_launchd_on_a_mac_and_systemd_on_linux(user_home):
    assert isinstance(service.manager(platform="macos", user_home=user_home), service.Launchd)
    assert isinstance(service.manager(platform="linux", user_home=user_home), service.Systemd)


def test_the_cli_lists_its_commands():
    from agentos.cli import app

    out = CliRunner().invoke(app, ["local", "--help"])
    assert out.exit_code == 0
    for name in ("serve", "start", "stop", "status", "logs"):
        assert name in out.output


def test_logs_before_a_start_say_there_is_none_yet(tmp_path):
    from agentos.cli import app

    out = CliRunner().invoke(app, ["local", "logs", "--home", str(tmp_path / "none")])
    assert out.exit_code == 0 and "No log yet" in out.output


def test_the_other_commands_find_a_laptops_home_when_there_is_no_server_node(tmp_path, monkeypatch):
    """`agentos environments list` and the rest use core.config.home_from_env: with no AGENTOS_HOME and no
    server node in /var/lib/agentos, a laptop's ~/.agentos (agentos local) is the home; a server wins."""
    from agentos.core import config
    from agentos.local import paths

    monkeypatch.delenv("AGENTOS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    server = tmp_path / "var-lib-agentos"
    monkeypatch.setattr(config, "DEFAULT_HOME", server)
    assert config.local_home() == paths.default_home()
    assert config.home_from_env() == server                  # nothing set up anywhere: the server default
    (paths.default_home()).mkdir(parents=True)
    (paths.default_home() / "node.toml").write_text('name = "local"\n')
    assert config.home_from_env() == paths.default_home()    # a laptop running agentos local
    server.mkdir()
    (server / "node.toml").write_text('name = "node-1"\n')
    assert config.home_from_env() == server                  # a server node wins
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path / "given"))
    assert config.home_from_env() == tmp_path / "given"      # AGENTOS_HOME always wins


def test_a_start_right_after_a_stop_waits_for_launchd_to_finish_the_stop(user_home):
    """launchd finishes a bootout in the background: a bootstrap right after `agentos local stop` failed with
    "5: Input/output error" on a real Mac (2026-11-15). The load is tried again, waiting longer each time."""
    calls, waits = [], []

    def run(cmd):
        calls.append(cmd[1])
        failing = sum(1 for c in calls if c == "bootstrap") <= 3  # launchd still busy for three tries
        code = 5 if cmd[1] == "bootstrap" and failing else (113 if cmd[1] == "print" else 0)
        return subprocess.CompletedProcess(cmd, code, "", "Bootstrap failed: 5: Input/output error" if code == 5 else "")

    mgr = service.Launchd(Paths(user_home), run, uid=501, sleep=waits.append)
    mgr.install(user_home / "agentos")
    assert calls.count("bootstrap") == 4 and waits == [0.5, 1.0, 1.5]


def test_a_load_that_never_works_says_where_to_look(user_home):
    def run(cmd):
        code = 5 if cmd[1] == "bootstrap" else (113 if cmd[1] == "print" else 0)
        return subprocess.CompletedProcess(cmd, code, "", "Bootstrap failed: 5: Input/output error")

    mgr = service.Launchd(Paths(user_home), run, uid=501, sleep=lambda s: None)
    with pytest.raises(service.LocalError, match="could not load"):
        mgr.install(user_home / "agentos")
