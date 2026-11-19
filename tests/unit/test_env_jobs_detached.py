"""A job on a VM survives a restart of the agentos service (field report 2026-11-16: an upgrade restarted
the service, its ssh sessions logged out, and every dev server on the VM died). The job runs detached on
the machine with its output in files there; the node follows those files and, after a restart, follows
them again from the offsets it has. Here the "VM" is this computer's shell: the launcher runs the very
scripts the ssh would (agentos.vms.ssh), with a stand-in for util-linux setsid where there is none (macOS).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager
from agentos.env.models import RemoteHost
from agentos.env.paths import EnvSettings
from agentos.env.sandbox import DirectLauncher
from agentos.vms import ssh

SETSID = '''#!{py}
import os, sys
args = [a for a in sys.argv[1:] if a != "-w"]
pid = os.fork()
if pid == 0:
    os.setsid()
    os.execvp(args[0], args)
_, status = os.waitpid(pid, 0)
sys.exit(os.waitstatus_to_exitcode(status))
'''
LANE = "env/vm1"


class ShLauncher(ssh.SshLauncher):
    """SshLauncher whose "ssh" is a local shell."""

    def __init__(self, path: str):
        self.path = path

    def transport(self, lane, script, *, tty=False, quiet=False):
        return ["/usr/bin/env", f"PATH={self.path}", "sh", "-c", script]

    async def run_script(self, lane, script, *, timeout_s):
        proc = await asyncio.create_subprocess_exec("sh", "-s", stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                    env={"PATH": self.path}, start_new_session=True)
        out, _ = await asyncio.wait_for(proc.communicate(script.encode()), timeout_s)
        return proc.returncode or 0, out.decode(errors="replace")


@pytest.fixture
def vm(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if shutil.which("setsid") is None:
        (bin_dir / "setsid").write_text(SETSID.format(py=sys.executable))
        (bin_dir / "setsid").chmod(0o755)
    wl = tmp_path / "wl"
    monkeypatch.setattr(ssh, "PGID_DIR", str(wl))
    path = f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin"
    made: list[EnvManager] = []

    def manager() -> EnvManager:
        mgr = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                         settings=EnvSettings(proxy_port=0, max_timeout_s=60))
        mgr.remote_launcher = ShLauncher(path)
        if not mgr.has(LANE):
            mgr.create_remote(LANE, RemoteHost(vm_id="vm_1", host="127.0.0.1", key_path="/k", known_hosts="/kh",
                                               host_alias="vm1", provider="docker"))
        made.append(mgr)
        return mgr

    yield manager, wl, tmp_path
    for mgr in made:
        for job in mgr.jobs.jobs.values():  # nothing of a test outlives it
            if job.info.vm_wid:
                pgid = wl / f"{job.info.vm_wid}.pgid"
                if pgid.exists():
                    with _quiet():
                        os.killpg(int(pgid.read_text().strip()), 9)


class _quiet:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


async def read_until(mgr, job_id, want: bytes, offsets=(0, 0), timeout=10.0):
    out, err = b"", b""
    so, se = offsets
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while want not in out + err:
        info, o, e = await mgr.job_read(job_id, stdout_offset=so, stderr_offset=se, wait_s=0.5)
        out, err, so, se = out + o, err + e, so + len(o), se + len(e)
        if info.state != "running" and not o and not e:
            break
        assert loop.time() < end, f"never saw {want!r}: {out!r} {err!r}"
    return out, err, (so, se)


async def wait_state(mgr, job_id, timeout=15.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        info, _, _ = await mgr.job_read(job_id, wait_s=0.5, until_exit=True)
        if info.state != "running":
            return info
        assert loop.time() < end, "the job did not end"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    import subprocess
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], check=False, capture_output=True, text=True).stdout
    return bool(out.strip()) and not out.strip().startswith("Z")


async def gone(pid: int, timeout=5.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while alive(pid):
        if loop.time() > end:
            return False
        await asyncio.sleep(0.1)
    return True


async def test_a_job_on_a_vm_runs_detached_and_ends_with_its_exit_code(vm):
    manager, wl, _ = vm
    mgr = manager()
    await mgr.start()
    info = await mgr.job_start(LANE, "echo out; echo err >&2; exit 3")
    assert info.vm_wid and info.vm_wid.startswith("j-")
    done = await wait_state(mgr, info.id)
    assert done.state == "exited" and done.exit_code == 3
    _, out, err = await mgr.job_read(info.id)
    assert out == b"out\n" and err == b"err\n"
    assert not list(wl.glob(f"{info.vm_wid}.*"))  # its files on the machine went once read
    await mgr.shutdown()


async def test_a_job_survives_a_restart_and_reads_on_from_the_same_offsets(vm):
    manager, _, tmp = vm
    child = tmp / "child.pid"
    mgr = manager()
    await mgr.start()
    info = await mgr.job_start(LANE, f"sleep 300 & echo $! > {child}; echo one; sleep 1.5; echo two; wait")
    _, _, offsets = await read_until(mgr, info.id, b"one")
    await mgr.shutdown()  # an upgrade: the service stops
    vite = int(child.read_text())
    assert alive(vite), "the dev server died with the node's ssh"
    meta = (mgr.paths.jobs(LANE) / f"{info.id}.json").read_text()
    assert '"state":"running"' in meta.replace(" ", "")

    again = manager()
    await again.start()
    more, _, offsets = await read_until(again, info.id, b"two", offsets=offsets)
    assert more == b"two\n", "the read went on from its offset, nothing twice and nothing lost"
    r, _, _ = await again.job_read(info.id)
    assert r.state == "running"
    cancelled = await again.job_cancel(info.id)
    assert cancelled.state == "cancelled"
    assert await gone(vite), "terminal_job_cancel ends the whole process tree after a reattach too"
    await again.shutdown()


async def test_a_timeout_that_passed_while_the_node_was_down_ends_the_job(vm):
    manager, _, tmp = vm
    child = tmp / "child.pid"
    mgr = manager()
    await mgr.start()
    info = await mgr.job_start(LANE, f"sleep 300 & echo $! > {child}; echo up; wait", timeout_s=2)
    await read_until(mgr, info.id, b"up")
    await mgr.shutdown()
    await asyncio.sleep(2.2)
    again = manager()
    await again.start()
    done = await wait_state(again, info.id)
    assert done.state == "timed_out"
    assert await gone(int(child.read_text()))
    await again.shutdown()


async def test_a_dropped_follower_is_followed_again(vm):
    manager, _, _ = vm
    mgr = manager()
    await mgr.start()
    info = await mgr.job_start(LANE, "echo a; sleep 1; echo b; sleep 0.5; echo c")
    out, _, offsets = await read_until(mgr, info.id, b"a")
    job = mgr.jobs.get(info.id)
    os.killpg(job.sp.proc.pid, 9)  # the connection drops
    rest, _, _ = await read_until(mgr, info.id, b"c", offsets=offsets)
    assert out + rest == b"a\nb\nc\n"
    done = await wait_state(mgr, info.id)
    assert done.state == "exited" and done.exit_code == 0
    await mgr.shutdown()


async def test_a_lane_going_cold_ends_its_detached_jobs(vm):
    manager, _, tmp = vm
    child = tmp / "child.pid"
    mgr = manager()
    await mgr.start()
    info = await mgr.job_start(LANE, f"sleep 300 & echo $! > {child}; echo up; wait")
    await read_until(mgr, info.id, b"up")
    await mgr.cool(LANE)  # an environment close
    r, _, _ = await mgr.job_read(info.id)
    assert r.state == "exited" and "stopped while it ran" in r.note
    assert await gone(int(child.read_text()))
    await mgr.shutdown()


async def test_a_machine_that_lost_the_job_while_the_node_was_down_says_lost(vm):
    manager, wl, tmp = vm
    child = tmp / "child.pid"
    mgr = manager()
    await mgr.start()
    info = await mgr.job_start(LANE, f"sleep 300 & echo $! > {child}; echo up; wait")
    await read_until(mgr, info.id, b"up")
    await mgr.shutdown()
    pgid = int((wl / f"{info.vm_wid}.pgid").read_text())
    os.killpg(pgid, 9)  # the machine restarted: its processes and /tmp are gone
    for f in wl.glob(f"{info.vm_wid}.*"):
        f.unlink()
    again = manager()
    await again.start()
    done = await wait_state(again, info.id)
    assert done.state == "lost" and "gone from the machine" in done.note
    await again.shutdown()


async def test_a_job_that_cannot_start_says_why(vm):
    manager, _, _ = vm
    mgr = manager()
    await mgr.start()
    info = await mgr.job_start(LANE, "true", cwd="/no/such/dir")
    done = await wait_state(mgr, info.id)
    assert done.state == "exited" and done.exit_code not in (0, None)
    _, _, err = await mgr.job_read(info.id)
    assert b"/no/such/dir" in err
    await mgr.shutdown()


def test_the_status_lines_parse():
    st = ssh.parse_status("A 0\nR 3 1794820409\nS 2 5\n")
    assert st == {"gone": False, "alive": False, "rc": 3, "ended_at": 1794820409.0, "timed_out": False,
                  "out": 2, "err": 5}
    assert ssh.parse_status("G\n")["gone"] is True
    assert ssh.parse_status("A 1\nT\nS       12        0\n")["timed_out"] is True
    assert ssh.parse_status("") is None and ssh.parse_status("Permission denied") is None


def test_the_follower_is_quiet_and_only_a_job_detaches(vm):
    from agentos.env.sandbox import Workload
    lane = type("L", (), {"remote": RemoteHost(vm_id="vm_1", host="h", key_path="/k", known_hosts="/kh",
                                               host_alias="a")})()
    real = ssh.SshLauncher()
    job = real.command(None, lane, Workload("j-ab12", ["sleep", "1"], {}, detach=(0, 0), detach_start=True,
                                            detach_timeout_s=60), cgroup=None, workdir=Path(vm[2] / "w"))
    assert "LogLevel=QUIET" in job.argv and "setsid sh -c" in job.argv[-1] and "sleep 120" in job.argv[-1]
    follow = real.command(None, lane, Workload("j-ab12", ["true"], {}, detach=(5, 7)), cgroup=None,
                          workdir=Path(vm[2] / "w"))
    assert "setsid" not in follow.argv[-1] and "o=5; e=7" in follow.argv[-1]
    plain = real.command(None, lane, Workload("x-ab12", ["true"], {}), cgroup=None, workdir=Path(vm[2] / "w"))
    assert "LogLevel=ERROR" in plain.argv and "setsid -w" in plain.argv[-1]


async def test_a_read_waiting_for_the_end_says_exited_not_running_with_an_exit_code(vm, monkeypatch):
    """Live on Hetzner (2026-11-16): an exit-0 job whose child was killed for memory. The background
    probe found the kill while the job's end was under way (its exit code set, the machine's files
    still going), which woke terminal_job_read(until_exit=true): the read said state running with
    exit_code 0 and "the job runs on". The read now lets an ending job finish first."""
    from agentos.env import jobs as jobs_mod
    from agentos.env.lanes import LaneRuntime

    kill = {"pid": 4025, "name": "python3", "rss_mb": 3529, "at": "2026-11-16T09:49:30Z", "in_job": True}
    ending = asyncio.Event()

    async def kills(self, wid, *, elapsed_s, final=False):
        return [kill] if ending.is_set() and not final else []  # the background probe finds it mid-end

    async def slow_forget(self, wid):
        ending.set()
        await asyncio.sleep(1.0)  # the machine's files go: the window in which the probe woke the read

    monkeypatch.setattr(jobs_mod, "OOM_EVERY_S", 0.05)
    monkeypatch.setattr(LaneRuntime, "oom_kills", kills)
    monkeypatch.setattr(LaneRuntime, "job_forget", slow_forget)
    manager, _, _ = vm
    mgr = manager()
    await mgr.start()
    info = await mgr.job_start(LANE, "echo 'child exit=137'; exit 0")
    done, _, _ = await mgr.job_read(info.id, wait_s=10, until_exit=True)
    assert done.state == "exited" and done.exit_code == 0, done
    await mgr.shutdown()
