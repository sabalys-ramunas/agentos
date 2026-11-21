"""Passkeys (WebAuthn) for the panel: the options a browser needs and the server-side checks.

The panel calls `navigator.credentials.create()` with `registration_options(...)` when a member
accepts an invite (or someone adds a passkey), and `navigator.credentials.get()` with
`authentication_options(...)` to sign in or to step up. The browser's answer (a
PublicKeyCredential serialized as JSON, binary fields base64url, as `toJSON()` makes it) comes
back to `verify_registration` or `verify_assertion`.

Verification uses the maintained `webauthn` package (py_webauthn, Duo Labs) when it is
installed, through its `verify_registration_response` and `verify_authentication_response`.
Without it, a small built-in verifier with the same contract runs the checks that matter when
attestation is not trusted (this node asks for `attestation: "none"`): the ceremony type, the
challenge, the origin, the RP id hash, user presence and user verification (always required),
the credential's public key (ES256, EdDSA, RS256) and its signature counter. Attestation
statements are not used to decide anything.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import struct
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from openajar.core.errors import OpenajarError

ES256, EDDSA, RS256 = -7, -8, -257
ALGORITHMS = (ES256, EDDSA, RS256)
CHALLENGE_BYTES = 32
TIMEOUT_MS = 120_000
FLAG_UP, FLAG_UV, FLAG_AT, FLAG_ED = 0x01, 0x04, 0x40, 0x80
MAX_CREDENTIAL_ID = 1023


class PasskeyError(OpenajarError):
    """The browser's answer does not prove what it should. The message is safe to show."""

    code = "passkey_invalid"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: Any) -> bytes:
    if not isinstance(text, str) or len(text) > 100_000:
        raise PasskeyError("a passkey field is missing or not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise PasskeyError("a passkey field is not base64url") from None


def new_challenge() -> bytes:
    return secrets.token_bytes(CHALLENGE_BYTES)


@dataclass(frozen=True)
class RelyingParty:
    """Who the passkeys are for: the RP id (a host name, `localhost` on a node without a public
    address) and the origins the panel is served from."""
    id: str
    origins: tuple[str, ...]
    name: str = "openajar"

    @classmethod
    def for_urls(cls, public_base_url: str | None, *, port: int | None, extra_origins: tuple[str, ...] = (),
                 name: str = "openajar") -> RelyingParty:
        """The RP from `public_base_url` (its host is the RP id, its origin the panel's), else
        localhost on the node's port. `extra_origins` with the same host (dev servers) are added."""
        if public_base_url:
            parts = urlsplit(public_base_url.strip())
            if parts.scheme not in ("https", "http") or not parts.hostname:
                raise ValueError("public_base_url must be an http(s) URL")
            rp_id = parts.hostname.lower()
            origins = [f"{parts.scheme}://{parts.netloc.lower()}"]
        else:
            rp_id = "localhost"
            origins = [f"http://localhost:{port}" if port else "http://localhost"]
        for origin in extra_origins:
            host = (urlsplit(origin).hostname or "").lower()
            if host == rp_id or host.endswith("." + rp_id):
                origins.append(origin.rstrip("/"))
        return cls(id=rp_id, origins=tuple(dict.fromkeys(origins)), name=name)


@dataclass(frozen=True)
class NewCredential:
    id: bytes
    public_key: bytes  # COSE_Key, as the authenticator sent it
    sign_count: int
    aaguid: bytes
    backed_up: bool


# ---------------------------------------------------------------- options (what the browser gets)

def registration_options(rp: RelyingParty, *, user_id: bytes, user_name: str, display_name: str,
                         challenge: bytes, exclude: list[bytes] = ()) -> dict:
    """PublicKeyCredentialCreationOptions as JSON (for PublicKeyCredential.parseCreationOptionsFromJSON).
    A passkey: a discoverable credential with user verification, no attestation."""
    return {
        "rp": {"id": rp.id, "name": rp.name},
        "user": {"id": b64url(user_id), "name": user_name, "displayName": display_name or user_name},
        "challenge": b64url(challenge),
        "pubKeyCredParams": [{"type": "public-key", "alg": alg} for alg in ALGORITHMS],
        "timeout": TIMEOUT_MS,
        "attestation": "none",
        "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True,
                                   "userVerification": "required"},
        "excludeCredentials": [{"type": "public-key", "id": b64url(c)} for c in exclude],
    }


def authentication_options(rp: RelyingParty, *, challenge: bytes, allow: list[bytes] = ()) -> dict:
    """PublicKeyCredentialRequestOptions as JSON. No `allow` list: the browser offers the
    passkeys it holds for this RP (sign in without typing a name)."""
    return {
        "rpId": rp.id,
        "challenge": b64url(challenge),
        "timeout": TIMEOUT_MS,
        "userVerification": "required",
        "allowCredentials": [{"type": "public-key", "id": b64url(c)} for c in allow],
    }


def credential_id(response: Any) -> bytes:
    """The credential id a browser answer names (`rawId`, else `id`)."""
    if not isinstance(response, dict):
        raise PasskeyError("the passkey answer must be a JSON object")
    raw = unb64url(response.get("rawId") or response.get("id"))
    if not raw or len(raw) > MAX_CREDENTIAL_ID:
        raise PasskeyError("the passkey answer has no credential id")
    return raw


def client_challenge(response: Any) -> bytes:
    """The challenge inside the answer's clientDataJSON (to find the ceremony it answers). The
    verify functions check it again against the stored one."""
    return unb64url(_client_data(response).get("challenge"))


# ---------------------------------------------------------------- verification

def verify_registration(rp: RelyingParty, response: Any, *, challenge: bytes) -> NewCredential:
    """Two independent verifiers must agree: the built-in one first (it names the exact reason
    for a refusal), then py_webauthn when installed (a maintained second opinion)."""
    cred = _builtin_registration(rp, response, challenge)
    lib = _library()
    if lib is not None:
        return _lib_registration(lib, rp, response, challenge)
    return cred


def verify_assertion(rp: RelyingParty, response: Any, *, challenge: bytes, public_key: bytes,
                     sign_count: int) -> int:
    """Check a sign-in answer for a known credential; returns the new signature counter. Both
    verifiers must agree, as for registration."""
    count = _builtin_assertion(rp, response, challenge, public_key, sign_count)
    lib = _library()
    if lib is not None:
        return _lib_assertion(lib, rp, response, challenge, public_key, sign_count)
    return count


def _library() -> Any | None:
    try:
        import webauthn  # type: ignore[import-not-found]
    except ImportError:
        return None
    return webauthn


def _lib_registration(lib: Any, rp: RelyingParty, response: Any, challenge: bytes) -> NewCredential:
    credential_id(response)
    try:
        v = lib.verify_registration_response(credential=response, expected_challenge=challenge,
                                             expected_rp_id=rp.id, expected_origin=list(rp.origins),
                                             require_user_verification=True,
                                             supported_pub_key_algs=_lib_algs(lib))
    except Exception as exc:  # the library raises its own exception classes
        raise PasskeyError("the passkey could not be verified") from exc
    aaguid = getattr(v, "aaguid", "") or ""
    try:
        aaguid_bytes = bytes.fromhex(str(aaguid).replace("-", "")) if aaguid else bytes(16)
    except ValueError:
        aaguid_bytes = bytes(16)
    return NewCredential(id=bytes(v.credential_id), public_key=bytes(v.credential_public_key),
                         sign_count=int(v.sign_count), aaguid=aaguid_bytes,
                         backed_up=bool(getattr(v, "credential_backed_up", False)))


def _lib_assertion(lib: Any, rp: RelyingParty, response: Any, challenge: bytes, public_key: bytes,
                   sign_count: int) -> int:
    credential_id(response)
    try:
        v = lib.verify_authentication_response(credential=response, expected_challenge=challenge,
                                               expected_rp_id=rp.id, expected_origin=list(rp.origins),
                                               credential_public_key=public_key,
                                               credential_current_sign_count=sign_count,
                                               require_user_verification=True)
    except Exception as exc:  # as above
        raise PasskeyError("the passkey could not be verified") from exc
    return int(v.new_sign_count)


def _lib_algs(lib: Any) -> list[Any]:
    from webauthn.helpers.structs import COSEAlgorithmIdentifier  # type: ignore[import-not-found]

    return [COSEAlgorithmIdentifier(a) for a in ALGORITHMS]


def _client_data(response: Any) -> dict:
    if not isinstance(response, dict) or not isinstance(response.get("response"), dict):
        raise PasskeyError("the passkey answer must be a PublicKeyCredential as JSON")
    raw = unb64url(response["response"].get("clientDataJSON"))
    try:
        data = json.loads(raw)
    except ValueError:
        raise PasskeyError("the passkey's client data is not JSON") from None
    if not isinstance(data, dict):
        raise PasskeyError("the passkey's client data is not a JSON object")
    return data


def _check_client(rp: RelyingParty, response: dict, kind: str, challenge: bytes) -> bytes:
    """Checks on clientDataJSON; returns its raw bytes (hashed into the signed data)."""
    raw = unb64url(response["response"].get("clientDataJSON"))
    data = _client_data(response)
    if data.get("type") != kind:
        raise PasskeyError(f"the passkey answer is not a {kind} ceremony")
    if not hmac.compare_digest(unb64url(data.get("challenge")), challenge):
        raise PasskeyError("the passkey answer is for another challenge")
    if data.get("origin") not in rp.origins:
        raise PasskeyError("the passkey answer came from another site")
    if data.get("crossOrigin") is True:
        raise PasskeyError("the passkey answer came from a frame of another site")
    return raw


def _check_auth_data(rp: RelyingParty, auth: bytes) -> tuple[int, int]:
    if len(auth) < 37:
        raise PasskeyError("the authenticator data is too short")
    if not hmac.compare_digest(auth[:32], hashlib.sha256(rp.id.encode()).digest()):
        raise PasskeyError("the passkey is for another site")
    flags = auth[32]
    if not flags & FLAG_UP:
        raise PasskeyError("the authenticator did not check that a person is present")
    if not flags & FLAG_UV:
        raise PasskeyError("the authenticator did not verify the person (PIN, fingerprint or face)")
    return flags, struct.unpack(">I", auth[33:37])[0]


def _builtin_registration(rp: RelyingParty, response: Any, challenge: bytes) -> NewCredential:
    raw_id = credential_id(response)
    _check_client(rp, response, "webauthn.create", challenge)
    att = cbor_decode(unb64url(response["response"].get("attestationObject")))
    if not isinstance(att, dict) or not isinstance(att.get("authData"), bytes) or not isinstance(att.get("fmt"), str):
        raise PasskeyError("the attestation object is malformed")
    auth = att["authData"]
    flags, count = _check_auth_data(rp, auth)
    if not flags & FLAG_AT or len(auth) < 55:
        raise PasskeyError("the authenticator sent no credential")
    aaguid = auth[37:53]
    (id_len,) = struct.unpack(">H", auth[53:55])
    if id_len == 0 or id_len > MAX_CREDENTIAL_ID or len(auth) < 55 + id_len:
        raise PasskeyError("the credential id is malformed")
    cred_id = auth[55:55 + id_len]
    cose_key, rest = cbor_decode_prefix(auth[55 + id_len:])
    if rest and not flags & FLAG_ED:
        raise PasskeyError("the authenticator data has trailing bytes")
    if not hmac.compare_digest(cred_id, raw_id):
        raise PasskeyError("the credential id does not match")
    public_key = auth[55 + id_len:len(auth) - len(rest)]
    _cose_public_key(cose_key)  # a key this node can verify with, or refused now
    if att["fmt"] == "none" and att.get("attStmt") not in ({}, None):
        raise PasskeyError("a none attestation carries no statement")
    return NewCredential(id=cred_id, public_key=public_key, sign_count=count, aaguid=aaguid,
                         backed_up=bool(flags & 0x10))


def _builtin_assertion(rp: RelyingParty, response: Any, challenge: bytes, public_key: bytes,
                       sign_count: int) -> int:
    credential_id(response)
    client_raw = _check_client(rp, response, "webauthn.get", challenge)
    auth = unb64url(response["response"].get("authenticatorData"))
    _flags, count = _check_auth_data(rp, auth)
    signature = unb64url(response["response"].get("signature"))
    key, alg = _cose_public_key(cbor_decode(public_key))
    _verify(key, alg, signature, auth + hashlib.sha256(client_raw).digest())
    if (count or sign_count) and count <= sign_count:
        raise PasskeyError("the passkey's counter went backwards; it may have been copied")
    return count


def _cose_public_key(cose: Any) -> tuple[Any, int]:
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    if not isinstance(cose, dict):
        raise PasskeyError("the credential public key is malformed")
    kty, alg = cose.get(1), cose.get(3)
    try:
        if kty == 2 and alg == ES256 and cose.get(-1) == 1:
            x, y = cose.get(-2), cose.get(-3)
            if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == len(y) == 32):
                raise PasskeyError("the EC2 key is malformed")
            return ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"),
                                                 ec.SECP256R1()).public_key(), alg
        if kty == 1 and alg == EDDSA and cose.get(-1) == 6:
            x = cose.get(-2)
            if not (isinstance(x, bytes) and len(x) == 32):
                raise PasskeyError("the OKP key is malformed")
            return ed25519.Ed25519PublicKey.from_public_bytes(x), alg
        if kty == 3 and alg == RS256:
            n, e = cose.get(-1), cose.get(-2)
            if not (isinstance(n, bytes) and isinstance(e, bytes) and len(n) >= 256):
                raise PasskeyError("the RSA key is malformed")
            return rsa.RSAPublicNumbers(int.from_bytes(e, "big"), int.from_bytes(n, "big")).public_key(), alg
    except ValueError:
        raise PasskeyError("the credential public key is not valid") from None
    raise PasskeyError("this kind of passkey is not supported (ES256, EdDSA or RS256)")


def _verify(key: Any, alg: int, signature: bytes, data: bytes) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding

    try:
        if alg == ES256:
            key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        elif alg == EDDSA:
            key.verify(signature, data)
        else:
            key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError):
        raise PasskeyError("the passkey signature is not valid") from None


# ---------------------------------------------------------------- CBOR (the subset WebAuthn uses)

MAX_CBOR_DEPTH = 8
MAX_CBOR_ITEMS = 4096


def cbor_decode(data: bytes) -> Any:
    value, rest = cbor_decode_prefix(data)
    if rest:
        raise PasskeyError("the CBOR data has trailing bytes")
    return value


def cbor_decode_prefix(data: bytes) -> tuple[Any, bytes]:
    """Decode one CBOR item (RFC 8949: integers, byte and text strings, arrays, maps, false, true,
    null; definite lengths only) and return it with the bytes after it."""
    if not isinstance(data, bytes | bytearray):
        raise PasskeyError("CBOR data must be bytes")
    budget = [MAX_CBOR_ITEMS]
    value, pos = _item(bytes(data), 0, 0, budget)
    return value, bytes(data[pos:])


def _item(data: bytes, pos: int, depth: int, budget: list[int]) -> tuple[Any, int]:
    if depth > MAX_CBOR_DEPTH:
        raise PasskeyError("the CBOR data is nested too deeply")
    budget[0] -= 1
    if budget[0] < 0 or pos >= len(data):
        raise PasskeyError("the CBOR data is truncated or too large")
    head = data[pos]
    major, info = head >> 5, head & 0x1F
    pos += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if pos + size > len(data):
            raise PasskeyError("the CBOR data is truncated")
        arg = int.from_bytes(data[pos:pos + size], "big")
        pos += size
    else:
        raise PasskeyError("indefinite-length CBOR is not accepted")
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        if pos + arg > len(data):
            raise PasskeyError("the CBOR data is truncated")
        raw = data[pos:pos + arg]
        if major == 2:
            return raw, pos + arg
        try:
            return raw.decode("utf-8"), pos + arg
        except UnicodeDecodeError:
            raise PasskeyError("a CBOR text string is not UTF-8") from None
    if major == 4:
        out = []
        for _ in range(arg):
            value, pos = _item(data, pos, depth + 1, budget)
            out.append(value)
        return out, pos
    if major == 5:
        items: dict = {}
        for _ in range(arg):
            key, pos = _item(data, pos, depth + 1, budget)
            if not isinstance(key, int | str) or key in items:
                raise PasskeyError("a CBOR map key is not valid")
            items[key], pos = _item(data, pos, depth + 1, budget)
        return items, pos
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], pos
    raise PasskeyError("this CBOR item is not accepted")
