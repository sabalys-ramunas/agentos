"""A browser call never hangs past its limit, even when closing the browser hangs (the 2026-11-15 test: a
browser_navigate on an overloaded 2-CPU machine held its lane for 9 minutes, and its close took 13),
and an agent's own stuck call is recovered instead of "call again later" forever."""

from __future__ import annotations

import asyncio

import pytest
from test_lanes import make
from test_lanes_deadlines import LANE, agent, hang_page, lane, stuck_call

from openajar.core import progress
from openajar.core.errors import EngineError, LaneBusy
from openajar.environments.probes import load_words
from openajar.node.lanes import access as access_mod


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(access_mod, "PROBE_S", 0.2)
    monkeypatch.setattr(access_mod, "CLOSE_WAIT_S", 0.5)
    monkeypatch.setattr(access_mod, "VERDICT_WAIT_S", 0.4)


def hang_close(session) -> asyncio.Event:
    """The browser's close never ends (a dead connection that ignores its cancellation)."""
    release = asyncio.Event()
    original = session.close

    async def stuck_close():
        await release.wait()
        await original()

    session.close = stuck_close
    return release


async def test_a_stuck_call_ends_and_frees_the_lane_even_when_the_close_hangs(tmp_path):
    mgr, engine = await lane(tmp_path, queue_timeout=5.0)
    session = engine.sessions[-1]
    hang_page(session)
    release = hang_close(session)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(EngineError) as err:
        await asyncio.wait_for(stuck_call(mgr, deadline_s=0.2), 3)
    assert loop.time() - t0 < 2  # the call does not wait for the close
    assert "openajar is closing the browser" in err.value.message
    await asyncio.sleep(0.7)  # CLOSE_WAIT_S later the lane is free, while the close still hangs
    assert not mgr._lanes[LANE].access.busy()
    release.set()
    await mgr.shutdown()


async def test_an_agents_own_stuck_call_is_recovered_and_it_is_told(tmp_path, monkeypatch):
    monkeypatch.setattr(access_mod, "CLOSE_WAIT_S", 1.0)  # the stuck call ignores its interrupt past this
    mgr, engine = await lane(tmp_path, queue_timeout=0.3)
    first = engine.sessions[-1]
    inside, done = asyncio.Event(), asyncio.Event()

    async def ignores_its_interrupt():
        async with mgr.using(LANE, holder=agent("a"), deadline_s=0.3, tool="browser_navigate"):
            inside.set()
            while not done.is_set():
                try:
                    await done.wait()
                except asyncio.CancelledError:
                    continue  # stuck on a dead connection

    stuck = asyncio.ensure_future(ignores_its_interrupt())
    await inside.wait()
    await asyncio.sleep(0.5)  # past its deadline; the page answers, so the browser stays
    state, token = progress.begin("browser_navigate")
    try:
        async with mgr.using(LANE, holder=agent("a"), deadline_s=5, tool="browser_navigate") as page:
            assert page is engine.sessions[-1]._page and engine.sessions[-1] is not first
    finally:
        progress.end(token)
    assert state.notices and "your own browser_navigate from" in state.notices[0]
    done.set()
    with __import__("contextlib").suppress(Exception):
        await asyncio.wait_for(stuck, 2)
    await mgr.shutdown()


async def test_busy_says_when_the_holder_is_the_callers_own_call(tmp_path):
    mgr, _ = await lane(tmp_path, queue_timeout=0.2)
    task = asyncio.ensure_future(stuck_call(mgr, deadline_s=30, who="a"))
    await asyncio.sleep(0.05)
    with pytest.raises(LaneBusy) as own:
        async with mgr.using(LANE, holder=agent("a"), deadline_s=5, tool="browser_navigate"):
            pass
    assert "your own browser_navigate from" in own.value.message and "call again later" not in own.value.message.lower()
    with pytest.raises(LaneBusy) as other:
        async with mgr.using(LANE, holder=agent("b"), deadline_s=5, tool="browser_navigate"):
            pass
    assert "agent a is using it since" in other.value.message and "your own" not in other.value.message
    task.cancel()
    await mgr.shutdown()


async def test_a_call_that_never_gets_the_browser_stops_at_the_wall_with_the_machines_load(tmp_path, monkeypatch):
    monkeypatch.setattr(access_mod, "WALL_S", 0.3)
    mgr, _ = await lane(tmp_path, queue_timeout=30.0)

    async def overloaded(lane_id):
        return "The machine is overloaded (load 5.0 on 2 CPUs)."

    mgr.slow_hints = [overloaded]
    holder = asyncio.ensure_future(stuck_call(mgr, deadline_s=None, who="b"))  # another agent holds it
    await asyncio.sleep(0.05)
    with pytest.raises(EngineError) as err:
        await asyncio.wait_for(stuck_call(mgr, deadline_s=120, who="a"), 3)
    assert "could not get the browser" in err.value.message and "load 5.0 on 2 CPUs" in err.value.message
    holder.cancel()
    await mgr.shutdown()


def test_the_load_words():
    out = "5.12 4.80 3.90 3/412 999\n2\n3900000\n200000\n"
    said = load_words(out, "small", "medium")
    assert "overloaded" in said and "load 5.1 on 2 CPUs" in said and 'size="medium"' in said
    calm = load_words("0.30 0.2 0.1 1/1 1\n4\n8000000\n6000000\n", "medium", "large")
    assert calm.startswith("The machine is not overloaded")
    assert load_words("garbage", None, None) is None


async def test_a_session_close_that_hangs_is_abandoned_after_its_timeout(tmp_path):
    from openajar.node.camoufox.session import CamoufoxSession

    class Ctx:
        async def close(self):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(3600)  # ignores it

    class Tracker:
        async def close(self):
            return None

    s = CamoufoxSession.__new__(CamoufoxSession)
    s._closed, s._ctx, s._close_timeout, s._pid, s.tracker = False, Ctx(), 0.1, None, Tracker()
    s._profile_dir = tmp_path
    for name, value in (("_playwright", None), ("_ctx_closed", False)):
        setattr(s, name, value)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with __import__("contextlib").suppress(Exception):
        await asyncio.wait_for(s.close(), 3)
    assert loop.time() - t0 < 2


_ = make
