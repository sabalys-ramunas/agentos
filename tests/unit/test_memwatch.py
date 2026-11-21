"""The memory cap every lane gets by default, and the watchdog (openajar.node.memwatch): what
the ceiling is on a given node, how the lane manager applies it, how a lane's memory is read, and
the order the watchdog acts in (warn and a card first, then a graceful close, never mid-call)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from test_lanes_claims import LANE, A, act, lane
from test_lanes_holds import build

from openajar.core.config import NodeConfig
from openajar.core.errors import ClaimLost
from openajar.core.models import LaneState
from openajar.core.runtime import Runtime
from openajar.node import memwatch
from openajar.node.claims import RUN, Holder
from openajar.node.lanes import ScopeLimiter
from openajar.node.memwatch import (
    RECYCLE_AFTER,
    LaneMemory,
    MemoryWatch,
    lane_memory_ceiling,
    resolve_ceiling,
)

MB = 1024 * 1024


# ---- the ceiling ---------------------------------------------------------------------------


def test_set_off_and_default():
    assert resolve_ceiling(2048) == (2048, "set")
    assert resolve_ceiling(0) == (None, "off")
    assert resolve_ceiling(None) == (1536, "default")  # LANE_MEMORY_DEFAULT_MB: ten lanes fit on 16 GB
    assert lane_memory_ceiling(NodeConfig(max_warm_lanes=3)) == (1536, "default")
    assert lane_memory_ceiling(NodeConfig(lane_memory_max_mb=0)) == (None, "off")


def test_zero_turns_the_ceiling_off_and_small_caps_are_refused(tmp_path):
    assert NodeConfig(home=tmp_path, lane_memory_max_mb=0).lane_memory_max_mb == 0
    with pytest.raises(ValueError, match="must be 0 .no cap. or at least 1536"):
        NodeConfig(home=tmp_path, lane_memory_max_mb=1024)


def test_the_default_ceiling_rides_on_default_scopes_only(tmp_path, monkeypatch):
    from openajar.node.lanes import resources as lanes_resources

    monkeypatch.setattr(ScopeLimiter, "supported", staticmethod(lambda: True))
    monkeypatch.setattr(lanes_resources, "default_scopes_fit", lambda: True)
    on = build(tmp_path / "on").mgr
    assert on.lane_memory_ceiling == (1536, "default") and on.limiter.max_bytes == 1536 * MB
    assert on.memory_status()["lane_memory_ceiling_mb"] == 1536
    assert on.memory_status()["lane_memory_ceiling_source"] == "default"
    off = build(tmp_path / "off", lane_memory_max_mb=0).mgr
    assert off.lane_memory_ceiling == (None, "off") and off.limiter.max_bytes is None  # scopes stay, no cap
    monkeypatch.setattr(lanes_resources, "default_scopes_fit", lambda: False)  # the hardened unit, not root
    hardened = build(tmp_path / "hardened").mgr
    assert hardened.limiter is None and hardened.lane_memory_ceiling == (1536, "default")  # the watchdog's only


# ---- reading a lane's memory ---------------------------------------------------------------


def _proc(root, pid, ppid, pss_kb=None, cgroup="0::/system.slice/agentosd.service"):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "stat").write_text(f"{pid} (firefox bin) S {ppid} 1 1 0")
    (d / "cgroup").write_text(cgroup + "\n")
    if pss_kb is not None:
        (d / "smaps_rollup").write_text(f"Rss: {pss_kb * 2} kB\nPss: {pss_kb} kB\n")


def test_lane_memory_reads_a_scope_or_sums_the_process_tree(tmp_path):
    proc, cg = tmp_path / "proc", tmp_path / "cg"
    (proc / "self").mkdir(parents=True)
    _proc(proc, 100, 1, 300_000)            # a browser without a scope: itself and its content processes
    _proc(proc, 101, 100, 900_000)
    _proc(proc, 102, 101, 100_000)
    _proc(proc, 200, 1, 10, cgroup="0::/openajar.slice/agentos-lanes.slice/openajar-lane-a--two-200.scope")
    scope = cg / "openajar.slice/agentos-lanes.slice/openajar-lane-a--two-200.scope"
    scope.mkdir(parents=True)
    (scope / "memory.current").write_text(str(1500 * MB))
    usage = LaneMemory(proc, cg).usage({"a/one": [100], "a/two": [200]})
    assert usage == {"a/one": 1300_000 // 1024, "a/two": 1500}
    assert LaneMemory(tmp_path / "nothing", cg).usage({"a/one": [1]}) == {}  # no /proc: a Mac


# ---- the watchdog --------------------------------------------------------------------------


class Lanes:
    """What the watchdog reads and calls on the lane manager that owns it."""

    def __init__(self, safe=True, available=2000):
        self.safe = safe
        self.recycled: list[str] = []
        self.events: list[tuple[str, str, dict]] = []
        self.memory_floor_mb = 800
        self.available = available

    def memory_probe(self):
        return self.available

    def lane_pids(self):
        return {"a/one": [1], "a/two": [2]}

    async def recycle_for_memory(self, lane_id, why):
        if self.safe:
            self.recycled.append(lane_id)
        return self.safe

    def _event(self, lane_id, kind, detail):
        self.events.append((lane_id, kind, detail))


class Approvals:
    def __init__(self):
        self.cards = []

    def create_question(self, **kw):
        self.cards.append(kw)


def watch_with(lanes, usage, ceiling=2000):
    """A watch wired the way agentosd wires it: its warnings become cards in Needs you."""
    lanes.memory_watch = MemoryWatch(lanes, ceiling_mb=ceiling, probe=lambda pids: dict(usage))
    runtime = Runtime(lanes=lanes, policy=SimpleNamespace(approvals=Approvals(), cfg=SimpleNamespace(question_ttl_s=60)))
    memwatch.install(runtime)
    return lanes.memory_watch, runtime.policy.approvals


async def test_a_lane_near_its_ceiling_is_warned_about_once_then_closed_when_it_stays_over():
    lanes = Lanes()
    usage = {"a/one": 1700, "a/two": 400}
    w, approvals = watch_with(lanes, usage)
    assert await w.tick() == [("a/one", "warn")]
    assert lanes.events[0][:2] == ("a/one", "memory_high") and len(approvals.cards) == 1
    card = approvals.cards[0]
    assert card["options"] is None and card["workspace"] == "a" and card["requester"].kind == "node"
    assert card["ttl_s"] == 60
    assert "1700 MB of its 2000 MB" in card["question"] and "—" not in card["question"]
    assert await w.tick() == []  # still at 85%: no second card, nothing closed
    usage["a/one"] = 1850  # over 90%: where MemoryHigh reclaims
    for _ in range(RECYCLE_AFTER - 1):
        assert await w.tick() == []
    assert await w.tick() == [("a/one", "recycle")] and lanes.recycled == ["a/one"]
    assert len(approvals.cards) == 1


async def test_a_lane_that_cannot_be_closed_now_is_tried_again_and_a_fall_ends_the_episode():
    lanes = Lanes(safe=False)
    usage = {"a/one": 1900}
    w, approvals = watch_with(lanes, usage)
    for _ in range(RECYCLE_AFTER - 1):
        await w.tick()
    assert await w.tick() == [("a/one", "wait")]  # a call or a run on it: the next look tries again
    usage["a/one"] = 600  # back under 70%: the episode is over
    assert await w.tick() == []
    usage["a/one"] = 1700
    assert await w.tick() == [("a/one", "warn")] and len(approvals.cards) == 2


async def test_the_node_short_of_memory_closes_its_biggest_lane_and_leaves_the_warning_to_health():
    lanes = Lanes(available=700)  # under the 800 MB floor: the health service warns, not the watchdog
    w, approvals = watch_with(lanes, {"a/one": 900, "a/two": 1200}, ceiling=None)
    assert await w.tick() == [] and not approvals.cards and not lanes.events
    lanes.available = 300  # under half the floor
    for _ in range(RECYCLE_AFTER - 1):
        assert await w.tick() == []
    assert await w.tick() == [("a/two", "recycle")]
    assert w.short == 0
    unsafe = Lanes(safe=False, available=300)
    w, _ = watch_with(unsafe, {"a/one": 900}, ceiling=None)
    for _ in range(RECYCLE_AFTER - 1):
        await w.tick()
    assert await w.tick() == [("", "wait")]  # nothing it can close now: it tries again next look


async def test_no_ceiling_means_no_lane_watch_and_a_card_is_optional():
    lanes = Lanes()
    w, approvals = watch_with(lanes, {"a/one": 5000}, ceiling=None)
    assert await w.tick() == [] and not approvals.cards
    bare = MemoryWatch(Lanes(), ceiling_mb=2000, probe=lambda pids: {"a/one": 1700})  # a CLI manager: no Needs you
    assert await bare.tick() == [("a/one", "warn")] and bare.lanes.events


async def test_the_watch_runs_at_most_every_watch_s_however_often_the_reaper_does():
    lanes = Lanes()
    looks = []
    w = MemoryWatch(lanes, ceiling_mb=2000, probe=lambda pids: looks.append(1) or {})
    for now in (100.0, 110.0, 129.0, 130.0, 135.0, 160.0):
        await w.step(now)
    assert len(looks) == 3  # at 100, 130 and 160


async def test_the_lane_manager_owns_the_watch_and_its_reaper_runs_it(tmp_path, monkeypatch):
    monkeypatch.setattr(ScopeLimiter, "supported", staticmethod(lambda: False))
    mgr = build(tmp_path).mgr
    mgr.reaper_interval = 0.01
    assert mgr.memory_watch.ceiling_mb == 1536 and mgr.memory_watch.lanes is mgr
    stepped = asyncio.Event()

    async def step(now):
        stepped.set()
        return []

    monkeypatch.setattr(mgr.memory_watch, "step", step)
    await mgr.start()
    try:
        await asyncio.wait_for(stepped.wait(), 2)
    finally:
        await mgr.shutdown()
    assert mgr.memory_status()["lanes_mb"] == {}  # what each lane used at the last look


def test_agentosd_has_no_separate_memory_watch_service():
    from openajar.node.daemon import FEATURE_SERVICES

    assert not any("memwatch" in name for name in FEATURE_SERVICES)


# ---- the graceful close --------------------------------------------------------------------


async def test_recycle_closes_an_idle_lane_gracefully_and_never_one_in_use(tmp_path):
    t = await lane(tmp_path)
    await t.mgr.claim(LANE, A, purpose="job", ttl_s=900)
    inside, release = asyncio.Event(), asyncio.Event()

    async def call():
        async with t.mgr.using(LANE, holder=A):
            inside.set()
            await release.wait()

    task = asyncio.create_task(call())
    await inside.wait()
    assert await t.mgr.recycle_for_memory(LANE, "test") is False  # a call is on it
    release.set()
    await task
    await t.mgr.take_control(LANE, by="owner")
    assert await t.mgr.recycle_for_memory(LANE, "test") is False  # a person has it
    await t.mgr.hand_back(LANE, by="owner")
    await t.mgr.claim(LANE, A, purpose="job", ttl_s=900)
    assert await t.mgr.recycle_for_memory(LANE, "its browser used 1900 MB") is True
    assert t.mgr.get(LANE).state == LaneState.COLD
    assert any(e.kind == "memory_recycled" for e in t.mgr.events(LANE))
    with pytest.raises(ClaimLost, match="its browser used 1900 MB"):
        await act(t.mgr, A)
    await act(t.mgr, A)  # it opens again on the next call
    assert t.mgr.get(LANE).state in (LaneState.WARM, LaneState.ACTIVE)


async def test_recycle_leaves_a_run_and_a_cold_lane_alone(tmp_path):
    t = await lane(tmp_path, lanes=(LANE, "biz-a/cold"))
    assert await t.mgr.recycle_for_memory("biz-a/cold", "test") is False
    inside, release = asyncio.Event(), asyncio.Event()

    async def run():
        async with t.mgr.using(LANE, holder=Holder.for_run("run_1"), mode=RUN):
            inside.set()
            await release.wait()

    task = asyncio.create_task(run())
    await inside.wait()
    assert await t.mgr.recycle_for_memory(LANE, "test") is False
    release.set()
    await task
