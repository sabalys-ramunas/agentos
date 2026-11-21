"""Grants from the controller's org: verified on the node, revocations cascade, offline rule."""

import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openajar.core import signing
from openajar.core.config import NodeConfig
from openajar.core.models import Effect
from openajar.core.runtime import Runtime
from openajar.policy import tokens
from openajar.policy.errors import Denied
from openajar.policy.grants import GrantBlock
from openajar.policy.issuers import SERVICE_NAME, TrustedIssuers
from openajar.policy.service import build
from openajar.policy.util import new_id


@pytest.fixture
def node(tmp_path):
    org = Ed25519PrivateKey.generate()
    config = NodeConfig(home=tmp_path, mcp_token="owner-token-x")
    runtime = Runtime(config=config)
    issuers = TrustedIssuers(tmp_path)
    issuers.trust("org:test", signing.public_b64(org))
    runtime.register(SERVICE_NAME, issuers)
    policy = build(runtime)
    yield policy, issuers, org
    policy.close()


def mint(key, **fields):
    block = {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "iat": int(time.time()),
             "sub": {"kind": "agent", "id": "helper", "owner": "owner"}, "workspaces": ["personal"],
             "ceiling": "draft", "exp": int(time.time()) + 600, "depth": 2, **fields}
    return tokens.mint(key, GrantBlock.model_validate(block).to_data()), block


def reason(call):
    with pytest.raises(Denied) as raised:
        call()
    return raised.value.reason


def test_an_org_grant_verifies_offline_with_its_scope(node):
    policy, _, org = node
    token, block = mint(org)
    principal, grant = policy.resolve(token)
    assert principal.id == "helper" and grant.id == block["id"]
    assert grant.lane_allowed("personal/reddit") and not grant.lane_allowed("work/x")
    assert grant.effect_allowed(Effect.DRAFT) and not grant.effect_allowed(Effect.COMMIT)
    assert reason(lambda: policy.resolve(mint(Ed25519PrivateKey.generate())[0])) == "invalid_token"


def test_the_nodes_own_grants_still_need_registration(node):
    policy, _, _ = node
    _, own = policy.issue(subject={"id": "local"}, workspaces=["personal"])
    assert policy.resolve(own)[0].id == "local"
    forged = tokens.mint(policy.key, mint(Ed25519PrivateKey.generate())[1] | {"exp": int(time.time()) + 60})
    assert reason(lambda: policy.resolve(forged)) == "unregistered"


def test_revocation_by_grant_id_or_revocation_id_cascades_to_narrowed_tokens(node):
    policy, issuers, org = node
    token, block = mint(org)
    child = tokens.append(token, {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "parent": block["id"],
                                  "iat": int(time.time()), "ceiling": "read"})
    _, grant = policy.resolve(child)
    assert grant.depth == 1
    issuers.revoke([block["rid"]])
    assert reason(lambda: policy.resolve(child)) == "revoked"
    assert reason(lambda: policy.resolve(token)) == "revoked"
    other, other_block = mint(org)
    issuers.revoke([other_block["id"]])
    assert reason(lambda: policy.resolve(other)) == "revoked"
    # already-resolved grants are checked again on every authorize
    live, live_block = mint(org)
    _, held = policy.resolve(live)
    issuers.revoke([live_block["rid"]])
    assert reason(lambda: policy.check_live(held)) == "revoked"


def test_revocations_persist(tmp_path):
    first = TrustedIssuers(tmp_path)
    assert first.revoke(["rv_a", "rv_a", "", 5]) == ["rv_a"]
    first.close()
    assert TrustedIssuers(tmp_path).is_revoked(["rv_a"])


def test_while_disconnected_only_grants_already_seen_work(node):
    policy, issuers, org = node
    online = [True]
    issuers.connected = lambda: online[0]
    seen, _ = mint(org)
    policy.resolve(seen)
    online[0] = False
    assert policy.resolve(seen)[0].id == "helper"
    assert reason(lambda: policy.resolve(mint(org)[0])) == "degraded"
    online[0] = True
    assert policy.resolve(mint(org)[0])[0].id == "helper"


def test_a_controller_grant_never_carries_admin(node):
    policy, _, org = node
    token, _ = mint(org, admin=True)
    assert reason(lambda: policy.resolve(token)) == "foreign_admin"


def test_without_a_trusted_issuer_nothing_foreign_verifies(tmp_path):
    runtime = Runtime(config=NodeConfig(home=tmp_path, mcp_token="owner-token-y"))
    policy = build(runtime)
    try:
        assert reason(lambda: policy.resolve(mint(Ed25519PrivateKey.generate())[0])) == "invalid_token"
    finally:
        policy.close()
