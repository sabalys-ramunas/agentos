"""Grant tokens: signing, offline verification, attenuation that only narrows, revocation, time."""

from __future__ import annotations

import os
import stat
import threading

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_policy_support import OWNER, Clock, make_service

from openajar.core.errors import InvalidRequest, NotFound
from openajar.core.models import Effect
from openajar.policy import tokens
from openajar.policy.errors import Conflict, Denied, PolicyKeyError, TokenInvalid
from openajar.policy.grants import BrainCap, Grant, LaneCap, Specials, parse_block
from openajar.policy.keys import load_or_create_root_key, load_public_key
from openajar.policy.service import PolicyService


@pytest.fixture
def svc(tmp_path):
    s, _ = make_service(tmp_path)
    yield s
    s.close()


def issue(svc, **kw):
    kw.setdefault("subject", {"id": "claude-code"})
    kw.setdefault("workspaces", ["biz-a"])
    kw.setdefault("ceiling", Effect.COMMIT)
    return svc.issue(**kw)


def test_key_file_is_private_and_reused(tmp_path):
    s1, _ = make_service(tmp_path)
    key = tmp_path / "secrets" / "policy.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "secrets").stat().st_mode) == 0o700
    g, tok = issue(s1)
    s2, _ = make_service(tmp_path)  # restart: same key, token still valid
    assert s2.resolve(tok)[1].id == g.id
    for name in ("policy.db", "traces.db"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600


def test_key_with_loose_permissions_is_refused(tmp_path):
    load_or_create_root_key(tmp_path / "secrets")
    os.chmod(tmp_path / "secrets" / "policy.key", 0o644)
    with pytest.raises(PolicyKeyError):
        load_or_create_root_key(tmp_path / "secrets")


def test_concurrent_key_creation_yields_one_key(tmp_path):
    out = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        out.append(load_or_create_root_key(tmp_path / "secrets").public_key().public_bytes_raw())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(set(out)) == 1


def test_offline_verification_with_public_key_only(svc, tmp_path):
    g, tok = issue(svc)
    pub = load_public_key(tmp_path / "secrets" / "policy.pub")
    blocks = [parse_block(b) for b in tokens.verify(tok, pub)]
    assert blocks[0].id == g.id
    other = Ed25519PrivateKey.generate().public_key()
    with pytest.raises(TokenInvalid):
        tokens.verify(tok, other)


def test_registry_stores_only_token_hashes(svc, tmp_path):
    _, tok = issue(svc)
    row = svc.db.one("SELECT token_sha256 FROM grants")
    assert row["token_sha256"] == tokens.token_hash(tok)
    data = (tmp_path / "policy.db").read_bytes() + (tmp_path / "policy.db-wal").read_bytes()
    assert tok.encode() not in data and tok[5:40].encode() not in data


@pytest.mark.parametrize("mangle", [
    lambda t: t[:-1],                      # truncated proof
    lambda t: t[: len(t) // 2],            # truncated in a block
    lambda t: t[:20],
    lambda t: t + "AA",                    # trailing bytes
    lambda t: t[:30] + ("A" if t[30] != "A" else "B") + t[31:],  # bit flip in signed data
    lambda t: "agt1_" + t[5:],             # the prefix names another version than the bytes
    lambda t: t.replace("_", "", 1),
    lambda t: "",
])
def test_tampered_or_truncated_tokens_fail(svc, mangle):
    _, tok = issue(svc)
    with pytest.raises(Denied) as e:
        svc.resolve(mangle(tok))
    assert e.value.reason in ("invalid_token", "unauthenticated")
    assert tok not in str(e.value)


def test_stripping_an_attenuation_block_fails(svc):
    _, tok = issue(svc)
    _, child = svc.attenuate(tok, ceiling=Effect.READ)
    version, blocks, tag, proof = tokens._decode(child)
    # drop the narrowing block but keep the child's proof key: must not verify
    stripped = tokens._encode(version, blocks[:1], tag, proof)
    with pytest.raises(TokenInvalid):
        svc.resolve(stripped)


def test_token_from_another_node_is_rejected(tmp_path):
    a, _ = make_service(tmp_path / "a")
    b, _ = make_service(tmp_path / "b")
    _, tok = issue(a)
    with pytest.raises(Denied) as e:
        b.resolve(tok)
    assert e.value.reason == "unknown_issuer"  # v2 names its issuer; B trusts only itself


def test_owner_token_is_a_full_grant(svc):
    p, g = svc.resolve(OWNER)
    assert g.owner_token and g.admin and p.id == "owner"
    assert g.ceiling == Effect.IRREVERSIBLE and g.lane_allowed("any-ws/any-lane")
    assert g.specials() == {"secret", "exec", "install_overlay", "install_base", "service", "brain", "memory", "vm"}
    with pytest.raises(Denied) as e:
        svc.resolve("owner-token-0123456789abcdeX")
    assert e.value.reason == "invalid_token"
    assert svc.resolve(f"Bearer {OWNER}")[1].owner_token


def test_principal_from_subject(svc):
    g, tok = issue(svc, subject={"id": "codex", "kind": "agent", "thread": "T-91"})
    p, _ = svc.resolve(tok)
    assert (p.kind, p.id, p.thread, p.grant_id) == ("agent", "codex", "T-91", g.id)


def test_unregistered_root_is_refused(svc):
    # signed by the right key but never issued through the registry
    from openajar.policy.grants import GrantBlock, Subject

    blk = GrantBlock(id="gr_aaaaaaaaaaaaaaaa", rid="rv_aaaaaaaaaaaaaaaa", iat=int(svc.clock()),
                     sub=Subject(id="x"), workspaces=["*"], exp=int(svc.clock()) + 60)
    tok = tokens.mint(svc.key, blk.to_data())
    with pytest.raises(Denied) as e:
        svc.resolve(tok)
    assert e.value.reason == "unregistered"


def test_unknown_caveat_fails_closed(svc):
    g, tok = issue(svc)
    child = tokens.append(tok, {"id": "gr_bbbbbbbbbbbbbbbb", "v": 2, "parent": g.id,
                                "iat": int(svc.clock()), "max_posts_per_hour": 3})
    with pytest.raises(TokenInvalid):
        svc.resolve(child)


# ------------------------------------------------------------------ time

def test_expiry_with_clock_skew(tmp_path):
    clock = Clock()
    svc, _ = make_service(tmp_path, clock=clock)
    _g, tok = issue(svc, ttl_s=3600)
    skew = svc.cfg.clock_skew_s
    clock.advance(3600 + skew - 1)
    svc.resolve(tok)  # inside the skew window
    clock.advance(2)
    with pytest.raises(Denied) as e:
        svc.resolve(tok)
    assert e.value.reason == "expired"


def test_not_yet_valid_with_skew(tmp_path):
    clock = Clock()
    svc, _ = make_service(tmp_path, clock=clock)
    _, tok = issue(svc, not_before=clock.t + 600, ttl_s=3600)
    with pytest.raises(Denied) as e:
        svc.resolve(tok)
    assert e.value.reason == "not_yet_valid"
    clock.advance(600 - svc.cfg.clock_skew_s + 1)
    svc.resolve(tok)


def test_child_cannot_outlive_parent(tmp_path):
    clock = Clock()
    svc, _ = make_service(tmp_path, clock=clock)
    _, tok = issue(svc, ttl_s=3600)
    with pytest.raises(InvalidRequest, match="exp"):
        svc.attenuate(tok, ttl_s=7200)
    # even hand-built offline, the parent's expiry still binds
    _, parent = svc.resolve(tok)
    child = tokens.append(tok, {"id": "gr_cccccccccccccccc", "v": 2, "parent": parent.id,
                                "iat": int(clock.t), "exp": int(clock.t) + 99999})
    clock.advance(3600 + svc.cfg.clock_skew_s + 1)
    with pytest.raises(Denied) as e:
        svc.resolve(child)
    assert e.value.reason == "expired"


def test_ttl_limits(svc):
    with pytest.raises(InvalidRequest):
        issue(svc, ttl_s=svc.cfg.max_grant_ttl_s + 1)


# ------------------------------------------------------------------ revocation

def test_revocation_cascades_to_all_descendants(svc):
    g, tok = issue(svc)
    c1, t1 = svc.attenuate(tok, ceiling=Effect.DRAFT)
    c2, t2 = svc.attenuate(t1, ceiling=Effect.READ)
    res = svc.revoke(g.id)
    assert set(res["cascades_to"]) == {c1.id, c2.id}
    for t in (tok, t1, t2):
        with pytest.raises(Denied) as e:
            svc.resolve(t)
        assert e.value.reason == "revoked"
    with pytest.raises(Conflict):
        svc.revoke(g.id)
    with pytest.raises(NotFound):
        svc.revoke("gr_doesnotexist00")
    states = {r["id"]: (r["state"], r["revoked_via"]) for r in svc.list_grants(include_inactive=True)}
    assert states[c2.id] == ("revoked", g.id)


def test_revoking_a_child_leaves_the_parent(svc):
    _g, tok = issue(svc)
    c1, t1 = svc.attenuate(tok, ceiling=Effect.DRAFT)
    svc.revoke(c1.id)
    svc.resolve(tok)
    with pytest.raises(Denied):
        svc.resolve(t1)


def test_offline_child_is_revoked_with_its_parent(svc):
    g, tok = issue(svc)
    child = tokens.append(tok, {"id": "gr_dddddddddddddddd", "v": 2, "parent": g.id,
                                "iat": int(svc.clock()), "ceiling": "read"})
    svc.resolve(child)  # first sight registers it
    assert svc.grant_record("gr_dddddddddddddddd")["source"] == "observed"
    svc.revoke(g.id)
    with pytest.raises(Denied):
        svc.resolve(child)


# ------------------------------------------------------------------ attenuation only narrows

WIDENINGS = [
    ("workspaces", {"workspaces": ["biz-b"]}),
    ("lanes", {"lanes": ["biz-ab/x"]}),
    ("lanes", {"lanes": ["*"]}),
    ("lanes", {"lanes": ["biz-*/x"]}),
    ("domains", {"domains": ["*"]}),
    ("ceiling", {"ceiling": "pay"}),
    ("specials", {"specials": {"install_base": {}}}),
    ("specials", {"specials": {"brain": {"provider": "openai"}}}),
    ("specials", {"specials": {"brain": {"tier": "large"}}}),
    ("specials", {"specials": {"brain": {"quota_tokens": 5_000_000}}}),
    ("specials", {"specials": {"exec": {"lanes": ["*"]}}}),
    ("budget", {"budget": {"usd": 50}}),
    ("budget", {"budget": {"tokens": 10**9}}),
    ("exp", {"ttl_s": 10**7}),
    ("admin", {"admin": True}),
    ("egress", {"egress": ["evil.example"]}),
    ("depth", {"depth": 9}),
    ("subject.owner", {"subject": {"id": "x", "owner": "someone-else"}}),
]


@pytest.fixture
def parent(svc):
    return issue(svc, workspaces=["biz-a"], domains=["instagram.com", "*.instagram.com"], ceiling=Effect.COMMIT,
                 specials=Specials(exec=LaneCap(lanes=["biz-a/term"]),
                                   brain=BrainCap(provider="anthropic", tier="medium", quota_tokens=200_000)),
                 budget={"usd": 10, "tokens": 100_000}, egress=["instagram.com"], depth=2, ttl_s=86400)


@pytest.mark.parametrize("field,kw", WIDENINGS, ids=[f"{f}-{i}" for i, (f, _) in enumerate(WIDENINGS)])
def test_attenuate_refuses_widening(svc, parent, field, kw):
    _, tok = parent
    with pytest.raises(InvalidRequest, match="widen"):
        svc.attenuate(tok, **kw)


def _offline(svc, tok, parent_id, **fields):
    data = {"id": "gr_eeeeeeeeeeeeeeee", "v": 2, "parent": parent_id,
            "iat": int(svc.clock()), **fields}
    return tokens.append(tok, data)


ADVERSARIAL = [
    ({"workspaces": ["biz-b"]}, Effect.READ, "biz-b/x", None, {}),
    ({"lanes": ["biz-ab/x"]}, Effect.READ, "biz-ab/x", None, {}),
    ({"lanes": ["*"]}, Effect.READ, "biz-b/x", None, {}),
    ({"domains": ["*"]}, Effect.READ, "biz-a/x", "evil.example", {}),
    ({"ceiling": "irreversible"}, Effect.IRREVERSIBLE, "biz-a/x", None, {}),
    ({"specials": {"install_base": {}}}, Effect.INSTALL_BASE, "biz-a/x", None, {}),
    ({"specials": {"exec": {}}}, Effect.EXEC, "biz-a/other", None, {"preset": "sandbox"}),
    ({"specials": {"brain": {"provider": "openai", "tier": "large"}}}, Effect.BRAIN, None, None,
     {"provider": "openai", "tier": "small"}),
    ({"specials": {"brain": {}}}, Effect.BRAIN, None, None, {"provider": "anthropic", "tier": "large"}),
]


@pytest.mark.parametrize("fields,effect,lane,domain,facts", ADVERSARIAL)
async def test_hand_built_widening_child_is_still_bounded(svc, parent, fields, effect, lane, domain, facts):
    """A holder builds a child offline (no node check) claiming more; every call stays
    inside the parent, because every block binds."""
    g, tok = parent
    child = _offline(svc, tok, g.id, **fields)
    p, cg = svc.resolve(child)
    ctx = svc.context(cg, principal=p, tool="t")
    with pytest.raises(Denied):
        await ctx.authorize(effect, lane_id=lane, domain=domain, facts=facts)


async def test_hand_built_admin_and_budget_widening(svc, parent):
    g, tok = parent
    child = _offline(svc, tok, g.id, admin=True, budget={"usd": 1000})
    _, cg = svc.resolve(child)
    assert not cg.admin
    ctx = svc.context(cg, tool="t")
    with pytest.raises(Denied) as e:
        await ctx.authorize(Effect.DRAFT, lane_id="biz-a/x", cost={"usd": 11})
    assert e.value.reason == "budget"


def test_narrowing_works_and_is_registered(svc, parent):
    g, tok = parent
    c, ctok = svc.attenuate(tok, subject={"id": "sub-agent"}, lanes=["biz-a/insta*"], ceiling=Effect.DRAFT,
                            specials=Specials(brain=BrainCap(provider="anthropic", tier="small",
                                                             quota_tokens=1000)),
                            budget={"usd": 1, "tokens": 10}, ttl_s=3600)
    p, cg = svc.resolve(ctok)
    assert p.id == "claude-code/sub-agent" and p.owner == "owner"  # a sub-agent of the parent (SEC-5)
    assert cg.lane_allowed("biz-a/instagram") and not cg.lane_allowed("biz-a/facebook")
    assert cg.ceiling == Effect.DRAFT and cg.specials() == {"brain"}
    rec = svc.grant_record(c.id)
    assert rec["parent_id"] == g.id and rec["source"] == "attenuated" and rec["state"] == "active"


def test_depth_limit(svc):
    _, tok = issue(svc, depth=1)
    _, t1 = svc.attenuate(tok, ceiling=Effect.DRAFT)
    with pytest.raises(Denied, match="depth"):
        svc.attenuate(t1, ceiling=Effect.READ)
    with pytest.raises(InvalidRequest, match="depth"):
        svc.attenuate(tok, depth=5)
    _, g1 = svc.resolve(t1)
    t2 = tokens.append(t1, {"id": "gr_ffffffffffffffff", "v": 2, "parent": g1.id,
                            "iat": int(svc.clock())})
    with pytest.raises(Denied) as e:
        svc.resolve(t2)
    assert e.value.reason == "depth"


def test_fanout_limit_counts_offline_children(svc):
    g, tok = issue(svc, fanout=2)
    svc.attenuate(tok, ceiling=Effect.READ)
    off = tokens.append(tok, {"id": "gr_gggggggggggggggg", "v": 2, "parent": g.id,
                              "iat": int(svc.clock())})
    svc.resolve(off)  # second child, fine
    with pytest.raises(Denied) as e:
        svc.attenuate(tok, ceiling=Effect.READ)
    assert e.value.reason == "fanout"
    svc.resolve(off)  # an already registered child keeps working


def test_reusing_a_registered_child_id_is_rejected(svc):
    g, tok = issue(svc, fanout=1)
    child, _ = svc.attenuate(tok, ceiling=Effect.READ)
    forged = tokens.append(tok, {"id": child.id, "v": 2, "parent": g.id,
                                 "iat": int(svc.clock()), "ceiling": "draft"})
    with pytest.raises(TokenInvalid):
        svc.resolve(forged)


def test_sealed_token_cannot_be_extended(svc):
    _, tok = issue(svc)
    sealed = tokens.seal(tok)
    svc.resolve(sealed)
    with pytest.raises(TokenInvalid):
        tokens.append(sealed, {"id": "gr_hhhhhhhhhhhhhhhh", "v": 2, "iat": 0})


def test_thread_pin_cannot_move(svc):
    g, tok = issue(svc, subject={"id": "a", "thread": "T-1"})
    with pytest.raises(InvalidRequest, match="thread"):
        svc.attenuate(tok, subject={"id": "b", "thread": "T-2"})
    child = tokens.append(tok, {"id": "gr_iiiiiiiiiiiiiiii", "v": 2, "parent": g.id,
                                "iat": int(svc.clock()), "sub": {"id": "b", "thread": "T-2"}})
    with pytest.raises(TokenInvalid):
        svc.resolve(child)


def test_owner_token_child_is_a_new_root(svc):
    _g, tok = svc.attenuate(OWNER, subject={"id": "worker"}, workspaces=["biz-a"], ceiling=Effect.DRAFT,
                           specials=Specials(), ttl_s=600)
    p, cg = svc.resolve(tok)
    assert p.id == "worker" and not cg.admin and cg.ceiling == Effect.DRAFT and cg.specials() == set()


def test_owner_child_with_explicit_empty_scope_reaches_nothing(svc):
    _, tok = svc.attenuate(OWNER, subject={"id": "w"}, workspaces=[], ttl_s=600)
    _, g = svc.resolve(tok)
    assert not g.lane_allowed("biz-a/x") and not g.has_lane_scope()
    _, tok2 = svc.attenuate(OWNER, subject={"id": "w"}, lanes=["biz-a/insta*"], ttl_s=600)
    _, g2 = svc.resolve(tok2)
    assert g2.lane_allowed("biz-a/instagram") and not g2.lane_allowed("biz-b/x")


def test_empty_scope_grant(svc):
    _g, tok = svc.issue(subject={"id": "nobody"}, ceiling=Effect.COMMIT)
    _, cg = svc.resolve(tok)
    assert not cg.has_lane_scope() and not cg.lane_allowed("biz-a/x")
    _, t2 = svc.attenuate(issue(svc)[1], lanes=[])
    _, g2 = svc.resolve(t2)
    assert not g2.lane_allowed("biz-a/x")


def test_grant_projection_is_readable(svc, parent):
    g, _ = parent
    rec = svc.grant_record(g.id)
    scope = rec["scope"]
    assert scope["ceiling"] == "commit" and set(scope["specials"]) == {"exec", "brain"}
    assert rec["budget"] == {"tokens": 100_000, "usd": 10.0}
    assert "token" not in rec and all("agt1_" not in str(v) for v in rec.values())


def test_grant_structure_rejects_bad_chains():
    from openajar.policy.grants import GrantBlock, Subject

    a = GrantBlock(id="gr_aaaaaaaaaaaaaaaa", rid="rv_aaaaaaaaaaaaaaaa", iat=0, sub=Subject(id="x"), exp=10)
    b = GrantBlock(id="gr_bbbbbbbbbbbbbbbb", rid="rv_bbbbbbbbbbbbbbbb", iat=0, parent="gr_zzzzzzzzzzzzzzzz")
    with pytest.raises(TokenInvalid):
        Grant([a, b]).check_structure()
    with pytest.raises(TokenInvalid):
        Grant([a, a]).check_structure()


def test_service_restart_keeps_registry(tmp_path):
    s1, _ = make_service(tmp_path)
    g, tok = issue(s1)
    s1.revoke(g.id)
    s1.close()
    s2 = PolicyService(tmp_path, clock=Clock())
    with pytest.raises(Denied):
        s2.resolve(tok)


# ------------------------------------------------------------------ grants an admin grant issues fit inside it

def test_a_grant_within_an_admin_grant_never_widens_it(svc):
    """The panel passes the caller's grant as `within`: an admin grant issues only what it holds."""
    admin, _ = svc.issue(subject={"kind": "agent", "id": "adm"}, workspaces=["biz-a"], ceiling="draft",
                         admin=True, ttl_s=86400)
    with pytest.raises(InvalidRequest, match="holds: lanes$"):
        svc.issue(subject={"id": "kid"}, workspaces=["biz-b"], ceiling="draft", ttl_s=3600, within=admin)
    with pytest.raises(InvalidRequest, match="holds: ceiling$"):
        svc.issue(subject={"id": "kid"}, workspaces=["biz-a"], ceiling="commit", ttl_s=3600, within=admin)
    with pytest.raises(InvalidRequest, match="holds: exp$"):
        svc.issue(subject={"id": "kid"}, workspaces=["biz-a"], ceiling="draft", ttl_s=30 * 86400, within=admin)
    narrow, _ = svc.issue(subject={"id": "kid"}, workspaces=["biz-a"], ceiling="read", ttl_s=3600, within=admin)
    assert narrow.ceiling == Effect.READ and narrow.lane_allowed("biz-a/x")


def test_a_grant_within_cannot_shed_a_limit_by_leaving_it_out(svc):
    """A narrowed child inherits what it leaves out; a fresh root does not, so leaving one out is widening."""
    admin, _ = svc.issue(subject={"kind": "agent", "id": "adm"}, workspaces=["biz-a"], ceiling="draft", admin=True,
                         ttl_s=86400, budget={"usd": 5}, domains=["example.com"])
    with pytest.raises(InvalidRequest, match="holds: budget, domains$"):
        svc.issue(subject={"id": "kid"}, workspaces=["biz-a"], ceiling="read", ttl_s=3600, within=admin)
    ok, _ = svc.issue(subject={"id": "kid"}, workspaces=["biz-a"], ceiling="read", ttl_s=3600, within=admin,
                      budget={"usd": 5}, domains=["example.com"])
    assert ok.ceiling == Effect.READ
    fanned, _ = svc.issue(subject={"kind": "agent", "id": "fan"}, workspaces=["biz-a"], ceiling="draft", admin=True,
                          ttl_s=86400, fanout=2)
    with pytest.raises(InvalidRequest, match="holds: fanout$"):
        svc.issue(subject={"id": "kid"}, workspaces=["biz-a"], ceiling="read", ttl_s=3600, within=fanned)


def test_the_owner_token_as_within_bounds_nothing(svc):
    _, owner = svc.resolve(OWNER)
    g, _ = svc.issue(subject={"id": "kid"}, workspaces=["biz-b"], ceiling="commit", ttl_s=30 * 86400,
                     within=owner)
    assert g.ceiling == Effect.COMMIT and g.lane_allowed("biz-b/x")
