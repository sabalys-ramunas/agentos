"""Signing in through the live view with the egress guard on, on a real Camoufox lane
(docs/design/egress-guard.md): the suggested identity and captcha hosts, a blocked frame's
notice on the viewer's event stream, and a site-list change that reaches the warm lane at once.

Run on the node:  OPENAJAR_TEST_SLOT=41 scripts/node-test.sh egressux tests/node/test_egressux_node.py -q

Nothing leaves the node. The lane's proxy is a loopback stand-in that answers every host itself
(www.reddit.com, www.google.com, accounts.google.lt and the rest are its own pages, over HTTP and
over CONNECT with a throwaway self-signed certificate the lane is told to accept), and every
proxy variable of this process points at a closed port, as in the QA egress node tests, so no
library download leaves either. The lane is a throwaway under .node-home/ of this checkout.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import ssl
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount

from openajar.core.config import NodeConfig
from openajar.core.errors import EgressRefused
from openajar.core.models import LaneSpec, ProxyConfig
from openajar.node import egress
from openajar.node.camoufox.engine import CamoufoxEngine, browser_pids_under
from openajar.node.display import DisplayManagerImpl, kill_processes
from openajar.node.egress import IDENTITY_SITES, SUGGESTED_SITES, Driver
from openajar.node.lanes import LaneManagerImpl
from openajar.node.store import LaneStore
from openajar.node.viewer_app import ViewerApp
from openajar.node.viewer_routes import control_api_routes

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb, x11vnc and Camoufox"),
]

ROOT = Path(os.environ.get("OPENAJAR_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
DISPLAY_BASE = int(os.environ.get("OPENAJAR_TEST_DISPLAY_BASE", "410"))
VNC_BASE = int(os.environ.get("OPENAJAR_TEST_VNC_BASE", "5410"))
VIEWER_BASE = int(os.environ.get("OPENAJAR_TEST_VIEWER_BASE", "6410"))
PROXY_PORT = VIEWER_BASE + 7
API_PORT = VIEWER_BASE + 8
OFFLINE_PROXY = "http://127.0.0.1:9"  # nothing listens here
LANE = "test/reddit"
OPEN_AGENT = Driver(kind="agent", id="node-test", grant_id="g-open")

# The frames a sign-in page can hold, one per provider in the identity group, and one that is not.
FRAMES = {
    "recaptcha": "https://www.google.com/recaptcha/api2/anchor?k=SITEKEY&co=SECRET",
    "gis": "https://accounts.google.com/gsi/button?client_id=CLIENT",
    "youtube": "https://accounts.youtube.com/accounts/CheckConnection?pmpo=SECRET",
    "hcaptcha": "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html",
    "turnstile": "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2",
    "arkose": "https://client-api.arkoselabs.com/fc/gc/?token=SECRET",
    "apple": "https://idmsa.apple.com/appleauth/auth/authorize/signin?frame_id=auth",
    "tracker": "https://tracker.example/pixel-frame",
}
LOGIN = ("<!doctype html><title>login</title><body>"
         + "".join(f'<iframe name="{k}" src="{u}" width="120" height="40"></iframe>' for k, u in FRAMES.items())
         + "</body>")
# Google's sign-in after the phone check: SetSID on google.com, then on the exit IP's country domain
REDIRECTS = {
    ("www.reddit.com", "/google-login"): "https://accounts.google.com/accounts/SetSID?ssdc=1&sidt=SECRET",
    ("accounts.google.com", "/accounts/SetSID"): "https://accounts.google.lt/accounts/SetSID?ssdc=1&sidt=SECRET",
    ("accounts.google.lt", "/accounts/SetSID"): "https://www.reddit.com/done",
}


# ---- the stand-in proxy ----------------------------------------------------------------------

class Seen:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: list[tuple[str, str]] = []

    def add(self, host: str, path: str) -> None:
        with self.lock:
            self.rows.append((host, path))

    def hosts(self) -> set[str]:
        with self.lock:
            return {h for h, _ in self.rows}


def answer(handler: BaseHTTPRequestHandler, host: str, path: str, seen: Seen) -> None:
    seen.add(host, path)
    status, extra = 200, []
    if (host, path) in REDIRECTS:
        status, extra, body = 302, [("Location", REDIRECTS[(host, path)])], b""
    elif host == "www.reddit.com" and path == "/login":
        body = LOGIN.encode()
    else:
        body = f"<!doctype html><title>{host}</title><body id='loaded'>{host}{path}</body>".encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    for k, v in extra:
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def make_proxy(seen: Seen, tls: ssl.SSLContext) -> ThreadingHTTPServer:
    class Inner(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        host = ""

        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            answer(self, self.host, urlsplit(self.path).path, seen)

        do_POST = do_GET

    class Proxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:  # an http:// URL, in absolute form
            u = urlsplit(self.path)
            answer(self, u.hostname or "", u.path or "/", seen)

        do_POST = do_GET

        def do_CONNECT(self) -> None:
            host = self.path.rsplit(":", 1)[0]
            self.send_response(200, "Connection established")
            self.end_headers()
            try:
                conn = tls.wrap_socket(self.connection, server_side=True)
            except (ssl.SSLError, OSError):
                return
            handler = type("Inner", (Inner,), {"host": host})
            with contextlib.suppress(OSError, ssl.SSLError):
                handler(conn, self.client_address, self.server)
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), Proxy)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def home():
    d = ROOT / f"egressux-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def proxy(home):
    key, cert = home / "proxy.key", home / "proxy.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out",
                    str(cert), "-days", "1", "-subj", "/CN=openajar-node-test"], check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    seen = Seen()
    server = make_proxy(seen, tls)
    try:
        yield seen
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
async def lanes(home, proxy, monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, OFFLINE_PROXY)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    config = NodeConfig(home=home, display_base=DISPLAY_BASE, vnc_base=VNC_BASE, viewer_base=VIEWER_BASE,
                        max_slots=4, max_warm_lanes=1, self_check=False,
                        control_idle_seconds=600)
    # geoip off: Camoufox would look up the exit IP through the proxy; the stand-in has none to give
    engine = CamoufoxEngine(extra_options={"geoip": False, "ignore_https_errors": True})
    engine.allow_private_network = True  # the stand-in proxy is on loopback
    stand_in = ProxyConfig(label="stand-in", host="127.0.0.1", port=PROXY_PORT)
    mgr = LaneManagerImpl(config, LaneStore(config.db_path), engine, DisplayManagerImpl(config),
                          lambda: {"stand-in": stand_in})
    await mgr.start()
    try:
        yield mgr
    finally:
        await mgr.shutdown()
        leftover = browser_pids_under(home)
        await kill_processes(leftover)
        assert not leftover, f"browsers left running after the test: {leftover}"


@contextlib.asynccontextmanager
async def serve(app):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=API_PORT, log_level="warning",
                                           lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{API_PORT}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5.0)


class Notices:
    """The viewer's event stream, read in the background: every `egress` event."""

    def __init__(self, client: httpx.AsyncClient, url: str):
        self.client, self.url = client, url
        self.events: list[dict] = []
        self.hello = asyncio.Event()
        self.task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        import json

        async with self.client.stream("GET", self.url) as r:
            name = None
            async for line in r.aiter_lines():
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: ") and name:
                    if name == "hello":
                        self.hello.set()
                    elif name == "egress":
                        self.events.append(json.loads(line[6:]))

    async def wait(self, host: str, timeout: float = 10.0) -> dict:
        for _ in range(int(timeout / 0.1)):
            found = [e for e in self.events if e["host"] == host]
            if found:
                return found[-1]
            await asyncio.sleep(0.1)
        raise AssertionError(f"no notice for {host}: {self.events}")

    async def close(self) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.task


async def frames_loaded(page) -> dict[str, bool]:
    await page.wait_for_timeout(1500)
    out = {}
    for name in FRAMES:
        frame = page.frame(name=name)
        try:
            out[name] = frame is not None and await frame.evaluate("!!document.getElementById('loaded')")
        except Exception:  # noqa: BLE001 - a stopped frame
            out[name] = False
    return out


async def test_signing_in_with_the_suggested_list_a_notice_and_a_live_change(lanes, proxy, monkeypatch):
    monkeypatch.setattr(egress.constants, "NOTICE_WINDOW_S", 1.0)  # each step below gets its own notice budget
    await lanes.create(LaneSpec(id=LANE, proxy_label="stand-in", sites=["reddit.com", "*.reddit.com"]))
    await lanes.open(LANE)
    session = lanes._lanes[LANE].session
    guard = session.guard
    assert guard is not None and guard.on_notice is not None  # bound by the manager
    await lanes.take_control(LANE, by="owner")

    viewer = ViewerApp(lanes, lanes.config)
    app = Starlette(routes=[*viewer.routes(), Mount("/api", routes=control_api_routes(lanes, viewer.tokens))])
    owner = viewer.tokens.issue(LANE, "control", owner=True).token
    async with serve(app) as base, httpx.AsyncClient(trust_env=False, timeout=15) as client:
        notices = Notices(client, f"{base}/view/test/reddit/events?t={owner}")
        await asyncio.wait_for(notices.hello.wait(), 10)
        page = await session.page()

        # 1. only the platform's own hosts: every provider frame is blocked, and the live view says why
        await page.goto("https://www.reddit.com/login", wait_until="load")
        loaded = await frames_loaded(page)
        assert not any(loaded.values()), loaded
        recaptcha = await notices.wait("www.google.com")
        assert recaptcha["kind"] == "subframe" and recaptcha["rule"] == "lane_sites"
        assert recaptcha["path"] == "/recaptcha/api2/anchor" and recaptcha["allow"] == "frame:www.google.com"
        assert recaptcha["command"] == f"openajar lane sites {LANE} add frame:www.google.com"
        assert "SECRET" not in repr(notices.events) and "SITEKEY" not in repr(notices.events)

        # 2. the suggested list, through the daemon's route: the warm lane has it at once
        r = await client.post(f"{base}/api/lanes/test/reddit/sites",
                              json={"op": "add", "sites": list(SUGGESTED_SITES["reddit"])})
        assert r.status_code == 200 and r.json()["running"] is True, r.text
        assert guard.sites[-len(IDENTITY_SITES):] == IDENTITY_SITES
        await asyncio.sleep(1.0)
        await page.reload(wait_until="load")
        loaded = await frames_loaded(page)
        assert loaded == {**{k: True for k in FRAMES}, "tracker": False}, loaded
        assert (await notices.wait("tracker.example"))["allow"] == "frame:tracker.example"

        # 3. the redirect through Google's country domain of the exit IP (accounts.google.lt)
        await page.goto("https://www.reddit.com/google-login", wait_until="load")
        assert page.url == "https://www.reddit.com/done"
        assert {"accounts.google.com", "accounts.google.lt"} <= proxy.hosts()

        # 4. a frame-only host is never a page: refused for an agent before anything is sent
        await lanes.hand_back(LANE, by="owner")
        guard.drive(OPEN_AGENT)
        with pytest.raises(EgressRefused, match="only as a frame"):
            guard.check_url("https://www.google.com/search?q=x")
        await lanes.take_control(LANE, by="owner")
        guard.drive(None)

        # 5. the owner allows the refused frame from the notice, in one click (an owner link only)
        r = await client.post(f"{base}/view/test/reddit/sites?t={viewer.tokens.issue(LANE, 'control').token}",
                              json={"add": "frame:tracker.example"})
        assert r.status_code == 403
        r = await client.post(f"{base}/view/test/reddit/sites?t={owner}", json={"add": "frame:tracker.example"})
        assert r.status_code == 200 and "frame:tracker.example" in r.json()["sites"], r.text
        await page.goto("https://www.reddit.com/login", wait_until="load")
        assert all((await frames_loaded(page)).values())
        await notices.close()
