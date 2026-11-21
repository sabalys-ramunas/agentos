"""agentos-runas on a real Linux node, as root: the real System (prlimit, unshare, setpriv), the
brain's own PID namespace, the sweeps, the scope, and the confinement probe through the helper.

Needs root and util-linux; the scope test needs systemd as PID 1. Two throwaway users are made
(and removed): a caller standing in for `openajar` and a brain user.
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from openajar import runas
from openajar.brains import confine, launch
from openajar.runas import Identity, RunasError, Server

pytestmark = [pytest.mark.node,
              pytest.mark.skipif(not sys.platform.startswith("linux") or os.geteuid() != 0
                                 or not runas.has_namespace_tools(), reason="needs root on Linux with util-linux")]

CALLER, BRAIN = "openajar-rt", "agentos-brain-rt"
SYSTEMD = Path("/run/systemd/system").is_dir()


def _user(name: str) -> Identity:
    if subprocess.run(["id", "-u", name], capture_output=True, check=False).returncode != 0:
        subprocess.run(["useradd", "--system", "--user-group", "--create-home", "--home-dir", f"/var/lib/{name}",
                        "--shell", "/usr/sbin/nologin", name], check=True)
    pw = pwd.getpwnam(name)
    return Identity(pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir)


@pytest.fixture(scope="module")
def users():
    caller, brain = _user(CALLER), _user(BRAIN)
    yield caller, brain
    for name in (BRAIN, CALLER):
        subprocess.run(["pkill", "-KILL", "-u", name], check=False)
        subprocess.run(["userdel", "-r", name], capture_output=True, check=False)


@pytest.fixture
def helper(users):
    """A Server with the real System; the peer check is the caller's uid (the test is root)."""
    caller, brain = users
    d = Path(tempfile.mkdtemp(prefix="rn", dir="/run"))
    server = Server(caller=caller, brain=brain, socket_path=d / "runas.sock", peer=lambda c: caller.uid)
    server.bind()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.stop()
    shutil.rmtree(d, ignore_errors=True)
    subprocess.run(["pkill", "-KILL", "-u", BRAIN], check=False)


def _brain_procs() -> list[str]:
    out = subprocess.run(["pgrep", "-u", BRAIN, "-a"], capture_output=True, text=True, check=False).stdout
    return [line for line in out.splitlines() if line.strip()]


def _sleeps() -> int:
    out = subprocess.run(["pgrep", "-x", "-u", BRAIN, "sleep"], capture_output=True, text=True, check=False).stdout
    return len(out.split())


def _until(cond, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def _start(server, argv, *, env=None, **kw):
    r, w = os.pipe()
    devnull = os.open(os.devnull, os.O_RDONLY)
    try:
        proc = runas.request(server.socket_path, {"identity": "brain", "argv": argv, "env": env or {"PATH": "/usr/bin:/bin"},
                                                  "cwd": "/", **kw}, (devnull, w, w))
    finally:
        os.close(w)
        os.close(devnull)
    return proc, r


def _read(fd: int) -> str:
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    os.close(fd)
    return b"".join(chunks).decode()


def test_a_brain_runs_as_the_brain_user_alone_in_its_namespace(helper, users):
    _, brain = users
    proc, out = _start(helper, ["/bin/sh", "-c", "id -u; id -G; ls /proc | grep -c '^[0-9]'; ulimit -c"],
                       rlimits={"core": 0})
    assert proc.wait(10) == 0 and proc.uid == brain.uid
    uid, groups, nprocs, core = _read(out).split("\n")[:4]
    assert int(uid) == brain.uid and groups.split() == [str(brain.gid)]
    assert int(nprocs) <= 4 and core == "0"  # bash (the init), sh, ls, grep: nothing of the host


def test_a_brain_cannot_read_another_brains_environment(helper):
    """Same uid, so without the namespace /proc/<pid>/environ of another brain is readable."""
    a, a_out = _start(helper, ["/bin/sleep", "30"], env={"PATH": "/usr/bin:/bin", "CHILD_TOKEN": "tok-of-task-A"})
    assert _until(lambda: _sleeps() == 1)
    host_pid = int(subprocess.run(["pgrep", "-x", "-u", BRAIN, "sleep"], capture_output=True, text=True,
                                  check=True).stdout.split()[0])
    b, b_out = _start(helper, ["/bin/sh", "-c", f"cat /proc/{host_pid}/environ /proc/*/environ 2>/dev/null; true"])
    assert b.wait(10) == 0
    assert "tok-of-task-A" not in _read(b_out)
    # the host itself (root) sees it: the token was there to find
    assert b"tok-of-task-A" in Path(f"/proc/{host_pid}/environ").read_bytes()
    a.close()
    os.close(a_out)


def test_closing_the_connection_kills_setsid_children_too(helper):
    """No systemd scope here: the namespace's init dies, and the kernel kills the rest."""
    proc, out = _start(helper, ["/bin/sh", "-c", "setsid sleep 301 & setsid sh -c 'sleep 302 &' ; sleep 303"])
    assert _until(lambda: _sleeps() == 3), _brain_procs()
    proc.close()
    assert _until(lambda: not _brain_procs()), _brain_procs()
    os.close(out)


def test_signals_reach_the_program_and_exit_codes_come_back(helper):
    proc, out = _start(helper, ["/bin/sh", "-c", "trap 'exit 5' TERM; sleep 30 & wait"])
    time.sleep(0.5)
    proc.signal_group(signal.SIGTERM)
    assert proc.wait(10) == 5
    proc.close()
    os.close(out)
    proc, out = _start(helper, ["/bin/sleep", "30"])
    time.sleep(0.3)
    proc.signal_group(signal.SIGINT)
    assert proc.wait(10) == -signal.SIGINT
    proc.close()
    os.close(out)


def test_the_request_env_never_reaches_root(helper, tmp_path):
    """LD_PRELOAD in a request is set only for the program, after the drop (env -i)."""
    marker = tmp_path / "preloaded"
    proc, out = _start(helper, ["/usr/bin/env"], env={"PATH": "/usr/bin:/bin", "LD_PRELOAD": "/nonexistent.so",
                                                      "MARK": str(marker)})
    assert proc.wait(10) == 0
    text = _read(out)
    assert "LD_PRELOAD=/nonexistent.so" in text and "MARK=" in text


def test_the_brains_env_is_never_in_any_argv(helper):
    """The env (it carries the child grant token) goes through an fd: no process of the start
    chain (prlimit, unshare, setpriv, env, the loader) ever shows it in /proc/<pid>/cmdline."""
    token = "openajar-test-token-" + os.urandom(8).hex()
    seen: list[str] = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            for entry in Path("/proc").iterdir():
                if entry.name.isdigit():
                    with contextlib.suppress(OSError):
                        if token.encode() in (entry / "cmdline").read_bytes():
                            seen.append(entry.name)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        for _ in range(5):
            proc, out = _start(helper, ["/usr/bin/env"], env={"PATH": "/usr/bin:/bin", "TOKEN": token})
            assert proc.wait(10) == 0
            assert f"TOKEN={token}" in _read(out)
    finally:
        stop.set()
        watcher.join(5)
    assert not seen


@pytest.mark.skipif(not SYSTEMD, reason="needs systemd as PID 1")
def test_a_brain_and_its_init_go_into_the_scope_and_the_scope_is_killed(helper):
    proc, out = _start(helper, ["/bin/sleep", "30"], scope="agentos-brain-rt-01-cli", limits={"memory_mb": 256})
    assert proc.scope == "agentos-brain-rt-01-cli.scope"
    pids = [int(p.split()[0]) for p in _brain_procs()]
    assert pids and all("agentos-brain-rt-01-cli.scope" in Path(f"/proc/{p}/cgroup").read_text() for p in pids)
    mem = subprocess.run(["systemctl", "show", "-pMemoryMax", "agentos-brain-rt-01-cli.scope"], capture_output=True,
                         text=True, check=False).stdout
    assert mem.strip() == f"MemoryMax={256 * 1024 * 1024}"
    proc.close()
    assert _until(lambda: not _brain_procs())
    os.close(out)


def test_limits_without_systemd_are_refused(users):
    caller, brain = users

    class NoSystemd(runas.System):
        def has_systemd(self):
            return False

    d = Path(tempfile.mkdtemp(prefix="rn", dir="/run"))
    server = Server(caller=caller, brain=brain, socket_path=d / "runas.sock", system=NoSystemd(),
                    peer=lambda c: caller.uid)
    server.bind()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(RunasError) as e:
            _start(server, ["/bin/true"], scope="agentos-brain-x", limits={"memory_mb": 256})
        assert e.value.code == "no_scope"
    finally:
        server.stop()
        shutil.rmtree(d, ignore_errors=True)


# ---- the real helper process: a crash, and the sweep after it ----------------------------------

REPO = Path(runas.__file__).resolve().parents[2]


def _serve(sock: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-I", "-S", "-B", runas.__file__, "serve", "--caller", CALLER,
                             "--brain-user", BRAIN, "--socket", str(sock)], stderr=subprocess.PIPE)


def _client(sock: Path, argv: list[str]) -> subprocess.Popen:
    """A caller (as the caller user) that starts a brain and holds the connection."""
    code = ("import os, sys, time; from openajar import runas; "
            f"p = runas.request({str(sock)!r}, {{'identity': 'brain', 'argv': {argv!r}, 'cwd': '/'}}, (0, 1, 2)); "
            "print(p.pid, flush=True); time.sleep(120)")
    return subprocess.Popen([sys.executable, "-c", code], user=CALLER, group=CALLER, extra_groups=[],
                            env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"},
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, cwd="/")


@pytest.mark.parametrize("after", ["restart", "sweep"])
def test_a_helper_killed_with_sigkill_leaves_no_brain_behind(users, after):
    if (REPO / "src").stat().st_uid != 0:
        pytest.skip("the helper refuses code not owned by root: run from a root-owned copy")
    d = Path(tempfile.mkdtemp(prefix="rn", dir="/run"))
    sock = d / "runas.sock"
    helper = _serve(sock)
    assert _until(lambda: sock.exists()), helper.stderr.read1().decode() if helper.poll() is not None else ""
    client = _client(sock, ["/bin/sh", "-c", "setsid sleep 304 & sleep 305"])
    try:
        assert client.stdout.readline().strip()
        assert _until(lambda: _sleeps() == 2), _brain_procs()
        assert list((d / "brains").glob("*.json"))
        helper.kill()  # no stop, no sweep: the brains are orphans now
        helper.wait(10)
        time.sleep(0.5)
        assert _sleeps() == 2
        if after == "restart":
            helper = _serve(sock)
        else:  # ExecStopPost
            subprocess.run([sys.executable, "-I", "-S", "-B", runas.__file__, "sweep", "--caller", CALLER,
                            "--brain-user", BRAIN, "--socket", str(sock)], check=True, timeout=30)
        assert _until(lambda: not _brain_procs()), _brain_procs()
        assert not list((d / "brains").glob("*.json"))
    finally:
        client.kill()
        helper.kill()
        shutil.rmtree(d, ignore_errors=True)


def test_check_refuses_a_brain_user_in_a_privileged_group(users):
    if (REPO / "src").stat().st_uid != 0:
        pytest.skip("the helper refuses code not owned by root: run from a root-owned copy")
    argv = [sys.executable, "-I", "-S", "-B", runas.__file__, "check", "--caller", CALLER, "--brain-user", BRAIN]
    assert subprocess.run(argv, capture_output=True, text=True, check=False).returncode == 0
    subprocess.run(["usermod", "-aG", "adm", BRAIN], check=True)
    try:
        out = subprocess.run(argv, capture_output=True, text=True, check=False)
        assert out.returncode == 78 and "adm" in out.stderr, out.stderr
    finally:
        subprocess.run(["gpasswd", "-d", BRAIN, "adm"], capture_output=True, check=False)


# ---- a root agentosd's own drop -----------------------------------------------------------------

def test_a_root_agentosds_drop_puts_the_brain_in_its_own_namespace(users):
    _, brain = users
    code, out, err = launch.run(launch.DirectLauncher({"uid": brain.uid, "gid": brain.gid}),
                                ["/bin/sh", "-c", "id -u; ls /proc | grep -c '^[0-9]'; echo $FOO"],
                                env={"PATH": "/usr/bin:/bin", "FOO": "bar"})
    assert code == 0, err
    uid, nprocs, foo = out.decode().split("\n")[:3]
    assert int(uid) == brain.uid and int(nprocs) <= 4 and foo == "bar"
    proc = launch.DirectLauncher({"uid": brain.uid, "gid": brain.gid}).spawn(
        ["/bin/sleep", "30"], env={"PATH": "/usr/bin:/bin"}, cwd="/", stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    proc.signal_group(signal.SIGTERM)
    assert proc.wait(10) == -signal.SIGTERM


# ---- the confinement probe through the helper ---------------------------------------------------

def _home(caller: Identity, mode: int) -> Path:
    home = Path(tempfile.mkdtemp(prefix="agh", dir="/srv" if Path("/srv").is_dir() else "/var/tmp"))
    for d in ("secrets", "logs", "brains"):
        (home / d).mkdir()
    for f in ("node.toml", "vault.db", "secrets/vault.key", "logs/agentosd.log"):
        (home / f).write_text("secret")
        (home / f).chmod(0o600)
    for p in [home, *home.rglob("*")]:
        os.chown(p, caller.uid, caller.gid)
    for d in ("secrets", "logs", "brains"):
        (home / d).chmod(0o700)
    home.chmod(mode)
    return home


@pytest.mark.parametrize("mode, ok", [(0o700, True), (0o711, False)])
def test_the_probe_through_the_helper_judges_the_home(helper, users, mode, ok):
    caller, brain = users
    home = _home(caller, mode)
    try:
        ident = launch.BrainIdentity(brain.name, brain.uid, brain.gid, brain.home, helper=helper.socket_path)
        conf = confine.Confinement(home, launcher=lambda: launch.HelperLauncher(helper.socket_path,
                                                                                expect_uid=brain.uid),
                                   identity=lambda: ident, python=confine.probe_python(),
                                   cache_dir=Path(tempfile.mkdtemp()))
        r = conf.check(force=True)
        assert r.ok is ok, r.reason
        assert r.uid == brain.uid
        if not ok:
            assert f"traverse {home} (can stat {home}/node.toml)" in r.reachable
        # the probe is told to expect another uid: refused, whatever it could reach
        other = launch.BrainIdentity(brain.name, brain.uid + 1, brain.gid, brain.home, helper=helper.socket_path)
        conf.identity = lambda: other
        r = conf.check(force=True)
        assert not r.ok and "not the brain user" in r.reason
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_whoami_over_the_real_socket(helper, users):
    _, brain = users
    info = runas.whoami(helper.socket_path)
    assert info["brain"] == {"name": brain.name, "uid": brain.uid, "gid": brain.gid, "home": brain.home,
                             "groups": [brain.gid]}
    assert info["namespaces"] is True and info["scopes"] is SYSTEMD
    assert json.dumps(info)


# ---- a non-root agentosd: who brains run as is one answer --------------------------------------

AGENTOSD = """
import json, sys
from pathlib import Path
from openajar.brains import confine
from openajar.brains.service import BrainService
from openajar.brains.settings import BrainsSettings
home, sock, run_as = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
svc = BrainService(None, home=home, settings=BrainsSettings(run_as=run_as, runas_socket=sock, confinement="require"))
try:
    r = svc.check_confinement(force=True)
    print(json.dumps({"ok": True, "reason": r.reason, "uid": r.uid}))
except confine.BrainUnconfined as e:
    print(json.dumps({"ok": False, "reason": e.message}))
"""


@pytest.mark.parametrize("run_as, ok, why", [
    (BRAIN, True, "cannot reach any"),
    ("no-such-brain-user", False, "does not exist"),
    ("nobody", False, f"starts brains as {BRAIN}"),
])
def test_a_non_root_agentosd_checks_and_runs_only_the_brain_user_the_helper_serves(helper, users, run_as, ok, why):
    """The review's escape: run_as naming no account, a helper up, the probe run as the brain user
    and passed, the CLI run as agentosd. Now agentosd (here as the caller user) is refused."""
    caller, brain = users
    home = _home(caller, 0o700)
    try:
        out = subprocess.run([sys.executable, "-c", AGENTOSD, str(home), str(helper.socket_path), run_as],
                             user=CALLER, group=CALLER, extra_groups=[], capture_output=True, text=True, cwd="/",
                             env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin", "HOME": str(home)},
                             check=False, timeout=60)
        result = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {"stderr": out.stderr}
        assert result.get("ok") is ok and why in result.get("reason", ""), result
        if ok:
            assert result["uid"] == brain.uid
    finally:
        shutil.rmtree(home, ignore_errors=True)
