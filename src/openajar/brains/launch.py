"""How a brain's programs start as the brain user: one interface, two mechanisms.

    DirectLauncher   agentosd (or its shim) is root and drops to the brain user itself. On
                     Linux through the same fixed chain as agentos-runas (unshare, setpriv,
                     env -i: each brain in a PID namespace of its own, so brains of one uid
                     cannot read each other's environment); elsewhere in C after fork
                     (subprocess `user=`). Without a brain user it runs programs as itself.
    HelperLauncher   agentosd is the unprivileged `agentos` user: the agentos-runas helper
                     (openajar.runas) starts them as the brain user and relays signals.

The shim starts the brain's CLI through one of them, the AUD-08 confinement check
(openajar.brains.confine) starts its probe through the same one, and BrainFiles reaches the
brain's own directories through it too: a non-root agentosd cannot chown, so it writes and
reads files in the brain's home by running small commands (mkdir, cat, tar, rm) as the brain.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from openajar import runas


class Proc(Protocol):
    pid: int
    returncode: int | None

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def signal_group(self, sig: int) -> None: ...
    def sweep(self) -> None: ...
    def close(self) -> None: ...


class Launcher(Protocol):
    kind: str           # "direct" | "helper"

    def spawn(self, argv: list[str], *, env: dict[str, str], cwd: str, stdin: int, stdout: int, stderr: int,
              rlimits: dict[str, int] | None = None, scope: str | None = None,
              limits: dict[str, int] | None = None) -> Proc: ...


def _set_rlimits(rlimits: dict[str, int]) -> None:
    import resource

    for name, value in rlimits.items():
        res = runas.RLIMITS.get(name)
        if res is None:
            continue
        try:
            _soft, hard = resource.getrlimit(res)
            new_hard = int(value) if hard == resource.RLIM_INFINITY else min(hard, int(value))
            resource.setrlimit(res, (min(int(value), new_hard), new_hard))
        except (ValueError, OSError):
            pass


@dataclass(frozen=True)
class BrainIdentity:
    """Who brains run as on this node, resolved once (BrainService.brain_identity) for the
    confinement probe, the attempt and BrainFiles alike. `name` None: agentosd's own user."""
    name: str | None
    uid: int
    gid: int
    home: str
    helper: Path | None = None          # the agentos-runas socket, when the helper starts brains
    groups: tuple[int, ...] = ()        # the brain user's groups, as the helper sees them
    scopes: bool = False                # the helper can put brains in systemd scopes

    def key(self) -> list[Any]:
        return [self.name, self.uid, self.gid, sorted(self.groups), str(self.helper) if self.helper else None]


class DirectProc:
    def __init__(self, proc: subprocess.Popen, *, ns: bool = False):
        self.proc = proc
        self.pid = proc.pid
        self.ns = ns

    def _code(self, code: int | None) -> int | None:
        # in a namespace the init reports a program killed by signal n as 128 + n
        if code is not None and self.ns and 128 < code <= 128 + 64:
            return -(code - 128)
        return code

    @property
    def returncode(self) -> int | None:
        return self._code(self.proc.returncode)

    def poll(self) -> int | None:
        return self._code(self.proc.poll())

    def wait(self, timeout: float | None = None) -> int:
        return self._code(self.proc.wait(timeout))  # type: ignore[return-value]

    def signal_group(self, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pid, sig)

    def sweep(self) -> None:
        self.signal_group(signal.SIGKILL)

    def close(self) -> None:
        pass


class DirectLauncher:
    """Fork and exec here; as root, drop to `run_as` ({"uid", "gid"}) first."""

    kind = "direct"

    def __init__(self, run_as: dict[str, int] | None = None):
        self.run_as = run_as

    def spawn(self, argv, *, env, cwd, stdin, stdout, stderr, rlimits=None, scope=None, limits=None) -> DirectProc:
        kwargs: dict[str, Any] = {}
        limits_now = dict(rlimits or {})
        ns = bool(self.run_as) and os.geteuid() == 0 and runas.has_namespace_tools()
        efd: int | None = None
        try:
            if ns:
                assert self.run_as is not None
                efd = runas.env_fd(env)  # the env goes through an fd: an argv is world readable
                argv = [*runas.ns_chain(int(self.run_as["uid"]), int(self.run_as["gid"]), efd, limits_now),
                        runas.TOOLS["bash"], "-c", runas.NS_INIT, "agentos-brain-init", *argv]
                env, limits_now = dict(runas.ROOT_ENV), {}
                kwargs["pass_fds"] = (efd,)
            elif self.run_as:
                kwargs.update(user=int(self.run_as["uid"]), group=int(self.run_as["gid"]), extra_groups=[])
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=True,
                close_fds=True, preexec_fn=(lambda: _set_rlimits(limits_now)) if limits_now else None,  # noqa: PLW1509 (single-threaded callers)
                **kwargs)
        finally:
            if efd is not None:
                os.close(efd)
        return DirectProc(proc, ns=ns)


class HelperLauncher:
    """Ask agentos-runas to start the program as the brain user (or, `identity="caller"`, as
    agentosd's own user in a scope of its own, detached). `expect_uid` is the uid the program
    must run as: the helper answers the uid it used, and anything else is killed and refused."""

    kind = "helper"

    def __init__(self, socket_path: str | Path, identity: str = "brain", *, detach: bool = False,
                 expect_uid: int | None = None):
        self.socket_path = Path(socket_path)
        self.identity = identity
        self.detach = detach
        self.expect_uid = expect_uid

    def spawn(self, argv, *, env, cwd, stdin, stdout, stderr, rlimits=None, scope=None,
              limits=None) -> runas.HelperProc:
        req: dict[str, Any] = {"identity": self.identity, "argv": list(argv), "env": dict(env), "cwd": cwd,
                               "rlimits": dict(rlimits or {}), "detach": self.detach}
        if scope:
            req["scope"] = scope
            if limits:
                req["limits"] = dict(limits)
        return runas.request(self.socket_path, req, (stdin, stdout, stderr), expect_uid=self.expect_uid)


def from_spec(spec: dict[str, Any] | None, run_as: dict[str, int] | None = None) -> Launcher:
    """The launcher a launch.json names (`{"kind": "helper", "socket": ..., "brain_uid": ...}`),
    else direct. A helper spec without the brain's uid is refused: the CLI must run as the uid
    the confinement check passed for, and nothing else."""
    if spec and spec.get("kind") == "helper":
        uid = spec.get("brain_uid")
        if not isinstance(uid, int) or isinstance(uid, bool):
            raise ValueError("launch.json names the agentos-runas helper but not the brain's uid")
        return HelperLauncher(spec["socket"], expect_uid=uid)
    return DirectLauncher(run_as)


def run(launcher: Launcher, argv: list[str], *, input: bytes | None = None, stdin_fd: int | None = None,
        env: dict[str, str] | None = None, cwd: str = "/", timeout: float = 120,
        max_bytes: int = 64 << 20) -> tuple[int, bytes, bytes]:
    """Run a short program to completion: (exit code, stdout, stderr). Output past `max_bytes` is
    dropped. A program still running after `timeout` is killed (exit code -9)."""
    env = env if env is not None else {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    in_r = in_w = None
    if stdin_fd is None:
        in_r, in_w = os.pipe()
    try:
        proc = launcher.spawn(argv, env=env, cwd=cwd, stdin=stdin_fd if stdin_fd is not None else in_r,
                              stdout=out_w, stderr=err_w)
    finally:
        for fd in (out_w, err_w, in_r):
            if fd is not None:
                os.close(fd)
    bufs: dict[int, bytearray] = {out_r: bytearray(), err_r: bytearray()}

    def drain(fd: int) -> None:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            if len(bufs[fd]) < max_bytes:
                bufs[fd].extend(chunk[: max_bytes - len(bufs[fd])])

    def feed() -> None:
        assert in_w is not None
        with contextlib.suppress(OSError):
            if input:
                view = memoryview(input)
                while view:
                    n = os.write(in_w, view[:65536])
                    view = view[n:]
        with contextlib.suppress(OSError):
            os.close(in_w)

    threads = [threading.Thread(target=drain, args=(fd,), daemon=True) for fd in (out_r, err_r)]
    if in_w is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()
    try:
        code = proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.sweep()
        code = -int(signal.SIGKILL)
    for t in threads:
        t.join(5)
    proc.close()
    for fd in (out_r, err_r):
        with contextlib.suppress(OSError):
            os.close(fd)
    return code, bytes(bufs[out_r]), bytes(bufs[err_r])


# ---- the brain's own directories ---------------------------------------------------------------

class BrainFilesError(Exception):
    pass


class BrainFiles:
    """Files in the brain user's home, reached as the brain user. Used when agentosd cannot chown
    (it is not root): everything there is created by the brain user and stays private to it."""

    def __init__(self, launcher: Launcher):
        self.launcher = launcher

    def _sh(self, script: str, *args: str, input: bytes | None = None, stdin_fd: int | None = None,
            timeout: float = 120) -> bytes:
        # paths are absolute, so none reads as an option (BSD chmod and dirname take no `--`)
        if any(a.startswith("-") for a in args):
            raise BrainFilesError(f"refusing an argument that reads as an option: {args}")
        code, out, err = run(self.launcher, ["/bin/sh", "-c", script, "agentos-brainfs", *args], input=input,
                             stdin_fd=stdin_fd, timeout=timeout)
        if code != 0:
            raise BrainFilesError(f"{script.split()[0]} as the brain user failed ({code}): "
                                  f"{err.decode(errors='replace').strip()[:300]}")
        return out

    def mkdirs(self, *paths: Path) -> None:
        self._sh('umask 077 && for d in "$@"; do mkdir -p -- "$d" && chmod 700 "$d" || exit 1; done',
                 *map(str, paths))

    def write(self, path: Path, content: str | bytes, mode: int) -> None:
        data = content.encode() if isinstance(content, str) else content
        # a temp file renamed over the name: a FIFO the brain planted there is replaced, not
        # written into (which would block until the timeout)
        self._sh('umask 077 && d="$(dirname "$1")" && mkdir -p -- "$d" && [ ! -d "$1" ] && '
                 't="$(mktemp "$d/.openajar-write.XXXXXX")" && { cat > "$t" && chmod "$2" "$t" && mv -f "$t" "$1"; } '
                 '|| { rm -f "$t"; exit 1; }', str(path), f"{mode:o}", input=data)

    def read(self, path: Path) -> bytes | None:
        code, out, _ = run(self.launcher, ["/bin/sh", "-c", '[ -f "$1" ] || exit 3; exec cat "$1"',
                                           "agentos-brainfs", str(path)])
        return out if code == 0 else None

    def remove(self, *paths: Path) -> None:
        self._sh('rm -rf -- "$@"', *map(str, paths))

    def unpack(self, src: Path, dest: Path) -> None:
        """Copy the tree at `src` (agentosd's own) into `dest` (the brain's), owned by the brain."""
        with tempfile.TemporaryFile(dir=src.parent) as tar_file:
            with tarfile.open(fileobj=tar_file, mode="w") as tar:
                for entry in sorted(src.iterdir()):
                    tar.add(entry, arcname=entry.name)
            tar_file.flush()
            tar_file.seek(0)
            self._sh('umask 077 && mkdir -p -- "$1" && exec tar -x --no-same-owner --no-same-permissions '
                     '-C "$1" -f -', str(dest), stdin_fd=tar_file.fileno(), timeout=600)


def describe(launcher: Launcher) -> str:
    if isinstance(launcher, HelperLauncher):
        return f"agentos-runas ({launcher.socket_path}, uid {launcher.expect_uid})"
    if isinstance(launcher, DirectLauncher) and launcher.run_as:
        return f"direct drop to uid {launcher.run_as['uid']}"
    return "direct (same user)"
