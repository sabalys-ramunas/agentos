"""One shared ssh connection per VM for the node's short commands (OpenSSH connection multiplexing).

Every lane_exec, file read, write and edit, job status and oom probe was a new `ssh` to the VM: a TCP
connect, a key exchange and a public-key login each time. That was 105 ms of a 146 ms lane_exec on the
docker provider (measured 2026-11-16, loopback), and more on a cloud VM, where each of those steps is
a round trip over the internet. A master connection (`ssh -M`, started by the node in the background
the first time a VM is used) carries those commands as sessions instead: about 13 ms each.

What it keeps as it was:

- A command never waits for the master and never depends on it. While none answers (the first call,
  the master just died, the VM restarted), the command connects on its own, as before, and the node
  starts a master for the next one. A client whose master went away between the check and its own
  connect falls back to a direct connection too (ssh's ControlMaster=no).
- Only one-shot commands share it (`Workload.mux`, set by agentos.env.execs, and `ssh.run`). A job's
  follower does not: it watches its own sshd ($PPID) to notice that the node let go, and on a shared
  connection that sshd lives on. Terminals, services, tunnels and probes keep their own connection.
- At most MAX_SESSIONS commands at once go over it (sshd's MaxSessions is 10 by default; past it a
  session is refused, not queued); the others connect on their own.
- The master's socket gives a shell on the VM to whoever can connect to it: it lives in a directory
  of this user's only (0700, checked), named by a hash of the VM's address, key and host key alias.
- The master ends after IDLE_S without a command, and with its VM: a deleted machine's connection
  drops (at once for a container, within ServerAliveInterval x ServerAliveCountMax for a cloud VM).
- AGENTOS_SSH_MUX=0 in the node's environment turns it off: every command connects on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import socket
import stat
import tempfile
import time
from pathlib import Path

from agentos.env.models import RemoteHost

log = logging.getLogger(__name__)

IDLE_S = 300           # the master ends this long after its last command (ControlPersist)
MAX_SESSIONS = 8       # commands at once over one master (sshd allows 10 sessions a connection by default)
CHECK_S = 2.0          # how long a master that answered is taken to be up without asking again
RETRY_S = 30.0         # after a master failed to start, the commands connect on their own this long
START_TIMEOUT_S = 30.0
# sockaddr_un holds 104 bytes on macOS (108 on Linux), and ssh first binds `<path>.<16 random characters>`
MAX_PATH = 104 - 18


class Lease:
    """A session on a VM's master: the ssh options that use it, given back with release()."""

    def __init__(self, mux: Mux, path: str):
        self.mux = mux
        self.path = path
        self.args = ["-o", "ControlMaster=no", "-o", f"ControlPath={path.replace('%', '%%')}"]
        self._done = False

    def release(self) -> None:
        if not self._done:
            self._done = True
            self.mux.sessions[self.path] = max(0, self.mux.sessions.get(self.path, 0) - 1)


class Mux:
    """The node's masters, by control socket path."""

    def __init__(self, base: str | None = None, *, enabled: bool = True):
        self.base = base
        self.enabled = enabled
        self.sessions: dict[str, int] = {}
        self.up_until: dict[str, float] = {}      # path -> monotonic time its master last answered + CHECK_S
        self.not_before: dict[str, float] = {}    # path -> no new master before this (one failed)
        self.starting: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ where
    def directory(self) -> Path | None:
        """This user's own directory for the sockets, or None when it is not safe to use."""
        d = Path(self.base or tempfile.gettempdir()) / f"agentos-ssh-{os.getuid()}"
        try:
            d.mkdir(mode=0o700, exist_ok=True)
            st = os.lstat(d)
        except OSError:
            return None
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            log.warning("ssh multiplexing is off: %s is not a private directory of this user", d)
            return None
        return d

    def path(self, remote: RemoteHost) -> str | None:
        d = self.directory()
        if d is None:
            return None
        key = "|".join((remote.user, remote.host, str(remote.port), remote.host_alias, remote.key_path,
                        remote.known_hosts))
        p = str(d / hashlib.sha256(key.encode()).hexdigest()[:16])
        return p if len(p.encode()) <= MAX_PATH else None

    # ------------------------------------------------------------------ use
    def lease(self, remote: RemoteHost) -> Lease | None:
        """A session on the VM's master when one answers and has room; else None (connect on your own),
        and a master is started for the next command when none runs."""
        if not self.enabled:
            return None
        path = self.path(remote)
        if path is None:
            return None
        if not self._up(path):
            self._start_soon(remote, path)
            return None
        if self.sessions.get(path, 0) >= MAX_SESSIONS:
            return None
        self.sessions[path] = self.sessions.get(path, 0) + 1
        return Lease(self, path)

    def _up(self, path: str) -> bool:
        now = time.monotonic()
        if self.up_until.get(path, 0.0) > now:
            return True
        if _answers(path):
            self.up_until[path] = now + CHECK_S
            return True
        self.up_until.pop(path, None)
        return False

    def _start_soon(self, remote: RemoteHost, path: str) -> None:
        task = self.starting.get(path)
        if task is not None and not task.done():
            return
        if self.not_before.get(path, 0.0) > time.monotonic():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no event loop (a synchronous caller): this command connects on its own
            return
        self.starting[path] = loop.create_task(self.start(remote, path), name=f"ssh-mux-{remote.vm_id}")

    async def start(self, remote: RemoteHost, path: str) -> bool:
        """Start the VM's master in the background (ssh daemonizes it once logged in). True when it answers."""
        from agentos.vms.ssh import ssh_base

        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)  # a master that died left its socket (the check above found nobody answering)
        base = ssh_base(remote)
        argv = [*base[:-2], "-o", "ControlMaster=yes", "-o", f"ControlPath={path.replace('%', '%%')}",
                "-o", f"ControlPersist={IDLE_S}", "-N", *base[-2:]]
        ok = False
        try:
            proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            try:
                await asyncio.wait_for(proc.wait(), START_TIMEOUT_S)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            ok = proc.returncode == 0 and _answers(path)
        except OSError as e:
            log.debug("no ssh master for %s: %s", remote.vm_id, e)
        if ok:
            self.up_until[path] = time.monotonic() + CHECK_S
        else:
            self.not_before[path] = time.monotonic() + RETRY_S
        return ok


def _answers(path: str) -> bool:
    """Does a master listen on this socket? (A connect to a unix socket answers at once.)"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(0.5)
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


MUX = Mux(enabled=os.environ.get("AGENTOS_SSH_MUX", "1") != "0")
