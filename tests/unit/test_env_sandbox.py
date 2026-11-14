import os
from pathlib import Path

import pytest

from agentos.core.errors import InvalidRequest
from agentos.core.models import LaneKind, Preset
from agentos.env import presets
from agentos.env.models import EnvLane
from agentos.env.paths import EnvPaths, EnvSettings
from agentos.env.sandbox import (
    BASE_CAPS,
    POINTS,
    BwrapLauncher,
    DirectLauncher,
    Workload,
    bwrap_args,
    check_home,
    plan_lane,
)

LINKS = (("usr/bin", "/bin"), ("usr/lib", "/lib"))


def mk(preset="sandbox", kind=LaneKind.TERMINAL, lane_id="biz-a/dev", **over):
    profile, limits, policy = presets.resolve(preset, kind=kind, overrides=over)
    return EnvLane(id=lane_id, kind=kind, preset=Preset(preset), profile=profile, limits=limits, policy=policy)


@pytest.fixture
def paths(tmp_path):
    return EnvPaths(tmp_path / "home")


def args_for(plan, **kw):
    wl = Workload("x-1", ["/bin/true"], {"PATH": "/usr/bin", "HOME": "/root"}, **kw)
    return bwrap_args(plan, wl, seccomp_fd=9, host_symlinks=LINKS, proc_dirs=())


def pairs(a, flag):
    return [(a[i + 1], a[i + 2]) for i, x in enumerate(a) if x == flag]


def test_sandbox_plan_uses_a_capped_tmpfs_and_overlays_every_point(paths):
    lane = mk()
    plan = plan_lane(lane, paths, EnvSettings())
    assert plan.disk.kind == "tmpfs" and plan.disk.size_mb == lane.limits.disk_mb
    assert [o.point.path for o in plan.overlays] == [p.path for p in POINTS]
    for ov in plan.overlays:
        assert ov.upper.is_relative_to(plan.disk.target) and ov.work.is_relative_to(plan.disk.target)
        assert ov.target.is_relative_to(paths.run)
    usr = next(o for o in plan.overlays if o.point.path == "usr")
    assert usr.lowers[-1] == Path("/usr")
    var = next(o for o in plan.overlays if o.point.path == "var")
    assert all(str(lw) != "/var" for lw in var.lowers)     # the node's /var is never a lower
    assert plan.netns == plan.run_dir / "netns"


def test_overlay_plan_with_cap_uses_an_image_and_without_cap_plain_dirs(paths):
    svc = mk("service", kind=LaneKind.SERVICE)
    plan = plan_lane(svc, paths, EnvSettings())
    assert plan.disk.kind == "image" and plan.disk.image == paths.upper_image(svc.id)
    nocap = mk("service", kind=LaneKind.SERVICE, disk_mb=None)
    nocap.limits.disk_mb = None
    plan2 = plan_lane(nocap, paths, EnvSettings())
    assert plan2.disk is None and plan2.upper_root == paths.upper(nocap.id)


def test_locked_plan_marks_writable_points_noexec(paths):
    plan = plan_lane(mk("locked"), paths, EnvSettings())
    by = {o.point.path: o for o in plan.overlays}
    assert by["root"].noexec and by["etc"].noexec and by["var"].noexec
    assert not by["usr"].noexec and not by["var/lib/dpkg"].noexec
    assert plan.disk.noexec and plan.scratch == plan.disk.target / "scratch"


def test_image_layers_and_mask_come_before_the_node(paths, tmp_path):
    layer = tmp_path / "img" / "layer"
    (layer / "usr").mkdir(parents=True)
    (paths.mask / "etc").mkdir(parents=True)
    plan = plan_lane(mk(), paths, EnvSettings(), image_layers=[layer], image_env={"A": "1"})
    usr = next(o for o in plan.overlays if o.point.path == "usr")
    etc = next(o for o in plan.overlays if o.point.path == "etc")
    opt = next(o for o in plan.overlays if o.point.path == "opt")
    assert usr.lowers[0] == layer / "usr"
    assert etc.lowers[0] == paths.mask / "etc"
    assert opt.lowers == [paths.empty]
    assert plan.env == {"A": "1"}


def test_bwrap_args_isolate_and_drop_privileges(paths):
    a = args_for(plan_lane(mk(), paths, EnvSettings()))
    for flag in ("--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try", "--die-with-parent",
                 "--new-session", "--clearenv"):
        assert flag in a
    assert "--unshare-net" not in a      # the lane netns is entered with nsenter instead
    assert a[a.index("--hostname") + 1] == "dev"
    caps = [a[i + 1] for i, x in enumerate(a) if x == "--cap-add"]
    assert set(caps) == set(BASE_CAPS) and a[a.index("--cap-drop") + 1] == "ALL"
    for bad in ("CAP_SYS_ADMIN", "CAP_NET_ADMIN", "CAP_MKNOD", "CAP_SYS_PTRACE", "CAP_DAC_READ_SEARCH"):
        assert bad not in caps
    assert a[a.index("--seccomp") + 1] == "9"
    assert ("/proc/sys", "/proc/sys") in pairs(a, "--ro-bind")
    ro = {d: s for s, d in pairs(a, "--ro-bind")}
    rw = {d: s for s, d in pairs(a, "--bind")}
    assert "/usr" in ro and "/var/lib/dpkg" in ro and "/opt" in ro
    assert "/etc" in rw and "/root" in rw and "/var" in rw
    assert "/" not in rw and "/home" not in rw
    assert a[a.index("--remount-ro") + 1] == "/"
    assert ("PATH", "/usr/bin") in pairs(a, "--setenv")


def test_install_workloads_get_writable_program_dirs_and_setfcap(paths):
    a = args_for(plan_lane(mk(), paths, EnvSettings()), install=True)
    rw = {d: s for s, d in pairs(a, "--bind")}
    assert "/usr" in rw and "/var/lib/dpkg" in rw
    assert "CAP_SETFCAP" in [a[i + 1] for i, x in enumerate(a) if x == "--cap-add"]


def test_pty_workloads_keep_their_session_and_locked_uses_noexec_scratch(paths):
    a = args_for(plan_lane(mk("locked"), paths, EnvSettings()), pty=True)
    assert "--new-session" not in a
    rw = {d: s for s, d in pairs(a, "--bind")}
    assert rw["/tmp"].endswith("scratch/tmp") and rw["/dev/shm"].endswith("scratch/shm")


def test_shared_filesystem_binds_root_and_volumes_and_extra_binds(paths):
    lane = mk("sandbox", filesystem="shared", volumes=["brandkit"])
    plan = plan_lane(lane, paths, EnvSettings())
    wl = Workload("x", ["true"], {}, binds=[("/srv/profile", "/profile", False)])
    a = bwrap_args(plan, wl, host_symlinks=LINKS, proc_dirs=())
    rw = pairs(a, "--bind")
    assert ("/", "/") in rw
    assert (str(paths.volume("brandkit")), "/volumes/brandkit") in rw
    assert ("/srv/profile", "/profile") in rw
    assert "--remount-ro" not in a


def test_home_under_a_visible_dir_is_refused(tmp_path):
    with pytest.raises(InvalidRequest, match="isolated lanes can see"):
        check_home(EnvPaths(Path("/usr/local/agentos")), EnvSettings())
    check_home(EnvPaths(tmp_path), EnvSettings())


def test_bwrap_launcher_wraps_cgroup_netns_and_payload(paths):
    lane = mk()
    plan = plan_lane(lane, paths, EnvSettings())
    cmd = BwrapLauncher(b"\x00" * 8).command(plan, lane, Workload("x-1", ["echo", "hi"], {}),
                                              cgroup="/sys/fs/cgroup/x", workdir=paths.home)
    try:
        assert cmd.argv[:2] == ["/bin/sh", "-c"] and cmd.argv[4] == "/sys/fs/cgroup/x"
        i = cmd.argv.index("nsenter")
        assert cmd.argv[i + 1] == f"--net={plan.netns}" and cmd.argv[i + 3] == "bwrap"
        assert cmd.argv[-3:] == ["--", "echo", "hi"]
        assert cmd.pass_fds and cmd.pass_fds == cmd.close_after_spawn
        assert cmd.env == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
    finally:
        for fd in cmd.close_after_spawn:
            os.close(fd)
    pty = BwrapLauncher(None).command(plan, lane, Workload("s", ["/bin/bash"], {}, pty=True), cgroup=None,
                                      workdir=paths.home)
    assert pty.takes_tty  # a shell whose sandbox dropped CAP_SYS_ADMIN could not take it from the spawn
    assert pty.argv[-5:] == ["--", "setsid", "-w", "-c", "/bin/bash"] and "--seccomp" not in pty.argv


def test_workbench_runs_directly_in_its_workdir(paths):
    lane = mk("workbench")
    cmd = BwrapLauncher(None).command(None, lane, Workload("x", ["ls"], {"A": "1"}), cgroup=None,
                                      workdir=paths.workdir(lane.id))
    assert cmd.argv == ["ls"] and cmd.cwd == str(paths.workdir(lane.id)) and cmd.env["A"] == "1"
    d = DirectLauncher().command(None, lane, Workload("x", ["ls"], {}), cgroup=None, workdir=paths.home)
    direct = DirectLauncher().command(None, lane, Workload("s", ["/bin/bash"], {}, pty=True), cgroup=None,
                                      workdir=paths.home)
    assert not direct.takes_tty  # no `setsid -c` here: the spawn takes the PTY for it
    assert d.argv == ["ls"]
