"""A member's routines on the node (docs/WORKFLOWS.md): a routine that publishes or commits, started by a
member who may not decide in its workspace, waits for a yes; a save by a member who may not turn a
routine on goes in but is not live until someone who may turns it on; a member may not retry one that
posts. The owner and a member who may decide run it directly."""

from __future__ import annotations

import pytest
from test_api_members import join, owner, role
from test_node_panel import Node

from openajar.core.ratelimit import RateLimiter

LANE = "biz-a/instagram"
FLOW = "biz-a.instagram.check"


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    n.api_app.state.api.limiter = RateLimiter(per_key=10_000, global_max=10_000)  # many people, one test address
    yield n
    n.close()


def posting_routine(name: str = "Post it", *, message: str = "Post") -> dict:
    return {"name": name, "workspace": "biz-a", "trigger": {"kind": "manual"},
            "steps": [{"id": "p", "kind": "action", "label": message, "operation": FLOW}]}


def reading_routine() -> dict:
    return {"name": "Look", "workspace": "biz-a", "trigger": {"kind": "manual"},
            "steps": [{"id": "n", "kind": "notify", "message": "It ran."}]}


@pytest.fixture
def posting_flow(node):
    node.runtime.flows.save({"id": FLOW, "lane": LANE, "effect": "commit",
                             "steps": [{"type": "navigate", "url": "https://www.instagram.com/"}]}, by="owner")
    node.policy.ask_first.set("flow", FLOW, False, by="owner", person=True)
    node.policy.ask_first.set("lane", LANE, False, by="owner", person=True)
    return FLOW


def test_a_member_who_may_not_decide_starts_a_posting_routine_and_it_waits_for_a_yes(node, posting_flow):
    boss = owner(node)
    wid = boss.post("/api/workflows", posting_routine()).json()["id"]
    op = join(node, boss, "op", [role("operator")])
    r = op.post(f"/api/workflows/{wid}/run", {"values": {}})
    assert r.status_code == 200, r.text
    run = node.runtime.runner.get(r.json()["id"])
    assert run.status == "waiting_approval" and run.approval_id, run
    ap = node.policy.approvals.get(run.approval_id)
    assert "op" in (ap.reason or "") and "say yes" in (ap.reason or "")
    assert op.post(f"/api/approvals/{ap.id}/approve").status_code == 403  # never their own yes
    # a member who may decide starts it directly, and so does the owner
    both = join(node, boss, "bo", [role("operator"), role("approver")])
    r = both.post(f"/api/workflows/{wid}/run", {"values": {}})
    assert r.status_code == 200 and node.runtime.runner.get(r.json()["id"]).approval_id is None, r.text
    r = boss.post(f"/api/workflows/{wid}/run", {"values": {}})
    assert r.status_code == 200 and node.runtime.runner.get(r.json()["id"]).approval_id is None, r.text


def test_a_member_may_not_retry_a_posting_routine_that_failed(node, posting_flow):
    boss = owner(node)
    wid = boss.post("/api/workflows", posting_routine()).json()["id"]
    op = join(node, boss, "op", [role("operator")])
    run_id = op.post(f"/api/workflows/{wid}/run", {"values": {}}).json()["id"]
    r = op.post(f"/api/workflows/{wid}/runs/{run_id}/retry")
    assert r.status_code == 403 and "ask first" in r.json()["error"]["message"], r.text


def test_a_read_only_routine_runs_directly_for_anyone_who_operates(node):
    boss = owner(node)
    lid = boss.post("/api/workflows", reading_routine()).json()["id"]
    op = join(node, boss, "op", [role("operator")])
    r = op.post(f"/api/workflows/{lid}/run", {"values": {}})
    assert r.status_code == 200 and node.runtime.runner.get(r.json()["id"]).approval_id is None, r.text


def test_a_member_who_may_not_turn_a_routine_on_saves_a_version_that_is_not_live(node):
    boss = owner(node)
    wid = boss.post("/api/workflows", reading_routine()).json()["id"]
    assert boss.post(f"/api/workflows/{wid}/enable", {}).status_code == 200
    live = boss.get(f"/api/workflows/{wid}").json()["live_version"]
    edited = {**reading_routine(), "steps": [{"id": "n", "kind": "notify", "message": "It ran again."}]}
    op = join(node, boss, "op", [role("operator")])
    r = op.req("PUT", f"/api/workflows/{wid}", edited)
    assert r.status_code == 200, r.text
    row = boss.get(f"/api/workflows/{wid}").json()
    assert row["version"] != live and row["live_version"] == live and row["needs_review"] is True, row
    # someone who may turn it on makes the new version live
    admin = join(node, boss, "ad", [role("admin")])
    r = admin.req("PUT", f"/api/workflows/{wid}", edited)
    assert r.status_code == 200, r.text
    row = boss.get(f"/api/workflows/{wid}").json()
    assert row["live_version"] == row["version"] and row["needs_review"] is False, row
    # the owner saves over it and it is live at once
    again = {**edited, "steps": [{"id": "n", "kind": "notify", "message": "Owner edit."}]}
    assert boss.req("PUT", f"/api/workflows/{wid}", again).status_code == 200
    row = boss.get(f"/api/workflows/{wid}").json()
    assert row["live_version"] == row["version"] and row["needs_review"] is False, row


def test_a_member_refused_an_owner_only_action_is_told_only_the_owner_can_do_it(node):
    boss = owner(node)
    op = join(node, boss, "op", [role("operator")])
    r = op.post("/api/lanes", {"id": "biz-a/newone", "kind": "browser"})  # the owner's alone
    assert r.status_code == 403, r.text
    message = r.json()["error"]["message"]
    assert message == "Only the owner of this computer can do this.", message
    assert not any(word in message for word in ("panel login", "token", "grant"))
