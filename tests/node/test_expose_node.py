"""`openajar expose` on a real Camoufox lane (openajar.node.expose, docs/DEVELOPING.md "Your dev server in
a lane").

Run on the node:  OPENAJAR_TEST_SLOT=43 scripts/node-test.sh expose tests/node/test_expose_node.py -q

Throwaway lanes under .node-home/ of this checkout, launched through `CamoufoxEngine`. The dev server
is an HTTP server on a unix socket, standing in for the socket sshd binds for `ssh -R`; a loopback
server on the slot's viewer port stands in for an OAuth provider. No real site, and no SSH: the SSH
half is the client's (tests/unit/test_expose_api.py).
"""

from __future__ import annotations

import os
import shutil
import socketserver
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Error as PlaywrightError

from openajar.core.config import NodeConfig
from openajar.core.models import LaneSpec
from openajar.node.camoufox.engine import CamoufoxEngine, browser_pids_under
from openajar.node.display import DisplayManagerImpl, kill_processes
from openajar.node.egress import Driver
from openajar.node.expose import names
from openajar.node.expose.service import ExposeService

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb, x11vnc and Camoufox"),
]

ROOT = Path(os.environ.get("OPENAJAR_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
DISPLAY_BASE = int(os.environ.get("OPENAJAR_TEST_DISPLAY_BASE", "430"))
VNC_BASE = int(os.environ.get("OPENAJAR_TEST_VNC_BASE", "5430"))
VIEWER_BASE = int(os.environ.get("OPENAJAR_TEST_VIEWER_BASE", "6430"))
PORT = VIEWER_BASE + 7
PROVIDER = f"http://127.0.0.1:{PORT}"
LANE, OTHER = "test/expose-a", "test/expose-b"
HOST = "myapp.expose.openajar.invalid"
URL = f"https://{HOST}/"
AGENT = Driver(kind="agent", id="node-test", grant_id="g", lists=(("127.0.0.1",),), ceiling="draft")


class Log:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []

    def add(self, row: dict[str, Any]) -> None:
        with self.lock:
            self.rows.append(row)

    def paths(self) -> list[str]:
        with self.lock:
            return [r["path"] for r in self.rows]


def dev_handler(log: Log) -> type[BaseHTTPRequestHandler]:
    class Dev(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def address_string(self) -> str:
            return "unix"

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            log.add({"path": self.path, "method": self.command, "host": self.headers.get("Host"),
                     "cookie": self.headers.get("Cookie"), "proto": self.headers.get("X-Forwarded-Proto"),
                     "body": body})
            extra: list[tuple[str, str]] = []
            if self.path == "/":
                payload = (b"<!doctype html><title>my app</title><body>hello from the dev server"
                           b"<a id='login' href='" + PROVIDER.encode() + b"/authorize'>Sign in</a></body>")
                extra = [("Set-Cookie", "seen=1; Path=/; Secure; SameSite=Lax"),
                         ("Set-Cookie", "sid=s3cr3t; Path=/; Secure; HttpOnly; SameSite=Lax")]
            elif self.path.startswith("/callback"):
                payload = b"<!doctype html><title>signed in</title><body>signed in</body>"
            else:
                payload = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if payload.startswith(b"<!") else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _handle

    return Dev


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def provider_server(log: Log) -> ThreadingHTTPServer:
    class Provider(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            u = urlsplit(self.path)
            log.add({"path": u.path})
            if u.path == "/authorize":  # the provider sends the browser back to the app, as OAuth does
                self.send_response(302)
                self.send_header("Location", f"{URL}callback?code=one-time-code&state=xyz")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = b"<!doctype html><title>provider</title><body>provider</body>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Provider)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Lanes:
    """The lane manager surface the expose service uses: the live session and lane events."""

    def __init__(self) -> None:
        self.sessions: dict[str, Any] = {}

    def get(self, lane_id: str):
        return SimpleNamespace(spec=SimpleNamespace(id=lane_id, kind="browser"))

    def live_session(self, lane_id: str):
        return self.sessions.get(lane_id)

    def _event(self, *args: Any) -> None:
        pass


@pytest.fixture
async def rig(monkeypatch):
    monkeypatch.setattr(names, "LEASE_S", 3600.0)  # no laptop renews it here
    home = ROOT / f"expose-{uuid.uuid4().hex[:8]}"
    home.mkdir(parents=True)
    config = NodeConfig(home=home, display_base=DISPLAY_BASE, vnc_base=VNC_BASE, viewer_base=VIEWER_BASE,
                        max_slots=4, self_check=False)
    displays = DisplayManagerImpl(config)
    await displays.sweep()
    lanes = Lanes()
    service = ExposeService(SimpleNamespace(get=lambda name: lanes if name == "lanes" else None), home=home)
    yield config, displays, lanes, service
    await service.shutdown()
    await displays.release_all()
    leftover = browser_pids_under(home)
    await kill_processes(leftover)
    shutil.rmtree(home, ignore_errors=True)
    assert not leftover, f"browsers left running after the test: {leftover}"


async def launch(config, displays, lane_id: str, *, private: bool):
    engine = CamoufoxEngine()
    engine.allow_private_network = private  # only so the lane can reach the stand-in provider on loopback
    spec = LaneSpec(id=lane_id, sites=["127.0.0.1"])
    fp = config.fingerprint_path(lane_id)
    viewer = await displays.acquire(lane_id, size=engine.display_size(fp))
    return await engine.launch(spec, profile_dir=config.profile_dir(lane_id), fingerprint_path=fp,
                               display=viewer.display, xauthority=viewer.xauthority, proxy=None)


async def start_expose(service: ExposeService, log: Log) -> tuple[dict, UnixHTTPServer]:
    link = await service.create_expose(LANE, name="myapp", port=5173, by="node-test")
    dev = UnixHTTPServer(link["socket"], dev_handler(log))  # what sshd binds for `ssh -R`
    threading.Thread(target=dev.serve_forever, daemon=True).start()
    await service.ready(link["id"])
    return link, dev


async def test_the_named_lane_opens_the_dev_server_and_an_oauth_redirect_comes_back(rig):
    config, displays, lanes, service = rig
    dev_log, provider_log = Log(), Log()
    provider = provider_server(provider_log)
    session = await launch(config, displays, LANE, private=True)
    lanes.sessions[LANE] = session
    await service.on_session(LANE, session)
    link, dev = await start_expose(service, dev_log)
    try:
        assert session.routes.patterns() == [names.URL_RE]
        guard = session.guard
        guard.drive(AGENT)  # an agent whose lists name only 127.0.0.1: the exposed host is in them now
        guard.check_url(URL)
        page = await session.page()
        response = await page.goto(URL, wait_until="load")
        assert response is not None and response.status == 200 and await page.title() == "my app"
        first = dev_log.rows[0]
        assert first["host"] == HOST and first["proto"] == "https" and first["path"] == "/"
        # the app's cookies: both set, the HttpOnly one hidden from the page, both sent back
        assert "seen=1" in await page.evaluate("document.cookie")
        assert "sid" not in await page.evaluate("document.cookie")
        names_set = {c["name"] for c in await session.context.cookies([URL])}
        assert {"seen", "sid"} <= names_set
        got = await page.evaluate("fetch('/api/me', {method: 'POST', body: 'x=1'}).then(r => r.json())")
        assert got == {"ok": True}
        api = dev_log.rows[-1]
        assert api["path"] == "/api/me" and api["body"] == b"x=1" and "sid=s3cr3t" in (api["cookie"] or "")
        # Sign in: the provider answers with a 302 back to the exposed host, a hop Juggler never routes
        await page.click("#login")
        await page.wait_for_url(f"{URL}callback?code=one-time-code&state=xyz", timeout=15_000)
        assert await page.title() == "signed in"
        callbacks = [p for p in dev_log.paths() if p.startswith("/callback")]
        assert callbacks == ["/callback?code=one-time-code&state=xyz"]  # the one-time code arrived once
        # a page on another site cannot fetch from it
        await page.goto(f"{PROVIDER}/page", wait_until="load")
        before = len(dev_log.rows)
        blocked = await page.evaluate(
            f"fetch('{URL}api/secret').then(() => 'reached', e => 'failed')")
        assert blocked == "failed" and len(dev_log.rows) == before
        # the owner ends it: the name stops resolving at once, and the route goes
        await service.end(link["id"])
        assert session.routes.patterns() == []
        with pytest.raises(PlaywrightError):  # as any name that does not exist (Firefox words it either way)
            await page.goto(URL, wait_until="load", timeout=10_000)
        assert len(dev_log.rows) == before
    finally:
        await session.close()
        await displays.release(LANE)
        dev.shutdown()
        dev.server_close()
        provider.shutdown()
        provider.server_close()


async def test_another_lane_and_the_nodes_loopback_stay_out_of_reach(rig):
    config, displays, lanes, service = rig
    dev_log, provider_log = Log(), Log()
    provider = provider_server(provider_log)
    link, dev = await start_expose(service, dev_log)
    try:
        # another lane: no route, so the reserved name does not resolve there
        other = await launch(config, displays, OTHER, private=False)
        lanes.sessions[OTHER] = other
        await service.on_session(OTHER, other)
        try:
            assert other.routes.patterns() == []
            page = await other.page()
            with pytest.raises(PlaywrightError):
                await page.goto(URL, wait_until="load", timeout=10_000)
            assert dev_log.rows == []
        finally:
            await other.close()
            await displays.release(OTHER)
        # the named lane, with the node's private network guard on: the exposed host answers, the
        # node's loopback (where the stand-in provider listens) still does not
        session = await launch(config, displays, LANE, private=False)
        lanes.sessions[LANE] = session
        await service.on_session(LANE, session)
        try:
            page = await session.page()
            await page.goto(URL, wait_until="load")
            assert await page.title() == "my app"
            reached = await page.evaluate(
                f"fetch('{PROVIDER}/page', {{mode: 'no-cors'}}).then(() => 'reached', e => 'failed')")
            assert reached == "failed" and provider_log.rows == []
            other_port = await page.evaluate(
                f"fetch('https://{HOST}:{PORT}/x').then(() => 'reached', e => 'failed')")
            assert other_port == "failed" and "/x" not in dev_log.paths()
        finally:
            await session.close()
            await displays.release(LANE)
    finally:
        await service.end(link["id"])
        dev.shutdown()
        dev.server_close()
        provider.shutdown()
        provider.server_close()
