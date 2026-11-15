"""The live view never flashes: a real lane, x11vnc, the relay and the viewer page in a real browser.

The owner saw the live view go white for an instant during the first real logins, without the
lane's page reloading. The cause was the health monitor's exit IP probe: once a minute it read a
warm lane's exit IP in a scratch page, and Camoufox opens that page as a second window over the
whole screen (black until painted, then white) until the fetch through the proxy is done.

While the page gets every kind of state event (control changes, lane state changes from agent
steps, the exit IP probe, the agent's request for control, block notices) and its layout changes
(notices, the activity panel, a narrow window), a probe in the viewer page counts:

- WebSockets opened after the first connection (a reconnect),
- canvases added to or removed from #screen (noVNC makes a new one per connection),
- the overlay appearing,
- frames where the lane's own page is mostly gone from the remote screen (sampled on every
  animation frame, straight from noVNC's canvas).

Run on the node:  AGENTOS_TEST_SLOT=48 scripts/node-test.sh viewerflash tests/node/test_viewerflash_node.py -q
Set AGENTOS_TEST_SHOTS=/some/dir to keep screenshots of the viewer page.

The viewer browser is a headless Camoufox on the node. Set AGENTOS_TEST_CDP=http://127.0.0.1:9447
to drive a Chrome somewhere else instead (the owner's setup: Chrome on a Mac, the node through an
SSH tunnel): start Chrome with --remote-debugging-port=9447 and forward both ways from the Mac,
ssh mynode -R 9447:127.0.0.1:9447 -L 7780:127.0.0.1:7780 ... (the slot's HTTP ports).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections import Counter

import pytest
from test_control_node import LANE, RANGES, home, serve, shot  # noqa: F401  (home is a fixture)

from agentos.core.config import NodeConfig
from agentos.core.models import LaneSpec
from agentos.node.camoufox.engine import browser_pids_under
from agentos.node.display import kill_processes
from agentos.node.lanes import build_lane_manager
from agentos.node.lanes import quarantine as lanes_quarantine
from agentos.node.viewer_app import build_viewer_app

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb and x11vnc"),
]

CDP = os.environ.get("AGENTOS_TEST_CDP")
PAGE_RGB = (29, 43, 68)  # the lane page's background; most of the remote screen
PAGE = """<!doctype html><html><head><title>flash test</title>
<style>html,body{margin:0;height:100%;background:#1d2b44;color:#9fb3d9;font:28px sans-serif}
#zone{position:fixed;inset:0;display:grid;place-items:center}</style></head>
<body><div id="zone"><div>agentos flash test <span id="tick">0</span><br><input id="field" autofocus></div></div>
<script>let n = 0; setInterval(() => { document.getElementById('tick').textContent = String(++n); }, 150);</script>
</body></html>"""

# The exit IP fetch as slow as through a real proxy, without leaving the node (the lane's network
# guard refuses loopback, and a fast fetch can finish before the scratch window is ever painted).
SLOW_SELF_CHECK_JS = "async () => { await new Promise((r) => setTimeout(r, 800)); return '203.0.113.7'; }"

# Runs in the viewer page before its scripts. Records everything that would show as a flash.
PROBE = """
(() => {
  const probe = window.__probe = { sockets: 0, canvasesAdded: 0, canvasesRemoved: 0, overlayShown: [],
    samples: 0, covered: [], log: [] };
  const [R, G, B] = PAGE_RGB;
  const t0 = performance.now();
  const at = () => Math.round(performance.now() - t0);
  const NativeWS = window.WebSocket;
  window.WebSocket = function (...args) {
    if (probe.live) probe.sockets += 1;
    probe.log.push(['ws', at()]);
    return new NativeWS(...args);
  };
  window.WebSocket.prototype = NativeWS.prototype;
  Object.assign(window.WebSocket, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
  function sample() {
    const canvas = document.querySelector('#screen canvas');
    if (probe.live && canvas && canvas.width > 0 && canvas.height > 0) {
      const ctx = canvas.getContext('2d');
      let page = 0, all = 0;
      for (const fy of [0.3, 0.45, 0.6, 0.75, 0.9]) {
        const row = ctx.getImageData(0, Math.floor(canvas.height * fy), canvas.width, 1).data;
        for (let i = 0; i < row.length; i += 64) {
          all += 1;
          if (Math.abs(row[i] - R) < 24 && Math.abs(row[i + 1] - G) < 24 && Math.abs(row[i + 2] - B) < 24) page += 1;
        }
      }
      probe.samples += 1;
      if (page < all / 2) probe.covered.push([at(), probe.phase || '', Math.round(100 * page / all)]);
    }
    requestAnimationFrame(sample);
  }
  requestAnimationFrame(sample);
  document.addEventListener('DOMContentLoaded', () => {
    const isCanvas = (node) => node.tagName === 'CANVAS' || Boolean(node.querySelector?.('canvas'));
    new MutationObserver((records) => {
      for (const r of records) {
        for (const node of r.addedNodes) if (isCanvas(node)) { if (probe.live) probe.canvasesAdded += 1; probe.log.push(['canvas+', at()]); }
        for (const node of r.removedNodes) if (isCanvas(node)) { if (probe.live) probe.canvasesRemoved += 1; probe.log.push(['canvas-', at()]); }
      }
    }).observe(document.getElementById('screen'), { childList: true, subtree: true });
    const overlay = document.getElementById('overlay');
    new MutationObserver(() => {
      if (!overlay.hidden && probe.live) probe.overlayShown.push([at(), document.getElementById('overlay-title').textContent]);
    }).observe(overlay, { attributes: true, attributeFilter: ['hidden'] });
  });
})();
""".replace("PAGE_RGB", json.dumps(PAGE_RGB))


def notice(n: int) -> dict:
    host = f"cdn{n}.example.net"
    return {"rule": "noted", "host": host, "kind": "subframe", "reason": "Not on the lane's site list",
            "path": "/x", "allow": host, "frame": True}


async def node_ip() -> str:
    return "203.0.113.7"  # what the slow self-check reads: the lane has no proxy, so it is the node's


@pytest.fixture
async def flash_node(home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(lanes_quarantine, "SELF_CHECK_JS", SLOW_SELF_CHECK_JS)
    config = NodeConfig(home=home, self_check=False, **RANGES)
    mgr = build_lane_manager(config, node_ip_probe=node_ip)
    await mgr.start()
    await mgr.create(LaneSpec(id=LANE))
    page_path = home / "page.html"
    page_path.write_text(PAGE)
    async with mgr.using(LANE, principal_kind="human") as page:
        await page.goto(page_path.as_uri())
    app = build_viewer_app(mgr, config)
    try:
        async with serve(app) as host:
            yield mgr, app.state.viewer.tokens, host
    finally:
        await mgr.shutdown()
        leftover = browser_pids_under(home)
        await kill_processes(leftover)
        assert not leftover, f"browsers left running: {leftover}"


@contextlib.asynccontextmanager
async def viewer_page():
    if CDP:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(CDP)
            context = await browser.new_context()
            try:
                yield await context.new_page()
            finally:
                await context.close()
        return
    from camoufox.async_api import AsyncCamoufox

    # canvas_noise off: the probe reads the live view's canvas, and noise would scramble it
    async with AsyncCamoufox(headless=True, humanize=False, locale="en-US", canvas_noise=False) as browser:
        yield await browser.new_page()


async def click_screen(page, x: float, y: float) -> None:
    """Click the remote screen at a fraction (x, y) of its width and height."""
    box = await page.locator("#screen canvas").bounding_box()
    await page.mouse.click(box["x"] + box["width"] * x, box["y"] + box["height"] * y)


async def test_nothing_a_watcher_sees_flashes_the_live_view(flash_node):
    mgr, tokens, host = flash_node
    url = f"http://{host}/view/{LANE}?t={tokens.issue(LANE, 'control', owner=True).token}"
    errors: list[str] = []
    probed: list[str | None] = []
    async with viewer_page() as page:
        await page.add_init_script(PROBE)
        await page.set_viewport_size({"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(url)
        await page.wait_for_selector("#overlay", state="hidden", timeout=20_000)
        await page.wait_for_timeout(1500)  # the first full frame
        await page.evaluate("window.__probe.live = true")

        async def phase(name: str) -> None:
            await page.evaluate(f"window.__probe.phase = {json.dumps(name)}")

        await phase("control")
        for _ in range(3):
            await mgr.take_control(LANE, by="owner")
            await page.wait_for_timeout(700)
            await mgr.hand_back(LANE, by="owner")
            await page.wait_for_timeout(700)

        await phase("agent steps")  # the lane goes active and back to warm
        for i in range(3):
            async with mgr.using(LANE) as lane_page:
                await lane_page.evaluate(f"document.title = 'step {i}'")
                await asyncio.sleep(0.3)
            await page.wait_for_timeout(2500)  # the event stream polls lane state every 2 s

        await phase("exit ip probe")  # the health monitor reads every warm lane's exit IP once a minute
        for _ in range(3):
            probed.append(await mgr.probe_exit_ip(LANE))
            await page.wait_for_timeout(700)

        await phase("human input")
        await page.get_by_role("button", name="Take control").click()
        await page.wait_for_selector("#app[data-interactive='true']", timeout=10_000)
        await click_screen(page, 0.5, 0.5)
        await page.keyboard.type("someone@example.com", delay=40)
        await page.keyboard.press("Tab")
        await page.mouse.wheel(0, 300)
        await click_screen(page, 0.5, 0.5)
        await page.wait_for_timeout(500)
        await mgr.request_control(LANE, by="claude-code", reason="done with the login")
        await page.wait_for_timeout(1000)

        await phase("notices")
        for i in range(3):
            for listener in list(mgr._notice_listeners):
                listener(LANE, notice(i))
            await page.wait_for_timeout(500)
        await shot(page, "flash-notices")
        for _ in range(3):
            await page.locator("#notices button", has_text="Dismiss").first.click()
            await page.wait_for_timeout(300)
        await mgr.hand_back(LANE, by="owner")
        await page.wait_for_timeout(800)

        await phase("layout")
        for _ in range(2):
            await page.keyboard.press("Alt+Shift+KeyA")
            await page.wait_for_timeout(800)
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_timeout(1000)
        await shot(page, "flash-narrow")
        await page.set_viewport_size({"width": 1440, "height": 900})
        await page.wait_for_timeout(1000)

        probe = await page.evaluate("window.__probe")

    assert errors == []
    assert probe["samples"] > 100
    covered = Counter(phase for _, phase, _ in probe["covered"])  # frames where the lane's page was gone
    assert covered == {}
    assert probe["sockets"] == 0, f"the live view reconnected: {probe['log']}"
    assert probe["canvasesAdded"] == 0 and probe["canvasesRemoved"] == 0, probe["log"]
    assert probe["overlayShown"] == []
    assert probed == [None, None, None]  # watched: the health monitor checks the proxy from the daemon
    await asyncio.sleep(0.5)  # the relay notices the viewer left
    assert await mgr.probe_exit_ip(LANE) == "203.0.113.7"  # nobody watches any more: read in the lane
