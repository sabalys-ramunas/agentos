"""Lane manager edge cases: races, cancellation, shutdown, imports, control and caps.

Fakes only (no browser, no X). Each test that pins a defect is `xfail(strict=True)` and names
its BUG; when the fix lands the test passes, strict xfail turns that into a failure, and the
marker is removed. Tests without a marker cover behaviour that already works.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import threading
from pathlib import Path

import pytest

from openajar.core.config import NodeConfig
from openajar.core.errors import AgentPaused, EngineError, HumanInControl, InvalidRequest, LaneBusy
from openajar.core.models import LaneSpec, LaneState, ProxyConfig, ViewerInfo
from openajar.node.lanes import LaneManagerImpl
from openajar.node.lanes import manager as lanes_manager
from openajar.node.store import LaneStore

PROXY = ProxyConfig(label="lt1", host="198.51.100.10", port=12323, username="user1", password="hunter2-secret")


# ---- fakes ---------------------------------------------------------------------------


class Page:
    def __init__(self, session: Session):
        self.session = session
        self.url = "about:blank"

    async def evaluate(self, js: str, arg=None):
        ip = self.session.engine.exit_ip
        if isinstance(ip, Exception):
            raise ip
        return ip


class Session:
    def __init__(self, engine: Engine, lane_id: str):
        self.engine = engine
        self.lane_id = lane_id
        self.pid = None
        self.alive = True
        self.closed = False
        self.closing = asyncio.Event()  # set when close() starts, so tests can interrupt it
        self._page = Page(self)

    async def page(self):
        if not self.alive:
            raise EngineError("the browser for this lane is not running")
        return self._page

    async def close(self):
        self.closing.set()
        if self.engine.close_seconds:
            await asyncio.sleep(self.engine.close_seconds)
        self.closed = True
        self.alive = False
        self.engine.running.discard(self.lane_id)


class Engine:
    name = "camoufox"

    def __init__(self, *, exit_ip: str | Exception = "203.0.113.7", close_seconds: float = 0.0,
                 launch_gate: asyncio.Event | None = None, launch_seconds: float = 0.0):
        self.exit_ip = exit_ip
        self.close_seconds = close_seconds
        self.launch_gate = launch_gate  # when set, launch() waits until the test opens it
        self.launch_seconds = launch_seconds
        self.entered = asyncio.Event()  # set when a launch has started
        self.sessions: list[Session] = []
        self.running: set[str] = set()  # lane ids with a live browser
        self.max_running = 0

    async def launch(self, spec, *, profile_dir, fingerprint_path, display, xauthority=None, proxy):
        self.entered.set()
        if self.launch_gate is not None:
            await self.launch_gate.wait()
        if self.launch_seconds:
            await asyncio.sleep(self.launch_seconds)
        if not fingerprint_path.exists():
            fingerprint_path.write_text(json.dumps({"navigator.platform": "Win32"}))
        session = Session(self, spec.id)
        self.sessions.append(session)
        self.running.add(spec.id)
        self.max_running = max(self.max_running, len(self.running))
        return session

    async def sweep_orphans(self, root: Path):
        return []


class Displays:
    def __init__(self, config: NodeConfig):
        self.config = config
        self.slots: dict[str, int] = {}
        self.released: list[str] = []
        self.fail_next: Exception | None = None

    async def acquire(self, lane_id: str, *, size: tuple[int, int] | None = None) -> ViewerInfo:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        if lane_id not in self.slots:
            used = set(self.slots.values())
            self.slots[lane_id] = next(i for i in range(self.config.max_slots) if i not in used)
        return self.get(lane_id)

    async def release(self, lane_id: str) -> None:
        if self.slots.pop(lane_id, None) is not None:
            self.released.append(lane_id)

    async def release_all(self) -> None:
        for lane_id in list(self.slots):
            await self.release(lane_id)

    def get(self, lane_id: str) -> ViewerInfo | None:
        if lane_id not in self.slots:
            return None
        i, c = self.slots[lane_id], self.config
        return ViewerInfo(display=f":{c.display_base + i}", vnc_port=c.vnc_base + i, viewer_port=c.viewer_base + i)

    async def sweep(self):
        return []


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make(tmp_path: Path, *, engine: Engine | None = None, proxies: dict | None = None,
         self_check: bool = False, **cfg):
    defaults = {"max_warm_lanes": 3, "busy_timeout": 1.0, "idle_seconds": 600,
                "display_base": 150, "vnc_base": 5850, "viewer_base": 6150}
    config = NodeConfig(home=tmp_path, self_check=self_check, **{**defaults, **cfg})
    engine = engine or Engine()
    displays = Displays(config)
    clock = Clock()
    loader = (lambda: dict(proxies)) if proxies is not None else (lambda: {"lt1": PROXY})
    mgr = LaneManagerImpl(config, LaneStore(config.db_path), engine, displays, loader,
                          clock=clock, reaper_interval=0.01)
    return mgr, engine, displays, clock


class _ShortLaneWaits:
    """asyncio, except the 30 s wait for an in-flight open during shutdown is cut to 0.2 s."""

    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def wait_for(aw, timeout):
        return await asyncio.wait_for(aw, 0.2 if timeout == 30.0 else timeout)


# ---- ids, config and kinds ------------------------------------------------------------


@pytest.mark.parametrize(("cap", "slots"), [(0, 10), (12, 10)])
def test_config_refuses_a_warm_cap_the_node_cannot_serve(tmp_path, cap, slots):
    with pytest.raises(ValueError):
        NodeConfig(home=tmp_path, max_warm_lanes=cap, max_slots=slots)


async def test_create_refuses_lane_kinds_this_node_cannot_run(tmp_path):
    mgr, *_ = make(tmp_path)
    for kind in ("terminal", "service", "android"):
        with pytest.raises(InvalidRequest):
            await mgr.create(LaneSpec(id=f"a/{kind}", kind=kind))
    assert mgr.list() == []


# ---- reads, time and secrets ----------------------------------------------------------


async def test_last_used_is_timezone_aware_utc(tmp_path):
    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    async with mgr.using("a/one"):
        pass
    last = mgr.get("a/one").last_used
    assert last is not None and last.utcoffset().total_seconds() == 0


async def test_lane_info_never_carries_the_proxy_password(tmp_path):
    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one", proxy_label="lt1"))
    info = await mgr.open("a/one")
    assert "hunter2-secret" not in info.model_dump_json()
    assert "hunter2-secret" not in repr(PROXY)


# ---- self-check ----------------------------------------------------------------------


async def test_exit_ip_matching_is_normalised_for_ipv6_and_whitespace(tmp_path):
    v6 = ProxyConfig(label="lt1", host="2001:DB8::10", port=1080)
    mgr, *_ = make(tmp_path, proxies={"lt1": v6}, self_check=True,
                   engine=Engine(exit_ip="2001:db8::10\n"))
    await mgr.create(LaneSpec(id="a/one", proxy_label="lt1"))
    assert (await mgr.open("a/one")).state == LaneState.WARM


async def test_non_ip_exit_text_quarantines_a_proxied_lane(tmp_path):
    mgr, *_ = make(tmp_path, self_check=True, engine=Engine(exit_ip="<html>Access denied</html>"))
    await mgr.create(LaneSpec(id="a/one", proxy_label="lt1"))
    info = await mgr.open("a/one")
    assert info.state == LaneState.QUARANTINED and "could not read the exit IP" in info.quarantine_reason


# ---- open, close, cancellation and the reaper -----------------------------------------


async def test_cancelled_open_releases_its_display_and_frees_the_lane(tmp_path):
    gate = asyncio.Event()
    mgr, engine, displays, _ = make(tmp_path, engine=Engine(launch_gate=gate))
    await mgr.create(LaneSpec(id="a/one"))
    opening = asyncio.create_task(mgr.open("a/one"))
    await engine.entered.wait()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert mgr.get("a/one").state == LaneState.COLD and not displays.slots
    gate.set()
    assert (await mgr.open("a/one")).state == LaneState.WARM


async def test_close_during_open_waits_then_leaves_the_lane_cold(tmp_path):
    gate = asyncio.Event()
    mgr, engine, displays, _ = make(tmp_path, engine=Engine(launch_gate=gate))
    await mgr.create(LaneSpec(id="a/one"))
    opening = asyncio.create_task(mgr.open("a/one"))
    await engine.entered.wait()
    closing = asyncio.create_task(mgr.close("a/one"))
    await asyncio.sleep(0.02)
    assert not closing.done(), "close() must wait for the open that holds the lane"
    gate.set()
    await opening
    await closing
    assert mgr.get("a/one").state == LaneState.COLD
    assert not engine.running and not displays.slots


async def test_reaper_leaves_a_lane_that_is_still_warming_alone(tmp_path):
    gate = asyncio.Event()
    mgr, engine, _, clock = make(tmp_path, idle_seconds=10, engine=Engine(launch_gate=gate))
    await mgr.create(LaneSpec(id="a/one"))
    opening = asyncio.create_task(mgr.open("a/one"))
    await engine.entered.wait()
    clock.advance(60)
    assert await mgr.reap_idle() == []
    gate.set()
    await opening
    assert mgr.get("a/one").state == LaneState.WARM


async def test_zero_busy_timeout_fails_at_once_when_every_slot_is_busy(tmp_path):
    mgr, *_ = make(tmp_path, max_warm_lanes=1, busy_timeout=0)
    await mgr.create(LaneSpec(id="a/one"))
    await mgr.create(LaneSpec(id="a/two"))
    async with mgr.using("a/one"):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(LaneBusy):
            await mgr.open("a/two")
        assert loop.time() - t0 < 0.5


async def test_mixed_open_use_close_storm_never_exceeds_the_cap(tmp_path):
    mgr, engine, displays, _ = make(tmp_path, max_warm_lanes=2, busy_timeout=10.0,
                                    engine=Engine(launch_seconds=0.002))
    names = [f"a/l{i}" for i in range(6)]
    for name in names:
        await mgr.create(LaneSpec(id=name))
    rng = random.Random(7)

    async def worker():
        for _ in range(12):
            lane = rng.choice(names)
            action = rng.choice(["open", "use", "close"])
            if action == "open":
                await mgr.open(lane)
            elif action == "use":
                async with mgr.using(lane):
                    await asyncio.sleep(0.005)
            else:
                await mgr.close(lane)

    results = await asyncio.gather(*(worker() for _ in range(8)), return_exceptions=True)
    unexpected = [r for r in results if isinstance(r, BaseException) and not isinstance(r, LaneBusy)]
    assert unexpected == []
    assert engine.max_running <= 2
    await mgr.shutdown()
    assert not engine.running and not displays.slots
    assert {i.state for i in mgr.list()} == {LaneState.COLD}


async def test_failed_display_acquire_leaves_the_lane_cold_and_retryable(tmp_path):
    mgr, engine, displays, _ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    displays.fail_next = EngineError("no free display slot")
    with pytest.raises(EngineError, match="no free display slot"):
        await mgr.open("a/one")
    assert mgr.get("a/one").state == LaneState.COLD and not engine.running
    assert (await mgr.open("a/one")).state == LaneState.WARM


async def test_cancelled_close_does_not_wedge_the_lane(tmp_path):
    mgr, engine, displays, _ = make(tmp_path, engine=Engine(close_seconds=5))
    await mgr.create(LaneSpec(id="a/one"))
    await mgr.open("a/one")
    closing = asyncio.create_task(mgr.close("a/one"))
    await engine.sessions[0].closing.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert mgr.get("a/one").state == LaneState.COLD
    assert not displays.slots
    async with mgr.using("a/one") as page:
        assert page is not None


async def test_shutdown_while_the_reaper_closes_a_lane_still_closes_its_browser(tmp_path):
    mgr, engine, displays, clock = make(tmp_path, idle_seconds=10, engine=Engine(close_seconds=0.3))
    await mgr.create(LaneSpec(id="a/one"))
    await mgr.open("a/one")
    session = engine.sessions[0]
    await mgr.start()
    clock.advance(11)
    await asyncio.wait_for(session.closing.wait(), 5)  # the reaper is inside close() now
    await mgr.shutdown()
    assert session.closed, "shutdown cut the browser's close short; the browser may still be running"
    assert mgr.get("a/one").state == LaneState.COLD and not displays.slots


async def test_shutdown_during_warming_ends_the_open_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(lanes_manager, "asyncio", _ShortLaneWaits())
    gate = asyncio.Event()
    mgr, engine, _displays, _ = make(tmp_path, engine=Engine(launch_gate=gate))
    await mgr.create(LaneSpec(id="a/one"))
    opening = asyncio.create_task(mgr.open("a/one"))
    await engine.entered.wait()
    await mgr.shutdown()  # the launch is still running: shutdown gives up on it and tears the lane down
    gate.set()
    info = await opening  # shutdown won, so the open should end as a cold lane with no browser
    assert info.state == LaneState.COLD and not engine.running


async def test_shutdown_waits_for_a_tool_call_that_is_using_the_lane(tmp_path):
    mgr, engine, _, _ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    entered = asyncio.Event()

    async def call() -> bool:
        async with mgr.using("a/one"):
            entered.set()
            await asyncio.sleep(0.2)  # a click or a form submit still in flight
            return engine.sessions[0].closed

    task = asyncio.create_task(call())
    await entered.wait()
    await mgr.shutdown()
    assert await task is False, "the browser was closed under a tool call that was still running"


# ---- import --------------------------------------------------------------------------


def _profile(root: Path, *, lock: str | None = None, outside: Path | None = None) -> Path:
    profile = root / "src-profile"
    (profile / "storage").mkdir(parents=True)
    (profile / "prefs.js").write_text('user_pref("x", 1);')
    (profile / "cookies.sqlite").write_bytes(b"SQLite format 3\0")
    if lock is not None:
        os.symlink(lock, profile / "lock")
    if outside is not None:
        os.symlink(outside, profile / "logins.json")
    for p in profile.rglob("*"):
        if not p.is_symlink():
            p.chmod(0o755 if p.is_dir() else 0o644)
    return profile


async def test_import_refuses_a_profile_locked_by_a_live_process(tmp_path):
    mgr, *_ = make(tmp_path / "home")
    profile = _profile(tmp_path, lock=f"127.0.0.1:+{os.getpid()}")  # this test process is alive
    with pytest.raises(InvalidRequest):
        await mgr.import_profile(LaneSpec(id="a/b"), profile, None)
    assert not mgr.config.profile_dir("a/b").exists()


async def test_import_does_not_keep_symlinks_that_point_outside_the_profile(tmp_path):
    mgr, *_ = make(tmp_path / "home")
    secret = tmp_path / "host-secret.txt"
    secret.write_text("not for the lane")
    profile = _profile(tmp_path, outside=secret)
    try:
        await mgr.import_profile(LaneSpec(id="a/b"), profile, None)
    except InvalidRequest:
        return  # refusing the profile is also a valid outcome
    dest = mgr.config.profile_dir("a/b").resolve()
    escaping = [p for p in dest.rglob("*") if p.is_symlink() and not p.resolve().is_relative_to(dest)]
    assert escaping == [], f"the imported profile points outside itself: {escaping}"


async def test_duplicate_import_does_not_delete_the_profile_of_the_first(tmp_path, monkeypatch):
    mgr, *_ = make(tmp_path / "home")
    profile = _profile(tmp_path)
    real_copytree = shutil.copytree
    gate = threading.Event()
    calls: list[Path] = []

    def copytree(src, dst, *args, **kwargs):
        calls.append(Path(dst))
        if len(calls) == 1:
            gate.wait(10)  # the first import is slow to start copying
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", copytree)
    spec = LaneSpec(id="a/b")
    first = asyncio.create_task(mgr.import_profile(spec, profile, None))
    while not calls:
        await asyncio.sleep(0.01)
    await mgr.import_profile(spec, profile, None)  # the second import completes first
    gate.set()
    with pytest.raises(OSError):  # the first import must fail, not win
        await first
    assert (mgr.config.profile_dir("a/b") / "prefs.js").is_file(), \
        "the failed first import deleted the profile the second import copied"


async def test_import_refuses_a_source_that_contains_its_destination(tmp_path):
    mgr, *_ = make(tmp_path / "home")
    lanes = tmp_path / "home" / "lanes"
    lanes.mkdir(parents=True)
    (lanes / "note.txt").write_text("x")
    with pytest.raises(InvalidRequest):
        await mgr.import_profile(LaneSpec(id="a/b"), lanes, None)


# ---- control (take over, hand back, stop) ----------------------------------------------


async def test_take_control_blocks_agent_calls(tmp_path):
    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    await mgr.take_control("a/one", by="owner")
    with pytest.raises(HumanInControl):
        async with mgr.using("a/one", principal_kind="agent"):
            pass


async def test_stop_agent_pauses_agent_calls_on_the_lane(tmp_path):
    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    await mgr.stop_agent("a/one", by="owner")
    with pytest.raises(AgentPaused):
        async with mgr.using("a/one", principal_kind="agent"):
            pass
