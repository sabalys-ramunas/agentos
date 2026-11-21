"""Routing for workspace memory (docs/design/workspace-memory.md 4.7, 4.8): internal writes are not
trifecta sinks, owner-trust writes always wait for a one-off approval bound to their content."""

from __future__ import annotations

import pytest
from test_policy_support import Clock, make_service

from openajar.core.errors import ApprovalPending
from openajar.core.models import Effect
from openajar.policy.errors import Denied
from openajar.policy.grants import Specials
from openajar.policy.memory_rules import MemoryCap
from openajar.policy.routing import FACTS, PolicyConfig, RoutingTable, Rule

LANE = "biz-a/instagram"
DECISION = {"type": "decision", "title": "No discounts in posts", "body": "biz-a never offers discounts."}
OWNER_TRUST = {"memory_owner_trust": True, "internal": True}


@pytest.fixture
def svc(tmp_path):
    s, _ = make_service(tmp_path, clock=Clock())
    yield s
    s.close()


def agent(svc, **kw):
    kw.setdefault("egress", ["instagram.com"])
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE,
                       specials=Specials(memory=MemoryCap(write=True)), **kw)
    return svc.context_for_token(tok, tool="memory_write"), tok


def taint_both(ctx) -> None:
    ctx.taint("untrusted", "private")


async def test_an_internal_write_skips_the_trifecta_while_a_navigation_still_waits(svc):
    ctx, _ = agent(svc)
    taint_both(ctx)
    d = await ctx.authorize(Effect.DRAFT, summary="write a memory note", content={"title": "r/x"},
                            facts={"internal": True})
    assert d.route == "auto"
    nav = svc.context_for_token(ctx.scrub[0], tool="browser_navigate", lane_id=LANE)
    with pytest.raises(ApprovalPending):
        await nav.authorize(Effect.READ, lane_id=LANE, domain="evil.example", facts={"sink": True})
    with pytest.raises(ApprovalPending):  # the same draft without `internal` is a sink
        await ctx.authorize(Effect.DRAFT, summary="write a memory note", content={"title": "r/x"})


async def test_internal_with_a_domain_is_not_internal(svc):
    ctx, _ = agent(svc)
    taint_both(ctx)
    with pytest.raises(ApprovalPending):
        await ctx.authorize(Effect.DRAFT, domain="evil.example", content={"q": "inbox"}, facts={"internal": True})


async def test_internal_only_counts_when_true(svc):
    ctx, _ = agent(svc)
    taint_both(ctx)
    with pytest.raises(ApprovalPending):
        await ctx.authorize(Effect.DRAFT, content={"title": "x"}, facts={"internal": "yes"})


async def test_an_owner_trust_write_waits_bound_to_its_content(svc):
    ctx, tok = agent(svc)
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.DRAFT, summary="claude-code wants to record a decision for biz-a",
                            content=DECISION, facts=OWNER_TRUST)
    ap = svc.approvals.get(e.value.approval_id)
    assert ap.effect == "draft" and "No discounts" in (ap.preview or "")
    # the requesting family cannot decide it, even with an admin grant of the same root
    with pytest.raises(Denied) as d:
        svc.approvals.decide(ap.id, "approve", by="agent-1", refuse_family=ctx.grant.root_id)
    assert d.value.reason == "self_approval"
    svc.approvals.decide(ap.id, "approve", by="owner")
    # other text is a new approval; the approved text is consumed once
    with pytest.raises(ApprovalPending):
        await ctx.authorize(Effect.DRAFT, summary="claude-code wants to record a decision for biz-a",
                            content={**DECISION, "body": "discounts are fine"}, facts=OWNER_TRUST)
    again = svc.context_for_token(tok, tool="memory_write")
    d2 = await again.authorize(Effect.DRAFT, summary="claude-code wants to record a decision for biz-a",
                               content=DECISION, facts=OWNER_TRUST)
    assert d2.route == "approved" and d2.via == ap.id


async def test_a_denied_proposal_is_refused_again_in_the_cooldown(svc):
    ctx, _ = agent(svc)
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.DRAFT, content=DECISION, facts=OWNER_TRUST)
    svc.approvals.decide(e.value.approval_id, "deny", by="owner")
    with pytest.raises(Denied) as d:
        await ctx.authorize(Effect.DRAFT, content=DECISION, facts=OWNER_TRUST)
    assert d.value.reason == "approval_denied"


async def test_a_standing_approval_never_covers_an_owner_trust_write(tmp_path):
    rules = [Rule(effect="draft", then="approval", note="review every draft")]
    cfg = PolicyConfig(routing=RoutingTable(rules=rules, standing_effects=["commit", "draft"]))
    svc, _ = make_service(tmp_path, clock=Clock(), config=cfg)
    try:
        svc.approvals.add_standing(effects=["draft"], workspaces=["biz-a"], until=svc.clock() + 3600, by="owner")
        ctx, _ = agent(svc)
        note = await ctx.authorize(Effect.DRAFT, lane_id=LANE, content={"title": "a note"}, facts={"internal": True})
        assert note.route == "standing"   # an ordinary memory note is covered
        with pytest.raises(ApprovalPending):
            await ctx.authorize(Effect.DRAFT, lane_id=LANE, content=DECISION, facts=OWNER_TRUST)
    finally:
        svc.close()


async def test_a_replaced_table_that_says_auto_still_waits(tmp_path):
    cfg = PolicyConfig(routing=RoutingTable(rules=[Rule(effect="*", then="auto")]))
    svc, _ = make_service(tmp_path, clock=Clock(), config=cfg)
    try:
        ctx, _ = agent(svc)
        assert (await ctx.authorize(Effect.DRAFT, content={"title": "a note"}, facts={"internal": True})).route == "auto"
        with pytest.raises(ApprovalPending):
            await ctx.authorize(Effect.DRAFT, content=DECISION, facts=OWNER_TRUST)
    finally:
        svc.close()


async def test_a_deny_rule_on_the_fact_still_denies(tmp_path):
    rules = [Rule(effect="draft", when={"memory_owner_trust": True}, then="deny", note="no proposals"),
             Rule(effect="*", then="auto")]
    svc, _ = make_service(tmp_path, clock=Clock(), config=PolicyConfig(routing=RoutingTable(rules=rules)))
    try:
        ctx, _ = agent(svc)
        with pytest.raises(Denied) as e:
            await ctx.authorize(Effect.DRAFT, content=DECISION, facts=OWNER_TRUST)
        assert e.value.reason == "routing"
    finally:
        svc.close()


async def test_forgetting_always_waits(svc):
    ctx, _ = agent(svc)
    with pytest.raises(ApprovalPending):
        await ctx.authorize(Effect.IRREVERSIBLE, content={"forget": "mem_x"}, facts={"internal": True})


def test_the_facts_load_from_toml_and_print():
    assert {"memory_owner_trust", "internal"} <= set(FACTS)
    table = RoutingTable.model_validate({"rules": [
        {"effect": "draft", "when": {"memory_owner_trust": True}, "then": "approval"},
        {"effect": "draft", "when": {"internal": True}, "then": "auto"}]})
    assert table.rules[0].when == {"memory_owner_trust": True}
    text = RoutingTable().render()
    assert "memory_owner_trust=yes" in text and "internal destinations" in text and "\u2014" not in text
    first_draft = next(r for r in RoutingTable().rules if r.effect == "draft")
    assert first_draft.when == {"memory_owner_trust": True} and first_draft.then == "approval"
