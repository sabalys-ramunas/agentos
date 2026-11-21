"""Which step of a browser launch on an environment's machine hung or failed (field report 2026-11-16:
browser_navigate failed twice with only "could not get the browser ... within 210 s", while the machine's
log showed its browser server up in 1.4 s). Each step (the ctl "browser" start, the connect through the
tunnel, the new context, the first page) has its own limit and time; the error and the log say which one
failed and how long each took, and a kernel's oom kill during the launch is said first. The machine and
Playwright are fakes, as in test_environments_vmbrowser."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from test_environments_vmbrowser import _create, routed  # noqa: F401 - the fixture

from openajar.env.models import ExecResult
from openajar.environments import vmbrowser

LANE = "env/e1234567890-browser"
# a real kernel's lines (openajar.env.oom), the kill shifted to just now by the probe's own clock
OOM = """N 5000.00 1794814685
M 3870000
[4999.000000] oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,mems_allowed=0,global_oom,task_memcg=/user.slice/user-0.slice/session-9.scope,task=camoufox-bin,pid=8123,uid=0
[4999.000100] Out of memory: Killed process 8123 (camoufox-bin) total-vm:9307440kB, anon-rss:2900000kB, file-rss:1172kB, shmem-rss:0kB, UID:0 pgtables:9636kB oom_score_adj:0
"""


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setitem(vmbrowser.STEP_LIMIT_S, "connect", 0.3)
    monkeypatch.setitem(vmbrowser.STEP_LIMIT_S, "context", 0.3)


async def test_a_connect_that_hangs_says_so_with_the_time_of_each_step(routed, quick, caplog):  # noqa: F811
    async def hangs(url, *, timeout):
        await asyncio.sleep(30)

    routed.pw.connect = hangs
    mgr = routed.mgr
    await mgr.start()
    try:
        await _create(mgr, LANE)
        with caplog.at_level(logging.WARNING, logger="openajar.environments"), \
                pytest.raises(Exception) as err:
            await mgr.open(LANE)
        text = str(getattr(err.value, "message", err.value))
        assert "connecting to it through the tunnel failed after 0.3 s (no answer within 0.3 s)" in text
        assert "Before it: browser server started in" in text
        assert "\u2014" not in text
        assert any("connecting to it through the tunnel" in r.getMessage() for r in caplog.records)
        assert mgr.get(LANE).state.value == "cold"
        assert [a["action"] for a in routed.machine.asked].count("stop") >= 1  # the machine's browser stopped
    finally:
        await mgr.shutdown()


async def test_a_context_that_hangs_names_its_step_and_a_stuck_call_hears_where_it_is(routed, quick):  # noqa: F811
    entered = asyncio.Event()
    real_connect = routed.pw.connect

    async def connect(url, *, timeout):
        browser = await real_connect(url, timeout=timeout)

        async def slow_context(**kw):
            entered.set()
            await asyncio.sleep(30)

        browser.new_context = slow_context
        return browser

    routed.pw.connect = connect
    mgr = routed.mgr
    await mgr.start()
    try:
        await _create(mgr, LANE)
        opening = asyncio.create_task(mgr.open(LANE))
        await asyncio.wait_for(entered.wait(), 5)
        hint = routed.vm.launch_hint(LANE)
        assert hint.startswith("The browser on the machine was opening: browser server started in")
        assert "connected through the tunnel in" in hint and "then opening its browser context for" in hint
        with pytest.raises(Exception, match="opening its browser context failed after"):
            await opening
        assert routed.vm.launch_hint(LANE) is None  # a failed launch leaves no hint behind
    finally:
        await mgr.shutdown()


async def test_a_launch_stopped_by_the_lanes_wall_keeps_its_steps_for_the_error(routed, quick):  # noqa: F811
    async def hangs(url, *, timeout):
        await asyncio.sleep(30)

    routed.pw.connect = hangs
    vmbrowser.STEP_LIMIT_S["connect"] = 30  # the wall comes first here (the fixture puts it back)
    mgr = routed.mgr
    await mgr.start()
    try:
        await _create(mgr, LANE)
        opening = asyncio.create_task(mgr.open(LANE))
        for _ in range(100):
            if routed.vm.launch_hint(LANE):
                break
            await asyncio.sleep(0.02)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        hint = routed.vm.launch_hint(LANE)
        assert hint and "was still opening when the call was stopped" in hint and "connecting" in hint
    finally:
        await mgr.shutdown()


async def test_a_kernel_oom_kill_during_the_launch_is_said_first(routed, quick):  # noqa: F811
    async def hangs(url, *, timeout):
        await asyncio.sleep(30)

    machine = routed.machine
    plain = machine.exec

    async def exec_(lane_id, cmd, *, timeout_s=None, stdin=None, **kw):
        if "dmesg" in cmd:
            return ExecResult(exit_code=0, stdout=OOM, stderr="", duration_s=0.01)
        return await plain(lane_id, cmd, timeout_s=timeout_s, stdin=stdin, **kw)

    machine.exec = exec_
    routed.pw.connect = hangs
    mgr = routed.mgr
    await mgr.start()
    try:
        await _create(mgr, LANE)
        with pytest.raises(Exception) as err:
            await mgr.open(LANE)
        text = str(getattr(err.value, "message", err.value))
        head = "env/e1234567890-browser: the browser on environment e1234567890's machine did not open: "
        assert text.startswith(head + "The kernel killed pid 8123 (camoufox-bin, 2.8 GB)")
        assert "out of memory" in text and 'size="medium"' in text  # half the machine or more: a bigger one
        assert "(Connecting to it through the tunnel failed after" in text
    finally:
        await mgr.shutdown()


async def test_a_launch_that_works_logs_the_time_of_each_step(routed, caplog):  # noqa: F811
    mgr = routed.mgr
    await mgr.start()
    try:
        await _create(mgr, LANE)
        with caplog.at_level(logging.INFO, logger="openajar.environments"):
            await mgr.open(LANE)
        line = next(r.getMessage() for r in caplog.records if "runs on environment" in r.getMessage())
        for done in ("browser server started in", "connected through the tunnel in", "context opened in",
                     "first page opened in", "s in all"):
            assert done in line
        assert routed.vm.launch_hint(LANE) is None
        browser = next(a for a in routed.machine.asked if a["action"] == "browser")
        assert json.dumps(browser)  # the start went through the ctl as before
    finally:
        await mgr.shutdown()


def test_the_start_step_fits_the_lanes_wall():
    from openajar.node.deadlines import WALL_S

    assert sum(vmbrowser.STEP_LIMIT_S.values()) < WALL_S
    assert set(vmbrowser.STEP_LIMIT_S) == set(vmbrowser.LAUNCH_STEPS) == set(vmbrowser.STEP_DOING)
