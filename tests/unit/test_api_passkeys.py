"""Passkeys (agentos.api.passkeys): a software authenticator registers and signs in, and every
check that matters refuses a forged, replayed or misdirected answer. The built-in verifier runs
here; with the `webauthn` package installed the last test runs the same ceremonies through it."""

from __future__ import annotations

import json

import pytest
from soft_authenticator import SoftAuthenticator, b64, cbor, unb64

from agentos.api import passkeys as pk
from agentos.api.passkeys import PasskeyError, RelyingParty

RP = RelyingParty.for_urls(None, port=7400)


@pytest.fixture(autouse=True)
def builtin(monkeypatch, request):
    if request.node.name != "test_the_webauthn_library_agrees":
        monkeypatch.setattr(pk, "_library", lambda: None)


def register(auth: SoftAuthenticator, rp: RelyingParty = RP):
    challenge = pk.new_challenge()
    opts = pk.registration_options(rp, user_id=b"m_1", user_name="va", display_name="VA", challenge=challenge)
    answer = auth.create(opts)
    return pk.verify_registration(rp, answer, challenge=challenge), answer


def sign_in(auth: SoftAuthenticator, cred, *, rp: RelyingParty = RP, count: int = 0, **kw):
    challenge = pk.new_challenge()
    answer = auth.get(pk.authentication_options(rp, challenge=challenge), **kw)
    return pk.verify_assertion(rp, answer, challenge=challenge, public_key=cred.public_key, sign_count=count), answer


@pytest.mark.parametrize("alg", ["es256", "eddsa"])
def test_register_then_sign_in(alg):
    auth = SoftAuthenticator(alg=alg)
    cred, answer = register(auth)
    assert cred.id == auth.last_id and cred.sign_count == 0 and pk.credential_id(answer) == cred.id
    count, _ = sign_in(auth, cred)
    assert count == 1
    assert sign_in(auth, cred, count=1)[0] == 2


def test_options_ask_for_a_passkey_with_user_verification():
    opts = pk.registration_options(RP, user_id=b"m_1", user_name="va", display_name="", challenge=b"x" * 32,
                                   exclude=[b"old"])
    assert opts["rp"] == {"id": "localhost", "name": "agentos"} and opts["attestation"] == "none"
    assert opts["authenticatorSelection"]["userVerification"] == "required"
    assert opts["authenticatorSelection"]["residentKey"] == "required"
    assert [p["alg"] for p in opts["pubKeyCredParams"]] == [-7, -8, -257]
    assert opts["excludeCredentials"] == [{"type": "public-key", "id": b64(b"old")}]
    get = pk.authentication_options(RP, challenge=b"y" * 32)
    assert get["userVerification"] == "required" and get["allowCredentials"] == []


def test_rp_from_the_public_url():
    rp = RelyingParty.for_urls("https://agentos.example.com/", port=7400,
                               extra_origins=("https://agentos.example.com:5173", "https://evil.example"))
    assert rp.id == "agentos.example.com"
    assert rp.origins == ("https://agentos.example.com", "https://agentos.example.com:5173")
    assert RelyingParty.for_urls(None, port=7400).origins == ("http://localhost:7400",)


def test_wrong_challenge_origin_rp_or_type_is_refused():
    auth = SoftAuthenticator()
    challenge = pk.new_challenge()
    opts = pk.registration_options(RP, user_id=b"m", user_name="va", display_name="va", challenge=challenge)
    with pytest.raises(PasskeyError, match="another challenge"):
        pk.verify_registration(RP, auth.create(opts), challenge=pk.new_challenge())
    with pytest.raises(PasskeyError, match="another site"):
        pk.verify_registration(RP, auth.create(opts, origin="https://evil.example"), challenge=challenge)
    with pytest.raises(PasskeyError, match="another site"):
        pk.verify_registration(RP, auth.create(opts, rp_id="evil.example"), challenge=challenge)
    register(auth)
    get_answer = auth.get(pk.authentication_options(RP, challenge=challenge))
    with pytest.raises(PasskeyError, match="not a webauthn.create"):
        pk.verify_registration(RP, get_answer, challenge=challenge)


def test_user_verification_is_required():
    auth = SoftAuthenticator(uv=False)
    with pytest.raises(PasskeyError, match="did not verify the person"):
        register(auth)
    good = SoftAuthenticator()
    cred, _ = register(good)
    good.uv = False
    with pytest.raises(PasskeyError, match="did not verify the person"):
        sign_in(good, cred)


def test_a_forged_signature_or_another_key_is_refused():
    auth = SoftAuthenticator()
    cred, _ = register(auth)
    challenge = pk.new_challenge()
    answer = auth.get(pk.authentication_options(RP, challenge=challenge))
    sig = bytearray(unb64(answer["response"]["signature"]))
    sig[-1] ^= 1
    forged = {**answer, "response": {**answer["response"], "signature": b64(bytes(sig))}}
    with pytest.raises(PasskeyError, match="signature is not valid"):
        pk.verify_assertion(RP, forged, challenge=challenge, public_key=cred.public_key, sign_count=0)
    other, _ = register(SoftAuthenticator())
    with pytest.raises(PasskeyError, match="signature is not valid"):
        pk.verify_assertion(RP, answer, challenge=challenge, public_key=other.public_key, sign_count=0)


def test_a_counter_that_goes_back_is_a_cloned_key():
    auth = SoftAuthenticator()
    cred, _ = register(auth)
    with pytest.raises(PasskeyError, match="counter went backwards"):
        sign_in(auth, cred, count=5)
    auth.counter_step = 0  # synced passkeys often keep 0: that is fine while both are 0
    auth.keys[cred.id] = (auth.keys[cred.id][0], "localhost", 0)
    assert sign_in(auth, cred, count=0)[0] == 0


def test_a_tampered_client_data_breaks_the_signature():
    auth = SoftAuthenticator()
    cred, _ = register(auth)
    challenge = pk.new_challenge()
    answer = auth.get(pk.authentication_options(RP, challenge=challenge))
    client = json.loads(unb64(answer["response"]["clientDataJSON"]))
    client["extra"] = "x"
    answer["response"]["clientDataJSON"] = b64(json.dumps(client).encode())
    with pytest.raises(PasskeyError, match="signature"):
        pk.verify_assertion(RP, answer, challenge=challenge, public_key=cred.public_key, sign_count=0)


def test_malformed_answers_are_refused_cleanly():
    for bad in (None, "x", {}, {"rawId": "!!"}, {"rawId": b64(b"x"), "response": {}},
                {"rawId": b64(b"x"), "response": {"clientDataJSON": b64(b"not json")}}):
        with pytest.raises(PasskeyError):
            pk.verify_registration(RP, bad, challenge=b"c" * 32)
    with pytest.raises(PasskeyError, match="indefinite"):
        pk.cbor_decode(b"\x9f\xff")
    with pytest.raises(PasskeyError, match="nested"):
        pk.cbor_decode(b"\x81" * 20 + b"\x00")
    with pytest.raises(PasskeyError, match="truncated"):
        pk.cbor_decode(b"\x5a\xff\xff\xff\xff")
    assert pk.cbor_decode(cbor({1: [b"a", "b", -3, True, None]})) == {1: [b"a", "b", -3, True, None]}


def test_the_webauthn_library_agrees():
    pytest.importorskip("webauthn")
    auth = SoftAuthenticator()
    cred, _ = register(auth)
    assert cred.id == auth.last_id and sign_in(auth, cred)[0] == 1
    with pytest.raises(PasskeyError):
        sign_in(auth, cred, count=9)
    with pytest.raises(PasskeyError):
        register(SoftAuthenticator(uv=False))
    with pytest.raises(PasskeyError):
        sign_in(auth, register(SoftAuthenticator())[0])
