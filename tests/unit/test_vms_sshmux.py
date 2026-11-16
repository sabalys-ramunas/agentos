"""One shared ssh connection per VM for one-shot commands (agentos.vms.sshmux). A unix socket that
listens stands in for a master here; the real thing was measured on the docker provider: 146 ms a
lane_exec before, about 50 ms after."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from agentos.env.lanes import LaneRuntime, Spawned
from agentos.env.models import RemoteHost
from agentos.env.sandbox import Workload
from agentos.vms import ssh, sshmux

R = RemoteHost(vm_id="vm_1", host="203.0.113.9", key_path="/k/vm_1/id", known_hosts="/k/vm_1/kh",
               host_alias="agentos-vm_1")


@pytest.fixture
def base():
    d = tempfile.mkdtemp(prefix="mx", dir="/tmp")  # a socket path must be short: no pytest tmp_path
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def mux(base):
    return sshmux.Mux(base)


class listen:
    """A stand-in master: listens on the socket and takes each connection, as ssh's does."""

    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        threading.Thread(target=self._take, daemon=True).start()

    def _take(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.close()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()


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

    async def start(remote, path):
        started.append(path)
        await asyncio.sleep(0.05)
        return False

    monkeypatch.setattr(mux, "start", start)

    async def go():
        assert mux.lease(R) is None and mux.lease(R) is None  # the second does not start another
        await asyncio.sleep(0.1)

    asyncio.run(go())
    assert started == [mux.path(R)]
    assert sshmux.Mux(enabled=False).lease(R) is None


def test_without_an_event_loop_nothing_starts(mux):
    assert mux.lease(R) is None and mux.starting == {}


def test_a_master_that_answers_carries_up_to_max_sessions(mux):
    path = mux.path(R)
    assert path is not None
    s = listen(path)
    try:
        leases = [mux.lease(R) for _ in range(sshmux.MAX_SESSIONS)]
        assert all(leases) and mux.lease(R) is None  # full: this one connects on its own
        lease = leases[0]
        assert lease is not None and lease.args == ["-o", "ControlMaster=no", "-o", f"ControlPath={path}"]
        lease.release()
        lease.release()  # twice is once
        assert mux.sessions[path] == sshmux.MAX_SESSIONS - 1 and mux.lease(R) is not None
    finally:
        s.close()


def test_a_dead_master_is_noticed_by_the_next_command(mux):
    path = mux.path(R)
    assert path is not None
    s = listen(path)
    lease = mux.lease(R)
    assert lease is not None
    lease.release()
    s.close()  # killed: its socket file stays, and nobody answers on it
    assert os.path.exists(path) and mux.lease(R) is None


async def test_a_failed_start_waits_retry_s_before_the_next(mux, base, monkeypatch):
    fake = Path(base) / "ssh"
    fake.write_text("#!/bin/sh\nexit 255\n")
    fake.chmod(0o755)
    monkeypatch.setattr(ssh, "SSH", str(fake))
    path = mux.path(R)
    assert path is not None
    assert await mux.start(R, path) is False
    assert mux.not_before[path] > 0
    assert mux.lease(R) is None and path not in mux.starting  # not tried again before RETRY_S


async def test_a_master_starts_with_the_pinned_options_in_the_background(mux, base, monkeypatch):
    """The fake ssh records its argv and listens on the ControlPath, as a master does once it is
    logged in and has gone to the background."""
    log = Path(base) / "argv"
    fake = Path(base) / "ssh"
    fake.write_text(f"""#!/bin/sh
printf '%s\\n' "$@" > {log}
for a; do case "$a" in ControlPath=*) p="${{a#ControlPath=}}";; esac; done
{shutil.which("python3")} -c "import socket,sys,os
s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(8); s.settimeout(5)
if os.fork(): os._exit(0)
while True: s.accept()[0].close()" "$p" < /dev/null > /dev/null 2>&1
""")
    fake.chmod(0o755)
    monkeypatch.setattr(ssh, "SSH", str(fake))
    path = mux.path(R)
    assert path is not None
    assert mux.lease(R) is None
    await asyncio.wait_for(mux.starting[path], 10)
    argv = log.read_text().split("\n")
    for opt in ("ControlMaster=yes", f"ControlPath={path}", f"ControlPersist={sshmux.IDLE_S}",
                "StrictHostKeyChecking=yes", "HostKeyAlias=agentos-vm_1", "ClearAllForwardings=yes"):
        assert opt in argv
    assert "-N" in argv and argv[-2] == "root@203.0.113.9"
    assert mux.lease(R) is not None


def test_only_a_one_shot_command_shares_the_connection(mux, base, monkeypatch):
    monkeypatch.setattr(sshmux, "MUX", mux)
    monkeypatch.setattr(ssh, "MUX", mux)
    path = mux.path(R)
    assert path is not None
    s = listen(path)
    lane = type("L", (), {"remote": R})()
    launcher = ssh.SshLauncher()
    w = Path(base) / "w"
    try:
        one = launcher.command(None, lane, Workload("x-ab12", ["true"], {}, mux=True), cgroup=None, workdir=w)
        assert f"ControlPath={path}" in one.argv and "setsid -w" in one.argv[-1]
        assert one.argv.index("ControlMaster=no") < one.argv.index("root@203.0.113.9")
        assert mux.sessions[path] == 1 and one.on_end is not None
        one.on_end()
        assert mux.sessions[path] == 0
        for wl in (Workload("x-ab12", ["true"], {}),                                   # not marked
                   Workload("s-ab12", ["bash"], {}, pty=True, mux=True),               # a terminal
                   Workload("j-ab12", ["true"], {}, detach=(0, 0), mux=True)):         # a job's follower
            cmd = launcher.command(None, lane, wl, cgroup=None, workdir=w)
            assert not any("ControlPath" in a for a in cmd.argv) and cmd.on_end is None
    finally:
        s.close()


async def test_run_goes_over_the_master_and_gives_its_session_back(mux, base, monkeypatch):
    monkeypatch.setattr(ssh, "MUX", mux)
    log = Path(base) / "argv"
    fake = Path(base) / "ssh"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\nfor a; do last="$a"; done\nexec sh -c "$last"\n')
    fake.chmod(0o755)
    monkeypatch.setattr(ssh, "SSH", str(fake))
    path = mux.path(R)
    assert path is not None
    s = listen(path)
    try:
        code, out = await ssh.run(R, "echo hi; exit 3", timeout_s=10)
        assert (code, out) == (3, "hi\n")
        assert f"ControlPath={path}" in log.read_text().split("\n") and mux.sessions[path] == 0
    finally:
        s.close()


async def test_release_ends_the_session_once():
    ended: list[int] = []
    rt = LaneRuntime.__new__(LaneRuntime)
    rt.cgroup, rt.workloads = None, {}
    proc = await asyncio.create_subprocess_exec("true")
    await proc.wait()
    sp = Spawned(proc, "x-1", "exec", None, on_end=lambda: ended.append(1))
    await rt.release(sp)
    await rt.release(sp)
    assert ended == [1]
