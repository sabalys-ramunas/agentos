"""The model router: a pure function from (step, workspace, grant, brains) to an ordered
candidate list. No I/O, no clock, no globals, so every rule is testable on its own.

Two axes, deliberately independent (docs/ARCHITECTURE.md section 4): privacy decides the
pool, cost decides the order within it.

  1. PRIVACY FLOOR (hard)   local-only never touches a cloud brain (it queues instead);
                            tainted private data goes local, or a cloud brain needs a co-sign;
                            redacted-cloud sends cloud candidates through the redactor.
  2. CAPABILITY FILTER      task or typed decision (a brain is one or the other, and neither
                            ever routes to the other), context, tools, vision, tier, the
                            grant's brain allowlist.
  3. RANK, cost-first       subscription seat > local model > metered API; within a kind
                            the owner's order. A busy subscription (seat taken or window
                            spent) still ranks above metered: waiting is free, metered is not.
  4. BUDGET GATE            metered brains need a dollar budget inside the grant's remainder.
  5. FALLBACK CHAIN         the ordered list; the caller excludes brains that already failed.
  6. QUEUE                  nothing eligible: wait (with the reason), never error. Internet
                            down means local only, cloud steps wait.

Every decision carries a short reason string; the service records it in traces.
"""

from __future__ import annotations

from collections.abc import Iterable

from agentos.brains.models import (
    BrainKind,
    BrainState,
    BrainView,
    Budget,
    Candidate,
    GrantView,
    Locality,
    Privacy,
    Rejection,
    Requirements,
    RouteDecision,
)

# lethal trifecta labels: private data plus untrusted input must not leave the node unsigned
PRIVATE = "private"
UNTRUSTED = "untrusted"


def _rank(view: BrainView) -> int:
    """Cost-first rank including seat availability."""
    busy = view.busy
    if view.kind == BrainKind.SUBSCRIPTION:
        return 2 if busy else 0
    if view.kind == BrainKind.LOCAL:
        return 3 if busy else 1
    return 5 if busy else 4


def route(
    req: Requirements,
    *,
    privacy: Privacy,
    taint: Iterable[str] = (),
    grant: GrantView | None = None,
    brains: Iterable[BrainView],
    budget: Budget | None = None,
    online: bool = True,
    pinned: str | None = None,
    preferred: str | None = None,
    exclude: Iterable[str] = (),
) -> RouteDecision:
    grant = grant or GrantView()
    budget = budget or Budget()
    taint = {t.lower() for t in taint}
    exclude = set(exclude)
    private_data = PRIVATE in taint
    trifecta = private_data and UNTRUSTED in taint

    candidates: list[tuple[tuple, Candidate]] = []
    rejected: list[Rejection] = []
    views = list(brains)

    if pinned and not any(v.name == pinned for v in views):
        return RouteDecision(action="queue", reason=f"brain {pinned} is not registered on this node")

    for v in views:
        if pinned and v.name != pinned:
            continue

        def reject(reason: str, approvable: str | None = None, name: str = v.name) -> None:
            rejected.append(Rejection(name=name, reason=reason, approvable=approvable))

        if v.name in exclude:
            reject("already failed for this task")
            continue
        if not v.enabled:
            reject("disabled by the owner")
            continue

        # 1. privacy floor
        if v.locality == Locality.CLOUD:
            if privacy == Privacy.LOCAL_ONLY:
                reject("local-only workspace: cloud brains are never used")
                continue
            # only a co-sign allows private data on a cloud brain, so one kept from before co-signs named
            # their brain (no list) names none: a task routed again asks again
            if private_data and grant.allow_private_cloud and not _named(grant.private_cloud_brains or [], v):
                reject("the co-sign to send private data named another brain", approvable="private_cloud")
                continue
            if private_data and not grant.allow_private_cloud:
                why = ("private data with untrusted input stays on local brains" if trifecta
                       else "private data stays on local brains")
                reject(why, approvable="private_cloud")
                continue
            if not online:
                reject("internet is down: cloud brains wait")
                continue

        # 2. capability filter
        if req.decisions != v.caps.decisions:
            reject("answers typed questions only, it cannot run a task" if v.caps.decisions
                   else "cannot answer typed questions")
            continue
        if req.min_context and v.caps.context_tokens < req.min_context:
            reject(f"context {v.caps.context_tokens} below the {req.min_context} needed")
            continue
        if req.tools and not v.caps.tools:
            reject("no tool use")
            continue
        if req.vision and not v.caps.vision:
            reject("no vision")
            continue
        if req.min_tier and v.caps.tier < req.min_tier:
            reject(f"tier {v.caps.tier} below the {req.min_tier} needed")
            continue
        if grant.max_tier is not None and v.caps.tier > grant.max_tier:
            reject(f"tier {v.caps.tier} above the grant's tier {grant.max_tier}")
            continue
        if grant.brains is not None and v.name not in grant.brains:
            reject("not in the grant's brain list")
            continue

        # state
        if v.state == BrainState.NEEDS_AUTH:
            reject(f"needs the owner to sign in again{_tail(v.state_reason)}")
            continue
        if v.state == BrainState.COOLING_DOWN:
            reject(f"cooling down{_tail(v.state_reason)}")
            continue
        if v.state == BrainState.DOWN:
            reject(f"down{_tail(v.state_reason)}")
            continue

        # 4. budget gate
        if v.kind == BrainKind.METERED:
            if not grant.allow_metered:
                reject("metered spend needs a co-sign", approvable="metered")
                continue
            if not _named(grant.metered_brains, v):
                reject("the co-sign to spend named another brain", approvable="metered")
                continue
            if budget.max_usd is None or budget.max_usd <= 0:
                reject("metered brains need a dollar budget (budget.max_usd)", approvable="metered")
                continue
            if grant.remaining_usd is not None and budget.max_usd > grant.remaining_usd:
                reject(f"${budget.max_usd:.2f} is over the grant's remaining ${grant.remaining_usd:.2f}",
                       approvable="metered")
                continue
        if (grant.remaining_tokens is not None and budget.max_tokens is not None
                and budget.max_tokens > grant.remaining_tokens):
            reject(f"{budget.max_tokens} tokens is over the grant's remaining {grant.remaining_tokens}")
            continue

        # 3. rank
        reason = _why(v, preferred)
        # a free workspace default leads (the owner chose it); otherwise cost rank decides
        key = (not (v.name == preferred and not v.busy), _rank(v), v.order, v.name)
        candidates.append((key, Candidate(
            name=v.name, kind=v.kind, reason=reason, wait=v.busy,
            redact=privacy == Privacy.REDACTED_CLOUD and v.locality == Locality.CLOUD)))

    ordered = [c for _, c in sorted(candidates, key=lambda kc: kc[0])]

    if ordered:
        first = ordered[0]
        if not first.wait:
            return RouteDecision(action="run", reason=f"{first.name}: {first.reason}",
                                 candidates=ordered, rejected=rejected)
        return RouteDecision(action="wait", reason=f"waiting for a free seat on {first.name}",
                             candidates=ordered, rejected=rejected)

    approvable = [r for r in rejected if r.approvable]
    if approvable and privacy != Privacy.LOCAL_ONLY:
        kinds = {r.approvable for r in approvable}
        kind = "private_cloud" if "private_cloud" in kinds else "metered"
        names = [r.name for r in approvable if r.approvable == kind]
        why = ("only cloud brains can take this and the data is private" if kind == "private_cloud"
               else "only metered brains are available and spending needs your co-sign")
        return RouteDecision(action="needs_approval", reason=why, rejected=rejected,
                             approval={"kind": kind, "brains": names})

    return RouteDecision(action="queue", reason=_queue_reason(privacy, online, rejected, pinned),
                         rejected=rejected)


ANY_BRAIN = "*"  # only a trial of what a yes would allow names it (BrainService._cosign_brain), never a grant


def _named(names: list[str] | None, v: BrainView) -> bool:
    """A co-sign that named brains covers only those, as they were when it was given."""
    return names is None or ANY_BRAIN in names or (v.identity is not None and v.identity in names)


def _tail(reason: str | None) -> str:
    return f" ({reason})" if reason else ""


def _why(v: BrainView, preferred: str | None) -> str:
    base = {
        BrainKind.SUBSCRIPTION: "subscription seat (already paid for)",
        BrainKind.LOCAL: "local model (free, stays on your network)",
        BrainKind.METERED: "metered API (fallback, debits the budget)",
    }[v.kind]  # a chat-completions brain is metered: same rank, same budget gate
    if v.name == preferred:
        base = f"workspace default, {base}"
    if v.seats_in_use >= v.seats_total:
        base += f", all {v.seats_total} seat(s) busy"
    elif v.window_saturated:
        base += ", usage window nearly spent"
    return base


def _queue_reason(privacy: Privacy, online: bool, rejected: list[Rejection], pinned: str | None) -> str:
    if pinned and rejected:
        return f"{pinned} is not available: {rejected[0].reason}"
    if privacy == Privacy.LOCAL_ONLY:
        if not any(r for r in rejected if "cloud brains are never used" not in r.reason):
            return "local-only workspace and no local brain is registered"
        return "local-only workspace and no local brain is ready"
    if not online:
        return "internet is down and no local brain is ready; cloud steps resume when it is back"
    if not rejected:
        return "no brains are registered on this node"
    cooling = [r for r in rejected if r.reason.startswith("cooling down")]
    if cooling and len(cooling) == len(rejected):
        return "every brain is cooling down after a rate limit"
    return "no brain can take this right now: " + "; ".join(f"{r.name}: {r.reason}" for r in rejected[:4])
