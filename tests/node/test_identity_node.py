"""Web Bot Auth signing on a real Camoufox lane (docs/design/agent-identity.md, Phase 1 acceptance).

Run on the node:  AGENTOS_TEST_SLOT=35 scripts/node-test.sh identity1 tests/node/test_identity_node.py -q

Throwaway lanes under .node-home/ of this checkout, launched through `CamoufoxEngine` with the
product's `IdentityProvider`, one browser at a time on the slot's first display. Two loopback
servers on the slot's last viewer ports: SIGNED (in the lane's sign_hosts) and OTHER (not). Every
request they receive is checked with the strict verifier against the lane's public key. The
redirect tests prove the guard: a signed host that redirects to another host never hands it a
signature, and one test pins today's Playwright behaviour with header overrides (the reason the
route does not use them). One interop test sends the route's signatures to Cloudflare's research
deployment, signed with its published test key (RFC 9421 B.1.4); set AGENTOS_TEST_NO_INTERNET=1 to
skip it. No real site, no owner account.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import statistics
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentos.core.config import NodeConfig
from agentos.core.models import LaneSpec
from agentos.node.display import DisplayManagerImpl, kill_processes
from agentos.node.engine_camoufox import PIN_META, CamoufoxEngine, browser_pids_under
from agentos.webbotauth import base, keys, settings
from agentos.webbotauth.route import IdentityProvider, LaneSigner
from agentos.webbotauth.settings import LaneIdentity
from agentos.webbotauth.trace import IdentityTrace
from agentos.webbotauth.verify import verify_request

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb, x11vnc and Camoufox"),
]

ROOT = Path(os.environ.get("AGENTOS_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
DISPLAY_BASE = int(os.environ.get("AGENTOS_TEST_DISPLAY_BASE", "350"))
VNC_BASE = int(os.environ.get("AGENTOS_TEST_VNC_BASE", "5350"))
VIEWER_BASE = int(os.environ.get("AGENTOS_TEST_VIEWER_BASE", "6350"))
SIGNED_PORT, OTHER_PORT = VIEWER_BASE + 9, VIEWER_BASE + 8
SIGNED = f"http://127.0.0.1:{SIGNED_PORT}"
OTHER = f"http://127.0.0.1:{OTHER_PORT}"
CROSS_SITE = f"http://localhost:{OTHER_PORT}"  # another site for cookies (127.0.0.1 and its ports are one)
AGENT = "https://agents.example"
SITE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "site"
EVIDENCE = os.environ.get("AGENTOS_TEST_EVIDENCE")
PNG_1PX = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
RESEARCH = "https://http-message-signatures-example.research.cloudflare.com"
NETWORK_LAYER_HEADERS = {"priority", "pragma", "cache-control"}
TEST_KEY_D = "n4Ni-HpISpVObnQMW0wOhCKROaIKqKtW_2ZYb2p9KcU"  # RFC 9421 B.1.4, published; tests only


class Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []
        self.keys: dict[str, Any] = {}

    def add(self, row: dict[str, Any]) -> None:
        with self.lock:
            self.rows.append(row)

    def take(self) -> list[dict[str, Any]]:
        with self.lock:
            rows, self.rows = self.rows, []
        return rows


def make_server(port: int, rec: Recorder, other: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            pairs = list(self.headers.items())
            u = urlsplit(self.path)
            q = parse_qs(u.query)
            host = self.headers.get("Host", "")
            check = verify_request(self.command, f"http://{host}{self.path}", pairs, rec.keys, nonce_bytes=64)
            rec.add({"port": port, "method": self.command, "path": self.path, "host": host, "headers": pairs,
                     "body": body, "check": check})
            ctype, payload, status, extra = "text/html", b"", 200, []
            if u.path == "/page":
                payload = (f'<!doctype html><html><head><link rel="stylesheet" href="/s.css"></head><body>'
                           f'<img src="/i.png"><img src="{other}/i.png?k=other-img">'
                           f'<script src="/s.js"></script></body></html>').encode()
                extra = [("Set-Cookie", "a=1; Path=/; SameSite=Lax"), ("Set-Cookie", "b=2; Path=/; HttpOnly")]
            elif u.path == "/embed":
                payload = f'<!doctype html><html><body><img src="{other}/i.png?k=cross-site"></body></html>'.encode()
            elif u.path == "/headers":
                ctype = "application/json"
                payload = json.dumps({k.lower(): v for k, v in pairs if k.lower() not in ("host", "connection")},
                                     sort_keys=True).encode()
            elif u.path == "/gz":
                ctype, payload = "application/json", gzip.compress(b'{"gz": true}')
                extra = [("Content-Encoding", "gzip"), ("Access-Control-Allow-Origin", "*")]
            elif u.path == "/i.png":
                ctype, payload = "image/png", PNG_1PX
            elif u.path == "/s.css":
                ctype, payload = "text/css", b"body{margin:0}"
            elif u.path == "/s.js":
                ctype, payload = "application/javascript", b"document.documentElement.dataset.sjs = '1';"
            elif u.path == "/api":
                ctype = "application/json"
                payload = json.dumps({"k": q.get("k", [""])[0], "len": len(body)}).encode()
                extra = [("Access-Control-Allow-Origin", "*")]
            elif u.path == "/redir":
                status, extra = 302, [("Location", q["to"][0]), ("Access-Control-Allow-Origin", "*")]
            elif u.path == "/target":
                payload = b"<!doctype html><html><body>target</body></html>"
                extra = [("Access-Control-Allow-Origin", "*")]
            elif (SITE_DIR / u.path.lstrip("/")).is_file() and ".." not in u.path:
                payload = (SITE_DIR / u.path.lstrip("/")).read_bytes()
                ctype = "text/html" if u.path.endswith(".html") else "application/octet-stream"
            else:
                status = 404
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _handle

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def rec():
    recorder = Recorder()
    servers = [make_server(SIGNED_PORT, recorder, OTHER), make_server(OTHER_PORT, recorder, SIGNED)]
    try:
        yield recorder
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


@pytest.fixture
async def rig():
    home = ROOT / f"identity-{uuid.uuid4().hex[:8]}"
    home.mkdir(parents=True)
    config = NodeConfig(home=home, display_base=DISPLAY_BASE, vnc_base=VNC_BASE, viewer_base=VIEWER_BASE,
                        max_slots=4, self_check=False)
    displays = DisplayManagerImpl(config)
    await displays.sweep()
    events: list[tuple[str, str, dict]] = []
    engine = CamoufoxEngine(identity=IdentityProvider(config, events=lambda *a: events.append(a)))
    engine.allow_private_network = True  # only so these lanes can reach the loopback servers
    yield config, displays, engine, events
    await displays.release_all()
    leftover = browser_pids_under(home)
    await kill_processes(leftover)
    shutil.rmtree(home, ignore_errors=True)
    assert not leftover, f"browsers left running after the test: {leftover}"


def give_identity(config: NodeConfig, lane_id: str, hosts: list[str], *, agent: str = AGENT,
                  key_d: str | None = None) -> keys.LaneKey:
    """What `agentos identity init` and `hosts add` do, for a throwaway lane."""
    profile = config.profile_dir(lane_id)
    profile.mkdir(parents=True, exist_ok=True)
    key = keys.create_key(config.secrets_dir, lane_id)
    if key_d is not None:  # the interop test only: the published test key
        test_key = Ed25519PrivateKey.from_private_bytes(base.b64url_decode(key_d))
        keys.key_path(config.secrets_dir, lane_id).write_bytes(test_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key = keys.load_key(config.secrets_dir, lane_id)
    ident = LaneIdentity(lane_id=lane_id, signature_agent=agent, keyid=key.keyid,
                         profile=settings.profile_binding(profile))
    for h in hosts:
        settings.add_host(ident, h, reason="node test", by="owner:test")
    settings.save(config.secrets_dir, ident)
    return key


class Lane:
    def __init__(self, config, displays, engine, spec):
        self.config, self.displays, self.engine, self.spec = config, displays, engine, spec
        self.session: Any = None

    async def __aenter__(self):
        fp = self.config.fingerprint_path(self.spec.id)
        viewer = await self.displays.acquire(self.spec.id, size=self.engine.display_size(fp))
        self.session = await self.engine.launch(self.spec, profile_dir=self.config.profile_dir(self.spec.id),
                                                fingerprint_path=fp, display=viewer.display,
                                                xauthority=viewer.xauthority, proxy=None)
        return self.session

    async def __aexit__(self, *exc):
        await self.session.close()
        await self.displays.release(self.spec.id)


def keep(name: str, data: Any) -> None:
    if EVIDENCE:
        path = Path(EVIDENCE)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{name}.json").write_text(json.dumps(data, indent=1, sort_keys=True, default=str))


def sig_headers(row: dict) -> list[str]:
    return [k for k, _ in row["headers"] if k.lower() in base.SIGNATURE_HEADERS]


def summary(rows: list[dict]) -> list[dict]:
    return [{"port": r["port"], "method": r["method"], "path": r["path"], "host": r["host"],
             "signed": bool(sig_headers(r)), "valid": r["check"].valid, "reason": r["check"].reason,
             "header_names": [k for k, _ in r["headers"]]} for r in rows if not r["path"].startswith("/favicon")]


async def test_route_signs_every_kind_of_request_to_a_listed_host_and_nothing_else(rig, rec):
    config, displays, engine, _ = rig
    spec = LaneSpec(id="test/id-kinds")
    key = give_identity(config, spec.id, [SIGNED])
    rec.keys = {key.keyid: key.public_key()}
    async with Lane(config, displays, engine, spec) as session:
        assert session.signer is not None and session.signer.patterns() == [f"{SIGNED}/**"]
        assert [k for k, _ in session.pin_notes][-1] == "identity_signing"
        page = await session.page()
        rec.take()
        await page.goto(f"{SIGNED}/page", wait_until="load")
        got = await page.evaluate("""async () => ({
          fetch: await (await fetch('/api?k=fetch')).json(),
          post: await (await fetch('/api?k=post', {method: 'POST', body: 'hello body',
                                                   headers: {'Content-Type': 'text/plain'}})).json(),
          xhr: await new Promise(r => { const x = new XMLHttpRequest(); x.open('GET', '/api?k=xhr');
                                        x.onloadend = () => r(JSON.parse(x.responseText)); x.send(); }),
          gz: await (await fetch('/gz')).json(),
          cookie: document.cookie, sjs: document.documentElement.dataset.sjs })""")
        await page.wait_for_timeout(300)
        cookies = {c["name"]: c for c in await session.context.cookies(SIGNED)}
    rows = rec.take()
    keep("kinds", summary(rows))
    assert got == {"fetch": {"k": "fetch", "len": 0}, "post": {"k": "post", "len": 10}, "xhr": {"k": "xhr", "len": 0},
                   "gz": {"gz": True}, "cookie": "a=1", "sjs": "1"}
    assert set(cookies) == {"a", "b"} and cookies["b"]["httpOnly"] is True  # two Set-Cookie headers kept
    signed_rows = {r["path"].split("?")[0]: r for r in rows if r["port"] == SIGNED_PORT}
    for path in ("/page", "/s.css", "/i.png", "/s.js", "/api", "/gz"):
        r = signed_rows[path]
        assert r["check"].valid, (path, r["check"].reason)
        assert r["check"].covered == ['"@authority"', '"@method"', '"signature-agent"']
    post = next(r for r in rows if r["path"] == "/api?k=post")
    assert post["check"].valid and post["body"] == b"hello body" and post["method"] == "POST"
    later = [r for r in rows if r["port"] == SIGNED_PORT and r["path"] != "/page"]
    assert all("a=1" in dict(r["headers"]).get("Cookie", "") for r in later)
    others = [r for r in rows if r["port"] == OTHER_PORT]
    assert {r["path"] for r in others} >= {"/i.png?k=other-img"}
    assert all(not sig_headers(r) for r in others), "a host outside sign_hosts got a signature header"
    trace = IdentityTrace(config.lane_dir(spec.id) / "identity-trace.jsonl").read(500)
    assert {r["kind"] for r in trace} <= {"signed", "unsigned"}
    assert sum(r["kind"] == "signed" for r in trace) >= 8
    text = (config.lane_dir(spec.id) / "identity-trace.jsonl").read_text()
    for r in rows:
        for k, v in r["headers"]:
            if k.lower() == "signature":
                assert v.split("=", 1)[1].strip(":") not in text


async def test_cross_site_embeds_of_a_signed_host_go_unsigned_with_the_browser_cookies(rig, rec):
    """A page on another site embeds an image from the signed host: a third-party context, so
    the browser sends it unsigned, with its own cookie rules (the driver would send every cookie)."""
    config, displays, engine, _ = rig
    spec = LaneSpec(id="test/id-xsite")
    key = give_identity(config, spec.id, [SIGNED])
    rec.keys = {key.keyid: key.public_key()}
    async with Lane(config, displays, engine, spec) as session:
        page = await session.page()
        await page.goto(f"{SIGNED}/page", wait_until="load")  # sets a=1 (Lax), b=2
        await session.context.add_cookies([{"name": "s", "value": "3", "url": SIGNED, "sameSite": "Strict"}])
        rec.take()
        await page.goto(f"{CROSS_SITE}/embed", wait_until="load")  # embeds SIGNED/i.png?k=cross-site
        await page.evaluate(f"""() => new Promise(r => {{ const i = new Image(); i.onload = i.onerror = r;
                                                         i.src = '{SIGNED}/i.png?k=from-cross-site'; }})""")
    rows = rec.take()
    keep("xsite", summary(rows))
    for path in ("/i.png?k=cross-site", "/i.png?k=from-cross-site"):
        embedded = next(r for r in rows if r["path"] == path)
        assert embedded["port"] == SIGNED_PORT and not sig_headers(embedded)
        assert "s=3" not in dict(embedded["headers"]).get("Cookie", ""), "a Strict cookie went cross-site"
    trace = IdentityTrace(config.lane_dir(spec.id) / "identity-trace.jsonl").read(500)
    assert any(r["kind"] == "unsigned" and "third-party" in r["why"] for r in trace)


async def test_redirects_off_a_signed_host_never_carry_the_signature(rig, rec):
    config, displays, engine, events = rig
    spec = LaneSpec(id="test/id-redirect")
    key = give_identity(config, spec.id, [SIGNED])
    rec.keys = {key.keyid: key.public_key()}
    async with Lane(config, displays, engine, spec) as session:
        page = await session.page()
        await page.goto(f"{SIGNED}/target", wait_until="load")
        rec.take()
        got = await page.evaluate(f"""async () => {{
          const same = await fetch('/redir?to=' + encodeURIComponent('/api?k=fetch-same'));
          const cross = await fetch('/redir?to=' + encodeURIComponent('{OTHER}/api?k=fetch-cross'));
          return [same.url, (await same.json()).k, cross.url, (await cross.json()).k]; }}""")
        await page.goto(f"{SIGNED}/redir?to=/target%3Fk%3Dnav-same", wait_until="load")
        nav_same = page.url
        await page.goto(f"{SIGNED}/redir?to={OTHER}/target%3Fk%3Dnav-cross", wait_until="load")
        nav_cross = page.url
        await page.goto(f"{OTHER}/redir?to={SIGNED}/target%3Fk%3Dnav-into", wait_until="load")
        nav_into = page.url
        await page.wait_for_timeout(500)
        stats = dict(session.signer.stats)
    rows = rec.take()
    keep("redirects", {"rows": summary(rows), "stats": stats})
    assert got == [f"{SIGNED}/api?k=fetch-same", "fetch-same", f"{OTHER}/api?k=fetch-cross", "fetch-cross"]
    assert (nav_same, nav_cross, nav_into) == (f"{SIGNED}/target?k=nav-same", f"{OTHER}/target?k=nav-cross",
                                               f"{SIGNED}/target?k=nav-into")
    first_hops = [r for r in rows if r["port"] == SIGNED_PORT and r["path"].startswith("/redir")]
    assert len(first_hops) == 4 and all(r["check"].valid for r in first_hops)
    def hop_for(k: str) -> dict:
        return next(r for r in rows if r["path"] in (f"/api?k={k}", f"/target?k={k}"))

    for k in ("fetch-cross", "nav-cross"):
        hop = hop_for(k)
        assert hop["port"] == OTHER_PORT and hop["host"] == f"127.0.0.1:{OTHER_PORT}", hop
        assert not sig_headers(hop), f"{k}: the next host got the signature"
    for k in ("fetch-same", "nav-same", "nav-into"):
        hop = hop_for(k)
        assert hop["host"] == f"127.0.0.1:{SIGNED_PORT}"
        assert not hop["check"].signed or hop["check"].valid, f"{k}: a stale signature"
    trace = IdentityTrace(config.lane_dir(spec.id) / "identity-trace.jsonl").read(500)
    hops = [(r["to_origin"], r["left_host"], r["carried_signature"]) for r in trace if r["kind"] == "redirect"]
    assert (OTHER, True, False) in hops and (SIGNED, False, False) in hops
    assert not any(c for _, _, c in hops)
    assert stats["redirects_off_host"] == 2 and not session.signer.stopped
    assert (spec.id, "identity_redirect_off_host", {"from": SIGNED, "to": OTHER, "carried_signature": False}) \
        in events


async def test_header_overrides_carry_the_signature_across_redirects_today(rig, rec):
    """Pins the Playwright and Camoufox behaviour that rules out route.continue_(headers=): the
    override is reused on the redirect hop, Host and the live signature included. If this starts
    failing, the browser was fixed, and signing through the browser itself can be reconsidered.
    The product's detector, on the same lane, must notice the leak and stop."""
    config, displays, engine, events = rig
    spec = LaneSpec(id="test/id-override")
    key = give_identity(config, spec.id, [])  # no product route: this test installs its own
    rec.keys = {key.keyid: key.public_key()}
    ident = settings.load(config.secrets_dir, spec.id)
    settings.add_host(ident, SIGNED, reason="detector", by="owner:test")
    detector = LaneSigner(key, ident, proxied=False, allow_private=True,
                          events=lambda *a: events.append(a))
    async with Lane(config, displays, engine, spec) as session:
        assert session.signer is None
        ctx = session.context
        assert not ctx._impl_obj._routes, "a lane with no hosts must have no route"

        async def override(route, request):
            signed = key.sign_request(method=request.method, url=request.url, signature_agent=AGENT)
            headers = {h["name"]: h["value"] for h in await request.headers_array()}
            await route.continue_(headers={**headers, **signed.headers})

        await ctx.route(f"{SIGNED}/**", override)
        ctx.on("request", detector._on_request)
        detector._context = ctx
        page = await session.page()
        await page.goto(f"{SIGNED}/target", wait_until="load")
        rec.take()
        await page.evaluate(f"""() => fetch('/redir?to=' + encodeURIComponent('{OTHER}/api?k=leak'))""")
        await page.wait_for_timeout(800)
        await ctx.unroute_all()
    rows = rec.take()
    keep("override", summary(rows))
    hop = next(r for r in rows if "k=leak" in r["path"])
    assert hop["port"] == OTHER_PORT
    assert sig_headers(hop), "the override no longer carries over: Playwright changed, revisit the route design"
    assert hop["host"] == f"127.0.0.1:{SIGNED_PORT}"  # the stale Host the spike found
    assert detector.stopped, "the detector missed a signature carried to another host"
    assert any(e[1] == "identity_redirect_leak" for e in events)


async def test_pinned_fingerprint_is_the_same_with_the_route_on_and_off(rig, rec):
    config, displays, engine, _ = rig
    spec = LaneSpec(id="test/id-fp")
    key = give_identity(config, spec.id, [])
    rec.keys = {key.keyid: key.public_key()}
    from test_fingerprint_pin_node import comparable, differences

    dumps, timings = {}, {}
    for state in ("off", "on", "off-again"):
        ident = settings.load(config.secrets_dir, spec.id)
        ident.hosts = []
        if state == "on":
            settings.add_host(ident, SIGNED, reason="node test", by="owner:test")
        settings.save(config.secrets_dir, ident)
        async with Lane(config, displays, engine, spec) as session:
            assert (session.signer is not None) == (state == "on")
            page = await session.page()
            rec.take()
            await page.goto(f"{SIGNED}/fingerprint.html")
            await page.wait_for_selector("#dump[data-state=done]", state="attached", timeout=30_000)
            dumps[state] = json.loads(await page.text_content("#dump"))
            times = {}
            for target in (SIGNED, OTHER):
                ms = []
                for _ in range(8):
                    t0 = time.perf_counter()
                    await page.goto(f"{target}/target", wait_until="load")
                    ms.append((time.perf_counter() - t0) * 1000)
                times[target] = round(statistics.median(ms), 1)
            timings[state] = times
        rows = rec.take()
        fp_row = next(r for r in rows if r["path"] == "/fingerprint.html")
        assert fp_row["check"].valid if state == "on" else not fp_row["check"].signed
    keep("fingerprint", {"dumps": dumps, "timings_ms": timings})
    noise = json.loads(config.fingerprint_path(spec.id).read_text())[PIN_META]["canvas_noise"]
    heads = {s: {k: v for k, v in d.pop("headers").items()
                 if k not in base.SIGNATURE_HEADERS and k not in ("accept-encoding", "cookie")}
             for s, d in dumps.items()}
    for state in ("on", "off-again"):
        diff = differences(comparable(dumps["off"], noise), comparable(dumps[state], noise))
        assert diff == [], f"route {state}: the page sees another device:\n" + "\n".join(diff)
    changed = {k: (heads["off"].get(k), heads["on"].get(k)) for k in heads["off"].keys() | heads["on"].keys()
               if heads["off"].get(k) != heads["on"].get(k)}
    assert heads["off"] == heads["off-again"]
    # Known cost on signed hosts (the driver sends the request): Firefox adds Priority, and
    # Pragma and Cache-Control for a no-store fetch, after the route sees the request, so they are
    # missing there. Everything a page or a site reads as the device (User-Agent, Accept,
    # Accept-Language, Sec-Fetch-*, Referer) is unchanged.
    assert set(changed) <= NETWORK_LAYER_HEADERS and all(on is None for _, on in changed.values()), changed
    print("navigation medians (ms):", json.dumps(timings))


@pytest.mark.skipif(bool(os.environ.get("AGENTOS_TEST_NO_INTERNET")), reason="AGENTOS_TEST_NO_INTERNET is set")
async def test_cloudflare_research_verifier_accepts_the_route_signatures(rig):
    """One interop check: the product route, signing with Cloudflare's published test key, against
    Cloudflare's research deployment (its /v0/api/verify answers valid, invalid: <reason> or neutral)."""
    config, displays, engine, _ = rig
    spec = LaneSpec(id="test/id-interop")
    key = give_identity(config, spec.id, [RESEARCH], agent=RESEARCH, key_d=TEST_KEY_D)
    assert key.keyid == "poqkLGiymh_W0uP6PZFw-dvez3QJT5SolqXBCW38r0U"
    async with Lane(config, displays, engine, spec) as session:
        page = await session.page()
        response = await page.goto(f"{RESEARCH}/v0/api/verify?k=document")
        document = (await response.text()).strip()
        fetched = await page.evaluate("async () => (await (await fetch('/v0/api/verify?k=fetch')).text()).trim()")
    trace = IdentityTrace(config.lane_dir(spec.id) / "identity-trace.jsonl").read(50)
    keep("interop", {"document": document, "fetch": fetched, "trace": trace})
    assert (document, fetched) == ("valid", "valid")
