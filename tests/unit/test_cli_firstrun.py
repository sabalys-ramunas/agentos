"""QA 2026-11-07 #16: the CLI on a node and as a newcomer reads it. `openajar status` on a node with
no connections shows that node (not "No connections", exit 1); `status --json` and
`status --check --json` are taken; help and hints name the visible commands, never the hidden old
ones; an empty list says there is none and how to add one."""

from __future__ import annotations

import json
import re

import pytest
from test_client import TOKEN, home, node, system  # noqa: F401  (fixtures)
from typer.testing import CliRunner

from openajar import cli_groups
from openajar.cli import app

runner = CliRunner()
HIDDEN = sorted(cli_groups.HOME, key=len, reverse=True)
HIDDEN_RE = re.compile(r"`openajar (" + "|".join(map(re.escape, HIDDEN)) + r")\b")


def test_status_on_a_running_node_shows_the_node(node, home, monkeypatch):  # noqa: F811
    node.write_toml()
    monkeypatch.setenv("OPENAJAR_HOME", str(node.home))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "This box: node node-test" in result.output and "personal/reddit" in result.output
    assert "No connections" not in result.output
    as_json = runner.invoke(app, ["status", "--json"])
    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.output)
    assert data["node"]["node"] == "node-test" and data["connections"] == []


def test_status_on_a_stopped_node_says_so_and_how_to_start(system, tmp_path, monkeypatch):  # noqa: F811
    from openajar.core.config import NodeConfig, save_config

    nh = tmp_path / "nh"
    nh.mkdir()
    save_config(NodeConfig(home=nh, name="box-1", mcp_port=1, mcp_token=TOKEN))
    monkeypatch.setenv("OPENAJAR_HOME", str(nh))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "box-1" in result.output and "not running" in result.output and "openajar up" in result.output


def test_status_check_takes_json(monkeypatch):
    import openajar.ops.cli as ops_cli

    seen = {}
    monkeypatch.setattr(ops_cli, "doctor_cmd", lambda **kw: seen.update(kw))
    result = runner.invoke(app, ["status", "--check", "--json"])
    assert result.exit_code == 0, result.output
    assert seen["as_json"] is True


def test_connect_remove_disconnects(monkeypatch):
    import openajar.client.cli as client_cli

    removed = []
    monkeypatch.setattr(client_cli, "disconnect", lambda name: removed.append(name))
    result = runner.invoke(app, ["connect", "--remove", "mynode"])
    assert result.exit_code == 0, result.output
    assert removed == ["mynode"]


@pytest.mark.parametrize("text", [cli_groups.GUIDE, cli_groups.CONNECT_EPILOG])
def test_guide_and_connect_epilog_name_no_hidden_command(text):
    assert not HIDDEN_RE.findall(text.replace("  openajar ", "  `openajar ")), text


def test_node_init_grant_line_names_the_access_group():
    from openajar.commands.base import GRANT_FOR_AGENTS

    assert GRANT_FOR_AGENTS.startswith("openajar access grants issue ")


def test_empty_workflow_and_mailbox_lists_say_there_are_none(monkeypatch):
    from openajar.runner.mail import cli as mail_cli
    from openajar.runner.workflows import cli as wf_cli

    class Wf:
        def list(self):
            return []

    class Mail:
        class mail:
            @staticmethod
            def list():
                return []

    monkeypatch.setattr(wf_cli, "_svc", lambda: Wf())
    monkeypatch.setattr(mail_cli, "_svc", lambda: Mail())
    wf = runner.invoke(app, ["routines", "workflows", "list"])
    assert wf.exit_code == 0 and "No workflows yet" in wf.output and "workflows apply" in wf.output
    mail = runner.invoke(app, ["vault", "mail", "list"])
    assert mail.exit_code == 0 and "No mailboxes yet" in mail.output and "vault mail connect" in mail.output


def test_version_flag_prints_the_version():
    from openajar import __version__

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and result.output.strip() == __version__
