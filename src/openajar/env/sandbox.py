"""Isolated lanes: the mount plan and the bubblewrap command line.

The lane's root is built from an allowlist, not by hiding things from the node's root:

    /usr                 overlay: base image layers + the node's /usr        (read-only for programs)
    /etc                 overlay: base image layers + mask layer + node /etc (shadow sanitized,
                         private keys and credential dirs hidden)
    /opt                 overlay: base image layers only
    /var                 overlay: base image layers only
    /var/lib/dpkg        overlay: base image layers + the node's dpkg state  (so apt sees what /usr has)
    /var/lib/apt, /var/cache/debconf   overlays over the node's apt state
    /root                overlay: the lane's home
    /proc /dev /tmp /run fresh, with /proc/sys read-only

Every overlay has its upper in the lane's writable layer (a capped ext4 image, a tmpfs for
ephemeral lanes, or a plain directory when no cap is set). Nothing else of the node is
mounted, so /root, /home, OPENAJAR_HOME (secrets, other lanes' uppers) and the rest of
/var are simply absent. The overlays are mounted once per warm lane on the host, under
OPENAJAR_HOME/run/env (0700), and every exec, shell and service of the lane binds them.

`bwrap_args()` is pure so it can be tested anywhere; launchers turn a workload into the
final argv (cgroup entry, network namespace, bubblewrap) or, on a Mac, run it directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from openajar.core.errors import InvalidRequest
from openajar.env.models import EnvLane
from openajar.env.paths import EnvPaths, EnvSettings


@dataclass(frozen=True)
class Point:
    path: str             # inside the lane, without the leading slash
    host: str | None      # the node directory used as the bottom lower, if any
    programs: bool        # read-only for normal workloads (only installs write here)

    @property
    def flat(self) -> str:
        return self.path.replace("/", "-")


POINTS: tuple[Point, ...] = (
    Point("usr", "/usr", True),
    Point("etc", "/etc", False),
    Point("opt", None, True),
    Point("var", None, False),
    Point("var/lib/dpkg", "/var/lib/dpkg", True),
    Point("var/lib/apt", "/var/lib/apt", False),
    Point("var/cache/debconf", "/var/cache/debconf", False),
    Point("root", None, False),
)

ROOT_SYMLINKS = (("usr/bin", "/bin"), ("usr/sbin", "/sbin"), ("usr/lib", "/lib"), ("usr/lib64", "/lib64"),
                 ("usr/lib32", "/lib32"), ("usr/libx32", "/libx32"))

# what a reduced-capability root keeps inside an isolated lane (no SYS_ADMIN, NET_ADMIN,
# NET_RAW, MKNOD, SYS_PTRACE, SYS_MODULE, DAC_READ_SEARCH ...)
BASE_CAPS = ("CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_FSETID", "CAP_KILL", "CAP_SETGID",
             "CAP_SETUID", "CAP_NET_BIND_SERVICE", "CAP_AUDIT_WRITE")
INSTALL_CAPS = (*BASE_CAPS, "CAP_SETFCAP")

PROC_MASK_FILES = ("/proc/kcore", "/proc/keys", "/proc/timer_list", "/proc/sched_debug", "/proc/latency_stats",
                   "/proc/kallsyms", "/proc/kmsg")
PROC_MASK_DIRS = ("/proc/acpi", "/proc/scsi", "/proc/fs")

ETC_SANITIZE = ("shadow", "gshadow", "shadow-", "gshadow-")
ETC_WHITEOUT_GLOBS = ("ssh/ssh_host_*_key", "security/opasswd")
ETC_OPAQUE = ("wireguard", "ssl/private", "letsencrypt", "NetworkManager/system-connections", "netplan",
              "headscale", "netbird", "tailscale", "openajar", "sudoers.d", "cloud/cloud.cfg.d")

DEFAULT_PATH = "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass
class OverlayMount:
    point: Point
    lowers: list[Path]    # top first
    upper: Path
    work: Path
    target: Path
    noexec: bool = False


@dataclass
class DiskMount:
    kind: str             # "image" | "tmpfs"
    target: Path
    size_mb: int
    image: Path | None = None
    noexec: bool = False


@dataclass
class LanePlan:
    lane: EnvLane
    run_dir: Path
    disk: DiskMount | None
    upper_root: Path
    work_root: Path
    overlays: list[OverlayMount]
    netns: Path | None
    volumes: list[tuple[str, Path]]
    env: dict[str, str] = field(default_factory=dict)   # image env

    @property
    def scratch(self) -> Path | None:
        """noexec scratch area (locked lanes) for /tmp, /run and /dev/shm."""
        if self.disk is not None and self.lane.policy.noexec_writable:
            return self.disk.target / "scratch"
        return None


@dataclass
class Workload:
    wid: str
    argv: list[str]
    env: dict[str, str]
    cwd: str | None = None
    pty: bool = False
    install: bool = False
    binds: list[tuple[str, str, bool]] = field(default_factory=list)  # (host, lane path, read-only)
    oom_record: bool = False  # on a VM: write the start record the node reads oom kills by (openajar.env.oom)
    # a job on a VM (openajar.env.jobs): run it detached from the ssh and follow its output files from these
    # (stdout, stderr) offsets; detach_start starts it first, with a watchdog past detach_timeout_s
    detach: tuple[int, int] | None = None
    detach_start: bool = False
    detach_timeout_s: float | None = None


@dataclass
class Command:
    argv: list[str]
    env: dict[str, str]
    cwd: str | None
    pass_fds: tuple[int, ...] = ()
    close_after_spawn: tuple[int, ...] = ()
    takes_tty: bool = False  # the payload makes the PTY its controlling terminal itself (setsid -c)


def check_home(paths: EnvPaths, settings: EnvSettings) -> None:
    """OPENAJAR_HOME must not live under a directory isolated lanes see from the node."""
    home = paths.home.resolve()
    for host in [p.host for p in POINTS if p.host] + list(settings.extra_host_lowers):
        try:
            home.relative_to(Path(host))
        except ValueError:
            continue
        raise InvalidRequest(f"OPENAJAR_HOME ({home}) is under {host}, which isolated lanes can see; move it")


def plan_lane(lane: EnvLane, paths: EnvPaths, settings: EnvSettings, *,
              image_layers: list[Path] | None = None, image_env: dict[str, str] | None = None,
              upper_root: Path | None = None, work_root: Path | None = None) -> LanePlan:
    """The mounts an isolated lane needs while warm. Pure: nothing is touched.

    `upper_root`/`work_root` replace the lane's writable layer (image builds use this)."""
    run_dir = paths.run_dir(lane.key)
    prof, lim, pol = lane.profile, lane.limits, lane.policy
    netns = run_dir / "netns" if prof.network in ("off", "isolated") else None
    volumes = [(v, paths.volume(v)) for v in prof.volumes]

    disk: DiskMount | None = None
    overlays: list[OverlayMount] = []
    if prof.filesystem == "shared":
        upper_root = work_root = paths.lane_dir(lane.id)
        return LanePlan(lane, run_dir, None, upper_root, work_root, [], netns, volumes, dict(image_env or {}))

    if upper_root is not None:
        work_root = work_root or upper_root.with_name(upper_root.name + "-work")
    elif prof.filesystem == "ephemeral":
        disk = DiskMount("tmpfs", run_dir / "disk", lim.disk_mb or 512, noexec=pol.noexec_writable)
        upper_root, work_root = disk.target / "upper", disk.target / "work"
    elif lim.disk_mb:
        disk = DiskMount("image", run_dir / "disk", lim.disk_mb, image=paths.upper_image(lane.id),
                         noexec=pol.noexec_writable)
        upper_root, work_root = disk.target / "upper", disk.target / "work"
    else:
        upper_root, work_root = paths.upper(lane.id), paths.ovl_work(lane.id)

    layers = list(image_layers or [])
    extra = [Point(h.strip("/"), h, True) for h in settings.extra_host_lowers]
    for point in (*POINTS, *extra):
        lowers: list[Path] = []
        for layer in layers:
            d = layer / point.flat
            if d.is_dir():
                lowers.append(d)
        mask = paths.mask / point.flat
        if mask.is_dir():
            lowers.append(mask)
        if point.host and Path(point.host).is_dir():
            lowers.append(Path(point.host))
        if not lowers:
            lowers.append(paths.empty)
        overlays.append(OverlayMount(
            point=point, lowers=lowers, upper=upper_root / point.flat, work=work_root / point.flat,
            target=run_dir / "mnt" / point.flat,
            noexec=pol.noexec_writable and not point.programs,
        ))
    return LanePlan(lane, run_dir, disk, upper_root, work_root, overlays, netns, volumes, dict(image_env or {}))


def bwrap_args(plan: LanePlan, wl: Workload, *, seccomp_fd: int | None = None,
               host_symlinks: tuple[tuple[str, str], ...] | None = None,
               proc_dirs: tuple[str, ...] | None = None, proc_files: tuple[str, ...] | None = None) -> list[str]:
    """The bubblewrap command line for one workload of an isolated lane (without the payload)."""
    lane = plan.lane
    prof, lim = lane.profile, lane.limits
    a: list[str] = ["bwrap", "--die-with-parent"]
    if not wl.pty:
        a.append("--new-session")
    if prof.processes == "isolated":
        a += ["--unshare-pid", "--unshare-ipc", "--unshare-uts", "--hostname", lane.name]
    a.append("--unshare-cgroup-try")
    a += ["--cap-drop", "ALL"]
    for cap in (INSTALL_CAPS if wl.install else BASE_CAPS):
        a += ["--cap-add", cap]
    if seccomp_fd is not None:
        a += ["--seccomp", str(seccomp_fd)]
    a.append("--clearenv")
    for k, v in sorted(wl.env.items()):
        a += ["--setenv", k, v]

    if prof.filesystem == "shared":
        a += ["--bind", "/", "/"]
    else:
        for ov in plan.overlays:
            ro = ov.point.programs and not wl.install
            a += ["--ro-bind" if ro else "--bind", str(ov.target), "/" + ov.point.path]
        links = host_symlinks if host_symlinks is not None else existing_root_symlinks()
        for target, link in links:
            a += ["--symlink", target, link]

    a += ["--proc", "/proc", "--ro-bind", "/proc/sys", "/proc/sys"]
    for f in (proc_files if proc_files is not None else tuple(x for x in PROC_MASK_FILES if os.path.exists(x))):
        a += ["--ro-bind", "/dev/null", f]
    for d in (proc_dirs if proc_dirs is not None else tuple(x for x in PROC_MASK_DIRS if os.path.isdir(x))):
        a += ["--tmpfs", d]
    a += ["--dev", "/dev"]

    scratch = plan.scratch
    if scratch is not None and not wl.install:
        a += ["--bind", str(scratch / "tmp"), "/tmp", "--bind", str(scratch / "run"), "/run",
              "--bind", str(scratch / "shm"), "/dev/shm"]
    elif prof.filesystem != "shared":
        a += ["--size", str(lim.tmp_mb * 1024 * 1024), "--tmpfs", "/tmp", "--tmpfs", "/run"]
    if prof.network == "shared" and prof.filesystem != "shared" and os.path.isdir("/run/systemd/resolve"):
        a += ["--ro-bind", "/run/systemd/resolve", "/run/systemd/resolve"]

    for name, host in plan.volumes:
        a += ["--bind", str(host), f"/volumes/{name}"]
    for host, dest, ro in wl.binds:
        a += ["--ro-bind" if ro else "--bind", host, dest]
    if prof.filesystem != "shared":
        a += ["--remount-ro", "/"]
    a += ["--chdir", wl.cwd or "/root"]
    return a


def existing_root_symlinks() -> tuple[tuple[str, str], ...]:
    """The merged-/usr symlinks the node actually has (/bin -> usr/bin, ...)."""
    out = [(t, link) for t, link in ROOT_SYMLINKS
           if os.path.islink(link) and os.readlink(link).lstrip("/") == t]
    return tuple(out) or ROOT_SYMLINKS[:4]


def cgroup_wrap(cgroup: str) -> list[str]:
    """Enter a cgroup before exec, without preexec_fn: the shell writes its own pid, then execs."""
    return ["/bin/sh", "-c", 'echo $$ > "$1/cgroup.procs" && shift && exec "$@"', "openajar-cg", cgroup]


class BwrapLauncher:
    """Linux: cgroup entry, then the lane's network namespace, then bubblewrap."""

    def __init__(self, seccomp_bytes: bytes | None):
        self.seccomp_bytes = seccomp_bytes

    def command(self, plan: LanePlan | None, lane: EnvLane, wl: Workload, *, cgroup: str | None,
                workdir: Path) -> Command:
        argv: list[str] = cgroup_wrap(cgroup) if cgroup else []
        if not lane.isolated:
            payload = ["setsid", "-w", "-c", *wl.argv] if wl.pty else wl.argv
            env = dict(wl.env)
            return Command(argv + payload, env, wl.cwd or str(workdir), takes_tty=wl.pty)
        assert plan is not None
        if plan.netns is not None:
            argv += ["nsenter", f"--net={plan.netns}", "--"]
        fds: tuple[int, ...] = ()
        sc_fd = None
        if lane.policy.seccomp and self.seccomp_bytes:
            r, w = os.pipe()
            os.write(w, self.seccomp_bytes)
            os.close(w)
            sc_fd, fds = r, (r,)
        argv += bwrap_args(plan, wl, seccomp_fd=sc_fd)
        payload = ["setsid", "-w", "-c", *wl.argv] if wl.pty else wl.argv
        argv += ["--", *payload]
        return Command(argv, {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}, None, fds, fds, takes_tty=wl.pty)


class DirectLauncher:
    """No sandbox: runs the payload on this machine (env0 lanes, and unit tests on a Mac).

    `root` maps the lane's /root to a host directory so tests can run real commands."""

    def __init__(self, root_map: dict[str, Path] | None = None):
        self.root_map = root_map or {}

    def command(self, plan: LanePlan | None, lane: EnvLane, wl: Workload, *, cgroup: str | None,
                workdir: Path) -> Command:
        cwd = wl.cwd
        if lane.isolated:
            root = self.root_map.get(lane.id, workdir)
            root.mkdir(parents=True, exist_ok=True)
            cwd = str(root) if not cwd or cwd == "/root" else cwd
        env = {**os.environ, **wl.env} if not lane.isolated else dict(wl.env)
        env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
        if lane.isolated:
            env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
            env["HOME"] = cwd or str(workdir)
        argv = (cgroup_wrap(cgroup) if cgroup else []) + wl.argv
        return Command(argv, env, cwd or str(workdir))
