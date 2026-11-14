"""Token v2 on a node (TOKENS.md): audience, issuer and key ids, key rotation with an overlap,
derived revocation ids, the v1 migration window, and org grants through policy/issuers.py."""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_policy_support import T0, Clock, issue_v1, make_service

from agentos.core import signing
from agentos.core.config import NodeConfig
from agentos.core.models import Effect
from agentos.core.runtime import Runtime
from agentos.policy import tokens
from agentos.policy.errors import Denied
from agentos.policy.grants import GrantBlock
from agentos.policy.issuers import SERVICE_NAME, TrustedIssuers
from agentos.policy.routing import PolicyConfig
from agentos.policy.service import PolicyService, build
from agentos.policy.util import new_id

ORG = "org_test"
NODE = "node_here"


def reason(call) -> str:
    with pytest.raises(Denied) as raised:
        call()
    return raised.value.reason


# ------------------------------------------------------------------ the node's own grants

@pytest.fixture
def svc(tmp_path):
    s, _ = make_service(tmp_path)
    yield s
    s.close()


def test_new_grants_are_v2_and_name_issuer_audience_and_key(svc):
    g, tok = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    assert tok.startswith("agt2_") and tokens.version_of(tok) == 2
    auth = g.blocks[0]
    assert (auth.v, auth.iss, auth.aud, auth.kid) == (2, svc.issuer, [svc.issuer], svc.kid)
    assert "rid" not in auth.to_data() and auth.rid == tokens.revocation_id(tokens.inspect(tok).blocks[0].sig)
    row = svc.db.one("SELECT v, rid, block_sha256 FROM grants WHERE id = ?", (g.id,))
    assert (row["v"], row["rid"], row["block_sha256"]) == (2, auth.rid, auth.digest())
    assert svc.resolve(tok)[0].id == "claude-code"


def test_a_narrowed_v2_block_carries_a_label_not_a_subject(svc):
    _, tok = svc.issue(subject={"id": "claude-code", "thread": "T-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    child, ctok = svc.attenuate(tok, subject={"id": "codex-worker"}, ceiling="read")
    data = child.blocks[-1].to_data()
    assert data["label"] == "codex-worker" and data["kind"] == "agent" and "sub" not in data and "rid" not in data
    p, _ = svc.resolve(ctok)
    assert (p.id, p.thread) == ("claude-code/codex-worker", "T-1")
    # a brain child is a labelled sub-agent too, re-signed by the node with the parent's key
    out = svc.mint_child_token(child.principal(), purpose="bt_1", workspace="biz-a", lanes=["biz-a/x"],
                               ceiling="read")
    bp, bg = svc.resolve(out["token"])
    assert (bp.kind, bp.id) == ("brain", "claude-code/codex-worker/bt_1") and bg.version == 2


def test_audience_another_node_is_refused(tmp_path):
    a, _ = make_service(tmp_path / "a")
    b, _ = make_service(tmp_path / "b", config=None)
    try:
        _, tok = a.issue(subject={"id": "x"}, workspaces=["biz-a"], audience=["node:somewhere-else"])
        assert reason(lambda: a.resolve(tok)) == "audience"
        _, tok_b = a.issue(subject={"id": "x"}, workspaces=["biz-a"])
        assert reason(lambda: b.resolve(tok_b)) == "unknown_issuer"
    finally:
        a.close()
        b.close()


def test_an_org_audience_is_accepted_once_enrolled(tmp_path):
    rt = Runtime()
    cfg = NodeConfig(home=tmp_path, mcp_token="owner-token-0123456789", node_id=NODE, org_id=ORG)
    rt.register("config", cfg)
    svc = PolicyService(tmp_path, node_config=cfg, runtime=rt, clock=Clock())
    try:
        assert svc.audiences() == {svc.issuer, f"node:{NODE}", f"org:{ORG}"}
        for aud in ([f"org:{ORG}"], [f"node:{NODE}"], ["node:other", svc.issuer]):
            _, tok = svc.issue(subject={"id": "x"}, workspaces=["biz-a"], audience=aud)
            svc.resolve(tok)
    finally:
        svc.close()


def test_the_local_issuer_id_is_stable_and_not_copied_with_the_key(tmp_path):
    a, _ = make_service(tmp_path / "a")
    first = a.issuer
    a.close()
    again, _ = make_service(tmp_path / "a")
    assert again.issuer == first
    again.close()


def test_registry_binds_the_root_block_bytes(svc):
    """The registry compares the root's hash too: a root signed with the node's key but never
    issued here, reusing a registered id, is refused (SEC-9)."""
    g, _ = svc.issue(subject={"id": "x"}, workspaces=["biz-a"], ceiling=Effect.READ)
    wider = {**g.blocks[0].to_data(), "ceiling": "irreversible"}
    forged = tokens.mint(svc.key, wider)
    assert reason(lambda: svc.resolve(forged)) == "invalid_token"


def test_revoking_a_parent_cascades_to_offline_and_registered_children(svc):
    g, tok = svc.issue(subject={"id": "x"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    _, node_child = svc.attenuate(tok, ceiling="draft")
    offline = tokens.append(tok, {"v": 2, "id": "gr_" + "o" * 16, "parent": g.id, "iat": int(T0), "ceiling": "read"})
    grandchild = tokens.append(node_child, {"v": 2, "id": "gr_" + "q" * 16,
                                            "parent": tokens.inspect(node_child).blocks[-1].data["id"],
                                            "iat": int(T0)})
    for t in (node_child, offline, grandchild):
        svc.resolve(t)
    out = svc.revoke(g.blocks[0].rid)  # by the root's derived revocation id
    assert out["id"] == g.id and len(out["cascades_to"]) == 3
    for t in (tok, node_child, offline, grandchild):
        assert reason(lambda t=t: svc.resolve(t)) == "revoked"


def test_a_re_signed_sibling_with_a_revoked_id_stays_revoked(svc):
    """The holder of a parent can re-sign a revoked child's exact block with a new key: the
    signature (and so the revocation id) differs, but the grant id is revoked."""
    g, tok = svc.issue(subject={"id": "x"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    block = {"v": 2, "id": "gr_" + "s" * 16, "parent": g.id, "iat": int(T0), "ceiling": "read"}
    first = tokens.append(tok, block)
    _, child = svc.resolve(first)
    svc.revoke(child.id)
    again = tokens.append(tok, block)
    assert tokens.inspect(again).blocks[-1].rid != child.blocks[-1].rid
    assert reason(lambda: svc.resolve(again)) == "revoked"


# ------------------------------------------------------------------ key rotation

def test_rotation_keeps_old_grants_for_the_overlap_then_retires_them(tmp_path):
    clock = Clock()
    svc, _ = make_service(tmp_path, clock=clock)
    try:
        g, old = svc.issue(subject={"id": "x"}, workspaces=["biz-a"], ttl_s=30 * 86400)
        old_kid = svc.kid
        out = svc.rotate_key(overlap_s=3600)
        assert out["previous"] == old_kid and out["kid"] == svc.kid != old_kid
        assert svc.resolve(old)[1].kid == old_kid
        new_g, new = svc.issue(subject={"id": "y"}, workspaces=["biz-a"])
        assert new_g.kid == svc.kid
        # during the overlap the node can still re-sign the old chain for a brain child
        brain = svc.mint_child_token(g.principal(), purpose="b", workspace="biz-a", lanes=["biz-a/x"],
                                     ceiling="read")
        svc.resolve(brain["token"])
        assert {k["kid"]: k["state"] for k in svc.signing_keys()} == {svc.kid: "active", old_kid: "retiring"}
        clock.advance(3601)
        for t in (old, brain["token"]):
            assert reason(lambda t=t: svc.resolve(t)) == "retired_key"
        assert reason(lambda: svc.grant_for(g.principal())) == "retired_key"
        svc.resolve(new)
        assert not list(tmp_path.joinpath("secrets").glob("policy-*.key"))  # the old private key is gone
    finally:
        svc.close()


def test_retire_ends_the_overlap_early_and_unknown_kids_are_refused(tmp_path):
    svc, _ = make_service(tmp_path)
    try:
        _, old = svc.issue(subject={"id": "x"}, workspaces=["biz-a"])
        old_kid = svc.kid
        svc.rotate_key()
        svc.resolve(old)
        svc.retire_key(old_kid)
        assert reason(lambda: svc.resolve(old)) == "retired_key"
        stranger = Ed25519PrivateKey.generate()
        block = {**tokens.inspect(old).blocks[0].data, "kid": tokens.key_id(stranger)}
        assert reason(lambda: svc.resolve(tokens.mint(stranger, block))) == "unknown_key"
    finally:
        svc.close()


def test_a_running_service_sees_a_rotation_made_by_the_cli(tmp_path):
    daemon, _ = make_service(tmp_path)
    cli, _ = make_service(tmp_path)
    try:
        link_key = daemon.approvals._link_key
        cli.rotate_key(overlap_s=60)
        _, tok = cli.issue(subject={"id": "x"}, workspaces=["biz-a"])
        assert daemon.resolve(tok)[1].kid == cli.kid
        _, mine = daemon.issue(subject={"id": "y"}, workspaces=["biz-a"])
        assert tokens.inspect(mine).authority["kid"] == cli.kid
        again, _ = make_service(tmp_path)
        assert again.approvals._link_key == link_key  # review and deny links survive the rotation
        again.close()
    finally:
        daemon.close()
        cli.close()


# ------------------------------------------------------------------ the v1 migration window

def test_v1_grants_work_until_the_first_rotation(tmp_path):
    svc, _ = make_service(tmp_path)
    try:
        g, v1 = issue_v1(svc, subject={"id": "legacy"})
        assert v1.startswith("agt1_")
        p, held = svc.resolve(v1)
        assert p.id == "legacy" and held.version == 1
        _, child = svc.attenuate(v1, ceiling="read")
        assert child.startswith("agt1_")  # a v1 chain narrows as v1
        svc.resolve(child)
        out = svc.rotate_key()
        assert out["v1_grants_ended"] == 2
        for t in (v1, child):
            with pytest.raises(Denied):
                svc.resolve(t)
        assert reason(lambda: svc.grant_by_id(g.id)) == "retired_key"
    finally:
        svc.close()


def test_v1_can_be_turned_off(tmp_path):
    svc, _ = make_service(tmp_path, config=PolicyConfig(accept_v1_tokens=False))
    try:
        g, v1 = issue_v1(svc, subject={"id": "legacy"})
        assert reason(lambda: svc.resolve(v1)) == "invalid_token"
        assert reason(lambda: svc.grant_by_id(g.id)) == "invalid_token"
    finally:
        svc.close()


def test_a_registry_from_before_v2_is_migrated(tmp_path):
    """An old policy.db: grants without v or block_sha256, revocations keyed by rid."""
    import sqlite3

    svc, _ = make_service(tmp_path)
    g, v1 = issue_v1(svc, subject={"id": "legacy"})
    svc.close()
    db = sqlite3.connect(tmp_path / "policy.db")
    db.executescript("""
        DROP TABLE revoked;
        CREATE TABLE revocations (rid TEXT PRIMARY KEY, grant_id TEXT NOT NULL, revoked_at REAL NOT NULL,
                                  revoked_by TEXT, reason TEXT);
        DROP INDEX grants_rid_v2;
        ALTER TABLE grants DROP COLUMN v;
        ALTER TABLE grants DROP COLUMN block_sha256;
    """)
    db.execute("INSERT INTO revocations VALUES (?, ?, ?, 'owner', NULL)", (g.blocks[0].rid, g.id, T0))
    db.commit()
    db.close()
    again, _ = make_service(tmp_path)
    try:
        assert reason(lambda: again.resolve(v1)) == "revoked"
        _, fresh = again.issue(subject={"id": "new"}, workspaces=["biz-a"])
        again.resolve(fresh)
    finally:
        again.close()


# ------------------------------------------------------------------ org grants (issuers.py)

@pytest.fixture
def node(tmp_path):
    org = Ed25519PrivateKey.generate()
    config = NodeConfig(home=tmp_path, mcp_token="owner-token-x", node_id=NODE, org_id=ORG)
    runtime = Runtime(config=config)
    issuers = TrustedIssuers(tmp_path)
    issuers.trust(f"org:{ORG}", signing.public_b64(org))
    runtime.register(SERVICE_NAME, issuers)
    policy = build(runtime)
    yield policy, issuers, org
    policy.close()


def org_token(key, *, aud=None, kid=None, iss=f"org:{ORG}", **fields):
    block = {"v": 2, "id": new_id("gr"), "iat": int(time.time()), "iss": iss, "aud": aud or [f"node:{NODE}"],
             "kid": kid or tokens.key_id(key), "sub": {"kind": "agent", "id": "helper", "owner": "owner"},
             "workspaces": ["personal"], "ceiling": "draft", "exp": int(time.time()) + 600, "depth": 2, **fields}
    return tokens.mint(key, GrantBlock.model_validate(block).to_data()), block


def test_an_org_issued_v2_token_verifies_on_the_node(node):
    policy, _, org = node
    token, block = org_token(org)
    principal, grant = policy.resolve(token)
    assert principal.id == "helper" and grant.id == block["id"] and grant.extra["issuer"] == f"org:{ORG}"
    assert grant.lane_allowed("personal/reddit") and not grant.effect_allowed(Effect.COMMIT)
    policy.resolve(org_token(org, aud=[f"org:{ORG}"])[0])  # org-wide


def test_an_org_token_for_another_node_or_org_is_refused(node):
    policy, _, org = node
    assert reason(lambda: policy.resolve(org_token(org, aud=["node:node_other"])[0])) == "audience"
    assert reason(lambda: policy.resolve(org_token(org, aud=["org:org_other"])[0])) == "audience"


def test_an_unknown_issuer_or_kid_is_refused(node):
    policy, _, org = node
    other = Ed25519PrivateKey.generate()
    assert reason(lambda: policy.resolve(org_token(other)[0])) == "unknown_key"
    assert reason(lambda: policy.resolve(org_token(other, iss="org:org_other")[0])) == "unknown_issuer"
    # the trusted kid, but signed with another key
    assert reason(lambda: policy.resolve(org_token(other, kid=tokens.key_id(org))[0])) == "invalid_token"


def test_an_org_key_rotation_overlaps_then_retires(tmp_path):
    clock = Clock(time.time())
    issuers = TrustedIssuers(tmp_path, clock=clock)
    old, new = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    old_kid = issuers.trust(f"org:{ORG}", signing.public_b64(old))
    config = NodeConfig(home=tmp_path, mcp_token="owner-token-x", node_id=NODE, org_id=ORG)
    runtime = Runtime(config=config)
    runtime.register(SERVICE_NAME, issuers)
    policy = build(runtime)
    try:
        token, _ = org_token(old)
        issuers.trust(f"org:{ORG}", signing.public_b64(new), overlap_s=300)
        policy.resolve(token)
        policy.resolve(org_token(new)[0])
        assert {k["kid"]: k["state"] for k in issuers.keys(f"org:{ORG}")}[old_kid] == "retiring"
        # the overlap survives a restart
        assert {k["kid"] for k in TrustedIssuers(tmp_path, clock=clock).keys(f"org:{ORG}")} == {
            old_kid, tokens.key_id(new)}
        clock.advance(301)
        assert reason(lambda: policy.resolve(token)) == "retired_key"
        policy.resolve(org_token(new)[0])
        issuers.retire(f"org:{ORG}", tokens.key_id(new))
        assert reason(lambda: policy.resolve(org_token(new)[0])) == "retired_key"
    finally:
        policy.close()


def test_revoking_an_org_parent_cascades_to_offline_children(node):
    policy, issuers, org = node
    token, block = org_token(org)
    child = tokens.append(token, {"v": 2, "id": new_id("gr"), "parent": block["id"], "iat": int(time.time()),
                                  "label": "sub", "kind": "agent", "ceiling": "read"})
    p, grant = policy.resolve(child)
    assert p.id == "helper/sub" and grant.depth == 1
    issuers.revoke([block["id"]])  # the controller sends grant ids
    assert reason(lambda: policy.resolve(child)) == "revoked"
    assert reason(lambda: policy.resolve(token)) == "revoked"
    # or the derived revocation id of the root block (every copy of the token carries it)
    other, other_block = org_token(org)
    other_child = tokens.append(other, {"v": 2, "id": new_id("gr"), "parent": other_block["id"],
                                        "iat": int(time.time())})
    policy.resolve(other_child)
    issuers.revoke([tokens.inspect(other).blocks[0].rid])
    assert reason(lambda: policy.resolve(other_child)) == "revoked"


def test_v1_org_tokens_still_verify_in_the_window(node):
    policy, _, org = node
    block = {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "iat": int(time.time()),
             "sub": {"kind": "agent", "id": "helper", "owner": "owner"}, "workspaces": ["personal"],
             "ceiling": "draft", "exp": int(time.time()) + 600}
    token = tokens.mint(org, GrantBlock.model_validate(block).to_data())
    assert policy.resolve(token)[0].id == "helper"
