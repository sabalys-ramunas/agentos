"""One shared ssh connection per VM for one-shot commands (agentos.vms.sshmux). A unix socket that
listens stands in for a master here (it says hello on connect, as ssh's does); the real thing was
measured on the docker provider: 146 ms a lane_exec before, about 40 ms after."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from agentos.env.lanes import LaneRuntime, Spawned
from agentos.env.models import RemoteHost
from agentos.env.sandbox import Workload
from agentos.vms import ssh, sshmux

R = RemoteHost(vm_id="vm_1", host="203.0.113.9", key_path="/k/vm_1/id", known_hosts="/k/vm_1/kh",
               host_alias="agentos-vm_1")
HELLO = b"\x00\x00\x00\x05\x00\x00\x00\x01\x00"
MUX_LINES = (b"mux_client_request_session: session request failed: Session open refused by peer\n",
             b"muxclient: master hello exchange failed\n",
             b"Control socket connect(/tmp/mx-none/m): Connection refused\n")


@pytest.fixture
def base():
    d = tempfile.mkdtemp(prefix="mx", dir="/tmp")  # a socket path must be short: no pytest tmp_path
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def mux(base):
    return sshmux.Mux(base)


class listen:
    """A stand-in master: takes each connection and says hello, as ssh's does; a stuck one (`stuck`)
    is accepted by the kernel and says nothing."""

    def __init__(self, path: str, *, stuck: bool = False):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        if not stuck:
            threading.Thread(target=self._take, daemon=True).start()

    def _take(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with contextlib.suppress(OSError):
                conn.sendall(HELLO)
            conn.close()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()


def fake_ssh(base: str, body: str) -> Path:
    """An `ssh` that logs each argv (one file per call) and then runs `body` (sh)."""
    fake = Path(base) / "ssh"
    fake.write_text(f'#!/bin/sh\nn=$(ls {base}/argv.* 2>/dev/null | wc -l | tr -d " ")\n'
                    f'printf "%s\\n" "$@" > {base}/argv.$n\n{body}\n')
    fake.chmod(0o755)
    return fake


def argvs(base: str) -> list[list[str]]:
    files = sorted(Path(base).glob("argv.*"), key=lambda p: int(p.suffix[1:]))
    return [p.read_text().split("\n") for p in files]


def test_the_socket_lives_in_a_private_directory_and_differs_per_machine(mux, base):
    p = mux.path(R)
    assert p is not None and Path(p).parent == Path(base) / f"agentos-ssh-{os.getuid()}"
    assert os.stat(Path(p).parent).st_mode & 0o777 == 0o700
    other = R.model_copy(update={"key_path": "/k/vm_2/id", "host_alias": "agentos-vm_2"})
    assert mux.path(other) != p and mux.path(R) == p


def test_a_directory_others_can_open_or_a_long_path_turns_it_off(mux, base):
    d = Path(base) / f"agentos-ssh-{os.getuid()}"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    assert mux.path(R) is None and mux.lease(R) is None
    assert sshmux.Mux("/tmp/" + "x" * 120).path(R) is None


def test_no_master_means_the_command_connects_on_its_own_and_one_master_starts(mux, monkeypatch):
    started: list[str] = []

    async def refresh(remote, path):
        started.append(path)
        await asyncio.sleep(0.05)
        return False

    monkeypatch.setattr(mux, "refresh", refresh)

    async def go():
        assert mux.lease(R) is None and mux.lease(R) is None  # the second does not start another
        await asyncio.sleep(0.1)

    asyncio.run(go())
    assert started == [mux.path(R)]
    assert sshmux.Mux(enabled=False).lease(R) is None


def test_without_an_event_loop_nothing_starts(mux):
    assert mux.lease(R) is None and mux.tasks == {}


async def test_a_master_that_says_hello_carries_up_to_max_sessions(mux):
    path = mux.path(R)
    assert path is not None
    s = listen(path)
    try:
        assert await mux.refresh(R, path) is True
        leases = [mux.lease(R) for _ in range(sshmux.MAX_SESSIONS)]
        assert all(leases) and mux.lease(R) is None  # full: this one connects on its own
        lease = leases[0]
        assert lease is not None and lease.args == ["-o", "ControlMaster=no", "-o", f"ControlPath={path}"]
        lease.release()
        lease.release()  # twice is once
        assert mux.sessions[path] == sshmux.MAX_SESSIONS - 1 and mux.lease(R) is not None
    finally:
        s.close()


async def test_a_stuck_master_is_no_master_and_the_check_never_blocks_the_loop(mux):
    """A stuck master still takes connections (the kernel does), so a connect said yes and each command
    then waited ConnectTimeout over it. Its missing hello says no, without blocking the event loop."""
    path = mux.path(R)
    assert path is not None
    s = listen(path, stuck=True)
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.ensure_future(tick())
    try:
        began = time.monotonic()
        assert await sshmux.hello(path, timeout_s=0.3) is False
        assert time.monotonic() - began < 1 and ticks >= 10
        assert await sshmux.hello(path + "-none") is False
    finally:
        ticker.cancel()
        s.close()


async def test_the_recheck_catches_a_master_that_got_stuck_and_starts_another(mux, monkeypatch):
    path = mux.path(R)
    assert path is not None
    s = listen(path)
    assert await mux.refresh(R, path)
    s.close()
    os.unlink(path)
    stuck = listen(path, stuck=True)
    started: list[str] = []

    async def start(remote, p):
        started.append(p)
        return False

    monkeypatch.setattr(mux, "start", start)
    try:
        mux.checked[path] = 0.0  # RECHECK_S went by: this command still goes over it, and a check starts
        lease = mux.lease(R)
        assert lease is not None
        lease.release()
        await asyncio.wait_for(mux.tasks[path], 5)
        assert started == [path] and mux.lease(R) is None
    finally:
        stuck.close()


@pytest.mark.parametrize("line", MUX_LINES)
def test_ssh_s_words_about_the_master_are_taken_out_and_the_master_rests(mux, line):
    path = "/tmp/mx-none/m"
    mux.healthy_until[path] = time.monotonic() + 60
    lease = sshmux.Lease(mux, path)
    mux.sessions[path] = 1
    assert lease.clean(line + b"the command's own error\n") == b"the command's own error\n"
    lease.release()
    assert mux.not_before[path] > time.monotonic() and path not in mux.healthy_until


GCC = b"mux_util.c:12:5: error: expected ;\n"


@pytest.mark.parametrize("data", [GCC + b"more\n", b"cc -c mux_util.c\n" + GCC,
                                  MUX_LINES[1] + b"x\n" + MUX_LINES[0],  # past the leading run: the command's
                                  b"Control socket connect(/elsewhere/m): Connection refused\n",  # not this lease's
                                  b"muxclient: master hello exchange failed, said a test\n"])
def test_a_command_s_own_lines_stay_whole_and_never_rest_the_master(mux, data):
    """Every line starting mux_ or Control socket connect( went, anywhere in the stream (gcc on mux_util.c
    returned an empty stderr and rested the master). Only ssh's exact lines, leading, go."""
    path = "/tmp/mx-none/m"
    lease = sshmux.Lease(mux, path)
    kept = lease.clean(data)
    assert kept == (data if not data.startswith(MUX_LINES[1]) else data[len(MUX_LINES[1]):])
    lease.release()
    assert (path in mux.not_before) == data.startswith(MUX_LINES[1])


def test_an_exit_status_of_255_alone_never_rests_the_master(mux):
    """255 is also what a command gives (an ssh run inside the VM): only ssh's own lines say the master failed."""
    path = "/tmp/mx-none/m"
    lease = sshmux.Lease(mux, path)
    lease.clean(b"")
    lease.release()
    assert path not in mux.not_before


async def test_a_failed_start_waits_retry_s_before_the_next(mux, base, monkeypatch):
    monkeypatch.setattr(ssh, "SSH", str(fake_ssh(base, "exit 255")))
    path = mux.path(R)
    assert path is not None
    assert await mux.start(R, path) is False
    assert mux.not_before[path] > 0
    assert mux.lease(R) is None and path not in mux.tasks  # not tried again before RETRY_S


async def test_a_master_starts_with_the_pinned_options_in_the_background(mux, base, monkeypatch):
    """The fake ssh listens on the ControlPath and says hello, as a master does once it is logged in
    and has gone to the background."""
    monkeypatch.setattr(ssh, "SSH", str(fake_ssh(base, f"""
for a; do case "$a" in ControlPath=*) p="${{a#ControlPath=}}";; esac; done
{shutil.which("python3")} -c "import socket,sys,os
s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(8); s.settimeout(5)
if os.fork(): os._exit(0)
while True:
    c=s.accept()[0]; c.sendall(b'hello-'); c.close()" "$p" < /dev/null > /dev/null 2>&1""")))
    path = mux.path(R)
    assert path is not None
    assert mux.lease(R) is None
    await asyncio.wait_for(mux.tasks[path], 10)
    [argv] = argvs(base)
    for opt in ("ControlMaster=yes", f"ControlPath={path}", f"ControlPersist={sshmux.IDLE_S}",
                "StrictHostKeyChecking=yes", "HostKeyAlias=agentos-vm_1", "ClearAllForwardings=yes"):
        assert opt in argv
    assert "-N" in argv and argv[-2] == "root@203.0.113.9"
    assert mux.lease(R) is not None and mux.started == {path}


def up(mux: sshmux.Mux, path: str) -> None:
    mux.healthy_until[path] = time.monotonic() + 60
    mux.checked[path] = time.monotonic()


def test_only_a_one_shot_command_shares_the_connection(mux, base, monkeypatch):
    monkeypatch.setattr(ssh, "MUX", mux)
    path = mux.path(R)
    assert path is not None
    up(mux, path)
    lane = type("L", (), {"remote": R})()
    launcher = ssh.SshLauncher()
    w = Path(base) / "w"
    one = launcher.command(None, lane, Workload("x-ab12", ["true"], {}, mux=True), cgroup=None, workdir=w)
    assert f"ControlPath={path}" in one.argv and "setsid -w" in one.argv[-1]
    assert one.argv.index("ControlMaster=no") < one.argv.index("root@203.0.113.9")
    assert mux.sessions[path] == 1 and one.session is not None
    one.session.release()
    assert mux.sessions[path] == 0
    for wl in (Workload("x-ab12", ["true"], {}),                                   # not marked
               Workload("s-ab12", ["bash"], {}, pty=True, mux=True),               # a terminal
               Workload("j-ab12", ["true"], {}, detach=(0, 0), mux=True)):         # a job's follower
        cmd = launcher.command(None, lane, wl, cgroup=None, workdir=w)
        assert not any("ControlPath" in a for a in cmd.argv) and cmd.session is None


async def test_run_goes_over_the_master_and_gives_its_session_back_without_ssh_s_words(mux, base, monkeypatch):
    monkeypatch.setattr(ssh, "MUX", mux)
    monkeypatch.setattr(ssh, "SSH", str(fake_ssh(
        base, 'echo "mux_client_request_session: session request failed: Session open refused by peer" >&2\nfor a; do last="$a"; done\n'
              'exec sh -c "$last"')))
    path = mux.path(R)
    assert path is not None
    up(mux, path)
    code, out = await ssh.run(R, "echo hi; exit 3", timeout_s=10)
    assert (code, out) == (3, "hi\n")
    assert f"ControlPath={path}" in argvs(base)[0] and mux.sessions[path] == 0
    assert mux.not_before[path] > time.monotonic()  # the master refused it: commands go on their own a while


async def test_run_s_timeout_holds_over_a_shared_connection(mux, base, monkeypatch):
    """A master keeps a killed client's pipes until the remote script ends: ssh.run(timeout_s=1) of a
    `sleep 15` came back after 15 s. Here a child of the fake ssh holds its stdout as the master does. The
    script's process group on the VM is ended (stop_script KILL), then the session goes back."""
    monkeypatch.setattr(ssh, "MUX", mux)
    holder = Path(base) / "holder.pid"
    monkeypatch.setattr(ssh, "SSH", str(fake_ssh(
        base, f'case "$*" in *"kill -s KILL"*) exit 0;; esac\nsleep 30 & echo $! > {holder}\nexec sleep 30')))
    path = mux.path(R)
    assert path is not None
    up(mux, path)
    began = time.monotonic()
    try:
        assert await ssh.run(R, "sleep 15", timeout_s=1) == (124, "timed out")
        assert time.monotonic() - began < 3
        await asyncio.wait_for(asyncio.gather(*ssh._ENDING), 5)
        first, stop = argvs(base)
        assert f"ControlPath={path}" in first and first[-2] == "bash -s"
        assert "ControlPath" not in " ".join(stop) and "kill -s KILL" in stop[-2] and "r-" in stop[-2]
        assert mux.sessions[path] == 0
    finally:
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(holder.read_text()), signal.SIGKILL)


def test_a_script_writes_its_process_group_for_the_stop_and_keeps_its_exit_status(base, monkeypatch):
    import subprocess

    monkeypatch.setattr(ssh, "PGID_DIR", base)
    script = ssh.grouped("r-ab12", f"cat {base}/r-ab12.pgid; exit 3")
    r = subprocess.run(["bash", "-s"], input=script.encode(), capture_output=True, check=False, start_new_session=True)
    assert r.returncode == 3 and r.stdout.strip().isdigit() and not (Path(base) / "r-ab12.pgid").exists()


async def test_stop_ends_a_vm_s_master_and_stop_all_the_ones_this_process_started(mux, base, monkeypatch):
    monkeypatch.setattr(ssh, "SSH", str(fake_ssh(base, "exit 0")))
    path = mux.path(R)
    assert path is not None
    other = R.model_copy(update={"key_path": "/k/vm_2/id", "host_alias": "agentos-vm_2"})
    path2 = mux.path(other)
    assert path2 is not None
    s, s2 = listen(path), listen(path2)
    try:
        up(mux, path)
        await mux.stop(R)
        assert argvs(base)[0][-3:-1] == ["exit", "agentos-vm"] and f"ControlPath={path}" in argvs(base)[0]
        assert path not in mux.healthy_until and not os.path.exists(path)
        mux.started = {path2}
        await mux.stop_all()
        assert f"ControlPath={path2}" in argvs(base)[1] and mux.started == set() and len(argvs(base)) == 2
    finally:
        s.close()
        s2.close()


async def test_release_gives_the_session_back_once():
    got: list[int] = []

    class Session:
        def clean(self, data):
            return data

        def release(self):
            got.append(1)

    rt = LaneRuntime.__new__(LaneRuntime)
    rt.cgroup, rt.workloads = None, {}
    proc = await asyncio.create_subprocess_exec("sh", "-c", "exit 4")
    await proc.wait()
    sp = Spawned(proc, "x-1", "exec", None, session=Session())
    await rt.release(sp)
    await rt.release(sp)
    assert got == [1]
