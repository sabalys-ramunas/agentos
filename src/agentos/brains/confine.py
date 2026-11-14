"""AUD-08: a brain starts only when its own identity cannot reach the node's secrets.

Before a brain's first attempt (and again whenever the layout changes), agentosd starts a small
probe exactly the way it starts the brain: the same launcher (agentos.brains.launch), so the
same user, groups and mechanism (a root drop, or the agentos-runas helper), and the same brain
identity (BrainService.brain_identity: the probe and the attempt use one answer). The probe
tries, as that identity:

    to traverse AGENTOS_HOME itself (and a controller home kept outside it): it must have no
                search (x) permission there, and a stat of a file known to exist beneath it
                (node.toml) must fail. This is what makes everything under the home, named
                below or not (logs/, a file added later), unreachable by path.
    to list     AGENTOS_HOME, secrets/, lanes/, vnc/, run/, brains/, logs/ and every lane's
                profile; /root/.cache/camoufox and agentosd's own Camoufox cache
    to read     node.toml (the node token), secrets/vault.key and every other file in secrets/
                (proxies.json), vault.db, channel.db (the channel store), agentos.db,
                brains/brains.db, traces.db, policy.db, every file in logs/, every
                vnc/display-<n>.xauth cookie and every lane profile's cookies.sqlite; the
                systemd journal (system.journal and agentosd's user journal, under
                /var/log/journal and /run/log/journal)
    to connect  every vnc/*.sock (the live views) and run/*.sock (ops, vault)
    to open     every lane display (the X server's abstract and /tmp sockets) with no cookie

Each must fail. What this does not show: that no other file on the machine holds a secret (a
copy an operator made elsewhere, readable by everyone, is outside what the probe knows).

The probe also reports its uid and groups. It fails when its uid is not the brain user's the
attempt will use (or is agentosd's own), and when the brain user (as the probe runs, and as
the helper sees its membership) is in agentosd's group, a privileged group (root, adm, sudo,
wheel, docker, systemd-journal, disk, shadow, lxd, libvirt, kvm) or a group that owns
AGENTOS_HOME or anything the probe checks in it. A probe that does not answer fails closed.
Any failure refuses the brain with `brain_unconfined` and says what was reachable.

A pass is cached for the boot (keyed on /proc/sys/kernel/random/boot_id) and the layout: the
targets above with their mode, owner and inode, the X servers listening right now (from
/proc/net/unix, with their socket inodes), the launcher, the brain identity (user, uid, gid,
group membership, helper) and the forbidden groups. A new lane, a new display cookie, a
display started or restarted, a chmod, a group change or a reboot changes the key and the probe
runs again. Failures are never cached, so a fix takes effect on the next attempt.

`check_env_lane` runs the same probe inside an isolated terminal or service lane, as the lane's
own user, through the env manager's normal exec path.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentos import runas
from agentos.brains import launch
from agentos.core.errors import AgentosError

PROBE_TIMEOUT = 30.0
CACHE_NAME = "confinement.json"

# Stdlib only, no imports from agentos: it runs as the brain user (which may not read the
# install) and inside isolated env lanes (which have the node's /usr but not /opt/agentos).
PROBE = r"""
import errno, json, os, socket, struct, sys
t = json.loads(sys.argv[1])
out = {"uid": os.getuid(), "gid": os.getgid(), "groups": sorted(set(os.getgroups())), "results": []}
def note(kind, target, blocked, detail):
    out["results"].append({"kind": kind, "target": target, "blocked": blocked, "detail": detail})
def code(e):
    return errno.errorcode.get(e.errno, str(e.errno)) if isinstance(e, OSError) and e.errno else type(e).__name__
for d, known in t.get("traverse", []):
    search = os.access(d, os.X_OK)
    try:
        os.stat(known)
        note("traverse", d, False, "can stat " + known)
    except OSError as e:
        note("traverse", d, not search, "has search (x) permission" if search else code(e))
for p in t.get("dirs", []):
    try:
        os.listdir(p)
        note("dir", p, False, "listed")
    except OSError as e:
        note("dir", p, True, code(e))
for p in t.get("files", []):
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
        try:
            os.read(fd, 1)
        finally:
            os.close(fd)
        note("file", p, False, "read")
    except OSError as e:
        note("file", p, True, code(e))
for p in t.get("sockets", []):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(p)
        note("socket", p, False, "connected")
    except OSError as e:
        note("socket", p, True, code(e))
    finally:
        s.close()
setup = b"l\0" + struct.pack("<HHHH", 11, 0, 0, 0) + b"\0\0"
for n in t.get("displays", []):
    for addr in ("\0/tmp/.X11-unix/X%d" % n, "/tmp/.X11-unix/X%d" % n):
        name = ":%d (%s)" % (n, "abstract" if addr[0] == "\0" else addr)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        try:
            s.connect(addr)
            s.sendall(setup)
            reply = s.recv(1)
            if reply == b"\x01":
                note("display", name, False, "accepted without a cookie")
            else:
                note("display", name, True, "refused without a cookie")
        except OSError as e:
            note("display", name, True, code(e))
        finally:
            s.close()
print(json.dumps(out))
"""


class BrainUnconfined(AgentosError):
    """A brain's identity could reach the node's secrets, or the check could not show that it
    cannot, so the brain was not started (AUD-08)."""
    code = "brain_unconfined"


@dataclass
class Targets:
    traverse: list[list[str]] = field(default_factory=list)   # [dir, a file known to exist beneath it]
    dirs: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    sockets: list[str] = field(default_factory=list)
    displays: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"traverse": self.traverse, "dirs": self.dirs, "files": self.files, "sockets": self.sockets,
                "displays": self.displays}

    def paths(self) -> list[str]:
        return [*(d for d, _ in self.traverse), *self.dirs, *self.files, *self.sockets]

    def count(self) -> int:
        return len(self.traverse) + len(self.dirs) + len(self.files) + len(self.sockets) + 2 * len(self.displays)


SECRET_FILES = ("node.toml", "vault.db", "channel.db", "agentos.db", "brains/brains.db", "traces.db", "policy.db",
                "audit.db", "panel.db")
SECRET_DIRS = ("", "secrets", "lanes", "vnc", "run", "brains", "logs")
# outside AGENTOS_HOME: the browser root installs, and the journal (it holds agentosd's logs)
OUTSIDE_DIRS = ("/root/.cache/camoufox",)
JOURNAL_DIRS = ("/var/log/journal", "/run/log/journal")


def _known_child(d: Path) -> str | None:
    """A file under `d` that agentosd can see, for the traversal test (node.toml when there)."""
    for name in ("node.toml", "controller.toml"):
        if (d / name).is_file():
            return str(d / name)
    with contextlib.suppress(OSError):
        for p in sorted(d.iterdir()):
            return str(p)
    return None


def _outside(home: Path) -> Targets:
    """What a brain must not reach outside AGENTOS_HOME (each, wherever it exists)."""
    t = Targets()
    inside = lambda p: p == home or home in p.parents
    cache = Path(os.path.expanduser("~")) / ".cache" / "camoufox"
    for d in dict.fromkeys([*OUTSIDE_DIRS, str(cache)]):
        if not inside(Path(d)):
            t.dirs.append(d)
    controller = os.environ.get("AGENTOS_CONTROLLER_HOME")
    if controller and not inside(Path(controller)) and Path(controller).is_dir():
        known = _known_child(Path(controller))
        if known:
            t.traverse.append([controller, known])
        t.dirs.append(controller)
        t.files += [str(Path(controller) / n) for n in ("controller.toml", "controller.db")
                    if (Path(controller) / n).is_file()]
    me = os.geteuid()
    for base in JOURNAL_DIRS:
        with contextlib.suppress(OSError):
            for machine in sorted(Path(base).iterdir()):
                for name in ("system.journal", f"user-{me}.journal"):
                    if me == 0 and name.startswith("user-"):
                        continue
                    if (machine / name).is_file():
                        t.files.append(str(machine / name))
    return t


def _exists(p: Path) -> bool:
    try:
        p.lstat()
    except OSError:
        return False
    return True


def collect_targets(home: Path, *, display_base: int | None = None, slots: int = 0) -> Targets:
    """What a brain must not reach on this node, as agentosd sees it now (only what exists)."""
    home = Path(home)
    t = Targets()
    known = _known_child(home) if home.is_dir() else None
    if known:
        t.traverse.append([str(home), known])
    for rel in SECRET_DIRS:
        d = home / rel if rel else home
        if d.is_dir():
            t.dirs.append(str(d))
    for rel in SECRET_FILES:
        if (home / rel).is_file():
            t.files.append(str(home / rel))
    for sub in ("secrets", "logs"):
        if (home / sub).is_dir():
            with contextlib.suppress(OSError):
                t.files += [str(p) for p in sorted((home / sub).iterdir()) if p.is_file()]
    from agentos.ops.layout import lane_dirs

    for lane in lane_dirs(home):
        if lane.profile.is_dir():
            t.dirs.append(str(lane.profile))
            cookies = lane.profile / "cookies.sqlite"
            if cookies.is_file():
                t.files.append(str(cookies))
    displays: set[int] = set()
    vnc = home / "vnc"
    if vnc.is_dir():
        with contextlib.suppress(OSError):
            for p in sorted(vnc.iterdir()):
                if p.suffix == ".xauth":
                    t.files.append(str(p))
                elif p.suffix == ".sock":
                    t.sockets.append(str(p))
                stem = p.stem.removeprefix("display-")
                if p.stem.startswith("display-") and stem.isdigit():
                    displays.add(int(stem))
    run = home / "run"
    if run.is_dir():
        with contextlib.suppress(OSError):
            t.sockets += [str(p) for p in sorted(run.iterdir()) if p.suffix == ".sock"]
    if display_base is not None:
        displays.update(range(display_base, display_base + max(slots, 0)))
    t.displays = sorted(displays)
    out = _outside(home)
    t.traverse += out.traverse
    t.dirs += out.dirs
    t.files += out.files
    return t


def forbidden_gids(home: Path, targets: Targets, own_gid: int) -> dict[int, str]:
    """The groups a brain user may not be in: privileged ones, agentosd's, and every group that
    owns AGENTOS_HOME or a path the probe checks in it."""
    out = {**runas.privileged_gids()}
    if own_gid != 0:
        out[own_gid] = "agentosd's group"
    home = Path(home)
    for p in [str(home), *targets.paths()]:
        if p == str(home) or home in Path(p).parents:
            with contextlib.suppress(OSError):
                gid = os.lstat(p).st_gid
                out.setdefault(gid, f"owner of {p}")
    return out


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:  # macOS: the kernel's boot time stands in
        import subprocess

        with contextlib.suppress(OSError, subprocess.SubprocessError):
            return subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True,
                                  timeout=5, check=False).stdout.strip()
        return "unknown"


X_SOCKET_RE = re.compile(r"^@?/tmp/\.X11-unix/X(\d+)$")


def x_listeners(net_unix: Path = Path("/proc/net/unix")) -> list[str]:
    """The X servers listening now, as "<display>:<socket inode>" (abstract and path sockets).
    A display started without a cookie after a pass must not ride on the cached pass."""
    try:
        lines = net_unix.read_text().splitlines()[1:]
    except OSError:
        return []  # macOS: no /proc
    out = set()
    for line in lines:
        cols = line.split()
        listening = len(cols) >= 8 and all(c.isalnum() for c in cols[3]) and int(cols[3], 16) & 0x10000  # ACCEPTCON
        if listening and (m := X_SOCKET_RE.match(cols[7])):
            out.add(f"{m.group(1)}:{cols[6]}:{'abstract' if cols[7].startswith('@') else 'path'}")
    return sorted(out)


def layout_key(home: Path, targets: Targets, *, launcher: str, identity: list[Any] | None, boot: str,
               forbidden: dict[int, str] | None = None, listeners: list[str] | None = None) -> str:
    h = hashlib.sha256()
    h.update(json.dumps([boot, launcher, identity, os.geteuid(), os.getegid(), targets.displays,
                         sorted((forbidden or {}).items()),
                         x_listeners() if listeners is None else listeners]).encode())
    for p in [str(home), *targets.paths()]:
        try:
            st = os.lstat(p)
            h.update(f"{p}\0{st.st_mode}\0{st.st_uid}\0{st.st_gid}\0{st.st_ino}\n".encode())
        except OSError:
            h.update(f"{p}\0missing\n".encode())
    return h.hexdigest()


@dataclass
class Result:
    ok: bool
    reason: str
    reachable: list[str] = field(default_factory=list)
    uid: int | None = None
    checked: int = 0
    checked_at: float = 0.0
    cached: bool = False
    launcher: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "reachable": self.reachable, "uid": self.uid,
                "checked": self.checked, "checked_at": self.checked_at, "cached": self.cached,
                "launcher": self.launcher}


def judge(raw: str, targets: Targets, *, expect_uid: int | None, own_uid: int, own_gid: int,
          identity: str = "the brain", forbidden: dict[int, str] | None = None,
          member_of: tuple[int, ...] = ()) -> Result:
    """Read the probe's answer. Fails closed: no answer, a wrong uid, a forbidden group (as the
    probe ran, or in `member_of`, the brain user's membership), or anything reachable."""
    try:
        line = raw.strip().splitlines()[-1]
        out = json.loads(line)
        results = list(out["results"])
        uid = int(out["uid"])
        groups = {int(g) for g in out.get("groups", [])} | {int(out.get("gid", -1))}
    except (ValueError, KeyError, IndexError, TypeError):
        return Result(False, f"the confinement probe gave no answer as {identity}; refusing to start it")
    if uid == own_uid:
        return Result(False, f"{identity} would run as agentosd's own user (uid {uid}), which owns AGENTOS_HOME",
                      uid=uid)
    if expect_uid is not None and uid != expect_uid:
        return Result(False, f"the probe ran as uid {uid}, not the brain user (uid {expect_uid})", uid=uid)
    if own_gid in groups and own_gid != 0:
        return Result(False, f"{identity} (uid {uid}) is in agentosd's group (gid {own_gid})", uid=uid)
    problem = runas.group_problem(f"{identity} (uid {uid})", groups | set(member_of), forbidden or {})
    if problem:
        return Result(False, f"{problem}; a brain user must be in no privileged group", uid=uid)
    expected = targets.count()
    if len(results) != expected:
        return Result(False, f"the confinement probe checked {len(results)} of {expected} targets; refusing",
                      uid=uid)
    reachable = [f"{r['kind']} {r['target']} ({r['detail']})" for r in results if not r.get("blocked")]
    if reachable:
        shown = "; ".join(reachable[:6]) + (f"; and {len(reachable) - 6} more" if len(reachable) > 6 else "")
        return Result(False, f"{identity} (uid {uid}) can reach the node's secrets: {shown}", reachable, uid=uid,
                      checked=len(results))
    return Result(True, f"{identity} (uid {uid}) cannot reach any of {len(results)} secrets", uid=uid,
                  checked=len(results))


def probe_python() -> str:
    """A Python the brain user can run: the system one (agentosd's venv interpreter may live where
    the brain user cannot read, which is the point)."""
    for cand in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if os.access(cand, os.X_OK):
            return cand
    import sys

    return sys.executable


def probe_argv(python: str, targets: Targets) -> list[str]:
    return [python, "-I", "-c", PROBE, json.dumps(targets.to_dict())]


class Confinement:
    """The check for one node: run the probe through `launcher`, cache passes per boot and layout."""

    def __init__(self, home: Path, *, launcher: Callable[[], launch.Launcher],
                 identity: Callable[[], launch.BrainIdentity | None], python: str, config: Any = None,
                 cache_dir: Path | None = None,
                 boot: Callable[[], str] = boot_id, clock: Callable[[], float] = time.time,
                 runner: Callable[..., tuple[int, bytes, bytes]] = launch.run):
        self.home = Path(home)
        self.launcher = launcher
        self.identity = identity
        self.python = python
        self.config = config
        self.cache_path = (cache_dir or self.home / "brains") / CACHE_NAME
        self.boot = boot
        self.clock = clock
        self.runner = runner
        self._memo: tuple[str, Result] | None = None

    def _targets(self) -> Targets:
        cfg = self.config
        base = getattr(cfg, "display_base", None)
        slots = int(getattr(cfg, "max_slots", 0) or 0)
        return collect_targets(self.home, display_base=base if isinstance(base, int) else None, slots=slots)

    def _load(self, key: str) -> Result | None:
        if self._memo and self._memo[0] == key:
            return self._memo[1]
        try:
            data = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return None
        if data.get("key") != key or not data.get("ok"):
            return None
        r = Result(True, data.get("reason", ""), uid=data.get("uid"), checked=int(data.get("checked", 0)),
                   checked_at=float(data.get("checked_at", 0)), launcher=data.get("launcher", ""))
        self._memo = (key, r)
        return r

    def _store(self, key: str, r: Result) -> None:
        self._memo = (key, r)
        tmp = self.cache_path.with_name(f".{CACHE_NAME}.{os.getpid()}.tmp")
        with contextlib.suppress(OSError):
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump({"key": key, **r.to_dict()}, fh)
            os.replace(tmp, self.cache_path)

    def check(self, *, force: bool = False) -> Result:
        try:
            ident = self.identity()
            launcher = self.launcher()
        except AgentosError as e:  # no brain identity to check (BrainUnconfined): refused, never cached
            self._memo = None
            return Result(False, e.message, checked_at=self.clock())
        desc = launch.describe(launcher)
        targets = self._targets()
        # the uid the attempt will run as; None (agentosd's own user, or root) is refused by judge
        expect = ident.uid if ident is not None and ident.name else None
        forbidden = forbidden_gids(self.home, targets, os.getegid())
        key = layout_key(self.home, targets, launcher=desc, identity=ident.key() if ident else None,
                         boot=self.boot(), forbidden=forbidden)
        if not force:
            hit = self._load(key)
            if hit is not None:
                return Result(**{**hit.__dict__, "cached": True})
        try:
            code, out, err = self.runner(launcher, probe_argv(self.python, targets), timeout=PROBE_TIMEOUT)
        except Exception as e:  # noqa: BLE001 - a probe that cannot start fails closed
            r = Result(False, f"the confinement probe could not start ({type(e).__name__}: {e}); refusing")
        else:
            if code != 0 and not out.strip():
                tail = err.decode(errors="replace").strip()[-300:]
                r = Result(False, f"the confinement probe failed ({code}){f': {tail}' if tail else ''}; refusing")
            else:
                r = judge(out.decode(errors="replace"), targets, expect_uid=expect, own_uid=os.geteuid(),
                          own_gid=os.getegid(), forbidden=forbidden, member_of=ident.groups if ident else ())
        r.checked_at = self.clock()
        r.launcher = desc
        if r.ok:
            self._store(key, r)
        else:
            self._memo = None
        return r


async def check_env_lane(manager: Any, lane_id: str, home: Path, *, config: Any = None) -> Result:
    """Run the probe inside an isolated env lane, as the lane's own user, through its exec path.
    Inside a lane AGENTOS_HOME is not mounted at all, so every target should be absent."""
    lane = manager.get(lane_id)
    if not lane.isolated:
        return Result(False, f"{lane_id} is not isolated: it runs as agentosd's own user on the node's files")
    base = getattr(config, "display_base", None)
    targets = collect_targets(home, display_base=base if isinstance(base, int) else None,
                              slots=int(getattr(config, "max_slots", 0) or 0))
    res = await manager.exec(lane_id, ["python3", "-I", "-", json.dumps(targets.to_dict())], stdin=PROBE,
                             timeout_s=PROBE_TIMEOUT)
    # root inside the lane (reduced capabilities) is fine: what matters is that nothing is mounted
    r = judge(res.stdout, targets, expect_uid=None, own_uid=-1, own_gid=-1, identity=f"env lane {lane_id}")
    r.launcher = "env exec"
    r.checked_at = time.time()
    return r
