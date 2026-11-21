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
from dataclasses import dataclass

from openajar.brains.models import (
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
    floor = _Floor(privacy=privacy, private_data=PRIVATE in taint,
                   trifecta=PRIVATE in taint and UNTRUSTED in taint, online=online)

    candidates: list[tuple[tuple, Candidate]] = []
    rejected: list[Rejection] = []
    views = list(brains)

    if pinned and not any(v.name == pinned for v in views):
        return RouteDecision(action="queue", reason=f"brain {pinned} is not registered on this node")

    for v in views:
        if pinned and v.name != pinned:
            continue
        no = _first_no(v, req, floor, grant, budget, exclude)
        if no is not None:
            rejected.append(Rejection(name=v.name, reason=no[0], approvable=no[1]))
            continue
        # 3. rank: a free workspace default leads (the owner chose it); otherwise cost rank decides
        key = (not (v.name == preferred and not v.busy), _rank(v), v.order, v.name)
        candidates.append((key, Candidate(
            name=v.name, kind=v.kind, reason=_why(v, preferred), wait=v.busy,
            redact=privacy == Privacy.REDACTED_CLOUD and v.locality == Locality.CLOUD)))

    ordered = [c for _, c in sorted(candidates, key=lambda kc: kc[0])]
    if ordered:
        first = ordered[0]
        if not first.wait:
            return RouteDecision(action="run", reason=f"{first.name}: {first.reason}",
                                 candidates=ordered, rejected=rejected)
        return RouteDecision(action="wait", reason=f"waiting for a free seat on {first.name}",
                             candidates=ordered, rejected=rejected)
    return _nothing_eligible(privacy, online, rejected, pinned)


@dataclass(frozen=True)
class _Floor:
    """What the privacy floor needs to know about the step, the same for every brain."""
    privacy: Privacy
    private_data: bool
    trifecta: bool
    online: bool


# A gate's no: the reason, and the co-sign that would turn it into a yes (or None).
_No = tuple[str, str | None]


def _first_no(v: BrainView, req: Requirements, floor: _Floor, grant: GrantView, budget: Budget,
              exclude: set[str]) -> _No | None:
    """The gates in order; the first that says no is the reason."""
    return (_owner_gate(v, exclude) or _privacy_gate(v, floor, grant) or _capability_gate(v, req, grant)
            or _state_gate(v) or _budget_gate(v, grant, budget))


def _owner_gate(v: BrainView, exclude: set[str]) -> _No | None:
    if v.name in exclude:
        return "already failed for this task", None
    if not v.enabled:
        return "disabled by the owner", None
    return None


def _privacy_gate(v: BrainView, floor: _Floor, grant: GrantView) -> _No | None:
    """1. The privacy floor, for cloud brains only."""
    if v.locality != Locality.CLOUD:
        return None
    if floor.privacy == Privacy.LOCAL_ONLY:
        return "local-only workspace: cloud brains are never used", None
    # only a co-sign allows private data on a cloud brain, so one kept from before co-signs named
    # their brain (no list) names none: a task routed again asks again
    if floor.private_data and grant.allow_private_cloud and not _named(grant.private_cloud_brains or [], v):
        return "the co-sign to send private data named another brain", "private_cloud"
    if floor.private_data and not grant.allow_private_cloud:
        why = ("private data with untrusted input stays on local brains" if floor.trifecta
               else "private data stays on local brains")
        return why, "private_cloud"
    if not floor.online:
        return "internet is down: cloud brains wait", None
    return None


def _capability_gate(v: BrainView, req: Requirements, grant: GrantView) -> _No | None:
    """2. What the step needs, and the grant's own limits on brains."""
    caps = v.caps
    if req.decisions != caps.decisions:
        return ("answers typed questions only, it cannot run a task" if caps.decisions
                else "cannot answer typed questions"), None
    if req.min_context and caps.context_tokens < req.min_context:
        return f"context {caps.context_tokens} below the {req.min_context} needed", None
    if req.tools and not caps.tools:
        return "no tool use", None
    if req.vision and not caps.vision:
        return "no vision", None
    if req.min_tier and caps.tier < req.min_tier:
        return f"tier {caps.tier} below the {req.min_tier} needed", None
    if grant.max_tier is not None and caps.tier > grant.max_tier:
        return f"tier {caps.tier} above the grant's tier {grant.max_tier}", None
    if grant.brains is not None and v.name not in grant.brains:
        return "not in the grant's brain list", None
    return None


def _state_gate(v: BrainView) -> _No | None:
    if v.state == BrainState.NEEDS_AUTH:
        return f"needs the owner to sign in again{_tail(v.state_reason)}", None
    if v.state == BrainState.COOLING_DOWN:
        return f"cooling down{_tail(v.state_reason)}", None
    if v.state == BrainState.DOWN:
        return f"down{_tail(v.state_reason)}", None
    return None


def _budget_gate(v: BrainView, grant: GrantView, budget: Budget) -> _No | None:
    """4. Metered brains need a co-sign naming them and a dollar budget the grant can cover."""
    if v.kind == BrainKind.METERED:
        if not grant.allow_metered:
            return "metered spend needs a co-sign", "metered"
        if not _named(grant.metered_brains, v):
            return "the co-sign to spend named another brain", "metered"
        if budget.max_usd is None or budget.max_usd <= 0:
            return "metered brains need a dollar budget (budget.max_usd)", "metered"
        if grant.remaining_usd is not None and budget.max_usd > grant.remaining_usd:
            return f"${budget.max_usd:.2f} is over the grant's remaining ${grant.remaining_usd:.2f}", "metered"
    if (grant.remaining_tokens is not None and budget.max_tokens is not None
            and budget.max_tokens > grant.remaining_tokens):
        return f"{budget.max_tokens} tokens is over the grant's remaining {grant.remaining_tokens}", None
    return None


def _nothing_eligible(privacy: Privacy, online: bool, rejected: list[Rejection], pinned: str | None) -> RouteDecision:
    """No brain passed every gate: ask for the co-sign that would let one, or queue with the reason."""
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
