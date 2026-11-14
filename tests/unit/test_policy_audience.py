"""The audience caveat (`aud` on any grant block): a token names the node it is for, a node refuses
one for another node, and narrowing can only keep or tighten it, never drop or move it."""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentos.core.config import NodeConfig
from agentos.core.errors import InvalidRequest
from agentos.core.runtime import Runtime
from agentos.node.channel import wire
from agentos.policy import tokens
from agentos.policy.errors import Denied
from agentos.policy.grants import GrantBlock
from agentos.policy.issuers import SERVICE_NAME, TrustedIssuers
from agentos.policy.service import build
from agentos.policy.util import new_id

ORG = "org_test"
HERE = "node_here"
THERE = "node_there"


def reason(call) -> str:
    with pytest.raises(Denied) as raised:
        call()
    return raised.value.reason


def node(home, org_key, node_id):
    home.mkdir(parents=True, exist_ok=True)
    runtime = Runtime(config=NodeConfig(home=home, mcp_token="owner-token-x", node_id=node_id, org_id=ORG))
    issuers = TrustedIssuers(home)
    issuers.trust(f"org:{ORG}", wire.public_b64(org_key))
    runtime.register(SERVICE_NAME, issuers)
    return build(runtime)


@pytest.fixture
def nodes(tmp_path):
    org = Ed25519PrivateKey.generate()
    here, there = node(tmp_path / "here", org, HERE), node(tmp_path / "there", org, THERE)
    yield here, there, org
    here.close()
    there.close()


def v1_token(key, *, aud=None):
    """A call token as the cloud mints it: a v1 authority block, optionally naming its node."""
    block = {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "iat": int(time.time()),
             "sub": {"kind": "agent", "id": "panel-call", "owner": "owner"}, "lanes": ["personal/reddit"],
             "ceiling": "read", "exp": int(time.time()) + 60}
    if aud is not None:
        block["aud"] = aud
    return tokens.mint(key, GrantBlock.model_validate(block).to_data()), block


def v1_child(token, parent, **fields):
    return tokens.append(token, {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "parent": parent["id"],
                                 "iat": int(time.time()), **fields})


def v2_token(key, *, aud):
    block = {"v": 2, "id": new_id("gr"), "iat": int(time.time()), "iss": f"org:{ORG}", "aud": aud,
             "kid": tokens.key_id(key), "sub": {"kind": "agent", "id": "helper", "owner": "owner"},
             "workspaces": ["personal"], "ceiling": "read", "exp": int(time.time()) + 600}
    return tokens.mint(key, GrantBlock.model_validate(block).to_data()), block


def v2_child(token, parent, **fields):
    return tokens.append(token, {"v": 2, "id": new_id("gr"), "parent": parent["id"], "iat": int(time.time()),
                                 **fields})


def test_a_v1_call_token_names_its_node_and_another_node_refuses_it(nodes):
    here, there, org = nodes
    token, _ = v1_token(org, aud=[f"node:{HERE}"])
    assert here.resolve(token)[0].id == "panel-call"
    assert reason(lambda: there.resolve(token)) == "audience"
    unnamed, _ = v1_token(org)  # a token without aud keeps working on any node of the org
    here.resolve(unnamed)
    there.resolve(unnamed)
    org_wide, _ = v1_token(org, aud=[f"org:{ORG}"])
    there.resolve(org_wide)
    assert reason(lambda: here.resolve(v1_token(org, aud=["org:org_other"])[0])) == "audience"


def test_a_narrowed_child_cannot_drop_or_change_the_audience(nodes):
    here, there, org = nodes
    token, block = v1_token(org, aud=[f"node:{HERE}"])
    dropped = v1_child(token, block, ceiling="read")  # no aud of its own
    assert reason(lambda: there.resolve(dropped)) == "audience"  # the parent's aud still holds
    here.resolve(dropped)
    moved = v1_child(token, block, aud=[f"node:{THERE}"])
    assert reason(lambda: there.resolve(moved)) == "audience"
    assert reason(lambda: here.resolve(moved)) == "audience"  # every block must name the node
    widened = v1_child(token, block, aud=[f"org:{ORG}"])
    assert reason(lambda: there.resolve(widened)) == "audience"


def test_a_v2_child_narrows_an_org_audience_to_one_node(nodes):
    here, there, org = nodes
    token, block = v2_token(org, aud=[f"org:{ORG}"])
    pinned = v2_child(token, block, aud=[f"node:{HERE}"])
    here.resolve(pinned)
    assert reason(lambda: there.resolve(pinned)) == "audience"
    there.resolve(token)  # the parent itself is still org-wide


def test_attenuating_on_the_node_keeps_or_narrows_the_audience(nodes):
    here, there, org = nodes
    token, _ = v2_token(org, aud=[f"org:{ORG}"])
    _, pinned = here.attenuate(token, audience=[f"node:{HERE}"])
    here.resolve(pinned)
    assert reason(lambda: there.resolve(pinned)) == "audience"
    with pytest.raises(InvalidRequest, match="aud"):
        here.attenuate(pinned, audience=[f"node:{THERE}"])  # moving it is a widening
    with pytest.raises(InvalidRequest, match="aud"):
        here.attenuate(pinned, audience=[f"org:{ORG}"])
    _, kept = here.attenuate(pinned, ceiling="read")  # no audience asked: the parent's stays
    assert reason(lambda: there.resolve(kept)) == "audience"


def test_audience_values_are_checked_when_a_block_is_read():
    with pytest.raises(ValueError, match="audience"):
        GrantBlock.model_validate({"v": 1, "id": "gr_abcdefgh", "rid": "rv_abcdefgh", "iat": 1, "aud": ["node_x"]})
    with pytest.raises(ValueError, match="once"):
        GrantBlock.model_validate({"v": 1, "id": "gr_abcdefgh", "rid": "rv_abcdefgh", "iat": 1,
                                   "aud": ["node:a", "node:a"]})
    with pytest.raises(ValueError):
        GrantBlock.model_validate({"v": 1, "id": "gr_abcdefgh", "rid": "rv_abcdefgh", "iat": 1, "aud": []})
