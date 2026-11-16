"""Commands in a VM lane: every exec, shell and service is one `ssh` from the node to the VM.

The VM holds nothing of agentos: no node token, no vault, no agentosd (an environment's VM holds
one gateway token for its repository, agentos.environments). A workload's environment goes on the
ssh command line, which `ps` on the node shows: never put a secret there. The node keeps the VM's private
key (one per VM) and the VM's host key, which agentos made itself and handed to the VM through
cloud-init, so the very first connection is already pinned (StrictHostKeyChecking=yes against a
known_hosts file of one line, under a HostKeyAlias that does not depend on the IP).
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
from pathlib import Path

from agentos.env import oom
from agentos.env.models import EnvLane, RemoteHost
from agentos.env.sandbox import Command, LanePlan, Workload

SSH = "ssh"
CONNECT_TIMEOUT_S = 10


def ssh_base(remote: RemoteHost, *, tty: bool = False) -> list[str]:
    """The ssh argv up to (not including) the remote command. No config file, no agent, no
    password, no forwarding of anything."""
    return [
        SSH, "-F", "/dev/null",
        "-i", remote.key_path,
        "-p", str(remote.port),
        "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none",
        "-o", "BatchMode=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={remote.known_hosts}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", f"HostKeyAlias={remote.host_alias}",
        "-o", "UpdateHostKeys=no",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "LogLevel=ERROR",
        "-tt" if tty else "-T",
        f"{remote.user}@{remote.host}",
    ]


# Where a workload on the VM writes its process group id, so the node can end every process it started
# (lane_job_cancel left a dev server's vite running: killing the local ssh never reached the VM's processes).
# `kill -s SIG -- -PGID`, not `kill -SIG -- -PGID`: Ubuntu's /bin/sh is dash, whose kill refuses the latter
# ("Illegal number: -") and signals nothing, so the group lived on (checked in ubuntu:24.04, 2026-11-16).
PGID_DIR = "/tmp/agentos-wl"
_WID_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def pgid_file(wid: str) -> str:
    return f"{PGID_DIR}/{wid}.pgid"


def remote_command(wl: Workload) -> str:
    """One string for the remote shell: the working directory, the workload's environment and
    its argv, each quoted. sshd runs it with the user's login shell. A workload without a terminal
    runs in a process group of its own (setsid -w, which still waits for it and passes its exit
    status), whose id it writes to pgid_file(wid) for stop_argv, and what it leaves in the background
    ends with it; one with a terminal ends with its terminal."""
    cwd = wl.cwd or "~"
    cd = "cd" if cwd == "~" else f"cd {shlex.quote(cwd)}"
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(wl.env.items()) if _env_name_ok(k))
    run = shlex.join(wl.argv)
    if env:
        run = f"env {env} {run}"
    if wl.pty or not wl.wid or set(wl.wid) - _WID_OK:
        return f"{cd} && exec {run}"
    f = pgid_file(wl.wid)
    # after the command, what it left in the background ends too (the shell ignores the TERM it sends)
    record = keep = ""
    if wl.oom_record:  # the start record (agentos.env.oom) stays after a failure, for the node to read and remove
        s = oom.start_file(PGID_DIR, wl.wid)
        record, keep = f"{oom.record(s)}; ", f"[ $rc -ne 0 ] || rm -f {s}; "
    group = shlex.quote(f'echo $$ > {f}; {record}"$@"; rc=$?; trap "" TERM; kill -s TERM -- -$$ 2>/dev/null; '
                        f'rm -f {f}; {keep}exit $rc')
    return f"mkdir -p {PGID_DIR} && {cd} && exec setsid -w sh -c {group} sh {run}"


def stop_script(wid: str, sig: str) -> str:
    """Signal every process of the workload's group on the VM; the file goes once it is killed."""
    f = shlex.quote(pgid_file(wid))
    tail = f"; rm -f {f}" if sig == "KILL" else ""
    return f'p=$(cat {f} 2>/dev/null) && [ -n "$p" ] && kill -s {sig} -- "-$p" 2>/dev/null{tail}; true'


def _env_name_ok(name: str) -> bool:
    return bool(name) and (name[0].isalpha() or name[0] == "_") and all(c.isalnum() or c == "_" for c in name)


class SshLauncher:
    """The launcher of a VM lane (the env manager picks it for lanes with `remote`)."""

    def command(self, plan: LanePlan | None, lane: EnvLane, wl: Workload, *, cgroup: str | None,
                workdir: Path) -> Command:
        remote = lane.remote
        assert remote is not None, "SshLauncher serves VM lanes only"
        from agentos.env.sandbox import cgroup_wrap
        argv = (cgroup_wrap(cgroup) if cgroup else []) + ssh_base(remote, tty=wl.pty) + [remote_command(wl)]
        # the local ssh client gets a plain environment: nothing of agentosd's leaks to it
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(workdir), "LANG": "C.UTF-8"}
        workdir.mkdir(parents=True, exist_ok=True)
        return Command(argv, env, str(workdir))

    def stop_argv(self, lane: EnvLane, wid: str, sig: str) -> list[str] | None:
        """The ssh command that sends `sig` (TERM or KILL) to a workload's process group on the VM
        (agentos.env.lanes calls it before it kills the local ssh). None for an id it never wrote."""
        if lane.remote is None or not wid or set(wid) - _WID_OK:
            return None
        return ssh_base(lane.remote) + [stop_script(wid, sig)]

    async def oom_kills(self, lane: EnvLane, wid: str, *, elapsed_s: float, final: bool = False) -> list[dict] | None:
        """The processes the VM's kernel killed for memory since the workload started (agentos.env.oom);
        None when the VM could not be asked. `final` removes the workload's start record."""
        if lane.remote is None or not wid or set(wid) - _WID_OK:
            return None
        code, out = await run(lane.remote, oom.probe_script(PGID_DIR, wid, final=final), timeout_s=oom.PROBE_TIMEOUT_S)
        if code != 0:
            return None
        return oom.parse(out, elapsed_s=elapsed_s)


async def probe(remote: RemoteHost, *, timeout_s: float = 20.0) -> tuple[bool, str]:
    """Run `true` on the VM. (True, "") when SSH answers with the pinned host key."""
    argv = ssh_base(remote) + ["true"]
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        return False, f"cannot run ssh: {e}"
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False, "ssh timed out"
    if proc.returncode == 0:
        return True, ""
    return False, (err or b"").decode(errors="replace").strip()[-300:] or f"ssh exited {proc.returncode}"


async def run(remote: RemoteHost, script: str, *, timeout_s: float = 900.0) -> tuple[int, str]:
    """Run a shell script on the VM (image builds). Returns (exit code, the last 4000 characters
    of its output)."""
    argv = ssh_base(remote) + ["bash -s"]
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(script.encode()), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "timed out"
    except BaseException:  # the caller gave up (a probe beside a read): no ssh is left behind
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    return proc.returncode or 0, (out or b"").decode(errors="replace")[-4000:]


async def push_tree(remote: RemoteHost, src: Path, dest: str, *, timeout_s: float = 600.0) -> None:
    """Copy the directory `src` (on the node) to `dest` (an absolute path on the VM) as one tar
    stream over SSH. Refuses a dest that exists and is not empty."""
    import os

    q = shlex.quote(dest)
    script = (f"if [ -e {q} ] && [ -n \"$(ls -A {q} 2>/dev/null)\" ]; then echo '{dest} exists and is not empty' >&2; "
              f"exit 17; fi; mkdir -p {q} && tar -x --no-same-owner -f - -C {q}")
    r, w = os.pipe()
    try:
        tar = await asyncio.create_subprocess_exec("tar", "-C", str(src), "-cf", "-", ".", stdout=w,
                                                   stderr=asyncio.subprocess.PIPE,
                                                   env={"PATH": "/usr/bin:/bin", "COPYFILE_DISABLE": "1"})
    finally:
        os.close(w)
    try:
        sh = await asyncio.create_subprocess_exec(*ssh_base(remote), script, stdin=r,
                                                  stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    except BaseException:
        tar.kill()
        await tar.wait()
        raise
    finally:
        os.close(r)
    try:
        (_, terr), (_, serr) = await asyncio.wait_for(asyncio.gather(tar.communicate(), sh.communicate()),
                                                      timeout=timeout_s)
    except TimeoutError:
        for p in (tar, sh):
            with contextlib.suppress(ProcessLookupError):
                p.kill()
            await p.wait()
        from agentos.vms.models import VmError
        raise VmError(f"copying to the VM took longer than {int(timeout_s)} s") from None
    from agentos.vms.models import VmError
    if sh.returncode == 17:
        raise VmError(f"{dest} already exists on the VM and is not empty; pick another dest")
    if sh.returncode != 0:
        raise VmError(f"copying to the VM failed: {(serr or b'').decode(errors='replace').strip()[-300:] or sh.returncode}")
    if tar.returncode != 0:
        raise VmError(f"packing the checkout failed: {(terr or b'').decode(errors='replace').strip()[-300:]}")
