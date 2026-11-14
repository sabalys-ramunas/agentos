"""RFC 9421 signature bases and the Web Bot Auth request headers (docs/design/agent-identity.md).

A signed request carries three headers:

    Signature-Agent: "https://agents.example"
    Signature-Input: sig1=("@authority" "@method" "signature-agent");created=...;expires=...;
                     keyid="<RFC 7638 thumbprint>";alg="ed25519";nonce="<64 random bytes>";tag="web-bot-auth"
    Signature: sig1=:<Ed25519 signature of the base>:

The defaults follow the Phase 0 results (design section 12.6), which override the earlier design:

- `Signature-Agent` is the bare sf-string form. Cloudflare's research verifier rejects a conforming
  dictionary form (`sig1="https://..."`); the dictionary form stays available and tested.
- The nonce is 64 random bytes in standard base64 (the reference library accepts nothing else).
- `created` is now minus 2 seconds and `expires` is `created` + 120: Cloudflare allows no future skew.
- Covered: `@authority`, `@method` and `signature-agent`. `@path` and `@query` stay out while a
  redirect hop can reuse a signature (a stale `@path` makes a same-host hop invalid).

The private key only ever signs a base built here, whose last line is `@signature-params` with
`tag="web-bot-auth"` (domain separation, design section 5.2).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from agentos.webbotauth.sf import Member, Params, SFError, parse_dictionary, ser_bare, ser_inner, ser_params

TAG = "web-bot-auth"
LABEL = "sig1"
WINDOW = 120             # seconds between created and expires
BACKDATE = 2             # created is this many seconds in the past
NONCE_BYTES = 64
COMPONENTS: tuple[str, ...] = ("@authority", "@method", "signature-agent")
FORMS = ("bare", "dict")
SIGNATURE_HEADERS = ("signature", "signature-input", "signature-agent")  # lowercase names
DERIVED = ("@authority", "@method", "@path", "@query", "@target-uri", "@scheme")


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_jwk(key: Ed25519PrivateKey | Ed25519PublicKey) -> dict[str, str]:
    """The public half as an OKP JWK (RFC 8037)."""
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"kty": "OKP", "crv": "Ed25519", "x": b64url(raw)}


def jwk_thumbprint(jwk: Mapping[str, str]) -> str:
    """RFC 7638 SHA-256 thumbprint, base64url. For OKP keys the members are crv, kty and x, in
    lexical order, without whitespace (RFC 8037 section 2)."""
    canon = json.dumps({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]}, separators=(",", ":"))
    return b64url(hashlib.sha256(canon.encode()).digest())


def keyid_of(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    return jwk_thumbprint(public_jwk(key))


def public_key_from_jwk(jwk: Mapping[str, str]) -> Ed25519PublicKey:
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or not isinstance(jwk.get("x"), str):
        raise ValueError("not an Ed25519 OKP key")
    return Ed25519PublicKey.from_public_bytes(b64url_decode(jwk["x"]))


# ---- URLs --------------------------------------------------------------------------------

def _split(url: str) -> tuple[str, str, int | None]:
    u = urlsplit(url)
    scheme = u.scheme.lower()
    host = (u.hostname or "").lower()
    try:
        port = u.port
    except ValueError:
        raise ValueError(f"bad port in {url!r}") from None
    if port is not None and ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        port = None
    return scheme, host, port


def authority_of(url: str) -> str:
    """`@authority` (RFC 9421 section 2.2.3): host, lowercased, and the port unless it is the
    scheme's default."""
    _, host, port = _split(url)
    if ":" in host:  # an IPv6 literal keeps its brackets
        host = f"[{host}]"
    return f"{host}:{port}" if port is not None else host


def origin_of(url: str) -> str:
    """scheme://authority, the unit `sign_hosts` lists."""
    scheme, _, _ = _split(url)
    return f"{scheme}://{authority_of(url)}"


def derived_value(name: str, method: str, url: str) -> str:
    u = urlsplit(url)
    if name == "@authority":
        return authority_of(url)
    if name == "@method":
        return method.upper()
    if name == "@path":
        return u.path or "/"
    if name == "@query":
        return "?" + u.query
    if name == "@scheme":
        return u.scheme.lower()
    if name == "@target-uri":
        return url.split("#", 1)[0]
    raise SFError(f"unsupported derived component {name}")


# ---- components and bases ----------------------------------------------------------------

def component_id(name: str, params: Params) -> str:
    return ser_bare(name) + ser_params(params)


def field_value(name: str, params: Params, headers: Mapping[str, str]) -> str:
    """A header component's value. `headers` maps lowercase names to the combined field value.
    With `key=`, the value is that dictionary member re-serialized (RFC 9421 section 2.1.2)."""
    if name not in headers:
        raise SFError(f"the covered header {name} is missing")
    raw = headers[name].strip()
    if "key" in params:
        members = parse_dictionary(raw)
        member = members.get(params["key"])
        if member is None:
            raise SFError(f"{name} has no member {params['key']}")
        value, mparams = member
        if isinstance(value, list):
            raise SFError(f"{name} member {params['key']} is an inner list")
        return ser_bare(value) + ser_params(mparams)
    if set(params) - {"key"}:
        raise SFError(f"unsupported parameters on {name}: {sorted(params)}")
    return raw


def lower_headers(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> dict[str, str]:
    """Lowercase names, repeated fields combined with ", " as RFC 9110 allows."""
    pairs = headers.items() if isinstance(headers, Mapping) else headers
    out: dict[str, str] = {}
    for k, v in pairs:
        k = k.lower()
        out[k] = f"{out[k]}, {v}" if k in out else v
    return out


def build_base(components: Sequence[Member], *, method: str, url: str, headers: Mapping[str, str],
               params: Params) -> tuple[bytes, str]:
    """The signature base (RFC 9421 section 2.5) and the serialized signature parameters.
    `headers` maps lowercase names to values."""
    lines = []
    seen = set()
    for name, cparams in components:
        if not isinstance(name, str) or name != name.lower():
            raise SFError(f"a component name must be a lowercase string: {name!r}")
        ident = component_id(name, cparams)
        if ident in seen:
            raise SFError(f"{ident} is covered twice")
        seen.add(ident)
        if name.startswith("@"):
            if cparams:
                raise SFError(f"unsupported parameters on {name}")
            value = derived_value(name, method, url)
        else:
            value = field_value(name, cparams, headers)
        if "\n" in value or "\r" in value:
            raise SFError(f"{ident} holds a line break")
        lines.append(f"{ident}: {value}")
    sig_params = ser_inner(list(components), params)
    lines.append(f'"@signature-params": {sig_params}')
    return "\n".join(lines).encode(), sig_params


def signature_agent_header(url: str, form: str = "bare", label: str = LABEL) -> str:
    if form == "bare":
        return ser_bare(url)
    if form == "dict":
        return f"{label}={ser_bare(url)}"
    raise ValueError(f"unknown Signature-Agent form {form!r} (known: {', '.join(FORMS)})")


@dataclass(frozen=True)
class Signed:
    """The three headers for one request, plus what may be written down about them. The
    `Signature` value is a bearer token until `expires`: it goes on the wire and nowhere else."""

    headers: dict[str, str]
    keyid: str
    label: str
    created: int
    expires: int
    covered: tuple[str, ...]
    base_sha256: str

    def record(self) -> dict[str, Any]:
        """Trace fields: never the Signature header, nor anything that rebuilds it."""
        return {"keyid": self.keyid, "created": self.created, "expires": self.expires,
                "covered": list(self.covered), "base_sha256": self.base_sha256}


def sign_request(key: Ed25519PrivateKey, *, method: str, url: str, signature_agent: str | None,
                 form: str = "bare", components: Sequence[str] = COMPONENTS, window: int = WINDOW,
                 now: float | None = None, nonce_bytes: int = NONCE_BYTES, label: str = LABEL) -> Signed:
    """Sign one request. `components` are names; `signature-agent` is dropped when there is no
    agent URL, and covered with `key=<label>` in the dictionary form. `@query` is covered only
    when the URL has a query."""
    if not 1 <= window <= 86400:
        raise ValueError("the signature window must be 1 second to 24 hours (protocol section 5.2)")
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"cannot sign a {scheme or 'relative'} URL")
    headers: dict[str, str] = {}
    comps: list[Member] = []
    for name in components:
        if name == "signature-agent":
            if signature_agent is None:
                continue
            headers["Signature-Agent"] = signature_agent_header(signature_agent, form, label)
            comps.append((name, {"key": label} if form == "dict" else {}))
        elif name == "@query" and not urlsplit(url).query:
            continue
        elif name.startswith("@") and name in DERIVED:
            comps.append((name, {}))
        else:
            raise ValueError(f"cannot cover {name!r} here")
    if not any(n in ("@authority", "@target-uri") for n, _ in comps):
        raise ValueError("a Web Bot Auth signature must cover @authority or @target-uri")
    created = int(time.time() if now is None else now) - BACKDATE
    params: Params = {"created": created, "expires": created + window, "keyid": keyid_of(key), "alg": "ed25519"}
    if nonce_bytes:
        params["nonce"] = base64.b64encode(os.urandom(nonce_bytes)).decode()
    params["tag"] = TAG
    base, sig_params = build_base(comps, method=method, url=url, headers=lower_headers(headers), params=params)
    signature = key.sign(base)
    headers["Signature-Input"] = f"{label}={sig_params}"
    headers["Signature"] = f"{label}={ser_bare(signature)}"
    return Signed(headers=headers, keyid=params["keyid"], label=label, created=created, expires=params["expires"],
                  covered=tuple(component_id(n, p) for n, p in comps), base_sha256=hashlib.sha256(base).hexdigest())
