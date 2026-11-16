"""A job on a VM ends with every process it started (lane_job_cancel left a dev server's vite running:
killing the local ssh never reached the VM). The remote command runs in a process group of its own,
and stop_argv signals that group over ssh. Here the "VM" is this computer's shell, with a stand-in
for util-linux setsid where there is none (macOS)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentos.env.models import RemoteHost
from agentos.env.sandbox import Workload
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


@pytest.fixture
def shell(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if shutil.which("setsid") is None:
        (bin_dir / "setsid").write_text(SETSID.format(py=sys.executable))
        (bin_dir / "setsid").chmod(0o755)
    monkeypatch.setattr(ssh, "PGID_DIR", str(tmp_path / "wl"))
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}

    def start(argv: list[str], wid: str) -> subprocess.Popen:
        cmd = ssh.remote_command(Workload(wid=wid, argv=argv, env={}, cwd=str(tmp_path)))
        return subprocess.Popen(["sh", "-c", cmd], env=env, start_new_session=True)

    def stop(wid: str, sig: str) -> None:
        subprocess.run(["sh", "-c", ssh.stop_script(wid, sig)], env=env, check=True)

    return start, stop, tmp_path


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is gone for this purpose
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], check=False, capture_output=True, text=True).stdout
    except OSError:
        return True
    return bool(out.strip()) and not out.strip().startswith("Z")


def wait_for(path: Path, timeout: float = 5) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def test_cancel_ends_the_dev_server_and_its_children(shell):
    start, stop, tmp = shell
    child = tmp / "child.pid"
    # like `pnpm dev`: a shell that starts vite (a child) and waits
    proc = start(["sh", "-c", f"sleep 300 & echo $! > {child}; wait"], "j-dev1")
    vite = int(wait_for(child))
    assert alive(vite)
    stop("j-dev1", "TERM")
    proc.wait(timeout=5)
    time.sleep(0.2)
    assert not alive(vite)
    stop("j-dev1", "KILL")
    assert not (Path(ssh.PGID_DIR) / "j-dev1.pgid").exists()


def test_a_command_that_ends_takes_its_background_children_along_and_keeps_its_exit_status(shell):
    start, _, tmp = shell
    child = tmp / "bg.pid"
    proc = start(["sh", "-c", f"sleep 300 & echo $! > {child}; exit 3"], "x-run1")
    assert proc.wait(timeout=5) == 3
    bg = int(wait_for(child))
    time.sleep(0.2)
    assert not alive(bg)


def test_a_terminal_workload_and_an_odd_id_keep_the_plain_command():
    plain = ssh.remote_command(Workload(wid="s1", argv=["bash"], env={}, pty=True))
    assert "setsid" not in plain and plain.endswith("exec bash")
    assert "setsid" not in ssh.remote_command(Workload(wid="a;rm -rf /", argv=["true"], env={}))
    lane = type("L", (), {"remote": RemoteHost(vm_id="vm_1", host="h", user="root", key_path="/k", known_hosts="/kh",
                                               host_alias="a")})()
    argv = ssh.SshLauncher().stop_argv(lane, "j-1", "TERM")
    assert argv[0] == "ssh" and "kill -s TERM" in argv[-1]
    assert ssh.SshLauncher().stop_argv(lane, "bad id", "TERM") is None
