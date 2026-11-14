"""Stealth and isolation on a real Camoufox (docs/design/stealth.md).

Run on the node:  AGENTOS_TEST_SLOT=33 scripts/node-test.sh stealth tests/node/test_stealth_node.py -q
or in the Docker node (docs/design/stealth.md, "Running the check"). Throwaway lanes under
.node-home/ of this checkout, on the slot's own displays and ports; at most two browsers at once.
No page reaches the internet: the lanes load a local fixture server and a local proxy.

- A lane whose proxy dies fails closed: Firefox never retries the request without the proxy
  (network.proxy.failover_direct), so nothing leaves from the node's own address.
- A lane without a proxy stays walled off from the node's loopback however often a page tries.
- A page's own script finds no automation trace, no worker or frame that disagrees with the
  window, and no raw WebRTC address (agentos.stealth.stealth on stealth_probe.html).
- The stealth check itself: two lanes, a relaunch and a short soak with a kill.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agentos.core.config import NodeConfig
from agentos.core.models import LaneSpec, ProxyConfig
from agentos.node.camoufox.engine import CamoufoxEngine, browser_pids_under
from agentos.node.display import DisplayManagerImpl, kill_processes
from agentos.stealth import stealth, stealthcheck

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb, x11vnc and Camoufox"),
]

ROOT = Path(os.environ.get("AGENTOS_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
DISPLAY_BASE = int(os.environ.get("AGENTOS_TEST_DISPLAY_BASE", "330"))
VNC_BASE = int(os.environ.get("AGENTOS_TEST_VNC_BASE", "5330"))
VIEWER_BASE = int(os.environ.get("AGENTOS_TEST_VIEWER_BASE", "6330"))
SITE_PORT = VIEWER_BASE + 7
PROXY_PORT = VIEWER_BASE + 8
VIA = "x-agentos-test-proxy"

FETCH = """async (url) => {
  try { const r = await fetch(url, {cache: 'no-store'}); return 'reached ' + r.status; }
  catch (e) { return 'blocked'; }
}"""


class _Seen:
    def __init__(self) -> None:
        self.direct = 0
        self.proxied = 0


@pytest.fixture
def site():
    seen = _Seen()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.headers.get(VIA):
                seen.proxied += 1
            else:
                seen.direct += 1
            body = b"fixture"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", SITE_PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield seen
    server.shutdown()
    server.server_close()


class ForwardingProxy:
    """A plain HTTP proxy that forwards absolute-URI GETs to 127.0.0.1 and marks them with a header,
    so the fixture server can tell a proxied request from one that went around the proxy."""

    def __init__(self, port: int):
        self.port = port
        self.sock: socket.socket | None = None
        self.stopped = threading.Event()
        self.reset = False  # accept, then drop every connection before answering

    def start(self) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.port))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        threading.Thread(target=self._serve, daemon=True).start()

    def stop(self) -> None:
        """The proxy dies: its port refuses connections from now on."""
        self.stopped.set()
        if self.sock is not None:
            self.sock.close()

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        with conn:
            if self.reset:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                return
            try:
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    head += chunk
                line, rest = head.split(b"\r\n", 1)
                method, url, version = line.decode("latin-1").split(" ")
                hostport, _, path = url.removeprefix("http://").partition("/")
                port = int(hostport.rsplit(":", 1)[1]) if ":" in hostport else 80
                headers = [h for h in rest.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
                           if not h.lower().startswith((b"proxy-", b"connection"))]
                upstream = socket.create_connection(("127.0.0.1", port), timeout=5)
                with upstream:
                    upstream.sendall(f"{method} /{path} {version}\r\n".encode() + b"\r\n".join(headers)
                                     + f"\r\n{VIA}: 1\r\nConnection: close\r\n\r\n".encode())
                    while data := upstream.recv(65536):
                        conn.sendall(data)
            except (OSError, ValueError):
                pass


@pytest.fixture
async def rig():
    home = ROOT / f"stealth-{uuid.uuid4().hex[:8]}"
    home.mkdir(parents=True)
    config = NodeConfig(home=home, display_base=DISPLAY_BASE, vnc_base=VNC_BASE, viewer_base=VIEWER_BASE,
                        max_slots=7, self_check=False)
    displays = DisplayManagerImpl(config)
    await displays.sweep()
    yield config, displays
    await displays.release_all()
    leftover = browser_pids_under(home)
    await kill_processes(leftover)
    shutil.rmtree(home, ignore_errors=True)
    assert not leftover, f"browsers left running after the test: {leftover}"


async def _launch(config, displays, engine, lane, proxy):
    spec = LaneSpec(id=lane)
    viewer = await displays.acquire(spec.id)
    return await engine.launch(spec, profile_dir=config.profile_dir(spec.id),
                               fingerprint_path=config.fingerprint_path(spec.id), display=viewer.display,
                               xauthority=viewer.xauthority, proxy=proxy)


@pytest.mark.parametrize("failure", ["refused", "reset", "unknown_host"])
async def test_a_dead_proxy_never_falls_back_to_a_direct_connection(rig, site, failure):
    config, displays = rig
    proxy = ForwardingProxy(PROXY_PORT)
    proxy.start()
    host = "agentos-dead-proxy.invalid" if failure == "unknown_host" else "127.0.0.1"
    engine = CamoufoxEngine(extra_options={"geoip": False})  # the guard as in production: localhost via the proxy
    session = await _launch(config, displays, engine, "qa/proxy", ProxyConfig(label="t", host=host, port=PROXY_PORT))
    try:
        page = await session.page()
        await page.goto("about:blank")
        url = f"http://127.0.0.1:{SITE_PORT}/"
        if failure != "unknown_host":
            assert (await page.evaluate(FETCH, url)).startswith("reached")
            assert site.proxied >= 1 and site.direct == 0
        if failure == "reset":
            proxy.reset = True
        else:
            proxy.stop()
        await asyncio.sleep(0.5)
        results = [await page.evaluate(FETCH, f"{url}?n={n}") for n in range(6)]
        for n in range(2):  # a navigation, not only a fetch
            try:
                await page.goto(f"{url}?nav={n}", timeout=15_000)
                results.append("navigated")
            except Exception:  # noqa: BLE001 - the page cannot load: what a dead proxy must do
                results.append("blocked")
        await asyncio.sleep(0.5)
        assert site.direct == 0, f"requests went around the dead proxy: {results}"
        assert all(r == "blocked" for r in results), results
    finally:
        proxy.stop()
        await session.close()


async def test_a_lane_without_a_proxy_stays_walled_off_from_loopback(rig, site):
    config, displays = rig
    engine = CamoufoxEngine(extra_options={"geoip": False})
    session = await _launch(config, displays, engine, "qa/noproxy", None)
    try:
        page = await session.page()
        await page.goto("about:blank")
        results = [await page.evaluate(FETCH, f"http://{host}:{SITE_PORT}/?n={n}")
                   for n in range(4) for host in ("127.0.0.1", "localhost")]
        assert site.direct == 0 and site.proxied == 0, results
        assert all(r == "blocked" for r in results), results
    finally:
        await session.close()


@pytest.mark.parametrize("target_os", ["windows", "linux"])
async def test_a_page_finds_no_automation_trace(rig, target_os, tmp_path):
    config, displays = rig
    engine = CamoufoxEngine(extra_options={"geoip": False})
    engine.allow_private_network = True  # only so the lane reaches the probe server
    engine.identity_os = [target_os]
    server = ThreadingHTTPServer(("127.0.0.1", SITE_PORT), stealthcheck._ProbeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    session = await _launch(config, displays, engine, f"qa/{target_os}", None)
    try:
        page = await session.page()
        await page.goto(f"http://127.0.0.1:{SITE_PORT}/fingerprint.html")
        await page.wait_for_selector("#dump[data-state=done]", state="attached", timeout=40_000)
        dump = json.loads(await page.text_content("#dump"))
        if os.environ.get("AGENTOS_TEST_EVIDENCE"):
            out = Path(os.environ["AGENTOS_TEST_EVIDENCE"])
            out.mkdir(parents=True, exist_ok=True)
            (out / f"probe-{target_os}.json").write_text(json.dumps(dump, indent=1, sort_keys=True))
        assert stealth.ua_os(dump["navigator"]["userAgent"]) == target_os
        findings = stealth.dump_findings("qa/x", dump)
        bad = [f.detail for f in findings if f.check in ("automation", "leak") and f.level != stealth.OK]
        assert bad == [], bad
        worker = [f.detail for f in findings if "worker" in f.detail or "frame" in f.detail]
        assert worker == [], worker
    finally:
        await session.close()
        server.shutdown()
        server.server_close()


async def test_the_stealth_check_runs_two_lanes_with_a_kill(tmp_path):
    test_slot = stealthcheck.Slot(DISPLAY_BASE // 10)
    home = ROOT / f"stealth-check-{uuid.uuid4().hex[:8]}"
    lab = stealthcheck.NodeLab(home, test_slot)
    opts = stealthcheck.Options(lanes=2, relaunches=1, soak_minutes=0.75, kill_every=15, min_available_mb=1200,
                                out=tmp_path / "out", seed=7)
    try:
        report = await stealthcheck.run(lab, opts)
    finally:
        leftover = browser_pids_under(home)
        await kill_processes(leftover)
        shutil.rmtree(home, ignore_errors=True)
    assert not leftover
    text = stealthcheck.format_report(report)
    assert report.stopped is None, text
    assert len(report.wakes) == 2 and all(len(w) >= 2 for w in report.wakes.values()), text
    assert report.soak["kills"], text
    failing = [f for f in report.findings if f.check in ("soak", "drift", "automation", "leak", "resources")
               and f.level == stealth.FAIL]
    assert failing == [], text
    assert (tmp_path / "out" / "report.json").exists()
