"""The policy side of durable runs: a ToolContext for a stored grant id, and one approval for one run."""

from __future__ import annotations

import pytest
from test_policy_support import Clock, make_service

from agentos.core.errors import ApprovalPending
from agentos.core.models import Effect, Principal
from agentos.policy.errors import Denied
from agentos.policy.grants import OWNER_GRANT_ID


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    svc, _ = make_service(tmp_path, clock=clock)
    yield svc, clock
    svc.close()


def reason(fn, *args, **kw) -> str:
    with pytest.raises(Denied) as e:
        fn(*args, **kw)
    return e.value.reason


async def test_a_stored_grant_id_gives_a_context_with_the_grants_scope(env):
    svc, _ = env
    g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    child, _ = svc.attenuate(tok, lanes=["biz-a/social"])
    ctx = svc.context_for_grant(child.id, tool="flow_run")
    assert ctx.grant.id == child.id and ctx.grant.chain_ids == [g.id, child.id]
    assert ctx.principal.id == "agent-1" and ctx.principal.grant_id == child.id
    assert (await ctx.authorize(Effect.DRAFT, lane_id="biz-a/social")).route == "auto"
    with pytest.raises(Denied) as e:
        await ctx.authorize(Effect.READ, lane_id="biz-a/other")
    assert e.value.reason == "out_of_scope"
    with pytest.raises(Denied) as e:
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/social")
    assert e.value.reason == "ceiling"
    named = svc.context_for_grant(g.id, principal=Principal(kind="flow", id="schedule:s1"))
    assert named.principal.id == "schedule:s1" and named.grant.id == g.id


def test_unknown_revoked_and_expired_grants_are_refused(env):
    svc, clock = env
    assert reason(svc.context_for_grant, None) == "unknown_grant"
    assert reason(svc.context_for_grant, "gr_nope") == "unknown_grant"
    g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ttl_s=600)
    child, _ = svc.attenuate(tok, lanes=["biz-a/social"])
    svc.revoke(g.id)
    assert reason(svc.context_for_grant, g.id) == "revoked"
    assert reason(svc.context_for_grant, child.id) == "revoked"  # revoking a parent cascades
    other, _ = svc.issue(subject={"id": "agent-2"}, workspaces=["biz-a"], ttl_s=600)
    clock.advance(601 + svc.cfg.clock_skew_s)
    assert reason(svc.context_for_grant, other.id) == "expired"


async def test_the_owner_grant_id_is_the_node_owner(env):
    svc, _ = env
    ctx = svc.context_for_grant(OWNER_GRANT_ID, tool="flow_run")
    assert ctx.grant.owner_token and ctx.principal.id == "owner" and ctx.principal.kind == "human"
    with pytest.raises(ApprovalPending):  # the owner still co-signs a commit through routing
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/social", content="post")


async def test_one_approval_is_consumed_by_exactly_one_caller(env):
    svc, _ = env
    ctx = svc.context_for_grant(OWNER_GRANT_ID, tool="flow_run")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/social", content="post")
    ap = e.value.approval_id
    assert svc.approvals.consume(ap) is False  # still pending
    svc.approvals.decide(ap, "approve", by="owner")
    assert svc.approvals.consume(ap) is True
    assert svc.approvals.consume(ap) is False
    assert svc.approvals.get(ap).state == "consumed"
    with pytest.raises(ApprovalPending) as again:  # the identical call asks anew
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/social", content="post")
    assert again.value.approval_id != ap


async def test_a_consumed_approval_is_not_reused_by_a_retried_call(env):
    svc, _ = env
    ctx = svc.context_for_grant(OWNER_GRANT_ID, tool="flow_run")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/social", content="post")
    svc.approvals.decide(e.value.approval_id, "approve", by="owner")
    assert (await ctx.authorize(Effect.COMMIT, lane_id="biz-a/social", content="post")).route == "approved"
    assert svc.approvals.consume(e.value.approval_id) is False  # the retried call used it


async def test_an_approval_binds_the_routing_facts(env):
    svc, _ = env
    ctx = svc.context_for_grant(OWNER_GRANT_ID, tool="t")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.EXEC, lane_id="biz-a/box", content="ls", facts={"preset": "workbench"})
    svc.approvals.decide(e.value.approval_id, "approve", by="owner")
    with pytest.raises(ApprovalPending):
        await ctx.authorize(Effect.EXEC, lane_id="biz-a/box", content="ls", facts={"preset": "workbench", "x": 1})
    assert (await ctx.authorize(Effect.EXEC, lane_id="biz-a/box", content="ls",
                                facts={"preset": "workbench"})).route == "approved"
