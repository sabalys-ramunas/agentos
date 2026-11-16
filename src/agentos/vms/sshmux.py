"""One shared ssh connection per VM for the node's short commands (OpenSSH connection multiplexing).

Every lane_exec, file read, write and edit, job status and oom probe was a new `ssh` to the VM: a TCP
connect, a key exchange and a public-key login each time. That was 105 ms of a 146 ms lane_exec on the
docker provider (measured 2026-11-16, loopback), and more on a cloud VM, where each of those steps is
a round trip over the internet. A master connection (`ssh -M`, started by the node in the background
the first time a VM is used) carries those commands as sessions instead: about 13 ms each.

What it keeps as it was:

- A command never waits for the master and never depends on it. While none is known to answer (the
  first call, the master died or hangs, the VM restarted), the command connects on its own, as before,
  and the node checks the master (its hello on connect) or starts one in the background for the next.
  A client whose master went away or refused it falls back to a direct connection too (ssh's
  ControlMaster=no); ssh's word about that never reaches a result (Lease.clean), and the master is
  not used again for RETRY_S.
- Only one-shot commands share it (`Workload.mux`, set by agentos.env.execs, and `ssh.run`). A job's
  follower does not: it watches its own sshd ($PPID) to notice that the node let go, and on a shared
  connection that sshd lives on. Terminals, services, tunnels and probes keep their own connection.
- At most MAX_SESSIONS commands at once go over it (sshd's MaxSessions is 10 by default; past it a
  session is refused, not queued); the others connect on their own.
- The master's socket gives a shell on the VM to whoever can connect to it: it lives in a directory
  of this user's only (0700, checked), named by a hash of the VM's address, key and host key alias.
- A command whose caller gave up or timed out has its process group on the VM ended before its
  session is given back (agentos.env.execs, ssh.run), so the count here stays sshd's.
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
import re
import socket
import stat
import tempfile
import time
from pathlib import Path

from agentos.env.models import RemoteHost

log = logging.getLogger(__name__)

IDLE_S = 300           # the master ends this long after its last command (ControlPersist)
MAX_SESSIONS = 8       # commands at once over one master (sshd allows 10 sessions a connection by default)
RETRY_S = 30.0         # after a master failed (to start, or a command), commands connect on their own this long
START_TIMEOUT_S = 30.0
HEALTHY_S = 60.0       # a master that said hello is used this long without another answer
RECHECK_S = 5.0        # a command more than this after the last hello asks again, in the background
HELLO_TIMEOUT_S = 0.5  # a master sends its hello at once on connect; one that does not is stuck
# what an ssh client says of a master it could not use (it then connects on its own): never a result's
MUX_LINE = re.compile(rb"^(?:muxclient|mux_[a-z_]+|Control socket connect\(|ControlSocket )[^\n]*(?:\n|$)",
                      re.MULTILINE)
# sockaddr_un holds 104 bytes on macOS (108 on Linux), and ssh first binds `<path>.<16 random characters>`
MAX_PATH = 104 - 18


class Lease:
    """A session on a VM's master: the ssh options that use it, given back with release()."""

    def __init__(self, mux: Mux, path: str):
        self.mux = mux
        self.path = path
        self.args = ["-o", "ControlMaster=no", "-o", f"ControlPath={path.replace('%', '%%')}"]
        self.troubled = False
        self._done = False

    def clean(self, data: bytes) -> bytes:
        """The client's output without ssh's lines about a master it could not use (it fell back to a
        connection of its own); seeing one marks the master troubled."""
        out, n = MUX_LINE.subn(b"", data)
        if n:
            self.troubled = True
        return out

    def release(self, rc: int | None = None) -> None:
        """Give the session back once the command's processes on the VM have ended. An exit status of
        255 (ssh's own failure) or a line clean() took out keeps the master unused for RETRY_S."""
        if self._done:
            return
        self._done = True
        self.mux.sessions[self.path] = max(0, self.mux.sessions.get(self.path, 0) - 1)
        if self.troubled or rc == 255:
            self.mux.trouble(self.path)


class Mux:
    """The node's masters, by control socket path."""

    def __init__(self, base: str | None = None, *, enabled: bool = True):
        self.base = base
        self.enabled = enabled
        self.sessions: dict[str, int] = {}
        self.healthy_until: dict[str, float] = {}  # path -> monotonic time its last hello is good until
        self.checked: dict[str, float] = {}        # path -> when it last said hello
        self.not_before: dict[str, float] = {}     # path -> not used, nor started, before this (one failed)
        self.tasks: dict[str, asyncio.Task] = {}   # path -> its check or start under way

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
        """A session on the VM's master when it said hello lately and has room; else None (connect on
        your own), and the master is checked, or one started, in the background for the next command."""
        if not self.enabled:
            return None
        path = self.path(remote)
        if path is None:
            return None
        now = time.monotonic()
        if self.not_before.get(path, 0.0) > now:
            return None
        if self.healthy_until.get(path, 0.0) <= now:
            self._soon(remote, path)
            return None
        if now - self.checked.get(path, 0.0) > RECHECK_S:
            self._soon(remote, path)  # this command goes over it; a stuck one is caught for the next
        if self.sessions.get(path, 0) >= MAX_SESSIONS:
            return None
        self.sessions[path] = self.sessions.get(path, 0) + 1
        return Lease(self, path)

    def trouble(self, path: str) -> None:
        """A command could not use the master (it hangs, is gone or refused a session): commands connect
        on their own for RETRY_S, then it is asked again."""
        self.healthy_until.pop(path, None)
        self.not_before[path] = time.monotonic() + RETRY_S

    def _soon(self, remote: RemoteHost, path: str) -> None:
        task = self.tasks.get(path)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no event loop (a synchronous caller): this command connects on its own
            return
        self.tasks[path] = loop.create_task(self.refresh(remote, path), name=f"ssh-mux-{remote.vm_id}")

    async def refresh(self, remote: RemoteHost, path: str) -> bool:
        """Ask the master for its hello; without one, start another in its place. True when one answers."""
        if await hello(path):
            self._healthy(path)
            return True
        self.healthy_until.pop(path, None)
        if self.not_before.get(path, 0.0) > time.monotonic():
            return False
        return await self.start(remote, path)

    def _healthy(self, path: str) -> None:
        now = time.monotonic()
        self.checked[path] = now
        self.healthy_until[path] = now + HEALTHY_S

    async def start(self, remote: RemoteHost, path: str) -> bool:
        """Start the VM's master in the background (ssh daemonizes it once logged in). True when it answers."""
        from agentos.vms.ssh import ssh_base

        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)  # a master that died or hangs left its socket: nothing reaches it any more
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
            ok = proc.returncode == 0 and await hello(path)
        except OSError as e:
            log.debug("no ssh master for %s: %s", remote.vm_id, e)
        if ok:
            self._healthy(path)
        else:
            self.not_before[path] = time.monotonic() + RETRY_S
        return ok

async def hello(path: str, timeout_s: float = HELLO_TIMEOUT_S) -> bool:
    """Does a master answer on this socket? It sends its hello as soon as it takes a connection; one
    that is stuck takes it (the kernel does) but says nothing. Never blocks the event loop."""
    if not os.path.exists(path):
        return False
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.setblocking(False)
    try:
        async with asyncio.timeout(timeout_s):
            await loop.sock_connect(s, path)
            return len(await loop.sock_recv(s, 4)) == 4
    except (OSError, TimeoutError):
        return False
    finally:
        s.close()


MUX = Mux(enabled=os.environ.get("AGENTOS_SSH_MUX", "1") != "0")
