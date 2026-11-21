"""Lane hardening on a real node: no websockify, the window fills the display, holds stop agents
and flows across a restart, the memory floor, per-lane memory scopes, quarantine.

Run on the node:  OPENAJAR_TEST_SLOT=27 scripts/node-test.sh lanesfix tests/node/test_lanes_hardening_node.py -q
Uses the ranges scripts/node-test.sh exports (displays, VNC and viewer ports of the slot), an
OPENAJAR_HOME under .node-home/ in this checkout, and real Camoufox browsers (one at a time).
A tiny HTTP server on the slot's last viewer port stands in for a site (the lanes no longer run
websockify, so those ports are free); only these tests' lanes may reach it.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from openajar.core.config import NodeConfig
from openajar.core.errors import AgentPaused, LaneQuarantined
from openajar.core.models import ControlState, LaneSpec, LaneState, Signal
from openajar.core.runtime import Runtime
from openajar.node.camoufox.engine import (
    browser_pids,
    browser_pids_under,
    display_size_of,
    window_position_of,
)
from openajar.node.display import iter_processes, kill_processes, listening_ports, process_group, tcp_ports_of
from openajar.node.flow_driver import LaneAgentPaused, driver_factory
from openajar.node.lanes import (
    LaneManagerImpl,
    ResourcesLow,
    ScopeLimiter,
    available_memory_mb,
    build_lane_manager,
)

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb, x11vnc and Camoufox"),
]

ROOT = Path(os.environ.get("OPENAJAR_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
DISPLAY_BASE = int(os.environ.get("OPENAJAR_TEST_DISPLAY_BASE", "270"))
VNC_BASE = int(os.environ.get("OPENAJAR_TEST_VNC_BASE", "5270"))
VIEWER_BASE = int(os.environ.get("OPENAJAR_TEST_VIEWER_BASE", "6270"))
RANGES = {"display_base": DISPLAY_BASE, "vnc_base": VNC_BASE, "viewer_base": VIEWER_BASE, "max_slots": 9}
SITE_PORT = VIEWER_BASE + 9


def policy(signal: Signal, *, now: datetime):
    if signal in (Signal.CAPTCHA, Signal.CHECKPOINT, Signal.BLOCKED):
        return f"needs_human:{signal.value}", None
    if signal == Signal.RATE_LIMITED:
        return "rate_limited", now + timedelta(hours=6)
    return None


class _Site(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        code = int(self.path.rsplit("/", 1)[-1]) if self.path.startswith("/status/") else 200
        body = f"<!doctype html><title>Status {code}</title><h1>Status {code}</h1>".encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", SITE_PORT), _Site)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{SITE_PORT}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def home():
    d = ROOT / f"hardening-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
async def managers(home):
    made: list[LaneManagerImpl] = []

    def make(**kw) -> LaneManagerImpl:
        cfg = {k: kw.pop(k) for k in list(kw) if k in NodeConfig.model_fields}
        mgr = build_lane_manager(NodeConfig(home=home, **{**RANGES, "self_check": False, **cfg}), **kw)
        made.append(mgr)
        return mgr

    yield make
    for mgr in made:
        await mgr.shutdown()
    leftover = browser_pids_under(home)
    await kill_processes(leftover)
    assert not leftover, f"browsers left running after the test: {leftover}"


def our_ports() -> set[int]:
    ranges = set(range(VNC_BASE, VNC_BASE + 10)) | set(range(VIEWER_BASE, VIEWER_BASE + 9))
    return (listening_ports() or set()) & ranges


def x_env(viewer) -> dict[str, str]:
    """The lane's display for X clients: Xvfb takes only clients that hold the display's cookie."""
    return {**os.environ, "DISPLAY": viewer.display, "XAUTHORITY": viewer.xauthority}


def windows(viewer) -> tuple[tuple[int, int], list[tuple[str, int, int, int, int]]]:
    """(root size, [(class, width, height, x, y) of each top-level window]) on the lane's display."""
    display = viewer.display
    root = subprocess.run(["xwininfo", "-root", "-display", display], capture_output=True, text=True, check=True,
                          timeout=10, env=x_env(viewer)).stdout
    values = dict(line.strip().split(":", 1) for line in root.splitlines() if ":" in line)
    size = (int(values["Width"]), int(values["Height"]))
    tree = subprocess.run(["xwininfo", "-root", "-children", "-display", display], capture_output=True, text=True,
                          check=True, timeout=10, env=x_env(viewer)).stdout
    found = []
    for line in tree.splitlines():
        parts = line.split()
        geometry = next((p for p in parts if "x" in p and "+" in p and p[0].isdigit()), None)
        if geometry is None or "(" not in line:
            continue
        dims, x, y = geometry.split("+")[0], *geometry.split("+")[1:3]
        width, height = map(int, dims.split("x"))
        found.append((line.split("(", 1)[1].split(")")[0], width, height, int(x), int(y)))
    return size, found


def corner_mean(viewer, width: int, height: int) -> float:
    """Mean brightness (0..1) of the 40x40 pixels of the X screen above and left of (width, height)."""
    shot = subprocess.run(["import", "-display", viewer.display, "-window", "root", "png:-"], capture_output=True,
                          timeout=20, check=True, env=x_env(viewer)).stdout
    out = subprocess.run(["convert", "png:-", "-crop", f"40x40+{width - 40}+{height - 40}", "-format",
                          "%[fx:mean]", "info:"], input=shot, capture_output=True, timeout=20, check=True).stdout
    return float(out)


# ---- the viewer path -----------------------------------------------------------------------


async def test_no_websockify_listens_and_x11vnc_is_the_only_listener(managers, home):
    mgr = managers()
    await mgr.start()
    await mgr.create(LaneSpec(id="test/view"))
    before = our_ports()
    info = await mgr.open("test/view")
    viewer = info.viewer
    assert viewer.viewer_port is None and viewer.tunnel_hint().startswith("the live view is served by agentosd")
    assert our_ports() - before == set()  # x11vnc is on a unix socket: no TCP port at all
    assert viewer.vnc_port is None and Path(viewer.vnc_socket).is_socket()
    ws = [argv for _, argv in iter_processes() if any("websockify" in os.path.basename(a) for a in argv[:2])
          and any(str(VIEWER_BASE + i) in " ".join(argv) for i in range(10))]
    assert ws == [], f"websockify still runs in our range: {ws}"
    vnc = [(pid, argv) for pid, argv in iter_processes()
           if os.path.basename(argv[0]) == "x11vnc" and viewer.display in argv]
    assert len(vnc) == 1 and {"-noipv6", "-norc", "-noremote", "-nocmds"} <= set(vnc[0][1])
    assert "-localhost" not in vnc[0][1]
    assert tcp_ports_of(vnc[0][0]) == set()
    await mgr.close("test/view")
    assert our_ports() == before


async def test_the_browser_window_sits_where_its_pin_says(managers, home):
    """The display is the pinned screen and the real window sits at the pinned position, so a page's
    mozInnerScreenX/Y (the real window) agree with window.screenX/Y (the pin): Camoufox issue 974."""
    mgr = managers()
    await mgr.start()
    lane_id = "test/fill"
    await mgr.create(LaneSpec(id=lane_id))
    for launch in ("first launch (the pin is made)", "pinned relaunch"):
        info = await mgr.open(lane_id)
        pin = json.loads(mgr.config.fingerprint_path(lane_id).read_text())
        window = (pin["window.outerWidth"], pin["window.outerHeight"])
        await asyncio.sleep(2)  # let the browser draw
        size, tops = await asyncio.to_thread(windows, info.viewer)
        navigator = [w for w in tops if "Navigator" in w[0]]
        assert navigator, f"{launch}: no browser window on {info.viewer.display}: {tops}"
        _, width, height, x, y = navigator[0]
        assert size == display_size_of(pin) == (info.viewer.width, info.viewer.height), \
            f"{launch}: screen {size}, pin screen {pin['screen.width']}x{pin['screen.height']}"
        assert (width, height) == window, f"{launch}: window {width}x{height}, pin {window}"
        assert (x, y) == window_position_of(pin), f"{launch}: the window sits at +{x}+{y}"
        async with mgr.using(lane_id) as page:
            seen = await page.evaluate("() => [screenX, screenY, Math.round(mozInnerScreenX), "
                                       "Math.round(mozInnerScreenY), outerHeight - innerHeight]")
        assert seen[2] == seen[0] and seen[3] == seen[1] + seen[4], f"{launch}: the page reads {seen}"
        assert await asyncio.to_thread(corner_mean, info.viewer, x + width, y + height) > 0.5, \
            f"{launch}: the bottom-right corner of the window is dark (not drawn)"
        await mgr.close(lane_id)


# ---- holds ---------------------------------------------------------------------------------


async def test_a_hold_stops_an_agent_and_a_flow_and_survives_a_restart(managers, home, site):
    from openajar.node.interaction import create_interaction

    mgr = managers(hold_policy=policy)
    mgr.engine.allow_private_network = True  # only so this lane can reach the stand-in site
    await mgr.start()
    lane_id = "test/hold"
    await mgr.create(LaneSpec(id=lane_id))
    interaction = mgr.watch(create_interaction(allow_private_targets=True))
    async with mgr.using(lane_id) as page:
        assert (await interaction.navigate(page, f"{site}/ok")).signal == Signal.OK
        blocked = await interaction.navigate(page, f"{site}/status/403")  # the site refuses us
    assert blocked.signal == Signal.BLOCKED
    info = mgr.get(lane_id)
    assert info.signal == Signal.BLOCKED and info.hold == "needs_human:blocked on 127.0.0.1"
    assert info.control.state == ControlState.PAUSED and info.hold_until is None

    async def agent_and_flow_stop(manager: LaneManagerImpl) -> None:
        with pytest.raises(AgentPaused, match="needs_human:blocked"):
            async with manager.using(lane_id):
                pass
        watched = manager.watch(create_interaction(allow_private_targets=True))
        runtime = Runtime(config=manager.config, lanes=manager, interaction=watched)
        with pytest.raises(LaneAgentPaused):
            async with driver_factory(runtime)(lane_id):
                pass

    await agent_and_flow_stop(mgr)
    await mgr.shutdown()
    assert browser_pids(mgr.config.profile_dir(lane_id), main_only=False) == []

    after = managers(hold_policy=policy)  # agentosd restarted
    after.engine.allow_private_network = True
    await after.start()
    assert after.get(lane_id).hold == "needs_human:blocked on 127.0.0.1"
    await agent_and_flow_stop(after)
    await after.hand_back(lane_id, by="owner")
    async with after.using(lane_id) as page:
        watched = after.watch(create_interaction(allow_private_targets=True))
        assert (await watched.navigate(page, f"{site}/ok")).signal == Signal.OK
    assert after.get(lane_id).hold is None
    await after.close(lane_id)


# ---- resources -----------------------------------------------------------------------------


async def test_the_memory_floor_refuses_a_warm_with_a_clear_error(managers, home):
    available = available_memory_mb()
    assert available is not None
    mgr = managers(memory_floor_mb=available + 4096)
    await mgr.start()
    await mgr.create(LaneSpec(id="test/ram"))
    before = our_ports()
    with pytest.raises(ResourcesLow) as e:
        await mgr.open("test/ram")
    assert e.value.code == "resources_low" and "MB of memory is free on this node" in e.value.message
    assert "test/ram was not opened" in e.value.message and "\u2014" not in e.value.message
    assert browser_pids_under(home) == [] and our_ports() == before
    assert not any(Path(f"/tmp/.X11-unix/X{DISPLAY_BASE + i}").exists() for i in range(9))
    assert mgr.get("test/ram").state == LaneState.COLD


@pytest.mark.skipif(not ScopeLimiter.supported(), reason="needs systemd and busctl")
async def test_a_lane_browser_runs_under_its_own_memory_limit(managers, home):
    mgr = managers(lane_memory_max_mb=1536)
    await mgr.start()
    lane_id = "test/limit"
    await mgr.create(LaneSpec(id=lane_id))
    await mgr.open(lane_id)
    pid = mgr._lanes[lane_id].session.pid
    cgroup = Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1]
    assert cgroup.rsplit("/", 1)[1].startswith("openajar-lane-test--limit-") and cgroup.endswith(".scope")
    limit = Path("/sys/fs/cgroup", cgroup.lstrip("/"), "memory.max").read_text().strip()
    assert limit == str(1536 * 1024 * 1024)
    children = [p for p in process_group(pid) if p != pid]  # content processes share the browser's group
    assert children and all(Path(f"/proc/{c}/cgroup").read_text().strip().endswith(cgroup) for c in children)
    await mgr.close(lane_id)
    for _ in range(50):
        if not Path("/sys/fs/cgroup", cgroup.lstrip("/")).exists():
            break
        await asyncio.sleep(0.1)
    assert not Path("/sys/fs/cgroup", cgroup.lstrip("/")).exists(), "the scope outlived the browser"


# ---- quarantine ----------------------------------------------------------------------------


async def test_quarantine_closes_the_real_browser_and_survives_a_restart(managers, home):
    mgr = managers()
    await mgr.start()
    lane_id = "test/quarantine"
    await mgr.create(LaneSpec(id=lane_id))
    await mgr.open(lane_id)
    profile = mgr.config.profile_dir(lane_id)
    assert browser_pids(profile)
    info = await mgr.quarantine(lane_id, "exit IP 203.0.113.9 does not match proxy lt1; found by the health check")
    assert info.state == LaneState.QUARANTINED and browser_pids(profile, main_only=False) == []
    await mgr.shutdown()
    after = managers()
    await after.start()
    with pytest.raises(LaneQuarantined):
        await after.open(lane_id)
    await after.hand_back(lane_id, by="owner")
    assert (await after.open(lane_id)).state == LaneState.WARM
    await after.close(lane_id)
