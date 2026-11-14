"""`agentos lane take|handback|stop|deny|view` against a daemon-like HTTP server running in a
thread: the owner's control API behind the same bearer check the MCP endpoint uses."""

from __future__ import annotations

import asyncio
import socket
import threading
import tomllib

import pytest
import tomli_w
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount
from test_lanes import make
from typer.testing import CliRunner

from agentos.cli import app
from agentos.core.config import load_config
from agentos.core.models import ControlState, LaneSpec
from agentos.mcp.server import BearerAuth
from agentos.node.viewer_app import ViewTokens, control_api_routes

LANE = "personal/reddit"
EM_DASH = "\u2014"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Daemon:
    """A lane manager and the control API on 127.0.0.1:<port>, in its own thread and loop."""

    def __init__(self, home, token: str, port: int, *, with_api: bool = True):
        self.home, self.token, self.port, self.with_api = home, token, port, with_api
        self.ready = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.mgr = None
        self.server: uvicorn.Server | None = None
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.mgr, *_ = make(self.home, mcp_port=self.port)
        await self.mgr.create(LaneSpec(id=LANE))
        routes = control_api_routes(self.mgr, ViewTokens.for_config(self.mgr.config)) if self.with_api else []
        api = BearerAuth(Starlette(routes=[Mount("/api", routes=routes)]), self.token)
        self.server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=self.port, log_level="warning",
                                                    lifespan="off"))
        task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)
        self.ready.set()
        await task
        await self.mgr.shutdown()

    def run(self, coro_factory):
        return asyncio.run_coroutine_threadsafe(coro_factory(), self.loop).result(5)

    def control(self):
        return self.mgr.get(LANE).control

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(10), "server did not start"
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "agentos-home"
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    monkeypatch.delenv("AGENTOS_SSH_HOST", raising=False)
    return home


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def daemon(home, runner):
    port = free_port()
    result = runner.invoke(app, ["node", "init", "--mcp-port", str(port), "--ssh-host", "mynode"])
    assert result.exit_code == 0, result.output
    cfg = load_config(home)
    with Daemon(home, cfg.mcp_token, port) as d:
        yield d


def lane(runner, *args):
    result = runner.invoke(app, ["lane", *args])
    assert EM_DASH not in result.output
    return result


def test_take_handback_stop_deny(daemon, runner):
    result = lane(runner, "take", LANE)
    assert result.exit_code == 0, result.output
    assert f"You have control of {LANE}" in result.output and "human_in_control" in result.output
    assert "10 min without input" in result.output
    assert daemon.control().state == ControlState.HUMAN and daemon.control().by == "owner"
    assert "already have control" in lane(runner, "take", LANE).output

    assert "No agent is asking" in lane(runner, "deny", LANE).output
    daemon.run(lambda: daemon.mgr.request_control(LANE, by="claude-code", reason="ready"))
    result = lane(runner, "deny", LANE)
    assert result.exit_code == 0 and "Refused the agent's request" in result.output
    assert not daemon.control().takeback_requested

    result = lane(runner, "stop", LANE)
    assert "Stopped agents on personal/reddit" in result.output and "agentos lane handback" in result.output
    assert daemon.control().state == ControlState.PAUSED
    assert "already stopped" in lane(runner, "stop", LANE).output

    result = lane(runner, "handback", LANE)
    assert result.exit_code == 0 and f"Handed {LANE} back to agents." in result.output
    assert daemon.control().state == ControlState.AGENT
    assert "already have control" in lane(runner, "handback", LANE).output


def test_handback_with_a_note(daemon, runner):
    lane(runner, "take", LANE)
    result = lane(runner, "handback", LANE, "--note", "Fixed the address; post it now")
    assert result.exit_code == 0, result.output
    control = daemon.control()
    assert control.state == ControlState.AGENT and control.handback_note == "Fixed the address; post it now"
    lane(runner, "take", LANE)
    result = lane(runner, "handback", LANE, "--note", "x" * 301)
    assert result.exit_code != 0 and "at most 300" in result.output
    assert daemon.control().state == ControlState.HUMAN


def test_view_prints_a_signed_link_and_the_tunnel(daemon, runner, home):
    result = lane(runner, "view", LANE, "--control", "--ttl", "120")
    assert result.exit_code == 0, result.output
    assert "control link" in result.output
    url = next(line.strip() for line in result.output.splitlines() if "/view/" in line)
    assert url.startswith(f"http://localhost:{daemon.port}/view/personal/reddit?t=")
    token = url.split("?t=", 1)[1]
    assert ViewTokens.for_config(load_config(home)).verify(token, LANE).can_control
    assert f"ssh -N -L {daemon.port}:localhost:{daemon.port} mynode" in result.output
    result = lane(runner, "view", LANE)
    assert "view-only link" in result.output
    assert lane(runner, "view", LANE, "--ttl", "5").exit_code == 2  # below the minimum


def test_unknown_lane_and_bad_ids(daemon, runner):
    result = lane(runner, "take", "personal/nope")
    assert result.exit_code == 1 and "lane_not_found" in result.output
    result = lane(runner, "take", "Not A Lane")
    assert result.exit_code == 2


def test_daemon_not_running(home, runner):
    port = free_port()
    assert runner.invoke(app, ["node", "init", "--mcp-port", str(port)]).exit_code == 0
    for cmd in ("take", "handback", "stop", "deny", "view"):
        result = lane(runner, cmd, LANE)
        assert result.exit_code == 1
        assert f"agentosd is not running on http://127.0.0.1:{port}" in result.output
        assert "agentos node serve" in result.output


def test_not_initialized(home, runner):
    result = lane(runner, "take", LANE)
    assert result.exit_code == 1 and "agentos node init" in result.output


def test_wrong_token(daemon, runner, home):
    path = home / "node.toml"
    data = tomllib.loads(path.read_text())
    data["mcp_token"] = "not-the-token"
    path.write_text(tomli_w.dumps(data))
    result = lane(runner, "take", LANE)
    assert result.exit_code == 1 and "refused the node token" in result.output
    assert daemon.control().state == ControlState.AGENT


def test_an_older_daemon_without_the_control_api(home, runner):
    port = free_port()
    assert runner.invoke(app, ["node", "init", "--mcp-port", str(port)]).exit_code == 0
    with Daemon(home, load_config(home).mcp_token, port, with_api=False):
        result = lane(runner, "take", LANE)
    assert result.exit_code == 1 and "no lane control API" in result.output
