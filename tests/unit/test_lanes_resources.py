"""Resource rules of the lane manager (memory floor, disk floor, per-lane memory limits), display
sizing to the browser window, and the small lifecycle commands (delete, set-proxy).

The real LaneManagerImpl over the fakes of test_lanes.py (no browser, no X).
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from test_lanes import FakeDisplays, FakeEngine
from test_lanes_holds import build

from agentos.core.config import (
    LANE_MEMORY_DEFAULT_MB,
    LANE_MEMORY_MIN_MB,
    NodeConfig,
    load_config,
    save_config,
)
from agentos.core.errors import ConfirmRequired, InvalidRequest, LaneBusy, LaneNotFound
from agentos.core.models import LaneSpec, LaneState, ViewerInfo
from agentos.node.camoufox.engine import window_size_of
from agentos.node.lanes import ResourcesLow, ScopeLimiter

MB = 1024 * 1024


class Memory:
    """MemAvailable that drops by `per_lane` MB for every browser the engine runs."""

    def __init__(self, engine: FakeEngine, total: int = 2000, per_lane: int = 600):
        self.engine, self.total, self.per_lane = engine, total, per_lane

    def __call__(self) -> int:
        return self.total - self.per_lane * len(self.engine.running)


# ---- memory ---------------------------------------------------------------------------


async def test_the_memory_floor_refuses_a_warm_with_a_clear_error(tmp_path):
    t = build(tmp_path, memory_probe=lambda: 300, memory_floor_mb=800)
    await t.mgr.create(LaneSpec(id="a/one"))
    with pytest.raises(ResourcesLow) as e:
        await t.mgr.open("a/one")
    assert e.value.code == "resources_low"
    assert "only 300 MB of memory is free" in e.value.message and "800 MB" in e.value.message
    assert "\u2014" not in e.value.message
    assert t.engine.launches == [] and t.mgr.get("a/one").state == LaneState.COLD
    with pytest.raises(ResourcesLow):
        async with t.mgr.using("a/one"):
            pass


async def test_low_memory_closes_the_least_recently_used_agent_lane_first(tmp_path):
    engine = FakeEngine()
    t = build(tmp_path, engine=engine, memory_probe=Memory(engine), memory_floor_mb=900)
    for name in ("a/one", "a/two", "a/three"):
        await t.mgr.create(LaneSpec(id=name))
    await t.mgr.open("a/one")
    t.clock.advance(1)
    await t.mgr.open("a/two")  # 2000 - 600 = 1400 free: fine
    t.clock.advance(1)
    async with t.mgr.using("a/one"):  # one is now the more recently used
        pass
    t.clock.advance(1)
    await t.mgr.open("a/three")  # 800 MB free is below the 900 MB floor: two (LRU) closes first
    states = {i.spec.id: i.state for i in t.mgr.list()}
    assert states == {"a/one": LaneState.WARM, "a/two": LaneState.COLD, "a/three": LaneState.WARM}


async def test_low_memory_never_closes_a_lane_a_human_holds_or_one_in_use(tmp_path):
    engine = FakeEngine()
    t = build(tmp_path, engine=engine, memory_probe=Memory(engine, total=1300), memory_floor_mb=800,
              busy_timeout=0.3)
    for name in ("a/human", "a/busy", "a/new"):
        await t.mgr.create(LaneSpec(id=name))
    await t.mgr.open("a/human")
    await t.mgr.take_control("a/human", by="owner")
    with pytest.raises(ResourcesLow):
        await t.mgr.open("a/new")  # 700 MB free, and the only warm lane is the human's
    assert t.mgr.get("a/human").state == LaneState.WARM

    await t.mgr.hand_back("a/human", by="owner")
    await t.mgr.close("a/human")
    entered, release = asyncio.Event(), asyncio.Event()

    async def call():
        async with t.mgr.using("a/busy"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(call())
    await entered.wait()
    with pytest.raises(ResourcesLow):
        await t.mgr.open("a/new")  # the busy lane is mid-call: waited busy_timeout, never evicted
    assert t.mgr.get("a/busy").state == LaneState.ACTIVE
    release.set()
    await task


async def test_a_paused_lane_is_closed_for_memory_only_after_agent_lanes(tmp_path):
    engine = FakeEngine()
    t = build(tmp_path, engine=engine, memory_probe=Memory(engine, total=2000), memory_floor_mb=900)
    for name in ("a/paused", "a/agent", "a/new"):
        await t.mgr.create(LaneSpec(id=name))
    await t.mgr.open("a/paused")
    t.clock.advance(1)
    await t.mgr.open("a/agent")
    await t.mgr.hold("a/paused", reason="needs_human:captcha")  # older, but held for a human
    await t.mgr.open("a/new")
    assert t.mgr.get("a/agent").state == LaneState.COLD and t.mgr.get("a/paused").state == LaneState.WARM


async def test_memory_reservations_count_against_the_floor(tmp_path):
    available = [1500]
    t = build(tmp_path, memory_probe=lambda: available[0], memory_floor_mb=800)
    await t.mgr.create(LaneSpec(id="a/one"))
    t.mgr.reserve_memory("brain:triage", 600)
    with pytest.raises(ResourcesLow, match="brain:other needs 200 MB"):
        t.mgr.reserve_memory("brain:other", 200)  # 1500 - 600 - 200 is below the floor
    t.mgr.reserve_memory("brain:triage", 700)  # replaces its own reservation: 800 left, at the floor
    available[0] = 1400  # something else grew
    with pytest.raises(ResourcesLow, match="700 MB of it reserved"):
        await t.mgr.open("a/one")
    t.mgr.release_memory("brain:triage")
    assert (await t.mgr.open("a/one")).state == LaneState.WARM
    status = t.mgr.memory_status()
    assert status["available_mb"] == 1400 and status["reserved_mb"] == 0 and status["floor_mb"] == 800


async def test_unknown_memory_never_blocks_a_lane(tmp_path):
    t = build(tmp_path, memory_probe=lambda: None, memory_floor_mb=800)
    await t.mgr.create(LaneSpec(id="a/one"))
    assert (await t.mgr.open("a/one")).state == LaneState.WARM


# ---- disk -----------------------------------------------------------------------------


async def test_a_nearly_full_disk_refuses_to_warm_rather_than_risk_the_cookies(tmp_path):
    t = build(tmp_path, disk_probe=lambda path: 900 * MB, disk_floor_bytes=2 * 1024**3)
    await t.mgr.create(LaneSpec(id="a/one"))
    with pytest.raises(ResourcesLow) as e:
        await t.mgr.open("a/one")
    assert "only 900 MB is free on the disk" in e.value.message and "2.0 GB" in e.value.message
    assert "cookies" in e.value.message and t.engine.launches == []


async def test_a_warm_lane_keeps_working_when_the_disk_fills(tmp_path):
    free = [10 * 1024**3]
    t = build(tmp_path, disk_probe=lambda path: free[0], disk_floor_bytes=2 * 1024**3)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.open("a/one")
    free[0] = 100 * MB
    t.mgr._watch_disk()  # logs once; the warm browser is not cut off mid-write
    async with t.mgr.using("a/one"):
        pass


# ---- per-lane memory limits -------------------------------------------------------------


async def test_the_scope_limiter_asks_systemd_for_a_scope_with_memory_max():
    calls = []

    async def run(argv):
        calls.append(argv)
        return 0, "o \"/org/freedesktop/systemd1/job/1\""

    limiter = ScopeLimiter(1024, run=run)
    unit = await limiter.apply("biz-a/instagram", [4321, 4322, 4321, 0])
    assert unit == "agentos-lane-biz-a--instagram-4321.scope"
    argv = calls[0]
    assert argv[:7] == ["busctl", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                        "org.freedesktop.systemd1.Manager", "StartTransientUnit", "ssa(sv)a(sa(sv))"]
    i = argv.index("PIDs")
    assert argv[i:i + 5] == ["PIDs", "au", "2", "4321", "4322"]
    i = argv.index("MemoryMax")
    assert argv[i:i + 3] == ["MemoryMax", "t", str(1024 * MB)]
    assert "MemorySwapMax" in argv and argv[-1] == "0"
    assert await limiter.apply("a/b", []) is None


async def test_every_scope_shares_the_cpu_per_lane_and_caps_tasks_cpu_and_memory():
    calls = []

    async def run(argv):
        calls.append(argv)
        return 0, ""

    def props(argv):
        i = argv.index("PIDs")
        count = int(argv[9])
        out, j = {}, i + 3 + int(argv[i + 2])
        while j < len(argv) - 1:
            out[argv[j]] = argv[j + 2]
            j += 3
        assert len(out) + 1 == count  # PIDs and the rest, as many as the call says
        return out

    await ScopeLimiter(run=run).apply("a/b", [10])
    plain = props(calls[-1])
    assert plain["CPUWeight"] == "100" and plain["TasksMax"] == "4096"
    # every lane in one slice: together they weigh like one unit against agentosd and the system
    assert plain["Slice"] == "agentos-lanes.slice"
    # one content process killed for memory must not stop the lane's whole browser (systemd's
    # default for a scope is stop: measured on systemd 255, the sibling processes die too)
    assert plain["OOMPolicy"] == "continue"
    assert "MemoryMax" not in plain and "CPUQuotaPerSecUSec" not in plain
    await ScopeLimiter(1024, cpu_max_pct=150, run=run).apply("a/b", [10])
    capped = props(calls[-1])
    assert capped["MemoryMax"] == str(1024 * MB) and capped["MemoryHigh"] == str(int(1024 * MB * 0.9))
    assert capped["MemorySwapMax"] == "0" and capped["CPUQuotaPerSecUSec"] == "1500000"
    assert ScopeLimiter(1024, cpu_max_pct=150).describe() == "memory 1.0 GB, CPU 150% of a core, 4096 tasks"


async def test_a_systemd_without_oompolicy_on_scopes_still_gets_the_scope():
    calls = []

    async def run(argv):
        calls.append(argv)
        if "OOMPolicy" in argv:
            return 1, "Call failed: Cannot set property OOMPolicy, or unknown property."
        return 0, ""

    limiter = ScopeLimiter(2048, run=run)
    assert await limiter.apply("a/b", [10]) == "agentos-lane-a--b-10.scope"
    assert ["OOMPolicy" in argv for argv in calls] == [True, False]
    assert await limiter.apply("a/c", [11]) == "agentos-lane-a--c-11.scope"
    assert "OOMPolicy" not in calls[-1] and len(calls) == 3  # asked once; later scopes go without


def test_lanes_get_scopes_by_default_where_systemd_runs(tmp_path, monkeypatch):
    from agentos.node.lanes import resources as lanes_resources

    monkeypatch.setattr(ScopeLimiter, "supported", staticmethod(lambda: True))
    monkeypatch.setattr(lanes_resources, "default_scopes_fit", lambda: False)  # not root, or a capped service
    assert build(tmp_path / "capped-service").mgr.limiter is None
    assert build(tmp_path / "asked", lane_memory_max_mb=2048).mgr.limiter.max_bytes == 2048 * MB
    monkeypatch.setattr(lanes_resources, "default_scopes_fit", lambda: True)
    on = build(tmp_path / "on").mgr
    assert on.limiter is not None and on.limiter.max_bytes == LANE_MEMORY_DEFAULT_MB * MB
    assert on.limiter.cpu_max_pct is None
    assert on.memory_status()["lane_memory_max_mb"] is None  # reported once systemd has applied it
    off = build(tmp_path / "off", lane_scopes=False).mgr
    assert off.limiter is None
    capped = build(tmp_path / "capped", lane_scopes=False, lane_cpu_max_pct=200).mgr
    assert capped.limiter.cpu_max_pct == 200
    assert capped.memory_status()["lane_limits"]["configured"] == "CPU 200% of a core, 4096 tasks"
    monkeypatch.setattr(ScopeLimiter, "supported", staticmethod(lambda: False))
    assert build(tmp_path / "mac").mgr.limiter is None


def test_the_default_cap_rides_on_the_default_scopes_and_never_asks_for_one(tmp_path, monkeypatch):
    from agentos.node.lanes import resources as lanes_resources

    monkeypatch.setattr(ScopeLimiter, "supported", staticmethod(lambda: True))
    monkeypatch.setattr(lanes_resources, "default_scopes_fit", lambda: True)
    props = {name: value for name, _, value in build(tmp_path / "on").mgr.limiter.properties()}
    assert props["MemoryMax"] == str(1536 * MB) and props["MemoryHigh"] == str(int(1536 * MB * 0.9))

    # the opt-outs: lane_scopes = false is no scope at all, 0 keeps the scopes without a memory cap
    assert build(tmp_path / "off", lane_scopes=False).mgr.limiter is None
    no_cap = build(tmp_path / "none", lane_memory_max_mb=0).mgr
    assert no_cap.limiter is not None and no_cap.limiter.max_bytes is None
    assert "MemoryMax" not in [name for name, _, _ in no_cap.limiter.properties()]
    assert build(tmp_path / "zero", lane_scopes=False, lane_memory_max_mb=0).mgr.limiter is None
    asked = build(tmp_path / "asked", lane_scopes=False, lane_memory_max_mb=2048).mgr
    assert asked.limiter.max_bytes == 2048 * MB  # a number is an explicit ask, scopes off or not

    # a capped or unprivileged service: no default scope, or browsers would leave the service's cap
    monkeypatch.setattr(lanes_resources, "default_scopes_fit", lambda: False)
    assert build(tmp_path / "capped").mgr.limiter is None
    cpu_only = build(tmp_path / "cpu", lane_cpu_max_pct=200).mgr
    assert cpu_only.limiter.cpu_max_pct == 200 and cpu_only.limiter.max_bytes is None


def test_ten_lanes_fit_a_16_gb_node_at_the_default_cap():
    node_mb = 16 * 1024
    assert 10 * LANE_MEMORY_DEFAULT_MB < node_mb  # every lane at its ceiling at once
    assert 10 * 1024 + NodeConfig().memory_floor_mb + 2048 < node_mb  # a real 1 GB lane, the floor, agentosd and the OS
    assert LANE_MEMORY_DEFAULT_MB >= LANE_MEMORY_MIN_MB  # the default is a value node.toml may hold


def test_the_default_cap_is_not_written_into_node_toml(tmp_path):
    save_config(NodeConfig(home=tmp_path))  # written out, it would be an explicit ask for a scope
    assert "lane_memory_max_mb" not in (tmp_path / "node.toml").read_text()
    assert load_config(tmp_path).lane_memory_max_mb is None
    save_config(NodeConfig(home=tmp_path, lane_memory_max_mb=0))  # and the opt-out survives a write
    assert load_config(tmp_path).lane_memory_max_mb == 0


async def test_a_failed_scope_never_stops_the_lane(tmp_path):
    async def run(argv):
        return 1, "Access denied"

    applied = []

    class Limiter(ScopeLimiter):
        async def apply(self, lane_id, pids):
            applied.append((lane_id, pids))
            return await super().apply(lane_id, pids)

    t = build(tmp_path, limiter=Limiter(512, run=run))
    await t.mgr.create(LaneSpec(id="a/one"))
    assert (await t.mgr.open("a/one")).state == LaneState.WARM
    assert applied == [("a/one", [t.engine.sessions[0].pid])]


async def test_caps_systemd_refuses_are_reported_as_not_applied_and_warned_once(tmp_path, caplog):
    answers = [(1, "Call failed: Access denied")] * 2 + [(0, "")]

    async def run(argv):
        return answers.pop(0)

    t = build(tmp_path, limiter=ScopeLimiter(2048, cpu_max_pct=150, run=run))
    status = t.mgr.memory_status()
    assert status["lane_memory_max_mb"] is None and status["lane_limits"]["working"] is None  # not tried yet
    with caplog.at_level(logging.DEBUG, logger="agentos.lanes"):
        for name in ("a/one", "a/two"):
            await t.mgr.create(LaneSpec(id=name))
            assert (await t.mgr.open(name)).state == LaneState.WARM
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "could not limit" in r.getMessage()]
    assert len(warnings) == 1 and "Access denied" in warnings[0].getMessage()
    status = t.mgr.memory_status()
    assert status["lane_memory_max_mb"] is None and status["lane_cpu_max_pct"] is None
    assert status["lane_limits"] == {"configured": "memory 2.0 GB, CPU 150% of a core, 4096 tasks", "applied": 0,
                                     "refused": 2, "working": False, "error": "Call failed: Access denied"}
    await t.mgr.create(LaneSpec(id="a/three"))
    await t.mgr.open("a/three")
    status = t.mgr.memory_status()
    assert status["lane_memory_max_mb"] == 2048 and status["lane_cpu_max_pct"] == 150
    assert status["lane_limits"]["working"] is True and status["lane_limits"]["applied"] == 1


# ---- the display fits the window --------------------------------------------------------


class SizedDisplays(FakeDisplays):
    def __init__(self, config):
        super().__init__(config)
        self.sizes: dict[str, tuple[int, int]] = {}
        self.acquired: list[tuple[str, tuple[int, int] | None]] = []

    async def acquire(self, lane_id, *, size=None):
        self.acquired.append((lane_id, size))
        self.sizes[lane_id] = size or (1920, 1080)
        return await super().acquire(lane_id)

    def get(self, lane_id):
        viewer = super().get(lane_id)
        if viewer is None:
            return None
        width, height = self.sizes[lane_id]
        return ViewerInfo(**{**viewer.model_dump(), "width": width, "height": height})


class WindowEngine(FakeEngine):
    """Pins a fingerprint with a 1600x856 window on first launch, like Camoufox does."""

    async def launch(self, spec, *, profile_dir, fingerprint_path, display, xauthority=None, proxy):
        if not fingerprint_path.exists():
            fingerprint_path.write_text(json.dumps({"navigator.platform": "Win32", "window.outerWidth": 1600,
                                                    "window.outerHeight": 856}))
        return await super().launch(spec, profile_dir=profile_dir, fingerprint_path=fingerprint_path,
                                    display=display, xauthority=xauthority, proxy=proxy)

    def window_size(self, fingerprint_path):
        return window_size_of(json.loads(fingerprint_path.read_text())) if fingerprint_path.exists() else None


async def test_the_display_is_made_the_size_of_the_browser_window(tmp_path):
    t = build(tmp_path, engine=WindowEngine())
    displays = t.mgr.displays = SizedDisplays(t.config)
    await t.mgr.create(LaneSpec(id="a/one"))
    info = await t.mgr.open("a/one")
    # first launch: the window size is known once the pin exists, so the display is made again at it
    assert displays.acquired == [("a/one", None), ("a/one", (1600, 856))]
    assert len(t.engine.launches) == 2 and t.engine.running == {"a/one"}
    assert (info.viewer.width, info.viewer.height) == (1600, 856)
    await t.mgr.close("a/one")
    await t.mgr.open("a/one")  # pinned: the right size at once, one launch
    assert displays.acquired[-1] == ("a/one", (1600, 856)) and len(t.engine.launches) == 3


class ScreenEngine(WindowEngine):
    """Also says the display is the pinned screen (the engine's display_size)."""

    def display_size(self, fingerprint_path):
        return (1600, 900) if fingerprint_path.exists() else None


async def test_the_display_is_made_the_pinned_screen_when_the_engine_says_so(tmp_path):
    t = build(tmp_path, engine=ScreenEngine())
    displays = t.mgr.displays = SizedDisplays(t.config)
    await t.mgr.create(LaneSpec(id="a/one"))
    info = await t.mgr.open("a/one")
    assert displays.acquired == [("a/one", None), ("a/one", (1600, 900))]
    assert (info.viewer.width, info.viewer.height) == (1600, 900)


def test_window_size_of_a_pin():
    assert window_size_of({"window.outerWidth": 1908, "window.outerHeight": 1023}) == (1908, 1023)
    assert window_size_of({"window.outerWidth": 1280, "window.outerHeight": 720,
                           "window.devicePixelRatio": 1.5}) == (1920, 1080)
    assert window_size_of({"navigator.platform": "Win32"}) is None
    assert window_size_of({"window.outerWidth": "x", "window.outerHeight": 1}) is None


# ---- delete and set-proxy -------------------------------------------------------------


async def test_delete_moves_a_cold_lane_to_the_trash(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    (t.config.profile_dir("a/one") / "cookies.sqlite").write_bytes(b"logged in")
    await t.mgr.open("a/one")
    with pytest.raises(InvalidRequest, match="close it first"):
        await t.mgr.delete("a/one")
    assert t.mgr.get("a/one").state == LaneState.WARM
    await t.mgr.close("a/one")

    trash = await t.mgr.delete("a/one")
    assert trash.parent == tmp_path / "trash" and trash.name.startswith("a--one--20261104T")
    assert (trash / "profile" / "cookies.sqlite").read_bytes() == b"logged in"
    assert not t.config.lane_dir("a/one").exists()
    with pytest.raises(LaneNotFound):
        t.mgr.get("a/one")
    assert t.mgr.events("a/one")[0].kind == "deleted"
    await t.mgr.create(LaneSpec(id="a/one"))  # a new lane by the same name starts empty
    assert list(t.config.profile_dir("a/one").iterdir()) == []
    second = await t.mgr.delete("a/one")
    assert second != trash and second.exists() and trash.exists()
    with pytest.raises(LaneNotFound):
        await t.mgr.delete("a/one")


async def test_delete_removes_a_lane_whose_record_cannot_be_read(tmp_path):
    import sqlite3

    t = build(tmp_path)
    with sqlite3.connect(t.config.db_path) as conn:
        conn.execute("INSERT INTO lanes (id, spec) VALUES (?, ?)", ("b/bad", '{"id": "b/bad", "kind": "martian"}'))
    with pytest.raises(InvalidRequest, match="agentos lane delete b/bad"):
        t.mgr.get("b/bad")
    assert await t.mgr.delete("b/bad") is None
    assert not t.mgr.store.exists("b/bad")


async def test_set_proxy_moves_a_cold_lane_and_records_it(tmp_path):
    t = build(tmp_path, self_check=True)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.open("a/one")
    assert t.mgr.get("a/one").exit_ip == "203.0.113.7"
    with pytest.raises(InvalidRequest, match="close it first"):
        await t.mgr.set_proxy("a/one", "lt1")
    await t.mgr.close("a/one")
    with pytest.raises(InvalidRequest, match="unknown proxy"):
        await t.mgr.set_proxy("a/one", "nope")
    with pytest.raises(ConfirmRequired, match="new IP address"):  # it has launched: its site knows its IP
        await t.mgr.set_proxy("a/one", "lt1", by="owner")
    assert t.mgr.get("a/one").spec.proxy_label is None
    info = await t.mgr.set_proxy("a/one", "lt1", confirm=True, by="owner")
    assert info.spec.proxy_label == "lt1" and info.exit_ip is None
    event = t.mgr.events("a/one")[0]
    assert event.kind == "proxy_changed" and event.detail == {"from": None, "to": "lt1", "by": "owner"}
    assert (await t.mgr.set_proxy("a/one", None, confirm=True)).spec.proxy_label is None


async def test_set_proxy_on_a_lane_that_never_launched_needs_no_yes(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    assert (await t.mgr.set_proxy("a/one", "lt1")).spec.proxy_label == "lt1"


# ---- a human watching or typing is never idle (AUD-16) -----------------------------------


async def test_a_lane_with_a_live_view_open_is_never_reaped_or_evicted(tmp_path):
    t = build(tmp_path, max_warm_lanes=1, busy_timeout=0.2, idle_seconds=600)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.create(LaneSpec(id="a/two"))
    await t.mgr.open("a/one")
    t.mgr.viewer_attached("a/one")  # the human is hunting for a 2FA code in the viewer
    t.clock.advance(3600)
    assert await t.mgr.reap_idle() == []
    with pytest.raises(LaneBusy):
        await t.mgr.open("a/two")  # the only warm lane is being watched: not evicted
    t.mgr.viewer_detached("a/one")
    t.mgr.viewer_detached("a/one")  # an extra detach never goes below zero
    t.clock.advance(601)
    assert await t.mgr.reap_idle() == ["a/one"]


async def test_recent_human_input_keeps_a_lane_from_eviction(tmp_path):
    t = build(tmp_path, max_warm_lanes=1, busy_timeout=0.2)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.create(LaneSpec(id="a/two"))
    await t.mgr.open("a/one")
    t.mgr.note_human_input("a/one")  # typed in the viewer, control already back with agents
    with pytest.raises(LaneBusy):
        await t.mgr.open("a/two")
    t.clock.advance(601)
    assert (await t.mgr.open("a/two")).state == LaneState.WARM


async def test_shutdown_during_the_memory_scope_leaves_the_lane_cold(tmp_path, monkeypatch):
    from test_lanes_edge import _ShortLaneWaits

    from agentos.node.lanes import manager as lanes_manager

    monkeypatch.setattr(lanes_manager, "asyncio", _ShortLaneWaits())
    gate, entered = asyncio.Event(), asyncio.Event()

    class Slow(ScopeLimiter):
        async def apply(self, lane_id, pids):
            entered.set()
            await gate.wait()

    t = build(tmp_path, limiter=Slow(512))
    await t.mgr.create(LaneSpec(id="a/one"))
    opening = asyncio.create_task(t.mgr.open("a/one"))
    await entered.wait()
    await t.mgr.shutdown()  # gives up waiting for the open and tears the lane down
    gate.set()
    info = await opening
    assert info.state == LaneState.COLD and not t.engine.running
    lane = t.mgr._lanes["a/one"]
    assert lane.session is None and lane.viewer is None


def test_new_lanes_draw_the_configured_os_and_a_bad_os_is_refused(tmp_path):
    from agentos.core.config import NodeConfig
    from agentos.node.lanes import build_lane_manager

    # the owner's decision (2026-11-04): lanes made from now on claim Linux, the node's own OS
    assert NodeConfig(home=tmp_path).lane_os == ["linux"]
    assert build_lane_manager(NodeConfig(home=tmp_path)).engine.identity_os == ["linux"]
    assert build_lane_manager(NodeConfig(home=tmp_path / "win", lane_os=["windows"])).engine.identity_os == ["windows"]
    assert build_lane_manager(NodeConfig(home=tmp_path / "mix", lane_os=[])).engine.identity_os is None
    with pytest.raises(ValueError, match="lane_os may list only windows, macos and linux"):
        NodeConfig(home=tmp_path, lane_os=["android"])
    # MemoryHigh sits at 90% of the cap: under 1.5 GB it throttles a logged-in feed (0.8 to 1 GB warm)
    assert NodeConfig(home=tmp_path, lane_memory_max_mb=0).lane_memory_max_mb == 0  # no cap
    with pytest.raises(ValueError, match="lane_memory_max_mb must be 0 .no cap. or at least 1536"):
        NodeConfig(home=tmp_path, lane_memory_max_mb=1024)
    assert NodeConfig(home=tmp_path, lane_memory_max_mb=1536).lane_memory_max_mb == 1536
    with pytest.raises(ValueError, match="lane_cpu_max_pct must be at least 10"):
        NodeConfig(home=tmp_path, lane_cpu_max_pct=5)


def _cgroups(tmp_path, own: str, caps: dict[str, dict[str, str]]):
    """A fake /sys/fs/cgroup with `own` (the daemon's cgroup) and its parents, every level uncapped
    unless `caps` says otherwise; and the daemon's /proc/self/cgroup."""
    root = tmp_path / "cgroup"
    level = root / own
    while level != root:
        level.mkdir(parents=True, exist_ok=True)
        rel = str(level.relative_to(root))
        for name in ("memory.max", "memory.high"):
            (level / name).write_text(caps.get(rel, {}).get(name, "max") + "\n")
        level = level.parent
    proc = tmp_path / "proc-self-cgroup"
    proc.write_text(f"0::/{own}\n")
    return {"proc_cgroup": proc, "cgroup_root": root, "euid": 0}


def test_default_scopes_only_where_no_level_above_the_daemon_caps_memory(tmp_path):
    from agentos.node.lanes import default_scopes_fit

    own = "system.slice/agentosd.service"
    assert default_scopes_fit(**_cgroups(tmp_path / "free", own, {}))
    assert not default_scopes_fit(**{**_cgroups(tmp_path / "user", own, {}), "euid": 1000})
    for rel, name in ((own, "memory.max"), (own, "memory.high"), ("system.slice", "memory.max"),
                      ("system.slice", "memory.high")):
        where = _cgroups(tmp_path / f"{rel}-{name}".replace("/", "_"), own, {rel: {name: str(2 * 1024**3)}})
        assert not default_scopes_fit(**where), f"{name} on {rel} was ignored"


def test_default_scopes_stay_off_on_cgroup_v1_or_an_unreadable_file(tmp_path):
    from agentos.node.lanes import default_scopes_fit

    v1 = _cgroups(tmp_path / "v1", "system.slice/agentosd.service", {})
    v1["proc_cgroup"].write_text("12:memory:/system.slice/agentosd.service\n1:name=systemd:/system.slice\n")
    assert not default_scopes_fit(**v1)
    gone = _cgroups(tmp_path / "gone", "system.slice/agentosd.service", {})
    (gone["cgroup_root"] / "system.slice" / "memory.high").unlink()
    assert not default_scopes_fit(**gone)
    assert not default_scopes_fit(**{**gone, "proc_cgroup": tmp_path / "missing"})
