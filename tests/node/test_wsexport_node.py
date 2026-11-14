"""A workspace export and import with real Camoufox lanes: cookies, local storage and the pinned
fingerprint survive the round trip, and a warm lane is paused for the copy and warmed again.

Run on the node:  AGENTOS_TEST_SLOT=30 scripts/node-test.sh wsexport tests/node/test_wsexport_node.py
Slot 30: node A uses displays :300 to :304 (VNC 5300+, viewer 6300+), node B :305 to :309, and the
fixture site listens on 127.0.0.1:7700. Homes live under .node-home/ in this checkout (override
with AGENTOS_TEST_ROOT). No proxy, no real site: the only page is the local fixture.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agentos.core.config import NodeConfig
from agentos.core.models import LaneSpec, LaneState
from agentos.core.system import Host
from agentos.node.camoufox.engine import browser_pids_under, read_browser_config
from agentos.node.display import kill_processes
from agentos.node.lanes import build_lane_manager
from agentos.workspaces.transfer.export import export_workspace
from agentos.workspaces.transfer.import_ import apply_import, plan_import
from agentos.workspaces.transfer.read import read_export, staging_dir
from agentos.workspaces.transfer.sources import ManagerCoordinator, Sources

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb and Camoufox"),
]

ROOT = Path(os.environ.get("AGENTOS_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
DISPLAY = int(os.environ.get("AGENTOS_TEST_DISPLAY_BASE", "300"))
VNC = int(os.environ.get("AGENTOS_TEST_VNC_BASE", "5300"))
VIEWER = int(os.environ.get("AGENTOS_TEST_VIEWER_BASE", "6300"))
SITE_PORT = int(os.environ.get("AGENTOS_TEST_WSX_SITE_PORT", "7700"))
PASSPHRASE = "node test export passphrase"
LANE = "wsx/site"
PAGE = b"""<!doctype html><html><head><title>wsexport</title></head><body><h1>wsexport</h1></body></html>"""


class _Site(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if self.path.startswith("/login"):  # what a site does when you log in: a persistent session cookie
            self.send_header("Set-Cookie", "session=logged-in-before-the-move; Max-Age=86400; Path=/; HttpOnly")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


@pytest.fixture(scope="module")
def site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", SITE_PORT), _Site)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{SITE_PORT}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def root():
    d = ROOT / f"wsx-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def config(home: Path, name: str, offset: int) -> NodeConfig:
    home.mkdir(mode=0o700)
    return NodeConfig(home=home, name=name, display_base=DISPLAY + offset, vnc_base=VNC + offset,
                      viewer_base=VIEWER + offset, max_slots=5, max_warm_lanes=1, self_check=False)


async def wait_warm(mgr, lane: str, timeout: float = 90.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while mgr.get(lane).state != LaneState.WARM:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{lane} did not warm again after the export")
        await asyncio.sleep(0.5)


async def test_cookies_and_the_device_survive_the_round_trip(root, site):
    a = build_lane_manager(config(root / "a", "wsx-node-a", 0))
    b = build_lane_manager(config(root / "b", "wsx-node-b", 5))
    for mgr in (a, b):
        mgr.engine.allow_private_network = True  # the fixture site is on 127.0.0.1 (as in test_io_node)
    try:
        await a.start()
        await b.start()
        await a.create(LaneSpec(id=LANE))
        async with a.using(LANE) as page:
            await page.goto(f"{site}/login")
            await page.evaluate("""() => {
                document.cookie = 'pref=dark-mode; max-age=86400; path=/';
                localStorage.setItem('draft', 'kept across nodes');
            }""")
            before = sorted((c["name"], c["value"]) for c in await page.context.cookies())
        assert ("session", "logged-in-before-the-move") in before and ("pref", "dark-mode") in before
        pinned = json.loads(a.config.fingerprint_path(LANE).read_text())

        # export while the lane is warm: it is paused for the copy and warmed again
        out = root / "wsx.agentos"
        src = Sources(config=a.config, lanes=a, coordinator=ManagerCoordinator(a, asyncio.get_running_loop()),
                      host=Host())
        result = await export_workspace(src, "wsx", out, passphrase=PASSPHRASE, log_n=12)
        assert [(lane["id"], lane["copied"], lane["was_warm"]) for lane in result.lanes] == [(LANE, "paused", True)]
        await wait_warm(a, LANE)
        await a.close(LANE)  # one device in one place: the source copy goes cold before the new one opens

        contents = await asyncio.to_thread(read_export, out, staging_dir(b.config.home, "import"), passphrase=PASSPHRASE)
        try:
            target = Sources(config=b.config, lanes=b, host=Host())
            plan = await plan_import(contents, target)
            assert plan.ok, plan.blockers
            assert any("ONE DEVICE, TWO PLACES" in w for w in plan.warnings)
            await apply_import(plan, contents, target)
        finally:
            contents.discard()
        assert b.get(LANE).state == LaneState.COLD
        assert json.loads(b.config.fingerprint_path(LANE).read_text()) == pinned

        await b.open(LANE)
        pid = b._lanes[LANE].session.pid
        changed = sorted(k for k, v in pinned.items() if read_browser_config(pid).get(k) != v)
        assert changed == [], f"the imported lane launched as another device: {changed}"
        async with b.using(LANE) as page:
            await page.goto(f"{site}/")
            after = sorted((c["name"], c["value"]) for c in await page.context.cookies())
            assert await page.evaluate("localStorage.getItem('draft')") == "kept across nodes"
            assert await page.evaluate("navigator.userAgent") == pinned["navigator.userAgent"]
        assert after == before
    finally:
        await a.shutdown()
        await b.shutdown()
        leftover = browser_pids_under(root)
        await kill_processes(leftover)
        assert not leftover, f"browsers left running after the test: {leftover}"
