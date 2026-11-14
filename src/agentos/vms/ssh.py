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


def remote_command(wl: Workload) -> str:
    """One string for the remote shell: the working directory, the workload's environment and
    its argv, each quoted. sshd runs it with the user's login shell."""
    cwd = wl.cwd or "~"
    cd = "cd" if cwd == "~" else f"cd {shlex.quote(cwd)}"
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(wl.env.items()) if _env_name_ok(k))
    run = shlex.join(wl.argv)
    if env:
        run = f"env {env} {run}"
    return f"{cd} && exec {run}"


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
