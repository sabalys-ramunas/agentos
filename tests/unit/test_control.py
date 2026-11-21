"""Lane control in the lane manager: take over, hand back, stop, requests, idle hand-back.

Uses the fake engine and displays from test_lanes (no browser, no X).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from test_lanes import make

from openajar.core.errors import AgentPaused, HumanInControl, InvalidRequest, LaneNotFound
from openajar.core.models import ControlState, LaneControl, LaneSpec, LaneState
from openajar.node import lanes as lanes_mod
from openajar.node.lanes import clean_reason
from openajar.node.lanes import control as lanes_control
from openajar.node.store import LaneStore

LANE = "personal/reddit"


class WallClock:
    def __init__(self):
        self.now = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def setup(tmp_path, **cfg):
    mgr, _, _, clock = make(tmp_path, **cfg)
    wall = WallClock()
    mgr.now = wall
    await mgr.create(LaneSpec(id=LANE))
    return mgr, clock, wall


def record(mgr):
    events: list[tuple[str, ControlState, ControlState]] = []
    mgr.add_control_listener(lambda lane_id, old, new: events.append((lane_id, old.state, new.state)))
    return events


# ---- the basic state machine -----------------------------------------------------------


async def test_default_is_agent_and_using_works(tmp_path):
    mgr, *_ = await setup(tmp_path)
    info = mgr.get(LANE)
    assert info.control.state == ControlState.AGENT and info.control.by is None
    async with mgr.using(LANE) as page:
        assert page is not None
    await mgr.shutdown()


async def test_take_blocks_agents_but_not_humans(tmp_path):
    mgr, _, wall = await setup(tmp_path)
    events = record(mgr)
    info = await mgr.take_control(LANE, by="owner")
    assert info.control.state == ControlState.HUMAN
    assert info.control.by == "owner" and info.control.since == wall.now
    assert events == [(LANE, ControlState.AGENT, ControlState.HUMAN)]
    for kind in ("agent", "flow", "brain"):
        with pytest.raises(HumanInControl, match="browser_wait_handback"):
            async with mgr.using(LANE, principal_kind=kind):
                pass
    with pytest.raises(HumanInControl):
        await mgr.open(LANE)
    with pytest.raises(HumanInControl):
        await mgr.close(LANE)
    async with mgr.using(LANE, principal_kind="human") as page:  # the panel may still look
        assert page is not None
    await mgr.open(LANE, principal_kind="human")
    await mgr.close(LANE, principal_kind="human")
    await mgr.shutdown()


async def test_take_twice_is_idempotent_and_another_human_takes_over(tmp_path):
    mgr, _, wall = await setup(tmp_path)
    events = record(mgr)
    await mgr.take_control(LANE, by="owner")
    wall.advance(30)
    again = await mgr.take_control(LANE, by="owner")
    assert again.control.since == wall.now - timedelta(seconds=30)  # unchanged
    assert len(events) == 1
    other = await mgr.take_control(LANE, by="phone")
    assert other.control.by == "phone" and other.control.since == wall.now
    assert len(events) == 2


async def test_stop_pauses_everyone_until_hand_back(tmp_path):
    mgr, *_ = await setup(tmp_path)
    events = record(mgr)
    info = await mgr.stop_agent(LANE, by="owner")
    assert info.control.state == ControlState.PAUSED
    with pytest.raises(AgentPaused, match="stopped agents"):
        async with mgr.using(LANE):
            pass
    assert (await mgr.stop_agent(LANE, by="owner")).control.state == ControlState.PAUSED
    assert len(events) == 1  # stopping twice is one change
    back = await mgr.hand_back(LANE, by="owner")
    assert back.control.state == ControlState.AGENT and back.control.by == "owner"
    async with mgr.using(LANE):
        pass
    assert [e[2] for e in events] == [ControlState.PAUSED, ControlState.AGENT]
    await mgr.shutdown()


async def test_take_from_paused_and_stop_from_human(tmp_path):
    mgr, *_ = await setup(tmp_path)
    await mgr.stop_agent(LANE, by="owner")
    assert (await mgr.take_control(LANE, by="owner")).control.state == ControlState.HUMAN
    assert (await mgr.stop_agent(LANE, by="owner")).control.state == ControlState.PAUSED


async def test_hand_back_when_agents_have_control_is_a_no_op(tmp_path):
    mgr, *_ = await setup(tmp_path)
    events = record(mgr)
    info = await mgr.hand_back(LANE, by="owner")
    assert info.control == LaneControl()
    assert events == []


async def test_unknown_lane_raises_everywhere(tmp_path):
    mgr, *_ = await setup(tmp_path)
    for call in (mgr.take_control("a/nope", by="owner"), mgr.hand_back("a/nope", by="owner"),
                 mgr.stop_agent("a/nope", by="owner"), mgr.deny_request("a/nope", by="owner"),
                 mgr.request_control("a/nope", by="agent", reason="x"),
                 mgr.wait_for_agent_control("a/nope", 0.1)):
        with pytest.raises(LaneNotFound):
            await call
    mgr.note_human_input("a/nope")  # never raises: the relay calls it per event


# ---- requests for control back ----------------------------------------------------------


async def test_request_deny_and_hand_back(tmp_path):
    mgr, *_ = await setup(tmp_path)
    events = record(mgr)
    # agents already have control: nothing to ask for
    info = await mgr.request_control(LANE, by="claude-code", reason="need it")
    assert not info.control.takeback_requested and events == []

    await mgr.take_control(LANE, by="owner")
    info = await mgr.request_control(LANE, by="claude-code", reason="  The login is done \u2014 I need to post.\n")
    c = info.control
    assert c.state == ControlState.HUMAN and c.takeback_requested
    assert c.takeback_reason == "The login is done, I need to post." and c.takeback_by == "claude-code"
    # asking again with the same reason is not a change; a new reason is
    await mgr.request_control(LANE, by="claude-code", reason="The login is done \u2014 I need to post.")
    assert len(events) == 2
    await mgr.request_control(LANE, by="claude-code", reason="Still waiting")
    assert len(events) == 3 and mgr.get(LANE).control.takeback_reason == "Still waiting"

    denied = await mgr.deny_request(LANE, by="owner")
    assert denied.control.state == ControlState.HUMAN and not denied.control.takeback_requested
    assert denied.control.takeback_reason is None and denied.control.takeback_by is None
    assert (await mgr.deny_request(LANE, by="owner")).control == denied.control  # nothing pending
    assert len(events) == 4


async def test_request_after_a_refusal_waits_for_the_cooldown(tmp_path):
    mgr, clock, _ = await setup(tmp_path)
    await mgr.take_control(LANE, by="owner")
    await mgr.request_control(LANE, by="agent", reason="please")
    await mgr.deny_request(LANE, by="owner")
    with pytest.raises(HumanInControl, match="just refused"):
        await mgr.request_control(LANE, by="agent", reason="please")
    clock.advance(lanes_mod.REQUEST_COOLDOWN_SECONDS + 1)
    info = await mgr.request_control(LANE, by="agent", reason="please, the post is ready")
    assert info.control.takeback_requested


async def test_request_needs_a_reason_and_is_cleaned(tmp_path):
    mgr, *_ = await setup(tmp_path)
    await mgr.take_control(LANE, by="owner")
    for reason in ("", "   ", "\n\t", "\u200b"):
        with pytest.raises(InvalidRequest, match="reason"):
            await mgr.request_control(LANE, by="agent", reason=reason)
    long = "x" * 1000
    info = await mgr.request_control(LANE, by="agent", reason=long)
    assert len(info.control.takeback_reason) == lanes_mod.MAX_REASON_CHARS
    assert info.control.takeback_reason.endswith("...")


def test_clean_reason():
    assert clean_reason("a\u202eb\x00c\r\nd") == "a b c d"  # bidi override and controls become spaces
    assert clean_reason("done\u2014next") == "done, next"
    assert clean_reason(None) == ""
    assert "\u2014" not in clean_reason("x \u2014 y \u2014 z")


async def test_hand_back_clears_a_pending_request_and_stop_clears_it_too(tmp_path):
    mgr, *_ = await setup(tmp_path)
    await mgr.take_control(LANE, by="owner")
    await mgr.request_control(LANE, by="agent", reason="ready")
    back = await mgr.hand_back(LANE, by="owner")
    assert back.control.state == ControlState.AGENT and not back.control.takeback_requested
    await mgr.take_control(LANE, by="owner")
    assert not mgr.get(LANE).control.takeback_requested  # a new take starts clean
    await mgr.request_control(LANE, by="agent", reason="ready")
    stopped = await mgr.stop_agent(LANE, by="owner")
    assert stopped.control.state == ControlState.PAUSED and not stopped.control.takeback_requested


async def test_another_human_taking_over_keeps_the_request(tmp_path):
    mgr, *_ = await setup(tmp_path)
    await mgr.take_control(LANE, by="owner")
    await mgr.request_control(LANE, by="agent", reason="ready")
    info = await mgr.take_control(LANE, by="phone")
    assert info.control.takeback_requested and info.control.takeback_reason == "ready"


# ---- races -------------------------------------------------------------------------------


async def test_take_over_while_an_agent_waits_for_the_lock_wins(tmp_path):
    mgr, *_ = await setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    order: list[str] = []

    async def first_agent():
        async with mgr.using(LANE):
            order.append("first in")
            entered.set()
            await release.wait()
            order.append("first done")  # in-flight work finishes its step

    async def second_agent():
        await entered.wait()
        async with mgr.using(LANE):  # passes the first check, then waits for the lock
            order.append("second in")

    first = asyncio.create_task(first_agent())
    second = asyncio.create_task(second_agent())
    await entered.wait()
    await asyncio.sleep(0.01)
    assert mgr._lanes[LANE].users == 2  # the second agent is waiting for the lane
    await mgr.take_control(LANE, by="owner")
    release.set()
    await first
    with pytest.raises(HumanInControl):
        await second
    assert order == ["first in", "first done"]
    assert mgr.get(LANE).state == LaneState.WARM and mgr._lanes[LANE].users == 0
    await mgr.shutdown()


async def test_take_over_while_the_lane_is_warming_wins(tmp_path):
    from test_lanes import FakeEngine

    mgr, *_ = make(tmp_path, engine=FakeEngine(delay=0.05))
    await mgr.create(LaneSpec(id=LANE))

    async def agent():
        async with mgr.using(LANE):
            pass

    task = asyncio.create_task(agent())
    await asyncio.sleep(0.01)  # inside open(), launching the browser
    await mgr.take_control(LANE, by="owner")
    with pytest.raises(HumanInControl):
        await task
    assert mgr.get(LANE).state == LaneState.WARM  # the lane warmed; the agent never got the page
    await mgr.shutdown()


async def test_hand_back_during_wait_for_agent_control(tmp_path):
    mgr, *_ = await setup(tmp_path)
    await mgr.take_control(LANE, by="owner")
    loop = asyncio.get_running_loop()
    started = loop.time()
    waiter = asyncio.create_task(mgr.wait_for_agent_control(LANE, 5.0))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await mgr.hand_back(LANE, by="owner")
    info = await asyncio.wait_for(waiter, 1.0)
    assert info.control.state == ControlState.AGENT
    assert loop.time() - started < 1.0


async def test_wait_for_agent_control_times_out_with_the_current_state(tmp_path):
    mgr, *_ = await setup(tmp_path)
    assert (await mgr.wait_for_agent_control(LANE, 0)).control.state == ControlState.AGENT
    await mgr.stop_agent(LANE, by="owner")
    info = await mgr.wait_for_agent_control(LANE, 0.05)
    assert info.control.state == ControlState.PAUSED
    assert (await mgr.wait_for_agent_control(LANE, 0)).control.state == ControlState.PAUSED
    assert (await mgr.wait_for_agent_control(LANE, -5)).control.state == ControlState.PAUSED


async def test_wait_keeps_waiting_through_other_changes(tmp_path):
    mgr, *_ = await setup(tmp_path)
    await mgr.take_control(LANE, by="owner")
    waiter = asyncio.create_task(mgr.wait_for_agent_control(LANE, 5.0))
    await asyncio.sleep(0.01)
    await mgr.request_control(LANE, by="agent", reason="ready")
    await mgr.stop_agent(LANE, by="owner")
    await asyncio.sleep(0.02)
    assert not waiter.done()
    await mgr.hand_back(LANE, by="owner")
    assert (await asyncio.wait_for(waiter, 1.0)).control.state == ControlState.AGENT


async def test_many_waiters_wake_together(tmp_path):
    mgr, *_ = await setup(tmp_path)
    await mgr.take_control(LANE, by="owner")
    waiters = [asyncio.create_task(mgr.wait_for_agent_control(LANE, 5.0)) for _ in range(5)]
    await asyncio.sleep(0.01)
    await mgr.hand_back(LANE, by="owner")
    results = await asyncio.wait_for(asyncio.gather(*waiters), 1.0)
    assert all(r.control.state == ControlState.AGENT for r in results)


# ---- idle hand-back ------------------------------------------------------------------------


async def test_idle_hand_back_after_control_idle_seconds(tmp_path):
    mgr, _, wall = await setup(tmp_path, control_idle_seconds=600)
    events = record(mgr)
    await mgr.take_control(LANE, by="owner")
    wall.advance(500)
    assert await mgr.reap_idle_control() == []
    mgr.note_human_input(LANE)  # the human is still there: the timer restarts
    wall.advance(500)
    assert await mgr.reap_idle_control() == []
    wall.advance(101)
    assert await mgr.reap_idle_control() == [LANE]
    c = mgr.get(LANE).control
    assert c.state == ControlState.AGENT and c.by == lanes_mod.IDLE_HANDBACK_BY
    assert events[-1] == (LANE, ControlState.HUMAN, ControlState.AGENT)


async def test_paused_never_idles_back(tmp_path):
    mgr, _, wall = await setup(tmp_path, control_idle_seconds=60)
    await mgr.stop_agent(LANE, by="owner")
    wall.advance(10_000)
    assert await mgr.reap_idle_control() == []
    assert mgr.get(LANE).control.state == ControlState.PAUSED


async def test_idle_hand_back_can_be_turned_off(tmp_path):
    mgr, _, wall = await setup(tmp_path, control_idle_seconds=0)
    await mgr.take_control(LANE, by="owner")
    wall.advance(10_000)
    assert await mgr.reap_idle_control() == []


async def test_reaper_loop_hands_back(tmp_path):
    mgr, _, wall = await setup(tmp_path, control_idle_seconds=60)
    await mgr.start()
    await mgr.take_control(LANE, by="owner")
    wall.advance(61)
    for _ in range(100):
        if mgr.get(LANE).control.state == ControlState.AGENT:
            break
        await asyncio.sleep(0.01)
    assert mgr.get(LANE).control.state == ControlState.AGENT
    await mgr.shutdown()


async def test_a_lane_a_human_holds_is_never_reaped_or_evicted(tmp_path):
    mgr, clock, _ = await setup(tmp_path, max_warm_lanes=1, busy_timeout=0.05, idle_seconds=60)
    await mgr.create(LaneSpec(id="personal/other"))
    await mgr.open(LANE)
    await mgr.take_control(LANE, by="owner")
    clock.advance(1000)
    assert await mgr.reap_idle() == []
    assert mgr.get(LANE).state == LaneState.WARM
    from openajar.core.errors import LaneBusy
    with pytest.raises(LaneBusy):
        await mgr.open("personal/other")  # the only slot is the human's
    await mgr.hand_back(LANE, by="owner")
    await mgr.open("personal/other")  # now the idle lane can go
    assert mgr.get(LANE).state == LaneState.COLD
    await mgr.shutdown()


async def test_human_input_keeps_a_lane_warm(tmp_path):
    mgr, clock, _ = await setup(tmp_path, idle_seconds=60)
    await mgr.open(LANE)
    await mgr.stop_agent(LANE, by="owner")  # paused lanes may be reaped when nobody uses them
    clock.advance(50)
    mgr.note_human_input(LANE)
    clock.advance(50)
    assert await mgr.reap_idle() == []
    clock.advance(11)
    assert await mgr.reap_idle() == [LANE]
    await mgr.shutdown()


# ---- listeners -----------------------------------------------------------------------------


async def test_listeners_sync_async_and_failing(tmp_path, caplog):
    mgr, *_ = await setup(tmp_path)
    seen: list[str] = []
    order: list[str] = []

    def broken(lane_id, old, new):
        raise RuntimeError("listener bug")

    async def broken_async(lane_id, old, new):
        raise RuntimeError("async listener bug")

    def sync_one(lane_id, old, new):
        # sync listeners run before take_control returns, in the same tick as the change
        order.append(f"sync:{mgr._controls[lane_id].state.value}")
        seen.append(f"sync {old.state.value}->{new.state.value}")

    async def async_one(lane_id, old, new):
        await asyncio.sleep(0)
        seen.append(f"async {new.state.value}")

    for cb in (broken, broken_async, sync_one, async_one):
        mgr.add_control_listener(cb)
    info = await mgr.take_control(LANE, by="owner")
    assert info.control.state == ControlState.HUMAN
    assert seen == ["sync agent->human", "async human"]
    assert order == ["sync:human"]
    assert "listener bug" in caplog.text and "async listener bug" in caplog.text
    mgr.remove_control_listener(sync_one)
    mgr.remove_control_listener(sync_one)  # removing twice is fine
    await mgr.hand_back(LANE, by="owner")
    assert seen == ["sync agent->human", "async human", "async agent"]


async def test_slow_async_listener_does_not_hold_up_the_change(tmp_path, monkeypatch):
    monkeypatch.setattr(lanes_control, "LISTENER_TIMEOUT", 0.05)
    mgr, *_ = await setup(tmp_path)
    finished, release = asyncio.Event(), asyncio.Event()

    async def slow(lane_id, old, new):
        await release.wait()  # held until the test lets it go: no race against a wall clock
        finished.set()

    mgr.add_control_listener(slow)
    await asyncio.wait_for(mgr.take_control(LANE, by="owner"), 5)  # returned with the listener held
    assert not finished.is_set()
    release.set()
    assert mgr._background  # still running, held so it is not garbage collected
    await asyncio.wait_for(finished.wait(), 1.0)
    await asyncio.sleep(0)
    assert not mgr._background


async def test_a_listener_may_change_control_itself(tmp_path):
    mgr, *_ = await setup(tmp_path)

    async def auto_deny(lane_id, old, new):
        if new.takeback_requested:
            await mgr.deny_request(lane_id, by="policy")

    mgr.add_control_listener(auto_deny)
    await mgr.take_control(LANE, by="owner")
    await mgr.request_control(LANE, by="agent", reason="ready")
    assert not mgr.get(LANE).control.takeback_requested


# ---- persistence -------------------------------------------------------------------------


async def test_control_survives_a_new_manager(tmp_path):
    mgr, _, wall = await setup(tmp_path)
    await mgr.take_control(LANE, by="owner")
    await mgr.request_control(LANE, by="claude-code", reason="ready to post")
    await mgr.shutdown()

    again, *_ = make(tmp_path)
    c = again.get(LANE).control  # read straight from the store, before start()
    assert c.state == ControlState.HUMAN and c.by == "owner" and c.since == wall.now
    assert c.takeback_requested and c.takeback_reason == "ready to post"
    await again.start()
    with pytest.raises(HumanInControl):
        async with again.using(LANE):
            pass
    await again.hand_back(LANE, by="owner")
    await again.shutdown()
    third, *_ = make(tmp_path)
    assert third.get(LANE).control.state == ControlState.AGENT
    assert [i.control.state for i in third.list()] == [ControlState.AGENT]


async def test_idle_hand_back_counts_time_across_a_restart(tmp_path):
    mgr, _, wall = await setup(tmp_path, control_idle_seconds=600)
    await mgr.take_control(LANE, by="owner")
    wall.advance(100)
    mgr.note_human_input(LANE)
    await mgr.shutdown()  # flushes the last input time

    again, *_ = make(tmp_path, control_idle_seconds=600)
    again.now = wall
    await again.start()
    assert again.get(LANE).control.last_human_input == wall.now
    wall.advance(599)
    assert await again.reap_idle_control() == []
    wall.advance(2)
    assert await again.reap_idle_control() == [LANE]
    await again.shutdown()


async def test_human_input_writes_are_throttled(tmp_path, monkeypatch):
    mgr, clock, wall = await setup(tmp_path)
    await mgr.take_control(LANE, by="owner")
    writes: list = []
    original = mgr.store.set_control
    monkeypatch.setattr(mgr.store, "set_control", lambda lane_id, c: (writes.append(c), original(lane_id, c)))
    for _ in range(50):
        wall.advance(0.01)
        mgr.note_human_input(LANE)
    assert len(writes) == 1  # the first event writes; the rest wait for the throttle
    assert mgr.get(LANE).control.last_human_input == wall.now  # memory is always current
    clock.advance(lanes_mod.INPUT_PERSIST_SECONDS)
    mgr.note_human_input(LANE)
    assert len(writes) == 2


async def test_a_store_failure_still_applies_control(tmp_path, monkeypatch, caplog):
    mgr, *_ = await setup(tmp_path)

    def broken(lane_id, control):
        raise OSError("disk full")

    monkeypatch.setattr(mgr.store, "set_control", broken)
    info = await mgr.take_control(LANE, by="owner")
    assert info.control.state == ControlState.HUMAN
    with pytest.raises(HumanInControl):
        async with mgr.using(LANE):
            pass
    assert "could not save control" in caplog.text


def test_a_broken_control_record_fails_safe(tmp_path):
    import sqlite3

    store = LaneStore(tmp_path / "openajar.db")
    store.add(LaneSpec(id=LANE))
    with sqlite3.connect(store.path) as conn:
        conn.execute("INSERT INTO lane_control (id, control) VALUES (?, ?)", (LANE, "{not json"))
    c = store.get(LANE).control
    assert c.state == ControlState.PAUSED and c.by == "recovery"


def test_store_delete_removes_control(tmp_path):
    store = LaneStore(tmp_path / "openajar.db")
    store.add(LaneSpec(id=LANE))
    store.set_control(LANE, LaneControl(state=ControlState.HUMAN, by="owner"))
    assert store.get(LANE).control.state == ControlState.HUMAN
    store.set_control(LANE, LaneControl(state=ControlState.PAUSED, by="owner"))  # upsert
    assert store.list()[0].control.state == ControlState.PAUSED
    assert store.delete(LANE)
    store.add(LaneSpec(id=LANE))
    assert store.get(LANE).control is None
