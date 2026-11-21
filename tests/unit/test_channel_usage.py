"""Heartbeat lanes: `cpu_pct` and `mem_mb` from /proc and cgroups, left out when unknown."""

import threading
from types import SimpleNamespace

from channel_fake_controller import NODE_ID, FakeController, node_pubkey, node_runtime, start, stop

from openajar.node.channel import bridge
from openajar.node.channel import usage as usage_module
from openajar.node.channel.usage import LaneUsage


def proc(tmp_path, processes):
    """A fake /proc: {pid: (ppid, utime+stime ticks, resident pages, name[, start time])}."""
    root = tmp_path / "proc"
    root.mkdir(exist_ok=True)
    for pid, (ppid, ticks, pages, name, *started) in processes.items():
        d = root / str(pid)
        d.mkdir(exist_ok=True)
        # pid (name) state ppid ... field 14 utime, 15 stime, 22 start time
        rest = ["S", str(ppid)] + ["0"] * 9 + [str(ticks), "0"] + ["0"] * 6 + [str(started[0] if started else 0)]
        rest += ["0"] * 3
        (d / "stat").write_text(f"{pid} ({name}) " + " ".join(rest))
        (d / "statm").write_text(f"1000 {pages} 0 0 0 0 0")
    (root / "self").mkdir(exist_ok=True)
    return root


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def runtime(lanes=None, envs=None):
    return SimpleNamespace(get=lambda name: {"lanes": lanes, "envs": envs}.get(name))


def test_a_browser_lane_counts_its_whole_process_tree(tmp_path):
    clock = Clock()
    lanes = SimpleNamespace(lane_pids=lambda: {"biz-a/social": [10], "biz-b/gone": [999]})
    root = proc(tmp_path, {10: (1, 100, 25600, "camoufox"), 11: (10, 50, 25600, "Web Content (tab) )x"),
                           12: (11, 50, 256, "child"), 20: (1, 900, 99999, "unrelated")})
    usage = LaneUsage(proc=root, clock=clock)
    usage.hz = 100
    usage.page = 4096
    first = usage.sample(runtime(lanes))
    assert first == {"biz-a/social": {"mem_mb": 201}}  # no cpu_pct until there is an interval
    root = proc(tmp_path, {10: (1, 150, 25600, "camoufox"), 11: (10, 100, 25600, "Web Content (tab) )x"),
                           12: (11, 50, 256, "child")})
    clock.now += 10
    second = usage.sample(runtime(lanes))
    assert second == {"biz-a/social": {"mem_mb": 201, "cpu_pct": 10.0}}  # 1 s of cpu in 10 s


def test_without_proc_or_pids_a_lane_has_no_numbers(tmp_path):
    usage = LaneUsage(proc=tmp_path / "missing")
    assert usage.sample(runtime(SimpleNamespace(lane_pids=lambda: {"biz-a/social": [10]}))) == {}
    assert usage.sample(runtime(SimpleNamespace())) == {}
    broken = SimpleNamespace(lane_pids=lambda: 1 / 0)
    assert usage.sample(runtime(broken)) == {}


def test_an_env_lane_reports_its_cgroups_memory_when_warm(tmp_path):
    warm = SimpleNamespace(lane=SimpleNamespace(id="dev/box", key="dev--box"), state=SimpleNamespace(value="warm"))
    cold = SimpleNamespace(lane=SimpleNamespace(id="dev/api", key="dev--api"), state=SimpleNamespace(value="cold"))
    envs = SimpleNamespace(list=lambda: [warm, cold],
                           host=SimpleNamespace(cgroup_stat=lambda key: {"memory_mb": 300.4, "pids": 3}))
    assert LaneUsage(proc=tmp_path / "missing").sample(runtime(envs=envs)) == {"dev/box": {"mem_mb": 300}}


def test_heartbeat_lanes_carry_usage_only_when_known():
    lane = {"id": "biz-a/social", "kind": "browser", "state": "warm", "signal": "ok", "last_used": None}
    assert bridge.heartbeat_lane(lane) == lane
    assert bridge.heartbeat_lane(lane, {"cpu_pct": 12.5, "mem_mb": 400}) == {**lane, "cpu_pct": 12.5, "mem_mb": 400}
    assert bridge.heartbeat_lane(lane, {"cpu_pct": None, "mem_mb": -1}) == lane
    assert bridge.heartbeat_lane(lane, {"cpu_pct": True}) == lane


def test_one_lane_that_fails_does_not_blank_the_others(tmp_path, monkeypatch):
    lanes = SimpleNamespace(lane_pids=lambda: {"biz-a/social": [10], "biz-b/broken": [20]})
    root = proc(tmp_path, {10: (1, 100, 25600, "camoufox"), 20: (1, 100, 25600, "camoufox")})
    real = usage_module._descendants

    def flaky(children, table, roots):
        if 20 in roots:
            raise RuntimeError("a lane whose processes cannot be walked")
        return real(children, table, roots)

    monkeypatch.setattr(usage_module, "_descendants", flaky)
    usage = LaneUsage(proc=root, clock=Clock())
    usage.page = 4096
    assert usage.sample(runtime(lanes)) == {"biz-a/social": {"mem_mb": 100}}


def test_a_reused_pid_is_not_credited_to_a_lane_whose_browser_exited(tmp_path):
    clock = Clock()
    lanes = SimpleNamespace(lane_pids=lambda: {"biz-a/social": [10]})
    root = proc(tmp_path, {10: (1, 100, 25600, "camoufox", 500)})
    usage = LaneUsage(proc=root, clock=clock)
    usage.page = 4096
    assert usage.sample(runtime(lanes)) == {"biz-a/social": {"mem_mb": 100}}
    # the browser exited and its pid now belongs to something else, started later
    root = proc(tmp_path, {10: (1, 9000, 99999, "postgres", 900)})
    clock.now += 10
    assert usage.sample(runtime(lanes)) == {}


async def test_the_heartbeat_walks_proc_off_the_event_loop(tmp_path, monkeypatch):
    threads = []
    monkeypatch.setattr(LaneUsage, "sample", lambda self, rt: threads.append(threading.current_thread()) or {})
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        await start(runtime)
        peer = await fake.peer()
        await peer.recv("heartbeat")
        await stop(runtime)
    assert threads and threading.main_thread() not in threads
