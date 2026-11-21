"""agentos-runas: the root helper that lets a non-root agentosd start brains as their own user.

agentosd as root starts each brain's CLI as `agentos-brain` itself (the shim drops to it). Run as
the unprivileged `agentos` user it cannot, and its hardened unit sets NoNewPrivileges, so sudo,
su or any setuid binary is out by construction. This helper is the one privileged piece:

    agentos-runas.service (root)  listens on /run/agentos-runas/runas.sock, a directory 0750 and a
                                  socket 0660, both group `openajar`; every connection's peer uid
                                  (SO_PEERCRED) must be the configured caller, so only agentosd
                                  (and what runs as `openajar`) can ask.

It does exactly one thing: start a program as one of two fixed identities, both named on its
command line by root, never in a request:

    the brain user (agentos-brain)   attached: the caller keeps the connection, can send SIGINT,
                                     SIGTERM, SIGKILL or SIGHUP to the program's process group, gets
                                     its exit code, and when the connection closes everything in
                                     its process group (and its scope) is killed.
    the caller itself (openajar)      detached: for the brain shim, so a task survives agentosd
                                     restarting. No privilege is gained: the caller can run that
                                     program itself; the helper only gives it its own scope.

Never root, never another user, never an arbitrary unit. The request is one JSON object with a
fixed schema (argv, env, cwd, three stdio file descriptors passed with SCM_RIGHTS, an optional
scope with MemoryMax, TasksMax and CPUQuota inside fixed bounds, and core, nofile and fsize
rlimits that only ever go down), at most 1 MiB and four levels deep, read under a timeout.

A brain starts in a PID namespace of its own (and a mount namespace with its own /proc), so no
brain sees another's processes: brains share one uid, and a process can read the environment
(the child grant token) of any process of its own uid it can see. What runs as root is fixed
system programs with fixed options: prlimit (the rlimits), unshare (the namespaces), setpriv
(the drop: setgroups([]), setgid, setuid, no_new_privs), each with the helper's own minimal
environment; then, as the brain, `env -i` and a bash loader that reads the request's env from a
file descriptor (never from argv, which /proc shows to everyone), and a gate in bash that waits
until the scope is set up and enters the working directory, so a root chdir can never hand the
brain a directory it could not open itself. A D-Bus StartTransientUnit moves the tree into
`agentos-brain-*.scope`. When the brain's first process ends, or the caller hangs up, the
namespace's init is killed and the kernel kills everything left in it, setsid or not, with or
without systemd. The caller's own (detached) programs start without namespaces, dropped in C
after fork by CPython's subprocess.

Each attached brain is written to /run/agentos-runas/brains/ while it runs. The helper sweeps
what is recorded there when it starts and when it stops (ExecStopPost), so a crash or a kill
of the helper never leaves a brain running on its own.

`whoami` answers the brain user the helper serves (name, uid, gid, home, groups): agentosd
starts a brain through the helper only when that is the brain user it expects, and the
confinement probe checks that it ran as exactly that uid.

The helper's unit (deploy/agentos-runas.service) sets NoNewPrivileges and RestrictSUIDSGID,
which everything it starts inherits: a brain, or a shim started as the caller outside agentosd's
own sandbox, can never gain privileges through a setuid program either. It runs this file as a
script with `python -I -S`: nothing from site-packages (and no .pth file) runs as root.

Why not polkit: systemd's manage-units check gives a polkit rule the unit name and the verb, never
a transient unit's properties, so a rule that lets `openajar` start `agentos-brain-*` units also
lets it start one with User=root. Why not sudo or a setuid binary: NoNewPrivileges blocks them,
and dropping NoNewPrivileges for agentosd would give everything it runs (browsers included) a path
to setuid programs. docs/DEVELOPING.md "Running agentosd as its own user" has the operator side.

    python -I -S -B /opt/agentos/src/openajar/runas.py serve --caller openajar --brain-user agentos-brain
"""

from __future__ import annotations

import argparse
import contextlib
import grp
import json
import logging
import os
import pwd
import re
import resource
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("openajar.runas")

PROTOCOL = 1
DEFAULT_SOCKET = Path("/run/agentos-runas/runas.sock")
MAX_REQUEST = 1 << 20
MAX_ARGV = 512
MAX_ENV = 256
MAX_DEPTH = 4                       # {"env": {...}} is 2; nothing in the schema nests deeper
MAX_CONNECTIONS = 64
REQUEST_TIMEOUT = 10.0              # a caller that stalls mid-request loses its slot
ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")    # always fullmatch: `$` takes a trailing newline
UNIT_RE = re.compile(r"agentos-brain-[A-Za-z0-9_.-]{1,96}")
# a brain user in any of these (or in the caller's group) could read what they guard
PRIVILEGED_GROUPS = ("root", "adm", "sudo", "wheel", "docker", "systemd-journal", "disk", "shadow", "lxd", "libvirt",
                     "kvm")
SIGNALS = {int(signal.SIGINT), int(signal.SIGTERM), int(signal.SIGKILL), int(signal.SIGHUP)}
RLIMITS = {"core": resource.RLIMIT_CORE, "nofile": resource.RLIMIT_NOFILE, "fsize": resource.RLIMIT_FSIZE}
# (min, max) for each scope property the caller may set; anything else is refused
SCOPE_BOUNDS = {"memory_mb": (64, 1 << 20), "tasks_max": (8, 1 << 20), "cpu_quota_pct": (1, 100 * 64)}
# entered after the drop: wait on the gate fd ($1) until the scope is set up, close it, cd, exec.
# bash, not sh: dash takes only single-digit fds in a redirection, and the gate's number is not.
GATE = 'g=$1; shift; read -r _ <&"$g" && eval "exec $g<&-" && cd -- "$1" && shift && exec "$@"'
# the same as the init of a brain's PID namespace: it runs the program as its child, not by exec.
# An init only gets the signals it handles, so SIGINT and SIGTERM to the process group reach the
# program, and when the program ends the init exits and the kernel kills what is left.
NS_GATE = 'g=$1; shift; read -r _ <&"$g" && eval "exec $g<&-" && cd -- "$1" && shift && { "$@"; s=$?; exit $s; }'
NS_INIT = '"$@"; s=$?; exit $s'  # the init alone (no gate), for a root agentosd's own drop
# entered first after the drop, under `env -i`: load the request's env from the fd (NUL separated
# NAME=value, names already checked against ENV_KEY_RE), close it, and exec the rest. Through an
# fd, never argv: every argv is world readable in /proc/<pid>/cmdline, and the env carries the
# child grant token. The fd is a literal in the script (env_load) and the one shell variable has
# a name no request may use (LOADER_VAR), so nothing in the env can steer the loader. PWD, OLDPWD
# and SHLVL are this shell's own, so the program sees what `env -i` with the variables in argv
# gave it. A name bash keeps read-only (UID, SHELLOPTS) cannot be set by a shell, so it is
# skipped, and bash's own counters (SECONDS, FUNCNAME, PIPESTATUS) do not come through as sent.
LOADER_VAR = "__agentos_env_kv"
READONLY_NAMES = ("UID", "EUID", "PPID", "SHELLOPTS", "BASHOPTS", "BASH_VERSINFO", "BASHPID", "GROUPS")


def env_load(fd: int) -> str:
    v = LOADER_VAR
    return (f"unset PWD OLDPWD SHLVL; while IFS= read -r -d '' {v}; do case ${{{v}%%=*}} in "
            f"{'|'.join(READONLY_NAMES)}) continue;; esac; export -- \"${v}\"; done <&{int(fd)}; "
            f"unset {v}; exec {int(fd)}<&-; exec \"$@\"")


TOOLS = {"prlimit": "/usr/bin/prlimit", "unshare": "/usr/bin/unshare", "setpriv": "/usr/bin/setpriv",
         "env": "/usr/bin/env", "bash": "/bin/bash"}


class RunasError(Exception):
    """A refused or failed request; `code` goes back to the caller."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---- request schema --------------------------------------------------------------------------

@dataclass
class SpawnRequest:
    identity: str                      # "brain" | "caller"
    argv: list[str]
    env: dict[str, str]
    cwd: str
    scope: str | None = None           # the unit name without ".scope"
    limits: dict[str, int] = field(default_factory=dict)
    rlimits: dict[str, int] = field(default_factory=dict)
    detach: bool = False


def _text(value: Any, what: str, limit: int = 65536) -> str:
    if not isinstance(value, str) or "\0" in value or len(value) > limit:
        raise RunasError("invalid", f"{what} must be a string without NUL, at most {limit} characters")
    return value


def parse_request(data: dict[str, Any]) -> SpawnRequest:
    """Validate one spawn request. Unknown keys are refused, so a field added later cannot be
    silently ignored by an older helper."""
    if not isinstance(data, dict):
        raise RunasError("invalid", "the request must be a JSON object")
    allowed = {"v", "op", "identity", "argv", "env", "cwd", "scope", "limits", "rlimits", "detach"}
    extra = set(data) - allowed
    if extra:
        raise RunasError("invalid", f"unknown fields: {', '.join(sorted(extra))}")
    if data.get("v") != PROTOCOL or data.get("op") != "spawn":
        raise RunasError("invalid", f"expected {{v: {PROTOCOL}, op: spawn}}")
    identity = data.get("identity")
    if identity not in ("brain", "caller"):
        raise RunasError("invalid", "identity must be brain or caller (users are fixed by the helper)")
    argv = data.get("argv")
    if not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV:
        raise RunasError("invalid", f"argv must be a list of 1 to {MAX_ARGV} strings")
    argv = [_text(a, "argv items") for a in argv]
    if not argv[0]:
        raise RunasError("invalid", "argv[0] is empty")
    env = data.get("env") or {}
    if not isinstance(env, dict) or len(env) > MAX_ENV:
        raise RunasError("invalid", f"env must be an object with at most {MAX_ENV} entries")
    for k, v in env.items():
        if not isinstance(k, str) or not ENV_KEY_RE.fullmatch(k) or k == LOADER_VAR:
            raise RunasError("invalid", f"env name {k!r} is not a plain variable name")
        _text(v, f"env {k}")
    cwd = _text(data.get("cwd") or "/", "cwd", 4096)
    if not cwd.startswith("/"):
        raise RunasError("invalid", "cwd must be an absolute path")
    scope = data.get("scope")
    if scope is not None and (not isinstance(scope, str) or not UNIT_RE.fullmatch(scope)):
        raise RunasError("invalid", "scope must be an agentos-brain-* unit name")
    limits = data.get("limits") or {}
    if not isinstance(limits, dict):
        raise RunasError("invalid", "limits must be an object")
    for k, v in limits.items():
        lo_hi = SCOPE_BOUNDS.get(k)
        if lo_hi is None:
            raise RunasError("invalid", f"limit {k!r} is not allowed (only {', '.join(SCOPE_BOUNDS)})")
        if not isinstance(v, int) or isinstance(v, bool) or not lo_hi[0] <= v <= lo_hi[1]:
            raise RunasError("invalid", f"limit {k} must be an integer from {lo_hi[0]} to {lo_hi[1]}")
    if limits and scope is None:
        raise RunasError("invalid", "limits need a scope")
    rlimits = data.get("rlimits") or {}
    if not isinstance(rlimits, dict):
        raise RunasError("invalid", "rlimits must be an object")
    for k, v in rlimits.items():
        if k not in RLIMITS:
            raise RunasError("invalid", f"rlimit {k!r} is not allowed (only {', '.join(RLIMITS)})")
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise RunasError("invalid", f"rlimit {k} must be a non-negative integer")
    detach = data.get("detach", False)
    if not isinstance(detach, bool):
        raise RunasError("invalid", "detach must be true or false")
    if detach and identity != "caller":
        raise RunasError("invalid", "only the caller's own programs can be detached; brains always stay attached")
    return SpawnRequest(identity, argv, dict(env), cwd, scope, dict(limits), dict(rlimits), detach)


# ---- identities ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Identity:
    name: str
    uid: int
    gid: int
    home: str = ""


def _account(name: str) -> Identity:
    try:
        pw = pwd.getpwnam(name)
    except KeyError:
        raise RunasError("config", f"no user {name}") from None
    return Identity(pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir)


def _groups_of(user: Identity) -> set[int]:
    gids = {user.gid}
    for g in grp.getgrall():
        if user.name in g.gr_mem:
            gids.add(g.gr_gid)
    return gids


def privileged_gids(names: tuple[str, ...] = PRIVILEGED_GROUPS) -> dict[int, str]:
    """gid -> name of the groups a brain user must not be in (those that exist here), and root."""
    out = {0: "root"}
    for name in names:
        with contextlib.suppress(KeyError):
            out[grp.getgrnam(name).gr_gid] = name
    return out


def group_problem(user: str, groups: set[int], forbidden: dict[int, str]) -> str | None:
    """Why `user`, a member of `groups` (primary and supplementary), may not run brains; None if fine."""
    bad = sorted(g for g in groups if g in forbidden)
    if not bad:
        return None
    return f"{user} is in the group{'s' if len(bad) > 1 else ''} {', '.join(f'{forbidden[g]} ({g})' for g in bad)}"


def check_identities(caller: Identity, brain: Identity, groups_of: Callable[[Identity], set[int]] = _groups_of,
                     forbidden: dict[int, str] | None = None) -> None:
    """The two identities the helper serves. Refused: root on either side, one user for both (the
    brain would read what the caller owns), and a brain user in the caller's group or in any
    privileged group (PRIVILEGED_GROUPS: logs, the journal, docker, disks, sudo), primary or
    supplementary."""
    if caller.uid == 0 or brain.uid == 0 or brain.gid == 0:
        raise RunasError("config", "neither the caller nor the brain user may be root")
    if caller.uid == brain.uid:
        raise RunasError("config", f"the brain user must differ from the caller ({caller.name})")
    bad = {**(privileged_gids() if forbidden is None else forbidden), caller.gid: f"{caller.name}'s group"}
    problem = group_problem(brain.name, groups_of(brain), bad)
    if problem:
        raise RunasError("config", f"{problem}; a brain user must be in no privileged group")


def check_code_owner(paths: list[Path], lstat: Callable[[Path], os.stat_result] = os.lstat) -> None:
    """The helper runs its own code as root: refuse to serve when anything on the way to it can be
    changed by someone other than root (the caller could otherwise swap the helper's code)."""
    seen: set[Path] = set()
    for start in paths:
        p = start
        while True:
            if p not in seen:
                seen.add(p)
                st = lstat(p)
                if st.st_uid != 0 or (stat.S_IMODE(st.st_mode) & 0o022 and not stat.S_ISLNK(st.st_mode)
                                      and not (stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX)):
                    raise RunasError("config", f"{p} can be changed by a user other than root; the helper "
                                     "will not run code from it as root")
            if p.parent == p:
                break
            p = p.parent


# ---- the system side (fakeable) --------------------------------------------------------------

@dataclass
class Child:
    """A started program. `pid` leads its process group (the helper's child, reaped by it); a
    brain's `init` is the init of its PID namespace: killing it kills everything in there. The
    start times guard against a reused pid after a helper restart."""
    pid: int
    init: int | None = None
    pid_start: str | None = None
    init_start: str | None = None
    pidfd: int | None = field(default=None, compare=False)

    def record(self) -> dict[str, Any]:
        return {"pid": self.pid, "init": self.init, "pid_start": self.pid_start, "init_start": self.init_start}


def lowered_rlimits(rlimits: dict[str, int]) -> dict[str, tuple[int, int]]:
    """(soft, hard) for each requested rlimit, never above the helper's own (a limit only goes down)."""
    out = {}
    for name, value in rlimits.items():
        _soft, hard = resource.getrlimit(RLIMITS[name])
        new_hard = value if hard == resource.RLIM_INFINITY else min(hard, value)
        out[name] = (min(value, new_hard), new_hard)
    return out


def has_namespace_tools() -> bool:
    return sys.platform.startswith("linux") and all(os.access(p, os.X_OK) for p in TOOLS.values())


def env_fd(env: dict[str, str]) -> int:
    """A file descriptor holding `env` for env_load, read from its start: a memfd (an unlinked
    temp file where there is none). Close-on-exec, so only the chain it is passed to gets it (root's
    unshare holds it while the brain runs; the loader closes it before the brain's program)."""
    for k, v in env.items():  # the helper checked these already; a root agentosd's own launch did not
        if not ENV_KEY_RE.fullmatch(k) or k == LOADER_VAR:
            raise ValueError(f"env name {k!r} is not a plain variable name")
        if "\0" in v:
            raise ValueError(f"env {k} holds a NUL")
    # surrogateescape: a value that came from undecodable bytes goes back as those bytes
    raw = b"".join(f"{k}={v}".encode("utf-8", "surrogateescape") + b"\0" for k, v in env.items())
    if hasattr(os, "memfd_create"):
        fd = os.memfd_create("agentos-env", os.MFD_CLOEXEC)
    else:
        fd, name = tempfile.mkstemp(prefix=".agentos-env-")
        os.unlink(name)
    try:
        view = memoryview(raw)
        while view:
            view = view[os.write(fd, view):]
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        raise
    return fd


def ns_chain(uid: int, gid: int, env_fd: int, rlimits: dict[str, int] | None = None) -> list[str]:
    """The fixed programs root runs to start a brain: prlimit, unshare (a PID namespace and a
    mount namespace with its own /proc), setpriv (the drop), then `env -i` and env_load, which
    reads the brain's env from `env_fd` (from env_fd(); pass it to the child). Append the
    program's argv. Run it with the helper's (or agentosd's) own minimal env: none of the brain's
    env reaches anything before the drop, and none of it is ever in an argv."""
    chain: list[str] = []
    if rlimits:
        chain += [TOOLS["prlimit"], *(f"--{n}={s}:{h}" for n, (s, h) in lowered_rlimits(rlimits).items()), "--"]
    return [*chain, TOOLS["unshare"], "--pid", "--fork", "--mount-proc", "--",
            TOOLS["setpriv"], f"--reuid={uid}", f"--regid={gid}", "--clear-groups", "--nnp", "--",
            TOOLS["env"], "-i", TOOLS["bash"], "-c", env_load(env_fd), "agentos-env"]


class System:
    """What the helper does as root. Tests replace it with a fake."""

    def has_systemd(self) -> bool:
        return Path("/run/systemd/system").is_dir() and shutil.which("busctl") is not None

    def has_namespaces(self) -> bool:
        return has_namespace_tools()

    def spawn(self, argv: list[str], *, env: dict[str, str], user: Identity, fds: tuple[int, int, int],
              gate: int, rlimits: dict[str, int], isolate: bool) -> Child:
        if not isolate:
            proc = subprocess.Popen(  # argv: the gate wrapper, then the validated request argv
                argv, env=env, cwd="/", stdin=fds[0], stdout=fds[1], stderr=fds[2], pass_fds=(gate,),
                start_new_session=True, close_fds=True, user=user.uid, group=user.gid, extra_groups=[],
                umask=0o077)
            proc.returncode = 0  # the helper reaps it itself (waitid), so Popen must never wait on it
            for name, value in rlimits.items():
                self.set_rlimit(proc.pid, name, value)
            return Child(proc.pid, pid_start=self.start_time(proc.pid))
        # root runs only these fixed programs, with the helper's own env; the request's env and
        # argv are arguments they pass on, and reach a program only after setpriv has dropped
        efd = env_fd(env)
        try:
            chain = [*ns_chain(user.uid, user.gid, efd, rlimits), *argv]
            proc = subprocess.Popen(chain, env=ROOT_ENV, cwd="/", stdin=fds[0], stdout=fds[1], stderr=fds[2],
                                    pass_fds=(gate, efd), start_new_session=True, close_fds=True, umask=0o077)
        finally:
            os.close(efd)
        proc.returncode = 0
        child = Child(proc.pid, pid_start=self.start_time(proc.pid))
        init = self._first_child(proc.pid, timeout=5)
        if init is None:
            self.killpg(proc.pid, signal.SIGKILL)
            self.reap(proc.pid)
            raise RunasError("spawn_failed", "the brain's PID namespace did not start")
        child.init, child.init_start = init, self.start_time(init)
        with contextlib.suppress(OSError, AttributeError):
            child.pidfd = os.pidfd_open(init)
        return child

    def _first_child(self, pid: int, timeout: float) -> int | None:
        """The process unshare forked (the namespace's init); it waits at the gate, so it is there
        and has no children of its own yet."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError, ValueError):
                kids = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
                if kids:
                    return int(kids[0])
            time.sleep(0.01)
        return None

    def start_time(self, pid: int) -> str | None:
        """The process's start time in clock ticks since boot (/proc/<pid>/stat field 22)."""
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return None

    def start_scope(self, unit: str, pids: list[int], limits: dict[str, int]) -> None:
        props: list[str] = ["PIDs", "au", str(len(pids)), *map(str, pids), "CollectMode", "s", "inactive-or-failed"]
        if "memory_mb" in limits:
            props += ["MemoryMax", "t", str(limits["memory_mb"] * 1024 * 1024)]
        if "tasks_max" in limits:
            props += ["TasksMax", "t", str(limits["tasks_max"])]
        if "cpu_quota_pct" in limits:
            props += ["CPUQuotaPerSecUSec", "t", str(limits["cpu_quota_pct"] * 10_000)]
        count = 2 + sum(1 for k in ("memory_mb", "tasks_max", "cpu_quota_pct") if k in limits)
        argv = ["busctl", "call", "--quiet", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager", "StartTransientUnit", "ssa(sv)a(sa(sv))",
                f"{unit}.scope", "fail", str(count), *props, "0"]
        out = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False, env=ROOT_ENV)
        if out.returncode != 0:
            raise RunasError("scope_failed", f"could not start {unit}.scope: {out.stderr.strip()[:300]}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # the job moves the pids; wait until it has
            with contextlib.suppress(OSError):
                if all(f"/{unit}.scope" in Path(f"/proc/{pid}/cgroup").read_text() for pid in pids):
                    return
            time.sleep(0.02)
        raise RunasError("scope_failed", f"{unit}.scope did not take pids {pids}")

    def kill_scope(self, unit: str) -> None:
        subprocess.run(["systemctl", "kill", "--signal=SIGKILL", f"{unit}.scope"], capture_output=True,
                       timeout=15, check=False, env=ROOT_ENV)

    def kill_tree(self, child: Child) -> None:
        """SIGKILL the namespace's init (the kernel then kills all of the namespace, setsid or not)
        and the process group. A pid whose start time changed is someone else's now."""
        if child.init:
            if child.pidfd is not None:
                with contextlib.suppress(OSError):
                    signal.pidfd_send_signal(child.pidfd, signal.SIGKILL)
            elif child.init_start is not None and self.start_time(child.init) == child.init_start:
                with contextlib.suppress(OSError):
                    os.kill(child.init, signal.SIGKILL)
        if child.pid_start is None or self.start_time(child.pid) == child.pid_start:
            self.killpg(child.pid, signal.SIGKILL)

    def set_rlimit(self, pid: int, name: str, value: int) -> None:
        soft, hard = lowered_rlimits({name: value})[name]
        resource.prlimit(pid, RLIMITS[name], (soft, hard))

    def exited(self, pid: int, timeout: float) -> int | None:
        """The exit code once the program has exited, without reaping it: until it is reaped its
        pid (and so its process group id) cannot be reused, so a later killpg cannot hit another
        process. None while it runs."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                return -1
            if info is not None:
                return info.si_status if info.si_code == os.CLD_EXITED else -info.si_status
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(0.05, max(deadline - time.monotonic(), 0)))

    def reap(self, pid: int) -> None:
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)

    def killpg(self, pid: int, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, sig)


ROOT_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


def peer_uid(conn: socket.socket) -> int:
    if hasattr(socket, "SO_PEERCRED"):
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", creds)[1]
    # macOS (dev and tests): LOCAL_PEERCRED returns a struct xucred
    raw = conn.getsockopt(0, 0x001, 76)
    return struct.unpack_from("Ii", raw)[1]


# ---- the server ------------------------------------------------------------------------------

def _send(conn: socket.socket, msg: dict[str, Any]) -> None:
    conn.sendall((json.dumps(msg) + "\n").encode())


def too_deep(raw: bytes, limit: int) -> bool:
    """True when JSON text nests arrays and objects deeper than `limit` (checked before parsing:
    a deep enough document makes json.loads raise RecursionError)."""
    depth, in_str, esc = 0, False, False
    for b in raw:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:      # backslash
                esc = True
            elif b == 0x22:      # quote
                in_str = False
        elif b == 0x22:
            in_str = True
        elif b in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > limit:
                return True
        elif b in (0x5D, 0x7D):  # ] }
            depth -= 1
    return False


def _recv_request(conn: socket.socket) -> tuple[dict[str, Any], list[int]]:
    """A 4-byte big-endian length, then that much JSON; the stdio fds ride on the first bytes.
    The fds received are closed on any failure, so a bad request never leaks one."""
    head, fds, _flags, _addr = socket.recv_fds(conn, 65536, 8)
    try:
        if len(head) < 4:
            raise RunasError("invalid", "short request")
        size = struct.unpack(">I", head[:4])[0]
        if size > MAX_REQUEST:
            raise RunasError("invalid", "request too large")
        body = bytearray(head[4:])
        while len(body) < size:
            try:
                chunk = conn.recv(min(65536, size - len(body)))
            except TimeoutError:
                raise RunasError("invalid", "the request did not arrive in time") from None
            if not chunk:
                raise RunasError("invalid", "short request")
            body.extend(chunk)
        raw = bytes(body[:size])
        if too_deep(raw, MAX_DEPTH):
            raise RunasError("invalid", f"the request nests deeper than {MAX_DEPTH} levels")
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            raise RunasError("invalid", "the request is not JSON") from None
        if not isinstance(data, dict):
            raise RunasError("invalid", "the request must be a JSON object")
        return data, fds
    except BaseException:
        for fd in fds:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise


class Server:
    def __init__(self, *, caller: Identity, brain: Identity, socket_path: Path = DEFAULT_SOCKET,
                 system: System | None = None, peer: Callable[[socket.socket], int] = peer_uid,
                 state_dir: Path | None = None, groups_of: Callable[[Identity], set[int]] = _groups_of):
        self.caller = caller
        self.brain = brain
        self.socket_path = socket_path
        self.system = system or System()
        self.peer = peer
        self.groups_of = groups_of
        self.scopes = self.system.has_systemd()
        self.isolate = self.system.has_namespaces()
        self.state_dir = state_dir or socket_path.parent / "brains"
        self._live: dict[int, tuple[Child, str | None]] = {}   # attached pid -> (child, scope)
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    # ---- socket ----
    def bind(self) -> socket.socket:
        """Sweep what an earlier run left recorded, then listen."""
        d = self.socket_path.parent
        d.mkdir(parents=True, exist_ok=True)
        root = os.geteuid() == 0  # tests bind as themselves
        if root:
            os.chown(d, 0, self.caller.gid)
        os.chmod(d, 0o750)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.sweep_recorded()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            sock.bind(str(self.socket_path))
        finally:
            os.umask(old)
        if root:
            os.chown(self.socket_path, 0, self.caller.gid)
        os.chmod(self.socket_path, 0o660)
        sock.listen(16)
        self._sock = sock
        return sock

    def serve_forever(self) -> None:
        sock = self._sock or self.bind()
        sock.settimeout(0.5)
        backoff = 0.05
        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError as e:
                if self._stop.is_set():
                    break
                # out of fds (EMFILE, ENFILE), of memory, or a connection gone before accept: wait, go on
                log.warning("accept failed (%s); trying again in %.2fs", e, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 2.0)
                continue
            backoff = 0.05
            if not self._slots.acquire(blocking=False):
                with contextlib.suppress(OSError):
                    conn.settimeout(1)
                    _send(conn, {"ok": False, "error": {"code": "busy", "message": "too many programs running"}})
                conn.close()
                continue
            try:
                threading.Thread(target=self._serve_slot, args=(conn,), daemon=True).start()
            except RuntimeError:  # no thread to be had: refuse this one, keep serving
                conn.close()
                self._slots.release()

    def stop(self) -> None:
        """Stop accepting and kill every attached brain (a helper restart interrupts brain tasks;
        they resume from their transcripts). Detached programs keep running."""
        self._stop.set()
        with self._lock:
            live = dict(self._live)
        for pid, (child, scope) in live.items():
            self._sweep(child, scope)
            self._forget(pid)

    # ---- what is running, on disk, for a crash ----
    def _record(self, child: Child, scope: str | None) -> None:
        path = self.state_dir / f"{child.pid}.json"
        tmp = self.state_dir / f".{child.pid}.tmp"
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            json.dump({**child.record(), "scope": scope}, fh)
        os.replace(tmp, path)

    def _forget(self, pid: int) -> None:
        with contextlib.suppress(OSError):
            (self.state_dir / f"{pid}.json").unlink()

    def sweep_recorded(self) -> int:
        """Kill every brain an earlier run of the helper recorded and did not see end (it crashed,
        or was killed). Runs at startup and from ExecStopPost."""
        n = 0
        for path in sorted(self.state_dir.glob("*.json")):
            try:
                rec = json.loads(path.read_text())
                child = Child(int(rec["pid"]), rec.get("init"), rec.get("pid_start"), rec.get("init_start"))
                scope = rec.get("scope")
            except (OSError, ValueError, KeyError, TypeError):
                path.unlink(missing_ok=True)
                continue
            if scope is not None and not (isinstance(scope, str) and UNIT_RE.fullmatch(scope)):
                scope = None
            self._sweep(child, scope)
            path.unlink(missing_ok=True)
            n += 1
        if n:
            log.warning("swept %d brain(s) left by an earlier run", n)
        return n

    def _serve_slot(self, conn: socket.socket) -> None:
        try:
            self.handle(conn)
        except Exception:  # one bad connection never takes the helper down
            log.exception("runas connection failed")
        finally:
            with contextlib.suppress(OSError):
                conn.close()
            self._slots.release()

    # ---- one connection ----
    def whoami(self) -> dict[str, Any]:
        b, c = self.brain, self.caller
        return {"brain": {"name": b.name, "uid": b.uid, "gid": b.gid, "home": b.home,
                          "groups": sorted(self.groups_of(b))},
                "caller": {"name": c.name, "uid": c.uid, "gid": c.gid},
                "scopes": self.scopes, "namespaces": self.isolate}

    def handle(self, conn: socket.socket) -> None:
        conn.settimeout(REQUEST_TIMEOUT)
        uid = self.peer(conn)
        if uid != self.caller.uid:
            log.warning("refused a connection from uid %s", uid)
            _send(conn, {"ok": False, "error": {"code": "forbidden", "message": f"only {self.caller.name} may ask"}})
            return
        fds: list[int] = []
        try:
            data, fds = _recv_request(conn)
            if data.get("op") == "whoami" and data.get("v") == PROTOCOL:
                if fds or set(data) - {"v", "op"}:
                    raise RunasError("invalid", "whoami takes no fields and no file descriptors")
                _send(conn, {"ok": True, **self.whoami()})
                return
            if len(fds) != 3:
                raise RunasError("invalid", "send exactly three file descriptors (stdin, stdout, stderr)")
            req = parse_request(data)
            child, scope = self._spawn(req, (fds[0], fds[1], fds[2]))
        except RunasError as e:
            _send(conn, {"ok": False, "error": {"code": e.code, "message": e.message}})
            return
        finally:
            for fd in fds:
                with contextlib.suppress(OSError):
                    os.close(fd)
        user = self.brain if req.identity == "brain" else self.caller
        ok = {"ok": True, "pid": child.pid, "scope": f"{scope}.scope" if scope else None, "uid": user.uid,
              "gid": user.gid}
        if req.detach:
            with contextlib.suppress(OSError):
                _send(conn, ok)
            threading.Thread(target=self._reap_detached, args=(child.pid,), daemon=True).start()
            return
        self._attend(conn, child, scope, ok)

    def _spawn(self, req: SpawnRequest, fds: tuple[int, int, int]) -> tuple[Child, str | None]:
        user = self.brain if req.identity == "brain" else self.caller
        isolate = req.identity == "brain" and self.isolate
        if req.limits and not self.scopes:
            raise RunasError("no_scope", "this host has no systemd, so no scope can hold the limits asked for; "
                             "ask without limits")
        scope = req.scope if self.scopes else None
        r, w = os.pipe()
        try:
            child = self.system.spawn(["/bin/bash", "-c", NS_GATE if isolate else GATE, "agentos-runas", str(r),
                                       req.cwd, *req.argv], env=req.env, user=user, fds=fds, gate=r,
                                      rlimits=req.rlimits, isolate=isolate)
        except (OSError, RunasError) as e:
            os.close(r)
            os.close(w)
            if isinstance(e, RunasError):
                raise
            raise RunasError("spawn_failed", f"could not start {Path(req.argv[0]).name}: {e.strerror or e}") from None
        os.close(r)
        try:
            if scope:
                self.system.start_scope(scope, [p for p in (child.pid, child.init) if p], req.limits)
            if not req.detach:
                with self._lock:
                    self._live[child.pid] = (child, scope)
                self._record(child, scope)  # before the gate opens: a crash from here on is swept
        except (RunasError, OSError, ValueError) as e:
            os.close(w)
            self._end(child, scope)
            if isinstance(e, RunasError):
                raise
            raise RunasError("spawn_failed", f"could not set up the program: {e}") from None
        os.write(w, b"go\n")
        os.close(w)
        log.info("started pid %s as %s%s%s", child.pid, user.name, f" in {scope}.scope" if scope else "",
                 " in its own PID namespace" if child.init else "")
        return child, scope

    def _attend(self, conn: socket.socket, child: Child, scope: str | None, ok: dict[str, Any]) -> None:
        """Relay signals until the program exits, report its exit code, and sweep its namespace,
        process group and scope when the caller is done (or gone)."""
        conn.settimeout(0.2)
        buf = b""
        code: int | None = None
        try:
            _send(conn, ok)
            while True:
                if code is None:
                    code = self.system.exited(child.pid, 0)
                    if code is not None:
                        if child.init and 128 < code <= 128 + 64:  # the init's exit for a program killed by a signal
                            code = -(code - 128)
                        _send(conn, {"exit": code})
                try:
                    chunk = conn.recv(4096)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._command(child, scope, line)
                if len(buf) > 4096:
                    break
        except OSError:
            pass
        finally:
            self._end(child, scope)

    def _command(self, child: Child, scope: str | None, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            return
        if not isinstance(msg, dict):
            return
        if msg.get("op") == "signal" and msg.get("sig") in SIGNALS:
            self.system.killpg(child.pid, int(msg["sig"]))  # the leader is not reaped yet: its pgid is ours
        elif msg.get("op") == "sweep":
            self._sweep(child, scope)

    def _sweep(self, child: Child, scope: str | None) -> None:
        self.system.kill_tree(child)
        if scope:
            self.system.kill_scope(scope)

    def _end(self, child: Child, scope: str | None) -> None:
        self._sweep(child, scope)
        self.system.reap(child.pid)
        if child.pidfd is not None:
            with contextlib.suppress(OSError):
                os.close(child.pidfd)
        with self._lock:
            self._live.pop(child.pid, None)
        self._forget(child.pid)

    def _reap_detached(self, pid: int) -> None:
        while self.system.exited(pid, 3600) is None:
            pass
        self.system.reap(pid)


# ---- the client --------------------------------------------------------------------------------

class HelperProc:
    """A program the helper started, with the subset of Popen the shim and the service use."""

    def __init__(self, sock: socket.socket, pid: int, scope: str | None, uid: int | None = None):
        self.sock = sock
        self.pid = pid
        self.scope = scope
        self.uid = uid
        self.returncode: int | None = None
        self._buf = b""

    def _read(self, timeout: float | None) -> None:
        if self.sock.fileno() < 0:
            return
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(4096)
        except (TimeoutError, BlockingIOError):
            return
        except OSError:
            chunk = b""
        if not chunk:
            if self.returncode is None:
                self.returncode = -int(signal.SIGKILL)  # the helper went away: treat it as killed
            self.sock.close()
            return
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            with contextlib.suppress(ValueError):
                msg = json.loads(line)
                if isinstance(msg, dict) and "exit" in msg:
                    self.returncode = int(msg["exit"])

    def poll(self) -> int | None:
        if self.returncode is None:
            self._read(0)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.returncode is None:
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                raise subprocess.TimeoutExpired("runas", timeout or 0)
            self._read(0.5 if left is None else min(left, 0.5))
        return self.returncode

    def signal_group(self, sig: int) -> None:
        self._tell({"op": "signal", "sig": int(sig)})

    def sweep(self) -> None:
        self._tell({"op": "sweep"})

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()

    def _tell(self, msg: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):
            self.sock.sendall((json.dumps(msg) + "\n").encode())


def _exchange(socket_path: Path, body: bytes, fds: list[int], timeout: float) -> tuple[socket.socket, dict[str, Any]]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path))
    except OSError as e:
        sock.close()
        raise RunasError("unavailable", f"cannot reach the agentos-runas helper at {socket_path}: {e}") from None
    try:
        first = struct.pack(">I", len(body)) + body[:60000]
        if fds:
            socket.send_fds(sock, [first], fds)
        else:
            sock.sendall(first)
        if len(body) > 60000:
            sock.sendall(body[60000:])
    except OSError:
        pass  # a refusal closes the connection early: its answer is still there to read
    line = b""
    try:
        while not line.endswith(b"\n"):
            chunk = sock.recv(1)
            if not chunk:
                break
            line += chunk
    except OSError:
        pass
    if not line.endswith(b"\n"):
        sock.close()
        raise RunasError("unavailable", "the agentos-runas helper closed the connection without an answer")
    resp = json.loads(line)
    if not resp.get("ok"):
        sock.close()
        err = resp.get("error") or {}
        raise RunasError(err.get("code", "error"), err.get("message", "refused"))
    return sock, resp


def request(socket_path: Path, req: dict[str, Any], fds: tuple[int, int, int], *, timeout: float = 30,
            expect_uid: int | None = None) -> HelperProc:
    """Send one spawn request; returns the running program (attached) or raises RunasError.
    With `expect_uid`, a program the helper started as any other uid is killed at once (closing
    the connection sweeps it) and refused."""
    body = json.dumps({"v": PROTOCOL, "op": "spawn", **req}).encode()
    sock, resp = _exchange(socket_path, body, list(fds), timeout)
    uid = resp.get("uid")
    if expect_uid is not None and uid != expect_uid:
        sock.close()
        raise RunasError("identity", f"the helper started the program as uid {uid}, not the brain user "
                         f"(uid {expect_uid})")
    return HelperProc(sock, int(resp["pid"]), resp.get("scope"), uid)


def whoami(socket_path: Path, *, timeout: float = 10) -> dict[str, Any]:
    """The identities the helper serves: {brain: {name, uid, gid, home, groups}, caller, scopes,
    namespaces}."""
    sock, resp = _exchange(socket_path, json.dumps({"v": PROTOCOL, "op": "whoami"}).encode(), [], timeout)
    sock.close()
    return resp


# ---- entry point -----------------------------------------------------------------------------

def _code_paths() -> list[Path]:
    """What the helper executes as root: this file, the interpreter, and everything on sys.path
    (the standard library, and site-packages when site was imported), with every .pth file in
    them (site runs those at startup). The unit runs it with -I -S, so sys.path holds only the
    standard library; run another way, whatever is on sys.path is checked instead."""
    import glob

    entries = [Path(p).resolve() for p in sys.path if p and os.path.exists(p)]
    pth = [Path(f) for e in entries if e.is_dir() for f in sorted(glob.glob(os.path.join(e, "*.pth")))]
    return [Path(__file__).resolve(), Path(sys.executable).resolve(), Path(os.__file__).resolve().parent,
            *entries, *pth]


def serve(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentos-runas")
    ap.add_argument("command", choices=["serve", "check", "sweep"])
    ap.add_argument("--caller", required=True)
    ap.add_argument("--brain-user", required=True)
    ap.add_argument("--socket", default=str(DEFAULT_SOCKET))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if os.geteuid() != 0:
            raise RunasError("config", "agentos-runas runs as root")
        caller, brain = _account(args.caller), _account(args.brain_user)
        if args.command == "sweep":  # ExecStopPost: whatever a crashed or killed run left behind
            server = Server(caller=caller, brain=brain, socket_path=Path(args.socket))
            if server.state_dir.is_dir():
                server.sweep_recorded()
            return 0
        check_identities(caller, brain)
        check_code_owner(_code_paths())
        if not System().has_namespaces():
            raise RunasError("config", f"needs util-linux's {', '.join(TOOLS)} at {', '.join(TOOLS.values())} "
                             "(each brain starts in a PID namespace of its own)")
    except RunasError as e:
        print(f"agentos-runas: {e.message}", file=sys.stderr)
        return 78  # EX_CONFIG: systemd shows it as a configuration error
    if args.command == "check":
        print(f"ok: {caller.name} may start programs as {brain.name}")
        return 0
    server = Server(caller=caller, brain=brain, socket_path=Path(args.socket))
    server.bind()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: server.stop())
    log.info("serving %s for %s (brains run as %s)", args.socket, caller.name, brain.name)
    try:
        server.serve_forever()
    finally:  # whatever ends the loop, no brain outlives the helper
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(serve())
