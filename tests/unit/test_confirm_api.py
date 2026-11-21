"""The confirmation setting's API and CLI: a person signed in, never a token and never an agent."""

from __future__ import annotations

import json

import pytest
from test_api_members import browser, join, owner, role
from test_node_panel import TOKEN, Node, bearer
from typer.testing import CliRunner

from openajar.core.ratelimit import RateLimiter
from openajar.policy.cli import confirmation_app


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    n.api_app.state.api.limiter = RateLimiter(per_key=10_000, global_max=10_000)
    yield n
    n.close()


ROUTES = (("GET", "/api/confirmation"), ("PUT", "/api/confirmation"), ("GET", "/api/confirmation/methods"))
RAISE = {"commit": {"level": "signed_in"}}


@pytest.mark.parametrize("method,path", ROUTES)
def test_no_bearer_token_reaches_it_not_even_the_node_token(node, method, path):
    tokens = [TOKEN, node.grant(), node.grant(subject={"id": "boss-bot"}, admin=True)]
    with browser(node) as c:
        for token in tokens:
            r = c.request(method, path, json=RAISE, headers=bearer(token))
            assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", (token[:6], r.text)
    assert node.policy.confirmation.current().commit.level == "chat"


def test_no_session_reaches_it(node):
    with browser(node) as c:
        assert c.get("/api/confirmation").status_code == 401


def test_a_cookie_write_needs_the_csrf_header(node):
    boss = owner(node)
    r = boss.client.put("/api/confirmation", json=RAISE)
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"


def test_the_owner_reads_the_defaults_and_what_can_be_turned_off(node):
    body = owner(node).get("/api/confirmation").json()
    assert body["commit"] == {"when": "auto", "level": "chat"}
    assert body["pay"] == {"when": "ask", "level": "present"}
    assert body["after_untrusted"] == {"level": "signed_in"}
    assert body["controller_max_level"] == "present" and body["methods_off"] == []
    assert body["can_turn_off"] == ["chat", "telegram"] and body["chat_clients"] == []
    assert body["defaults"]["pay"]["level"] == "present"


def test_the_owner_changes_a_row_and_a_running_node_reads_it(node):
    boss = owner(node)
    r = boss.req("PUT", "/api/confirmation", {"commit": {"when": "ask", "level": "signed_in"}})
    assert r.status_code == 200, r.text
    assert r.json()["commit"] == {"when": "ask", "level": "signed_in"} and r.json()["set_by"] == "owner"
    assert node.policy.ask_first.is_on("lane", "biz-a/instagram") is True


@pytest.mark.parametrize("patch", [{"pay": {"when": "auto"}}, {"irreversible": {"level": "chat"}},
                                   {"after_untrusted": {"level": "chat"}}])
def test_a_floor_is_refused_with_a_reason(node, patch):
    r = owner(node).req("PUT", "/api/confirmation", patch)
    assert r.status_code == 403 and "floor" in r.json()["error"]["message"]


def test_nonsense_is_a_bad_request(node):
    boss = owner(node)
    assert boss.req("PUT", "/api/confirmation", {"surprise": {"level": "chat"}}).status_code == 400
    assert boss.req("PUT", "/api/confirmation", {"commit": {"level": "root"}}).status_code == 400
    assert boss.req("PUT", "/api/confirmation", {}).status_code == 400


def test_an_admin_of_every_workspace_changes_commit_and_only_commit(node):
    ada = join(node, owner(node), "ada", [role("admin", scope="*")])
    assert ada.req("PUT", "/api/confirmation", RAISE).status_code == 200
    r = ada.req("PUT", "/api/confirmation", {"pay": {"level": "signed_in"}})
    assert r.status_code == 403 and "owner_only" in r.json()["error"]["message"]
    assert node.policy.confirmation.current().pay.level == "present"


def test_an_admin_needs_a_recent_check_to_change_it(node):
    ada = join(node, owner(node), "ada", [role("admin", scope="*")])
    node.api_app.state.api.settings.step_up_window = 0
    r = ada.req("PUT", "/api/confirmation", RAISE)
    assert r.status_code == 403 and r.json()["error"]["code"] == "step_up_required"


def test_an_admin_of_one_workspace_does_not_change_a_node_wide_setting(node):
    ada = join(node, owner(node), "ada", [role("admin")])
    assert ada.req("PUT", "/api/confirmation", RAISE).status_code == 403


def test_a_member_reads_it(node):
    va = join(node, owner(node), "va", [role("viewer")])
    assert va.get("/api/confirmation").status_code == 200
    assert va.get("/api/confirmation/methods").status_code == 200
    assert va.req("PUT", "/api/confirmation", RAISE).status_code == 403


def test_methods_list_the_ready_and_the_planned(node):
    rows = {m["id"]: m for m in owner(node).get("/api/confirmation/methods").json()}
    assert rows["panel"] == {"id": "panel", "label": "The panel", "level": "signed_in", "status": "ready", "on": True,
                             "can_turn_off": False}
    assert rows["chat"]["can_turn_off"] is True
    assert {m for m, row in rows.items() if row["status"] == "planned"} == {"mac", "ios", "android", "watch", "totp"}


def test_chat_can_be_turned_off_and_only_the_owner_turns_it_back_on(node):
    boss = owner(node)
    ada = join(node, boss, "ada", [role("admin", scope="*")])
    assert ada.req("PUT", "/api/confirmation", {"methods_off": ["chat"]}).status_code == 200
    assert {m["id"]: m["on"] for m in boss.get("/api/confirmation/methods").json()}["chat"] is False
    assert ada.req("PUT", "/api/confirmation", {"methods_off": []}).status_code == 403
    assert boss.req("PUT", "/api/confirmation", {"methods_off": []}).status_code == 200


@pytest.mark.parametrize("method", ["panel", "cli", "passkey"])
def test_a_way_to_decide_can_never_be_turned_off(node, method):
    assert owner(node).req("PUT", "/api/confirmation", {"methods_off": [method]}).status_code == 400


def test_the_owners_chat_clients_are_listed_by_name(node):
    db = node.policy.db
    for i, client in enumerate(("claude-code 2.1", "claude-code 2.1", "some-client 1")):
        db.execute("INSERT INTO approvals (id, kind, state, summary, requester, created_at, expires_at, decided_at, "
                   "decided_method, decided_detail) VALUES (?, 'action', 'consumed', 's', '{}', 0, 0, ?, 'chat', ?)",
                   (f"ap_{i}", float(i), json.dumps({"client": client})))
    clients = owner(node).get("/api/confirmation").json()["chat_clients"]
    assert {c["client"]: c["decisions"] for c in clients} == {"claude-code 2.1": 2, "some-client 1": 1}


def test_no_tool_changes_it():
    from openajar.policy.tools import tool_specs

    assert not [s.name for s in tool_specs(None) if "confirmation" in s.name]


# ---- the CLI ----

@pytest.fixture
def cli(node, monkeypatch):
    monkeypatch.setenv("OPENAJAR_HOME", str(node.config.home))
    return lambda *a: CliRunner().invoke(confirmation_app, list(a))


def test_cli_shows_the_setting_and_the_methods(cli):
    res = cli("show")
    assert res.exit_code == 0
    assert "pay" in res.output and "present" in res.output and "chat" in res.output
    assert json.loads(cli("show", "--json").output)["pay"]["level"] == "present"


def test_cli_sets_a_row(node, cli):
    assert cli("set", "pay.level", "signed_in").exit_code == 0
    assert node.policy.confirmation.current().pay.level == "signed_in"
    assert cli("set", "commit.when", "ask").exit_code == 0
    assert node.policy.ask_first.is_on("lane", "biz-a/instagram") is True


def test_cli_turns_chat_off_and_on(node, cli):
    assert cli("set", "chat", "off").exit_code == 0
    assert node.policy.confirmation.current().methods_off == ["chat"]
    assert cli("set", "chat", "on").exit_code == 0
    assert node.policy.confirmation.current().methods_off == []


@pytest.mark.parametrize("args", [("pay.when", "auto"), ("pay.level", "chat"), ("after_untrusted.level", "chat"),
                                  ("nope.level", "chat"), ("pay", "present"), ("chat", "maybe")])
def test_cli_refuses_what_a_person_cannot_set(node, cli, args):
    res = cli("set", *args)
    assert res.exit_code != 0
    assert node.policy.confirmation.current().pay.level == "present"
