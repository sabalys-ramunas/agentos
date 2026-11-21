"""Privileged Linux mechanisms behind one small interface: mounts, cgroups, network namespaces.

`LinuxHost` does the real thing (root on a Linux node). `NullHost` is what a Mac or a
non-root user gets: no mounts, no cgroups, so only env0 (workbench) lanes can run. Unit
tests use `FakeHost`, which records what would have happened.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import shutil
import signal
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Protocol

from openajar.core.errors import OpenajarError
from openajar.env.models import Limits
from openajar.env.paths import EnvPaths, EnvSettings
from openajar.env.sandbox import ETC_OPAQUE, ETC_SANITIZE, ETC_WHITEOUT_GLOBS


class HostError(OpenajarError):
    code = "host_error"


class Host(Protocol):
    can_isolate: bool

    def report(self) -> dict: ...
    def mount_overlay(self, lowers: list[Path], upper: Path, work: Path, target: Path, *,
                      noexec: bool = False) -> None: ...
    def mount_tmpfs(self, target: Path, size_mb: int, *, noexec: bool = False) -> None: ...
    def make_disk_image(self, image: Path, size_mb: int) -> None: ...
    def mount_disk_image(self, image: Path, target: Path, *, noexec: bool = False) -> None: ...
    def unmount(self, target: Path) -> None: ...
    def mounts_under(self, prefix: Path) -> list[str]: ...
    def build_mask(self, dest: Path, extra_opaque: list[str]) -> None: ...
    def disk_usage(self, path: Path) -> tuple[int, int] | None: ...
    def netns_create(self, path: Path) -> None: ...
    def netns_delete(self, path: Path) -> None: ...
    def socket_in_netns(self, path: Path | None, family: int = socket.AF_INET,
                        type: int = socket.SOCK_STREAM) -> socket.socket: ...
    def cgroup_lane(self, key: str, limits: Limits) -> str | None: ...
    def cgroup_workload(self, key: str, wid: str) -> str | None: ...
    def cgroup_signal(self, path: str, sig: int) -> None: ...
    def cgroup_kill(self, path: str) -> None: ...
    def cgroup_remove(self, path: str) -> None: ...
    def cgroup_stat(self, key: str) -> dict: ...
    def cgroup_sweep(self) -> int: ...
    def cgroup_prune(self) -> None: ...


def _run(argv: list[str], *, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if check and p.returncode != 0:
        raise HostError(f"{argv[0]} failed ({p.returncode}): {(p.stderr or p.stdout).strip()[:500]}")
    return p


def _unescape_mount(path: str) -> str:
    return path.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


class LinuxHost:
    can_isolate = True

    def __init__(self, paths: EnvPaths, settings: EnvSettings):
        self.paths = paths
        self.settings = settings
        self.cg_root = Path(settings.cgroup_root)
        self.cg_inst = self.cg_root / paths.instance
        self._host_netns = os.open("/proc/self/ns/net", os.O_RDONLY)
        self._ns_lock = threading.Lock()

    # ---------------------------------------------------------------- report
    def report(self) -> dict:
        bwrap = shutil.which("bwrap")
        version = _run([bwrap, "--version"], check=False).stdout.strip() if bwrap else None
        return {
            "root": os.geteuid() == 0,
            "bwrap": version,
            "cgroup2": Path("/sys/fs/cgroup/cgroup.controllers").exists(),
            "overlay": "overlay" in Path("/proc/filesystems").read_text() or _modprobe_overlay(),
            "nsenter": bool(shutil.which("nsenter")),
            "mkfs.ext4": bool(shutil.which("mkfs.ext4")),
        }

    # ---------------------------------------------------------------- mounts
    def mount_overlay(self, lowers, upper, work, target, *, noexec=False):
        for d in (upper, work, target):
            d.mkdir(parents=True, exist_ok=True)
        # a crash can leave overlay's private work/work dir behind; it must be empty to mount
        for child in work.iterdir():
            shutil.rmtree(child, ignore_errors=True)
        for p in [*lowers, upper, work]:
            if any(c in str(p) for c in ",:"):
                raise HostError(f"path not usable in an overlay mount: {p}")
        opts = (f"lowerdir={':'.join(str(p) for p in lowers)},upperdir={upper},workdir={work},"
                "index=off,metacopy=off,redirect_dir=off,nodev,nosuid")
        if noexec:
            opts += ",noexec"
        _run(["mount", "-t", "overlay", "overlay", "-o", opts, str(target)])

    def mount_tmpfs(self, target, size_mb, *, noexec=False):
        target.mkdir(parents=True, exist_ok=True)
        opts = f"size={int(size_mb)}m,mode=0700,nodev,nosuid" + (",noexec" if noexec else "")
        _run(["mount", "-t", "tmpfs", "-o", opts, "agentos-env", str(target)])

    def make_disk_image(self, image, size_mb):
        image.parent.mkdir(parents=True, exist_ok=True)
        with open(image, "wb") as f:
            f.truncate(int(size_mb) * 1024 * 1024)
        image.chmod(0o600)
        _run(["mkfs.ext4", "-q", "-F", "-m", "0", "-L", "openajar-upper", str(image)], timeout=120)

    def mount_disk_image(self, image, target, *, noexec=False):
        target.mkdir(parents=True, exist_ok=True)
        p = _run(["e2fsck", "-p", str(image)], check=False, timeout=300)
        if p.returncode >= 4:
            raise HostError(f"lane disk {image.name} is damaged (e2fsck {p.returncode}); remove or repair it")
        opts = "loop,nodev,nosuid" + (",noexec" if noexec else "")
        _run(["mount", "-o", opts, str(image), str(target)])

    def unmount(self, target):
        p = _run(["umount", str(target)], check=False)
        if p.returncode != 0 and self._is_mounted(target):
            _run(["umount", "-l", str(target)], check=False)

    def _is_mounted(self, target: Path) -> bool:
        t = str(target)
        return any(m == t for m in self._mountpoints())

    def _mountpoints(self) -> list[str]:
        out = []
        with open("/proc/self/mountinfo") as f:
            for line in f:
                out.append(_unescape_mount(line.split()[4]))
        return out

    def mounts_under(self, prefix):
        pre = str(prefix).rstrip("/") + "/"
        found = [m for m in self._mountpoints() if m.startswith(pre)]
        return sorted(set(found), key=lambda m: m.count("/"), reverse=True)

    def build_mask(self, dest, extra_opaque):
        """Mask layer for /etc: sanitized shadow files, whiteouts for private keys, opaque dirs."""
        tmp = dest.with_name(dest.name + ".new")
        shutil.rmtree(tmp, ignore_errors=True)
        etc = tmp / "etc"
        etc.mkdir(parents=True)
        for name in ETC_SANITIZE:
            src = Path("/etc") / name
            if src.is_file():
                lines = []
                for line in src.read_text(errors="replace").splitlines():
                    parts = line.split(":")
                    if len(parts) > 1:
                        parts[1] = "*"
                    lines.append(":".join(parts))
                (etc / name).write_text("\n".join(lines) + "\n")
                os.chmod(etc / name, 0o640)
        for pattern in ETC_WHITEOUT_GLOBS:
            parent = Path("/etc") / os.path.dirname(pattern)
            if not parent.is_dir():
                continue
            for entry in os.listdir(parent):
                if fnmatch.fnmatch(entry, os.path.basename(pattern)):
                    rel = Path(os.path.dirname(pattern)) / entry
                    (etc / rel).parent.mkdir(parents=True, exist_ok=True)
                    os.mknod(etc / rel, stat.S_IFCHR | 0o000, os.makedev(0, 0))
        for rel in [*ETC_OPAQUE, *extra_opaque]:
            rel = rel.strip("/").removeprefix("etc/")
            if not (Path("/etc") / rel).exists():
                continue
            d = etc / rel
            d.mkdir(parents=True, exist_ok=True)
            os.setxattr(d, "trusted.overlay.opaque", b"y")
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)

    def disk_usage(self, path):
        try:
            st = os.statvfs(path)
        except OSError:
            return None
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return total - free, total

    # ---------------------------------------------------------------- netns
    def netns_create(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and self._is_mounted(path):
            return
        path.touch()
        _run(["unshare", f"--net={path}", "true"])
        _run(["nsenter", f"--net={path}", "--", "ip", "link", "set", "lo", "up"])

    def netns_delete(self, path):
        if path.exists():
            if self._is_mounted(path):
                self.unmount(path)
            try:
                path.unlink()
            except OSError:
                pass

    def socket_in_netns(self, path, family=socket.AF_INET, type=socket.SOCK_STREAM):
        """A socket that lives in the lane's network namespace (created by a short-lived thread)."""
        if path is None:
            return socket.socket(family, type)
        out: dict = {}

        def worker():
            fd = os.open(path, os.O_RDONLY)
            try:
                os.setns(fd, os.CLONE_NEWNET)
                try:
                    out["sock"] = socket.socket(family, type)
                finally:
                    os.setns(self._host_netns, os.CLONE_NEWNET)
            except BaseException as e:  # noqa: BLE001
                out["err"] = e
            finally:
                os.close(fd)

        with self._ns_lock:
            t = threading.Thread(target=worker, name="openajar-netns")
            t.start()
            t.join()
        if "err" in out:
            raise HostError(f"cannot open a socket in {path}: {out['err']}")
        return out["sock"]

    # ---------------------------------------------------------------- cgroups
    def _ensure_root(self) -> None:
        if (self.cg_inst / "cgroup.subtree_control").exists():
            return
        want = {"cpu", "memory", "pids"}
        top = Path("/sys/fs/cgroup")
        have = set((top / "cgroup.subtree_control").read_text().split())
        missing = want - have
        if missing:
            (top / "cgroup.subtree_control").write_text(" ".join(f"+{c}" for c in sorted(missing)))
        for d in (self.cg_root, self.cg_inst):
            d.mkdir(exist_ok=True)
            (d / "cgroup.subtree_control").write_text("+cpu +memory +pids")

    def cgroup_lane(self, key, limits):
        self._ensure_root()
        d = self.cg_inst / key
        d.mkdir(exist_ok=True)
        (d / "memory.max").write_text(f"{limits.memory_mb}M" if limits.memory_mb else "max")
        if (d / "memory.swap.max").exists():
            (d / "memory.swap.max").write_text("0" if limits.memory_mb else "max")
        (d / "pids.max").write_text(str(limits.pids) if limits.pids else "max")
        if limits.cpus:
            (d / "cpu.max").write_text(f"{int(limits.cpus * 100000)} 100000")
        else:
            (d / "cpu.max").write_text("max 100000")
        try:
            (d / "cgroup.subtree_control").write_text("+memory +pids")
        except OSError:
            pass
        return str(d)

    def cgroup_workload(self, key, wid):
        d = self.cg_inst / key / wid
        d.mkdir(exist_ok=True)
        return str(d)

    def _procs(self, path: str) -> list[int]:
        try:
            return [int(x) for x in Path(path, "cgroup.procs").read_text().split()]
        except (OSError, ValueError):
            return []

    def cgroup_signal(self, path, sig):
        for pid in self._procs(path):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        for child in Path(path).iterdir() if Path(path).is_dir() else []:
            if child.is_dir():
                self.cgroup_signal(str(child), sig)

    def cgroup_kill(self, path):
        p = Path(path)
        if not p.is_dir():
            return
        kill = p / "cgroup.kill"
        if kill.exists():
            try:
                kill.write_text("1")
            except OSError:
                self.cgroup_signal(path, signal.SIGKILL)
        else:
            self.cgroup_signal(path, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "populated 0" in (p / "cgroup.events").read_text():
                return
            time.sleep(0.02)

    def cgroup_remove(self, path):
        p = Path(path)
        if not p.is_dir():
            return
        for child in [c for c in p.iterdir() if c.is_dir()]:
            self.cgroup_remove(str(child))
        self.cgroup_kill(path)
        for _ in range(50):
            try:
                p.rmdir()
                return
            except OSError as e:
                if e.errno == errno.ENOENT:
                    return
                time.sleep(0.05)

    def cgroup_stat(self, key):
        d = self.cg_inst / key
        out: dict = {}
        try:
            out["memory_mb"] = round(int((d / "memory.current").read_text()) / 1048576, 1)
            out["pids"] = int((d / "pids.current").read_text())
            events = dict(line.split() for line in (d / "memory.events").read_text().splitlines())
            out["oom_kills"] = int(events.get("oom_kill", 0))
        except (OSError, ValueError):
            pass
        return out

    def cgroup_prune(self):
        """Remove this home's (now empty) cgroup directory, and the shared root if nobody uses it."""
        for d in (self.cg_inst, self.cg_root):
            try:
                d.rmdir()
            except OSError:
                return

    def cgroup_sweep(self):
        """Kill and remove every cgroup this OPENAJAR_HOME left behind (crash recovery)."""
        n = 0
        if not self.cg_inst.is_dir():
            return 0
        for lane in [c for c in self.cg_inst.iterdir() if c.is_dir()]:
            n += sum(1 for _ in lane.iterdir() if _.is_dir())
            self.cgroup_remove(str(lane))
        return n


def _modprobe_overlay() -> bool:
    return subprocess.run(["modprobe", "overlay"], capture_output=True, check=False).returncode == 0


class NullHost:
    """No privileged mechanisms (a Mac, or not root): only env0 lanes can run."""
    can_isolate = False

    def __init__(self, reason: str = "isolated lanes need a Linux node with bubblewrap, running as root"):
        self.reason = reason

    def report(self):
        return {"isolation": False, "reason": self.reason}

    def _no(self, *a, **k):
        raise HostError(self.reason)

    mount_overlay = mount_tmpfs = make_disk_image = mount_disk_image = build_mask = _no
    netns_create = _no

    def unmount(self, target):
        pass

    def mounts_under(self, prefix):
        return []

    def disk_usage(self, path):
        try:
            st = os.statvfs(path)
            return (st.f_blocks - st.f_bavail) * st.f_frsize, st.f_blocks * st.f_frsize
        except OSError:
            return None

    def netns_delete(self, path):
        pass

    def socket_in_netns(self, path, family=socket.AF_INET, type=socket.SOCK_STREAM):
        if path is not None:
            raise HostError(self.reason)
        return socket.socket(family, type)

    def cgroup_lane(self, key, limits):
        return None

    def cgroup_workload(self, key, wid):
        return None

    def cgroup_signal(self, path, sig):
        pass

    def cgroup_kill(self, path):
        pass

    def cgroup_remove(self, path):
        pass

    def cgroup_stat(self, key):
        return {}

    def cgroup_sweep(self):
        return 0

    def cgroup_prune(self):
        pass


class FakeHost(NullHost):
    """Records calls; pretends isolation works so the manager's logic can be tested on a Mac."""
    can_isolate = True

    def __init__(self):
        super().__init__("fake")
        self.calls: list[tuple] = []
        self.mounted: set[str] = set()
        self.netns: set[str] = set()
        self.cgroups: set[str] = set()

    def report(self):
        return {"fake": True}

    def mount_overlay(self, lowers, upper, work, target, *, noexec=False):
        for d in (upper, work):
            d.mkdir(parents=True, exist_ok=True)
        # writes through the fake "merged" dir land in the upper, like a real overlay
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() and not target.is_symlink():
            target.symlink_to(upper, target_is_directory=True)
        self.calls.append(("overlay", [str(x) for x in lowers], str(upper), str(target), noexec))
        self.mounted.add(str(target))

    def mount_tmpfs(self, target, size_mb, *, noexec=False):
        target.mkdir(parents=True, exist_ok=True)
        self.calls.append(("tmpfs", str(target), size_mb, noexec))
        self.mounted.add(str(target))

    def make_disk_image(self, image, size_mb):
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"")
        self.calls.append(("mkimage", str(image), size_mb))

    def mount_disk_image(self, image, target, *, noexec=False):
        target.mkdir(parents=True, exist_ok=True)
        self.calls.append(("image", str(image), str(target), noexec))
        self.mounted.add(str(target))

    def unmount(self, target):
        self.calls.append(("umount", str(target)))
        self.mounted.discard(str(target))
        if Path(target).is_symlink():
            Path(target).unlink()

    def mounts_under(self, prefix):
        pre = str(prefix).rstrip("/") + "/"
        return sorted((m for m in self.mounted if m.startswith(pre)), key=lambda m: m.count("/"), reverse=True)

    def build_mask(self, dest, extra_opaque):
        (dest / "etc").mkdir(parents=True, exist_ok=True)
        self.calls.append(("mask", str(dest)))

    def netns_create(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        self.netns.add(str(path))
        self.calls.append(("netns", str(path)))

    def netns_delete(self, path):
        self.netns.discard(str(path))
        self.calls.append(("netns-del", str(path)))

    def socket_in_netns(self, path, family=socket.AF_INET, type=socket.SOCK_STREAM):
        return socket.socket(family, type)

    def cgroup_lane(self, key, limits):
        self.calls.append(("cgroup", key, limits.model_dump()))

    def cgroup_remove(self, path):
        self.calls.append(("cgroup-rm", path))

    def cgroup_sweep(self):
        self.calls.append(("cgroup-sweep",))
        return 0
