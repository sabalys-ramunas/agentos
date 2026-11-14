"""Policy for callers outside MCP: the panel (authenticate_panel, scoped contexts), the runner
(authorize under a stored principal) and brains (child grants, allowance, taint, owner tokens)."""

from __future__ import annotations

import pytest
from test_policy_support import OWNER, Clock, make_service

from agentos.core.errors import ApprovalPending, InvalidRequest
from agentos.core.models import Effect, Principal
from agentos.policy import tokens
from agentos.policy.errors import Denied, TokenInvalid
from agentos.policy.grants import BrainCap, Budget, LaneCap, Specials


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    svc, _rt = make_service(tmp_path, clock=clock)
    yield svc, clock
    svc.close()


def grant(svc, **kw):
    kw.setdefault("subject", {"id": "agent-1", "thread": "T-1"})
    kw.setdefault("workspaces", ["biz-a"])
    kw.setdefault("ceiling", Effect.COMMIT)
    _g, tok = svc.issue(**kw)
    principal, _ = svc.resolve(tok)
    return principal, tok


# ------------------------------------------------------------------ panel

def test_authenticate_panel_owner_grant_and_garbage(env):
    svc, _ = env
    owner = svc.authenticate_panel(OWNER)
    assert owner is not None and owner.grant_id == "owner" and owner.kind == "human"
    principal, tok = grant(svc)
    got = svc.authenticate_panel(tok)
    assert got == principal and got.grant_id == principal.grant_id
    for bad in (None, "", "nope", tok[:-3] + "AAA", "Bearer " + "x" * 50, "agt1_" + "\x00" * 10):
        assert svc.authenticate_panel(bad) is None


def test_revoked_or_expired_grants_do_not_authenticate(env):
    svc, clock = env
    principal, tok = grant(svc, ttl_s=600)
    svc.revoke(principal.grant_id)
    assert svc.authenticate_panel(tok) is None
    p2, tok2 = grant(svc, ttl_s=600)
    clock.advance(3600)
    assert svc.authenticate_panel(tok2) is None
    with pytest.raises(Denied):
        svc.principal_context(p2)


def test_principal_context_scopes_to_the_grant(env):
    svc, _ = env
    principal, _ = grant(svc, workspaces=None, lanes=["biz-a/instagram"])
    ctx = svc.principal_context(principal, tool="panel")
    assert not ctx.admin
    assert ctx.can_access_lane("biz-a/instagram") and not ctx.can_access_lane("biz-a/facebook")
    ctx.precheck(Effect.READ, lane_id="biz-a/instagram")
    with pytest.raises(Denied, match="out_of_scope"):
        ctx.precheck(Effect.READ, lane_id="biz-b/x")
    with pytest.raises(Denied, match="special"):
        ctx.precheck(Effect.EXEC, lane_id="biz-a/instagram")
    owner = svc.owner_context(tool="panel")
    assert owner.admin and owner.can_access_lane("anything/at-all")


def test_a_principal_without_a_known_grant_is_refused(env):
    svc, _ = env
    for p in (Principal(id="x"), Principal(id="x", grant_id="gr_doesnotexist00"), {"id": "x"}, None):
        with pytest.raises(Denied, match="unregistered"):
            svc.grant_for(p)


def test_no_token_block_may_use_the_owner_grant_id(env):
    """A holder appending a block named "owner" would read the owner's approvals and traces."""
    svc, _ = env
    _, tok = grant(svc)
    data = {"v": 2, "id": "owner", "iat": 0, "parent": svc.resolve(tok)[1].id}
    forged = tokens.append(tok, data)
    with pytest.raises(TokenInvalid, match="reserved"):
        svc.resolve(forged)
    assert svc.authenticate_panel(forged) is None


def test_is_owner_token_covers_admin_grants(env):
    svc, _ = env
    assert svc.is_owner_token(OWNER) and svc.is_owner_token(f"  {OWNER} ")
    _, admin = grant(svc, admin=True)
    _, plain = grant(svc)
    assert svc.is_owner_token(admin)
    assert not svc.is_owner_token(plain) and not svc.is_owner_token("") and not svc.is_owner_token("junk")


# ------------------------------------------------------------------ runner

async def test_authorize_under_a_stored_principal_routes_and_traces(env):
    svc, _ = env
    principal, _ = grant(svc)
    stored = principal.model_dump()  # what a run keeps
    d = await svc.authorize(stored, Effect.READ, lane_id="biz-a/x")
    assert d.route == "auto"
    with pytest.raises(ApprovalPending):
        await svc.authorize(stored, Effect.COMMIT, lane_id="biz-a/x", summary="Post", content={"p": 1})
    with pytest.raises(Denied, match="ceiling"):
        await svc.authorize(stored, Effect.PAY, lane_id="biz-a/x")
    with pytest.raises(Denied, match="unregistered"):
        await svc.authorize({"kind": "flow", "id": "schedule:s1"}, Effect.READ, lane_id="biz-a/x")
    events, _ = svc.traces.query(kind="authorize")
    assert {e.status for e in events} >= {"pending", "denied"}
    assert any(e.principal == "schedule:s1" for e in events)


async def test_owner_principal_from_the_cli(env):
    svc, _ = env
    d = await svc.authorize(Principal(kind="human", id="owner", grant_id="owner"), Effect.READ, lane_id="a/b")
    assert d.route == "auto"


# ------------------------------------------------------------------ brains

def test_child_token_attenuates_the_parent(env):
    svc, clock = env
    parent, _ = grant(svc, ceiling=Effect.COMMIT, budget=Budget(tokens=5000, usd=2.0),
                      specials=Specials(brain=BrainCap(), exec=LaneCap(), secret=LaneCap()), ttl_s=7200)
    out = svc.mint_child_token(parent, purpose="brain task bt_1", workspace="biz-a",
                               lanes=["biz-a/instagram"], ceiling="irreversible", specials=["brain", "exec"],
                               ttl_seconds=86400, budget={"tokens": 999_999, "usd": None})
    child, g = svc.resolve(out["token"])
    assert g.id == out["grant_id"] and child.kind == "brain" and child.thread == "T-1"
    assert g.chain_ids[0] == parent.grant_id  # narrowed from the parent's own chain
    assert g.ceiling == Effect.COMMIT  # never above the parent
    assert g.specials() == set()  # never brain or exec
    assert g.lane_allowed("biz-a/instagram") and not g.lane_allowed("biz-a/facebook")
    assert g.expires_at <= int(clock()) + 7200  # never after the parent
    assert svc.remaining(g) == {"tokens": 5000, "usd": 2.0}
    assert not g.admin and svc.grant_record(out["grant_id"])["depth"] == 1
    with pytest.raises(Denied, match="depth"):
        svc.attenuate(out["token"], lanes=["biz-a/instagram"])  # depth 0: no further delegation
    svc.revoke(parent.grant_id)  # the parent's revocation reaches the child
    with pytest.raises(Denied, match="revoked"):
        svc.resolve(out["token"])


def test_child_token_refusals(env):
    svc, _ = env
    parent, _ = grant(svc, workspaces=None, lanes=["biz-a/instagram"])
    with pytest.raises(Denied, match="out_of_scope"):
        svc.mint_child_token(parent, purpose="p", workspace="biz-a", lanes=["biz-a/facebook"])
    with pytest.raises(InvalidRequest, match="workspace"):
        svc.mint_child_token(parent, purpose="p", workspace="biz-b", lanes=["biz-a/instagram"])
    with pytest.raises(InvalidRequest, match="at least one lane"):
        svc.mint_child_token(parent, purpose="p", workspace="biz-a", lanes=[])
    with pytest.raises(InvalidRequest, match="cannot carry"):
        svc.mint_child_token(parent, purpose="p", workspace="biz-a", lanes=["biz-a/instagram"],
                             specials=["service"])
    with pytest.raises(Denied, match="special"):
        svc.mint_child_token(parent, purpose="p", workspace="biz-a", lanes=["biz-a/instagram"],
                             specials=["secret"])


def test_child_of_the_owner_is_a_narrow_root_and_revocable(env):
    svc, _ = env
    owner = svc.authenticate_panel(OWNER)
    out = svc.mint_child_token(owner, purpose="brain task bt_2", workspace="personal",
                               lanes=["personal/reddit"], ttl_seconds=600)
    _child, g = svc.resolve(out["token"])
    assert not svc.is_owner_token(out["token"]) and g.ceiling == Effect.COMMIT and g.specials() == set()
    assert g.lane_allowed("personal/reddit") and not g.lane_allowed("biz-a/x")
    svc.revoke(out["grant_id"])
    with pytest.raises(Denied, match="revoked"):
        svc.resolve(out["token"])
    assert OWNER not in out["token"]


def test_brain_allowance(env):
    svc, _ = env
    no_brain, _ = grant(svc)
    assert svc.brain_allowance(no_brain) == {"allow_metered": False, "remaining_usd": 0.0, "remaining_tokens": 0,
                                             "max_tier": None, "brains": []}
    p, _ = grant(svc, ceiling=Effect.PAY, budget=Budget(usd=5.0),
                 specials=Specials(brain=BrainCap(tier="medium", quota_tokens=1000)))
    a = svc.brain_allowance(p)
    assert a["allow_metered"] and a["remaining_usd"] == 5.0 and a["remaining_tokens"] == 1000 and a["max_tier"] == 2
    commit_only, _ = grant(svc, specials=Specials(brain=BrainCap()))
    assert not svc.brain_allowance(commit_only)["allow_metered"]
    assert svc.brain_allowance(svc.authenticate_panel(OWNER))["allow_metered"]


def test_taint_for_reads_the_principals_grant_family(env):
    svc, _ = env
    p, tok = grant(svc)
    svc.context_for_token(tok, tool="t").taint("untrusted")
    child_tok = svc.attenuate(tok, ttl_s=600)[1]
    svc.context_for_token(child_tok, tool="t").taint("private")  # a child's reads taint the family
    other, other_tok = grant(svc, subject={"id": "agent-2"})
    svc.context_for_token(other_tok, tool="t").taint("owner")  # another family stays apart
    child = svc.resolve(child_tok)[0]
    assert svc.taint_for(p) == svc.taint_for(child) == {"untrusted", "private"}
    assert svc.taint_for(p.model_dump()) == {"untrusted", "private"}
    assert svc.taint_for(other) == {"owner"}
    assert svc.taint_for(Principal(id="nobody")) == set() and svc.taint_for(None) == set()
