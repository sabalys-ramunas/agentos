"""The browser worker on one lane in the panel API: a person signed in reads and sets both switches,
and no bearer token reaches them, the owner token included."""

from __future__ import annotations

import pytest
from test_api_members import browser, join, owner, role
from test_node_panel import TOKEN, Node, bearer

from openajar.api.auth import PRESENCE_HEADER
from openajar.core.ratelimit import RateLimiter
from openajar.plugins.service import LANE_WORKER, PluginService

LANE = "biz-a/instagram"
URL = f"/api/lanes/{LANE}/worker"
ASK = f"{URL}/ask-first"


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    n.api_app.state.api.limiter = RateLimiter(per_key=10_000, global_max=10_000)
    n.runtime.register("plugins", PluginService(n.runtime, home=n.config.home))
    yield n
    n.close()


def worker_on(node: Node, lane: str = LANE) -> bool:
    return node.runtime.get("plugins").lane_stop(LANE_WORKER, lane) is None


def history(node: Node) -> list[dict]:
    node.policy.traces.flush()
    events, _ = node.policy.traces.query(kind="plugin_change", limit=20)
    return [e.public() for e in events]


def test_the_owner_reads_both_switches_and_nobody_has_chosen_yet(node):
    got = owner(node).get(URL)
    assert got.status_code == 200, got.text
    assert got.json() == {
        "lane": LANE,
        "worker": {"plugin": "browser-worker", "plugin_status": "unavailable", "on": True, "problem": None},
        "ask_first": {"choice": None, "asks": False, "written_words_ask": True, "after_a_page_asks": True,
                      "set_by": None, "by_person": None, "set_at": None}}


def test_the_owner_sets_the_worker_and_it_is_in_the_action_log(node):
    boss = owner(node)
    off = boss.req("PUT", URL, {"on": False})
    assert off.status_code == 200, off.text
    assert off.json()["worker"]["on"] is False and not worker_on(node)
    [row] = history(node)
    assert row["principal"] == "panel:owner" and row["summary"] == f"browser-worker turned off on {LANE}"
    on = boss.req("PUT", URL, {"on": True})
    assert on.json()["worker"]["on"] is True and worker_on(node)
    assert history(node)[0]["summary"] == f"browser-worker turned on on {LANE}"


def test_the_owner_chooses_how_a_publish_goes(node):
    boss = owner(node)
    asks = boss.req("PUT", ASK, {"ask_first": True}).json()["ask_first"]
    assert asks["choice"] == "asks_first" and asks["asks"] is True and asks["written_words_ask"] is True
    alone = boss.req("PUT", ASK, {"ask_first": False}).json()["ask_first"]
    assert alone["choice"] == "runs_on_its_own" and alone["asks"] is False and alone["written_words_ask"] is False
    assert alone["by_person"] is True and alone["set_by"] == "owner" and alone["set_at"]
    assert node.policy.ask_first.chosen_off("lane", LANE)


@pytest.mark.parametrize("token", ["agent", "owner"])
def test_no_bearer_token_reaches_these_routes(node, token):
    tok = node.grant(ceiling="commit") if token == "agent" else TOKEN
    with browser(node) as c:
        calls = [c.get(URL, headers=bearer(tok)),
                 c.put(URL, json={"on": True}, headers=bearer(tok)),
                 c.put(URL, json={"on": False}, headers=bearer(tok)),
                 c.put(ASK, json={"ask_first": False}, headers=bearer(tok))]  # on is anyone's: see below
    for r in calls:
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
        assert "—" not in r.text
    assert worker_on(node) and node.policy.ask_first.get("lane", LANE) is None


def test_nobody_signed_out_reaches_them(node):
    with browser(node) as c:
        assert c.get(URL).status_code == 401
        assert c.put(URL, json={"on": False}).status_code == 401


def test_a_cookie_write_needs_the_csrf_header(node):
    boss = owner(node)
    assert boss.client.put(URL, json={"on": False}).status_code == 403
    assert worker_on(node)


def test_the_looser_way_needs_an_admin_of_the_workspace(node):
    boss = owner(node)
    admin = join(node, boss, "va", [role("admin")])
    approver = join(node, boss, "ap", [role("approver")])
    assert approver.get(URL).status_code == 200
    assert approver.req("PUT", URL, {"on": False}).status_code == 200  # the safe way: anyone who sees the lane
    assert approver.req("PUT", ASK, {"ask_first": True}).status_code == 200
    for r in (approver.req("PUT", URL, {"on": True}), approver.req("PUT", ASK, {"ask_first": False})):
        assert r.status_code == 403 and r.json()["error"]["code"] == "denied", r.text
    assert not worker_on(node) and not node.policy.ask_first.chosen_off("lane", LANE)
    assert admin.req("PUT", URL, {"on": True}).status_code == 200 and worker_on(node)
    alone = admin.req("PUT", ASK, {"ask_first": False})
    assert alone.status_code == 200 and alone.json()["ask_first"]["set_by"] == admin.member["id"]
    assert history(node)[0]["principal"] == f"panel:{admin.member['id']}"


def test_a_lane_outside_the_callers_reach_is_refused_before_any_lookup(node):
    boss = owner(node)
    admin = join(node, boss, "va", [role("admin")])
    other = "/api/lanes/biz-b/instagram/worker"
    assert admin.get(other).status_code == 403
    assert admin.req("PUT", other, {"on": False}).status_code == 403
    assert admin.req("PUT", f"{other}/ask-first", {"ask_first": False}).status_code == 403
    assert worker_on(node, "biz-b/instagram")


def test_a_lane_that_is_not_there_is_404_and_nothing_is_stored(node):
    boss = owner(node)
    missing = "/api/lanes/biz-a/nothing/worker"
    assert boss.get(missing).status_code == 404
    assert boss.req("PUT", missing, {"on": False}).status_code == 404
    assert boss.req("PUT", f"{missing}/ask-first", {"ask_first": False}).status_code == 404
    assert worker_on(node, "biz-a/nothing") and node.policy.ask_first.get("lane", "biz-a/nothing") is None


def test_a_body_that_says_anything_else_is_refused(node):
    boss = owner(node)
    assert boss.req("PUT", URL, {"on": "off"}).status_code == 400
    assert boss.req("PUT", URL, {"on": False, "lane": "biz-b/instagram"}).status_code == 400
    assert worker_on(node)


# ---- Ask me first on a lane: ON is anyone whose reach covers the lane; OFF needs a person ----

def test_the_node_token_turns_ask_first_on_without_a_code(node):
    with browser(node) as c:
        r = c.put(ASK, json={"ask_first": True}, headers=bearer(TOKEN))
    assert r.status_code == 200, r.text
    assert r.json()["ask_first"]["choice"] == "asks_first"
    assert "worker" not in r.json()  # the worker's state is a person's to read, as GET is
    assert node.policy.ask_first.is_on("lane", LANE)


def test_a_grant_whose_scope_reaches_the_lane_turns_ask_first_on(node):
    agent = node.grant(ceiling="commit")
    with browser(node) as c:
        r = c.put(ASK, json={"ask_first": True}, headers=bearer(agent))
    assert r.status_code == 200, r.text
    assert r.json()["ask_first"]["choice"] == "asks_first"
    assert node.policy.ask_first.is_on("lane", LANE)


def test_a_grant_outside_the_lane_is_refused_ask_first_on(node):
    outside = node.grant(workspaces=["biz-b"], ceiling="commit")
    with browser(node) as c:
        r = c.put(ASK, json={"ask_first": True}, headers=bearer(outside))
    assert r.status_code == 403 and r.json()["error"]["code"] == "denied", r.text
    assert node.policy.ask_first.get("lane", LANE) is None


def test_turning_ask_first_off_stays_a_person_only(node):
    agent = node.grant(ceiling="commit")
    with browser(node) as c:
        for tok in (TOKEN, agent):
            r = c.put(ASK, json={"ask_first": False}, headers=bearer(tok))
            assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
    assert node.policy.ask_first.get("lane", LANE) is None


def test_a_person_turns_ask_first_off_in_the_panel_or_with_a_one_time_code(node):
    assert owner(node).req("PUT", ASK, {"ask_first": False}).status_code == 200
    node.store.add_code("worker-off-code", ttl_seconds=60)
    with browser(node) as c:
        r = c.put(ASK, json={"ask_first": False}, headers={**bearer(TOKEN), PRESENCE_HEADER: "worker-off-code"})
    assert r.status_code == 200, r.text
    assert node.policy.ask_first.chosen_off("lane", LANE)
