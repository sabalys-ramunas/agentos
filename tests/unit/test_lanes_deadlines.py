"""Every tool call on a lane has an overall deadline (agentos.node.deadlines), and a browser that
stopped answering is closed for real (the 2026-11-10 re-test: a LinkedIn tab near its memory ceiling
hung a browser_navigate, which then held the lane for every caller until a person killed it)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from test_lanes import make

from agentos.core.errors import EngineError, LaneBusy
from agentos.core.models import LaneSpec, LaneState
from agentos.mcp import registry
from agentos.node import deadlines, interrupts
from agentos.node import tools as node_tools
from agentos.node.claims import Holder
from agentos.node.lanes import access as access_mod

LANE = "biz/linkedin"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(access_mod, "PROBE_S", 0.2)
    monkeypatch.setattr(access_mod, "CLOSE_WAIT_S", 0.5)


async def lane(tmp_path, **cfg):
    mgr, engine, *_ = make(tmp_path, **cfg)
    await mgr.create(LaneSpec(id=LANE))
    await mgr.open(LANE)
    return mgr, engine


def hang_page(session) -> None:
    """The lane's content process stops answering: every evaluate on its page never returns."""
    async def never(js, arg=None):
        await asyncio.Event().wait()
    session._page.evaluate = never


def agent(name: str) -> Holder:
    return Holder(key=name, label=f"agent {name}", kind="agent", identity=name)


async def stuck_call(mgr, *, deadline_s=None, tool="browser_navigate", who="a"):
    async with mgr.using(LANE, holder=agent(who), deadline_s=deadline_s, tool=tool):
        await asyncio.Event().wait()


async def test_a_call_that_never_returns_hits_its_deadline_and_frees_the_lane(tmp_path):
    mgr, engine = await lane(tmp_path, queue_timeout=5.0)
    task = asyncio.ensure_future(stuck_call(mgr, deadline_s=0.2))
    await asyncio.sleep(0.05)
    second = asyncio.ensure_future(mgr.using(LANE, holder=agent("b")).__aenter__())
    with pytest.raises(EngineError) as err:
        await asyncio.wait_for(task, 2)
    assert "browser_navigate" in err.value.message and "past its 0.2 s limit" in err.value.message
    assert "still answers" in err.value.message  # the page answered the probe: the browser stays
    page = await asyncio.wait_for(second, 2)  # the next caller gets the lane
    assert page is engine.sessions[-1]._page and not engine.sessions[-1].closed
    await mgr.shutdown()


async def test_a_dead_browser_is_closed_and_the_next_call_opens_it_again(tmp_path):
    mgr, engine = await lane(tmp_path)
    first = engine.sessions[-1]
    hang_page(first)
    with pytest.raises(EngineError) as err:
        await asyncio.wait_for(stuck_call(mgr, deadline_s=0.2), 3)
    assert "the browser was closed; the next call opens it again" in err.value.message
    assert first.closed and mgr.get(LANE).state == LaneState.COLD
    async with mgr.using(LANE, holder=agent("b")) as page:  # opens it fresh
        assert page is engine.sessions[-1]._page and engine.sessions[-1] is not first
    assert len(engine.launches) == 2
    await mgr.shutdown()


async def test_lane_close_closes_a_lane_with_a_stuck_call(tmp_path):
    mgr, engine = await lane(tmp_path, queue_timeout=0.3)
    session = engine.sessions[-1]
    task = asyncio.ensure_future(stuck_call(mgr, deadline_s=None))  # no deadline: only browser_close can end it
    await asyncio.sleep(0.05)
    hang_page(session)
    info = await asyncio.wait_for(mgr.close(LANE, holder=agent("b")), 5)
    assert info.state == LaneState.COLD and session.closed
    note = mgr.take_close_note(LANE)
    assert note and note["stuck_calls"][0]["tool"] == "browser_navigate"
    with pytest.raises(EngineError, match="closed while this call was stuck"):
        await asyncio.wait_for(task, 2)
    assert not mgr._lanes[LANE].access.busy()
    await mgr.shutdown()


async def test_lane_close_still_waits_for_a_call_that_is_only_slow(tmp_path):
    mgr, engine = await lane(tmp_path, queue_timeout=0.2)
    task = asyncio.ensure_future(stuck_call(mgr, deadline_s=30))  # inside its deadline, page answers
    await asyncio.sleep(0.05)
    with pytest.raises(LaneBusy):
        await mgr.close(LANE, holder=agent("b"))
    assert not engine.sessions[-1].closed and mgr.take_close_note(LANE) is None
    task.cancel()
    await mgr.shutdown()


async def test_the_memory_watchdog_closes_a_lane_whose_only_call_is_past_its_deadline(tmp_path):
    mgr, engine = await lane(tmp_path)
    session = engine.sessions[-1]
    release = asyncio.Event()

    async def stubborn():  # swallows its interrupt: it stays on the lane past its deadline
        async with mgr.using(LANE, holder=agent("a"), deadline_s=0.1, tool="browser_act"):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
            await release.wait()

    task = asyncio.ensure_future(stubborn())
    await asyncio.sleep(0.15)
    assert any(c.overdue for c in mgr._lanes[LANE].calls)
    assert await mgr.recycle_for_memory(LANE, "its browser used 1430 MB of its 1536 MB ceiling")
    assert session.closed and mgr.get(LANE).state == LaneState.COLD
    assert not mgr._lanes[LANE].access.busy()  # its place on the lane was taken from it
    release.set()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    await mgr.shutdown()


async def test_a_long_legitimate_call_is_not_cut_short(tmp_path):
    mgr, engine = await lane(tmp_path)
    async with mgr.using(LANE, holder=agent("a"), deadline_s=0.5, tool="browser_wait"):
        await asyncio.sleep(0.3)  # inside its deadline
    assert not engine.sessions[-1].closed
    # the table: a wait gets its own timeout on top, a login the minutes a code card may wait
    assert deadlines.deadline_for("browser_wait", extra_s=60) >= 120
    assert deadlines.deadline_for("login") > 900
    assert deadlines.deadline_for("browser_act", extra_s=deadlines.typing_s("x" * 2000)) > 1200
    await mgr.shutdown()


async def test_tool_calls_take_the_lane_with_their_deadline():
    seen: dict = {}

    class Lanes:
        def using(self, on, *, principal_kind="agent", holder=None, mode="act", deadline_s=None, tool=None):
            seen.update(deadline_s=deadline_s, tool=tool)

    for tool, extra, want in (("browser_wait", 60.0, 120.0), ("login", 0.0, 1080.0), ("browser_task", 0.0, 120.0)):
        ctx = SimpleNamespace(tool=tool, principal=None, meta=None, runtime=None)
        node_tools.ctx_session(ctx, LANE, lanes=Lanes(), extra_s=extra)
        assert seen == {"deadline_s": want, "tool": tool}


async def test_a_call_whose_client_went_away_is_cancelled_and_frees_the_lane(tmp_path):
    mgr, engine = await lane(tmp_path)
    gone = asyncio.Event()
    context = SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={registry.CLIENT_GONE_KEY: gone})))

    async def call():
        watch = registry.watch_client(context, "browser_navigate")
        assert watch is not None
        try:
            await stuck_call(mgr, deadline_s=None)
        finally:
            watch.cancel()

    task = asyncio.ensure_future(call())
    await asyncio.sleep(0.05)
    assert mgr._lanes[LANE].access.busy()
    gone.set()  # the MCP client disconnected
    with pytest.raises(EngineError, match="went away"):
        await asyncio.wait_for(task, 2)
    assert not mgr._lanes[LANE].access.busy() and not engine.sessions[-1].closed
    await mgr.shutdown()


async def test_a_client_gone_outside_a_lane_ends_the_call_quietly():
    gone = asyncio.Event()
    context = SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={registry.CLIENT_GONE_KEY: gone})))

    async def call():
        watch = registry.watch_client(context, "approval_wait")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return interrupts.take()
        finally:
            watch.cancel()

    task = asyncio.ensure_future(call())
    await asyncio.sleep(0)
    gone.set()
    assert await asyncio.wait_for(task, 1) == interrupts.CLIENT_GONE
