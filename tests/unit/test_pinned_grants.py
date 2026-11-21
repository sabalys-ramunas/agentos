"""Pins the narrowing check (openajar.policy.grants): widening_problems and _cap_widening for each
special's caps, each field varied on its own against a few parents, and some all at once."""

from __future__ import annotations

from types import SimpleNamespace

from pinned import check, outcome

from openajar.core.models import Effect
from openajar.policy.grants import (
    BrainCap,
    Budget,
    Grant,
    GrantBlock,
    LaneCap,
    Specials,
    Subject,
    _cap_widening,
    owner_block,
    widening_problems,
)
from openajar.policy.memory_rules import MemoryCap

TIERS, ALIASES = ["small", "large"], {"big": "large"}
H1, H2 = "sha256:" + "1" * 64, "sha256:" + "2" * 64

LANES = [None, [], ["biz-a/*"], ["biz-a/x"], ["biz-b/x"], ["biz-a/x", "biz-b/x"]]
SETS = [None, [], ["a"], ["a", "b"], ["c"]]
PORTS = [None, [], [80], [80, 443], [22]]
REPOS = [None, ["github.com/acme/*"], ["github.com/acme/api"], ["github.com/other/x"]]
LIMITS = [None, 0, 5, 10]
MONEY = [None, 0.0, 2.5, 10.0]
FLAGS = [None, False, True]
NAMES = [None, "x", "y"]
TIER = [None, "small", "large", "big", "huge"]
EFFECTS = [["read"], ["read", "draft"], ["commit"]]

CAP_FIELDS = {
    "secret": {"lanes": LANES},
    "exec": {"lanes": LANES},
    "vm": {"lanes": LANES, "profiles": SETS, "repos": REPOS},
    "install_overlay": {"sources": SETS},
    "service": {"max_ttl_s": LIMITS, "ports": PORTS},
    "approve": {"lanes": LANES, "effects": EFFECTS, "max_usd": MONEY},
    "control": {"lanes": LANES, "watch_only": FLAGS, "max_session_s": LIMITS},
    "brain": {"provider": NAMES, "credential_owner": NAMES, "tier": TIER, "quota_tokens": LIMITS,
              "quota_usd": MONEY},
    "install_base": {},
    "nothing_known": {"lanes": LANES},
}


def _cap(fields: dict, **values) -> SimpleNamespace:
    base = {f: (["read"] if f == "effects" else None) for f in fields}
    return SimpleNamespace(**{**base, **values})


def cap_cases() -> dict:
    cases = {}
    for name, fields in CAP_FIELDS.items():
        for field, values in fields.items():
            for c in values:
                for p in values:
                    cases[f"{name}.{field} child={c!r} parent={p!r}"] = (name, _cap(fields, **{field: c}),
                                                                         [_cap(fields, **{field: p})])
        widest = _cap(fields, **{f: v[0] for f, v in fields.items()})
        narrow = _cap(fields, **{f: v[-1] for f, v in fields.items()})
        middle = _cap(fields, **{f: v[len(v) // 2] for f, v in fields.items()})
        cases[f"{name} all fields, two parents"] = (name, widest, [narrow, middle])
        cases[f"{name} narrow under wide"] = (name, narrow, [widest])
        cases[f"{name} no parents"] = (name, widest, [])
    mem = [MemoryCap(workspaces=["biz-a"], write=False), MemoryCap(workspaces=["biz-a", "biz-b"], write=True),
           MemoryCap()]
    for i, c in enumerate(mem):
        for j, p in enumerate(mem):
            cases[f"memory child={i} parent={j}"] = ("memory", c, [p])
    return cases


def test_cap_widening_is_pinned():
    check("cap_widening", {k: outcome(lambda a=a: _cap_widening(a[0], a[1], a[2], TIERS, ALIASES))
                           for k, a in cap_cases().items()})


_IDS = iter(f"gr_{i:016d}" for i in range(10_000))


def block(parent: GrantBlock | None = None, **kw) -> GrantBlock:
    gid = next(_IDS)
    return GrantBlock(id=gid, rid="rv_" + gid[3:], iat=0, parent=parent.id if parent else None, **kw)


AGENT = Subject(id="agent-1")
ROOT = block(sub=AGENT, workspaces=["biz-a"], lanes=["biz-a/*"], domains=["example.com", "*.shop.example"],
             egress=["*.example.com"], ceiling=Effect.DRAFT,
             specials=Specials(secret=LaneCap(lanes=["biz-a/*"]), brain=BrainCap(tier="small", quota_tokens=100),
                               memory=MemoryCap(workspaces=["biz-a"])),
             budget=Budget(usd=10, tokens=1000, usd_per_day=5, commits_per_day=3), nbf=100, exp=1000, depth=3,
             fanout=5, pins={"tool": H1}, aud=["org:o1"])
SECOND = block(ROOT, lanes=["biz-a/x", "biz-a/y"], budget=Budget(usd=4, tokens_per_day=50), depth=1)
PARENTS = {
    "root": Grant([ROOT]),
    "chain": Grant([ROOT, SECOND]),
    "owner": Grant([owner_block()]),
    "bare": Grant([block(sub=AGENT, workspaces=["*"], ceiling=Effect.IRREVERSIBLE)]),
}
CHILD_FIELDS = {
    "lanes": [["biz-a/x"], ["biz-a/z"], ["biz-b/x"], []],
    "workspaces": [["biz-a"], ["biz-b"]],
    "domains": [["example.com"], ["a.shop.example"], ["other.com"], []],
    "egress": [["api.example.com"], ["evil.com"], []],
    "ceiling": [Effect.READ, Effect.DRAFT, Effect.COMMIT, Effect.IRREVERSIBLE],
    "specials": [Specials(), Specials(secret=LaneCap(lanes=["biz-a/x"])), Specials(secret=LaneCap(lanes=["biz-b/x"])),
                 Specials(secret=LaneCap()), Specials(brain=BrainCap(tier="large")),
                 Specials(brain=BrainCap(tier="small", quota_tokens=50)), Specials(exec=LaneCap()),
                 Specials(memory=MemoryCap(workspaces=["biz-a"])), Specials(memory=MemoryCap(workspaces=["biz-b"]))],
    "budget": [Budget(), Budget(usd=5, tokens=10), Budget(usd=50), Budget(tokens=5000), Budget(usd=1, tokens=1),
               Budget(usd=1, tokens=1, usd_per_day=9), Budget(usd=1, tokens=1, commits_per_day=1),
               Budget(usd=1, tokens=1, tokens_per_day=100), Budget(usd=1, tokens=1, commits_per_hour=99)],
    "exp": [500, 1000, 2000],
    "nbf": [50, 100, 200],
    "admin": [False, True],
    "aud": [["org:o1"], ["node:n1"], ["org:o2"], ["org:o1", "node:n1"]],
    "pins": [{}, {"tool": H1}, {"tool": H2}, {"other": H1}],
    "depth": [0, 1, 2, 3, 5],
    "fanout": [0, 5, 6],
    "sub": [AGENT, Subject(id="agent-2"), Subject(kind="human", id="agent-1"), Subject(id="agent-1", owner="x")],
}


def widening_cases() -> dict:
    cases = {}
    for pname, parent in PARENTS.items():
        cases[f"{pname}: empty child"] = (parent, {})
        for field, values in CHILD_FIELDS.items():
            for i, v in enumerate(values):
                cases[f"{pname}: {field}[{i}]"] = (parent, {field: v})
        cases[f"{pname}: everything wider"] = (parent, {f: v[-1] for f, v in CHILD_FIELDS.items()})
        cases[f"{pname}: everything first"] = (parent, {f: v[0] for f, v in CHILD_FIELDS.items()})
    return cases


def test_widening_problems_is_pinned():
    def run(parent: Grant, fields: dict) -> list[str]:
        return widening_problems(parent, block(parent.blocks[-1], **fields), tiers=TIERS, aliases=ALIASES)

    check("widening_problems", {k: outcome(lambda a=a: run(*a)) for k, a in widening_cases().items()})
