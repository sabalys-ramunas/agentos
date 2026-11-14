"""Displays and live views for warm lanes.

Slot N (0..max_slots-1) is one Xvfb `:display_base+N` and one x11vnc on the unix socket
`<vnc socket dir>/display-<display_base+N>.sock` (no TCP listener, no password). Both TCP
listeners are turned off: `-rfbport 0` stops only the IPv4 one, and x11vnc 0.9.16 still opens
`[::]:5900` (`-noipv6` does not stop it) until `-rfbportv6 -1` is given too.
The socket directory is mode 0700 and owned by agentosd's user, so the socket is the access
control: a TCP port on localhost, even with `-localhost`, could be opened by any local user,
the brain user included, and drive a logged-in browser. Xvfb itself also listens on an abstract
unix socket that any local process can open, so each display has its own Xauthority file
(`display-<n>.xauth` in that directory, a fresh MIT-MAGIC-COOKIE-1, mode 0600) that Xvfb is
started with (`-auth`). x11vnc and the browser get it through XAUTHORITY; it is removed with the
display. Humans reach x11vnc only through agentosd's signed-link relay and the channel's vnc
stream (agentos.node.vnc_relay); no websockify is started, since an unauthenticated websockify on
a localhost port is reachable by any web page in the owner's browser while a tunnel is open.
Websockify processes left in our viewer port range by an older version are still swept, and so
are x11vnc processes an older version started on TCP (they match by display).

x11vnc is ready when the socket path is listening AND the listener is the x11vnc process this
manager started (its fd table holds the listening inode). In the private directory nobody else
can bind the path anyway; the pid check makes "ready" mean "ours" even for root.

The Xvfb screen is sized from the lane's pin (acquire(size=...), from the engine's display_size):
the pinned screen, so the browser window can sit at its pinned position and a page's screen and
window values agree with the real ones; the window's own size when the screen cannot hold it there
or is larger than MAX_SIZE. The window size comes from the pin and is never changed to fit the
screen: sites read it. The framebuffer costs width x height x 4 bytes (8 MB at 1920x1080, 31 MB
for a 3456x2234 Retina screen, 133 MB at MAX_SIZE), counted in a warm lane's memory.

Every process runs in its own session so release() can terminate the whole process group.

Also home to the small /proc helpers the engine and the lane manager share.
"""

from __future__ import annotations

import asyncio
import errno
import itertools
import logging
import os
import secrets
import socket
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agentos.core.config import NodeConfig
from agentos.core.errors import EngineError
from agentos.core.models import ViewerInfo
from agentos.core.proc import iter_processes, kill_processes, pid_alive, process_group  # noqa: F401 (re-exported)

log = logging.getLogger("agentos.display")

X_SOCKET_DIR = Path("/tmp/.X11-unix")
X_AUTH_NAME = b"MIT-MAGIC-COOKIE-1"
FAMILY_LOCAL = 256  # an Xauthority family: a unix-domain connection, whose address is this host's name
X_LOCK_DIR = Path("/tmp")  # where Xvfb writes .X<n>-lock


# ---- process helpers: agentos.core.proc (re-exported here for older imports) ---------


def _listeners() -> dict[int, set[int]] | None:
    """{port: socket inodes} of every TCP socket in LISTEN state on this host, or None when
    /proc/net is unavailable."""
    found: dict[int, set[int]] = {}
    readable = False
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text().splitlines()[1:]
        except OSError:
            continue
        readable = True
        for line in lines:
            parts = line.split()
            if len(parts) > 9 and parts[3] == "0A":
                found.setdefault(int(parts[1].rsplit(":", 1)[1], 16), set()).add(int(parts[9]))
    return found if readable else None


def listening_ports() -> set[int] | None:
    """TCP ports in LISTEN state on this host, or None when /proc/net is unavailable."""
    found = _listeners()
    return None if found is None else set(found)


def tcp_ports_of(pid: int) -> set[int]:
    """TCP ports process `pid` listens on (empty without /proc). x11vnc's must be empty."""
    mine = socket_inodes(pid)
    ports: set[int] = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) > 9 and parts[3] == "0A" and int(parts[9]) in mine:
                ports.add(int(parts[1].rsplit(":", 1)[1], 16))
    return ports


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                return False
            raise
    return True


def unix_listeners(path: Path) -> set[int] | None:
    """Inodes of the unix sockets listening on `path`, or None when /proc/net/unix is unavailable."""
    try:
        lines = Path("/proc/net/unix").read_text().splitlines()[1:]
    except OSError:
        return None
    inodes: set[int] = set()
    for line in lines:
        # Num RefCount Protocol Flags Type St Inode Path; Flags 00010000 is __SO_ACCEPTCON (listening)
        parts = line.split(None, 7)
        if len(parts) == 8 and parts[7] == str(path) and int(parts[3], 16) & 0x10000:
            inodes.add(int(parts[6]))
    return inodes


def socket_inodes(pid: int) -> set[int]:
    """Inodes of the sockets process `pid` holds open (empty when its fds cannot be read)."""
    inodes: set[int] = set()
    try:
        fds = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return inodes
    for fd in fds:
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(int(target[8:-1]))
    return inodes


def _socket_listening(path: Path, pid: int | None = None) -> bool:
    """Something listens on the unix socket `path`; with `pid`, that something is process `pid`.
    Without /proc (macOS dev boxes) a connect is the only check there is."""
    listeners = unix_listeners(path)
    if listeners is None:
        if not path.is_socket():
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(str(path)) == 0
    if not listeners:
        return False
    return pid is None or bool(listeners & socket_inodes(pid))


def _write_xauthority(path: Path, num: int) -> None:
    """Write the one Xauthority entry for display :num, with a fresh 16-byte cookie, mode 0600. The
    bytes are libXau's format (big-endian shorts and counted strings): the entry that
    `xauth add :num . <hex>` writes, so no xauth binary is needed on the node."""
    def counted(data: bytes) -> bytes:
        return len(data).to_bytes(2, "big") + data

    entry = (FAMILY_LOCAL.to_bytes(2, "big") + counted(socket.gethostname().encode())
             + counted(str(num).encode()) + counted(X_AUTH_NAME) + counted(secrets.token_bytes(16)))
    path.unlink(missing_ok=True)  # O_EXCL: a file a crash left keeps neither its old mode nor its cookie
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(entry)


def read_xauthority(path: str | Path) -> tuple[bytes, bytes] | None:
    """(auth name, cookie) of the first entry in the Xauthority file `path` (one written by
    _write_xauthority holds one entry), or None when it cannot be read. For X clients inside this
    process (engine_camoufox.place_window), which cannot take XAUTHORITY from the environment."""
    try:
        data = Path(path).read_bytes()
        fields, at = [], 2  # family, then address, number, name, data: each a counted string
        for _ in range(4):
            size = int.from_bytes(data[at:at + 2], "big")
            fields.append(data[at + 2:at + 2 + size])
            at += 2 + size
    except OSError:
        return None
    name, cookie = fields[2], fields[3]
    return (name, cookie) if name and cookie else None


# ---- displays ---------------------------------------------------------------------

@dataclass
class _Slot:
    index: int
    viewer: ViewerInfo
    procs: list[asyncio.subprocess.Process] = field(default_factory=list)

    def alive(self) -> bool:
        """Every process of the slot still runs (Xvfb and x11vnc)."""
        return bool(self.procs) and all(p.returncode is None and pid_alive(p.pid) for p in self.procs)


MAX_SOCKET_PATH = 103  # sun_path holds 108 bytes on Linux and 104 on macOS, NUL included
DEFAULT_SIZE = (1920, 1080)
MIN_SIZE = (320, 240)
MAX_SIZE = (7680, 4320)
DEPTH = 24


def parse_geometry(geometry: str) -> tuple[int, int]:
    """"1920x1080x24" or "1920x1080" -> (1920, 1080)."""
    width, height = geometry.split("x")[:2]
    return int(width), int(height)


def fit_size(size: tuple[int, int] | None, default: tuple[int, int] = DEFAULT_SIZE) -> tuple[int, int]:
    """A screen size Xvfb can serve: the default when size is missing or out of range."""
    if not size:
        return default
    width, height = size
    if not (MIN_SIZE[0] <= width <= MAX_SIZE[0] and MIN_SIZE[1] <= height <= MAX_SIZE[1]):
        log.warning("window size %sx%s is out of range; using %sx%s", width, height, *default)
        return default
    return int(width), int(height)


class DisplayManagerImpl:
    def __init__(
        self,
        config: NodeConfig,
        *,
        xvfb: str = "Xvfb",
        x11vnc: str = "x11vnc",
        geometry: str = "1920x1080x24",
        start_timeout: float = 15.0,
        log_dir: Path | None = None,
        socket_dir: Path | None = None,
        sweep_own_only: bool = False,
    ):
        self.config = config
        # Sweep only the processes that name this manager's socket directory (their -auth and
        # -unixsock): a scratch home (the stealth check) must never kill another home's display
        # in its range, while agentosd also sweeps what an older version left without them.
        self.sweep_own_only = sweep_own_only
        self.xvfb, self.x11vnc = xvfb, x11vnc
        self.default_size = parse_geometry(geometry)
        self.start_timeout = start_timeout
        self.log_dir = log_dir or config.home / "logs"
        self.socket_dir = socket_dir or config.home / "vnc"
        self._slots: dict[str, _Slot] = {}

    # -- slot numbers --
    def _display_num(self, index: int) -> int:
        return self.config.display_base + index

    def _vnc_socket(self, index: int) -> Path:
        return self.socket_dir / f"display-{self._display_num(index)}.sock"

    def _xauth_file(self, index: int) -> Path:
        return self.socket_dir / f"display-{self._display_num(index)}.xauth"

    def _private_socket_dir(self) -> Path:
        """The socket directory, created mode 0700 and owned by this process's user. One that
        another user owns is refused: whoever owns it could open every lane's view."""
        self.socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.socket_dir.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise EngineError(f"{self.socket_dir} is not a directory owned by this user; live views stay off")
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.chmod(self.socket_dir, 0o700)
        return self.socket_dir

    def _viewer_port(self, index: int) -> int:
        return self.config.viewer_base + index

    # -- protocol --
    def get(self, lane_id: str) -> ViewerInfo | None:
        slot = self._slots.get(lane_id)
        return slot.viewer if slot else None

    def healthy(self, lane_id: str) -> bool:
        """The lane's display and VNC server are both still running."""
        slot = self._slots.get(lane_id)
        return slot is not None and slot.alive()

    async def acquire(self, lane_id: str, *, size: tuple[int, int] | None = None) -> ViewerInfo:
        """A display for the lane, `size` (width, height) pixels (default 1920x1080). A display
        the lane already holds is returned as it is when it still runs; one whose Xvfb or x11vnc
        died is replaced (a dead view must never be handed out as live)."""
        width, height = fit_size(size, self.default_size)
        slot = self._slots.get(lane_id)
        if slot is not None:
            if slot.alive():
                return slot.viewer
            log.warning("display %s of %s stopped running; starting it again", slot.viewer.display, lane_id)
            await self.release(lane_id)
        taken = {s.index for s in self._slots.values()}
        for index in range(self.config.max_slots):
            if index in taken or not self._slot_free(index):
                continue
            slot = _Slot(index, ViewerInfo(display=f":{self._display_num(index)}",
                                           vnc_socket=str(self._vnc_socket(index)),
                                           xauthority=str(self._xauth_file(index)), width=width, height=height))
            self._slots[lane_id] = slot  # reserve before the first await
            try:
                await self._start(slot)
            except BaseException:
                self._slots.pop(lane_id, None)
                await self._stop(slot)
                raise
            log.info("display %s (%dx%d) ready for %s (VNC %s)", slot.viewer.display, width, height,
                     lane_id, slot.viewer.vnc_socket)
            return slot.viewer
        raise EngineError(f"no free display slot (all {self.config.max_slots} are in use)")

    async def release(self, lane_id: str) -> None:
        slot = self._slots.get(lane_id)
        if slot is None:
            return
        try:
            await self._stop(slot)
        finally:
            self._slots.pop(lane_id, None)

    async def release_all(self) -> None:
        await asyncio.gather(*(self.release(lane_id) for lane_id in list(self._slots)), return_exceptions=True)

    async def sweep(self) -> list[int]:
        """Kill display processes left in our slot range by a previous run and clear stale
        X lock files. Only matches our own display numbers and ports (and, with sweep_own_only,
        only processes that name our socket directory)."""
        ours = {s.index for s in self._slots.values()}
        mine = str(self.socket_dir).rstrip("/") + "/"
        stale: list[int] = []
        for pid, argv in iter_processes():
            index = self._match_slot_process(argv)
            if index is None or index in ours or pid == os.getpid():
                continue
            if self.sweep_own_only and not any(a.startswith(mine) for a in argv[1:]):
                continue
            stale.append(pid)
        if stale:
            log.info("stopping %d display processes left by a previous run", len(stale))
            await kill_processes(stale)
        for index in range(self.config.max_slots):
            if index not in ours:
                self._clear_stale_lock(self._display_num(index))
        return stale

    # -- internals --
    def _match_slot_process(self, argv: list[str]) -> int | None:
        name = os.path.basename(argv[0])
        displays = {f":{self._display_num(i)}": i for i in range(self.config.max_slots)}
        if name == "Xvfb" and len(argv) > 1:
            return displays.get(argv[1])
        if name == "x11vnc":
            for flag, value in itertools.pairwise(argv):
                if flag == "-display" and value in displays:
                    return displays[value]
            return None
        if any("websockify" in os.path.basename(a) for a in argv[:2]):
            ports = {f"127.0.0.1:{self._viewer_port(i)}": i for i in range(self.config.max_slots)}
            for a in argv:
                if a in ports:
                    return ports[a]
        return None

    def _clear_stale_lock(self, num: int) -> bool:
        """Remove /tmp/.X<n>-lock (and the socket) when no live process owns it.
        Returns True when display n is free."""
        lock = X_LOCK_DIR / f".X{num}-lock"
        sock = X_SOCKET_DIR / f"X{num}"
        if lock.exists():
            try:
                pid = int(lock.read_text().strip())
            except (OSError, ValueError):
                pid = 0
            if pid and pid_alive(pid):
                return False
            log.info("removing stale X lock for :%d", num)
            lock.unlink(missing_ok=True)
            sock.unlink(missing_ok=True)
        elif sock.exists():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                try:
                    s.connect(str(sock))
                    return False  # a server without a lock file is listening
                except OSError:
                    sock.unlink(missing_ok=True)
        return True

    def _slot_free(self, index: int) -> bool:
        self._private_socket_dir()  # another user's directory is refused before anything in it is touched
        num = self._display_num(index)
        if not self._clear_stale_lock(num):
            log.warning("display :%d is in use by another process; skipping slot %d", num, index)
            return False
        sock = self._vnc_socket(index)
        if sock.exists() or sock.is_symlink():
            if _socket_listening(sock):
                log.warning("%s is in use by another process; skipping slot %d", sock, index)
                return False
            sock.unlink(missing_ok=True)  # left by an x11vnc that died; it would refuse to bind
        return True

    async def _spawn(self, slot: _Slot, name: str, argv: list[str], *,
                     env: dict[str, str] | None = None) -> asyncio.subprocess.Process:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"display-{self._display_num(slot.index)}-{name}.log"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=fd, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True, env=env)
        except FileNotFoundError:
            raise EngineError(f"{argv[0]} is not installed on this node") from None
        finally:
            os.close(fd)
        slot.procs.append(proc)
        return proc

    async def _wait_ready(self, proc: asyncio.subprocess.Process, what: str, ready: Callable[[], bool]) -> None:
        deadline = time.monotonic() + self.start_timeout
        while not ready():
            if proc.returncode is not None:
                raise EngineError(f"{what} exited during startup (code {proc.returncode}); see {self.log_dir}")
            if time.monotonic() > deadline:
                raise EngineError(f"{what} did not become ready within {self.start_timeout:.0f}s")
            await asyncio.sleep(0.1)

    async def _start(self, slot: _Slot) -> None:
        num = self._display_num(slot.index)
        sock = self._private_socket_dir() / self._vnc_socket(slot.index).name
        if len(os.fsencode(sock)) > MAX_SOCKET_PATH:
            raise EngineError(f"{sock} is longer than a unix socket path may be; use a shorter AGENTOS_HOME")
        auth = self._xauth_file(slot.index)
        _write_xauthority(auth, num)
        geometry = f"{slot.viewer.width}x{slot.viewer.height}x{DEPTH}"
        xvfb = await self._spawn(slot, "xvfb", [
            self.xvfb, f":{num}", "-screen", "0", geometry, "-auth", str(auth), "-nolisten", "tcp"])
        await self._wait_ready(xvfb, f"Xvfb :{num}", lambda: (X_SOCKET_DIR / f"X{num}").exists())
        x11vnc = await self._spawn(slot, "x11vnc", [
            self.x11vnc, "-display", f":{num}", "-auth", str(auth), "-rfbport", "0", "-rfbportv6", "-1",
            # No -localhost: with -unixsock it makes x11vnc refuse every client on the socket
            # ("localhost does not match 127.0.0.1"); TCP is already off, and the 0700 dir is the gate.
            "-unixsock", str(sock), "-noipv6", "-nopw", "-forever", "-shared", "-norc", "-noremote",
            "-nocmds"], env={**os.environ, "XAUTHORITY": str(auth)})
        await self._wait_ready(x11vnc, f"x11vnc on {sock}", lambda: _socket_listening(sock, x11vnc.pid))

    async def _stop(self, slot: _Slot) -> None:
        procs = [p for p in reversed(slot.procs) if p.returncode is None]
        await kill_processes([p.pid for p in procs])
        for p in procs:
            try:
                await asyncio.wait_for(p.wait(), 2.0)
            except TimeoutError:
                log.warning("process %d did not exit after SIGKILL", p.pid)
        slot.procs.clear()
        self._clear_stale_lock(self._display_num(slot.index))
        try:
            self._private_socket_dir()
        except EngineError:  # not ours any more: nothing in that directory is ours to remove
            return
        self._vnc_socket(slot.index).unlink(missing_ok=True)
        self._xauth_file(slot.index).unlink(missing_ok=True)
