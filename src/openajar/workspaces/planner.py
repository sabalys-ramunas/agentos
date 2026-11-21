"""Plans: what applying a template would do. Computed from facts the caller supplies, with no side
effects, so the same function serves `plan`, the approval summary and the re-check inside apply.
"""

from __future__ import annotations

from collections.abc import Callable

from openajar.workspaces.models import Plan, PlanStep, Template
from openajar.workspaces.rules import LaneFact, proxy_conflicts

NO_PROXY_WARNING = ("no proxy: this lane exits on the node's own IP, the same IP as every other lane "
                    "without a proxy")


def _on(proxy: str | None) -> str:
    return f"proxy {proxy}" if proxy is not None else "no proxy"


def existing_problems(*, proxy: str | None, platform: str, current: LaneFact) -> list[str]:
    """Why a lane that already exists cannot be the one the plan wants. Existing lanes are never moved."""
    problems: list[str] = []
    if current.proxy != proxy:
        problems.append(f"already exists on {_on(current.proxy)}, the plan wants {_on(proxy)}: "
                        "existing lanes are never moved")
    if current.platform != platform:
        problems.append(f"already exists with platform {current.platform}, the template says {platform}")
    return problems


def make_plan(template: Template, workspaces: list[str], *, proxies: dict[str, str | None],
              existing: dict[str, LaneFact], override: bool, reason: str | None,
              shown: Callable[[str], bool] | None = None) -> Plan:
    """`proxies` maps every planned lane id to its proxy label (None for no proxy). `existing` maps
    every lane the node has now to its facts. Steps come out in workspace order, then surface order.
    `shown(lane)`: a reason never names a lane the reader may not see (rules.proxy_conflicts)."""
    accepted = dict(existing)
    steps: list[PlanStep] = []
    for workspace in workspaces:
        for surface in template.surfaces:
            lane = f"{workspace}/{surface.name}"
            proxy = proxies[lane]
            base = {"lane": lane, "workspace": workspace, "surface": surface.name,
                    "platform": surface.platform, "engine": surface.engine, "proxy": proxy,
                    "home_url": surface.home_url, "login_url": surface.login_url, "sites": surface.sites,
                    "warnings": [NO_PROXY_WARNING] if proxy is None else []}
            current = existing.get(lane)
            if current is not None:
                problems = existing_problems(proxy=proxy, platform=surface.platform, current=current)
                steps.append(PlanStep(**base, action="conflict" if problems else "exists", reasons=problems))
                continue
            candidate = LaneFact(lane, workspace, surface.platform, proxy)
            reasons = proxy_conflicts(candidate, accepted.values(), shown)
            if reasons and not override:
                steps.append(PlanStep(**base, action="conflict", reasons=reasons))
                continue
            steps.append(PlanStep(**base, action="create", overridden=reasons))
            accepted[lane] = candidate
    return Plan(template=template.id, workspaces=list(workspaces), steps=steps,
                override_proxy_rules=override, reason=reason if override else None)


def approval_summary(plan: Plan) -> str:
    """The text the owner reads before approving. Written from the plan's steps, never from the
    caller's words, so the approval shows what will really happen."""
    creates = plan.creates
    count = len(creates)
    text = (f"Create {count} {'lane' if count == 1 else 'lanes'} from template {plan.template}: "
            + ", ".join(f"{step.lane} ({_on(step.proxy)})" for step in creates))
    existing = plan.count("exists")
    if existing:
        text += f"; {existing} already exist and match"
    overriding = sum(1 for step in creates if step.overridden)
    if overriding:
        text += f"; OVERRIDES the proxy rules for {overriding} {'lane' if overriding == 1 else 'lanes'}"
    return text


def refusal(plan: Plan) -> str:
    """The message for a plan that cannot be applied, one clause per conflicting lane."""
    clauses = []
    for step in plan.conflicts:
        extra = len(step.reasons) - 1
        clauses.append(f"{step.lane}: {step.reasons[0]}" + (f" (and {extra} more)" if extra else ""))
    count = len(clauses)
    return (f"{count} {'conflict' if count == 1 else 'conflicts'}, nothing was created: "
            + " | ".join(clauses))
