"""Shared lanes on a real node: one real Camoufox lane that two agents and a flow run want at once.

- A claim on a cold lane warms it once; the holder's multi-step job (navigate, focus, type, click,
  read back) is never interleaved by the other agent's navigate, which waits in the queue and runs
  after the release. The other agent's snapshot and screenshot run during the claim and leave the
  holder's page as it was.
- Reads share the lane: a long screenshot and a snapshot overlap in time.
- A flow run claims its lane implicitly (the runner's log context names the run); the brain's read
  passes, its act waits for the end of the run, and a claim over the run fails busy naming it.

Run on the node:  AGENTOS_TEST_SLOT=46 scripts/node-test.sh sharedlanes tests/node/test_sharedlanes_node.py -q
Uses the slot's display and port ranges (AGENTOS_TEST_*), a throwaway profile under .node-home/ and a
local page (file://), never a real site or account.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import pytest

from agentos.core import logs
from agentos.core.config import NodeConfig
from agentos.core.errors import LaneBusy
from agentos.core.models import LaneSpec, LaneState, Principal
from agentos.node import human
from agentos.node.camoufox.engine import browser_pids_under
from agentos.node.claims import READ, Holder
from agentos.node.display import kill_processes
from agentos.node.flow_driver import _using
from agentos.node.interaction import PlaywrightInteraction
from agentos.node.lanes import build_lane_manager

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb and Camoufox"),
]

ROOT = Path(os.environ.get("AGENTOS_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
RANGES = {"display_base": int(os.environ.get("AGENTOS_TEST_DISPLAY_BASE", "460")),
          "vnc_base": int(os.environ.get("AGENTOS_TEST_VNC_BASE", "5460")),
          "viewer_base": int(os.environ.get("AGENTOS_TEST_VIEWER_BASE", "6460")), "max_slots": 10}
LANE = "shared/one"

COMPOSER = """<!doctype html><html><head><title>composer</title></head><body style="background:#fff">
<h1>New post</h1><label>Caption <textarea id="t" style="width:400px;height:80px"></textarea></label>
<button id="b" style="width:200px;height:60px" onclick="document.getElementById('s').textContent='Posted: '
 + document.getElementById('t').value">Post</button><p id="s">Not posted</p>
<div style="height:2000px"></div></body></html>"""
ELSEWHERE = """<!doctype html><html><head><title>elsewhere</title></head><body><h1>Another page</h1></body></html>"""


def agent(name: str, kind: str = "agent") -> Holder:
    return Holder.for_principal(Principal(kind=kind, id=name, grant_id=f"g_{name}"), session="node-test")


A, B, BRAIN = agent("claude-code"), agent("codex"), agent("brain-task-1", kind="brain")


@pytest.fixture
def home():
    d = ROOT / f"sharedlanes-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
async def node(home):
    config = NodeConfig(home=home, name="sharedlanes-node", self_check=False, queue_timeout=60, **RANGES)
    mgr = build_lane_manager(config)
    await mgr.start()
    await mgr.create(LaneSpec(id=LANE))
    pages = {}
    for name, html in (("composer", COMPOSER), ("elsewhere", ELSEWHERE)):
        path = home / f"{name}.html"
        path.write_text(html)
        pages[name] = path.as_uri()
    try:
        yield mgr, pages, home
    finally:
        await mgr.shutdown()
        leftover = browser_pids_under(home)
        await kill_processes(leftover)
        assert not leftover, f"browsers left running after the test: {leftover}"


def _pid(mgr) -> int:
    return mgr._lanes[LANE].session.pid


async def test_a_claimed_multi_step_post_is_never_interleaved(node):
    mgr, pages, _ = node
    interaction = PlaywrightInteraction()
    assert mgr.get(LANE).state == LaneState.COLD
    claimed = await mgr.claim(LANE, A, purpose="posting a caption", ttl_s=120)
    assert claimed["state"] == "warm"
    pid = _pid(mgr)
    log: list[str] = []

    async def other_navigates():
        async with mgr.using(LANE, holder=B) as page:
            log.append("B navigate")
            await page.goto(pages["elsewhere"])

    async def step(name: str, fn):
        async with mgr.using(LANE, holder=A) as page:
            log.append(f"A {name}")
            await fn(page)

    await step("navigate", lambda page: page.goto(pages["composer"]))
    intruder = asyncio.create_task(other_navigates())
    await asyncio.sleep(0.2)
    assert mgr.queue_info(LANE)["queue"][0]["kept_out_by_claim"]

    async with mgr.using(LANE, holder=B, mode=READ) as page:  # the other agent reads during the claim
        snap = await interaction.snapshot(page)
        png = await interaction.screenshot(page)
        log.append("B read")
    assert "New post" in snap.text and png[:8] == b"\x89PNG\r\n\x1a\n"

    await step("focus", lambda page: page.focus("#t"))
    await step("type", lambda page: human.type_text(page, "hello from agentos"))
    await step("click", lambda page: human.click(page, page.locator("#b")))

    async def read_back(page):
        assert await page.locator("#s").text_content() == "Posted: hello from agentos"
        assert page.url == pages["composer"]

    await step("read back", read_back)
    assert not intruder.done()
    await mgr.release(LANE, A)
    await asyncio.wait_for(intruder, 30)
    assert log == ["A navigate", "B read", "A focus", "A type", "A click", "A read back", "B navigate"]
    assert _pid(mgr) == pid, "the claim warmed the lane once; nothing restarted it"
    async with mgr.using(LANE, holder=B, mode=READ) as page:
        assert page.url == pages["elsewhere"]


async def test_reads_share_a_real_lane(node):
    mgr, pages, _ = node
    interaction = PlaywrightInteraction()
    async with mgr.using(LANE, holder=A) as page:
        await page.goto(pages["composer"])
    spans: dict[str, tuple[float, float]] = {}

    async def read(name: str, holder: Holder, fn):
        async with mgr.using(LANE, holder=holder, mode=READ) as page:
            t0 = time.monotonic()
            await fn(page)
            await asyncio.sleep(0.3)  # long enough that a serialized second read could not overlap
            spans[name] = (t0, time.monotonic())

    await asyncio.gather(read("screenshot", A, lambda page: interaction.screenshot(page)),
                         read("snapshot", B, lambda page: interaction.snapshot(page)))
    (a0, a1), (b0, b1) = spans["screenshot"], spans["snapshot"]
    assert a0 < b1 and b0 < a1, f"the two reads did not overlap: {spans}"


async def test_a_flow_run_and_the_node_brain_on_one_lane(node):
    mgr, pages, _ = node
    gate, log = asyncio.Event(), []
    interaction = PlaywrightInteraction()

    async def flow_run():
        with logs.log_context(run="run_node_1"):
            async with _using(mgr, LANE) as page:  # what the flow driver enters for a run
                await page.goto(pages["composer"])
                await page.focus("#t")
                log.append("flow started")
                await gate.wait()
                await human.type_text(page, "flow text")
                log.append("flow done")

    run = asyncio.create_task(flow_run())
    while "flow started" not in log:
        await asyncio.sleep(0.05)
    claim = mgr.queue_info(LANE)["claim"]
    assert claim["implicit"] and claim["run_id"] == "run_node_1"
    async with mgr.using(LANE, holder=BRAIN, mode=READ) as page:
        assert "New post" in (await interaction.snapshot(page)).text
        log.append("brain read")
    with pytest.raises(LaneBusy, match="flow run run_node_1 has claimed it"):
        await mgr.claim(LANE, A, purpose="post", wait_s=0.5)

    async def brain_acts():
        async with mgr.using(LANE, holder=BRAIN) as page:
            log.append("brain act")
            assert await page.locator("#t").input_value() == "flow text"  # the run finished first

    brain = asyncio.create_task(brain_acts())
    await asyncio.sleep(0.3)
    assert not brain.done()
    gate.set()
    await asyncio.wait_for(asyncio.gather(run, brain), 60)
    assert log == ["flow started", "brain read", "flow done", "brain act"]
    assert mgr.queue_info(LANE)["claim"] is None
