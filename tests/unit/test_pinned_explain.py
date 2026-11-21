"""Pins what `openajar grants explain` says of a grant's specials and limits (openajar.policy.explain):
each special with and without its caps, chains of two blocks, budgets, meters, time and delegation."""

from __future__ import annotations

from pinned import check, outcome
from pinned_grants_blocks import block

from openajar.core.models import Effect
from openajar.policy.explain import _limits, _specials
from openajar.policy.grants import (
    BrainCap,
    Budget,
    EmptyCap,
    Grant,
    InstallCap,
    LaneCap,
    ServiceCap,
    Specials,
    Subject,
    VmCap,
)
from openajar.policy.memory_rules import MemoryCap
from openajar.policy.routing import RoutingTable

AGENT = Subject(id="agent-1")
H = "sha256:" + "1" * 64
NOW = 1_800_000_000

SPECIALS = {
    "none": Specials(),
    "secret": Specials(secret=LaneCap()),
    "secret on lanes": Specials(secret=LaneCap(lanes=["biz-a/instagram", "biz-b/*"])),
    "exec": Specials(exec=LaneCap(lanes=["*/*"])),
    "install overlay": Specials(install_overlay=InstallCap()),
    "install overlay sources": Specials(install_overlay=InstallCap(sources=["npm:left-pad@1.3.0", "pip:x==1"])),
    "install base": Specials(install_base=EmptyCap()),
    "service": Specials(service=ServiceCap()),
    "service ports and ttl": Specials(service=ServiceCap(ports=[3000], max_ttl_s=7200)),
    "service ports": Specials(service=ServiceCap(ports=[80, 443, 8080])),
    "brain": Specials(brain=BrainCap()),
    "brain everything": Specials(brain=BrainCap(provider="anthropic", tier="large", quota_tokens=1_500_000,
                                                quota_usd=12.5)),
    "brain quota": Specials(brain=BrainCap(quota_usd=3)),
    "vm": Specials(vm=VmCap()),
    "vm all caps": Specials(vm=VmCap(lanes=["biz-a/*"], profiles=["small", "gpu"], repos=["github.com/acme/*"])),
    "memory read": Specials(memory=MemoryCap()),
    "memory write types private": Specials(memory=MemoryCap(workspaces=["biz-a", "biz-b"], write=True,
                                                            types=["fact", "decision"], private=True)),
    "memory write": Specials(memory=MemoryCap(write=True)),
    "everything": Specials.everything(),
}


def grant_of(specials: Specials, child: Specials | None = None) -> Grant:
    root = block(sub=AGENT, workspaces=["biz-a", "biz-b"], ceiling=Effect.IRREVERSIBLE, specials=specials)
    if child is None:
        return Grant([root])
    return Grant([root, block(root, specials=child)])


LIMITS = {
    "nothing": {},
    "budget": {"budget": Budget(usd=10, tokens=12000)},
    "budget usd only": {"budget": Budget(usd=2.5)},
    "meters": {"budget": Budget(usd_per_day=5, tokens_per_day=1000, commits_per_hour=3, commits_per_day=20)},
    "starts later": {"nbf": NOW + 3600},
    "started": {"nbf": NOW - 10},
    "expires": {"exp": NOW + 86400 * 3},
    "expired": {"exp": NOW - 1},
    "expires soon": {"exp": NOW + 90},
    "no delegation": {"depth": 0},
    "one level": {"depth": 1},
    "three levels, fan out": {"depth": 3, "fanout": 4},
    "no fan out": {"fanout": 0},
    "pins": {"pins": {"lane_post": H}},
}


def test_specials_are_pinned():
    routing = RoutingTable()
    cases = {name: outcome(lambda s=s: _specials(grant_of(s), routing)) for name, s in SPECIALS.items()}
    cases["secret narrowed by a child"] = outcome(lambda: _specials(
        grant_of(Specials(secret=LaneCap(), service=ServiceCap(max_ttl_s=7200), brain=BrainCap(quota_tokens=9)),
                 Specials(secret=LaneCap(lanes=["biz-a/x"]), service=ServiceCap(max_ttl_s=300),
                          brain=BrainCap(quota_tokens=5, tier="small"))), routing))
    check("explain_specials", cases)


def test_limits_are_pinned():
    cases = {}
    for name, fields in LIMITS.items():
        g = Grant([block(sub=AGENT, workspaces=["biz-a"], ceiling=Effect.COMMIT, **fields)])
        cases[name] = outcome(lambda g=g: _limits(g, None, NOW))
        cases[f"{name}, remaining"] = outcome(lambda g=g: _limits(g, {"usd": 1.25, "tokens": 900}, NOW))
    root = block(sub=AGENT, workspaces=["biz-a"], budget=Budget(usd=10, usd_per_day=4), depth=3, exp=NOW + 9000)
    chain = Grant([root, block(root, budget=Budget(usd=3, usd_per_day=6), depth=1, exp=NOW + 600)])
    cases["a chain"] = outcome(lambda: _limits(chain, {"usd": 2}, NOW))
    check("explain_limits", cases)
