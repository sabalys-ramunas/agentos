"""A freeze on a real node: a real Camoufox lane with agent work in flight (a long page wait, human
typing, a human click), frozen through the real freeze, policy and lane services. Measures the time
from the freeze call to the last in-flight agent action being stopped, and checks the browser is
left as it was (warm, same process, page state kept) with a human still able to look.

Run on the node:  OPENAJAR_TEST_SLOT=43 scripts/node-test.sh freeze tests/node/test_freeze_node.py -q -s
Uses the slot's display and port ranges (OPENAJAR_TEST_*) and an OPENAJAR_HOME under .node-home/.
Set OPENAJAR_TEST_FREEZE_ROUNDS to change how many freezes are measured (default 5).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import statistics
import sys
import time
import uuid
from pathlib import Path

import pytest

from openajar.core.config import NodeConfig
from openajar.core.errors import Frozen
from openajar.core.models import Effect, LaneSpec, LaneState
from openajar.core.runtime import Runtime
from openajar.freeze.service import FreezeService
from openajar.node import human
from openajar.node.camoufox.engine import browser_pids_under
from openajar.node.display import kill_processes
from openajar.node.lanes import build_lane_manager
from openajar.policy.service import PolicyService

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb and Camoufox"),
]

ROOT = Path(os.environ.get("OPENAJAR_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
RANGES = {"display_base": int(os.environ.get("OPENAJAR_TEST_DISPLAY_BASE", "430")),
          "vnc_base": int(os.environ.get("OPENAJAR_TEST_VNC_BASE", "5430")),
          "viewer_base": int(os.environ.get("OPENAJAR_TEST_VIEWER_BASE", "6430")), "max_slots": 10}
ROUNDS = int(os.environ.get("OPENAJAR_TEST_FREEZE_ROUNDS", "5"))
LANE = "freeze/one"
TARGET_MS = 2000

PAGE = """<!doctype html><html><head><title>openajar freeze test</title></head><body style="background:#fff">
<h1>freeze</h1><textarea id="t" style="width:400px;height:80px"></textarea>
<button id="b" style="width:200px;height:60px" onclick="this.dataset.clicks=+(this.dataset.clicks||0)+1"
 onmousedown="this.dataset.downs=+(this.dataset.downs||0)+1" onmouseup="this.dataset.ups=+(this.dataset.ups||0)+1"
 >Press</button>
<div id="d" style="height:3000px"></div></body></html>"""


@pytest.fixture
def home():
    d = ROOT / f"freeze-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
async def node(home):
    config = NodeConfig(home=home, name="freeze-node", mcp_token="freeze-node-test-token", self_check=False,
                        **RANGES)
    mgr = build_lane_manager(config)
    rt = Runtime(config=config, lanes=mgr)
    policy = PolicyService(home, node_config=config, runtime=rt)
    for name, svc in (("policy", policy), ("approvals", policy.approvals), ("traces", policy.traces),
                      ("notifier", policy.notifier)):
        rt.register(name, svc)
    freeze = FreezeService(rt, stop_wait_s=5.0)
    rt.register("freeze", freeze)
    await mgr.start()
    await mgr.create(LaneSpec(id=LANE))
    page_path = home / "page.html"
    page_path.write_text(PAGE)
    try:
        yield mgr, policy, freeze, page_path.as_uri()
    finally:
        await freeze.stop()
        await mgr.shutdown()
        policy.close()
        leftover = browser_pids_under(home)
        await kill_processes(leftover)
        assert not leftover, f"browsers left running after the test: {leftover}"


async def _typing(mgr, url: str, ready: asyncio.Event):
    async with mgr.using(LANE, principal_kind="agent") as page:
        if page.url != url:
            await page.goto(url)
        await page.focus("#t")
        ready.set()
        await human.type_text(page, "an agent typing a long reply that the freeze cuts off between keys " * 5)


async def _clicking(mgr, url: str, ready: asyncio.Event):
    async with mgr.using(LANE, principal_kind="agent") as page:
        if page.url != url:
            await page.goto(url)
        await human.click(page, page.locator("#b"))  # one whole click first
        ready.set()
        for _ in range(50):
            await human.click(page, page.locator("#b"))


async def _measure(mgr, freeze, make, url: str, reason: str) -> tuple[float, float, dict]:
    ready = asyncio.Event()
    t = asyncio.create_task(make(mgr, url, ready))
    await asyncio.wait_for(ready.wait(), 60)
    await asyncio.sleep(0.4 + 0.3 * (hash(reason) % 4))  # land mid-wait, mid-word or mid-click
    t0 = time.monotonic()
    report = await freeze.freeze("lane", LANE, reason=reason, by="owner")
    returned = (time.monotonic() - t0) * 1000
    with pytest.raises(Frozen):
        await t
    stopped = (time.monotonic() - t0) * 1000
    return returned, stopped, report


async def test_freeze_stops_real_agent_work_fast_and_keeps_the_browser(node):
    mgr, policy, freeze, url = node
    _g, token = policy.issue(subject={"id": "agent-1"}, lanes=[LANE], ceiling=Effect.COMMIT)

    async def waiting(mgr, url, ready):
        async with mgr.using(LANE, principal_kind="agent") as page:
            if page.url != url:
                await page.goto(url)
            ready.set()
            await page.wait_for_timeout(60_000)

    kinds = {"page wait": waiting, "typing": _typing, "click": _clicking}
    results: dict[str, list[float]] = {k: [] for k in kinds}
    pid = None
    for round_no in range(ROUNDS):
        for kind, make in kinds.items():
            _returned, stopped, report = await _measure(mgr, freeze, make, url, f"round {round_no} {kind}")
            assert report["stopped"]["calls"] == 1 and report["stopped"]["still_running"] == 0, report
            assert report["lanes_warm"] == [LANE]
            results[kind].append(max(stopped, report["stopped"]["ms"]))
            info = mgr.get(LANE)
            assert info.state in (LaneState.WARM, LaneState.ACTIVE)
            session = mgr._lanes[LANE].session
            pid = pid or session.pid
            assert session.pid == pid and session.alive  # the same browser, never restarted
            async with mgr.using(LANE, principal_kind="human") as page:  # a human looks
                assert await page.title() == "openajar freeze test"
            await freeze.unfreeze("lane", LANE, by="owner")

    # the page kept its state through every freeze: what the agent typed and clicked is still there
    async with mgr.using(LANE, principal_kind="human") as page:
        typed = await page.input_value("#t")
        # read through the DOM: our scripts run in Camoufox's isolated world
        clicks, downs, ups = [int(await page.get_attribute("#b", f"data-{k}") or 0) for k in ("clicks", "downs", "ups")]
    assert typed.startswith("an a") and clicks >= ROUNDS  # each round typed a few keys before the freeze
    assert downs == ups  # an interrupt never left the button pressed: a click is never cut in half
    with pytest.raises(Exception, match="revoked"):
        policy.resolve(token)  # the first freeze revoked the lane's grant, and it stayed revoked

    flat = [ms for values in results.values() for ms in values]
    lines = [(f"freeze latency on a real Camoufox lane ({ROUNDS} rounds per kind; freeze call to the last "
             f"in-flight agent action stopped):")]
    for kind, values in results.items():
        lines.append(f"  {kind:<10} median {statistics.median(values):7.1f} ms  max {max(values):7.1f} ms")
    lines.append(f"  overall    median {statistics.median(flat):7.1f} ms  max {max(flat):7.1f} ms "
                 f"(target {TARGET_MS} ms)")
    print("\n" + "\n".join(lines))
    assert max(flat) < TARGET_MS, lines


async def test_a_wedged_page_does_not_hold_the_freeze_up(node):
    mgr, _, freeze, url = node
    freeze.stop_wait_s = 1.0
    ready = asyncio.Event()

    async def wedged():
        async with mgr.using(LANE, principal_kind="agent") as page:
            await page.goto(url)
            ready.set()
            # a script that never returns: the browser is busy, the call hangs in Playwright
            await page.evaluate("() => new Promise(() => {})")

    t = asyncio.create_task(wedged())
    await asyncio.wait_for(ready.wait(), 60)
    await asyncio.sleep(0.3)
    t0 = time.monotonic()
    report = await freeze.freeze("lane", LANE, reason="wedged", by="owner")
    took = (time.monotonic() - t0) * 1000
    with pytest.raises(Frozen):
        await t
    print(f"\nwedged page: freeze returned in {took:.1f} ms, stopped {report['stopped']}")
    assert took < TARGET_MS and report["stopped"]["still_running"] == 0
    with pytest.raises(Frozen):
        async with mgr.using(LANE, principal_kind="agent"):
            pass
    assert mgr._lanes[LANE].session.alive


async def test_a_frozen_lane_survives_a_restart_of_the_lane_manager(node, home):
    mgr, _policy, freeze, url = node
    async with mgr.using(LANE, principal_kind="agent") as page:
        await page.goto(url)
    await freeze.freeze("lane", LANE, reason="before the restart", by="owner")
    await mgr.shutdown()
    config = mgr.config
    again = build_lane_manager(config)
    rt = Runtime(config=config, lanes=again)
    FreezeService(rt)
    await again.start()
    try:
        with pytest.raises(Frozen, match="before the restart"):
            async with again.using(LANE, principal_kind="agent"):
                pass
    finally:
        await again.shutdown()
