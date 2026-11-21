"""`openajar up` prints the connect line with the same SSH host as `openajar node init` and every
tunnel hint: OPENAJAR_SSH_HOST, then node.toml, then this machine's hostname, never `<ssh-host>`."""

from __future__ import annotations

import socket

import pytest
from typer.testing import CliRunner

from openajar.cli import app
from openajar.controller import cli as controller_cli


class FakeController:
    def login_link(self):
        return "http://localhost:7300/#code=x", None

    def close(self) -> None:
        pass


@pytest.fixture
def up(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OPENAJAR_SSH_HOST", raising=False)
    monkeypatch.setattr(controller_cli, "_controller", lambda cfg: FakeController())

    async def run_up(node_cfg, cfg, *, on_ready, **kw):
        on_ready(cfg, node_cfg.name)

    monkeypatch.setattr("openajar.controller.up.run_up", run_up)
    monkeypatch.setattr("openajar.core.logs.setup", lambda *a, **kw: [])

    def invoke(*args: str) -> str:
        result = CliRunner().invoke(app, ["up", *args])
        assert result.exit_code == 0, result.output
        return result.output

    return invoke


def connect_line(output: str) -> str:
    return next(line for line in output.splitlines() if "Connect:" in line)


def test_up_uses_openajar_ssh_host(up, monkeypatch):
    monkeypatch.setenv("OPENAJAR_SSH_HOST", "mybox")
    line = connect_line(up())
    assert "openajar connect mybox " in line and "<ssh-host>" not in line


def test_up_falls_back_to_the_hostname(up):
    line = connect_line(up())
    assert f"openajar connect {socket.gethostname()} " in line and "<ssh-host>" not in line


def test_the_flag_still_wins(up, monkeypatch):
    monkeypatch.setenv("OPENAJAR_SSH_HOST", "mybox")
    assert "openajar connect other " in connect_line(up("--ssh-host", "other"))


def test_the_controller_connect_lines_use_the_same_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENAJAR_SSH_HOST", "mybox")
    cfg = controller_cli.ensure_config(controller_cli.controller_home(tmp_path / "home"))
    lines = "\n".join(controller_cli.connect_lines(cfg))
    assert "openajar connect mybox " in lines and "<ssh-host>" not in lines
