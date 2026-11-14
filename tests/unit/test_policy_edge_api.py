"""Edge cases in the panel API, approval previews and the action log's cursors.

Bugs are marked xfail(strict=True) with a BUG number, as in test_policy_edge_authorize.py.
"""

from __future__ import annotations

import pytest
from test_node_panel import TOKEN, Node, bearer
from test_policy_support import make_service

from agentos.core.errors import ApprovalPending
from agentos.core.models import Effect
from agentos.policy.tools import human_ask, traces_query

LANE = "biz-a/instagram"


async def pending_id(coro) -> str:
    with pytest.raises(ApprovalPending) as e:
        await coro
    return e.value.approval_id


def ctx_for_token(svc, tok: str):
    p, g = svc.resolve(tok)
    return svc.context(g, principal=p, tool="t", token=tok)


def admin_agent(svc):
    """An agent that holds an admin grant (the panel's kind of grant), and its token."""
    _, tok = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling=Effect.COMMIT, admin=True)
    return ctx_for_token(svc, tok), tok


@pytest.fixture
def svc_rt(tmp_path):
    svc, rt = make_service(tmp_path)
    yield svc, rt
    svc.close()


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    yield n
    n.close()


async def test_admin_grant_cannot_approve_its_own_request(node):
    admin = node.grant(subject={"id": "claude-code"}, ceiling="commit", admin=True)
    ap = await pending_id(node.policy.context_for_token(admin, tool="t").authorize(
        Effect.COMMIT, lane_id=LANE, content="post it"))
    with node.client() as client:
        r = client.post(f"/api/approvals/{ap}/approve", headers=bearer(admin), json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
    assert node.policy.approvals.get(ap).state == "pending"


async def test_admin_grant_cannot_answer_its_own_question(node):
    admin = node.grant(subject={"id": "claude-code"}, ceiling="commit", admin=True)
    asked = await human_ask(node.policy.context_for_token(admin, tool="human_ask"),
                            question="Send the invoice?", options=["yes", "no"])
    with node.client() as client:
        r = client.post(f"/api/questions/{asked['approval_id']}/answer", headers=bearer(admin),
                        json={"answer": "yes"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
    assert node.policy.approvals.get(asked["approval_id"]).state == "pending"


async def test_long_preview_says_when_it_was_cut(svc_rt):
    svc, _ = svc_rt
    ctx, _ = admin_agent(svc)
    ap = await pending_id(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content="A" * 2100 + "EVIL"))
    text = svc.approvals.get(ap).preview or ""
    assert "EVIL" in text or "truncated" in text


async def test_short_preview_shows_the_whole_content(svc_rt):
    svc, _ = svc_rt
    ctx, _ = admin_agent(svc)
    ap = await pending_id(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content={"caption": "hello EVIL"}))
    assert "hello EVIL" in (svc.approvals.get(ap).preview or "")


async def test_action_log_cursor_never_returns_another_grants_events(svc_rt):
    svc, _ = svc_rt
    ctx_a = ctx_for_token(svc, svc.issue(subject={"id": "a"}, workspaces=["biz-a"], ceiling=Effect.READ)[1])
    ctx_b = ctx_for_token(svc, svc.issue(subject={"id": "b"}, workspaces=["biz-a"], ceiling=Effect.READ)[1])
    for i in range(3):
        ctx_a.trace("note", summary=f"a{i}")
        ctx_b.trace("note", summary=f"b{i}")
    page_a = await traces_query(ctx_a, kind="note", limit=1)
    assert page_a["next"] is not None
    # B presents A's cursor: it must only ever see B's own events, older than that cursor
    page_b = await traces_query(ctx_b, kind="note", before=page_a["next"], limit=50)
    assert page_b["events"] and all(e["grant_id"] == ctx_b.grant.id for e in page_b["events"])


async def test_a_grant_family_never_decides_its_own_requests_and_a_person_still_can(node):
    admin = node.grant(subject={"id": "claude-code"}, ceiling="commit", admin=True)
    child = node.policy.attenuate(admin, admin=True, ttl_s=600)[1]
    ctx = node.policy.context_for_token(admin, tool="t")
    ap = await pending_id(ctx.authorize(Effect.COMMIT, lane_id=LANE, content="post it"))
    ap2 = await pending_id(ctx.authorize(Effect.COMMIT, lane_id=LANE, content="again"))
    with node.client() as client:
        csrf = node.sign_in(client)
        # the requester's own child is no person either
        assert client.post(f"/api/approvals/{ap}/approve", headers=bearer(child), json={}).status_code == 403
        # a batch deny needs no person, so the family rule answers each item
        batch = client.post("/api/approvals/batch", headers=bearer(admin), json={"ids": [ap], "decision": "deny"})
        assert batch.json()["ok"] is False and "self_approval" in batch.json()["results"][0]["error"]["message"]
        assert node.policy.approvals.get(ap).state == "pending"
        # the node token is no person; the owner signed in decides the family's request, and another one
        assert client.post(f"/api/approvals/{ap}/approve", headers=bearer(TOKEN), json={}).status_code == 403
        assert client.post(f"/api/approvals/{ap}/approve", headers=csrf, json={}).status_code == 200
        assert client.post(f"/api/approvals/{ap2}/deny", headers=csrf, json={}).status_code == 200


async def test_the_node_token_cannot_approve_its_own_request_but_the_signed_in_owner_can(node):
    agent = node.policy.context_for_token(TOKEN, tool="t")  # Claude Code holding the node token (M1)
    ap = await pending_id(agent.authorize(Effect.COMMIT, lane_id=LANE, content="post it"))
    with node.client() as client:
        assert client.post(f"/api/approvals/{ap}/approve", headers=bearer(TOKEN), json={}).status_code == 403
        assert node.policy.approvals.get(ap).state == "pending"
        csrf = node.sign_in(client)
        assert client.post(f"/api/approvals/{ap}/approve", headers=csrf, json={}).status_code == 200
    assert node.policy.approvals.get(ap).state == "approved"


async def test_preview_cut_says_how_much_is_hidden(svc_rt):
    svc, _ = svc_rt
    ctx, _ = admin_agent(svc)
    ap = await pending_id(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content="B" * 5000))
    text = svc.approvals.get(ap).preview or ""
    assert len(text) <= 2000 and text.endswith("[preview truncated: 3100 more characters are not shown]")
