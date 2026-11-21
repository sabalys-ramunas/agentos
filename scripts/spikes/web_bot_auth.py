#!/usr/bin/env python3
"""SPIKE, NOT PRODUCT CODE. Phase 0 of docs/design/agent-identity.md (section 8).

Throwaway measurement script for Web Bot Auth (RFC 9421 HTTP Message Signatures, Ed25519) on a
Camoufox lane driven by Playwright. Nothing in src/ imports this, nothing here is a product
interface, and the signer below is not the Phase 1 signer (that one gets test vectors, key files
and review). It signs ONLY with Cloudflare's published RFC 9421 test key (RFC 9421 Appendix
B.1.4), which must never be used in production.

It answers four questions with numbers:

  q1  route signing on Camoufox: a document, a subresource, a fetch and an XHR, plus redirects
  q2  the pinned fingerprint does not move with the route installed (navigator values, the
      request headers the server sees, navigation timing)
  q3  interop: which Signature-Agent form Cloudflare's test deployments accept
  q4  a 120-second signature window verifies against the node clock

Run it on a node dev slot only, never on the PoC or production node data:

  DISPLAY_NUM=330 .venv/bin/python scripts/spikes/web_bot_auth.py all --out <dir>/results.json

Subcommands: selftest (the signer against RFC 9421 B.2.6 and the draft's E.2.1 and E.2.2
vectors), interop (q3 matrix), edges (q3 format checks), browser (q1 and q2), repeat (q2
launch-to-launch check and the redirect Host controls), clock (q4), all. The browser parts start
their own Xvfb on :DISPLAY_NUM, two loopback test servers (ports BASE_PORT+1 and BASE_PORT+2), and
a throwaway Camoufox profile under --work, with no proxy. It stops every process it started. External hosts it contacts: the
two Cloudflare test endpoints below and time.cloudflare.com (SNTP).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import http.server
import json
import os
import re
import shutil
import signal
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

# ---- the published test key (RFC 9421 B.1.4, cloudflareresearch/web-bot-auth examples) ------

TEST_JWK = {
    "kty": "OKP",
    "crv": "Ed25519",
    "d": "n4Ni-HpISpVObnQMW0wOhCKROaIKqKtW_2ZYb2p9KcU",
    "x": "JrQLj5P_89iXES9-vFgrIy29clF9CC_oPPsw3c5D0bs",
}
RESEARCH = "https://http-message-signatures-example.research.cloudflare.com"
RESEARCH_VERIFY = RESEARCH + "/v0/api/verify"
CRAWLTEST = "https://crawltest.com/cdn-cgi/web-bot-auth"
SIGNATURE_AGENT_URL = RESEARCH  # the directory the test key is published under
USER_AGENT = "openajar-spike/0 (web-bot-auth phase 0; test key only)"


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(b64url_decode(TEST_JWK["d"]))
PUBLIC_KEY = Ed25519PublicKey.from_public_bytes(b64url_decode(TEST_JWK["x"]))


def jwk_thumbprint(jwk: dict) -> str:
    """RFC 7638 thumbprint, OKP members per RFC 8037: crv, kty, x in lexical order, no spaces."""
    canon = json.dumps({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]}, separators=(",", ":"))
    return b64url(hashlib.sha256(canon.encode()).digest())


KEYID = jwk_thumbprint(TEST_JWK)

# ---- a small RFC 8941 structured-field parser and serializer (enough for these headers) ----


class Token(str):
    pass


class SFError(ValueError):
    pass


class _P:
    def __init__(self, s: str):
        self.s, self.i = s, 0

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def ws(self) -> None:
        while self.peek() in (" ", "\t") and self.peek():
            self.i += 1

    def key(self) -> str:
        m = re.compile(r"[a-z*][a-z0-9_\-.*]*").match(self.s, self.i)
        if not m:
            raise SFError(f"bad key at {self.i}")
        self.i = m.end()
        return m.group()

    def bare(self) -> Any:
        c = self.peek()
        if c == '"':
            self.i += 1
            out = []
            while True:
                c = self.peek()
                if not c:
                    raise SFError("unterminated string")
                self.i += 1
                if c == "\\":
                    out.append(self.peek())
                    self.i += 1
                elif c == '"':
                    return "".join(out)
                else:
                    out.append(c)
        if c == ":":
            end = self.s.index(":", self.i + 1)
            raw = self.s[self.i + 1:end]
            self.i = end + 1
            return base64.b64decode(raw, validate=True)
        if c == "?":
            self.i += 2
            return self.s[self.i - 1] == "1"
        m = re.compile(r"-?\d+(\.\d+)?").match(self.s, self.i)
        if m:
            self.i = m.end()
            return float(m.group()) if m.group(1) else int(m.group())
        m = re.compile(r"[A-Za-z*][A-Za-z0-9:/!#$%&'*+\-.^_`|~]*").match(self.s, self.i)
        if m:
            self.i = m.end()
            return Token(m.group())
        raise SFError(f"bad item at {self.i}: {self.s[self.i:self.i + 10]!r}")

    def params(self) -> dict:
        out: dict = {}
        while self.peek() == ";":
            self.i += 1
            self.ws()
            k = self.key()
            v: Any = True
            if self.peek() == "=":
                self.i += 1
                v = self.bare()
            out[k] = v
        return out

    def item_or_inner(self) -> tuple[Any, dict]:
        if self.peek() == "(":
            self.i += 1
            items = []
            while True:
                self.ws()
                if self.peek() == ")":
                    self.i += 1
                    break
                v = self.bare()
                items.append((v, self.params()))
                if self.peek() not in (" ", ")"):
                    raise SFError("bad inner list")
            return items, self.params()
        v = self.bare()
        return v, self.params()

    def dictionary(self) -> dict:
        out: dict = {}
        while True:
            self.ws()
            k = self.key()
            if self.peek() == "=":
                self.i += 1
                out[k] = self.item_or_inner()
            else:
                out[k] = (True, self.params())
            self.ws()
            if not self.peek():
                return out
            if self.peek() != ",":
                raise SFError("expected comma")
            self.i += 1

    def done(self) -> None:
        self.ws()
        if self.peek():
            raise SFError(f"trailing data at {self.i}")


def parse_dictionary(s: str) -> dict:
    p = _P(s.strip())
    d = p.dictionary()
    p.done()
    return d


def parse_item(s: str) -> tuple[Any, dict]:
    p = _P(s.strip())
    v = p.item_or_inner()
    p.done()
    return v


def ser_bare(v: Any) -> str:
    if isinstance(v, bool):
        return "?1" if v else "?0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, Token):
        return str(v)
    if isinstance(v, bytes):
        return ":" + base64.b64encode(v).decode() + ":"
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise SFError(f"cannot serialize {v!r}")


def ser_params(p: dict) -> str:
    return "".join(f";{k}" if v is True else f";{k}={ser_bare(v)}" for k, v in p.items())


def ser_inner(items: list, params: dict) -> str:
    return "(" + " ".join(ser_bare(v) + ser_params(p) for v, p in items) + ")" + ser_params(params)


# ---- RFC 9421 signer and a strict local verifier -------------------------------------------

def authority_of(url: str) -> str:
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    port = u.port
    if port and not ((u.scheme == "https" and port == 443) or (u.scheme == "http" and port == 80)):
        return f"{host}:{port}"
    return host


def signature_agent_value(form: str, url: str = SIGNATURE_AGENT_URL, label: str = "sig1") -> str:
    if form == "bare":           # Cloudflare's documented form: a quoted sf-string
        return ser_bare(url)
    if form == "dict":           # the protocol draft's MUST: a dictionary member
        return f"{label}={ser_bare(url)}"
    if form == "dict-typed":     # what Cloudflare's own example signers send
        return f"{label}={ser_bare(url)};type=directory"
    if form == "unquoted":       # negative control: Cloudflare says this fails
        return url
    raise ValueError(form)


def sign_request(method: str, url: str, *, form: str | None = "bare", components: str = "full",
                 nonce_bytes: int | None = 64, created: int | None = None, window: int = 120,
                 label: str = "sig1", tamper: bool = False, sa_url: str = SIGNATURE_AGENT_URL,
                 sa_label: str | None = None, sa_cover: str = "auto", sa_raw_base: bool = False,
                 tag: str = "web-bot-auth", extra: tuple[tuple[str, dict, str], ...] = ()) -> dict[str, str]:
    """The three Web Bot Auth headers for one request. components: "min" covers @authority and
    signature-agent; "full" adds @method, @path and @query (when present).

    The other knobs exist to map what a verifier rejects: sa_cover "none" leaves signature-agent
    uncovered, "name" covers it without the key parameter; sa_raw_base puts the raw header value
    in the base for a key= component; extra adds (name, params, value) components."""
    u = urlsplit(url)
    created = int(time.time()) if created is None else created
    comps: list[tuple[str, dict]] = [("@authority", {})]
    values: dict[str, str] = {"@authority": authority_of(url)}
    headers: dict[str, str] = {}
    if components == "full":
        comps += [("@method", {}), ("@path", {})]
        values["@method"] = method.upper()
        values["@path"] = u.path or "/"
        if u.query:
            comps.append(("@query", {}))
            values["@query"] = "?" + u.query
    if form is not None:
        sa = signature_agent_value(form, url=sa_url, label=sa_label or label)
        headers["Signature-Agent"] = sa
        if sa_cover == "auto" and form.startswith("dict"):
            comps.append(("signature-agent", {"key": sa_label or label}))
        elif sa_cover in ("auto", "name"):
            comps.append(("signature-agent", {}))
    for name, cp, value in extra:
        comps.append((name, cp))
        values[ser_bare(name) + ser_params(cp)] = value
    params: dict[str, Any] = {"created": created, "expires": created + window, "keyid": KEYID,
                              "alg": "ed25519"}
    if nonce_bytes:
        params["nonce"] = base64.b64encode(os.urandom(nonce_bytes)).decode()
    params["tag"] = tag
    sig_params = ser_inner(comps, params)
    lines = []
    for name, cp in comps:
        ident = ser_bare(name) + ser_params(cp)
        if ident in values:
            v = values[ident]
        elif name.startswith("@"):
            v = values[name]
        elif sa_raw_base and name == "signature-agent":
            v = headers["Signature-Agent"]
        else:
            v = component_value(name, cp, headers)
        lines.append(f"{ser_bare(name)}{ser_params(cp)}: {v}")
    lines.append(f'"@signature-params": {sig_params}')
    base = "\n".join(lines).encode()
    sig = PRIVATE_KEY.sign(base)
    if tamper:
        sig = bytes([sig[0] ^ 1]) + sig[1:]
    headers["Signature-Input"] = f"{label}={sig_params}"
    headers["Signature"] = f"{label}=:{base64.b64encode(sig).decode()}:"
    return headers


def component_value(name: str, cparams: dict, headers: dict[str, str]) -> str:
    lower = {k.lower(): v for k, v in headers.items()}
    if name not in lower:
        raise SFError(f"covered header {name} is missing")
    raw = lower[name].strip()
    if "key" in cparams:
        d = parse_dictionary(raw)
        if cparams["key"] not in d:
            raise SFError(f"{name} has no member {cparams['key']}")
        v, p = d[cparams["key"]]
        return ser_bare(v) + ser_params(p)
    return raw


def verify_request(method: str, scheme: str, target: str, headers: list[tuple[str, str]], *,
                   now: float | None = None, skew: int = 0) -> dict[str, Any]:
    """Strict verification of one received request against the test key, with this node's clock.
    target is the raw request target (path and query); headers keep the order they arrived in."""
    now = time.time() if now is None else now
    hd: dict[str, str] = {}
    for k, v in headers:
        hd[k.lower()] = (hd[k.lower()] + ", " + v) if k.lower() in hd else v
    out: dict[str, Any] = {"signed": "signature" in hd}
    if not out["signed"]:
        return out
    try:
        sin = parse_dictionary(hd["signature-input"])
        sigs = parse_dictionary(hd["signature"])
        label = next(iter(sin))
        items, params = sin[label]
        sig = sigs[label][0]
        if params.get("tag") != "web-bot-auth":
            raise SFError("tag is not web-bot-auth")
        if params.get("keyid") != KEYID:
            raise SFError("unknown keyid")
        created, expires = params["created"], params["expires"]
        if created > now + skew:
            raise SFError(f"created is {created - now:.1f}s in the future")
        if expires < now - skew:
            raise SFError(f"expired {now - expires:.1f}s ago")
        path, _, query = target.partition("?")
        host = hd.get("host", "")
        values = {"@authority": authority_of(f"{scheme}://{host}"), "@method": method.upper(),
                  "@path": path or "/", "@query": "?" + query,
                  "@target-uri": f"{scheme}://{host}{target}"}
        lines = []
        for name, cp in items:
            v = values[name] if name.startswith("@") else component_value(name, cp, hd)
            lines.append(f"{ser_bare(name)}{ser_params(cp)}: {v}")
        lines.append(f'"@signature-params": {ser_inner(items, params)}')
        PUBLIC_KEY.verify(sig, "\n".join(lines).encode())
        out.update(valid=True, created=created, expires=expires,
                   covered=[ser_bare(n) + ser_params(p) for n, p in items])
    except Exception as e:  # noqa: BLE001  (a spike: every failure is a result)
        out.update(valid=False, reason=f"{type(e).__name__}: {e}")
    return out


# ---- plain HTTP probes against the Cloudflare test endpoints -------------------------------

def http_get(url: str, headers: dict[str, str] | None = None, timeout: float = 20) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body, status, rh = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        body, status, rh = e.read(), e.code, dict(e.headers)
    return {"status": status, "body": body.decode(errors="replace")[:300], "date": rh.get("Date"),
            "ms": round((time.time() - t0) * 1000), "sent_at": t0}


def research_ok(r: dict) -> bool:
    return r["status"] == 200 and r["body"].strip() == "valid"


def crawltest_ok(r: dict) -> bool:
    return r["status"] == 200


def run_interop(pace: float = 0.4) -> dict[str, Any]:
    """q3: every Signature-Agent form, nonce size and component set, against both endpoints,
    plus negative controls (tampered signature, no Signature-Agent, unsigned)."""
    rows = []
    cases = []
    for form in ("bare", "dict", "dict-typed", "unquoted"):
        for nonce in (None, 16, 64):
            for comps in ("min", "full"):
                cases.append({"form": form, "nonce": nonce, "components": comps})
    cases += [{"form": "bare", "nonce": 64, "components": "full", "tamper": True},
              {"form": "dict", "nonce": 64, "components": "full", "tamper": True},
              {"form": None, "nonce": 64, "components": "full"},
              {"form": "unsigned"}]
    for c in cases:
        for endpoint, ok in ((RESEARCH_VERIFY, research_ok), (CRAWLTEST, crawltest_ok)):
            url = endpoint + ("?q=1" if c.get("components") == "full" and endpoint == RESEARCH_VERIFY else "")
            if c["form"] == "unsigned":
                hdrs: dict[str, str] = {}
            else:
                hdrs = sign_request("GET", url, form=c["form"], components=c["components"],
                                    nonce_bytes=c["nonce"], tamper=c.get("tamper", False))
            r = http_get(url, hdrs)
            rows.append({**c, "endpoint": endpoint, "url": url, "status": r["status"], "body": r["body"],
                         "accepted": ok(r), "headers": hdrs})
            time.sleep(pace)
    return {"rows": rows}


EDGE_CASES: list[tuple[str, dict[str, Any]]] = [
    ("bare (control)", {"form": "bare"}),
    ("dict (control)", {"form": "dict"}),
    ("dict, covered as plain signature-agent (raw value in base)", {"form": "dict", "sa_cover": "name"}),
    ("dict, key=sig1 covered, raw header value in base", {"form": "dict", "sa_raw_base": True}),
    ("dict, member label sig2, signature label sig1", {"form": "dict", "sa_label": "sig2"}),
    ("dict-typed, key=sig1, raw header value in base", {"form": "dict-typed", "sa_raw_base": True}),
    ("bare, signature-agent not covered", {"form": "bare", "sa_cover": "none"}),
    ("dict, signature-agent not covered", {"form": "dict", "sa_cover": "none"}),
    ("bare, http:// agent URL", {"form": "bare", "sa_url": "http://http-message-signatures-example.research.cloudflare.com"}),
    ("dict, http:// agent URL", {"form": "dict", "sa_url": "http://http-message-signatures-example.research.cloudflare.com"}),
    ("bare, covers @query-param", {"form": "bare", "extra": (("@query-param", {"name": "q"}, "1"),)}),
    ("bare, tag=other", {"form": "bare", "tag": "other"}),
    ("bare, expired 60 s ago", {"form": "bare", "created_off": -180}),
    ("bare, created 600 s ahead", {"form": "bare", "created_off": 600}),
    ("bare, tampered", {"form": "bare", "tamper": True}),
    ("no Signature-Agent header", {"form": None}),
]


def run_interop_edges(pace: float = 0.4) -> dict[str, Any]:
    """q3 detail: which malformed variants each endpoint rejects and with what, so a 401 from
    crawltest (unknown key) can be told apart from a 400 (rejected format)."""
    rows = []
    for name, c in EDGE_CASES:
        c = dict(c)
        off = c.pop("created_off", 0)
        for endpoint, ok in ((RESEARCH_VERIFY, research_ok), (CRAWLTEST, crawltest_ok)):
            url = endpoint + "?q=1"
            hdrs = sign_request("GET", url, components="full", created=int(time.time()) + off, **c)
            r = http_get(url, hdrs)
            rows.append({"case": name, "endpoint": endpoint, "status": r["status"], "body": r["body"].strip(),
                         "accepted": ok(r), "headers": hdrs})
            time.sleep(pace)
    return {"rows": rows}


def summarize_interop(res: dict) -> list[str]:
    out = []
    for r in res["rows"]:
        name = "research" if r["endpoint"] == RESEARCH_VERIFY else "crawltest"
        tag = f"form={r['form']} nonce={r.get('nonce')} comps={r.get('components')}"
        if r.get("tamper"):
            tag += " TAMPERED"
        out.append(f"{name:9} {tag:45} -> {r['status']} {'ACCEPT' if r['accepted'] else 'reject'} "
                   f"{r['body'].strip()[:70]!r}")
    return out


# ---- q4: the node clock and a 120-second window ---------------------------------------------

def sntp(host: str = "time.cloudflare.com") -> dict[str, float]:
    best: dict[str, float] | None = None
    for _ in range(4):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(3)
            t0 = time.time()
            s.sendto(b"\x23" + 47 * b"\0", (host, 123))
            data, _ = s.recvfrom(48)
            t3 = time.time()
        def ts(off: int, data: bytes = data) -> float:
            sec, frac = struct.unpack("!II", data[off:off + 8])
            return sec - 2208988800 + frac / 2**32
        t1, t2 = ts(32), ts(40)
        sample = {"offset_ms": ((t1 - t0) + (t2 - t3)) / 2 * 1000, "delay_ms": ((t3 - t0) - (t2 - t1)) * 1000}
        if best is None or sample["delay_ms"] < best["delay_ms"]:
            best = sample
        time.sleep(0.2)
    assert best is not None
    return {k: round(v, 2) for k, v in best.items()}


def run_clock(form: str, *, replay_at: tuple[int, ...] = (0, 30, 60, 90, 110, 118, 125, 135),
              pace: float = 0.4) -> dict[str, Any]:
    out: dict[str, Any] = {"form": form}
    out["sntp_time_cloudflare_com"] = sntp()
    with contextlib.suppress(Exception):
        out["timedatectl"] = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "-p", "NTP"],
                                            capture_output=True, text=True, timeout=5, check=False).stdout.split()
    with contextlib.suppress(Exception):
        out["timesync_status"] = subprocess.run(["timedatectl", "timesync-status"], capture_output=True,
                                                text=True, timeout=5, check=False).stdout.strip().splitlines()
    # Boundary map: created offsets around now (future skew) and old signatures near expiry.
    bounds = []
    for endpoint, ok in ((RESEARCH_VERIFY, research_ok), (CRAWLTEST, crawltest_ok)):
        for created_off in (0, 1, 2, 5, 10, 30, 60, -60, -115, -119, -121, -125, -180):
            now = int(time.time())
            h = sign_request("GET", endpoint, form=form, components="full", created=now + created_off)
            r = http_get(endpoint, h)
            bounds.append({"endpoint": endpoint, "created_offset_s": created_off, "window_s": 120,
                           "expires_offset_s": created_off + 120, "status": r["status"],
                           "body": r["body"].strip()[:80], "accepted": ok(r)})
            time.sleep(pace)
    out["bounds"] = bounds
    # Real time: sign once per endpoint with a 120 s window, then send the same headers again
    # as the clock runs (this also shows that neither endpoint rejects a replayed nonce).
    t_sign = int(time.time())
    signed = {ep: sign_request("GET", ep, form=form, components="full", created=t_sign)
              for ep in (RESEARCH_VERIFY, CRAWLTEST)}
    replay = []
    for at in replay_at:
        delay = t_sign + at - time.time()
        if delay > 0:
            time.sleep(delay)
        for ep, ok in ((RESEARCH_VERIFY, research_ok), (CRAWLTEST, crawltest_ok)):
            r = http_get(ep, signed[ep])
            u = urlsplit(ep)
            local = verify_request("GET", "https", u.path, [("Host", u.netloc), *signed[ep].items()])
            replay.append({"endpoint": ep, "age_s": round(r["sent_at"] - t_sign, 1), "status": r["status"],
                           "body": r["body"].strip()[:80], "accepted": ok(r),
                           "local_verifier_valid": local.get("valid"), "local_reason": local.get("reason")})
    out["replay"] = replay
    out["sntp_after"] = sntp()
    return out


# ---- q1 and q2: the browser -----------------------------------------------------------------

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")

PROBE_JS = r"""
async () => {
  const n = navigator, o = {};
  for (const k of ['userAgent','appVersion','appCodeName','appName','platform','oscpu','product',
                   'productSub','vendor','vendorSub','buildID','language','hardwareConcurrency',
                   'maxTouchPoints','doNotTrack','cookieEnabled','pdfViewerEnabled','webdriver','onLine',
                   'globalPrivacyControl'])
    o['navigator.' + k] = n[k] === undefined ? '<undefined>' : n[k];
  o['navigator.languages'] = JSON.stringify(n.languages);
  o['navigator.plugins.length'] = n.plugins.length;
  o['navigator.mimeTypes.length'] = n.mimeTypes.length;
  o['navigator.serviceWorker (in)'] = 'serviceWorker' in n;
  o['navigator.userAgentData (in)'] = 'userAgentData' in n;
  for (const k of ['width','height','availWidth','availHeight','availTop','availLeft','colorDepth','pixelDepth'])
    o['screen.' + k] = screen[k];
  for (const k of ['innerWidth','innerHeight','outerWidth','outerHeight','screenX','screenY','devicePixelRatio'])
    o['window.' + k] = window[k];
  o['window.isSecureContext'] = window.isSecureContext;
  o['window.history.length'] = '<varies>';
  o['intl.timeZone'] = Intl.DateTimeFormat().resolvedOptions().timeZone;
  o['intl.locale'] = Intl.DateTimeFormat().resolvedOptions().locale;
  o['date.timezoneOffset'] = new Date().getTimezoneOffset();
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    o['webgl.vendor'] = gl.getParameter(gl.VENDOR);
    o['webgl.renderer'] = gl.getParameter(gl.RENDERER);
    if (ext) { o['webgl.unmaskedVendor'] = gl.getParameter(ext.UNMASKED_VENDOR_WEBGL);
               o['webgl.unmaskedRenderer'] = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL); }
    o['webgl.extensions'] = (gl.getSupportedExtensions() || []).length;
    o['webgl.maxTextureSize'] = gl.getParameter(gl.MAX_TEXTURE_SIZE);
  } catch (e) { o['webgl.error'] = String(e); }
  const c = document.createElement('canvas'); c.width = 240; c.height = 60;
  const x = c.getContext('2d'); x.textBaseline = 'top'; x.font = '16px Arial';
  x.fillStyle = '#f60'; x.fillRect(10, 10, 100, 30); x.fillStyle = '#069';
  x.fillText('openajar spike ☺ 1.0', 4, 20);
  o['canvas.dataURL'] = c.toDataURL();
  const fonts = ['Arial','Helvetica','Times New Roman','Courier New','Verdana','Georgia','DejaVu Sans',
                 'Liberation Sans','Segoe UI','Roboto','Ubuntu','Noto Sans','Menlo','Monaco','Calibri'];
  const m = document.createElement('canvas').getContext('2d');
  const w = f => { m.font = `40px "${f}", monospace`; return m.measureText('mmmmmmmmmlli10OO').width; };
  m.font = '40px monospace'; const base = m.measureText('mmmmmmmmmlli10OO').width;
  o['fonts.present'] = fonts.filter(f => w(f) !== base).join(',');
  try { const ac = new OfflineAudioContext(1, 4410, 44100); o['audio.sampleRate'] = ac.sampleRate; } catch (e) {}
  o['speech.voices'] = (window.speechSynthesis && speechSynthesis.getVoices().length) || 0;
  o['ed25519.webcrypto'] = await crypto.subtle.generateKey({name: 'Ed25519'}, false, ['sign'])
                             .then(() => true, e => String(e));
  return o;
}
"""

NAV_TIMING_JS = """() => { const e = performance.getEntriesByType('navigation')[0];
  return {duration: e.duration, ttfb: e.responseStart - e.requestStart, fetchToReq: e.requestStart - e.fetchStart,
          domContentLoaded: e.domContentLoadedEventEnd - e.startTime, protocol: e.nextHopProtocol}; }"""


class Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []

    def add(self, row: dict[str, Any]) -> None:
        with self.lock:
            self.rows.append(row)

    def take(self) -> list[dict[str, Any]]:
        with self.lock:
            rows, self.rows = self.rows, []
            return rows


def make_server(port: int, rec: Recorder, other_port: int) -> http.server.ThreadingHTTPServer:
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a: Any) -> None:
            pass

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            hdrs = list(self.headers.items())
            u = urlsplit(self.path)
            q = parse_qs(u.query)
            row = {"port": port, "method": self.command, "path": self.path, "headers": hdrs,
                   "body_len": len(body), "body_sha": hashlib.sha256(body).hexdigest()[:12] if body else None,
                   "verify": verify_request(self.command, "http", self.path, hdrs), "t": time.time()}
            rec.add(row)
            ctype, payload, status, extra = "text/html", b"", 200, {}
            if u.path == "/page":
                payload = (f'<!doctype html><html><head><title>spike</title>'
                           f'<link rel="stylesheet" href="/sub.css?k=css"></head><body><p>spike page</p>'
                           f'<img src="/sub.png?k=img"><img src="http://127.0.0.1:{other_port}/sub.png?k=xorigin-img">'
                           f'<script src="/sub.js?k=script"></script></body></html>').encode()
                extra["Set-Cookie"] = "spike=1; Path=/; SameSite=Lax"
            elif u.path == "/heavy":
                n = int(q.get("n", ["60"])[0])
                host = q.get("host", [""])[0]
                tags = "".join(f'<img src="{host}/sub.png?h={i}">' for i in range(n))
                tags += "".join(f'<script src="{host}/sub.js?h={i}"></script>' for i in range(10))
                payload = f"<!doctype html><html><head><title>heavy</title></head><body>{tags}</body></html>".encode()
            elif u.path == "/plain":
                payload = b"<!doctype html><html><head><title>plain</title></head><body>plain</body></html>"
            elif u.path == "/sub.png":
                ctype, payload = "image/png", PNG_1PX
            elif u.path == "/sub.css":
                ctype, payload = "text/css", b"body{margin:0}"
            elif u.path == "/sub.js":
                ctype, payload = "application/javascript", b"window.__subjs = 1;"
            elif u.path == "/api":
                ctype, payload = "application/json", json.dumps({"ok": True, "k": q.get("k")}).encode()
                extra["Access-Control-Allow-Origin"] = "*"
            elif u.path == "/redir":
                status, extra["Location"] = 302, q["to"][0]
                extra["Access-Control-Allow-Origin"] = "*"
            elif u.path == "/target":
                payload = b"<!doctype html><html><body>target</body></html>"
                extra["Access-Control-Allow-Origin"] = "*"
            elif u.path == "/favicon.ico":
                status = 404
            else:
                status = 404
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _handle

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Signing:
    """The route under test: sign every request whose authority is in sign_hosts."""

    def __init__(self, sign_hosts: dict[str, str], *, preserve: bool = False, components: str = "full",
                 drop: tuple[str, ...] = (), override: bool = True):
        self.sign_hosts = sign_hosts  # authority -> Signature-Agent form
        # preserve: rebuild the override from headers_array() so names keep their case and order,
        # and append the three signature headers last. Otherwise the documented dict merge.
        self.preserve = preserve
        self.components = components
        self.drop = {d.lower() for d in drop}  # header names left out of the override
        self.override = override  # False: the control, continue_() with no header changes
        self.events: list[dict[str, Any]] = []
        self.handler_ms: list[float] = []

    def matches(self, url: str) -> bool:
        return authority_of(url) in self.sign_hosts

    async def handler(self, route: Any, request: Any) -> None:
        t0 = time.perf_counter()
        form = self.sign_hosts.get(authority_of(request.url))
        if form is None:  # a callable matcher or a broad glob can hand us other hosts
            self.events.append({"url": request.url, "type": request.resource_type, "signed": False})
            await route.continue_()
            return
        if not self.override:
            self.events.append({"url": request.url, "type": request.resource_type, "signed": False})
            await route.continue_()
            return
        sig = sign_request(request.method, request.url, form=form, components=self.components)
        if self.preserve:
            headers = {h["name"]: h["value"] for h in await request.headers_array()}
            headers.update(sig)
        else:
            headers = {**request.headers, **{k.lower(): v for k, v in sig.items()}}
        headers = {k: v for k, v in headers.items() if k.lower() not in self.drop}
        self.events.append({"url": request.url, "type": request.resource_type, "signed": True,
                            "is_navigation": request.is_navigation_request(),
                            "redirected_from": request.redirected_from.url if request.redirected_from else None})
        self.handler_ms.append((time.perf_counter() - t0) * 1000)
        await route.continue_(headers=headers)

    async def install(self, ctx: Any, mode: str) -> Any:
        if mode == "glob":
            # one pattern per host, matched inside the Playwright driver
            pats = []
            for host in self.sign_hosts:
                scheme = "http" if host.startswith("127.0.0.1") else "https"
                pat = f"{scheme}://{host}/**"
                await ctx.route(pat, self.handler)
                pats.append(pat)
            return pats
        if mode == "callable":
            await ctx.route(self.matches, self.handler)
            return [self.matches]
        raise ValueError(mode)

    @staticmethod
    async def uninstall(ctx: Any, pats: list) -> None:
        for p in pats:
            await ctx.unroute(p)


def kill_leftovers(work: Path) -> list[str]:
    """Kill any browser still holding the throwaway profile (there should be none)."""
    pids = subprocess.run(["pgrep", "-f", str(work / "profile")], capture_output=True, text=True,
                          check=False).stdout.split()
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pid), signal.SIGKILL)
    return pids


@contextlib.contextmanager
def xvfb(display_num: int):
    if Path(f"/tmp/.X{display_num}-lock").exists():
        raise SystemExit(f"display :{display_num} is in use; refusing to touch it")
    proc = subprocess.Popen(["Xvfb", f":{display_num}", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if Path(f"/tmp/.X11-unix/X{display_num}").exists():
            break
        time.sleep(0.1)
    try:
        yield f":{display_num}"
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(5)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)


async def launch_lane(work: Path, display: str, extra: dict[str, Any] | None = None) -> Any:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from openajar.core.models import LaneSpec
    from openajar.node.camoufox.engine import CamoufoxEngine

    engine = CamoufoxEngine(extra_options=extra or {})
    # Loopback test servers: lift the engine's private-network PAC for this throwaway lane only.
    engine.allow_private_network = True  # type: ignore[attr-defined]
    spec = LaneSpec(id="spike/identity")
    return await engine.launch(spec, profile_dir=work / "profile", fingerprint_path=work / "fingerprint.json",
                               display=display, proxy=None)


def doc_headers(rows: list[dict], path_prefix: str, port: int | None = None) -> list[tuple[str, str]] | None:
    for r in rows:
        if r["path"].startswith(path_prefix) and (port is None or r["port"] == port):
            return r["headers"]
    return None


def strip_sig(hdrs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(k, v) for k, v in hdrs if k.lower() not in ("signature", "signature-input", "signature-agent")]


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, round(p * (len(xs) - 1)))], 2)


def stats(xs: list[float]) -> dict[str, float]:
    return {"n": len(xs), "median": round(statistics.median(xs), 2), "p90": pct(xs, 0.9),
            "mean": round(statistics.fmean(xs), 2), "min": round(min(xs), 2), "max": round(max(xs), 2)}


async def probe(page: Any, url: str) -> dict[str, Any]:
    await page.goto(url, wait_until="load")
    o = await page.evaluate(PROBE_JS)
    o["canvas.sha256"] = hashlib.sha256(o.pop("canvas.dataURL", "").encode()).hexdigest()[:16]
    return o


async def q1_local(ctx: Any, rec: Recorder, signed: str, unsigned: str, form: str, *,
                   preserve: bool = False, components: str = "full", drop: tuple[str, ...] = (),
                   override: bool = True, no_route: bool = False) -> dict[str, Any]:
    signing = Signing({signed: form}, preserve=preserve, components=components, drop=drop, override=override)
    pats = [] if no_route else await signing.install(ctx, "glob")
    page = await ctx.new_page()
    rec.take()
    s, u = f"http://{signed}", f"http://{unsigned}"
    await page.goto(f"{s}/page", wait_until="load")
    await page.evaluate("""async () => {
      await fetch('/api?k=fetch');
      await fetch('/api?k=fetch-post', {method: 'POST', body: 'hello body', headers: {'Content-Type': 'text/plain'}});
      await new Promise(r => { const x = new XMLHttpRequest(); x.open('GET', '/api?k=xhr'); x.onloadend = r; x.send(); });
      await fetch('/redir?to=' + encodeURIComponent('/api?k=fetch-redir-same'));
      await fetch('/redir?to=' + encodeURIComponent('""" + u + """/api?k=fetch-redir-cross'));
    }""")
    await page.wait_for_timeout(300)
    await page.goto(f"{s}/redir?to=" + "/target%3Fk%3Dnav-redir-same", wait_until="load")
    await page.goto(f"{s}/redir?to=" + f"{u}/target%3Fk%3Dnav-redir-to-unsigned", wait_until="load")
    await page.goto(f"{u}/redir?to=" + f"{s}/target%3Fk%3Dnav-redir-into-signed", wait_until="load")
    await page.wait_for_timeout(300)
    rows = rec.take()
    await Signing.uninstall(ctx, pats)
    await page.close()
    table = []
    for r in rows:
        if r["path"].startswith("/favicon"):
            continue
        h = {k.lower(): v for k, v in r["headers"]}
        table.append({"port": r["port"], "host_header": h.get("host"), "method": r["method"], "path": r["path"],
                      "header_names": [k for k, _ in r["headers"]],
                      "user_agent": h.get("user-agent"),
                      "sec_fetch_dest": h.get("sec-fetch-dest"), "sec_fetch_mode": h.get("sec-fetch-mode"),
                      "cookie": h.get("cookie"), "body_len": r["body_len"], "verify": r["verify"],
                      "signature_input": h.get("signature-input"), "signature_agent": h.get("signature-agent")})
    return {"form": form, "preserve": preserve, "components": components, "server_rows": table,
            "route_events": signing.events,
            "handler_ms": stats(signing.handler_ms) if signing.handler_ms else None}


async def q1_cloudflare(ctx: Any, form: str, *, preserve: bool = False) -> dict[str, Any]:
    research_host = urlsplit(RESEARCH).netloc
    signing = Signing({research_host: form, "crawltest.com": form}, preserve=preserve)
    pats = await signing.install(ctx, "glob")
    page = await ctx.new_page()
    bodies: list[dict[str, Any]] = []

    async def on_response(resp: Any) -> None:
        if "/v0/api/verify" in resp.url or "web-bot-auth" in resp.url:
            try:
                txt = (await resp.text())[:120]
            except Exception as e:  # noqa: BLE001
                txt = f"<no body: {e}>"
            bodies.append({"url": resp.url, "status": resp.status, "type": resp.request.resource_type,
                           "body": txt.strip()})
    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
    out: dict[str, Any] = {"form": form, "preserve": preserve}
    resp = await page.goto(f"{RESEARCH_VERIFY}?k=document", wait_until="load")
    out["research_document"] = {"status": resp.status, "body": (await page.inner_text("body")).strip()[:120]}
    out["research_js"] = await page.evaluate("""async () => {
      const f = await fetch('/v0/api/verify?k=fetch').then(r => r.text());
      const x = await new Promise(res => { const q = new XMLHttpRequest(); q.open('GET', '/v0/api/verify?k=xhr');
        q.onloadend = () => res(q.responseText); q.send(); });
      const img = new Image(); img.src = '/v0/api/verify?k=img'; document.body.appendChild(img);
      const s = document.createElement('script'); s.src = '/v0/api/verify?k=script'; document.body.appendChild(s);
      return {fetch: f.trim(), xhr: x.trim()};
    }""")
    await page.wait_for_timeout(1500)
    resp = await page.goto(CRAWLTEST + "?k=document", wait_until="load")
    out["crawltest_document"] = {"status": resp.status, "body": (await page.inner_text("body")).strip()[:120]}
    out["crawltest_js"] = await page.evaluate("""async () => {
      const f = await fetch('/cdn-cgi/web-bot-auth?k=fetch'); const x = await new Promise(res => {
        const q = new XMLHttpRequest(); q.open('GET', '/cdn-cgi/web-bot-auth?k=xhr');
        q.onloadend = () => res([q.status, q.responseText]); q.send(); });
      const img = new Image(); img.src = '/cdn-cgi/web-bot-auth?k=img'; document.body.appendChild(img);
      return {fetch: [f.status, (await f.text()).trim()], xhr: [x[0], x[1].trim()]};
    }""")
    await page.wait_for_timeout(1500)
    out["responses"] = bodies
    out["route_events"] = [{"url": e["url"], "type": e["type"], "signed": e["signed"]} for e in signing.events]
    await Signing.uninstall(ctx, pats)
    await page.close()
    return out


async def q2_timing(ctx: Any, rec: Recorder, signed: str, unsigned: str, form: str, n: int,
                    remote_n: int) -> dict[str, Any]:
    """Interleaved navigations: route off, glob route on, callable route on. Signed host and an
    unsigned host on loopback, then the Cloudflare research page off and on."""
    page = await ctx.new_page()
    signing = Signing({signed: form})
    modes = ("none", "glob", "callable")
    res: dict[str, dict[str, list[float]]] = {f"{m}/{h}": {"goto_ms": [], "duration": [], "ttfb": []}
                                              for m in modes for h in ("signed_host", "unsigned_host")}
    for i in range(n):
        for mode in modes:
            pats = await signing.install(ctx, mode) if mode != "none" else []
            for hname, host in (("signed_host", signed), ("unsigned_host", unsigned)):
                t0 = time.perf_counter()
                await page.goto(f"http://{host}/plain?i={i}&m={mode}", wait_until="load")
                goto_ms = (time.perf_counter() - t0) * 1000
                nt = await page.evaluate(NAV_TIMING_JS)
                bucket = res[f"{mode}/{hname}"]
                bucket["goto_ms"].append(goto_ms)
                bucket["duration"].append(nt["duration"])
                bucket["ttfb"].append(nt["ttfb"])
            if pats:
                await Signing.uninstall(ctx, pats)
    rec.take()
    out: dict[str, Any] = {k: {m: stats(v) for m, v in b.items()} for k, b in res.items()}
    remote: dict[str, list[float]] = {"none": [], "signed": []}
    research_host = urlsplit(RESEARCH).netloc
    rsig = Signing({research_host: form})
    for i in range(remote_n):
        for mode in ("none", "signed"):
            pats = await rsig.install(ctx, "glob") if mode == "signed" else []
            t0 = time.perf_counter()
            await page.goto(f"{RESEARCH}/v0/api/verify?i={i}&m={mode}", wait_until="load")
            remote[mode].append((time.perf_counter() - t0) * 1000)
            if pats:
                await Signing.uninstall(ctx, pats)
            await asyncio.sleep(0.3)
    out["remote_research_goto_ms"] = {m: stats(v) for m, v in remote.items()}
    # A heavier page: 60 images and 10 scripts from the same host, so every subresource is routed.
    heavy: dict[str, list[float]] = {f"{m}/{h}": [] for m in ("none", "glob", "glob-preserve")
                                     for h in ("signed_host", "unsigned_host")}
    for i in range(max(5, n // 3)):
        for mode in ("none", "glob", "glob-preserve"):
            sg = Signing({signed: form}, preserve=mode == "glob-preserve")
            pats = await sg.install(ctx, "glob") if mode != "none" else []
            for hname, host in (("signed_host", signed), ("unsigned_host", unsigned)):
                t0 = time.perf_counter()
                await page.goto(f"http://{host}/heavy?n=60&i={i}&m={mode}", wait_until="load")
                heavy[f"{mode}/{hname}"].append((time.perf_counter() - t0) * 1000)
            if pats:
                await Signing.uninstall(ctx, pats)
    rec.take()
    out["heavy_page_goto_ms"] = {k: stats(v) for k, v in heavy.items()}
    out["handler_ms"] = stats(signing.handler_ms + rsig.handler_ms)
    await page.close()
    return out


async def run_browser(args: argparse.Namespace) -> dict[str, Any]:
    work = Path(args.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    signed, unsigned = f"127.0.0.1:{args.base_port + 1}", f"127.0.0.1:{args.base_port + 2}"
    rec = Recorder()
    servers = [make_server(args.base_port + 1, rec, args.base_port + 2),
               make_server(args.base_port + 2, rec, args.base_port + 1)]
    out: dict[str, Any] = {"signed_host": signed, "unsigned_host": unsigned}
    try:
        with xvfb(args.display) as display:
            # Launch 0 pins the fingerprint, exactly as a new lane does.
            s = await launch_lane(work, display)
            out["camoufox_pid"] = s.pid
            await s.close()
            pin = json.loads((work / "fingerprint.json").read_text())
            out["pin_keys"] = len(pin)
            probes: dict[str, Any] = {}
            doc_hdrs: dict[str, Any] = {}
            sub_hdrs: dict[str, Any] = {}
            plan = [("A_no_route", None, None, False), ("B_route_glob", "glob", None, False),
                    ("B2_route_glob_preserve", "glob", None, True), ("C_no_route", None, None, False),
                    ("D_route_callable", "callable", None, False),
                    ("E_route_glob_sw_block", "glob", {"service_workers": "block"}, False)]
            xo_hdrs: dict[str, Any] = {}
            for name, mode, extra, preserve in plan:
                s = await launch_lane(work, display, extra)
                ctx = s.context
                signing = Signing({signed: args.form}, preserve=preserve)
                if mode:
                    await signing.install(ctx, mode)
                page = await ctx.new_page()
                rec.take()
                probes[name] = await probe(page, f"http://{signed}/page")
                await page.wait_for_timeout(300)
                rows = rec.take()
                doc_hdrs[name] = doc_headers(rows, "/page", args.base_port + 1)
                sub_hdrs[name] = doc_headers(rows, "/sub.png", args.base_port + 1)
                xo_hdrs[name] = doc_headers(rows, "/sub.png", args.base_port + 2)
                if name == "A_no_route":
                    probes["A2_same_launch"] = await probe(page, f"http://{signed}/page")
                    rec.take()
                if name == "B_route_glob":
                    await ctx.unroute_all()
                    out["q1_local"] = {f: await q1_local(ctx, rec, signed, unsigned, f) for f in ("bare", "dict")}
                    out["q1_local"]["bare-min"] = await q1_local(ctx, rec, signed, unsigned, "bare", components="min")
                    out["q1_cloudflare"] = {f: await q1_cloudflare(ctx, f) for f in ("bare", "dict")}
                    out["q2_timing"] = await q2_timing(ctx, rec, signed, unsigned, args.form, args.n, args.remote_n)
                if name == "B2_route_glob_preserve":
                    await ctx.unroute_all()
                    out["q1_local"]["bare-preserve"] = await q1_local(ctx, rec, signed, unsigned, "bare", preserve=True)
                    out["q1_cloudflare"]["bare-preserve"] = await q1_cloudflare(ctx, "bare", preserve=True)
                await s.close()
            base = probes["A_no_route"]
            out["q2_probes"] = probes
            out["q2_probe_diffs"] = {name: {k: [base.get(k), p.get(k)] for k in set(base) | set(p)
                                            if base.get(k) != p.get(k)} for name, p in probes.items()}
            # navigator values against the pin itself
            pin_cmp = {}
            for k, v in pin.items():
                if k in base:
                    pin_cmp[k] = {"pin": v, "page": base[k], "equal": v == base[k]}
            out["q2_pin_vs_page"] = pin_cmp
            out["q2_doc_headers"] = {k: v for k, v in doc_hdrs.items()}
            out["q2_header_diffs"] = {
                name: {"document_same_order_and_values": strip_sig(h or []) == strip_sig(doc_hdrs["A_no_route"] or []),
                       "subresource_same_order_and_values": strip_sig(sub_hdrs[name] or []) ==
                       strip_sig(sub_hdrs["A_no_route"] or []),
                       "unsigned_host_same_order_and_values": (xo_hdrs[name] or []) == (xo_hdrs["A_no_route"] or []),
                       "document_names": [k for k, _ in (h or [])],
                       "subresource_names": [k for k, _ in (sub_hdrs[name] or [])]}
                for name, h in doc_hdrs.items()}
            out["q2_unsigned_host_headers"] = xo_hdrs
            out["q2_sub_headers"] = sub_hdrs
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()
        out["leftover_browser_pids"] = kill_leftovers(work)
    return out


# drop everything from the override except the three signature headers: does adding headers alone
# (without re-setting any existing one) still pin Host on the redirect hop?
SIGNATURE_AND_PAGE_HEADERS = ("host", "user-agent", "accept", "accept-language", "accept-encoding",
                              "upgrade-insecure-requests", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
                              "sec-fetch-user", "connection", "cookie", "referer", "priority", "content-type",
                              "origin", "content-length")


async def _has_host(ctx: Any, signed: str) -> dict[str, Any]:
    """What Playwright hands the route handler: is Host among request.headers / headers_array()?"""
    seen: dict[str, Any] = {}

    async def h(route: Any, request: Any) -> None:
        seen["headers_keys"] = sorted(request.headers)
        seen["headers_array_names"] = [x["name"] for x in await request.headers_array()]
        await route.continue_()
    await ctx.route(f"http://{signed}/**", h)
    page = await ctx.new_page()
    await page.goto(f"http://{signed}/plain?k=hostcheck")
    await ctx.unroute_all()
    await page.close()
    return seen


NOISY_KEYS = ("canvas.sha256", "speech.voices", "window.history.length")


async def run_repeat(args: argparse.Namespace) -> dict[str, Any]:
    """q2 detail: K launches alternating no route and route, one probe each, every probe value
    compared with the first launch and with the pin. Also one q1_local pass that records the Host
    header on redirect hops."""
    work = Path(args.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    signed, unsigned = f"127.0.0.1:{args.base_port + 1}", f"127.0.0.1:{args.base_port + 2}"
    rec = Recorder()
    servers = [make_server(args.base_port + 1, rec, args.base_port + 2),
               make_server(args.base_port + 2, rec, args.base_port + 1)]
    out: dict[str, Any] = {"launches": []}
    try:
        with xvfb(args.display) as display:
            s = await launch_lane(work, display)
            await s.close()
            pin = json.loads((work / "fingerprint.json").read_text())
            first: dict[str, Any] | None = None
            for i in range(args.repeat):
                route = i % 2 == 1
                s = await launch_lane(work, display)
                ctx = s.context
                if route:
                    await Signing({signed: args.form}).install(ctx, "glob")
                page = await ctx.new_page()
                p = await probe(page, f"http://{signed}/page")
                if i == 1:
                    await ctx.unroute_all()
                    out["q1_local_hosts"] = await q1_local(ctx, rec, signed, unsigned, "bare", components="min")
                    out["q1_local_hosts_full"] = await q1_local(ctx, rec, signed, unsigned, "bare")
                    out["q1_local_hosts_drop_host"] = await q1_local(ctx, rec, signed, unsigned, "bare", drop=("host",))
                    out["q1_local_hosts_control_no_route"] = await q1_local(ctx, rec, signed, unsigned, "bare",
                                                                            no_route=True)
                    out["q1_local_hosts_control_no_override"] = await q1_local(ctx, rec, signed, unsigned, "bare",
                                                                               override=False)
                    out["q1_local_hosts_extra_header_only"] = await q1_local(
                        ctx, rec, signed, unsigned, "bare", drop=tuple(SIGNATURE_AND_PAGE_HEADERS))
                    out["request_headers_has_host"] = await _has_host(ctx, signed)
                rec.take()
                await s.close()
                first = first or p
                diff = {k: [first.get(k), p.get(k)] for k in set(first) | set(p)
                        if k not in NOISY_KEYS and first.get(k) != p.get(k)}
                pin_diff = {k: [pin[k], p[k]] for k in pin if k in p and pin[k] != p[k]}
                out["launches"].append({"i": i, "route": route, "maxTouchPoints": p.get("navigator.maxTouchPoints"),
                                        "diff_vs_first": diff, "diff_vs_pin": pin_diff,
                                        "pinned_keys_checked": sum(1 for k in pin if k in p)})
            out["pin_has_maxTouchPoints"] = "navigator.maxTouchPoints" in pin
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()
        out["leftover_browser_pids"] = kill_leftovers(work)
    return out


def summarize_browser(b: dict) -> list[str]:
    out = []
    for form, q in b.get("q1_local", {}).items():
        out.append(f"q1 local form={form}:")
        for r in q["server_rows"]:
            v = r["verify"]
            st = "unsigned" if not v.get("signed") else ("VALID" if v.get("valid") else f"INVALID ({v.get('reason')})")
            out.append(f"   :{r['port']} {r['method']:4} {r['path'][:60]:60} dest={r['sec_fetch_dest']:9} "
                       f"cookie={r['cookie']!s:9} {st}")
    for form, q in b.get("q1_cloudflare", {}).items():
        out.append(f"q1 cloudflare form={form}: research doc={q['research_document']} js={q['research_js']}")
        out.append(f"   crawltest doc={q['crawltest_document']} js={q['crawltest_js']}")
        for r in q["responses"]:
            out.append(f"   resp {r['type']:10} {r['status']} {r['body'][:40]!r} {r['url'][-40:]}")
    out.append("q2 probe diffs vs A_no_route: " + json.dumps(b.get("q2_probe_diffs")))
    out.append("q2 header diffs [doc, sub, unsigned-host]: " + json.dumps({k: [v["document_same_order_and_values"],
                                                      v["subresource_same_order_and_values"],
                                                      v["unsigned_host_same_order_and_values"]]
                                                  for k, v in b.get("q2_header_diffs", {}).items()}))
    out.append("q2 pin mismatches: " + json.dumps({k: v for k, v in b.get("q2_pin_vs_page", {}).items()
                                                    if not v["equal"]}))
    t = b.get("q2_timing", {})
    for k, v in t.items():
        out.append(f"q2 timing {k}: {json.dumps(v)}")
    return out


def selftest() -> dict[str, Any]:
    """The signer against published vectors: RFC 9421 B.2.6 and the protocol draft's E.2.1
    (dictionary Signature-Agent) and E.2.2 (legacy sf-string). Ed25519 is deterministic, so a
    byte-exact match proves the signature base serialization."""
    out: dict[str, Any] = {"keyid": KEYID, "keyid_matches_published": KEYID == "poqkLGiymh_W0uP6PZFw-dvez3QJT5SolqXBCW38r0U"}

    def sig(comps: list[tuple[str, dict]], vals: dict[str, str], params: dict[str, Any]) -> str:
        lines = [f"{ser_bare(n)}{ser_params(p)}: {vals[ser_bare(n) + ser_params(p)]}" for n, p in comps]
        lines.append(f'"@signature-params": {ser_inner(comps, params)}')
        return base64.b64encode(PRIVATE_KEY.sign("\n".join(lines).encode())).decode()

    comps = [("date", {}), ("@method", {}), ("@path", {}), ("@authority", {}), ("content-type", {}),
             ("content-length", {})]
    vals = {'"date"': "Tue, 20 Apr 2021 02:07:55 GMT", '"@method"': "POST", '"@path"': "/foo",
            '"@authority"': "example.com", '"content-type"': "application/json", '"content-length"': "18"}
    out["rfc9421_B.2.6"] = sig(comps, vals, {"created": 1618884473, "keyid": "test-key-ed25519"}) == \
        "wqcAqbmYJ2ji2glfAMaRy4gruYYnx2nEFN2HN6jrnDnQCK1u02Gb04v9EDgwUPiu4A0w6vuQv5lIp5WPpBKRCw=="
    sa = {"signature-agent": 'sig2="https://signature-agent.test"'}
    comps = [("@authority", {}), ("signature-agent", {"key": "sig2"})]
    vals = {'"@authority"': "example.com",
            '"signature-agent";key="sig2"': component_value("signature-agent", {"key": "sig2"}, sa)}
    out["draft_E.2.1_dict"] = sig(comps, vals, {
        "created": 1735689600, "keyid": KEYID, "alg": "ed25519", "expires": 4889289600,
        "nonce": "n9p433xm+NJ3ph3upfBIGmsuwHw387YV7Q/F+6BSpGCVjYCqQw6rznNA8PVVLySrAWsv0hQtFioQb6E1YsauiA==",
        "tag": "web-bot-auth"}) == "CGokAoY/FrPpyuzTe1Fb2kLTAH9Idw6qSFd1+D77r1firsMRCxeuLu5e6O+ek3zQgDMAzjiXtLVfqE00xnABCQ=="
    comps = [("@authority", {}), ("signature-agent", {})]
    vals = {'"@authority"': "example.com", '"signature-agent"': '"https://signature-agent.test"'}
    out["draft_E.2.2_legacy"] = sig(comps, vals, {
        "created": 1735689600, "keyid": KEYID, "alg": "ed25519", "expires": 1735693200,
        "nonce": "e8N7S2MFd/qrd6T2R3tdfAuuANngKI7LFtKYI/vowzk4lAZYadIX6wW25MwG7DCT9RUKAJ0qVkU0mEeLElW1qg==",
        "tag": "web-bot-auth"}) == "jdq0SqOwHdyHr9+r5jw3iYZH6aNGKijYp/EstF4RQTQdi5N5YYKrD+mCT1HA1nZDsi6nJKuHxUi/5Syp3rLWBA=="
    h = sign_request("GET", "http://127.0.0.1:6331/x?y=1", form="dict")
    out["roundtrip_local_verifier"] = verify_request("GET", "http", "/x?y=1",
                                                     [("Host", "127.0.0.1:6331"), *h.items()]).get("valid")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["interop", "edges", "clock", "browser", "repeat", "all", "selftest"])
    ap.add_argument("--repeat", type=int, default=12, help="launches for the repeat check")
    ap.add_argument("--out", default="web_bot_auth_results.json")
    ap.add_argument("--work", default=".agentos-home/spike-wba")
    ap.add_argument("--display", type=int, default=int(os.environ.get("DISPLAY_NUM", "330")))
    ap.add_argument("--base-port", type=int, default=6330)
    ap.add_argument("--form", default="bare", help="Signature-Agent form for the browser timing runs")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--remote-n", type=int, default=10)
    args = ap.parse_args()
    if args.display in (99, *range(100, 110)) or args.base_port in (5900, 6080, 6100, 7400):
        raise SystemExit("refusing a reserved display or port")
    results: dict[str, Any] = {"keyid": KEYID, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if args.what == "selftest":
        print(json.dumps(selftest(), indent=1))
        return
    if args.what in ("interop", "all"):
        results["selftest"] = selftest()
        print(json.dumps(results["selftest"]), flush=True)
        results["interop"] = run_interop()
        print("\n".join(summarize_interop(results["interop"])), flush=True)
    if args.what in ("edges", "all"):
        results["interop_edges"] = run_interop_edges()
        for r in results["interop_edges"]["rows"]:
            print(f"{'research' if r['endpoint'] == RESEARCH_VERIFY else 'crawltest':9} {r['case']:58} -> "
                  f"{r['status']} {'ACCEPT' if r['accepted'] else 'reject'} {r['body'][:75]!r}", flush=True)
    if args.what in ("browser", "all"):
        results["browser"] = asyncio.run(run_browser(args))
        print("\n".join(summarize_browser(results["browser"])), flush=True)
    if args.what in ("repeat", "all"):
        results["repeat"] = asyncio.run(run_repeat(args))
        for row in results["repeat"]["launches"]:
            print(json.dumps(row), flush=True)
        print("request.headers includes host:", results["repeat"].get("request_headers_has_host"))
        for key in ("q1_local_hosts", "q1_local_hosts_full", "q1_local_hosts_drop_host",
                    "q1_local_hosts_control_no_route", "q1_local_hosts_control_no_override",
                    "q1_local_hosts_extra_header_only"):
            for r in results["repeat"].get(key, {}).get("server_rows", []):
                v = r["verify"]
                st = "unsigned" if not v.get("signed") else ("VALID" if v.get("valid") else "INVALID")
                print(f"{key} :{r['port']} Host={r['host_header']} {r['path'][:60]} {st}", flush=True)
    if args.what in ("clock", "all"):
        results["clock"] = run_clock(args.form)
        print(json.dumps({k: v for k, v in results["clock"].items() if k != "bounds"}, indent=1), flush=True)
        for r in results["clock"]["bounds"]:
            print(f"clock {r['endpoint'][-30:]:30} created{r['created_offset_s']:+5d}s -> {r['status']} "
                  f"{'ACCEPT' if r['accepted'] else 'reject'} {r['body'][:60]!r}")
    Path(args.out).write_text(json.dumps(results, indent=1, default=str))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
