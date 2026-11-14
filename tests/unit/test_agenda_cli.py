"""`agentos agenda ...`: the owner's view of the agenda from a shell on the node."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from agentos.agenda.cli import app
from agentos.core.runtime import Runtime
from agentos.policy.service import PolicyService
from agentos.runner.fakes import post_flow
from agentos.runner.service import build as runner_build

FLOW = "biz-a.social.post"
SOON = (datetime.now(UTC) + timedelta(days=30)).replace(hour=9, minute=0, second=0, microsecond=0)


def _home(tmp_path, monkeypatch, *, ask_first: bool):
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    runner = runner_build(Runtime(), home=tmp_path)
    runner.flows.save(post_flow())
    runner.store.close()
    policy = PolicyService(tmp_path)
    if ask_first:
        policy.ask_first.set("flow", FLOW, True, by="owner", person=True)
    policy.close()


def _id(output: str) -> str:
    return output.split()[0]


def test_the_owner_adds_proposes_approves_lists_and_cancels(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, ask_first=True)
    cli = CliRunner()
    r = cli.invoke(app, ["add", "--title", "Send the November invoice reminder", "--flow", FLOW,
                         "-p", "caption=Your invoice is due", "-p", "image=x.png", "--at", SOON.isoformat()])
    assert r.exit_code == 0, r.output
    entry = _id(r.output)
    assert r.output.startswith(f"{entry} draft")
    r = cli.invoke(app, ["propose", entry])
    assert f"{entry} proposed" in r.output and "waiting in Needs you" in r.output
    r = cli.invoke(app, ["approve", entry, "--note", "ok"])
    assert r.exit_code == 0 and r.output.startswith(f"{entry} scheduled"), r.output
    r = cli.invoke(app, ["list", "--from", (SOON - timedelta(days=1)).isoformat(), "--days", "3"])
    assert entry in r.output and "sched" in r.output and "Send the November invoice reminder" in r.output
    shown = json.loads(cli.invoke(app, ["show", entry]).output)
    assert shown["approval"]["via"] == "person" and shown["approval"]["by"] == "owner:cli"
    r = cli.invoke(app, ["cancel", entry, "--reason", "paid early"])
    assert r.output.startswith(f"{entry} cancelled: paid early")
    r = cli.invoke(app, ["approve", entry])
    assert r.exit_code == 1 and "agenda_state" in r.output


def test_an_owner_entry_that_runs_on_its_own_is_scheduled_when_added(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, ask_first=False)
    r = CliRunner().invoke(app, ["add", "--title", "Post", "--flow", FLOW, "-p", "caption=hi", "-p", "image=x.png",
                                 "--not-before", SOON.isoformat(), "--approve"])
    assert r.exit_code == 0 and " scheduled" in r.output, r.output
    r = CliRunner().invoke(app, ["add", "--title", "Post", "--flow", "nope.flow.x"])
    assert r.exit_code == 1 and "not_found" in r.output
