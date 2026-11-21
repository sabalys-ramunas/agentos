""""Ask me first" on the panel API (`GET`/`POST /api/ask-first`) and the CLI (`openajar flows ask-first`):
anyone who reaches the lane turns it on; only a person signed in (the owner, or an admin member of the
workspace) or the CLI turns it off; a bearer token, the owner token included, never does."""

from __future__ import annotations

import json

import pytest
from test_api_members import browser, join, owner, role
from test_node_panel import TOKEN, Node, bearer
from typer.testing import CliRunner

from openajar.core.ratelimit import RateLimiter
from openajar.policy.ask_first import open_store
from openajar.runner.cli import app as runner_app

LANE = "biz-a/instagram"


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    n.api_app.state.api.limiter = RateLimiter(per_key=10_000, global_max=10_000)
    yield n
    n.close()


def change(on: bool, scope: str = "lane", ident: str = LANE) -> dict:
    return {"scope": scope, "id": ident, "ask_first": on}


def test_the_owner_signed_in_turns_it_on_and_off(node):
    boss = owner(node)
    r = boss.post("/api/ask-first", change(True))
    assert r.status_code == 200, r.text
    assert r.json()["ask_first"] is True and r.json()["by_person"] is True and r.json()["set_by"] == "owner"
    assert boss.get("/api/ask-first").json() == [r.json()]
    off = boss.post("/api/ask-first", change(False))
    assert off.status_code == 200 and off.json()["ask_first"] is False
    assert boss.post("/api/ask-first", {"scope": "lane", "id": "no-slash", "ask_first": True}).status_code == 400


def test_a_bearer_token_turns_it_on_but_never_off(node):
    agent = node.grant(ceiling="commit")
    with browser(node) as c:
        on = c.post("/api/ask-first", json=change(True), headers=bearer(agent))
        assert on.status_code == 200 and on.json()["by_person"] is False, on.text
        assert on.json()["set_by"].startswith("agent:")
        for tok in (agent, TOKEN):  # the owner token is what agents are often given
            r = c.post("/api/ask-first", json=change(False), headers=bearer(tok))
            assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
            assert "—" not in r.text
        # outside the agent's lanes
        assert c.post("/api/ask-first", json=change(True, ident="biz-b/instagram"),
                      headers=bearer(agent)).status_code == 403
    assert node.policy.ask_first.is_on("lane", LANE)


def test_only_an_admin_member_turns_it_off_in_their_workspace(node):
    boss = owner(node)
    admin = join(node, boss, "va", [role("admin")])
    approver = join(node, boss, "ap", [role("approver")])
    assert approver.post("/api/ask-first", change(True)).status_code == 200  # stricter: anyone in scope
    r = approver.post("/api/ask-first", change(False))
    assert r.status_code == 403 and r.json()["error"]["code"] == "denied"
    assert node.policy.ask_first.is_on("lane", LANE)
    r = admin.post("/api/ask-first", change(False))
    assert r.status_code == 200, r.text
    assert r.json()["by_person"] is True and r.json()["set_by"] == admin.member["id"]
    assert admin.post("/api/ask-first", change(True, ident="biz-b/instagram")).status_code == 403


def test_a_token_save_inherits_and_a_signed_in_save_does_not(node):
    source = {"id": "biz-a.instagram.check", "lane": LANE, "effect": "read",
              "steps": [{"type": "navigate", "url": "https://instagram.com/"}]}
    node.runtime.flows.save(source, by="owner")
    boss = owner(node)
    boss.post("/api/ask-first", change(True, "flow", "biz-a.instagram.check"))
    assert boss.post("/api/flows", {**source, "id": "biz-a.instagram.mine"}).status_code == 201
    assert not node.policy.ask_first.is_on("flow", "biz-a.instagram.mine")
    with browser(node) as c:
        r = c.post("/api/flows", json={**source, "id": "biz-a.instagram.copy"}, headers=bearer(TOKEN))
        assert r.status_code == 201, r.text
    assert node.policy.ask_first.is_on("flow", "biz-a.instagram.copy")


def test_flows_list_shows_the_switch(node):
    node.runtime.flows.save({"id": "biz-a.instagram.check", "lane": LANE, "effect": "read",
                             "steps": [{"type": "navigate", "url": "https://instagram.com/"}]}, by="owner")
    boss = owner(node)
    [flow] = boss.get("/api/flows").json()
    assert flow["ask_first"] is False and flow["ask_first_by"] is None
    boss.post("/api/ask-first", change(True, "flow", "biz-a.instagram.check"))
    [flow] = boss.get("/api/flows").json()
    assert flow["ask_first"] is True and flow["ask_first_by"] == "person"


# ---------------------------------------------------------------- the CLI

def test_cli_sets_lists_and_shows(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path))
    run = CliRunner().invoke
    empty = run(runner_app, ["flows", "ask-first"])
    assert empty.exit_code == 0 and "runs on its own" in empty.output
    on = run(runner_app, ["flows", "ask-first", "biz-a.instagram.post", "on"])
    assert on.exit_code == 0, on.output
    assert "asks you first" in on.output
    acct = run(runner_app, ["flows", "ask-first", "--account", LANE, "on"])
    assert acct.exit_code == 0 and f"account {LANE}" in acct.output
    listed = run(runner_app, ["flows", "ask-first", "--json"])
    rows = json.loads(listed.output)
    assert {(r["scope"], r["id"], r["ask_first"], r["set_by"]) for r in rows} == {
        ("flow", "biz-a.instagram.post", True, "owner:cli"), ("lane", LANE, True, "owner:cli")}
    off = run(runner_app, ["flows", "ask-first", "biz-a.instagram.post", "off"])
    assert off.exit_code == 0 and "runs on its own" in off.output
    assert not open_store(tmp_path).is_on("flow", "biz-a.instagram.post")
    bad = run(runner_app, ["flows", "ask-first", "biz-a.instagram.post", "maybe"])
    assert bad.exit_code == 1
    for res in (empty, on, acct, off, bad):
        assert "—" not in res.output


@pytest.mark.parametrize("variable", ["OPENAJAR_LANE", "OPENAJAR_BRAIN_TASK"])
def test_a_process_an_agent_runs_turns_an_account_on_and_never_off(tmp_path, monkeypatch, variable):
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path))
    monkeypatch.setenv(variable, "biz-a/shell")
    run = CliRunner().invoke
    off = run(runner_app, ["flows", "ask-first", "--account", LANE, "off"])
    assert off.exit_code == 1 and "only a person" in off.output and "—" not in off.output
    assert open_store(tmp_path).get("lane", LANE) is None  # still no choice, so written words still ask
    assert run(runner_app, ["flows", "ask-first", "--account", LANE, "on"]).exit_code == 0
    setting = open_store(tmp_path).get("lane", LANE)
    assert setting.ask_first is True and setting.by_person is False
    assert run(runner_app, ["flows", "ask-first", "--account", LANE, "off"]).exit_code == 1
    assert open_store(tmp_path).is_on("lane", LANE)


def test_the_cli_says_when_nobody_has_chosen(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAJAR_LANE", raising=False)
    monkeypatch.delenv("OPENAJAR_BRAIN_TASK", raising=False)
    run = CliRunner().invoke
    unset = run(runner_app, ["flows", "ask-first", "--account", LANE])
    assert unset.exit_code == 0 and "no choice made yet" in unset.output and "still ask you" in unset.output
    assert json.loads(run(runner_app, ["flows", "ask-first", "--account", LANE, "--json"]).output)["chosen"] is False
    assert run(runner_app, ["flows", "ask-first", "--account", LANE, "off"]).exit_code == 0
    chosen = run(runner_app, ["flows", "ask-first", "--account", LANE])
    assert "runs on its own" in chosen.output and "no choice" not in chosen.output
    assert open_store(tmp_path).chosen_off("lane", LANE)
