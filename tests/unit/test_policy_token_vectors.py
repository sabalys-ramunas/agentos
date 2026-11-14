"""Grant token test vectors (tests/fixtures/tokens/vectors.json, specified in
src/agentos/policy/TOKENS.md): fixed keys, tokens and the expected verification result, so
another implementation (the cloud's minting service) can check itself byte for byte.

`build_vectors()` makes every vector from fixed seeds. One test checks that the fixture is
exactly what it builds (the format did not drift); another verifies each vector the way a
node does and compares the outcome. To rewrite the fixture after an intended format change:
`AGENTOS_WRITE_TOKEN_VECTORS=1 uv run pytest tests/unit/test_policy_token_vectors.py`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentos.policy import tokens
from agentos.policy.errors import Denied
from agentos.policy.grants import Grant, blocks_from_token
from agentos.policy.util import b64u, b64u_decode

FIXTURE = Path(__file__).parent.parent / "fixtures" / "tokens" / "vectors.json"
ISSUER = "org:org_vectors"
AUDIENCES = ["node:node_vectors", "org:org_vectors"]


def _key(name: str) -> Ed25519PrivateKey:
    """Seed = SHA-256("agentos token vector " + name)."""
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(f"agentos token vector {name}".encode()).digest())


def _pub(name: str) -> str:
    return b64u(_key(name).public_key().public_bytes_raw())


def _raw_append(token: str, data: bytes, next_name: str) -> str:
    """Append a block whose bytes are exactly `data` (for refused encodings)."""
    version, blocks, _, proof = tokens._decode(token)
    holder = Ed25519PrivateKey.from_private_bytes(proof)
    nxt = _key(next_name)
    nxt_pub = nxt.public_key().public_bytes_raw()
    sig = holder.sign(tokens._payload(version, len(blocks), data, nxt_pub, blocks[-1][2]))
    return tokens._encode(version, [*blocks, (data, nxt_pub, sig)], tokens._PROOF_KEY, nxt.private_bytes_raw())


def _authority(**over) -> dict:
    block = {"v": 2, "id": "gr_vectorroot00000", "iat": 1_800_000_000, "exp": 1_900_000_000,
             "iss": ISSUER, "aud": ["node:node_vectors"], "kid": tokens.key_id(_key("root")),
             "sub": {"kind": "agent", "id": "claude-code", "owner": "owner"},
             "workspaces": ["biz-a"], "ceiling": "commit", "budget": {"usd": 2.5, "tokens": 100000},
             "depth": 2}
    block.update(over)
    return block


CHILD = {"v": 2, "id": "gr_vectorchild0000", "parent": "gr_vectorroot00000", "iat": 1_800_000_100,
         "label": "codex-worker", "kind": "agent", "lanes": ["biz-a/insta"], "ceiling": "read"}


def _describe(token: str) -> dict:
    opened = tokens.inspect(token)
    return {"version": opened.version, "sealed": opened.sealed,
            "blocks": [{"canonical": b.raw.decode(), "rid": b.rid, "sig": b64u(b.sig)} for b in opened.blocks]}


def build_vectors() -> dict:
    root = _key("root")
    plain = tokens.mint(root, _authority(), next_key=_key("next-0"))
    narrowed = tokens.append(plain, CHILD, next_key=_key("next-1"))
    _, blocks, _, _ = tokens._decode(plain)
    data0, next0, _ = blocks[0]
    v1 = tokens.mint(root, {"v": 1, "id": "gr_vectorv1root000", "rid": "rv_vectorv1root000", "iat": 1_800_000_000,
                            "exp": 1_900_000_000, "sub": {"kind": "agent", "id": "claude-code", "owner": "owner"},
                            "workspaces": ["biz-a"], "ceiling": "read"}, next_key=_key("next-0"))
    flipped = bytearray(b64u_decode(plain[5:]))
    flipped[9] ^= 0x01  # inside the authority block's data
    child_text = tokens.canonical(CHILD).decode()
    vectors = [
        ("v2 authority block", plain, "valid"),
        ("v2 attenuated", narrowed, "valid"),
        ("v2 sealed", tokens.seal(narrowed), "valid"),
        ("v1 authority block (migration window)", v1, "valid"),
        ("data byte flipped", "agt2_" + b64u(bytes(flipped)), "invalid_token"),
        ("block stripped", tokens._encode(2, list(tokens._decode(narrowed)[1][:1]), tokens._PROOF_KEY,
                                          tokens._decode(narrowed)[3]), "invalid_token"),
        ("prefix names another version", "agt1_" + narrowed[5:], "invalid_token"),
        ("child with whitespace", _raw_append(plain, child_text.replace(",", ", ").encode(), "next-1"),
         "invalid_token"),
        ("child with duplicate keys", _raw_append(plain, child_text.replace('"ceiling":"read"',
                                                  '"ceiling":"read","ceiling":"commit"').encode(), "next-1"),
         "invalid_token"),
        ("child with keys out of order", _raw_append(plain, json.dumps(CHILD, separators=(",", ":")).encode(),
                                                     "next-1"), "invalid_token"),
        ("child chooses its revocation id", tokens.append(plain, {**CHILD, "rid": "rv_vectorchild0000"},
                                                          next_key=_key("next-1")), "invalid_token"),
        ("child replaces the subject", tokens.append(plain, {**{k: v for k, v in CHILD.items()
                                                                if k not in ("label", "kind")},
                                                             "sub": {"kind": "human", "id": "owner", "owner": "owner"}},
                                                     next_key=_key("next-1")), "invalid_token"),
        ("unknown key id", tokens.mint(_key("other-org"), _authority(kid=tokens.key_id(_key("other-org"))),
                                       next_key=_key("next-0")), "unknown_key"),
        ("kid of a trusted key, signed by another", tokens.mint(_key("other-org"), _authority(),
                                                                next_key=_key("next-0")), "invalid_token"),
        ("unknown issuer", tokens.mint(root, _authority(iss="org:org_elsewhere"), next_key=_key("next-0")),
         "unknown_issuer"),
        ("audience is another node", tokens.mint(root, _authority(aud=["node:node_other"]),
                                                 next_key=_key("next-0")), "audience"),
        ("org-wide audience", tokens.mint(root, _authority(aud=["org:org_vectors"]), next_key=_key("next-0")),
         "valid"),
        ("child narrows to this node", tokens.append(tokens.mint(root, _authority(aud=["org:org_vectors"]),
                                                                 next_key=_key("next-0")),
                                                     {**CHILD, "aud": ["node:node_vectors"]}, next_key=_key("next-1")),
         "valid"),
        ("child names another node", tokens.append(tokens.mint(root, _authority(aud=["org:org_vectors"]),
                                                               next_key=_key("next-0")),
                                                   {**CHILD, "aud": ["node:node_other"]}, next_key=_key("next-1")),
         "audience"),
        ("authority without a key id", tokens.mint(root, {k: v for k, v in _authority().items() if k != "kid"},
                                                   next_key=_key("next-0")), "unknown_key"),
    ]
    out = []
    for name, token, expect in vectors:
        entry = {"name": name, "token": token, "expect": expect}
        if expect == "valid":
            entry["decoded"] = _describe(token)
        out.append(entry)
    return {
        "about": "Grant token test vectors; see src/agentos/policy/TOKENS.md. Keys are Ed25519 with "
                 "seed = SHA-256(\"agentos token vector \" + name); public keys are unpadded base64url.",
        "keys": {name: {"seed": hashlib.sha256(f"agentos token vector {name}".encode()).hexdigest(),
                        "public": _pub(name), "kid": tokens.key_id(_key(name))}
                 for name in ("root", "next-0", "next-1", "other-org")},
        "verifier": {"trusted": [{"iss": ISSUER, "kid": tokens.key_id(root), "public": _pub("root")}],
                     "audiences": AUDIENCES, "v1_key": _pub("root")},
        "signing": {
            "authority_block": json.loads(data0),
            "authority_canonical": data0.decode(),
            "authority_signature_input_hex": tokens._payload(2, 0, data0, next0, b"").hex(),
            "child_block": CHILD,
            "child_canonical": child_text,
        },
        "vectors": out,
    }


def verify_like_a_node(token: str, verifier: dict) -> str:
    """The outcome a node reaches for `token` (TOKENS.md, "Verifying"): valid, or a reason."""
    trusted = {(k["iss"], k["kid"]): k["public"] for k in verifier["trusted"]}

    def key_for(version: int, authority: dict):
        from agentos.policy.issuers import load_key
        if version < 2:
            return load_key(verifier["v1_key"])
        if not any(iss == authority.get("iss") for iss, _ in trusted):
            raise Denied("unknown_issuer", "not trusted")
        pub = trusted.get((authority.get("iss"), authority.get("kid")))
        if pub is None:
            raise Denied("unknown_key", "not trusted")
        return load_key(pub)

    try:
        opened = tokens.open_token(token, key_for)
        grant = Grant(blocks_from_token(opened))
        grant.check_structure()
        if not grant.audience_allows(set(verifier["audiences"])):
            raise Denied("audience", "another node")
    except Denied as e:
        return e.reason
    return "valid"


def test_fixture_is_what_the_format_builds():
    built = build_vectors()
    if os.environ.get("AGENTOS_WRITE_TOKEN_VECTORS"):
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(built, indent=2, ensure_ascii=False) + "\n")
    assert json.loads(FIXTURE.read_text()) == built


@pytest.mark.parametrize("vector", json.loads(FIXTURE.read_text())["vectors"], ids=lambda v: v["name"])
def test_vector_verifies_as_expected(vector):
    fixture = json.loads(FIXTURE.read_text())
    assert verify_like_a_node(vector["token"], fixture["verifier"]) == vector["expect"]
    if vector["expect"] == "valid":
        opened = tokens.inspect(vector["token"])
        for block, want in zip(opened.blocks, vector["decoded"]["blocks"], strict=True):
            assert block.raw.decode() == want["canonical"]
            assert block.rid == want["rid"]
            if opened.version >= 2:
                assert want["rid"] == tokens.revocation_id(b64u_decode(want["sig"]))


def test_signature_input_matches_the_documented_layout():
    fixture = json.loads(FIXTURE.read_text())
    signing = fixture["signing"]
    data = signing["authority_canonical"].encode()
    assert tokens.canonical(signing["authority_block"]) == data
    next_pub = b64u_decode(fixture["keys"]["next-0"]["public"])
    expected = b"agentos.grant.block.v2\x00" + (0).to_bytes(4, "big") + len(data).to_bytes(4, "big") + data + next_pub
    assert bytes.fromhex(signing["authority_signature_input_hex"]) == expected
