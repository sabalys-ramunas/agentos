"""The generated tool reference (openajar tools, docs/TOOLS.md)."""

from typing import Annotated, Literal

from pydantic import Field
from typer.testing import CliRunner

from openajar.cli import app
from openajar.core.models import ActionKind
from openajar.mcp.reference import type_name


def test_type_names_read_like_the_docs():
    assert type_name(str) == "str"
    assert type_name(int | None) == "int | None"
    assert type_name(list[str]) == "list[str]"
    assert type_name(Annotated[str, Field(description="x")]) == "str"
    assert type_name(Literal["view", "control"]) == "'view' | 'control'"
    assert "click" in type_name(ActionKind) and "type" in type_name(ActionKind)


def test_openajar_tools_lists_every_served_tool():
    result = CliRunner().invoke(app, ["tools", "--json"])
    assert result.exit_code == 0, result.output
    import json

    names = {row["name"] for row in json.loads(result.output)}
    assert {"browser_open", "browser_snapshot", "browser_act", "browser_file_put", "vault_list", "flow_run"} <= names
    assert "—" not in result.output


def test_config_show_names_each_source_and_never_prints_the_token(tmp_path, monkeypatch):
    import json

    home = tmp_path / "home"
    monkeypatch.setenv("OPENAJAR_HOME", str(home))
    monkeypatch.setenv("OPENAJAR_SSH_HOST", "node-from-env")
    runner = CliRunner()
    assert runner.invoke(app, ["node", "init", "--mcp-port", "7444"]).exit_code == 0
    token = (home / "node.toml").read_text().split('mcp_token = "')[1].split('"')[0]
    result = runner.invoke(app, ["config", "show", "--json"])
    assert result.exit_code == 0, result.output
    rows = {r["name"]: r for r in json.loads(result.output)}
    assert rows["mcp_port"]["value"] == 7444 and rows["mcp_port"]["source"] == "node.toml"
    assert rows["ssh_host"]["value"] == "node-from-env" and rows["ssh_host"]["source"] == "env OPENAJAR_SSH_HOST"
    assert rows["max_slots"]["source"] == "node.toml (same as default)"
    assert rows["mcp_token"]["value"] == "(set, hidden)"
    plain = runner.invoke(app, ["config", "show"])
    assert token not in result.output and token not in plain.output


def test_dashboard_lists_questions_once_and_reads_watch_json():
    from types import SimpleNamespace

    from openajar.tui.runs import schedule_when

    assert schedule_when(SimpleNamespace(cron=None, watch='{"url": "x", "every_minutes": 15}')) == "watch every 15m"
    assert schedule_when(SimpleNamespace(cron="0 9 * * *", watch=None)) == "0 9 * * *"
    assert schedule_when(SimpleNamespace(cron=None, watch="not json")) == "watch"
