"""The router is pure: every rule tested on its own."""

from agentos.brains.models import (
    BrainKind,
    BrainState,
    BrainView,
    Budget,
    Capabilities,
    GrantView,
    Locality,
    Privacy,
    Requirements,
)
from agentos.brains.router import route


def view(name, kind, *, state=BrainState.READY, seats=1, used=0, ctx=200_000, vision=True, tier=3, order=100,
         saturated=False, reason=None, enabled=True):
    return BrainView(name=name, kind=BrainKind(kind),
                     locality=Locality.LOCAL if kind == "local" else Locality.CLOUD,
                     state=state, state_reason=reason, caps=Capabilities(context_tokens=ctx, vision=vision, tier=tier),
                     order=order, seats_total=seats, seats_in_use=used, window_saturated=saturated, enabled=enabled)


SUB = view("claude-max", "subscription")
LOCAL = view("ollama", "local", ctx=32_000, vision=False, tier=1)
METERED = view("anthropic-api", "metered")
ALL = [METERED, LOCAL, SUB]
REQ = Requirements()
FULL = Privacy.FULL_CLOUD
OK_GRANT = GrantView(allow_metered=True)


def names(d):
    return [c.name for c in d.candidates]


def test_rank_subscription_then_local_then_metered():
    d = route(REQ, privacy=FULL, grant=OK_GRANT, brains=ALL, budget=Budget(max_usd=1))
    assert d.action == "run"
    assert names(d) == ["claude-max", "ollama", "anthropic-api"]
    assert "subscription" in d.reason


def test_owner_order_within_a_kind():
    a = view("sub-a", "subscription", order=20)
    b = view("sub-b", "subscription", order=10)
    assert names(route(REQ, privacy=FULL, brains=[a, b]))[:2] == ["sub-b", "sub-a"]


def test_local_only_never_uses_cloud_even_when_cloud_is_free():
    d = route(REQ, privacy=Privacy.LOCAL_ONLY, grant=OK_GRANT, brains=ALL, budget=Budget(max_usd=5))
    assert names(d) == ["ollama"]
    assert {r.name for r in d.rejected} == {"claude-max", "anthropic-api"}
    assert all("never used" in r.reason for r in d.rejected)


def test_local_only_with_no_local_brain_queues_and_never_offers_cloud():
    d = route(REQ, privacy=Privacy.LOCAL_ONLY, grant=OK_GRANT, brains=[SUB, METERED], budget=Budget(max_usd=5))
    assert d.action == "queue" and d.candidates == [] and d.approval is None
    assert "local-only" in d.reason and "no local brain" in d.reason


def test_local_only_with_local_brain_down_queues():
    down = view("ollama", "local", state=BrainState.DOWN, reason="model server not answering")
    d = route(REQ, privacy=Privacy.LOCAL_ONLY, brains=[SUB, down])
    assert d.action == "queue" and "no local brain is ready" in d.reason


def test_private_taint_goes_local():
    d = route(REQ, privacy=FULL, taint={"private"}, grant=OK_GRANT, brains=ALL, budget=Budget(max_usd=1))
    assert names(d) == ["ollama"]
    assert any(r.approvable == "private_cloud" for r in d.rejected)


def test_private_taint_without_local_needs_a_cosign():
    d = route(REQ, privacy=FULL, taint={"private", "untrusted"}, brains=[SUB])
    assert d.action == "needs_approval"
    assert d.approval == {"kind": "private_cloud", "brains": ["claude-max"]}
    assert "untrusted" in d.rejected[0].reason  # trifecta named


def test_private_taint_after_cosign_may_use_cloud():
    named = SUB.model_copy(update={"identity": "claude-max:1"})  # the co-sign names the brain it covers
    grant = GrantView(allow_private_cloud=True, private_cloud_brains=["claude-max:1"])
    assert names(route(REQ, privacy=FULL, taint={"private"}, grant=grant, brains=[named])) == ["claude-max"]


def test_untrusted_alone_does_not_force_local():
    assert names(route(REQ, privacy=FULL, taint={"untrusted"}, brains=[SUB]))[0] == "claude-max"


def test_redacted_cloud_marks_cloud_candidates_for_the_redactor():
    d = route(REQ, privacy=Privacy.REDACTED_CLOUD, brains=[SUB, LOCAL])
    by = {c.name: c for c in d.candidates}
    assert by["claude-max"].redact is True and by["ollama"].redact is False


def test_internet_down_means_local_only():
    d = route(REQ, privacy=FULL, grant=OK_GRANT, brains=ALL, budget=Budget(max_usd=1), online=False)
    assert names(d) == ["ollama"]
    d2 = route(REQ, privacy=FULL, brains=[SUB, METERED], online=False)
    assert d2.action == "queue" and "internet is down" in d2.reason


def test_capability_filter():
    d = route(Requirements(min_context=100_000, vision=True), privacy=FULL, brains=[SUB, LOCAL])
    assert names(d) == ["claude-max"]
    assert "context" in next(r.reason for r in d.rejected if r.name == "ollama")
    d = route(Requirements(min_tier=3), privacy=FULL, brains=[LOCAL])
    assert d.action == "queue" and "tier" in d.rejected[0].reason


def test_grant_tier_ceiling_and_brain_allowlist():
    d = route(REQ, privacy=FULL, grant=GrantView(max_tier=2), brains=[SUB, LOCAL])
    assert names(d) == ["ollama"]
    d = route(REQ, privacy=FULL, grant=GrantView(brains=["ollama"]), brains=[SUB, LOCAL])
    assert names(d) == ["ollama"]


def test_states_excluded_with_reasons():
    cool = view("claude-max", "subscription", state=BrainState.COOLING_DOWN, reason="until 15:00")
    auth = view("codex-plus", "subscription", state=BrainState.NEEDS_AUTH, reason="token expired")
    d = route(REQ, privacy=FULL, brains=[cool, auth, LOCAL])
    assert names(d) == ["ollama"]
    reasons = {r.name: r.reason for r in d.rejected}
    assert "cooling down (until 15:00)" in reasons["claude-max"]
    assert "sign in again" in reasons["codex-plus"]


def test_all_cooling_down_queues_with_reason():
    cool = view("claude-max", "subscription", state=BrainState.COOLING_DOWN)
    d = route(REQ, privacy=FULL, brains=[cool])
    assert d.action == "queue" and "cooling down" in d.reason


def test_busy_subscription_waits_rather_than_spending_money():
    busy = view("claude-max", "subscription", used=1)
    d = route(REQ, privacy=FULL, grant=OK_GRANT, brains=[busy, METERED], budget=Budget(max_usd=1))
    assert d.action == "wait" and names(d) == ["claude-max", "anthropic-api"]
    assert d.candidates[0].wait is True


def test_busy_subscription_routes_onward_to_free_local():
    busy = view("claude-max", "subscription", used=1)
    d = route(REQ, privacy=FULL, brains=[busy, LOCAL])
    assert d.action == "run" and names(d)[0] == "ollama"


def test_saturated_window_counts_as_busy():
    sat = view("claude-max", "subscription", saturated=True)
    d = route(REQ, privacy=FULL, brains=[sat, LOCAL])
    assert names(d)[0] == "ollama"


def test_metered_needs_grant_and_dollar_budget():
    d = route(REQ, privacy=FULL, brains=[METERED])  # grant does not allow metered
    assert d.action == "needs_approval" and d.approval["kind"] == "metered"
    d = route(REQ, privacy=FULL, grant=OK_GRANT, brains=[METERED], budget=Budget())  # no max_usd
    assert d.action == "needs_approval" and "dollar budget" in d.rejected[0].reason
    d = route(REQ, privacy=FULL, grant=OK_GRANT, brains=[METERED], budget=Budget(max_usd=2))
    assert d.action == "run"


def test_metered_spend_beyond_the_grants_remaining_budget():
    g = GrantView(allow_metered=True, remaining_usd=1.0)
    d = route(REQ, privacy=FULL, grant=g, brains=[METERED], budget=Budget(max_usd=5))
    assert d.action == "needs_approval"
    assert "over the grant's remaining" in d.rejected[0].reason


def test_token_budget_gate():
    g = GrantView(remaining_tokens=1000)
    d = route(REQ, privacy=FULL, grant=g, brains=[SUB], budget=Budget(max_tokens=5000))
    assert d.action == "queue" and "tokens" in d.rejected[0].reason


def test_fallback_chain_excludes_failed_brains():
    d = route(REQ, privacy=FULL, brains=[SUB, LOCAL], exclude={"claude-max"})
    assert names(d) == ["ollama"]
    assert d.rejected[0].reason == "already failed for this task"


def test_pinned_brain_is_strict():
    d = route(REQ, privacy=FULL, brains=[SUB, LOCAL], pinned="ollama")
    assert names(d) == ["ollama"]
    cool = view("claude-max", "subscription", state=BrainState.COOLING_DOWN)
    d = route(REQ, privacy=FULL, brains=[cool, LOCAL], pinned="claude-max")
    assert d.action == "queue" and "claude-max is not available" in d.reason
    d = route(REQ, privacy=FULL, brains=[SUB], pinned="nope")
    assert d.action == "queue" and "not registered" in d.reason


def test_pinned_cloud_brain_still_respects_local_only():
    d = route(REQ, privacy=Privacy.LOCAL_ONLY, brains=[SUB, LOCAL], pinned="claude-max")
    assert d.action == "queue" and d.candidates == []


def test_workspace_default_goes_first():
    d = route(REQ, privacy=FULL, brains=[SUB, LOCAL], preferred="ollama")
    assert names(d) == ["ollama", "claude-max"]
    assert "workspace default" in d.candidates[0].reason
    busy_default = view("ollama", "local", used=1)
    assert names(route(REQ, privacy=FULL, brains=[SUB, busy_default], preferred="ollama"))[0] == "claude-max"


def test_disabled_and_empty():
    d = route(REQ, privacy=FULL, brains=[view("x", "subscription", enabled=False)])
    assert d.action == "queue" and "disabled" in d.reason
    d = route(REQ, privacy=FULL, brains=[])
    assert d.action == "queue" and "no brains" in d.reason


def test_every_decision_has_a_reason_and_summary():
    for privacy in Privacy:
        for online in (True, False):
            d = route(REQ, privacy=privacy, brains=ALL, online=online, budget=Budget(max_usd=1))
            assert d.reason and d.summary()
            assert all(c.reason for c in d.candidates) and all(r.reason for r in d.rejected)


# ---- an OpenAI-compatible chat-completions brain: the metered pool's cheapest member ----------


def chat(name="chat-cheap", **kw):
    args = {"ctx": 32_768, "vision": False, "tier": 1, "seats": 2}
    return view(name, "metered", **{**args, **kw}).model_copy(
        update={"caps": Capabilities(context_tokens=args.get("ctx", 32_768), tools=False, vision=False,
                                     tier=kw.get("tier", 1))})


BOUNDED = Requirements(tools=False)
CHAT = chat()


def test_chat_brain_ranks_with_metered_below_subscription_and_local():
    d = route(BOUNDED, privacy=FULL, grant=OK_GRANT, brains=[CHAT, LOCAL, SUB], budget=Budget(max_usd=1))
    assert d.action == "run" and names(d) == ["claude-max", "ollama", "chat-cheap"]
    assert [c.kind for c in d.candidates][-1] == BrainKind.METERED


def test_chat_brain_is_ordered_within_metered_by_the_owners_order():
    cheap, dear = chat(order=1), view("anthropic-api", "metered", order=2)
    d = route(BOUNDED, privacy=FULL, grant=OK_GRANT, brains=[dear, cheap], budget=Budget(max_usd=1))
    assert names(d) == ["chat-cheap", "anthropic-api"]


def test_a_tier_1_grant_sends_a_bounded_job_past_the_frontier_subscription_to_the_chat_brain():
    grant = GrantView(allow_metered=True, max_tier=1)
    d = route(BOUNDED, privacy=FULL, grant=grant, brains=[SUB, METERED, CHAT], budget=Budget(max_usd=0.05))
    assert d.action == "run" and names(d) == ["chat-cheap"]
    assert {r.name for r in d.rejected} == {"claude-max", "anthropic-api"}


def test_the_chat_brain_passes_the_budget_gate_like_any_metered_brain():
    d = route(BOUNDED, privacy=FULL, grant=GrantView(), brains=[CHAT], budget=Budget(max_usd=1))
    assert d.action == "needs_approval" and d.approval == {"kind": "metered", "brains": ["chat-cheap"]}
    d = route(BOUNDED, privacy=FULL, grant=OK_GRANT, brains=[CHAT], budget=Budget())
    assert d.action == "needs_approval" and "dollar budget" in d.rejected[0].reason
    over = GrantView(allow_metered=True, remaining_usd=0.5)
    d = route(BOUNDED, privacy=FULL, grant=over, brains=[CHAT], budget=Budget(max_usd=1))
    assert d.action == "needs_approval" and "remaining" in d.rejected[0].reason


def test_a_job_that_needs_tools_never_lands_on_the_chat_brain():
    d = route(REQ, privacy=FULL, grant=OK_GRANT, brains=[CHAT], budget=Budget(max_usd=1))
    assert d.action == "queue" and d.rejected[0].reason == "no tool use"


def test_the_chat_brain_is_a_cloud_brain_whatever_its_cost():
    assert route(BOUNDED, privacy=Privacy.LOCAL_ONLY, grant=OK_GRANT, brains=[CHAT],
                 budget=Budget(max_usd=1)).action == "queue"
    d = route(BOUNDED, privacy=FULL, taint=["private"], grant=OK_GRANT, brains=[CHAT], budget=Budget(max_usd=1))
    assert d.action == "needs_approval" and d.approval["kind"] == "private_cloud"
    assert route(BOUNDED, privacy=FULL, grant=OK_GRANT, brains=[CHAT], budget=Budget(max_usd=1),
                 online=False).action == "queue"
    d = route(BOUNDED, privacy=Privacy.REDACTED_CLOUD, grant=OK_GRANT, brains=[CHAT], budget=Budget(max_usd=1))
    assert d.candidates[0].redact


def test_a_chat_endpoint_that_failed_falls_to_the_next_brain_in_the_chain():
    cheap, dear = chat(order=1), view("anthropic-api", "metered", order=2)
    kw = {"privacy": FULL, "grant": OK_GRANT, "budget": Budget(max_usd=1)}
    d = route(BOUNDED, brains=[cheap, dear, LOCAL], exclude=["chat-cheap", "ollama"], **kw)
    assert d.action == "run" and names(d) == ["anthropic-api"]
    d = route(BOUNDED, brains=[cheap], exclude=["chat-cheap"], **kw)
    assert d.action == "queue" and d.rejected[0].reason == "already failed for this task"
    down = chat(state=BrainState.DOWN, reason="no answer")
    d = route(BOUNDED, brains=[down, dear], **kw)
    assert names(d) == ["anthropic-api"]


# ---- the two kinds: a task and a typed decision ----


def decider(name="jev", *, order=100, state=BrainState.READY, reason=None, ctx=64_000):
    return BrainView(name=name, kind=BrainKind.METERED, locality=Locality.CLOUD, state=state, state_reason=reason,
                     caps=Capabilities(context_tokens=ctx, tools=False, tier=1, decisions=True), order=order,
                     seats_total=2)


DECIDE = Requirements(tools=False, decisions=True)
BOUNDED = Requirements(tools=False)  # the one task shape a tool-less brain could otherwise take
TASK_BRAIN = view("cheap", "metered", tier=1)


def test_a_task_never_routes_to_a_decision_brain():
    for req in (REQ, BOUNDED):
        d = route(req, privacy=FULL, grant=OK_GRANT, brains=[decider()], budget=Budget(max_usd=1))
        assert d.action == "queue" and names(d) == []
        assert "typed questions" in d.rejected[0].reason and d.rejected[0].approvable is None


def test_a_task_pinned_to_a_decision_brain_still_does_not_run_on_it():
    d = route(BOUNDED, privacy=FULL, grant=OK_GRANT, brains=[decider(), TASK_BRAIN], budget=Budget(max_usd=1),
              pinned="jev")
    assert d.action == "queue" and names(d) == [] and "jev is not available" in d.reason


def test_a_task_routes_past_a_decision_brain_to_the_others():
    d = route(BOUNDED, privacy=FULL, grant=OK_GRANT, brains=[decider(order=1), TASK_BRAIN], budget=Budget(max_usd=1))
    assert names(d) == ["cheap"]


def test_a_decision_only_routes_to_a_decision_brain():
    d = route(DECIDE, privacy=FULL, grant=OK_GRANT, brains=[*ALL, decider()], budget=Budget(max_usd=1))
    assert d.action == "run" and names(d) == ["jev"]
    assert {r.name for r in d.rejected} == {"claude-max", "ollama", "anthropic-api"}
    assert all(r.reason == "cannot answer typed questions" for r in d.rejected)


def test_a_decision_pinned_to_a_task_brain_does_not_run_on_it():
    d = route(DECIDE, privacy=FULL, grant=OK_GRANT, brains=[SUB, decider()], budget=Budget(max_usd=1),
              pinned="claude-max")
    assert d.action == "queue" and names(d) == []


def test_with_only_task_brains_a_decision_has_nowhere_to_go():
    d = route(DECIDE, privacy=FULL, grant=OK_GRANT, brains=ALL, budget=Budget(max_usd=1))
    assert d.action == "queue" and names(d) == []


def test_the_privacy_floor_comes_before_everything_for_a_decision():
    d = route(DECIDE, privacy=Privacy.LOCAL_ONLY, grant=OK_GRANT, brains=[decider()], budget=Budget(max_usd=1))
    assert d.action == "queue" and names(d) == []
    assert "cloud brains are never used" in d.rejected[0].reason and d.rejected[0].approvable is None


def test_private_data_needs_the_co_sign_for_a_decision_and_passes_with_it():
    d = route(DECIDE, privacy=FULL, taint=["private"], grant=OK_GRANT, brains=[decider()], budget=Budget(max_usd=1))
    assert d.action == "needs_approval" and d.approval == {"kind": "private_cloud", "brains": ["jev"]}
    signed = GrantView(allow_metered=True, allow_private_cloud=True, private_cloud_brains=["jev:1"])
    d = route(DECIDE, privacy=FULL, taint=["private"], grant=signed,
              brains=[decider().model_copy(update={"identity": "jev:1"})], budget=Budget(max_usd=1))
    assert d.action == "run"


def test_a_decision_is_metered_like_any_other_spend():
    d = route(DECIDE, privacy=FULL, brains=[decider()], budget=Budget(max_usd=1))
    assert d.action == "needs_approval" and d.approval["kind"] == "metered"
    d = route(DECIDE, privacy=FULL, grant=OK_GRANT, brains=[decider()])
    assert d.action == "needs_approval"


def test_redacted_cloud_marks_a_decision_for_the_redactor():
    d = route(DECIDE, privacy=Privacy.REDACTED_CLOUD, grant=OK_GRANT, brains=[decider()], budget=Budget(max_usd=1))
    assert d.candidates[0].redact is True


def test_decision_brains_chain_in_the_owners_order_and_skip_a_cooling_one():
    a, b = decider("jev-a", order=20), decider("jev-b", order=10)
    d = route(DECIDE, privacy=FULL, grant=OK_GRANT, brains=[a, b], budget=Budget(max_usd=1))
    assert names(d) == ["jev-b", "jev-a"]
    cooling = decider("jev-b", order=10, state=BrainState.COOLING_DOWN, reason="until 12:00")
    d = route(DECIDE, privacy=FULL, grant=OK_GRANT, brains=[a, cooling], budget=Budget(max_usd=1))
    assert names(d) == ["jev-a"]


def test_a_decision_too_long_for_the_context_is_rejected():
    req = Requirements(tools=False, decisions=True, min_context=70_000)
    d = route(req, privacy=FULL, grant=OK_GRANT, brains=[decider()], budget=Budget(max_usd=1))
    assert d.action == "queue" and "context" in d.reason
