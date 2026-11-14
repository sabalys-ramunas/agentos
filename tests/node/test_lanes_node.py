"""Lanes on a real node: Xvfb + x11vnc displays and real Camoufox browsers.

Run on the node:  scripts/node-test.sh lanes tests/node/test_lanes_node.py -q
Uses the ranges scripts/node-test.sh exports for its slot (AGENTOS_TEST_DISPLAY_BASE and
AGENTOS_TEST_VNC_BASE; by default the lanes agent's :110-:119 and 5810-5819) and an
AGENTOS_HOME under .node-home/ in this checkout (override with AGENTOS_TEST_ROOT).
At most two browsers run at once, and every test closes what it opened.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agentos.core.config import NodeConfig
from agentos.core.models import LaneSpec, LaneState
from agentos.node.display import (
    DisplayManagerImpl,
    iter_processes,
    kill_processes,
    listening_ports,
    pid_alive,
    tcp_ports_of,
)
from agentos.node.engine_camoufox import (
    LAUNCHER_KEYS,
    PIN_META,
    PIN_VERSION,
    browser_pids,
    browser_pids_under,
    read_browser_config,
)
from agentos.node.lanes import LaneManagerImpl, build_lane_manager

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb and Camoufox"),
]

ROOT = Path(os.environ.get("AGENTOS_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
DISPLAY_BASE = int(os.environ.get("AGENTOS_TEST_DISPLAY_BASE", "110"))
VNC_BASE = int(os.environ.get("AGENTOS_TEST_VNC_BASE", "5810"))
VIEWER_BASE = int(os.environ.get("AGENTOS_TEST_VIEWER_BASE", "6110"))
RANGES = {"display_base": DISPLAY_BASE, "vnc_base": VNC_BASE, "viewer_base": VIEWER_BASE, "max_slots": 10}
PAGE = """<!doctype html><html><head><title>agentos lanes test</title></head>
<body style="background:#fff"><h1>agentos lanes</h1><button id="b">Press</button></body></html>"""


def node_config(home: Path, **kw) -> NodeConfig:
    return NodeConfig(home=home, **{**RANGES, "self_check": False, **kw})


@pytest.fixture
def home():
    d = ROOT / f"lanes-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def page_url(home) -> str:
    path = home / "page.html"
    path.write_text(PAGE)
    return path.as_uri()


@pytest.fixture
async def managers(home):
    made: list[LaneManagerImpl] = []

    def make(**kw) -> LaneManagerImpl:
        mgr = build_lane_manager(node_config(home, **kw))
        made.append(mgr)
        return mgr

    yield make
    for mgr in made:
        await mgr.shutdown()
    leftover = browser_pids_under(home)
    await kill_processes(leftover)
    assert not leftover, f"browsers left running after the test: {leftover}"


def env_of(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace").split("\0")
    return dict(kv.split("=", 1) for kv in raw if "=" in kv)


def screen_mean(display: str, xauthority: str | None = None) -> float:
    """Mean brightness (0..1) of the whole X screen, via ImageMagick (with the display's cookie)."""
    env = {**os.environ, **({"XAUTHORITY": xauthority} if xauthority else {})}
    shot = subprocess.run(["import", "-display", display, "-window", "root", "png:-"],
                          capture_output=True, timeout=20, check=True, env=env).stdout
    out = subprocess.run(["convert", "png:-", "-format", "%[fx:mean]", "info:"], input=shot,
                         capture_output=True, timeout=20, check=True).stdout
    return float(out)


# ---- displays ----------------------------------------------------------------------


async def test_display_slots_start_bind_localhost_and_release(home):
    dm = DisplayManagerImpl(node_config(home))
    await dm.sweep()
    try:
        v1 = await dm.acquire("test/one")
        v2 = await dm.acquire("test/two")
        assert await dm.acquire("test/one") == v1  # idempotent
        assert v1.display != v2.display
        for v in (v1, v2):
            n = int(v.display[1:])
            slot = n - DISPLAY_BASE
            assert 0 <= slot <= 9 and v.vnc_port is None and v.viewer_port is None
            assert v.vnc_socket == str(home / "vnc" / f"display-{n}.sock")
            assert Path(f"/tmp/.X11-unix/X{n}").exists()
            assert VIEWER_BASE + slot not in listening_ports()  # no websockify any more
        assert stat.S_IMODE((home / "vnc").stat().st_mode) == 0o700
        reader, writer = await asyncio.open_unix_connection(v1.vnc_socket)
        assert (await asyncio.wait_for(reader.read(12), 5)).startswith(b"RFB ")
        writer.close()
        procs = [p.pid for p in dm._slots["test/one"].procs]
        assert len(procs) == 2 and all(pid_alive(p) for p in procs)  # Xvfb and x11vnc only
        assert tcp_ports_of(procs[1]) == set(), "x11vnc listens on TCP: any local user could drive the lane"

        await dm.release("test/one")
        n1 = int(v1.display[1:])
        assert dm.get("test/one") is None
        assert not Path(f"/tmp/.X11-unix/X{n1}").exists() and not Path(f"/tmp/.X{n1}-lock").exists()
        assert not Path(v1.vnc_socket).exists()
        assert not any(pid_alive(p) for p in procs)
        assert Path(v2.vnc_socket).is_socket()  # the other slot is untouched
    finally:
        await dm.release_all()
    assert not dm._slots


# ---- browsers ----------------------------------------------------------------------


async def test_real_lane_pins_then_reuses_fingerprint(managers, home, page_url):
    mgr = managers(self_check=True)  # no proxy: the self-check records the node's own IP
    await mgr.start()
    lane_id = "test/pin"
    await mgr.create(LaneSpec(id=lane_id))
    cfg = mgr.config
    fp_path, profile = cfg.fingerprint_path(lane_id), cfg.profile_dir(lane_id)
    assert not fp_path.exists()

    info = await mgr.open(lane_id)
    assert info.state == LaneState.WARM and info.viewer is not None
    assert info.exit_ip, "the self-check should record the node's exit IP"
    assert fp_path.exists() and stat.S_IMODE(fp_path.stat().st_mode) == 0o600
    stored = json.loads(fp_path.read_text())
    assert stored[PIN_META]["version"] == PIN_VERSION
    pinned = {k: v for k, v in stored.items() if k != PIN_META}
    pid1 = mgr._lanes[lane_id].session.pid
    # The display is remade at the pinned window size on a first launch, so the warm browser is
    # already a relaunch with the pin. It carries exactly the pinned config: nothing Camoufox drew
    # again on this launch, nothing missing (only the launcher keys, such as the addon paths, are
    # not part of the pin).
    running = read_browser_config(pid1)
    assert pid1 and {k: v for k, v in running.items() if k not in LAUNCHER_KEYS} == pinned
    assert env_of(pid1)["DISPLAY"] == info.viewer.display

    async with mgr.using(lane_id) as page:
        assert mgr.get(lane_id).state == LaneState.ACTIVE
        await page.goto(page_url)
        assert await page.title() == "agentos lanes test"
        assert await page.evaluate("navigator.userAgent") == pinned["navigator.userAgent"]
        await page.evaluate("localStorage.setItem('agentos', 'kept')")
    assert mgr.get(lane_id).state == LaneState.WARM
    assert await asyncio.to_thread(screen_mean, info.viewer.display, info.viewer.xauthority) > 0.2, "browser not drawn on the display"

    await mgr.close(lane_id)
    assert mgr.get(lane_id).state == LaneState.COLD
    assert browser_pids(profile, main_only=False) == []
    assert not pid_alive(pid1)

    info = await mgr.open(lane_id)
    assert info.state == LaneState.WARM
    pid2 = mgr._lanes[lane_id].session.pid
    assert pid2 and pid2 != pid1
    relaunched = {k: v for k, v in read_browser_config(pid2).items() if k not in LAUNCHER_KEYS}
    changed = sorted(k for k in pinned.keys() | relaunched.keys() if relaunched.get(k) != pinned.get(k))
    assert changed == [], f"fingerprint keys changed on relaunch: {changed}"
    assert json.loads(fp_path.read_text()) == stored  # the pin itself is never rewritten
    async with mgr.using(lane_id) as page:
        await page.goto(page_url)
        assert await page.evaluate("navigator.userAgent") == pinned["navigator.userAgent"]
        assert await page.evaluate("navigator.platform") == pinned["navigator.platform"]
        assert await page.evaluate("localStorage.getItem('agentos')") == "kept"  # same profile

    await mgr.close(lane_id)
    assert browser_pids(profile, main_only=False) == []


async def test_cap_evicts_least_recently_used_real_lane(managers, home, page_url):
    mgr = managers(max_warm_lanes=2)
    await mgr.start()
    ids = ["test/one", "test/two", "test/three"]
    for lane_id in ids:
        await mgr.create(LaneSpec(id=lane_id))

    await mgr.open("test/one")
    await mgr.open("test/two")
    async with mgr.using("test/one") as page:  # two is now the least recently used
        await page.goto(page_url)
    two_display = mgr.view("test/two").display
    info = await mgr.open("test/three")
    assert info.state == LaneState.WARM

    states = {i.spec.id: i.state for i in mgr.list()}
    assert states == {"test/one": LaneState.WARM, "test/two": LaneState.COLD, "test/three": LaneState.WARM}
    assert len(browser_pids_under(home / "lanes")) == 2
    assert browser_pids(mgr.config.profile_dir("test/two"), main_only=False) == []
    assert info.viewer.display == two_display  # the freed slot was reused
    assert all(Path(v.vnc_socket).is_socket() for v in (mgr.view("test/one"), info.viewer))

    await mgr.shutdown()
    assert browser_pids_under(home / "lanes") == []
    assert {i.state for i in mgr.list()} == {LaneState.COLD}


async def test_close_forces_a_hung_browser(managers, home):
    """When the graceful close hangs, close() kills the browser's process group."""
    mgr = managers()
    await mgr.create(LaneSpec(id="test/hung"))
    await mgr.open("test/hung")
    session = mgr._lanes["test/hung"].session
    pid = session.pid
    assert os.getpgid(pid) == pid  # Playwright starts the browser as its own group leader
    group = [p for p, _ in iter_processes() if _pgid(p) == pid]
    assert len(group) >= 2  # the main process and at least one content process

    async def hang(*_a, **_k):
        await asyncio.sleep(3600)

    session._close_timeout = 1.0
    session._ctx.close = hang
    await mgr.close("test/hung")
    assert mgr.get("test/hung").state == LaneState.COLD
    assert not any(pid_alive(p) for p in group)
    assert browser_pids(mgr.config.profile_dir("test/hung"), main_only=False) == []


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None


@pytest.fixture
def orphans(home):
    """What a crashed daemon leaves behind: a browser on a lane profile and a display in
    our range. The browser is python3 under the camoufox-bin name, so no RAM is spent."""
    profile = home / "lanes" / "test" / "orphan" / "profile"
    profile.mkdir(parents=True)
    fake_bin = home / "bin" / "camoufox-bin"
    fake_bin.parent.mkdir()
    fake_bin.symlink_to(os.path.realpath(sys.executable))
    browser = subprocess.Popen([str(fake_bin), "-c", "import time; time.sleep(120)", "-profile", str(profile)],
                               start_new_session=True)
    xvfb = subprocess.Popen(["Xvfb", f":{DISPLAY_BASE + 9}", "-screen", "0", "640x480x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    yield browser, xvfb
    for p in (browser, xvfb):
        if p.poll() is None:
            p.kill()
        p.wait()


async def test_start_sweeps_orphans_from_a_previous_run(managers, home, orphans):
    browser, xvfb = orphans
    last = DISPLAY_BASE + 9
    for _ in range(50):
        if browser_pids_under(home / "lanes") and Path(f"/tmp/.X11-unix/X{last}").exists():
            break
        await asyncio.sleep(0.1)
    assert browser_pids_under(home / "lanes") == [browser.pid]
    assert Path(f"/tmp/.X{last}-lock").exists()

    mgr = managers()
    await mgr.start()
    for _ in range(100):
        if browser.poll() is not None and xvfb.poll() is not None:
            break
        await asyncio.sleep(0.1)
    assert browser.poll() is not None and xvfb.poll() is not None
    assert browser_pids_under(home / "lanes") == []
    assert not Path(f"/tmp/.X{last}-lock").exists()
