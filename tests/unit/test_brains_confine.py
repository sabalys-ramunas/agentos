"""AUD-08: the confinement check before a brain starts, and the runner through agentos-runas."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_runas import FakeSystem
from testkit.brains import add, make_service, wait_status

from openajar.brains import confine, launch
from openajar.brains import identity as identity_mod
from openajar.brains.confine import BrainUnconfined, Confinement, Targets, collect_targets, judge
from openajar.core.models import Principal
from openajar.core.runtime import Runtime
from openajar.runas import Identity, Server

P = Principal(id="claude-code", thread="T-1")
ME = os.getuid()
BRAIN_ID = launch.BrainIdentity("agentos-brain", 991, 991, "/var/lib/agentos-brain")
SELF_ID = launch.BrainIdentity(None, ME, os.getgid(), "")


def make_home(tmp_path: Path) -> Path:
    """A node's OPENAJAR_HOME with every kind of secret the probe looks for."""
    home = tmp_path / "openajar"
    for d in ("secrets", "vnc", "run", "brains", "lanes/personal/reddit/profile", "lanes/biz/x/profile"):
        (home / d).mkdir(parents=True)
    for f in ("node.toml", "secrets/vault.key", "secrets/proxies.json", "vault.db", "channel.db", "openajar.db",
              "brains/brains.db", "vnc/display-100.xauth", "vnc/display-100.sock", "run/ops.sock",
              "lanes/personal/reddit/profile/cookies.sqlite", "lanes/biz/x/profile/cookies.sqlite"):
        (home / f).write_text("secret")
    return home


# ---- what the probe looks at -------------------------------------------------------------------

def test_targets_cover_every_secret_on_the_node(tmp_path):
    home = make_home(tmp_path)
    t = collect_targets(home, display_base=100, slots=2)
    rel = lambda xs: {str(Path(x).relative_to(home)) if x != str(home) else "." for x in xs
                      if x == str(home) or home in Path(x).parents}
    assert {".", "secrets", "vnc", "run", "brains", "lanes", "lanes/personal/reddit/profile",
            "lanes/biz/x/profile"} <= rel(t.dirs)
    assert {"node.toml", "secrets/vault.key", "secrets/proxies.json", "vault.db", "channel.db", "openajar.db",
            "brains/brains.db", "vnc/display-100.xauth", "lanes/personal/reddit/profile/cookies.sqlite",
            "lanes/biz/x/profile/cookies.sqlite"} <= rel(t.files)
    assert rel(t.sockets) == {"vnc/display-100.sock", "run/ops.sock"}
    assert t.displays == [100, 101]


def _answer(targets: Targets, *, uid=991, gid=991, groups=(991,), reachable: set[str] = frozenset()) -> str:
    results = [{"kind": k, "target": p, "blocked": p not in reachable, "detail": "EACCES"}
               for k, items in (("traverse", [d for d, _ in targets.traverse]), ("dir", targets.dirs),
                                ("file", targets.files), ("socket", targets.sockets))
               for p in items]
    for n in targets.displays:
        results += [{"kind": "display", "target": f":{n} (abstract)", "blocked": True, "detail": "refused"},
                    {"kind": "display", "target": f":{n} (/tmp)", "blocked": True, "detail": "ENOENT"}]
    return "noise\n" + json.dumps({"uid": uid, "gid": gid, "groups": list(groups), "results": results})


def test_judge_passes_only_when_everything_is_blocked(tmp_path):
    t = collect_targets(make_home(tmp_path), display_base=100, slots=1)
    ok = judge(_answer(t), t, expect_uid=991, own_uid=990, own_gid=990)
    assert ok.ok and ok.checked == len(t.paths()) + 2
    bad = judge(_answer(t, reachable={t.files[0], t.sockets[0]}), t, expect_uid=991, own_uid=990, own_gid=990)
    assert not bad.ok and len(bad.reachable) == 2 and "can reach the node's secrets" in bad.reason
    assert t.files[0] in bad.reason


@pytest.mark.parametrize("kw, why", [
    ({"uid": 990}, "agentosd's own user"),
    ({"uid": 1234}, "not the brain user"),
    ({"groups": (991, 990)}, "agentosd's group"),
    ({"gid": 990}, "agentosd's group"),
])
def test_judge_refuses_the_wrong_identity(tmp_path, kw, why):
    t = collect_targets(make_home(tmp_path))
    r = judge(_answer(t, **kw), t, expect_uid=991, own_uid=990, own_gid=990)
    assert not r.ok and why in r.reason


def test_judge_fails_closed_on_a_bad_answer(tmp_path):
    t = collect_targets(make_home(tmp_path))
    for raw in ("", "not json", json.dumps({"uid": 991})):
        assert not judge(raw, t, expect_uid=991, own_uid=990, own_gid=990).ok
    short = json.loads(_answer(t).splitlines()[-1])
    short["results"] = short["results"][:-1]
    r = judge(json.dumps(short), t, expect_uid=991, own_uid=990, own_gid=990)
    assert not r.ok and "checked" in r.reason


def test_root_agentosd_without_a_brain_user_is_refused(tmp_path):
    t = collect_targets(make_home(tmp_path))
    r = judge(_answer(t, uid=0, gid=0, groups=(0,)), t, expect_uid=None, own_uid=0, own_gid=0)
    assert not r.ok and "agentosd's own user" in r.reason


# ---- the real probe, as the test's own user ------------------------------------------------------

def test_the_probe_sees_what_its_user_can_read(tmp_path):
    """Run as the owner of the files, everything is reachable; chmod 000 on one file blocks just it."""
    if ME == 0:
        pytest.skip("root reads everything")
    home = make_home(tmp_path)
    locked = home / "secrets" / "vault.key"
    locked.chmod(0)
    t = collect_targets(home)
    code, out, err = launch.run(launch.DirectLauncher(), confine.probe_argv(confine.probe_python(), t))
    assert code == 0, err
    r = judge(out.decode(), t, expect_uid=None, own_uid=-1, own_gid=-1, identity="the test")
    assert not r.ok
    reachable = " ".join(r.reachable)
    assert str(home / "node.toml") in reachable and str(locked) not in reachable
    assert "socket" not in reachable  # plain files named .sock refuse a connect
    locked.chmod(0o600)


def test_a_traversable_home_is_refused_even_when_every_named_target_is_closed(tmp_path):
    """The review's case: OPENAJAR_HOME 0711 (search but no read), every listed file closed, and
    logs/agentosd.log, which no list names, readable. Listing failed, so it all looked blocked."""
    if ME == 0:
        pytest.skip("root reads everything")
    home = make_home(tmp_path)
    (home / "logs").mkdir()
    (home / "logs" / "agentosd.log").write_text("token=...")
    t = collect_targets(home)  # as agentosd sees the home
    inside = lambda p: Path(p) == home or home in Path(p).parents
    t.dirs, t.files = [d for d in t.dirs if inside(d)], [f for f in t.files if inside(f)]
    t.files.remove(str(home / "logs" / "agentosd.log"))  # a log the lists did not name
    for f in t.files:
        Path(f).chmod(0)
    dirs = sorted((Path(d) for d in t.dirs), reverse=True)
    for d in dirs:
        d.chmod(0o311)
    try:
        assert t.traverse and t.traverse[0][0] == str(home)
        code, out, err = launch.run(launch.DirectLauncher(), confine.probe_argv(confine.probe_python(), t))
        assert code == 0, err
        r = judge(out.decode(), t, expect_uid=None, own_uid=-1, own_gid=-1, identity="the test")
        assert not r.ok and any(x.startswith(f"traverse {home} (can stat") for x in r.reachable), r.reason
        assert (home / "logs" / "agentosd.log").read_text() == "token=..."  # what it guards against
    finally:
        for d in dirs:
            d.chmod(0o700)


def test_secret_places_outside_the_home_are_checked(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    journal = tmp_path / "journal" / "abc123"
    journal.mkdir(parents=True)
    (journal / "system.journal").write_text("x")
    (journal / f"user-{os.geteuid()}.journal").write_text("x")
    (journal / "user-4242.journal").write_text("x")  # someone else's (the brain's own): not a target
    controller = tmp_path / "controller"
    controller.mkdir()
    (controller / "controller.toml").write_text("key")
    monkeypatch.setattr(confine, "JOURNAL_DIRS", (str(tmp_path / "journal"),))
    monkeypatch.setenv("OPENAJAR_CONTROLLER_HOME", str(controller))
    t = collect_targets(home)
    assert "/root/.cache/camoufox" in t.dirs and str(controller) in t.dirs
    assert [str(controller), str(controller / "controller.toml")] in t.traverse
    assert str(journal / "system.journal") in t.files and str(controller / "controller.toml") in t.files
    if os.geteuid() != 0:
        assert str(journal / f"user-{os.geteuid()}.journal") in t.files
    assert str(journal / "user-4242.journal") not in t.files


@pytest.mark.parametrize("kw, member_of", [({"groups": (991, 27)}, ()), ({}, (991, 27)), ({"gid": 27}, ())])
def test_a_brain_user_in_a_privileged_group_is_refused(tmp_path, kw, member_of):
    """As the probe ran (its gid and groups) or as the helper reads /etc/group (membership the
    drop clears now, but a login or newgrp would bring back)."""
    t = collect_targets(make_home(tmp_path))
    forbidden = {0: "root", 27: "sudo"}
    r = judge(_answer(t, **kw), t, expect_uid=991, own_uid=990, own_gid=990, forbidden=forbidden,
              member_of=member_of)
    assert not r.ok and "sudo (27)" in r.reason
    assert judge(_answer(t), t, expect_uid=991, own_uid=990, own_gid=990, forbidden=forbidden).ok


def test_groups_owning_the_home_are_forbidden(tmp_path):
    home = make_home(tmp_path)
    t = collect_targets(home)
    forbidden = confine.forbidden_gids(home, t, own_gid=990)
    assert os.lstat(home / "node.toml").st_gid in forbidden and 0 in forbidden and 990 in forbidden


# ---- the cache -----------------------------------------------------------------------------------

class Runner:
    def __init__(self, *, uid=991, reachable=frozenset(), fail: Exception | None = None):
        self.uid, self.reachable, self.fail = uid, set(reachable), fail
        self.calls = 0

    def __call__(self, launcher, argv, *, timeout):
        self.calls += 1
        if self.fail:
            raise self.fail
        t = Targets(**json.loads(argv[-1]))
        return 0, _answer(t, uid=self.uid, reachable=self.reachable).encode(), b""


def make_conf(home: Path, runner: Runner, boot: list[str]) -> Confinement:
    return Confinement(home, launcher=lambda: launch.DirectLauncher({"uid": 991, "gid": 991}),
                       identity=lambda: BRAIN_ID, python="/usr/bin/python3", runner=runner, boot=lambda: boot[0])


def test_a_pass_is_cached_until_the_layout_or_the_boot_changes(tmp_path):
    home = make_home(tmp_path)
    runner, boot = Runner(), ["boot-1"]
    conf = make_conf(home, runner, boot)
    first = conf.check()
    assert first.ok and not first.cached and runner.calls == 1
    assert conf.check().cached and runner.calls == 1
    assert make_conf(home, runner, boot).check().cached and runner.calls == 1  # from confinement.json
    (home / "lanes" / "personal" / "new" / "profile").mkdir(parents=True)  # a new lane
    assert not conf.check().cached and runner.calls == 2
    (home / "vnc" / "display-101.xauth").write_text("cookie")  # a new display
    assert not conf.check().cached and runner.calls == 3
    (home / "node.toml").chmod(0o600)  # a chmod
    assert not conf.check().cached and runner.calls == 4
    boot[0] = "boot-2"  # a reboot
    assert not conf.check().cached and runner.calls == 5
    assert not conf.check(force=True).cached and runner.calls == 6
    assert json.loads((home / "brains" / "confinement.json").read_text())["ok"] is True


def test_a_group_change_runs_the_probe_again(tmp_path):
    home = make_home(tmp_path)
    runner, ident = Runner(), [BRAIN_ID]
    conf = make_conf(home, runner, ["b"])
    conf.identity = lambda: ident[0]
    assert conf.check().ok and conf.check().cached and runner.calls == 1
    ident[0] = launch.BrainIdentity("agentos-brain", 991, 991, "/var/lib/agentos-brain", groups=(991, 5000))
    assert not conf.check().cached and runner.calls == 2


def test_failures_are_never_cached(tmp_path):
    home = make_home(tmp_path)
    runner, boot = Runner(reachable={str(home / "node.toml")}), ["b"]
    conf = make_conf(home, runner, boot)
    assert not conf.check().ok and not conf.check().ok and runner.calls == 2
    runner.reachable = set()
    assert conf.check().ok and runner.calls == 3


def test_a_probe_that_cannot_start_fails_closed(tmp_path):
    home = make_home(tmp_path)
    conf = make_conf(home, Runner(fail=OSError("no such helper")), ["b"])
    r = conf.check()
    assert not r.ok and "could not start" in r.reason
    conf = make_conf(home, lambda *a, **k: (1, b"", b"Traceback: boom"), ["b"])
    r = conf.check()
    assert not r.ok and "boom" in r.reason


# ---- the service refuses an unconfined brain ----------------------------------------------------

async def test_an_unconfined_brain_is_refused_at_submit_and_before_it_starts(tmp_path):
    svc = make_service(tmp_path, runtime=Runtime(), confinement="require")
    await add(svc, "claude-max", "subscription", "anthropic")
    # the real check: brains would run as agentosd's own user here (no brain user, no helper)
    with pytest.raises(BrainUnconfined, match="agentosd's own user") as e:
        await svc.submit(P, prompt="FAKE:ok", workspace="personal")
    assert e.value.code == "brain_unconfined"

    class PassOnce(Runner):  # passes at submit; the layout changes, and the check before the start fails
        def __call__(self, launcher, argv, *, timeout):
            if self.calls:
                self.reachable = {str(svc.home / "brains")}
            return super().__call__(launcher, argv, timeout=timeout)

    svc.brain_identity = lambda: SELF_ID  # from here the fake probe answers as another uid
    boots = iter(range(1000))
    svc._confinement = Confinement(svc.home, launcher=svc._launcher, identity=lambda: None, python="/usr/bin/python3",
                                   runner=PassOnce(uid=ME + 1), cache_dir=svc.dir, boot=lambda: str(next(boots)))
    r = await svc.submit(P, prompt="FAKE:ok", workspace="personal")
    st = await wait_status(svc, r["id"])
    assert st["status"] == "failed" and st["status_reason"].startswith("brain_unconfined:"), st


async def test_a_confined_brain_runs(tmp_path):
    svc = make_service(tmp_path, runtime=Runtime(), confinement="require")
    svc.brain_identity = lambda: SELF_ID  # the fake probe answers as another uid
    svc._confinement = Confinement(svc.home, launcher=svc._launcher, identity=lambda: None, python="/usr/bin/python3",
                                   runner=Runner(uid=ME + 1), cache_dir=svc.dir)
    await add(svc, "claude-max", "subscription", "anthropic", credential="sk-ant-oat01-" + "a" * 40)
    r = await svc.submit(P, prompt="FAKE:ok", workspace="personal")
    st = await wait_status(svc, r["id"])
    assert st["status"] == "succeeded_unverified", st


# ---- the whole runner through agentos-runas (as the test's own user) ------------------------------

@pytest.fixture
def helper_mode(tmp_path, monkeypatch):
    """agentosd 'unprivileged' with a helper; the brain user is the test user with its own home."""
    if ME == 0:
        pytest.skip("helper mode is for a non-root agentosd")
    d = Path(tempfile.mkdtemp(prefix="brh", dir="/tmp"))
    me = Identity("me", ME, os.getgid())
    brain = Identity("agentos-brain", ME + 1, 4242)  # FakeSystem runs it as the test user all the same
    server = Server(caller=me, brain=brain, socket_path=d / "r.sock", system=FakeSystem())
    server.bind()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    brain_home = tmp_path / "brainhome"
    brain_home.mkdir()
    real = identity_mod.pwd.getpwnam
    monkeypatch.setattr(identity_mod.pwd, "getpwnam", lambda n: SimpleNamespace(
        pw_name=n, pw_uid=ME + 1, pw_gid=4242, pw_dir=str(brain_home)) if n == "agentos-brain" else real(n))
    yield server, brain_home
    server.stop()
    shutil.rmtree(d, ignore_errors=True)


async def test_the_runner_goes_through_the_helper(tmp_path, helper_mode):
    server, brain_home = helper_mode
    svc = make_service(tmp_path, runtime=Runtime(), run_as="agentos-brain", runas_socket=str(server.socket_path),
                       isolation="auto")
    assert svc._via_helper() and isinstance(svc._launcher(), launch.HelperLauncher)
    await add(svc, "claude-max", "subscription", "anthropic", credential="sk-ant-oat01-" + "a" * 40)
    r = await svc.submit(P, prompt="FAKE:ok", workspace="personal", lanes=None)
    st = await wait_status(svc, r["id"])
    assert st["status"] == "succeeded_unverified", st
    work = brain_home / "tasks" / r["id"] / "work"
    assert (work / "fake-dump.json").exists() and (svc.task_dir(r["id"]) / "work").resolve() == work.resolve()
    users = [s["user"] for s in server.system.spawned]
    assert "me" in users  # the brain, the shim (caller) and the BrainFiles commands all went through it
    shim = next(s for s in server.system.spawned if "openajar.brains.shim" in " ".join(s["argv"]))
    assert shim["argv"][-1].endswith("/attempts/01")
    launch_json = json.loads((svc.attempt_dir(r["id"], 1) / "launch.json").read_text())
    assert launch_json["run_as"] is None and launch_json["launcher"]["kind"] == "helper"
    assert launch_json["launcher"]["brain_uid"] == ME + 1
    # this helper has no systemd: no scope and no limits asked for (it would refuse limits)
    assert launch_json["launcher"]["scope"] is None and launch_json["launcher"]["limits"] is None


def _blocked_probe(uid):
    """What a confined brain user would answer: everything blocked, as `uid`."""
    def runner(launcher, argv, *, timeout):
        return 0, _answer(Targets(**json.loads(argv[-1])), uid=uid).encode(), b""
    return runner


async def test_a_run_as_naming_no_account_never_starts_a_brain(tmp_path, helper_mode):
    """The probe ran through the helper (as the brain user) and passed, while the attempt fell back
    to agentosd's own user: one identity now answers for both, and a missing account refuses."""
    server, _ = helper_mode
    svc = make_service(tmp_path, runtime=Runtime(), run_as="no-such-brain-user", runas_socket=str(server.socket_path),
                       confinement="require")
    svc.confinement().runner = _blocked_probe(ME + 1)
    await add(svc, "claude-max", "subscription", "anthropic", credential="sk-ant-oat01-" + "a" * 40)
    with pytest.raises(BrainUnconfined, match="does not exist"):
        await svc.submit(P, prompt="FAKE:ok", workspace="personal", lanes=None)
    assert server.system.spawned == []


async def test_with_the_check_off_a_missing_brain_user_still_never_runs_as_agentosd(tmp_path, helper_mode):
    server, _ = helper_mode
    svc = make_service(tmp_path, runtime=Runtime(), run_as="no-such-brain-user", runas_socket=str(server.socket_path),
                       confinement="off")
    await add(svc, "claude-max", "subscription", "anthropic", credential="sk-ant-oat01-" + "a" * 40)
    r = await svc.submit(P, prompt="FAKE:ok", workspace="personal", lanes=None)
    st = await wait_status(svc, r["id"], until=("failed",))
    assert "no-such-brain-user" in st["status_reason"] and not st["attempts"], st  # refused while preparing
    assert server.system.spawned == []


async def test_a_helper_serving_another_brain_user_is_refused(tmp_path, helper_mode, monkeypatch):
    server, brain_home = helper_mode
    real = identity_mod.pwd.getpwnam
    monkeypatch.setattr(identity_mod.pwd, "getpwnam", lambda n: SimpleNamespace(
        pw_name=n, pw_uid=ME + 2, pw_gid=4243, pw_dir=str(brain_home)) if n == "other-brain" else real(n))
    svc = make_service(tmp_path, runtime=Runtime(), run_as="other-brain", runas_socket=str(server.socket_path),
                       confinement="require")
    svc.confinement().runner = _blocked_probe(ME + 1)
    with pytest.raises(BrainUnconfined, match="starts brains as agentos-brain"):
        svc.check_confinement()


def test_the_probe_must_run_as_the_identity_the_attempt_uses(tmp_path):
    home = make_home(tmp_path)
    conf = make_conf(home, Runner(uid=992), ["b"])  # the identity is uid 991
    r = conf.check()
    assert not r.ok and "not the brain user (uid 991)" in r.reason
    assert make_conf(home, Runner(uid=991), ["b"]).check().ok


# ---- env lanes -----------------------------------------------------------------------------------

class FakeEnv:
    """An env manager whose isolated lane has no OPENAJAR_HOME mounted (or, leaky, can read it)."""

    def __init__(self, *, isolated=True, leaky=False):
        self.isolated, self.leaky = isolated, leaky
        self.calls: list[tuple] = []

    def get(self, lane_id):
        return SimpleNamespace(id=lane_id, isolated=self.isolated)

    async def exec(self, lane_id, cmd, *, timeout_s=None, stdin=None):
        self.calls.append((lane_id, cmd, stdin))
        t = Targets(**json.loads(cmd[-1]))
        out = _answer(t, uid=0, gid=0, groups=(0,), reachable={t.files[0]} if self.leaky else set())
        return SimpleNamespace(stdout=out, exit_code=0)


async def test_env_lanes_are_checked_through_their_own_exec(tmp_path):
    home = make_home(tmp_path)
    env = FakeEnv()
    r = await confine.check_env_lane(env, "personal/term", home)
    assert r.ok and r.launcher == "env exec"
    lane, cmd, stdin = env.calls[0]
    assert lane == "personal/term" and cmd[:3] == ["python3", "-I", "-"] and stdin == confine.PROBE
    r = await confine.check_env_lane(FakeEnv(leaky=True), "personal/term", home)
    assert not r.ok and "env lane personal/term" in r.reason
    r = await confine.check_env_lane(FakeEnv(isolated=False), "personal/wb", home)
    assert not r.ok and "not isolated" in r.reason


def test_a_display_started_after_a_pass_runs_the_probe_again(tmp_path, monkeypatch):
    """Xvfb started (or restarted) without a new file in OPENAJAR_HOME still changes the key."""
    net = tmp_path / "unix"
    header = "Num       RefCount Protocol Flags    Type St Inode Path\n"
    net.write_text(header + "0: 00000002 00000000 00010000 0001 01 4242 /run/systemd/notify\n")
    real = confine.x_listeners
    monkeypatch.setattr(confine, "x_listeners", lambda: real(net))
    home = make_home(tmp_path)
    runner, boot = Runner(), ["b"]
    conf = make_conf(home, runner, boot)
    conf.check()
    assert conf.check().cached and runner.calls == 1
    net.write_text(header + "0: 00000002 00000000 00010000 0001 01 5151 @/tmp/.X11-unix/X101\n")
    assert not conf.check().cached and runner.calls == 2
    net.write_text(header + "0: 00000002 00000000 00010000 0001 01 6161 @/tmp/.X11-unix/X101\n")  # restarted
    assert not conf.check().cached and runner.calls == 3


def test_x_listeners_reads_listening_x_sockets_only(tmp_path):
    net = tmp_path / "unix"
    net.write_text("Num RefCount Protocol Flags Type St Inode Path\n"
                   "0: 2 0 10000 1 01 11 @/tmp/.X11-unix/X100\n"
                   "0: 2 0 10000 1 01 12 /tmp/.X11-unix/X100\n"
                   "0: 3 0 0 1 03 13 @/tmp/.X11-unix/X100\n"  # a connected client, not a listener
                   "0: 2 0 10000 1 01 14 /var/lib/agentos/vnc/display-100.sock\n")
    assert confine.x_listeners(net) == ["100:11:abstract", "100:12:path"]
    assert confine.x_listeners(tmp_path / "missing") == []
