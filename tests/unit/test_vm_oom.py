"""What the kernel killed for memory on a VM, and the other field-report fixes of lane_exec and
lane_job_read (2026-11-16): a job kept saying `running` after the kernel OOM-killed its 4.1 GB child,
`pkill -f` killed its own shell with no word why, and waiting for a dev server took sleep loops.

The kernel lines below are real ones (ubuntu:24.04 in docker, `head -c 2000m /dev/zero | tail` under a
300 MB limit), with a session scope put in as task_memcg the way a VM's sshd session has one."""

from __future__ import annotations

import asyncio

import pytest
from test_env_jobs import Ctx, tools

from agentos.core.errors import InvalidRequest
from agentos.core.runtime import Runtime
from agentos.env import lane_jobs, oom
from agentos.env.host import FakeHost
from agentos.env.jobs import Job
from agentos.env.lanes import LaneRuntime
from agentos.env.manager import EnvManager
from agentos.env.models import ExecResult
from agentos.env.paths import EnvSettings
from agentos.env.sandbox import DirectLauncher, Workload
from agentos.env.tools import self_kill_note
from agentos.environments import machine, scripts
from agentos.vms import ssh

SESSION = "/user.slice/user-0.slice/session-7.scope"
KERNEL = f"""U 211647.09
C {SESSION}
N 211700.00 1794814685
[211637.544472] oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,mems_allowed=0,global_oom,task_memcg=/user.slice/user-0.slice/session-3.scope,task=tail,pid=77068,uid=0
[211637.544826] Out of memory: Killed process 77068 (tail) total-vm:307308kB, anon-rss:305024kB, file-rss:1172kB, shmem-rss:0kB, UID:0 pgtables:636kB oom_score_adj:0
[211652.583555] oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,mems_allowed=0,global_oom,task_memcg={SESSION},task=node,pid=4242,uid=0
[211652.583593] Out of memory: Killed process 4242 (node) total-vm:9307440kB, anon-rss:4200000kB, file-rss:1172kB, shmem-rss:0kB, UID:0 pgtables:9636kB oom_score_adj:0
[211660.100000] oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,mems_allowed=0,global_oom,task_memcg=/system.slice/docker-ab.scope,task=postgres,pid=901,uid=999
[211660.100100] Out of memory: Killed process 901 (postgres) total-vm:507440kB, anon-rss:300000kB, file-rss:2000kB, shmem-rss:5000kB, UID:999 pgtables:636kB oom_score_adj:0
"""


def test_the_kills_since_the_start_with_whose_they_are():
    kills = oom.parse(KERNEL, elapsed_s=999)
    assert [k["pid"] for k in kills] == [4242, 901]          # the kill before the start is not this job's
    node, pg = kills
    assert node == {"pid": 4242, "name": "node", "rss_mb": 4103, "at": "2026-11-16T07:37:17Z", "in_job": True}
    assert pg["in_job"] is False and pg["rss_mb"] == 300


def test_without_a_session_scope_the_kills_are_listed_and_whose_is_unknown():
    text = KERNEL.replace(f"C {SESSION}", "C /")               # a container's cgroup, or no accounting
    assert {k["in_job"] for k in oom.parse(text, elapsed_s=999)} == {None}
    # no start record (the shell cleaned up after a success): the node's own count sets the start
    no_start = "\n".join(line for line in KERNEL.splitlines() if not line.startswith(("U ", "C ")))
    assert [k["pid"] for k in oom.parse(no_start, elapsed_s=45)] == [4242, 901]
    assert [k["pid"] for k in oom.parse(no_start, elapsed_s=41)] == [901]
    assert oom.parse("dmesg: read kernel buffer failed: Operation not permitted", elapsed_s=5) == []


def test_the_note_names_the_victim_and_what_to_do():
    kills = oom.parse(KERNEL, elapsed_s=999)
    running = oom.note(kills, running=True)
    assert running.startswith("The kernel killed pid 4242 (node, 4.0 GB) of this job at 07:37:17 UTC: out of memory")
    assert "pid 901 (postgres, 300 MB), not of this job" in running
    assert "lane_job_cancel" in running and "bigger size" in running
    done = oom.note(kills[:1], running=False, what="command")
    assert "of this command" in done and "lane_job_cancel" not in done
    assert oom.note(None, running=True) is None
    for text in (running, done):
        assert "—" not in text


def test_merge_keeps_each_kill_once():
    kills = oom.parse(KERNEL, elapsed_s=999)
    assert oom.merge(kills[:1], kills) == kills
    assert oom.merge(None, []) == []


def test_the_workload_shell_writes_its_start_record_only_when_asked():
    plain = ssh.remote_command(Workload(wid="j-a1", argv=["true"], env={}))
    assert ".start" not in plain
    cmd = ssh.remote_command(Workload(wid="j-a1", argv=["true"], env={}, oom_record=True))
    assert "/tmp/agentos-wl/j-a1.start" in cmd and "/proc/self/cgroup" in cmd
    assert "[ $rc -ne 0 ] || rm -f /tmp/agentos-wl/j-a1.start" in cmd   # kept after a failure, for the node
    probe = oom.probe_script("/tmp/agentos-wl", "j-a1", final=True)
    assert "dmesg" in probe and "rm -f /tmp/agentos-wl/j-a1.start" in probe
    assert "rm -f /tmp/agentos-wl/j-a1.start" not in oom.probe_script("/tmp/agentos-wl", "j-a1", final=False)


def _res(**kw) -> ExecResult:
    base = {"exit_code": 255, "stdout": "", "stderr": "", "duration_s": 0.1}
    return ExecResult(**{**base, **kw})


def test_pkill_f_that_killed_its_own_shell_says_so():
    note = self_kill_note("pkill -f vite; pnpm dev", _res())
    assert "matched its own shell" in note and "pkill -f '[v]ite'" in note and "lane_job_cancel" in note
    assert self_kill_note("pkill --full 'next dev'", _res(exit_code=None, signal="SIGTERM"))
    assert self_kill_note("kill $(pgrep -f vite)", _res(exit_code=143))
    assert self_kill_note("pkill -f vite", _res(exit_code=1)) is None           # nothing matched: no news
    assert self_kill_note("pkill -f vite", _res(stdout="x", stdout_bytes=1)) is None
    assert self_kill_note("pkill node", _res()) is None                         # by name: the shell is safe
    assert "—" not in note


def test_swap_words_for_the_machine_notes_and_the_snapshot_scrub():
    assert machine.swap_words("swap: 8192 MB zram") == "8 GB, compressed in memory (zram)"
    assert machine.swap_words("swap: 3800 MB file") == "3.7 GB swapfile"
    assert machine.swap_words("swap: 2048 MB") == "2 GB"
    assert machine.swap_words("swap: none (a container)") == "none (a container)"
    assert machine.swap_words("") is None
    assert machine.SWAP_OFF in scripts.SCRUB and scripts.SCRUB.endswith("sync; echo ok=1")
    assert "fstab" not in machine.SWAP_ON   # made at every boot, never written into the image


def test_the_port_wait_reads_listen_sockets_and_has_its_own_deadline():
    script = lane_jobs.port_wait_script(3000, 90)
    assert "0BB8" in script or "printf '%04X' 3000" in script
    assert "/proc/net/tcp6" in script and "+ 90" in script


# ------------------------------------------------------------------ through the tools, on this computer

@pytest.fixture
async def env(tmp_path):
    mgr = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0, max_timeout_s=30))
    mgr.create("dev/run", preset="sandbox", overrides={"filesystem": "overlay"})
    yield Runtime(envs=mgr)
    await mgr.shutdown()



async def test_until_port_returns_when_the_port_listens(env, monkeypatch):
    monkeypatch.setattr(lane_jobs, "port_wait_script", lambda port, secs: "sleep 0.2; echo listening")
    t, ctx = tools(env), Ctx(env)
    job = await t["lane_job_start"](ctx, on="dev/run", cmd="echo up; sleep 20")
    r = await t["lane_job_read"](ctx, job=job["id"], until_port=3000, wait_ms=10000)
    assert r["listening"] is True and r["state"] == "running"
    await t["lane_job_cancel"](ctx, job=job["id"])


async def test_until_port_gives_the_end_of_the_output_when_the_job_ends_first(env, monkeypatch):
    monkeypatch.setattr(lane_jobs, "port_wait_script", lambda port, secs: f"sleep {secs}; echo waited")
    t, ctx = tools(env), Ctx(env)
    job = await t["lane_job_start"](ctx, on="dev/run", cmd="seq 1 5000; echo boom >&2; exit 3")
    r = await t["lane_job_read"](ctx, job=job["id"], until_port=3000, wait_ms=10000, max_bytes=100)
    assert r["listening"] is False and r["state"] == "exited" and r["exit_code"] == 3
    assert r["stdout"].endswith("5000\n") and "boom" in r["stderr"]
    assert r["next_stdout_offset"] == r["stdout_bytes"]
    assert "ended before port 3000 listened" in r["note"]
    with pytest.raises(InvalidRequest):
        await t["lane_job_read"](ctx, job=job["id"], until_port=70000)


async def test_a_read_returns_early_when_the_kernel_killed_one_of_the_jobs_processes(env, monkeypatch):
    kills = oom.parse(KERNEL, elapsed_s=999)[:1]

    async def fake(self, wid, *, elapsed_s, final=False):
        await asyncio.sleep(0.05)
        return kills

    monkeypatch.setattr(Job, "on_vm", property(lambda self: self.rt is not None))
    monkeypatch.setattr(LaneRuntime, "oom_kills", fake)
    t, ctx = tools(env), Ctx(env)
    job = await t["lane_job_start"](ctx, on="dev/run", cmd="sleep 20")
    loop = asyncio.get_running_loop()
    start = loop.time()
    r = await t["lane_job_read"](ctx, job=job["id"], until_exit=True, wait_ms=60000)
    assert loop.time() - start < 5
    assert r["state"] == "running" and r["oom_killed"] is True and r["oom"][0]["pid"] == 4242
    assert "of this job" in r["note"] and "lane_job_cancel" in r["note"]
    await t["lane_job_cancel"](ctx, job=job["id"])
