"""A strict Web Bot Auth verifier (protocol draft section 5.2), for tests and `agentos identity test-sign`.

Strict means it refuses what a lenient verifier lets through: a signature without
`tag="web-bot-auth"`, without `created`, `expires` or `keyid`, with an `alg` other than ed25519, a
window over 24 hours, a `created` in the future (no skew unless asked for, like Cloudflare's), an
expired signature, a request that carries `Signature-Agent` without covering it, a covered
`signature-agent` whose value is not an https URL, and any component it does not support (such
as `@query-param`, which Cloudflare also rejects). Keys come from the caller, by keyid: there is
no directory fetch here.
"""

from __future__ import annotations

import base64
import binascii
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agentos.webbotauth import base
from agentos.webbotauth.sf import SFError, parse_dictionary, parse_item

MAX_WINDOW = 86400
SUPPORTED = {"@authority", "@method", "@path", "@query", "@target-uri", "@scheme"}


@dataclass
class Result:
    signed: bool
    valid: bool = False
    reason: str | None = None
    keyid: str | None = None
    label: str | None = None
    created: int | None = None
    expires: int | None = None
    covered: list[str] = field(default_factory=list)
    signature_agent: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [])}


class _Reject(Exception):
    pass


def _agent_url(value: str) -> str:
    """The URL a covered signature-agent value names: an sf-string, or with key= a member."""
    try:
        item, _ = parse_item(value)
    except SFError:
        raise _Reject("signature-agent is not a structured string") from None
    if type(item) is not str:  # a Token (an unquoted URL) is not accepted
        raise _Reject("signature-agent is not a quoted URL")
    u = urlsplit(item)
    if u.scheme != "https" or not u.hostname:
        raise _Reject("signature-agent is not an https URL")
    return item


def verify_request(method: str, url: str, headers: Mapping[str, str] | Iterable[tuple[str, str]],
                   keys: Mapping[str, Ed25519PublicKey], *, now: float | None = None, skew: int = 0,
                   nonce_bytes: int | None = None, max_window: int | None = MAX_WINDOW) -> Result:
    """Verify one request as received. `url` is the full URL the request was sent to (scheme,
    the Host it carried, path and query). With `nonce_bytes`, a nonce of exactly that length is
    required (Cloudflare's reference library requires 64). `max_window` bounds `expires - created`
    (the draft RECOMMENDS 24 hours at most; its own E.2.1 vector runs for a century)."""
    now = time.time() if now is None else now
    hd = base.lower_headers(headers)
    if "signature" not in hd and "signature-input" not in hd:
        return Result(signed=False)
    out = Result(signed=True)
    try:
        try:
            inputs = parse_dictionary(hd.get("signature-input", ""))
            sigs = parse_dictionary(hd["signature"]) if "signature" in hd else {}
        except SFError as e:
            raise _Reject(f"malformed signature headers: {e}") from None
        label = next((k for k, (v, p) in inputs.items() if isinstance(v, list) and p.get("tag") == base.TAG), None)
        if label is None:
            raise _Reject('no signature with tag="web-bot-auth"')
        out.label = label
        items, params = inputs[label]
        sig = sigs.get(label, (None, {}))[0]
        if not isinstance(sig, bytes):
            raise _Reject(f"no Signature for {label}")
        created, expires, keyid = params.get("created"), params.get("expires"), params.get("keyid")
        if type(created) is not int or type(expires) is not int:
            raise _Reject("created and expires are required integers")
        if not isinstance(keyid, str):
            raise _Reject("keyid is required")
        out.keyid, out.created, out.expires = keyid, created, expires
        if "alg" in params and params["alg"] != "ed25519":
            raise _Reject(f"unsupported alg {params['alg']!r}")
        if expires <= created or (max_window is not None and expires - created > max_window):
            raise _Reject("the window between created and expires is empty or over 24 hours")
        if created > now + skew:
            raise _Reject(f"created is {created - now:.1f}s in the future")
        if now > expires + skew:
            raise _Reject(f"expired {now - expires:.1f}s ago")
        if nonce_bytes is not None:
            nonce = params.get("nonce")
            try:
                raw = base64.b64decode(nonce, validate=True) if isinstance(nonce, str) else b""
            except (binascii.Error, ValueError):
                raw = b""
            if len(raw) != nonce_bytes:
                raise _Reject(f"the nonce is not {nonce_bytes} bytes")
        names = [n for n, _ in items]
        out.covered = [base.component_id(n, p) for n, p in items]
        if "@authority" not in names and "@target-uri" not in names:
            raise _Reject("neither @authority nor @target-uri is covered")
        for name, cparams in items:
            if not isinstance(name, str):
                raise _Reject("a covered component is not a string")
            if name.startswith("@") and name not in SUPPORTED:
                raise _Reject(f"unsupported component {name}")
            if name == "signature-agent":
                value = base.field_value(name, cparams, hd)
                out.signature_agent = _agent_url(value)
        if "signature-agent" in hd and "signature-agent" not in names:
            raise _Reject("Signature-Agent is sent but not covered")
        key = keys.get(keyid)
        if key is None:
            raise _Reject("unknown keyid")
        sig_base, _ = base.build_base(items, method=method, url=url, headers=hd, params=params)
        try:
            key.verify(sig, sig_base)
        except InvalidSignature:
            raise _Reject("invalid signature") from None
        out.valid = True
    except (_Reject, SFError, ValueError) as e:
        out.reason = str(e)
    return out
