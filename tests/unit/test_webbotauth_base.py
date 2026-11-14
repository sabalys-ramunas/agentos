"""Web Bot Auth signing core: published test vectors, the signer's defaults, and the strict verifier.

Ed25519 is deterministic, so a byte-exact signature proves the signature base serialization.
Vectors: RFC 9421 B.2.6, and the protocol draft's E.2.1 (dictionary Signature-Agent) and E.2.2
(legacy sf-string). A golden one too: the request Cloudflare's research verifier answered `valid`
in Phase 0 (docs/design/agent-identity.md, section 12.4). The key is the published RFC 9421 test
key (B.1.4), never a production key.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentos.webbotauth import base, sf
from agentos.webbotauth.verify import verify_request

TEST_JWK = {"kty": "OKP", "crv": "Ed25519", "d": "n4Ni-HpISpVObnQMW0wOhCKROaIKqKtW_2ZYb2p9KcU",
            "x": "JrQLj5P_89iXES9-vFgrIy29clF9CC_oPPsw3c5D0bs"}
TEST_KEYID = "poqkLGiymh_W0uP6PZFw-dvez3QJT5SolqXBCW38r0U"
KEY = Ed25519PrivateKey.from_private_bytes(base.b64url_decode(TEST_JWK["d"]))
KEYS = {TEST_KEYID: KEY.public_key()}
RESEARCH = "https://http-message-signatures-example.research.cloudflare.com"


def sign_vector(components, url, headers, params, method="GET") -> str:
    sig_base, _ = base.build_base(components, method=method, url=url, headers=base.lower_headers(headers),
                                  params=params)
    return base64.b64encode(KEY.sign(sig_base)).decode()


def test_the_test_key_thumbprint():
    assert base.jwk_thumbprint(TEST_JWK) == TEST_KEYID
    assert base.keyid_of(KEY) == base.keyid_of(KEY.public_key()) == TEST_KEYID
    assert base.public_jwk(KEY) == {"kty": "OKP", "crv": "Ed25519", "x": TEST_JWK["x"]}
    assert base.keyid_of(base.public_key_from_jwk(TEST_JWK)) == TEST_KEYID


def test_rfc9421_b26():
    comps = [("date", {}), ("@method", {}), ("@path", {}), ("@authority", {}), ("content-type", {}),
             ("content-length", {})]
    headers = {"Date": "Tue, 20 Apr 2021 02:07:55 GMT", "Content-Type": "application/json", "Content-Length": "18"}
    got = sign_vector(comps, "https://example.com/foo?param=Value&Pet=dog", headers,
                      {"created": 1618884473, "keyid": "test-key-ed25519"}, method="POST")
    assert got == "wqcAqbmYJ2ji2glfAMaRy4gruYYnx2nEFN2HN6jrnDnQCK1u02Gb04v9EDgwUPiu4A0w6vuQv5lIp5WPpBKRCw=="


E21 = {"created": 1735689600, "keyid": TEST_KEYID, "alg": "ed25519", "expires": 4889289600,
       "nonce": "n9p433xm+NJ3ph3upfBIGmsuwHw387YV7Q/F+6BSpGCVjYCqQw6rznNA8PVVLySrAWsv0hQtFioQb6E1YsauiA==",
       "tag": "web-bot-auth"}
E21_SIG = "CGokAoY/FrPpyuzTe1Fb2kLTAH9Idw6qSFd1+D77r1firsMRCxeuLu5e6O+ek3zQgDMAzjiXtLVfqE00xnABCQ=="
E22 = {"created": 1735689600, "keyid": TEST_KEYID, "alg": "ed25519", "expires": 1735693200,
       "nonce": "e8N7S2MFd/qrd6T2R3tdfAuuANngKI7LFtKYI/vowzk4lAZYadIX6wW25MwG7DCT9RUKAJ0qVkU0mEeLElW1qg==",
       "tag": "web-bot-auth"}
E22_SIG = "jdq0SqOwHdyHr9+r5jw3iYZH6aNGKijYp/EstF4RQTQdi5N5YYKrD+mCT1HA1nZDsi6nJKuHxUi/5Syp3rLWBA=="


def test_draft_e21_dictionary_form():
    comps = [("@authority", {}), ("signature-agent", {"key": "sig2"})]
    headers = {"Signature-Agent": 'sig2="https://signature-agent.test"'}
    assert sign_vector(comps, "https://example.com/", headers, E21) == E21_SIG
    sig_input = "sig2=" + sf.ser_inner(comps, E21)
    received = {**headers, "Signature-Input": sig_input, "Signature": f"sig2=:{E21_SIG}:"}
    assert "24 hours" in verify_request("GET", "https://example.com/", received, KEYS, now=1735690000).reason
    result = verify_request("GET", "https://example.com/", received, KEYS, now=1735690000, max_window=None)
    assert result.valid, result.reason
    assert result.signature_agent == "https://signature-agent.test" and result.label == "sig2"


def test_draft_e22_legacy_form():
    comps = [("@authority", {}), ("signature-agent", {})]
    headers = {"Signature-Agent": '"https://signature-agent.test"'}
    assert sign_vector(comps, "https://example.com/", headers, E22) == E22_SIG
    result = verify_request("GET", "https://example.com/", {
        **headers, "Signature-Input": "sig1=" + sf.ser_inner(comps, E22), "Signature": f"sig1=:{E22_SIG}:"},
        KEYS, now=1735690000)
    assert result.valid, result.reason
    assert verify_request("GET", "https://example.com/", {
        **headers, "Signature-Input": "sig1=" + sf.ser_inner(comps, E22), "Signature": f"sig1=:{E22_SIG}:"},
        KEYS, now=1735693201).reason.startswith("expired")


def test_golden_request_cloudflare_accepted():
    """Section 12.4: these exact headers got `valid` from Cloudflare's research verifier."""
    comps = [("@authority", {}), ("@method", {}), ("@path", {}), ("@query", {}), ("signature-agent", {})]
    params = {"created": 1793768938, "expires": 1793769058, "keyid": TEST_KEYID, "alg": "ed25519",
              "nonce": "f0wYUTctfClla1DllORminLs43kccNPMdLQloVCm3g0hDN/u9smt8b/mb8gL9uOMcgBXie/dPwT9bDMzowav1w==",
              "tag": "web-bot-auth"}
    url = RESEARCH + "/v0/api/verify?q=1"
    headers = {"Signature-Agent": f'"{RESEARCH}"'}
    sig = sign_vector(comps, url, headers, params)
    assert sig == "xZw9GTucYarmKPg7BpfzDCCsNTkcG35CS0Etdf8MxfihC87D9POeayUTPoXTOf+Yxjee7Iu3tsfYHmWurt2LAA=="
    assert "sig1=" + sf.ser_inner(comps, params) == (
        'sig1=("@authority" "@method" "@path" "@query" "signature-agent");created=1793768938;expires=1793769058;'
        f'keyid="{TEST_KEYID}";alg="ed25519";nonce="{params["nonce"]}";tag="web-bot-auth"')
    result = verify_request("GET", url, {**headers, "Signature-Input": "sig1=" + sf.ser_inner(comps, params),
                                         "Signature": f"sig1=:{sig}:"}, KEYS, now=1793769000, nonce_bytes=64)
    assert result.valid, result.reason


# ---- the signer's defaults (design section 12.6) -----------------------------------------------

def test_defaults_bare_form_three_components_64_byte_nonce_backdated():
    signed = base.sign_request(KEY, method="get", url="https://shop.example:8443/a/b?c=d",
                               signature_agent="https://agents.example", now=1_800_000_000.7)
    assert list(signed.headers) == ["Signature-Agent", "Signature-Input", "Signature"]
    assert signed.headers["Signature-Agent"] == '"https://agents.example"'
    assert signed.covered == ('"@authority"', '"@method"', '"signature-agent"')
    assert (signed.created, signed.expires) == (1_800_000_000 - 2, 1_800_000_000 - 2 + 120)
    inputs = sf.parse_dictionary(signed.headers["Signature-Input"])
    items, params = inputs["sig1"]
    assert [n for n, _ in items] == ["@authority", "@method", "signature-agent"]
    assert list(params) == ["created", "expires", "keyid", "alg", "nonce", "tag"]
    assert len(base64.b64decode(params["nonce"])) == 64 and params["tag"] == "web-bot-auth"
    assert params["keyid"] == TEST_KEYID
    result = verify_request("GET", "https://shop.example:8443/other/path", signed.headers, KEYS,
                            now=1_800_000_001, nonce_bytes=64)
    assert result.valid, result.reason  # @path is not covered, so a same-host hop stays valid


def test_each_request_gets_a_fresh_nonce_and_signature():
    a = base.sign_request(KEY, method="GET", url="https://a.example/", signature_agent="https://agents.example")
    b = base.sign_request(KEY, method="GET", url="https://a.example/", signature_agent="https://agents.example")
    assert a.headers["Signature"] != b.headers["Signature"]
    assert a.headers["Signature-Input"] != b.headers["Signature-Input"]


def test_dictionary_form_signs_and_verifies():
    signed = base.sign_request(KEY, method="POST", url="https://a.example/x", signature_agent="https://agents.example",
                               form="dict", now=1_800_000_000)
    assert signed.headers["Signature-Agent"] == 'sig1="https://agents.example"'
    assert '"signature-agent";key="sig1"' in signed.covered
    assert verify_request("POST", "https://a.example/x", signed.headers, KEYS, now=1_800_000_000).valid


def test_record_never_holds_the_signature():
    signed = base.sign_request(KEY, method="GET", url="https://a.example/", signature_agent="https://agents.example")
    record = signed.record()
    sig = signed.headers["Signature"].split("=", 1)[1].strip(":")
    text = repr(record)
    assert sig not in text and "Signature" not in text and "nonce" not in text
    assert set(record) == {"keyid", "created", "expires", "covered", "base_sha256"}


def test_signer_refuses_what_the_protocol_forbids():
    with pytest.raises(ValueError, match="window"):
        base.sign_request(KEY, method="GET", url="https://a.example/", signature_agent=None, window=86401)
    with pytest.raises(ValueError, match="cannot sign"):
        base.sign_request(KEY, method="GET", url="ftp://a.example/", signature_agent=None)
    with pytest.raises(ValueError, match="@authority or @target-uri"):
        base.sign_request(KEY, method="GET", url="https://a.example/", signature_agent=None, components=("@method",))
    with pytest.raises(ValueError, match="cannot cover"):
        base.sign_request(KEY, method="GET", url="https://a.example/", signature_agent=None,
                          components=("@authority", "@query-param"))
    with pytest.raises(ValueError, match="form"):
        base.sign_request(KEY, method="GET", url="https://a.example/", signature_agent="https://x.example", form="x")


def test_authority_and_origin():
    assert base.authority_of("https://Example.COM:443/x") == "example.com"
    assert base.authority_of("http://127.0.0.1:6351/x") == "127.0.0.1:6351"
    assert base.authority_of("http://a.example:80/") == "a.example"
    assert base.origin_of("HTTPS://A.example:8443/p?q") == "https://a.example:8443"


# ---- the strict verifier --------------------------------------------------------------------

def fresh(**kw):
    kw.setdefault("signature_agent", "https://agents.example")
    return base.sign_request(KEY, method="GET", url="https://a.example/p", now=1_800_000_000, **kw).headers


@pytest.mark.parametrize(("mutate", "reason"), [
    (lambda h: {**h, "Signature": h["Signature"][:10] + ("A" if h["Signature"][10] != "A" else "B") + h["Signature"][11:]},
     "invalid signature"),
    (lambda h: {**h, "Signature-Input": h["Signature-Input"].replace('tag="web-bot-auth"', 'tag="other"')},
     "web-bot-auth"),
    (lambda h: {**h, "Signature-Input": h["Signature-Input"].replace(f'keyid="{TEST_KEYID}"', 'keyid="nope"')},
     "unknown keyid"),
    (lambda h: {**h, "Signature-Agent": '"https://evil.example"'}, "invalid signature"),
    (lambda h: {**h, "Signature-Agent": "https://agents.example"}, "signature-agent"),
    (lambda h: {k: v for k, v in h.items() if k != "Signature"}, "no Signature"),
    (lambda h: {**h, "Signature-Input": "garbage(("}, "malformed"),
])
def test_verifier_rejects(mutate, reason):
    result = verify_request("GET", "https://a.example/p", mutate(fresh()), KEYS, now=1_800_000_000)
    assert result.signed and not result.valid
    assert reason in result.reason


def test_verifier_time_rules():
    h = fresh()
    created = 1_800_000_000 - 2
    assert verify_request("GET", "https://a.example/p", h, KEYS, now=created).valid
    assert "future" in verify_request("GET", "https://a.example/p", h, KEYS, now=created - 1).reason
    assert verify_request("GET", "https://a.example/p", h, KEYS, now=created - 1, skew=1).valid
    assert verify_request("GET", "https://a.example/p", h, KEYS, now=created + 120).valid
    assert "expired" in verify_request("GET", "https://a.example/p", h, KEYS, now=created + 121).reason


def test_verifier_binds_the_authority_and_method():
    h = fresh()
    assert "invalid signature" in verify_request("GET", "https://other.example/p", h, KEYS, now=1_800_000_000).reason
    assert "invalid signature" in verify_request("POST", "https://a.example/p", h, KEYS, now=1_800_000_000).reason


def test_verifier_requires_coverage_rules():
    no_agent = base.sign_request(KEY, method="GET", url="https://a.example/", signature_agent=None, now=1_800_000_000)
    assert verify_request("GET", "https://a.example/", no_agent.headers, KEYS, now=1_800_000_000).valid
    smuggled = {**no_agent.headers, "Signature-Agent": '"https://agents.example"'}
    assert "not covered" in verify_request("GET", "https://a.example/", smuggled, KEYS, now=1_800_000_000).reason
    http_agent = fresh(signature_agent="http://agents.example")
    assert "https" in verify_request("GET", "https://a.example/p", http_agent, KEYS, now=1_800_000_000).reason


def test_verifier_window_and_nonce_rules():
    comps = [("@authority", {})]
    for params, reason in (({"created": 1, "expires": 90000, "keyid": TEST_KEYID, "tag": "web-bot-auth"}, "24 hours"),
                           ({"created": 1, "keyid": TEST_KEYID, "tag": "web-bot-auth"}, "required"),
                           ({"created": 1, "expires": 2, "tag": "web-bot-auth"}, "keyid"),
                           ({"created": 1, "expires": 2, "keyid": TEST_KEYID, "alg": "rsa-pss-sha512",
                             "tag": "web-bot-auth"}, "alg")):
        sig = sign_vector(comps, "https://a.example/", {}, params)
        h = {"Signature-Input": "sig1=" + sf.ser_inner(comps, params), "Signature": f"sig1=:{sig}:"}
        assert reason in verify_request("GET", "https://a.example/", h, KEYS, now=1.5).reason
    short = base.sign_request(KEY, method="GET", url="https://a.example/", signature_agent=None, nonce_bytes=16,
                              now=1_800_000_000)
    assert verify_request("GET", "https://a.example/", short.headers, KEYS, now=1_800_000_000).valid
    assert "64 bytes" in verify_request("GET", "https://a.example/", short.headers, KEYS, now=1_800_000_000,
                                        nonce_bytes=64).reason


def test_verifier_rejects_query_param_and_reports_unsigned():
    comps = [("@authority", {}), ("@query-param", {"name": "a"})]
    params = {"created": 1, "expires": 2, "keyid": TEST_KEYID, "tag": "web-bot-auth"}
    h = {"Signature-Input": "sig1=" + sf.ser_inner(comps, params), "Signature": "sig1=:AAAA:"}
    assert "unsupported" in verify_request("GET", "https://a.example/?a=1", h, KEYS, now=1.5).reason
    assert verify_request("GET", "https://a.example/", {"Accept": "*/*"}, KEYS).signed is False


# ---- structured fields ---------------------------------------------------------------------

def test_sf_round_trip_and_errors():
    text = 'sig1=("@authority" "signature-agent";key="sig1");created=1;tag="web-bot-auth", sig2=:AAEC:'
    d = sf.parse_dictionary(text)
    items, params = d["sig1"]
    assert items == [("@authority", {}), ("signature-agent", {"key": "sig1"})]
    assert sf.ser_inner(items, params) == '("@authority" "signature-agent";key="sig1");created=1;tag="web-bot-auth"'
    assert d["sig2"] == (b"\x00\x01\x02", {})
    assert sf.parse_item('"a \\"q\\" b"') == ('a "q" b', {})
    assert isinstance(sf.parse_item("https://x")[0], sf.Token)
    for bad in ("", "a=(", 'a="x', "a=:!!:", "a=1,", "A=1", 'a="\\x"'):
        with pytest.raises(sf.SFError):
            sf.parse_dictionary(bad)
    with pytest.raises(sf.SFError):
        sf.ser_bare("café")
    with pytest.raises(sf.SFError):
        base.build_base([("@authority", {}), ("@authority", {})], method="GET", url="https://a/", headers={},
                        params={})
