"""Attenuation only narrows (SEC-3): for random parents and random children built offline
(without the node's widening check), anything the child allows its parent allows too, for
every call shape and every field: lanes, workspaces, domains, egress, ceiling, specials and
their parameters (memory workspaces, types and flags too), pins, admin, expiry, depth and
fan-out. A child's subject is its parent's or a sub-agent of it, never a human, the panel or
another identity (SEC-5). For a person's grant (subject kind human) the same holds for `approve`
(effects, lanes, max_usd) and `control` (lanes, watch_only, max_session_s); an agent's chain that
carries either is refused."""

from __future__ import annotations

import random

import pytest
from test_policy_support import T0, make_service

from agentos.core.models import Effect
from agentos.policy.errors import Denied, TokenInvalid
from agentos.policy.grants import (
    LADDER,
    ApproveCap,
    BrainCap,
    Budget,
    ControlCap,
    EmptyCap,
    Grant,
    GrantBlock,
    InstallCap,
    LaneCap,
    ServiceCap,
    Specials,
    Subject,
    widening_problems,
)
from agentos.policy.memory_rules import MemoryCap, check_memory

LANES = ["biz-a/x", "biz-a/insta", "biz-a/term", "biz-b/x", "personal/reddit"]
LANE_PATTERNS = ["*", "biz-a/*", "biz-a/insta*", "biz-b/x", "personal/*", "biz-*/x", "biz-a/term"]
WORKSPACES = ["*", "biz-a", "biz-b", "personal"]
DOMAINS = ["*", "instagram.com", "*.instagram.com", "evil.example", "*.example"]
HOSTS = ["instagram.com", "www.instagram.com", "evil.example", "a.example", "shop.example", None]
SOURCES = ["npm:left-pad@1.3.0", "npm:evil@6.6.6", "pip:requests@2.32.0"]
PINS = [{"lane_read": "sha256:aa"}, {"lane_read": "sha256:bb"}, {"lane_post": "sha256:cc"}, {}]
TOOLS = [("lane_read", "sha256:aa"), ("lane_read", "sha256:bb"), ("lane_post", "sha256:cc")]
MEMORY_WORKSPACES = ["biz-a", "biz-b", "personal", "other"]
MEMORY_NEEDS = [{}, {"write": True}, {"write": True, "entry_type": "fact"}, {"write": True, "entry_type": "decision"},
                {"private": True}, {"audience_owner": True}]
FACTS = [{}, {"ports": [8080]}, {"ports": [22]}, {"ports": [8080, 22]},
         {"provider": "anthropic", "tier": "small", "credential_owner": "owner"},
         {"provider": "openai", "tier": "large"}, {"provider": "anthropic"}, {"tier": "medium"}]


def _some(rng: random.Random, items: list, most: int = 2) -> list:
    return rng.sample(items, rng.randint(0, most))


def _maybe(rng: random.Random, make):
    return make() if rng.random() < 0.5 else None


APPROVE_EFFECTS = [Effect.COMMIT, Effect.PAY, Effect.IRREVERSIBLE, Effect.EXEC]
AMOUNTS = [None, 10.0, 50.0, 75.0]


def _human_specials(rng: random.Random) -> dict:
    return {
        "approve": _maybe(rng, lambda: ApproveCap(effects=rng.sample(APPROVE_EFFECTS, rng.randint(1, 3)),
                                                  lanes=_maybe(rng, lambda: _some(rng, LANE_PATTERNS)),
                                                  max_usd=_maybe(rng, lambda: rng.choice([10.0, 50.0])))),
        "control": _maybe(rng, lambda: ControlCap(lanes=_maybe(rng, lambda: _some(rng, LANE_PATTERNS)),
                                                  watch_only=_maybe(rng, lambda: rng.random() < 0.5),
                                                  max_session_s=_maybe(rng, lambda: rng.choice([600, 3600])))),
    }


def _specials(rng: random.Random, human: bool = False) -> Specials:
    return Specials(**(_human_specials(rng) if human else {}),
        secret=_maybe(rng, lambda: LaneCap(lanes=_maybe(rng, lambda: _some(rng, LANE_PATTERNS)))),
        exec=_maybe(rng, lambda: LaneCap(lanes=_maybe(rng, lambda: _some(rng, LANE_PATTERNS)))),
        install_overlay=_maybe(rng, lambda: InstallCap(sources=_maybe(rng, lambda: _some(rng, SOURCES)))),
        install_base=_maybe(rng, EmptyCap),
        service=_maybe(rng, lambda: ServiceCap(max_ttl_s=_maybe(rng, lambda: rng.choice([60, 3600])),
                                               ports=_maybe(rng, lambda: _some(rng, [8080, 22, 443])))),
        brain=_maybe(rng, lambda: BrainCap(provider=_maybe(rng, lambda: rng.choice(["anthropic", "openai"])),
                                           tier=_maybe(rng, lambda: rng.choice(["small", "medium", "large"])),
                                           credential_owner=_maybe(rng, lambda: "owner"))),
        memory=_maybe(rng, lambda: MemoryCap(
            workspaces=_maybe(rng, lambda: _some(rng, WORKSPACES)),
            types=_maybe(rng, lambda: _some(rng, ["fact", "document", "decision"])),
            write=rng.random() < 0.6, private=rng.random() < 0.5, audience_owner=rng.random() < 0.5)))


SUBJECTS = [None, {"id": "agent-1"}, {"id": "agent-1/w"}, {"id": "agent-1/w/x", "kind": "brain"},
            {"id": "owner", "kind": "human"}, {"id": "agent-1/w", "kind": "panel"}, {"id": "agent-2"},
            {"id": "agent-1/w", "thread": "T-2"}, {"id": "agent-1", "owner": "mallory"}]


# v2 narrowed blocks name a sub-agent by label (and may pin a thread) instead of a subject
LABELS = [{}, {"label": "w", "kind": "agent"}, {"label": "w/x", "kind": "brain"}, {"label": "f", "kind": "flow"},
          {"thread": "T-1"}, {"thread": "T-2"}, {"label": "w", "kind": "agent", "thread": "T-2"}]


def _identity(rng: random.Random, authority: bool, version: int, human: bool = False) -> dict:
    if authority:
        sub = Subject(kind="human", id="m_va", owner="m_va") if human else \
            Subject(id="agent-1", thread=rng.choice([None, "T-1"]))
        out: dict = {"sub": sub}
        if version >= 2:
            out.update(iss="node:local_test", aud=["node:local_test"], kid="ed25519:" + "0" * 32)
        return out
    if version >= 2:
        return dict(rng.choice(LABELS))
    pick = rng.choice(SUBJECTS)
    return {"sub": None if pick is None else Subject(**pick)}


def _block(rng: random.Random, i: int, parent: str | None, version: int = 1, human: bool = False) -> GrantBlock:
    """A random block. `human`: the chain is a person's grant; its authority block may carry approve
    and control. Any narrowed block may try to add them (an agent's chain that does is refused)."""
    letter = "abcdefgh"[i]
    authority = parent is None
    ident = _identity(rng, authority, version, human)
    return GrantBlock(
        v=version, id=f"gr_{letter * 16}", rid=f"rv_{letter * 16}" if version < 2 else None,
        parent=parent, iat=int(T0), **ident,
        workspaces=_maybe(rng, lambda: _some(rng, WORKSPACES)),
        lanes=_maybe(rng, lambda: _some(rng, LANE_PATTERNS)),
        domains=_maybe(rng, lambda: _some(rng, DOMAINS)),
        egress=_maybe(rng, lambda: _some(rng, DOMAINS)),
        ceiling=_maybe(rng, lambda: rng.choice(LADDER)),
        specials=_maybe(rng, lambda: _specials(rng, human=human or not authority)),
        budget=_maybe(rng, lambda: Budget(usd=rng.choice([None, 1.0, 5.0]), tokens=rng.choice([None, 100]))),
        nbf=_maybe(rng, lambda: int(T0) + rng.choice([-60, 0, 60])),
        exp=int(T0) + rng.choice([600, 3600]) if authority else _maybe(rng, lambda: int(T0) + rng.choice([300, 7200])),
        depth=_maybe(rng, lambda: rng.randint(0, 3)),
        fanout=_maybe(rng, lambda: rng.randint(0, 5)),
        pins=_maybe(rng, lambda: rng.choice(PINS)),
        admin=_maybe(rng, lambda: rng.random() < 0.5),
    )


def _effect_ok(svc, g: Grant, eff: Effect, lane: str | None, facts: dict) -> bool:
    try:
        svc._check_effect(g, eff, lane, facts)
    except Denied:
        return False
    return True


def _memory_ok(g: Grant, ws: str, needs: dict) -> bool:
    try:
        check_memory(g, ws, **needs)
    except Denied:
        return False
    return True


@pytest.fixture(scope="module")
def svc(tmp_path_factory):
    s, _ = make_service(tmp_path_factory.mktemp("mono"))
    yield s
    s.close()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("seed", range(40))
def test_a_child_never_allows_what_its_parent_does_not(svc, seed, version):
    rng = random.Random(seed)
    for _ in range(25):
        human = rng.random() < 0.3
        blocks = [_block(rng, 0, None, version, human)]
        for i in range(1, rng.randint(2, 4)):
            blocks.append(_block(rng, i, blocks[-1].id, version, human))
        parent, child = Grant(blocks[:-1]), Grant(blocks)
        try:
            child.check_structure()
        except (TokenInvalid, Denied):
            continue  # a refused child allows nothing
        if not human:
            assert child.caps("approve") is None and child.caps("control") is None
        _human_narrows(parent, child, seed)
        for lane in LANES:
            assert not child.lane_allowed(lane) or parent.lane_allowed(lane)
        for host in HOSTS:
            if host is not None:
                assert not child.domain_allowed(host) or parent.domain_allowed(host)
            assert not child.egress_allows(host) or parent.egress_allows(host), (seed, host, child.blocks)
        p, c = parent.subject, child.subject
        assert (c.kind, c.id) == (p.kind, p.id) or (c.kind in ("agent", "flow", "brain")
                                                    and c.id.startswith(p.id + "/"))
        assert c.owner == p.owner and (p.thread is None or c.thread == p.thread)
        assert child.ceiling.rank <= parent.ceiling.rank
        assert child.specials() <= parent.specials()
        assert not child.has_lane_scope() or parent.has_lane_scope()
        assert not child.admin or parent.admin
        for tool, manifest in TOOLS:
            assert not child.pins_allow(tool, manifest) or parent.pins_allow(tool, manifest)
        if parent.expires_at is not None:
            assert child.expires_at is not None and child.expires_at <= parent.expires_at
        if parent.not_before is not None:
            assert child.not_before is not None and child.not_before >= parent.not_before
        if (limit := parent.fanout_limit()) is not None:
            assert child.fanout_limit() is not None and child.fanout_limit() <= limit
        assert {bid for bid, _ in parent.budgets()} <= {bid for bid, _ in child.budgets()}
        for eff in Effect:
            assert not child.effect_allowed(eff) or parent.effect_allowed(eff)
            for lane in [None, *LANES]:
                for facts in FACTS:
                    if _effect_ok(svc, child, eff, lane, facts):
                        assert _effect_ok(svc, parent, eff, lane, facts), (seed, eff, lane, facts, child.blocks)
        for ws in MEMORY_WORKSPACES:
            for needs in MEMORY_NEEDS:
                if _memory_ok(child, ws, needs):
                    assert _memory_ok(parent, ws, needs), (seed, ws, needs, child.blocks)
        for source in SOURCES:
            assert not svc._source_allowlisted(child, source) or svc._source_allowlisted(parent, source)
        for ttl in (30, 600, 5000) if "service" in child.specials() else ():
            assert not svc._ttl_within(child, ttl, T0) or svc._ttl_within(parent, ttl, T0)


def _human_narrows(parent: Grant, child: Grant, seed: int) -> None:
    """approve and control only narrow: what the child may decide or control, the parent may."""
    for eff in [None, *APPROVE_EFFECTS, Effect.DRAFT]:
        for lane in [None, *LANES]:
            for amount in AMOUNTS:
                value = None if eff is None else eff.value
                if child.approve_problem(value, lane, amount_usd=amount) is None:
                    assert parent.approve_problem(value, lane, amount_usd=amount) is None, (seed, eff, lane, amount,
                                                                                            child.blocks)
    for lane in LANES:
        c = child.control_terms(lane)
        if c is None:
            continue
        p = parent.control_terms(lane)
        assert p is not None, (seed, lane, child.blocks)
        assert c["watch_only"] or not p["watch_only"]
        if p["max_session_s"] is not None:
            assert c["max_session_s"] is not None and c["max_session_s"] <= p["max_session_s"]


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("seed", range(20))
def test_the_widening_check_names_every_human_special_widening(svc, seed, version):
    """A narrowed block that the node's widening check accepts never decides or controls more than
    its parent, and a block adding approve or control to an agent's chain is always named."""
    rng = random.Random(1000 + seed)
    for _ in range(25):
        human = rng.random() < 0.6
        parent = Grant([_block(rng, 0, None, version, human)])
        block = _block(rng, 1, parent.id, version, True)
        problems = widening_problems(parent, block, tiers=svc.cfg.brain_tiers, aliases=svc.cfg.brain_tier_aliases)
        added = block.specials.human() if block.specials is not None else set()
        if not human and added:
            assert any(p.startswith(("specials.approve", "specials.control")) for p in problems)
            continue
        if any(p.startswith(("specials.approve", "specials.control")) for p in problems):
            continue
        child = Grant([*parent.blocks, block])
        try:
            child.check_structure()
        except (TokenInvalid, Denied):
            continue
        _human_narrows(parent, child, seed)
