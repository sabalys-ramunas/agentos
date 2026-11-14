"""Authorization: scope, ceilings, specials, budgets, the routing table, approvals, taint."""

from __future__ import annotations

import asyncio
import threading

import pytest
from test_policy_support import OWNER, PRESENT, Clock, make_service

from agentos.core.errors import ApprovalPending, InvalidRequest
from agentos.core.models import Effect
from agentos.policy.approval_binding import content_digest
from agentos.policy.errors import Conflict, Denied
from agentos.policy.grants import BrainCap, InstallCap, LaneCap, ServiceCap, Specials
from agentos.policy.routing import PolicyConfig, RoutingTable, Rule, load_policy_config
from agentos.policy.service import PolicyService

ALL = Specials(secret=LaneCap(), exec=LaneCap(), install_overlay=InstallCap(sources=["npm:left-pad@1.3.0"]),
               install_base={}, service=ServiceCap(max_ttl_s=3600),
               brain=BrainCap(provider="anthropic", tier="medium", quota_tokens=1000))


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    svc, _rt = make_service(tmp_path, clock=clock)
    yield svc, clock
    svc.close()


def plan(*contents) -> dict:
    """A standing approval's binding to what the owner saw (AUD-10): tool t and these contents."""
    return {"targets": ["tool:t"], "contents": [content_digest(c) for c in contents]}


def ctx_for(svc, **kw):
    kw.setdefault("subject", {"id": "agent-1"})
    kw.setdefault("workspaces", ["biz-a"])
    kw.setdefault("ceiling", Effect.IRREVERSIBLE)
    kw.setdefault("specials", ALL)
    g, tok = svc.issue(**kw)
    p, g = svc.resolve(tok)
    return svc.context(g, principal=p, tool="t", token=tok), tok


async def pending(coro) -> str:
    with pytest.raises(ApprovalPending) as e:
        await coro
    return e.value.approval_id


async def denied(coro, reason: str | None = None) -> Denied:
    with pytest.raises(Denied) as e:
        await coro
    if reason:
        assert e.value.reason == reason, e.value
    return e.value


# ------------------------------------------------------------------ scope and ceiling

async def test_unknown_effect_fails_closed(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    await denied(ctx.authorize("teleport", lane_id="biz-a/x"), "unknown_effect")


async def test_ceiling(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, ceiling=Effect.DRAFT)
    await ctx.authorize(Effect.DRAFT, lane_id="biz-a/x")
    await denied(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x"), "ceiling")


async def test_special_not_granted(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, specials=None)
    await denied(ctx.authorize(Effect.EXEC, lane_id="biz-a/x"), "special")


@pytest.mark.parametrize("pattern,lane,ok", [
    ("biz-a/*", "biz-a/x", True),
    ("biz-a/*", "biz-ab/x", False),
    ("biz-a/insta*", "biz-a/instagram", True),
    ("biz-a/insta*", "biz-a/facebook", False),
    ("biz-*/x", "biz-b/x", True),
    ("biz-*/x", "biz-b/xy", False),
    ("*", "any/thing", True),
])
async def test_lane_wildcards_do_not_over_match(env, pattern, lane, ok):
    svc, _ = env
    ctx, _ = ctx_for(svc, workspaces=None, lanes=[pattern])
    if ok:
        await ctx.authorize(Effect.READ, lane_id=lane)
    else:
        await denied(ctx.authorize(Effect.READ, lane_id=lane), "out_of_scope")


async def test_workspace_does_not_match_prefix(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, workspaces=["biz-a"])
    await denied(ctx.authorize(Effect.READ, lane_id="biz-ab/x"), "out_of_scope")


@pytest.mark.parametrize("pattern,host,ok", [
    ("instagram.com", "instagram.com", True),
    ("instagram.com", "badinstagram.com", False),
    ("instagram.com", "www.instagram.com", False),
    ("*.instagram.com", "www.instagram.com", True),
    ("*.instagram.com", "instagram.com", False),
    ("*.instagram.com", "instagram.com.evil.io", False),
    ("instagram.com", "https://INSTAGRAM.com:443/p/1", True),
])
async def test_domain_wildcards(env, pattern, host, ok):
    svc, _ = env
    ctx, _ = ctx_for(svc, domains=[pattern])
    if ok:
        await ctx.authorize(Effect.READ, lane_id="biz-a/x", domain=host)
    else:
        await denied(ctx.authorize(Effect.READ, lane_id="biz-a/x", domain=host), "domain")


async def test_bad_lane_id_is_invalid(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    with pytest.raises(InvalidRequest):
        await ctx.authorize(Effect.READ, lane_id="Biz-A/../x")


async def test_empty_scope_allows_only_node_reads(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, workspaces=None)
    await ctx.authorize(Effect.READ)
    await denied(ctx.authorize(Effect.READ, lane_id="biz-a/x"), "out_of_scope")


async def test_lane_limited_specials(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, specials=Specials(secret=LaneCap(lanes=["biz-a/insta*"])))
    await denied(ctx.authorize(Effect.SECRET, lane_id="biz-a/facebook"), "special")
    await denied(ctx.authorize(Effect.SECRET), "special")


# ------------------------------------------------------------------ routing: read, draft, commit

async def test_read_and_draft_are_auto(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    assert (await ctx.authorize(Effect.READ, lane_id="biz-a/x")).route == "auto"
    assert (await ctx.authorize(Effect.DRAFT, lane_id="biz-a/x")).route == "auto"


async def test_commit_needs_approval_then_runs_once(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    post = {"lane_id": "biz-a/insta", "domain": "instagram.com", "summary": "Post 1 photo", "content": {"caption": "hi"}}
    ap_id = await pending(ctx.authorize(Effect.COMMIT, **post))
    assert await pending(ctx.authorize(Effect.COMMIT, **post)) == ap_id  # retries do not pile up
    ap = svc.approvals.get(ap_id)
    assert ap.state == "pending" and ap.content_hash.startswith("sha256:") and ap.summary == "Post 1 photo"
    svc.approvals.decide(ap_id, "approve", by="owner:test")
    d = await ctx.authorize(Effect.COMMIT, **post)
    assert d.route == "approved" and d.via == ap_id
    assert svc.approvals.get(ap_id).state == "consumed"
    # replaying the same call needs a new approval: single use
    assert await pending(ctx.authorize(Effect.COMMIT, **post)) != ap_id


async def test_approval_for_content_a_does_not_cover_content_b(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    a = {"lane_id": "biz-a/insta", "summary": "Post", "content": {"caption": "A"}}
    b = {"lane_id": "biz-a/insta", "summary": "Post", "content": {"caption": "B"}}
    ap_a = await pending(ctx.authorize(Effect.COMMIT, **a))
    svc.approvals.decide(ap_a, "approve", by="owner")
    ap_b = await pending(ctx.authorize(Effect.COMMIT, **b))
    assert ap_b != ap_a
    assert svc.approvals.get(ap_a).state == "approved"  # still waiting for content A
    await ctx.authorize(Effect.COMMIT, **a)


async def test_approval_is_bound_to_the_requesting_grant(env):
    svc, _ = env
    ctx1, _ = ctx_for(svc)
    ctx2, _ = ctx_for(svc)
    post = {"lane_id": "biz-a/insta", "summary": "Post", "content": "x"}
    ap = await pending(ctx1.authorize(Effect.COMMIT, **post))
    svc.approvals.decide(ap, "approve", by="owner")
    assert await pending(ctx2.authorize(Effect.COMMIT, **post)) != ap


async def test_approval_decided_twice(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    ap = await pending(ctx.authorize(Effect.PAY, lane_id="biz-a/shop", content={"usd": 5}))
    svc.approvals.decide(ap, "approve", by="owner", proof=PRESENT)
    with pytest.raises(Conflict):
        svc.approvals.decide(ap, "deny", by="owner")
    with pytest.raises(Conflict):
        svc.approvals.decide(ap, "approve", by="owner", proof=PRESENT)


async def test_concurrent_decisions_have_one_winner(env, tmp_path):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    ap = await pending(ctx.authorize(Effect.PAY, lane_id="biz-a/shop", content=1))
    others = [PolicyService(tmp_path, clock=svc.clock) for _ in range(4)]
    results = []
    barrier = threading.Barrier(4)

    def decide(s, d):
        barrier.wait()
        try:
            s.approvals.decide(ap, d, by="owner", proof=PRESENT)
            results.append(d)
        except Conflict:
            results.append("conflict")

    threads = [threading.Thread(target=decide, args=(s, d)) for s, d in zip(others, ["approve", "deny"] * 2,
                                                                          strict=True)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("conflict") == 3


async def test_denied_action_is_not_retried_into_the_queue(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    call = {"lane_id": "biz-a/x", "content": "rm everything"}
    ap = await pending(ctx.authorize(Effect.IRREVERSIBLE, **call))
    svc.approvals.decide(ap, "deny", by="owner", note="no")
    await denied(ctx.authorize(Effect.IRREVERSIBLE, **call), "approval_denied")


async def test_denial_cooldown_says_when_it_ends_and_a_scheduled_run_asks_again(env):
    """Finding 33 of 2026-11-10: a denial of a manual call refuses the same ask for a while, and says
    until when; a run its schedule started is judged on its own and asks a person again."""
    svc, _ = env
    ctx, tok = ctx_for(svc)
    call = {"lane_id": "biz-a/x", "content": "tick the box"}
    ap = await pending(ctx.authorize(Effect.IRREVERSIBLE, **call))
    svc.approvals.decide(ap, "deny", by="owner", note="no")
    e = await denied(ctx.authorize(Effect.IRREVERSIBLE, **call), "approval_denied")
    assert "not asked again until" in e.message and "in about 60 min" in e.message
    assert "—" not in e.message
    p, g = svc.resolve(tok)
    fired = svc.context(g, principal=p, tool="t", token=tok)
    fired.scheduled = True
    again = await pending(fired.authorize(Effect.IRREVERSIBLE, **call))  # asks; never runs unasked
    assert again != ap


async def test_pay_and_irreversible_always_need_approval_even_for_owner(env):
    svc, _ = env
    p, g = svc.resolve(OWNER)
    ctx = svc.context(g, principal=p, tool="t")
    svc.approvals.add_standing(effects=["commit"], workspaces=["biz-a"], until=svc.clock() + 3600, by="owner")
    await pending(ctx.authorize(Effect.PAY, lane_id="biz-a/x"))
    await pending(ctx.authorize(Effect.IRREVERSIBLE, lane_id="biz-a/x"))
    with pytest.raises(InvalidRequest):
        svc.approvals.add_standing(effects=["pay"], workspaces=["biz-a"], until=svc.clock() + 3600, by="owner")


async def test_expired_approval_cannot_be_used(env):
    svc, clock = env
    ctx, _ = ctx_for(svc)
    call = {"lane_id": "biz-a/x", "content": "c"}
    ap = await pending(ctx.authorize(Effect.COMMIT, **call))
    clock.advance(svc.cfg.approval_ttl_s + 1)
    assert svc.approvals.sweep() == 1
    assert svc.approvals.get(ap).state == "expired"
    with pytest.raises(Conflict):
        svc.approvals.decide(ap, "approve", by="owner")
    assert await pending(ctx.authorize(Effect.COMMIT, **call)) != ap


async def test_too_many_pending(tmp_path):
    svc, _ = make_service(tmp_path, config=PolicyConfig(max_pending_per_grant=2))
    ctx, _ = ctx_for(svc)
    await pending(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content=1))
    await pending(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content=2))
    await denied(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content=3), "too_many_pending")


# ------------------------------------------------------------------ standing approvals

async def test_standing_approval_exhausted_exactly_at_limit(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    sa = svc.approvals.add_standing(effects=["commit"], workspaces=["biz-a"], count=2,
                                    until=svc.clock() + 86400, by="owner", **plan(0, 1, 3))
    for i in range(2):
        d = await ctx.authorize(Effect.COMMIT, lane_id="biz-a/insta", content=i)
        assert d.route == "standing" and d.via == sa.id
    await pending(ctx.authorize(Effect.COMMIT, lane_id="biz-a/insta", content=3))
    assert svc.approvals.list_standing()[0].uses == 2


async def test_standing_approval_race_at_the_limit(env, tmp_path):
    svc, _ = env
    _g, tok = svc.issue(subject={"id": "a"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    svc.approvals.add_standing(effects=["commit"], lanes=["biz-a/*"], count=3, until=svc.clock() + 86400, by="o",
                               **plan(*range(8)))
    results = []
    barrier = threading.Barrier(8)

    def call(i):
        s = PolicyService(tmp_path, clock=svc.clock)
        p, gg = s.resolve(tok)
        ctx = s.context(gg, principal=p, tool="t")
        barrier.wait()
        try:
            asyncio.run(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content=i))
            results.append("ok")
        except ApprovalPending:
            results.append("pending")

    threads = [threading.Thread(target=call, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("ok") == 3 and results.count("pending") == 5


async def test_standing_scope_wildcards_and_grant_binding(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    other, _ = ctx_for(svc)
    svc.approvals.add_standing(effects=["commit"], lanes=["biz-a/insta*"], until=svc.clock() + 3600,
                               grant_id=ctx.grant.id, by="owner", **plan(1))
    assert (await ctx.authorize(Effect.COMMIT, lane_id="biz-a/instagram", content=1)).route == "standing"
    await pending(ctx.authorize(Effect.COMMIT, lane_id="biz-a/facebook", content=1))
    await pending(other.authorize(Effect.COMMIT, lane_id="biz-a/instagram", content=1))


async def test_standing_per_day_and_expiry(env):
    svc, clock = env
    ctx, _ = ctx_for(svc)
    svc.approvals.add_standing(effects=["commit"], workspaces=["biz-a"], per_day=1, until=clock.t + 3 * 86400,
                               by="owner", **plan(1, 2, 3, 4))
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content=1)
    await pending(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content=2))
    clock.advance(86400)
    await ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content=3)
    clock.advance(3 * 86400)
    await pending(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content=4))


async def test_standing_revoked(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    sa = svc.approvals.add_standing(effects=["commit"], workspaces=["biz-a"], until=svc.clock() + 3600, by="o")
    svc.approvals.revoke_standing(sa.id, by="o")
    await pending(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content=1))
    with pytest.raises(Conflict):
        svc.approvals.revoke_standing(sa.id, by="o")


# ------------------------------------------------------------------ specials routing

async def test_a_vault_fill_never_asks_on_any_lane_or_day(env):
    """A password in the vault is the owner's permission to use it (docs/PRINCIPLES.md, 3)."""
    svc, clock = env
    ctx, _ = ctx_for(svc)
    call = {"lane_id": "biz-a/insta", "summary": "fill the instagram login"}
    assert (await ctx.authorize(Effect.SECRET, **call)).route == "auto"
    assert (await ctx.authorize(Effect.SECRET, lane_id="biz-a/facebook")).route == "auto"
    clock.advance(86400)
    assert (await ctx.authorize(Effect.SECRET, **call)).route == "auto"
    assert svc.approvals.list(state="pending") == []


def test_the_default_table_has_no_row_that_makes_a_login_ask():
    table = RoutingTable()
    assert [r.then for r in table.rules if r.effect == "secret"] == ["auto"]
    assert "secret" not in table.first_use_period


async def test_a_grant_without_the_secret_special_is_still_refused(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, specials=Specials(exec=LaneCap()))
    with pytest.raises(Denied) as e:
        await ctx.authorize(Effect.SECRET, lane_id="biz-a/insta")
    assert e.value.reason == "special"


async def test_a_secret_limited_to_other_lanes_is_still_refused(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, specials=Specials(secret=LaneCap(lanes=["biz-a/insta*"])))
    with pytest.raises(Denied):
        await ctx.authorize(Effect.SECRET, lane_id="biz-a/facebook")


async def test_a_routing_row_for_secret_is_how_a_node_asks_at_every_login(env):
    svc, _ = env
    svc.cfg.routing.rules = [Rule(effect="secret", then="approval", note="this owner signs every login"),
                             *svc.cfg.routing.rules]
    ctx, _ = ctx_for(svc)
    await pending(ctx.authorize(Effect.SECRET, lane_id="biz-a/insta"))


async def test_exec_isolated_vs_workbench(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    iso = {"lane_id": "biz-a/sbx", "content": "ls", "facts": {"preset": "sandbox"}}
    ap = await pending(ctx.authorize(Effect.EXEC, **iso))  # first use
    svc.approvals.decide(ap, "approve", by="owner")
    await ctx.authorize(Effect.EXEC, **iso)
    assert (await ctx.authorize(Effect.EXEC, lane_id="biz-a/sbx", content="pwd",
                                facts={"preset": "sandbox"})).route == "auto"
    wb = {"lane_id": "biz-a/wb", "content": "ls", "facts": {"preset": "workbench"}}
    ap = await pending(ctx.authorize(Effect.EXEC, **wb))
    svc.approvals.decide(ap, "approve", by="owner")
    await ctx.authorize(Effect.EXEC, **wb)
    await pending(ctx.authorize(Effect.EXEC, lane_id="biz-a/wb", content="pwd", facts={"preset": "workbench"}))
    # no isolation fact at all: treated as shared
    await pending(ctx.authorize(Effect.EXEC, lane_id="biz-a/sbx", content="whoami"))


async def test_a_command_on_the_node_itself_always_waits_for_a_person(env):
    """A workbench lane can read the vault and the node token (QA 2026-11-10, finding 29): its env tools
    send `runs_on_node`, and then neither an owner's routing row nor a standing approval lets it run alone."""
    svc, _ = env
    why = "it runs on the node itself with no sandbox (workbench lane biz-a/wb). As agentos, ..."
    facts = {"isolated": False, "runs_on_node": why}
    svc.cfg.routing.rules = [Rule(effect="exec", then="auto", note="the owner turned exec on"), *svc.cfg.routing.rules]
    ctx, _ = ctx_for(svc)
    assert (await ctx.authorize(Effect.EXEC, lane_id="biz-a/sbx", content="ls",
                                facts={"isolated": True})).route == "auto"  # the row works elsewhere
    svc.approvals.add_standing(effects=["exec"], workspaces=["biz-a"], until=svc.clock() + 3600, by="owner")
    assert (await ctx.authorize(Effect.EXEC, lane_id="biz-a/shared", content="ls",
                                facts={"isolated": False})).route in ("auto", "standing")  # without the fact
    ap = await pending(ctx.authorize(Effect.EXEC, lane_id="biz-a/wb", content="cat node.toml", facts=facts))
    card = svc.approvals.get(ap)
    assert "node itself" in str(card.model_dump() if hasattr(card, "model_dump") else card)
    panel = svc.owner_context(tool="panel")  # the owner in person is that person
    assert (await panel.authorize(Effect.EXEC, lane_id="biz-a/wb", content="ls", facts=facts)).route == "auto"


@pytest.mark.parametrize("source,route", [
    ("npm:left-pad@1.3.0", "auto"),
    ("npm:left-pad@latest", "approval"),
    ("npm:left-pad", "approval"),
    ("npm:left-pad@1.3.1", "approval"),
    ("npm:evil@1.0.0", "approval"),
    (None, "approval"),
])
async def test_install_overlay(env, source, route):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    call = ctx.authorize(Effect.INSTALL_OVERLAY, lane_id="biz-a/x", facts={"source": source})
    if route == "auto":
        await call
    else:
        await pending(call)


async def test_install_base_always_approval(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    await pending(ctx.authorize(Effect.INSTALL_BASE, lane_id="biz-a/x", content="dev-box@2"))


@pytest.mark.parametrize("ttl,route", [(600, "auto"), (3600, "auto"), (3601, "approval"), (None, "approval")])
async def test_service_ttl(env, ttl, route):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    call = ctx.authorize(Effect.SERVICE, lane_id="biz-a/svc", facts={"ttl_s": ttl})
    if route == "auto":
        await call
    else:
        await pending(call)


async def test_service_ttl_cannot_outlive_grant(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, ttl_s=1800)
    await pending(ctx.authorize(Effect.SERVICE, lane_id="biz-a/svc", facts={"ttl_s": 3000}))


async def test_brain_routing(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    sub = {"provider": "anthropic", "tier": "sonnet"}
    assert (await ctx.authorize(Effect.BRAIN, cost={"tokens": 600}, facts=sub)).route == "auto"
    await pending(ctx.authorize(Effect.BRAIN, cost={"tokens": 600}, facts=sub))  # 1200 > 1000 quota
    await pending(ctx.authorize(Effect.BRAIN, facts={**sub, "metered": True}))
    await pending(ctx.authorize(Effect.BRAIN, cost={"usd": 0.01}, facts=sub))  # any money is metered
    await denied(ctx.authorize(Effect.BRAIN, facts={"provider": "openai", "tier": "small"}), "provider")
    await denied(ctx.authorize(Effect.BRAIN, facts={"provider": "anthropic", "tier": "opus"}), "tier")
    await denied(ctx.authorize(Effect.BRAIN, facts={"provider": "anthropic"}), "tier")
    await denied(ctx.authorize(Effect.BRAIN, facts={"provider": "anthropic", "tier": "galaxy-brain"}), "tier")


async def test_brain_quota_resets_daily(env):
    svc, clock = env
    ctx, _ = ctx_for(svc)
    sub = {"provider": "anthropic", "tier": "small"}
    await ctx.authorize(Effect.BRAIN, cost={"tokens": 1000}, facts=sub)
    await pending(ctx.authorize(Effect.BRAIN, facts=sub))
    clock.advance(86400)
    await ctx.authorize(Effect.BRAIN, cost={"tokens": 10}, facts=sub)


# ------------------------------------------------------------------ budgets

async def test_budget_is_reserved_and_exhausts(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, budget={"usd": 1.0, "tokens": 100})
    await ctx.authorize(Effect.DRAFT, lane_id="biz-a/x", cost={"usd": 0.6, "tokens": 60})
    await denied(ctx.authorize(Effect.DRAFT, lane_id="biz-a/x", cost={"usd": 0.5}), "budget")
    await ctx.authorize(Effect.DRAFT, lane_id="biz-a/x", cost={"usd": 0.4, "tokens": 40})
    await denied(ctx.authorize(Effect.READ, lane_id="biz-a/x"), "budget")  # spent
    assert svc.remaining(ctx.grant) == {"tokens": 0, "usd": 0.0}


async def test_budget_refunded_when_call_waits_for_approval(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, budget={"usd": 1.0})
    await pending(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", cost={"usd": 0.9}, content=1))
    assert svc.remaining(ctx.grant)["usd"] == 1.0


async def test_child_spend_counts_against_parent(env):
    svc, _ = env
    _g, tok = svc.issue(subject={"id": "p"}, workspaces=["biz-a"], ceiling=Effect.DRAFT, budget={"usd": 1.0})
    _, c1 = svc.attenuate(tok, budget={"usd": 0.8})
    _, c2 = svc.attenuate(tok, budget={"usd": 0.8})
    ctx1 = svc.context(svc.resolve(c1)[1], tool="t")
    ctx2 = svc.context(svc.resolve(c2)[1], tool="t")
    await ctx1.authorize(Effect.DRAFT, lane_id="biz-a/x", cost={"usd": 0.7})
    await denied(ctx2.authorize(Effect.DRAFT, lane_id="biz-a/x", cost={"usd": 0.7}), "budget")
    await ctx2.authorize(Effect.DRAFT, lane_id="biz-a/x", cost={"usd": 0.3})


def test_budget_race_between_two_processes(tmp_path):
    svc, _ = make_service(tmp_path)
    _g, tok = svc.issue(subject={"id": "p"}, workspaces=["biz-a"], ceiling=Effect.DRAFT, budget={"usd": 1.0})
    results: list[str] = []

    def call(barrier: threading.Barrier) -> None:
        s = PolicyService(tmp_path, clock=svc.clock)
        ctx = s.context(s.resolve(tok)[1], tool="t")
        barrier.wait()
        try:
            asyncio.run(ctx.authorize(Effect.DRAFT, lane_id="biz-a/x", cost={"usd": 0.15}))
            results.append("ok")
        except Denied:
            results.append("denied")

    for _ in range(5):  # five rounds of two separate processes racing near the limit
        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=call, args=(barrier,)) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    # 1.00 / 0.15: six fit, the seventh and later never do, even when racing
    assert results.count("ok") == 6 and results.count("denied") == 4
    _, g2 = svc.resolve(tok)
    assert round(svc.remaining(g2)["usd"], 6) == 0.1


async def test_charge_after_the_fact(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, budget={"tokens": 100})
    await ctx.charge(tokens=150)  # spend happened; recorded even past the limit
    await denied(ctx.authorize(Effect.READ, lane_id="biz-a/x"), "budget")
    with pytest.raises(Denied):
        await ctx.charge(tokens=1, strict=True)


async def test_negative_or_bad_cost(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    with pytest.raises(InvalidRequest):
        await ctx.authorize(Effect.READ, cost={"usd": -1})
    with pytest.raises(InvalidRequest):
        await ctx.authorize(Effect.READ, cost={"usd": float("nan")})


# ------------------------------------------------------------------ revocation mid-call

async def test_parent_revoked_mid_call(env):
    svc, _ = env
    g, tok = svc.issue(subject={"id": "p"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    _, child = svc.attenuate(tok, ceiling=Effect.DRAFT)
    ctx = svc.context(svc.resolve(child)[1], tool="t")
    await ctx.authorize(Effect.READ, lane_id="biz-a/x")
    svc.revoke(g.id)  # the human revokes the parent while the call is running
    await denied(ctx.authorize(Effect.DRAFT, lane_id="biz-a/x"), "revoked")


async def test_grant_expires_mid_call(env):
    svc, clock = env
    ctx, _ = ctx_for(svc, ttl_s=60)
    await ctx.authorize(Effect.READ, lane_id="biz-a/x")
    clock.advance(60 + svc.cfg.clock_skew_s + 1)
    await denied(ctx.authorize(Effect.READ, lane_id="biz-a/x"), "expired")


# ------------------------------------------------------------------ taint: the lethal trifecta

async def test_trifecta_requires_approval_outside_egress(env):
    svc, _ = env
    ctx, _ = ctx_for(svc, egress=["instagram.com"])
    svc.approvals.add_standing(effects=["commit"], workspaces=["biz-a"], until=svc.clock() + 3600, by="owner",
                               **plan("private notes", "x"))
    post = {"lane_id": "biz-a/insta", "domain": "evil.example", "content": "private notes"}
    assert (await ctx.authorize(Effect.COMMIT, **post)).route == "standing"  # clean session
    ctx.trace("web_content", url="https://example.com/post")
    ctx.trace("private_data", what="inbox")
    assert svc.taint.get(ctx.session_key) == {"untrusted", "private"}
    ap = await pending(ctx.authorize(Effect.COMMIT, **post))  # standing does not cover it
    assert "outside its allowed list" in svc.approvals.get(ap).reason
    # inside the egress allowlist the standing approval applies again
    assert (await ctx.authorize(Effect.COMMIT, lane_id="biz-a/insta", domain="instagram.com",
                                content="x")).route == "standing"
    # draft is a sink too
    await pending(ctx.authorize(Effect.DRAFT, lane_id="biz-a/insta", domain="evil.example", content="y"))
    # reads stay free
    await ctx.authorize(Effect.READ, lane_id="biz-a/insta", domain="evil.example")


async def test_trifecta_needs_both_labels_and_explicit_api(env):
    svc, _ = env
    ctx, _ = ctx_for(svc)
    ctx.taint("untrusted")
    await ctx.authorize(Effect.DRAFT, lane_id="biz-a/x", domain="a.example")
    ctx.trace("custom", taint=["private"])
    await pending(ctx.authorize(Effect.DRAFT, lane_id="biz-a/x", domain="a.example"))
    with pytest.raises(InvalidRequest):
        ctx.taint("radioactive")


async def test_taint_is_per_grant_family_and_expires(env):
    svc, clock = env
    ctx, _ = ctx_for(svc)
    ctx.taint("untrusted", "private")
    other = svc.context(ctx.grant, principal=ctx.principal, tool="t")
    other.meta.session = "another-mcp-session"  # a reconnect does not clear the family's taint (SEC-4)
    await pending(other.authorize(Effect.DRAFT, lane_id="biz-a/x", domain="a.example"))
    stranger, _ = ctx_for(svc, subject={"id": "agent-2"})  # another family starts clean
    await stranger.authorize(Effect.DRAFT, lane_id="biz-a/x", domain="a.example")
    clock.advance(svc.cfg.session_ttl_s + 1)
    await ctx.authorize(Effect.DRAFT, lane_id="biz-a/x", domain="b.example")


async def test_the_owner_in_person_is_never_held_by_the_agents_taint(env):
    svc, _ = env
    agent = svc.context_for_token(OWNER, tool="t")  # the node token, as an agent uses it in M1
    agent.taint("untrusted", "private")
    await pending(agent.authorize(Effect.DRAFT, lane_id="biz-a/x", domain="a.example"))
    panel = svc.owner_context(tool="panel")  # the owner signed in to the panel
    panel.taint("private")
    assert (await panel.authorize(Effect.DRAFT, lane_id="biz-a/x", domain="a.example")).route == "auto"


# ------------------------------------------------------------------ the routing table is data

def test_routing_overrides_from_policy_toml(tmp_path):
    (tmp_path / "policy.toml").write_text(
        '[routing]\nrules = [{effect = "draft", then = "approval", note = "be careful"},'
        ' {effect = "read", then = "auto"}]\n')
    cfg = load_policy_config(tmp_path)
    assert cfg.routing.decide(Effect.DRAFT, {}).then == "approval"
    assert cfg.routing.decide(Effect.COMMIT, {}).then == "approval"  # no rule: fail closed


def test_routing_rejects_unknown_facts_and_standing_pay():
    with pytest.raises(ValueError):
        Rule(effect="exec", when={"vibes": True}, then="auto")
    with pytest.raises(ValueError):
        RoutingTable(standing_effects=["pay"])
    with pytest.raises(ValueError):
        Rule(effect="teleport", then="auto")


async def test_routing_deny_rule(tmp_path):
    rules = [Rule(effect="exec", then="deny", note="no shells here"), Rule(effect="read", then="auto")]
    svc, _ = make_service(tmp_path, config=PolicyConfig(routing=RoutingTable(rules=rules)))
    ctx, _ = ctx_for(svc)
    await denied(ctx.authorize(Effect.EXEC, lane_id="biz-a/x"), "routing")


def test_routing_render_has_no_em_dash():
    text = RoutingTable().render()
    assert "commit" in text and "\u2014" not in text


async def test_vm_always_asks_and_a_grant_limits_profiles_and_lanes(env):
    from agentos.policy.grants import VmCap
    svc, _ = env
    ctx, _ = ctx_for(svc, specials=Specials(vm=VmCap(lanes=["biz-a/build-*"], profiles=["small"])))
    await pending(ctx.authorize(Effect.VM, lane_id="biz-a/build-1", content={"create": 1},
                                facts={"profile": "small"}))
    with pytest.raises(Denied):
        await ctx.authorize(Effect.VM, lane_id="biz-a/build-1", content={"create": 2}, facts={"profile": "build"})
    with pytest.raises(Denied):
        await ctx.authorize(Effect.VM, lane_id="biz-a/other", content={"create": 3}, facts={"profile": "small"})
    plain, _ = ctx_for(svc, specials=Specials(exec=LaneCap()))
    with pytest.raises(Denied):
        await plain.authorize(Effect.VM, lane_id="biz-a/build-1", content={"create": 4}, facts={"profile": "small"})
    repos, _ = ctx_for(svc, specials=Specials(vm=VmCap(profiles=["small"], repos=["github.com/acme/*"])))
    await pending(repos.authorize(Effect.VM, lane_id="biz-a/build-1", content={"clone": 1},
                                  facts={"repo": "github.com/acme/api"}))  # a clone states no profile
    with pytest.raises(Denied):
        await repos.authorize(Effect.VM, lane_id="biz-a/build-1", content={"clone": 2},
                              facts={"repo": "github.com/other/api"})
    with pytest.raises(Denied):  # a size of the caller's own is not one of the profiles
        await repos.authorize(Effect.VM, lane_id="biz-a/build-1", content={"create": 5}, facts={"profile": "custom"})
