"""Take over and hand back on a real node: Xvfb + x11vnc + the relay, and the viewer page in a
real browser driving a real Camoufox lane.

Run on the node:  scripts/node-test.sh control tests/node/test_control_node.py -q
Uses the ranges scripts/node-test.sh exports for its slot (displays, VNC ports and HTTP ports
from AGENTOS_TEST_DISPLAY_BASE, AGENTOS_TEST_VNC_BASE and AGENTOS_TEST_MCP_PORT_BASE; by default
the control builder's :140-:149, 5840-5849 and 7440-7449), and an AGENTOS_HOME under .node-home/
in this checkout. Needs xdotool.
Set AGENTOS_TEST_SHOTS=/some/dir to keep screenshots of the viewer page.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import struct
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import uvicorn
from websockets.asyncio.client import connect

from agentos.core.config import NodeConfig
from agentos.core.models import ControlState, LaneSpec, ViewerInfo
from agentos.node.camoufox.engine import browser_pids_under
from agentos.node.display import DisplayManagerImpl, iter_processes, kill_processes, port_free, tcp_ports_of
from agentos.node.lanes import LaneManagerImpl, build_lane_manager
from agentos.node.store import LaneStore
from agentos.node.viewer_app import build_viewer_app

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb and x11vnc"),
    pytest.mark.skipif(shutil.which("xdotool") is None, reason="needs xdotool (apt install xdotool)"),
]

ROOT = Path(os.environ.get("AGENTOS_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
DISPLAY_BASE = int(os.environ.get("AGENTOS_TEST_DISPLAY_BASE", "140"))
VIEWER_BASE = int(os.environ.get("AGENTOS_TEST_VIEWER_BASE", "6140"))
RANGES = {"display_base": DISPLAY_BASE, "vnc_base": int(os.environ.get("AGENTOS_TEST_VNC_BASE", "5840")),
          "viewer_base": VIEWER_BASE, "max_slots": 10}
HTTP_BASE = int(os.environ.get("AGENTOS_TEST_MCP_PORT_BASE", "0")) or 7440
HTTP_PORTS = range(HTTP_BASE, HTTP_BASE + 10)
LANE = "test/control"
SHOTS = os.environ.get("AGENTOS_TEST_SHOTS")


# ---- helpers ------------------------------------------------------------------------------


def xdo_env(viewer: ViewerInfo) -> dict[str, str]:
    """The lane's display for xdotool: Xvfb takes only clients that hold the display's cookie."""
    assert viewer.xauthority, "a node's display always has its cookie file"
    return {**os.environ, "DISPLAY": viewer.display, "XAUTHORITY": viewer.xauthority}


def mouse(viewer: ViewerInfo) -> tuple[int, int]:
    out = subprocess.run(["xdotool", "getmouselocation", "--shell"], env=xdo_env(viewer),
                         capture_output=True, text=True, check=True, timeout=5).stdout
    values = dict(line.split("=", 1) for line in out.split())
    return int(values["X"]), int(values["Y"])


def move_mouse(viewer: ViewerInfo, x: int, y: int) -> None:
    subprocess.run(["xdotool", "mousemove", str(x), str(y)], env=xdo_env(viewer), check=True, timeout=5)


def listening() -> list[str]:
    out = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True, check=True, timeout=5).stdout
    return [line.split()[3] for line in out.splitlines()]


def http_port() -> int:
    for port in HTTP_PORTS:
        if port_free(port):
            return port
    raise RuntimeError(f"no free HTTP port in {HTTP_PORTS.start}-{HTTP_PORTS.stop - 1}")


@contextlib.asynccontextmanager
async def serve(app):
    port = http_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.02)
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


class NoBrowserSession:
    pid = None
    alive = True

    async def page(self):
        return object()

    async def close(self):
        pass


class NoBrowserEngine:
    """A lane without a browser: the relay tests only need the display and x11vnc."""
    name = "camoufox"

    async def launch(self, spec, *, profile_dir, fingerprint_path, display, xauthority=None, proxy):
        return NoBrowserSession()

    async def sweep_orphans(self, root):
        return 0


class RfbClient:
    """Just enough RFB 3.8 to get through the handshake and send input."""

    def __init__(self, ws):
        self.ws = ws
        self.buf = bytearray()

    async def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            self.buf += await asyncio.wait_for(self.ws.recv(), 10)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    async def handshake(self) -> tuple[int, int]:
        assert (await self.read(12)).startswith(b"RFB 003.")
        await self.ws.send(b"RFB 003.008\n")
        count = (await self.read(1))[0]
        assert 1 in await self.read(count), "x11vnc should offer security type None"
        await self.ws.send(bytes([1]))
        assert await self.read(4) == bytes(4), "security result"
        await self.ws.send(bytes([0]))  # ask for exclusive access; the relay makes it shared
        init = await self.read(24)
        width, height = struct.unpack(">HH", init[:4])
        await self.read(struct.unpack(">I", init[20:24])[0])
        return width, height

    async def pointer(self, x: int, y: int, mask: int = 0) -> None:
        await self.ws.send(struct.pack(">BBHH", 5, mask, x, y))


@contextlib.asynccontextmanager
async def rfb(url: str):
    async with connect(url, subprotocols=["binary"], max_size=None) as ws:
        client = RfbClient(ws)
        assert await client.handshake() == (1920, 1080)
        yield client


@pytest.fixture
def home():
    d = ROOT / f"control-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
async def relay_node(home):
    """A lane with a real display and x11vnc (no browser), the viewer app on HTTP_PORTS."""
    config = NodeConfig(home=home, self_check=False, **RANGES)
    mgr = LaneManagerImpl(config, LaneStore(config.db_path), NoBrowserEngine(), DisplayManagerImpl(config))
    await mgr.start()
    await mgr.create(LaneSpec(id=LANE))
    info = await mgr.open(LANE)
    app = build_viewer_app(mgr, config)
    try:
        async with serve(app) as host:
            yield mgr, info.viewer, app.state.viewer.tokens, host
    finally:
        await mgr.shutdown()


# ---- the relay with a small RFB client ------------------------------------------------------


async def test_pointer_moves_only_for_a_human_with_a_control_link(relay_node):
    mgr, viewer, tokens, host = relay_node
    assert DISPLAY_BASE <= int(viewer.display[1:]) < DISPLAY_BASE + 10

    def url(mode: str) -> str:
        return f"ws://{host}/viewer-ws/{LANE}?t={tokens.issue(LANE, mode).token}"

    move_mouse(viewer, 10, 10)

    async with rfb(url("control")) as client:
        await client.pointer(500, 400)  # agents have control: dropped
        await asyncio.sleep(0.5)
        assert mouse(viewer) == (10, 10)

        await mgr.take_control(LANE, by="owner")
        await client.pointer(500, 400)
        await asyncio.sleep(0.5)
        assert mouse(viewer) == (500, 400)
        assert mgr.get(LANE).control.last_human_input is not None

    async with rfb(url("view")) as watcher:  # a view link never moves it, even under human control
        await watcher.pointer(700, 600)
        await asyncio.sleep(0.5)
        assert mouse(viewer) == (500, 400)

    await mgr.stop_agent(LANE, by="owner")  # paused: nobody acts
    async with rfb(url("control")) as client:
        await client.pointer(800, 100)
        await asyncio.sleep(0.5)
        assert mouse(viewer) == (500, 400)

    await mgr.take_control(LANE, by="owner")
    async with rfb(url("control")) as client:
        await client.pointer(900, 300)
        await asyncio.sleep(0.5)
        assert mouse(viewer) == (900, 300)
        await mgr.hand_back(LANE, by="owner")  # mid-session: the same connection loses input
        await client.pointer(100, 900)
        await asyncio.sleep(0.5)
        assert mouse(viewer) == (900, 300)


async def test_two_viewers_share_the_screen(relay_node):
    """ClientInit is forced to shared, so a second viewer never disconnects the first."""
    mgr, viewer, tokens, host = relay_node
    await mgr.take_control(LANE, by="owner")
    token = tokens.issue(LANE, "control").token
    async with rfb(f"ws://{host}/viewer-ws/{LANE}?t={token}") as first, \
            rfb(f"ws://{host}/viewer-ws/{LANE}?t={token}"):
        await asyncio.sleep(0.5)
        await first.pointer(321, 123)
        await asyncio.sleep(0.5)
        assert mouse(viewer) == (321, 123)


async def test_relay_bypasses_websockify_and_x11vnc_stays_local(relay_node):
    _, viewer, _, _ = relay_node
    assert isinstance(viewer, ViewerInfo) and viewer.viewer_port is None
    assert viewer.vnc_port is None and Path(viewer.vnc_socket).is_socket()  # a private unix socket, no port
    x11vnc = [pid for pid, argv in iter_processes() if os.path.basename(argv[0]) == "x11vnc" and viewer.display in argv]
    assert len(x11vnc) == 1 and tcp_ports_of(x11vnc[0]) == set()
    slot = int(viewer.display[1:]) - DISPLAY_BASE
    assert not [addr for addr in listening() if addr.endswith(f":{VIEWER_BASE + slot}")]  # no websockify


# ---- the viewer page in a real browser, driving a real Camoufox lane --------------------------

PAGE = """<!doctype html><html><head><title>control test</title>
<style>html,body{margin:0;height:100%;background:#f3f5fa;font:28px sans-serif}
#zone{position:fixed;inset:0;display:grid;place-items:center}</style></head>
<body><div id="zone"><div>agentos take-over test<br><input id="field" autofocus></div></div>
<script>
document.addEventListener('mousedown', e => {
  document.body.dataset.clicks = String(Number(document.body.dataset.clicks || 0) + 1);
  document.body.dataset.at = e.clientX + ',' + e.clientY;
  e.preventDefault();  // keep the focus in the field
});
</script></body></html>"""


@pytest.fixture
async def browser_node(home):
    config = NodeConfig(home=home, self_check=False, **RANGES)
    mgr = build_lane_manager(config)
    await mgr.start()
    await mgr.create(LaneSpec(id=LANE))
    page_path = home / "page.html"
    page_path.write_text(PAGE)
    async with mgr.using(LANE, principal_kind="human") as page:
        await page.goto(page_path.as_uri())
    SCREEN_WIDTH[0] = mgr.view(LANE).width or 1920
    app = build_viewer_app(mgr, config)
    try:
        async with serve(app) as host:
            yield mgr, app.state.viewer.tokens, host
    finally:
        await mgr.shutdown()
        leftover = browser_pids_under(home)
        await kill_processes(leftover)
        assert not leftover, f"browsers left running: {leftover}"


async def lane_clicks(mgr) -> int:
    async with mgr.using(LANE, principal_kind="human") as page:
        return int(await page.evaluate("Number(document.body.dataset.clicks || 0)"))


async def lane_field(mgr) -> str:
    async with mgr.using(LANE, principal_kind="human") as page:
        return await page.evaluate("document.getElementById('field').value")


async def shot(page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


SCREEN_WIDTH = [1920]  # the lane's display width (the display is sized to the browser window)


async def click_screen(page, x: int, y: int) -> None:
    """Click the remote screen at lane coordinates (x, y) through the scaled canvas."""
    box = await page.locator("#screen canvas").bounding_box()
    scale = box["width"] / SCREEN_WIDTH[0]
    await page.mouse.click(box["x"] + x * scale, box["y"] + y * scale)


async def test_viewer_page_takes_over_and_hands_back(browser_node):
    from camoufox.async_api import AsyncCamoufox

    mgr, tokens, host = browser_node
    url = f"http://{host}/view/{LANE}?t={tokens.issue(LANE, 'control', owner=True).token}"
    errors: list[str] = []
    async with AsyncCamoufox(headless=True, humanize=False, locale="en-US") as browser:
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(url)
        await page.wait_for_selector("#overlay", state="hidden", timeout=20_000)  # live view connected
        assert await page.locator("#mode-text").text_content() == "View only. The agent has control."  # the pill fades after 4 s
        assert await page.locator("#who-text").inner_text() == "Agent in control"
        await shot(page, "1-agent")

        before = await lane_clicks(mgr)
        await click_screen(page, 960, 540)  # agents have control: the click must not reach the lane
        await asyncio.sleep(1.0)
        assert await lane_clicks(mgr) == before

        await page.get_by_role("button", name="Take control").click()
        await page.wait_for_selector("#app[data-interactive='true']", timeout=10_000)
        assert mgr.get(LANE).control.state == ControlState.HUMAN
        assert await page.locator("#who-text").inner_text() == "You have control"
        await click_screen(page, 960, 540)
        for _ in range(50):
            if await lane_clicks(mgr) > before:
                break
            await asyncio.sleep(0.1)
        assert await lane_clicks(mgr) == before + 1
        await page.keyboard.type("hi there")
        for _ in range(50):
            if await lane_field(mgr) == "hi there":
                break
            await asyncio.sleep(0.1)
        assert await lane_field(mgr) == "hi there"
        await shot(page, "2-human")

        await mgr.request_control(LANE, by="claude-code", reason="The login is done; I need to post the reply.")
        banner = page.locator("#request-banner")
        await banner.wait_for(state="visible", timeout=10_000)
        assert "The login is done; I need to post the reply." in await banner.inner_text()
        await page.wait_for_timeout(400)
        await shot(page, "3-request")
        await page.get_by_role("button", name="Keep control").click()
        await banner.wait_for(state="hidden", timeout=10_000)
        assert not mgr.get(LANE).control.takeback_requested

        await page.keyboard.press("Alt+Shift+KeyH")  # the shortcut, while keys go to the lane
        await page.wait_for_selector("#app[data-control='agent']", timeout=10_000)
        assert mgr.get(LANE).control.state == ControlState.AGENT
        clicks = await lane_clicks(mgr)
        await click_screen(page, 900, 500)
        await asyncio.sleep(1.0)
        assert await lane_clicks(mgr) == clicks

        await page.get_by_role("button", name="Stop agent").click()
        dialog = page.locator("#dlg-stop")
        await dialog.wait_for(state="visible")
        await shot(page, "4-stop-confirm")
        await page.locator("#btn-stop-confirm").click()
        await page.wait_for_selector("#app[data-control='paused']", timeout=10_000)
        assert mgr.get(LANE).control.state == ControlState.PAUSED

        await page.locator("#btn-theme").click()  # auto -> light
        await page.wait_for_timeout(300)
        await shot(page, "5-paused-light")
        await page.locator("#btn-theme").click()  # light -> dark
        await page.wait_for_timeout(300)
        assert await page.evaluate("document.documentElement.dataset.theme") == "dark"
        await shot(page, "6-paused-dark")
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_timeout(500)
        await shot(page, "7-paused-mobile-dark")
        await page.get_by_role("button", name="Hand back to agent").click()
        await page.wait_for_selector("#app[data-control='agent']", timeout=10_000)

    assert errors == []


async def test_view_link_page_has_no_controls(browser_node):
    from camoufox.async_api import AsyncCamoufox

    mgr, tokens, host = browser_node
    await mgr.take_control(LANE, by="owner")
    url = f"http://{host}/view/{LANE}?t={tokens.issue(LANE, 'view').token}"
    async with AsyncCamoufox(headless=True, humanize=False, locale="en-US") as browser:
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1280, "height": 800})
        await page.goto(url)
        await page.wait_for_selector("#overlay", state="hidden", timeout=20_000)
        assert await page.locator("#actions").is_hidden()
        assert await page.locator("#mode-text").text_content() == "View only"
        assert await page.locator("#hint").inner_text() == "This link can watch but not control."
        before = await lane_clicks(mgr)
        await click_screen(page, 960, 540)
        await asyncio.sleep(1.0)
        assert await lane_clicks(mgr) == before
        await shot(page, "8-view-only")
