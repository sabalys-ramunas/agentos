"""Security review of the grant token format.

Attacks on the wire format and the registry: forging, stripping, reordering, splicing,
reopening a sealed token, signature and encoding malleability, framing, revocation ids,
audience binding and canonical block encoding.

A test marked `xfail(strict=True, reason="SEC-<n>: ...")` demonstrates an open finding:
it fails today and passes once the finding is fixed (then drop the marker). The other
tests pin properties that hold and must keep holding.
"""

from __future__ import annotations

import json
import shutil

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_policy_support import T0, Clock, issue_v1, make_service

from agentos.core.errors import InvalidRequest
from agentos.core.models import Effect
from agentos.core.tools import ToolSpec
from agentos.policy import tokens
from agentos.policy.errors import Denied, TokenInvalid
from agentos.policy.util import b64u, b64u_decode
from agentos.policy.wrap import ToolCallError, bind_request_token, wrap_tool

_L = 2**252 + 27742317777372353535851937790883648493  # the Ed25519 group order
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


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


def child(parent_id: str, letter: str, **fields) -> dict:
    return {"id": f"gr_{letter * 16}", "v": 2, "parent": parent_id, "iat": int(T0), **fields}


def append_raw(token: str, data: bytes) -> str:
    """Append a block whose bytes are exactly `data`, signed with the holder's proof key."""
    version, blocks, _, proof = tokens._decode(token)
    holder = Ed25519PrivateKey.from_private_bytes(proof)
    nxt = Ed25519PrivateKey.generate()
    nxt_pub = nxt.public_key().public_bytes_raw()
    sig = holder.sign(tokens._payload(version, len(blocks), data, nxt_pub, blocks[-1][2]))
    return tokens._encode(version, [*blocks, (data, nxt_pub, sig)], tokens._PROOF_KEY, nxt.private_bytes_raw())


# ------------------------------------------------------------------ properties that hold

def test_proof_key_cannot_sign_an_authority_block(svc):
    """The holder owns the proof key; signing a wider authority block with it must fail."""
    g, tok = issue(svc)
    _, _, _, proof = tokens._decode(tok)
    holder = Ed25519PrivateKey.from_private_bytes(proof)
    wider = {**g.blocks[0].to_data(), "ceiling": "irreversible", "admin": True, "workspaces": ["*"]}
    with pytest.raises(TokenInvalid):
        svc.resolve(tokens.mint(holder, wider))


def test_holder_of_a_child_cannot_rewrite_or_drop_its_block(svc):
    """A sub-agent holds only the key after its own block, never the key that signed it."""
    _, tok = issue(svc)
    _, narrow = svc.attenuate(tok, ceiling="read", workspaces=["biz-a"])
    v, blocks, _, proof = tokens._decode(narrow)
    holder = Ed25519PrivateKey.from_private_bytes(proof)
    wider = tokens.canonical({**json.loads(blocks[1][0]), "ceiling": "commit"})
    nxt = Ed25519PrivateKey.generate()
    nxt_pub = nxt.public_key().public_bytes_raw()
    sig = holder.sign(tokens._payload(v, 1, wider, nxt_pub, blocks[0][2]))
    rewritten = tokens._encode(v, [blocks[0], (wider, nxt_pub, sig)], tokens._PROOF_KEY, nxt.private_bytes_raw())
    dropped = tokens._encode(v, [blocks[0]], tokens._PROOF_KEY, proof)
    for forged in (rewritten, dropped):
        with pytest.raises(TokenInvalid):
            svc.resolve(forged)


def test_blocks_cannot_be_reordered(svc):
    g, tok = issue(svc)
    a = tokens.append(tok, child(g.id, "a", ceiling="draft"))
    ab = tokens.append(a, child("gr_" + "a" * 16, "b", ceiling="read"))
    v, blocks, tag, proof = tokens._decode(ab)
    swapped = tokens._encode(v, [blocks[0], blocks[2], blocks[1]], tag, proof)
    with pytest.raises(TokenInvalid):
        svc.resolve(swapped)


def test_blocks_from_other_tokens_cannot_be_spliced(svc):
    g1, t1 = issue(svc)
    g2, t2 = issue(svc, subject={"id": "other"}, workspaces=["biz-b"])
    other_child = tokens._decode(tokens.append(t2, child(g2.id, "x")))[1][1]
    # another token's attenuation block after this token's authority block
    v, b1, _, p1 = tokens._decode(t1)
    with pytest.raises(TokenInvalid):
        svc.resolve(tokens._encode(v, [b1[0], other_child], tokens._PROOF_KEY, p1))
    # a grandchild from one branch grafted onto a sibling branch of the same token
    a = tokens.append(t1, child(g1.id, "a"))
    a_grand = tokens._decode(tokens.append(a, child("gr_" + "a" * 16, "c")))[1][2]
    _, bb, _, pb = tokens._decode(tokens.append(t1, child(g1.id, "b")))
    with pytest.raises(TokenInvalid):
        svc.resolve(tokens._encode(v, [*bb, a_grand], tokens._PROOF_KEY, pb))


def test_sealed_token_cannot_be_reopened(svc):
    _, tok = issue(svc)
    sealed = tokens.seal(tok)
    assert tokens.is_sealed(sealed) and svc.resolve(sealed)
    v, blocks, _, _ = tokens._decode(sealed)
    reopened = tokens._encode(v, blocks, tokens._PROOF_KEY, Ed25519PrivateKey.generate().private_bytes_raw())
    with pytest.raises(TokenInvalid):
        svc.resolve(reopened)
    with pytest.raises(TokenInvalid):
        tokens.append(sealed, {"v": 2, "id": "gr_" + "z" * 16})


def _malleate(sig: bytes) -> bytes:
    s = int.from_bytes(sig[32:], "little") + _L
    assert s < 2**256
    return sig[:32] + s.to_bytes(32, "little")


def test_malleated_signatures_are_rejected(svc):
    """S + L is the same Ed25519 point equation; a strict verifier must refuse it, or one
    grant would have many valid encodings."""
    _, tok = issue(svc)
    v, blocks, tag, proof = tokens._decode(tok)
    data, nxt, sig = blocks[0]
    with pytest.raises(TokenInvalid):
        tokens.verify(tokens._encode(v, [(data, nxt, _malleate(sig))], tag, proof), svc.public_key)
    _, sealed_blocks, stag, sproof = tokens._decode(tokens.seal(tok))
    with pytest.raises(TokenInvalid):
        tokens.verify(tokens._encode(v, sealed_blocks, stag, _malleate(sproof)), svc.public_key)


def _raw(tok: str) -> bytearray:
    return bytearray(b64u_decode(tok[len(tokens.PREFIX):]))


def _frame(raw: bytes) -> str:
    return tokens.PREFIX + b64u(bytes(raw))


FRAMING = {
    "trailing byte": lambda r: r + b"\x00",
    "no blocks": lambda r: r[:3] + b"\x00" + r[4:],
    "too many blocks": lambda r: r[:3] + bytes([tokens.MAX_BLOCKS + 1]) + r[4:],
    "count larger than present": lambda r: r[:3] + b"\x02" + r[4:],
    "huge length": lambda r: r[:4] + b"\xff\xff\xff\xff" + r[8:],
    "other version": lambda r: r[:2] + b"\x01" + r[3:],
    "unknown version": lambda r: r[:2] + b"\x03" + r[3:],
    "unknown proof tag": lambda r: r[:-33] + b"\x03" + r[-32:],
}


@pytest.mark.parametrize("name", list(FRAMING))
def test_framing_is_strict(svc, name):
    _, tok = issue(svc)
    with pytest.raises(TokenInvalid):
        svc.resolve(_frame(FRAMING[name](_raw(tok))))


@pytest.mark.parametrize("text", ["agt1_", "agt1_====", "agt1_ab cd", "agt1_abcé", "AGT1_abcd", "x" * 40_000,
                                  "agt2_", "agt2_====", "agt3_abcd", "AGT2_abcd"])
def test_garbage_tokens_fail_closed(svc, text):
    with pytest.raises(Denied):
        svc.resolve(text)


def test_revocation_holds_through_reencoding_sealing_and_offline_children(svc):
    """Many strings name one grant: a sealed copy and offline children (and in v1 a re-encoded
    token with base64 trailing bits, which v2 refuses). Revocation is by grant id and derived
    revocation id, so none of them escapes."""
    g, tok = issue(svc)
    variants = [tokens.seal(tok), tokens.append(tok, child(g.id, "o"))]
    for n in range(3):  # find a token whose base64 has spare bits, then flip one
        t = tokens.append(tok, child(g.id, "p", note="x" * n))
        body = t[len(tokens.PREFIX):]
        if len(body) % 4 in (2, 3):
            alt = tokens.PREFIX + body[:-1] + _B64[_B64.index(body[-1]) ^ 1]
            assert alt != t and b64u_decode(alt[5:]) == b64u_decode(body)
            with pytest.raises(TokenInvalid):
                svc.resolve(alt)  # v2: one string per token
            variants.append(t)
            break
    for v in variants:
        svc.resolve(v)
    svc.revoke(g.id)
    for v in variants:
        with pytest.raises(Denied) as e:
            svc.resolve(v)
        assert e.value.reason == "revoked"


def test_offline_child_cannot_outlive_its_parent(tmp_path):
    clock = Clock()
    svc, _ = make_service(tmp_path, clock=clock)
    g, tok = issue(svc, ttl_s=3600)
    longer = tokens.append(tok, child(g.id, "l", exp=int(T0) + 10 * 86400))
    svc.resolve(longer)
    clock.advance(3600 + svc.cfg.clock_skew_s + 1)
    with pytest.raises(Denied) as e:
        svc.resolve(longer)
    assert e.value.reason == "expired"


async def test_tokens_never_reach_results_errors_or_stored_files(tmp_path):
    svc, rt = make_service(tmp_path)
    _, tok = issue(svc)

    async def h_echo(ctx, *, note: str) -> dict:
        """Echo a note."""
        await ctx.authorize(Effect.READ)
        return {"note": note}

    fn = wrap_tool(ToolSpec(name="echo", handler=h_echo, lane_arg=None), rt)
    with bind_request_token(tok, session="s-1"):
        out = await fn(note=f"my token is {tok}")
    assert tok not in json.dumps(out)
    bad = tok[:-6] + "AAAAAA"
    with bind_request_token(bad, session="s-1"), pytest.raises(ToolCallError) as e:
        await fn(note=f"and this one {bad}")
    assert bad not in str(e.value)
    svc.traces.flush()
    svc.close()
    stored = b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and "secrets" not in p.parts)
    for t in (tok, bad):
        assert t.encode() not in stored and t[5:60].encode() not in stored


# ------------------------------------------------------------------ open findings

def test_revoking_one_grant_never_revokes_an_unrelated_one(svc):
    """SEC-6 (fixed in token v2): a v2 block cannot carry a revocation id; each block's id is
    derived from its signature and unique in the registry."""
    victim, victim_tok = issue(svc, subject={"id": "victim"})
    attacker, attacker_tok = issue(svc, subject={"id": "attacker"}, workspaces=["biz-b"])
    copied = tokens.append(attacker_tok, {**child(attacker.id, "k"), "rid": victim.blocks[0].rid})
    try:
        _, observed = svc.resolve(copied)
    except Denied:
        observed = None  # fixed: the node refuses a rid it did not derive
    if observed is not None:
        svc.revoke(observed.id, reason="the sub-agent misbehaved")
    svc.resolve(victim_tok)
    # an honest offline child gets its own id, derived from its signature
    honest_tok = tokens.append(attacker_tok, child(attacker.id, "h"))
    _, honest = svc.resolve(honest_tok)
    assert honest.blocks[-1].rid == tokens.revocation_id(tokens.inspect(honest_tok).blocks[-1].sig)
    rids = [r["rid"] for r in svc.db.all("SELECT rid FROM grants")]
    assert len(rids) == len(set(rids)) == 3
    svc.revoke(honest.blocks[-1].rid)  # by its revocation id
    with pytest.raises(Denied, match="revoked"):
        svc.resolve(honest_tok)
    svc.resolve(victim_tok)
    svc.resolve(attacker_tok)


def test_revoking_a_v1_child_with_a_copied_rid_spares_the_victim(svc):
    """SEC-6 for v1 tokens still accepted in the migration window: revocation is by grant id
    (bound to one block by the registry), so a copied v1 rid revokes nothing else."""
    victim, victim_tok = issue_v1(svc, subject={"id": "victim"})
    attacker, attacker_tok = issue_v1(svc, subject={"id": "attacker"}, workspaces=["biz-b"])
    copied = tokens.append(attacker_tok, {"v": 1, "id": "gr_" + "k" * 16, "rid": victim.blocks[0].rid,
                                          "parent": attacker.id, "iat": int(T0)})
    _, observed = svc.resolve(copied)
    svc.revoke(observed.id, reason="the sub-agent misbehaved")
    svc.resolve(victim_tok)
    svc.resolve(attacker_tok)
    with pytest.raises(Denied, match="revoked"):
        svc.resolve(copied)


def test_authority_block_names_its_audience_and_key(tmp_path):
    a, _ = make_service(tmp_path / "node-a")
    (tmp_path / "node-b" / "secrets").mkdir(parents=True)
    shutil.copy(tmp_path / "node-a" / "secrets" / "policy.key", tmp_path / "node-b" / "secrets" / "policy.key")
    b, _ = make_service(tmp_path / "node-b")  # the cloud model: one org grant key, many nodes
    _, tok = issue(a)
    authority = tokens.verify(tok, b.public_key)[0]  # node B accepts the signature
    with pytest.raises(Denied):
        b.resolve(tok)  # refused today only because B's registry lacks the root
    assert {"aud", "iss", "kid"} <= set(authority)


# v2 blocks that differ from the canonical one only in encoding; each is JSON a lenient parser
# would accept, and might read differently from this node
CANONICAL = '{"iat":0,"id":"gr_dddddddddddddddd","lanes":["biz-a/x"],"parent":"%s","v":2}'
NON_CANONICAL = {
    "duplicate keys": '{"iat":0,"id":"gr_dddddddddddddddd","lanes":["biz-a/x"],"lanes":["biz-a/y"],"parent":"%s",'
                      '"v":2}',
    "infinity": '{"budget":{"usd":Infinity},"iat":0,"id":"gr_dddddddddddddddd","parent":"%s","v":2}',
    "nan": '{"budget":{"usd":NaN},"iat":0,"id":"gr_dddddddddddddddd","parent":"%s","v":2}',
    "whitespace": '{ "iat": 0, "id": "gr_dddddddddddddddd", "lanes": ["biz-a/x"], "parent": "%s", "v": 2 }',
    "key order": '{"id":"gr_dddddddddddddddd","iat":0,"lanes":["biz-a/x"],"parent":"%s","v":2}',
    "escaped character": '{"iat":0,"id":"gr_dddddddddddddddd","lanes":["biz-a\\u002fx"],"parent":"%s","v":2}',
    "float for an integer": '{"iat":0.0,"id":"gr_dddddddddddddddd","lanes":["biz-a/x"],"parent":"%s","v":2}',
    "string for an integer": '{"iat":"0","id":"gr_dddddddddddddddd","lanes":["biz-a/x"],"parent":"%s","v":2}',
    "null field": '{"ceiling":null,"iat":0,"id":"gr_dddddddddddddddd","lanes":["biz-a/x"],"parent":"%s","v":2}',
    "trailing newline": CANONICAL + "\n",
    "byte order mark": "\ufeff" + CANONICAL,
}


@pytest.mark.parametrize("name", list(NON_CANONICAL))
def test_non_canonical_blocks_are_refused(svc, name):
    g, tok = issue(svc)
    svc.resolve(append_raw(tok, (CANONICAL % g.id).encode()))  # the canonical bytes are accepted
    forged = append_raw(tok, (NON_CANONICAL[name] % g.id).encode())
    with pytest.raises((TokenInvalid, InvalidRequest)):
        svc.resolve(forged)
