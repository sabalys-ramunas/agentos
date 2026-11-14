"""Web pages in a lane must not reach the node's own services (live viewers, the MCP port).

Firefox normally sends localhost straight past any proxy. The engine therefore routes
localhost through the lane's proxy (which cannot reach the node's loopback in production)
and, for lanes without a proxy, blackholes loopback and private ranges with a PAC.
These tests drive a real Camoufox on an Xvfb display and use the page's own fetch().
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentos.core.models import LaneSpec, ProxyConfig
from agentos.node.camoufox.engine import CamoufoxEngine

pytestmark = pytest.mark.node

DISPLAY = os.environ.get("AGENTOS_TEST_GUARD_DISPLAY", ":160")
SECRET_PORT = int(os.environ.get("AGENTOS_TEST_GUARD_SECRET_PORT", "7461"))
PROXY_PORT = int(os.environ.get("AGENTOS_TEST_GUARD_PROXY_PORT", "7462"))

FETCH = """async (url) => {
  try { await fetch(url, {mode: 'no-cors', cache: 'no-store'}); return 'reached'; }
  catch (e) { return 'blocked'; }
}"""


@pytest.fixture(scope="module")
def display():
    if shutil.which("Xvfb") is None:
        pytest.skip("Xvfb not installed")
    num = DISPLAY.lstrip(":")
    proc = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + 10
    while not os.path.exists(f"/tmp/.X11-unix/X{num}"):
        if time.time() > deadline:
            proc.kill()
            pytest.fail("Xvfb did not start")
        time.sleep(0.1)
    yield DISPLAY
    proc.terminate()
    proc.wait(timeout=5)


class _Counter:
    def __init__(self):
        self.hits = 0
        self.targets: list[str] = []


@pytest.fixture
def secret_server():
    counter = _Counter()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            counter.hits += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"node secret")

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", SECRET_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield counter
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def recording_proxy():
    """A proxy that records what the browser asked it for and refuses everything."""
    seen = _Counter()
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", PROXY_PORT))
    sock.listen(16)
    stop = threading.Event()

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                continue
            with conn:
                try:
                    first = conn.recv(4096).split(b"\r\n", 1)[0].decode("latin-1")
                    seen.targets.append(first)
                    conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield seen
    stop.set()
    t.join(timeout=2)
    sock.close()


async def _launch(tmp_path, display, proxy):
    engine = CamoufoxEngine(extra_options={"geoip": False})
    spec = LaneSpec(id="qa/guard")
    profile = tmp_path / "profile"
    profile.mkdir()
    session = await engine.launch(spec, profile_dir=profile, fingerprint_path=tmp_path / "fp.json",
                                  display=display, proxy=proxy)
    return session


async def test_lane_without_proxy_cannot_reach_node_loopback(tmp_path, display, secret_server):
    session = await _launch(tmp_path, display, proxy=None)
    try:
        page = await session.page()
        await page.goto("about:blank")
        assert await page.evaluate(FETCH, f"http://127.0.0.1:{SECRET_PORT}/") == "blocked"
        assert await page.evaluate(FETCH, f"http://localhost:{SECRET_PORT}/") == "blocked"
        assert secret_server.hits == 0
        # not over-blocking: the public internet still works
        assert await page.evaluate(FETCH, "https://example.com/") == "reached"
    finally:
        await session.close()


async def test_proxied_lane_sends_localhost_through_the_proxy(tmp_path, display, secret_server, recording_proxy):
    proxy = ProxyConfig(label="t", host="127.0.0.1", port=PROXY_PORT)
    session = await _launch(tmp_path, display, proxy=proxy)
    try:
        page = await session.page()
        await page.goto("about:blank")
        await page.evaluate(FETCH, f"http://127.0.0.1:{SECRET_PORT}/")
        await asyncio.sleep(0.5)
        # the request went to the proxy (which, in production, cannot reach the node), never straight to the node
        assert secret_server.hits == 0
        assert any(f"127.0.0.1:{SECRET_PORT}" in line for line in recording_proxy.targets), recording_proxy.targets
    finally:
        await session.close()
