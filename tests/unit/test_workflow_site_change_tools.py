"""What a person and an agent reach a `site_change` trigger through: the template, the catalog, the CLI."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from siteapi_http_support import SURFACE
from test_workflow_site_change import READ, make, runs
from typer.testing import CliRunner
from workflow_support import OWNER


@pytest.fixture
def node(tmp_path):
    return make(tmp_path)


def test_the_template_says_what_it_does_and_waits_for_an_endpoint(node):
    made = node.wf.from_template("new_row", "harbor")
    saved = node.wf.save(made, OWNER)
    assert saved["trigger"]["kind"] == "site_change" and saved["missing"]
    made["trigger"].update({"operation": f"{READ}@{SURFACE}", "list": "orders", "key": "number"})
    done = node.wf.save(made, OWNER)
    assert done["missing"] == []


async def test_the_template_tells_you_of_a_new_row(node):
    made = node.wf.from_template("new_row", "harbor")
    made["trigger"].update({"operation": f"{READ}@{SURFACE}", "list": "orders", "key": "number"})
    wid = node.wf.save(made, OWNER)["id"]
    node.wf.enable(wid, OWNER)
    await node.settle()
    node.site.state["rows"].append(("A-5", "50"))
    node.h.clock.set(node.h.clock.now() + timedelta(minutes=15))
    await node.settle()
    [view] = runs(node, wid)
    assert view["status"].startswith("succeeded") and "A-5" in node.step(view, "tell")["input"]["message"]


def test_the_catalog_says_what_the_trigger_gives(node):
    fields = {f["key"]: f for f in node.wf.catalog()["trigger_fields"]["site_change"]}
    assert fields["added"]["rows"] is True and fields["added"]["untrusted"] is True and fields["added"]["type"] == "list"
    assert "untrusted" not in fields["count_added"]


def test_the_cli_says_how_a_watch_stands(tmp_path, monkeypatch):
    from agentos.runner.workflows import cli

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    plain = {"name": "Plain", "workspace": "harbor", "trigger": {"kind": "manual"},
             "steps": [{"id": "n", "kind": "notify", "label": "tell you", "message": "It ran."}]}
    path = tmp_path / "plain.json"
    path.write_text(json.dumps(plain))
    assert CliRunner().invoke(cli.app, ["apply", str(path)]).exit_code == 0
    out = CliRunner().invoke(cli.app, ["watch", "harbor.routine.plain"])
    assert out.exit_code == 1 and "does not watch a site" in out.output
    unfinished = {**plain, "name": "Orders", "trigger": {"kind": "site_change", "every_minutes": 15}}
    path.write_text(json.dumps(unfinished))
    assert CliRunner().invoke(cli.app, ["apply", str(path)]).exit_code == 0
    out = CliRunner().invoke(cli.app, ["watch", "harbor.routine.orders"])
    assert out.exit_code == 0 and "It is off, so nothing is read." in out.output and "none read yet" in out.output
