"""Two lanes on one device (AUD-02) launch as the same computer, as a page reads it,
while each runs its own browser, profile and display at the same time; a lane of its own is
another computer.

Run on the node or in the Docker node (docs/design/stealth.md section 6):
  AGENTOS_TEST_SLOT=32 scripts/node-test.sh devices tests/node/test_devices_node.py -q
Throwaway lanes only, under an AGENTOS_HOME in .node-home/ of this checkout, with no proxy and no
account; the fixture site (tests/fixtures/site/fingerprint.html) on the slot's last viewer port.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest
from test_fingerprint_pin_node import comparable, differences, keep, site  # noqa: F401 - the fixture

from agentos.core.config import NodeConfig
from agentos.core.models import LaneSpec
from agentos.node.display import DisplayManagerImpl, kill_processes
from agentos.node.engine_camoufox import (
    LAUNCHER_KEYS,
    PIN_META,
    CamoufoxEngine,
    browser_pids_under,
    read_browser_config,
)
from agentos.node.lanes import LaneManagerImpl
from agentos.node.store import LaneStore

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb, x11vnc and Camoufox"),
]

ROOT = Path(os.environ.get("AGENTOS_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
DISPLAY_BASE = int(os.environ.get("AGENTOS_TEST_DISPLAY_BASE", "310"))
VNC_BASE = int(os.environ.get("AGENTOS_TEST_VNC_BASE", "5310"))
VIEWER_BASE = int(os.environ.get("AGENTOS_TEST_VIEWER_BASE", "6310"))


@pytest.fixture
async def mgr():
    home = ROOT / f"devices-{uuid.uuid4().hex[:8]}"
    home.mkdir(parents=True)
    config = NodeConfig(home=home, display_base=DISPLAY_BASE, vnc_base=VNC_BASE, viewer_base=VIEWER_BASE,
                        max_slots=9, max_warm_lanes=3, self_check=False, memory_floor_mb=256,
                        disk_floor_bytes=256 * 1024**2)
    displays = DisplayManagerImpl(config)
    engine = CamoufoxEngine(identity_os=["linux"])
    engine.allow_private_network = True  # only so these lanes can reach the fixture server
    manager = LaneManagerImpl(config, LaneStore(config.db_path), engine, displays, dict)
    await manager.start()
    yield manager
    await manager.shutdown()
    leftover = browser_pids_under(home)
    await kill_processes(leftover)
    shutil.rmtree(home, ignore_errors=True)
    assert not leftover, f"browsers left running after the test: {leftover}"


async def dump_of(mgr: LaneManagerImpl, lane_id: str, url: str) -> dict:
    async with mgr.using(lane_id) as page:
        await page.goto(url + "/fingerprint.html")
        await page.wait_for_selector("#dump[data-state=done]", state="attached", timeout=30_000)
        return json.loads(await page.text_content("#dump"))


async def test_two_lanes_on_one_device_are_one_computer_in_two_browsers(mgr, site):  # noqa: F811
    await mgr.create_device("biz-a-pc")
    for name in ("instagram", "facebook"):
        await mgr.create(LaneSpec(id=f"biz-a/{name}", device="biz-a-pc"))
    await mgr.create(LaneSpec(id="personal/reddit"))
    # warm all three at once: the device's first launch pins it, the second lane waits for it
    await asyncio.gather(*(mgr.open(i) for i in ("biz-a/instagram", "biz-a/facebook", "personal/reddit")))

    sessions = {i: mgr._lanes[i].session for i in ("biz-a/instagram", "biz-a/facebook", "personal/reddit")}
    pids = {i: s.pid for i, s in sessions.items()}
    assert len(set(pids.values())) == 3, pids  # three browsers
    displays = {i: mgr._lanes[i].viewer.display for i in sessions}
    assert len(set(displays.values())) == 3, displays
    cmdlines = {i: Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace") for i, pid in pids.items()}
    for lane_id, line in cmdlines.items():
        assert str(mgr.config.profile_dir(lane_id)) in line  # its own profile

    running = {i: {k: v for k, v in read_browser_config(pid).items() if k not in LAUNCHER_KEYS}
               for i, pid in pids.items()}
    assert running["biz-a/instagram"] == running["biz-a/facebook"]
    assert running["biz-a/instagram"] != running["personal/reddit"]
    device_pin = json.loads(mgr.config.device_pin_path("biz-a-pc").read_text())
    pinned = {k: v for k, v in device_pin.items() if k != PIN_META}
    assert running["biz-a/instagram"] == pinned
    for lane_id in ("biz-a/instagram", "biz-a/facebook"):
        assert json.loads(mgr.config.fingerprint_path(lane_id).read_text()) == device_pin  # the copy

    dumps = {i: await dump_of(mgr, i, site) for i in sessions}
    for lane_id, dump in dumps.items():
        keep(f"device-{lane_id.replace('/', '-')}", dump)
    canvas_noise = bool(device_pin[PIN_META]["canvas_noise"])
    same = differences(comparable(dumps["biz-a/instagram"], canvas_noise),
                       comparable(dumps["biz-a/facebook"], canvas_noise))
    assert same == [], "the two lanes of one device differ:\n" + "\n".join(same)
    for key in ("userAgent",):
        assert dumps["biz-a/instagram"]["navigator"][key] == pinned["navigator.userAgent"]
    a, b = dumps["biz-a/instagram"], dumps["biz-a/facebook"]
    assert (a["screen"]["width"], a["screen"]["height"]) == (b["screen"]["width"], b["screen"]["height"])
    assert (a["window"]["screenX"], a["window"]["screenY"]) == (pinned["window.screenX"], pinned["window.screenY"])
    assert a["intl"]["timeZone"] == b["intl"]["timeZone"] == pinned["timezone"]
    assert a["webgl"]["renderer"] == b["webgl"]["renderer"] == pinned["webGl:renderer"]
    other = differences(comparable(a, canvas_noise), comparable(dumps["personal/reddit"], canvas_noise))
    assert other, "a lane of its own shows the same computer as the device"

    # cookies stay apart: a cookie set in one lane is not in the other
    async with mgr.using("biz-a/instagram") as page:
        await page.context.add_cookies([{"name": "only_instagram", "value": "1", "url": site}])
    async with mgr.using("biz-a/facebook") as page:
        names = {c["name"] for c in await page.context.cookies(site)}
    assert "only_instagram" not in names
