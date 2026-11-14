"""Activate on a card, over the real panel app on the real policy: only a person in the panel, or at
the node, turns a proposal into a live API. A token never does, whoever holds it."""

from __future__ import annotations

import asyncio
import copy

import pytest
from test_node_panel import TOKEN, Node, bearer

from agentos.core.errors import PolicyDenied
from agentos.runner.models import TRY_SOURCE
from agentos.runner.siteapis import tools as site_tools
from agentos.runner.siteapis.models import SAMPLE
from agentos.runner.siteapis.service import ACTIVATE, LEAVE

API, LANE = "harbor-supplier", "biz-a/instagram"
FLOW = f"connector.{API}.open_orders"


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    n.svc = n.runtime.get("site_apis")
    yield n
    n.close()


def propose(node: Node, token: str) -> str:
    doc = copy.deepcopy(SAMPLE)
    doc["surfaces"] = [LANE]
    ctx = node.policy.context_for_token(token, tool="site_api_propose")
    return asyncio.run(site_tools.site_api_propose(ctx, draft=doc))["approval_id"]


def answer(client, card: str, headers: dict, value: str = ACTIVATE):
    return client.post(f"/api/questions/{card}/answer", json={"answer": value}, headers=headers)


def is_live(node: Node) -> bool:
    node.svc.check_proposals()
    return node.svc.state_of(API) == "active" and node.runtime.get("flows").store.get_flow(FLOW) is not None


def test_a_signed_in_owner_answering_the_card_activates_it(node):
    card = propose(node, node.grant(subject={"id": "asker"}, ceiling="draft"))
    with node.client() as client:
        assert answer(client, card, node.sign_in(client)).status_code == 200
    assert is_live(node)


def test_the_owner_activates_what_an_agent_on_the_node_token_proposed(node):
    card = propose(node, TOKEN)
    with node.client() as client:
        assert answer(client, card, node.sign_in(client)).status_code == 200
    assert is_live(node)


def test_leave_it_activates_nothing(node):
    card = propose(node, node.grant(subject={"id": "asker"}, ceiling="draft"))
    with node.client() as client:
        assert answer(client, card, node.sign_in(client), LEAVE).status_code == 200
    assert not is_live(node)


def test_the_node_token_does_not_activate_another_agents_card(node):
    card = propose(node, node.grant(subject={"id": "asker"}, ceiling="draft"))
    with node.client() as client:
        r = answer(client, card, bearer(TOKEN))
    # a token is never a person (agentos.core.presence): refused before the card is touched
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
    assert not is_live(node) and node.svc.state_of(API) == "proposed"
    assert node.policy.approvals.get(card).state == "pending"


def test_an_agent_grant_cannot_answer_the_card(node):
    card = propose(node, node.grant(subject={"id": "asker"}, ceiling="draft"))
    with node.client() as client:
        assert answer(client, card, bearer(node.grant(subject={"id": "other"}))).status_code == 403
    assert not is_live(node) and node.policy.approvals.get(card).state == "pending"


def test_an_admin_grant_does_not_activate_another_agents_card(node):
    card = propose(node, node.grant(subject={"id": "asker"}, ceiling="draft"))
    with node.client() as client:
        r = answer(client, card, bearer(node.grant(subject={"id": "admin-agent"}, admin=True, ceiling="draft")))
    # a token is never a person (agentos.core.presence): refused before the card is touched
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
    assert not is_live(node) and node.svc.state_of(API) == "proposed"
    assert node.policy.approvals.get(card).state == "pending"


@pytest.mark.parametrize("proposer", ["admin", "node"])
def test_the_agent_that_proposed_cannot_answer_its_own_card(node, proposer):
    token = node.grant(subject={"id": "admin-agent"}, admin=True, ceiling="draft") if proposer == "admin" else TOKEN
    card = propose(node, token)
    with node.client() as client:
        r = answer(client, card, bearer(token))
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"  # a token is never a person
    assert not is_live(node) and node.policy.approvals.get(card).state == "pending"


def test_a_narrowed_child_cannot_answer_its_familys_card(node):
    admin = node.grant(subject={"id": "admin-agent"}, admin=True, ceiling="draft")
    child = node.policy.attenuate(admin, admin=True, ttl_s=600)[1]
    card = propose(node, admin)
    with node.client() as client:
        assert answer(client, card, bearer(child)).status_code == 403
    assert not is_live(node) and node.policy.approvals.get(card).state == "pending"


def test_a_card_asks_under_the_grant_that_proposed_it(node):
    token = node.grant(subject={"id": "asker"}, ceiling="draft")
    card = node.policy.approvals.get(propose(node, token))
    assert card.grant_path == node.policy.context_for_token(token, tool="x").grant.path


def test_the_node_cli_activates_and_a_member_named_owner_does_not(node):
    card = propose(node, node.grant(subject={"id": "asker"}, ceiling="draft"))
    node.policy.approvals.decide(card, "approve", by="owner", via="passkey", answer=ACTIVATE)
    assert not is_live(node)
    card = node.svc.propose(API, by={"kind": "agent", "id": "asker", "owner": "owner"})["approval_id"]
    node.policy.approvals.decide(card, "approve", by="owner:cli", answer=ACTIVATE)
    assert is_live(node)


def test_a_controller_relaying_an_answer_does_not_activate(node):
    card = propose(node, node.grant(subject={"id": "asker"}, ceiling="draft"))
    node.policy.approvals.decide(card, "approve", by="controller:owner", answer=ACTIVATE)
    assert not is_live(node)


# ---- what may run: the active version, and a try ----


OWNER = {"kind": "human", "id": "owner", "owner": "owner"}


def staged(node: Node) -> str:
    """A version a try staged and nobody activated, beside the active one."""
    doc = copy.deepcopy(SAMPLE)
    doc["surfaces"] = [LANE]
    doc["version"] = "1.0.1"
    doc["endpoints"]["open_orders"]["steps"].insert(1, {"type": "wait_for", "locator": {"role": "main"}})
    node.svc.save(doc, by=OWNER)
    return node.svc.prepare_try(API, "open_orders", {"status": "open"}, by=OWNER, on=LANE)[0].version


def active(node: Node) -> str:
    doc = copy.deepcopy(SAMPLE)
    doc["surfaces"] = [LANE]
    node.svc.save(doc, by=OWNER)
    node.svc.activate(API, by=OWNER)
    return node.runtime.get("flows").get(FLOW).version


def never_activated(node: Node) -> str:
    doc = copy.deepcopy(SAMPLE)
    doc["surfaces"] = [LANE]
    node.svc.save(doc, by=OWNER)
    return node.svc.prepare_try(API, "open_orders", {"status": "open"}, by=OWNER, on=LANE)[0].version


RUN = {"on": LANE, "params": {"status": "open"}}


def pinned(head: bool, node: Node) -> str:
    if head:
        active(node)
    return staged(node) if head else never_activated(node)


BOTH = pytest.mark.parametrize("head", [True, False], ids=["beside the active one", "never activated"])


@BOTH
def test_no_token_runs_a_version_that_is_not_active(node, head):
    pin = pinned(head, node)
    admin = node.grant(subject={"id": "admin-agent"}, admin=True, ceiling="draft")
    with node.client() as client:
        for headers in (bearer(TOKEN), bearer(admin)):
            r = client.post(f"/api/connectors/{API}.open_orders/run", json={**RUN, "version": pin}, headers=headers)
            assert r.status_code == 403, r.text
    assert not node.runtime.get("runner").list(flow_id=FLOW)


@BOTH
def test_the_flows_route_runs_no_pinned_version_either(node, head):
    pin = pinned(head, node)
    with node.client() as client:
        client.post(f"/api/flows/{FLOW}/run", json={**RUN, "version": pin}, headers=bearer(TOKEN))
    assert pin not in {r.flow_version for r in node.runtime.get("runner").list(flow_id=FLOW)}


@BOTH
def test_the_runner_refuses_a_pin_whoever_asks(node, head):
    pin = pinned(head, node)
    with pytest.raises(PolicyDenied, match="not active"):
        node.runtime.get("runner").submit(flow_id=FLOW, version=pin, params={"status": "open"}, surface=LANE, principal=OWNER)


@BOTH
def test_a_schedule_cannot_pin_a_version_that_is_not_active(node, head):
    pin = pinned(head, node)
    scheduler = node.runtime.get("runner").scheduler
    with pytest.raises(PolicyDenied, match="not active"):
        scheduler.add(flow=FLOW, on=LANE, version=pin, cron="0 9 * * *", params={"status": "open"}, principal=OWNER)
    assert not node.runtime.get("runner").store.list_schedules()


def test_the_panels_schedule_route_has_no_version_to_pin(node):
    pin = pinned(True, node)
    body = {"flow": FLOW, "version": pin, "cron": "0 9 * * *", "params": {"status": "open"}}
    with node.client() as client:
        person = node.sign_in(client)
        assert client.post("/api/schedules", json=body, headers=person).status_code == 400
    assert not node.runtime.get("runner").store.list_schedules()


def test_the_active_version_still_runs_and_schedules_when_it_is_named(node):
    pin = active(node)
    with node.client() as client:
        r = client.post(f"/api/connectors/{API}.open_orders/run", json={**RUN, "version": pin}, headers=bearer(TOKEN))
    assert r.status_code == 202, r.text
    s, _ = node.runtime.get("runner").scheduler.add(flow=FLOW, on=LANE, version=pin, cron="0 9 * * *", params={"status": "open"},
                                                    principal=OWNER)
    assert s.flow_version == pin


def test_a_try_still_runs_its_staged_version(node):
    active(node)
    pin = staged(node)
    fv, target, effect = node.svc.prepare_try(API, "open_orders", {"status": "open"}, by=OWNER, on=LANE)
    assert fv.version == pin and effect == "read"
    run, _ = node.runtime.get("runner").submit(flow_id=fv.id, version=fv.version, params=target.caller_params, surface=target.surface.id,
                                               target=target, principal=OWNER, source=TRY_SOURCE, authorized=True)
    assert run.flow_version == pin


def test_a_version_that_stopped_being_active_stops_running(node):
    old = active(node)
    pin = staged(node)
    node.svc.activate(API, by=OWNER)
    assert node.runtime.get("flows").get(FLOW).version == pin
    with node.client() as client:
        r = client.post(f"/api/connectors/{API}.open_orders/run", json={**RUN, "version": old}, headers=bearer(TOKEN))
    assert r.status_code == 403
