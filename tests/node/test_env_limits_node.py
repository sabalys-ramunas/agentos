# ruff: noqa: F811  (pytest fixtures imported from test_env_node_support)
import asyncio
import subprocess
import time
from pathlib import Path

import pytest
from test_env_node_support import mgr, needs_node  # noqa: F401

pytestmark = [pytest.mark.node, needs_node]


def lane_cg(mgr, lane_id) -> Path:
    return Path(mgr.settings.cgroup_root) / mgr.paths.instance / mgr.get(lane_id).key


async def test_limits_are_written_to_the_lane_cgroup(mgr):
    mgr.create("lim/a", overrides={"memory_mb": 96, "cpus": 0.5, "pids": 40})
    await mgr.warm("lim/a")
    cg = lane_cg(mgr, "lim/a")
    assert (cg / "memory.max").read_text().strip() == str(96 * 1024 * 1024)
    assert (cg / "cpu.max").read_text().strip() == "50000 100000"
    assert (cg / "pids.max").read_text().strip() == "40"


async def test_fork_bomb_is_contained_by_pids_max(mgr):
    mgr.create("lim/bomb", overrides={"pids": 50})
    t = time.monotonic()
    task = asyncio.create_task(mgr.exec("lim/bomb", "bomb() { bomb | bomb & }; bomb; sleep 3", timeout_s=4))
    await asyncio.sleep(1.5)
    # the node stays responsive while the bomb hits its ceiling
    host = await asyncio.to_thread(subprocess.run, ["true"], check=False, timeout=5)
    assert host.returncode == 0
    peak = int((lane_cg(mgr, "lim/bomb") / "pids.peak").read_text())
    assert peak <= 50
    await task
    assert time.monotonic() - t < 15
    assert int((lane_cg(mgr, "lim/bomb") / "pids.current").read_text()) == 0
    r = await mgr.exec("lim/bomb", "echo still-usable")
    assert r.stdout == "still-usable\n"


async def test_memory_limit_kills_only_inside_the_lane(mgr):
    mgr.create("lim/hog", overrides={"memory_mb": 64})
    mgr.create("lim/bystander")
    bystander = asyncio.create_task(mgr.exec("lim/bystander", "sleep 3; echo survived"))
    r = await mgr.exec("lim/hog", "python3 -c 'b = bytearray(400 * 1024 * 1024); print(len(b))'", timeout_s=30)
    assert r.exit_code != 0 and r.oom_killed and r.stdout == ""
    assert (await bystander).stdout == "survived\n"
    r = await mgr.exec("lim/hog", "echo lane-alive")
    assert r.stdout == "lane-alive\n"


@pytest.mark.parametrize("fs", ["overlay", "ephemeral"])
async def test_disk_use_of_the_writable_layer_is_capped_and_reported(mgr, fs):
    lane = f"lim/disk-{fs}"
    mgr.create(lane, overrides={"filesystem": fs, "disk_mb": 64})
    r = await mgr.exec(lane, "dd if=/dev/zero of=/root/big bs=1M count=200 2>&1; echo rc=$?", timeout_s=60)
    assert "No space left on device" in r.stdout and "rc=1" in r.stdout
    info = mgr.info(lane)
    assert info.disk["cap_mb"] <= 64 and info.disk["used_mb"] >= 40
    r = await mgr.exec(lane, "rm /root/big; echo ok > /root/small && cat /root/small")
    assert r.stdout == "ok\n"
