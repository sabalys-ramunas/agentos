"""Edge cases in authorization: approval binding, taint across grants, routing facts, budgets.

Bugs found by QA are marked xfail(strict=True) with a BUG number; the test passes once the bug is fixed
and the marker must then be removed.
"""

from __future__ import annotations

import pytest
from test_policy_support import PRESENT, Clock, make_service

from openajar.core.errors import ApprovalPending
from openajar.core.models import Effect
from openajar.policy.approval_binding import content_digest
from openajar.policy.context import PolicyContext
from openajar.policy.errors import Denied
from openajar.policy.grants import BrainCap, InstallCap, LaneCap, ServiceCap, Specials

ALL = Specials(secret=LaneCap(), exec=LaneCap(), install_overlay=InstallCap(sources=["npm:left-pad@1.3.0"]),
               install_base={}, service=ServiceCap(max_ttl_s=3600),
               brain=BrainCap(provider="anthropic", tier="medium", quota_tokens=1000))


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    svc, _rt = make_service(tmp_path, clock=clock)
    yield svc, clock
    svc.close()


def ctx_for(svc, **kw) -> tuple[PolicyContext, str]:
    kw.setdefault("subject", {"id": "agent-1"})
    kw.setdefault("workspaces", ["biz-a"])
    kw.setdefault("ceiling", Effect.IRREVERSIBLE)
    kw.setdefault("specials", ALL)
    g, tok = svc.issue(**kw)
    p, g = svc.resolve(tok)
    return svc.context(g, principal=p, tool="t", token=tok), tok


def ctx_from_token(svc, tok: str) -> PolicyContext:
    p, g = svc.resolve(tok)
    return svc.context(g, principal=p, tool="t", token=tok)


async def pending(coro) -> str:
    with pytest.raises(ApprovalPending) as e:
        await coro
    return e.value.approval_id


async def denied(coro, reason: str) -> Denied:
    with pytest.raises(Denied) as e:
        await coro
    assert e.value.reason == reason, e.value
    return e.value


# ------------------------------------------------------------------ approvals bind to what will happen

async def test_approval_does_not_cover_a_different_cost(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, specials=None, budget={"usd": 1000})
    ap = await pending(ctx.authorize(Effect.PAY, lane_id="biz-a/shop", content="order-1", cost={"usd": 5}))
    svc.approvals.decide(ap, "approve", by="owner", proof=PRESENT)
    await pending(ctx.authorize(Effect.PAY, lane_id="biz-a/shop", content="order-1", cost={"usd": 500}))


async def test_a_derived_grant_cannot_re_ask_about_a_denied_action(env):
    svc, _ = env
    ctx, tok = ctx_for(svc, ceiling=Effect.COMMIT)
    ap = await pending(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content="spam"))
    svc.approvals.decide(ap, "deny", by="owner")
    child = ctx_from_token(svc, svc.attenuate(tok)[1])
    await denied(child.authorize(Effect.COMMIT, lane_id="biz-a/x", content="spam"), "approval_denied")


# ------------------------------------------------------------------ the lethal trifecta follows the work

async def test_derived_grant_inherits_taint_from_its_parent(env):
    svc, _ = env
    ctx, tok = ctx_for(svc, egress=["instagram.com"])
    ctx.trace("web_content", url="https://example.com/offer")
    ctx.trace("private_data", what="inbox")
    child = ctx_from_token(svc, svc.attenuate(tok, workspaces=["biz-a"])[1])
    await pending(child.authorize(Effect.DRAFT, lane_id="biz-a/x", domain="evil.example", content="notes"))


async def test_trifecta_applies_to_calls_that_name_no_domain(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, ceiling=Effect.COMMIT)
    svc.approvals.add_standing(effects=["commit"], workspaces=["biz-a"], until=svc.clock() + 3600, by="owner",
                               targets=["tool:t"], contents=[content_digest(1), content_digest(2)])
    assert (await ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content=1)).route == "standing"
    ctx.trace("web_content", url="https://example.com")
    ctx.trace("private_data", what="inbox")
    # no domain means "outside the egress allowlist", so the standing approval does not apply
    await pending(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content=2))


# ------------------------------------------------------------------ routing facts fail closed

@pytest.mark.parametrize("effect,facts", [
    (Effect.EXEC, {}),                                       # isolation not stated: not isolated
    (Effect.INSTALL_OVERLAY, {}),                            # no source: not allowlisted
    (Effect.INSTALL_OVERLAY, {"source": "npm:left-pad@latest"}),  # unpinned
    (Effect.SERVICE, {}),                                    # no TTL: not within the grant
    (Effect.SERVICE, {"ttl_s": 999_999}),                    # TTL past max_ttl_s
])
async def test_unstated_routing_facts_never_auto_route(env, effect, facts):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    await pending(ctx.authorize(effect, lane_id="biz-a/x", facts=facts))


async def test_service_ports_must_be_stated_when_the_grant_limits_them(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, specials=Specials(service=ServiceCap(max_ttl_s=3600, ports=[8080])), ceiling=Effect.READ)
    await denied(ctx.authorize(Effect.SERVICE, lane_id="biz-a/svc", facts={"ttl_s": 60}), "special")


async def test_stated_service_ports_are_checked_against_the_grant(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, specials=Specials(service=ServiceCap(max_ttl_s=3600, ports=[8080])), ceiling=Effect.READ)
    await denied(ctx.authorize(Effect.SERVICE, lane_id="biz-a/svc", facts={"ttl_s": 60, "ports": [22]}), "special")
    assert (await ctx.authorize(Effect.SERVICE, lane_id="biz-a/svc", facts={"ttl_s": 60, "ports": [8080]})
            ).route == "auto"


# ------------------------------------------------------------------ standing approvals

async def test_standing_workspace_patterns_match_like_grant_patterns(env):
    svc, _ = env
    svc.approvals.add_standing(effects=["commit"], workspaces=["biz-*"], until=svc.clock() + 3600, by="owner",
                               targets=["tool:t"], contents=[content_digest(1)])
    ctx, _ = ctx_for(svc, ceiling=Effect.COMMIT)
    assert (await ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content=1)).route == "standing"


# ------------------------------------------------------------------ budgets

async def test_sub_micro_dollar_costs_are_still_debited(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, specials=None, budget={"usd": 1.0})
    await ctx.authorize(Effect.DRAFT, lane_id="biz-a/x", cost={"usd": 4e-7})
    assert svc.remaining(ctx.grant)["usd"] < 1.0


@pytest.mark.parametrize("cost", [{"usd": float("inf")}, {"usd": float("nan")}, {"tokens": float("inf")},
                                  {"usd": 1e300}, {"tokens": 10**30}])
async def test_absurd_costs_are_refused_cleanly(env, cost):
    from openajar.core.errors import InvalidRequest

    svc, _ = env
    ctx, _ = ctx_for(svc, specials=None, budget={"usd": 1.0, "tokens": 100})
    with pytest.raises(InvalidRequest):
        await ctx.authorize(Effect.DRAFT, lane_id="biz-a/x", cost=cost)
    with pytest.raises(InvalidRequest):
        await ctx.charge(usd=cost.get("usd", 0.0), tokens=cost.get("tokens", 0))
    assert svc.remaining(ctx.grant) == {"usd": 1.0, "tokens": 100}


@pytest.mark.parametrize("budget", [{"usd": float("inf")}, {"usd": float("nan")}])
def test_an_infinite_budget_is_refused_when_issued(env, budget):
    from openajar.core.errors import InvalidRequest

    svc, _ = env
    with pytest.raises(InvalidRequest):
        svc.issue(subject={"id": "a"}, workspaces=["biz-a"], budget=budget)


async def test_round_costs_are_debited_exactly(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, specials=None, budget={"usd": 1.0})
    for _ in range(10):
        await ctx.authorize(Effect.DRAFT, lane_id="biz-a/x", cost={"usd": 0.1})
    assert svc.remaining(ctx.grant)["usd"] == 0.0  # 0.1 is 100000 micro-dollars, never 100001
