"""agentos-runas: the request schema, the identities it serves, and a real socket round trip."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos import runas
from agentos.runas import Identity, RunasError, Server, check_code_owner, check_identities, parse_request

CALLER = Identity("agentos", 990, 990)
BRAIN = Identity("agentos-brain", 991, 991)


def req(**kw):
    return {"v": runas.PROTOCOL, "op": "spawn", "identity": "brain", "argv": ["/usr/bin/true"], **kw}


# ---- the request schema ------------------------------------------------------------------------

def test_a_plain_request_parses():
    r = parse_request(req(env={"HOME": "/var/lib/agentos-brain"}, cwd="/tmp", scope="agentos-brain-t1-01-cli",
                          limits={"memory_mb": 1536, "tasks_max": 512, "cpu_quota_pct": 150},
                          rlimits={"core": 0, "nofile": 8192}))
    assert r.identity == "brain" and r.argv == ["/usr/bin/true"] and r.scope == "agentos-brain-t1-01-cli"
    assert r.limits["memory_mb"] == 1536 and not r.detach


@pytest.mark.parametrize("bad, why", [
    ({"identity": "root"}, "identity"),
    ({"identity": "agentos-brain"}, "identity"),
    ({"user": "root"}, "unknown fields"),
    ({"uid": 0}, "unknown fields"),
    ({"properties": {"User": "root"}}, "unknown fields"),
    ({"v": 2}, "expected"),
    ({"op": "exec"}, "expected"),
    ({"argv": []}, "argv"),
    ({"argv": "sh -c id"}, "argv"),
    ({"argv": ["/bin/sh", "a\0b"]}, "NUL"),
    ({"env": {"LD PRELOAD": "x"}}, "plain variable"),
    ({"env": {"X": 1}}, "env X"),
    ({"cwd": "relative"}, "absolute"),
    ({"scope": "sshd"}, "agentos-brain-"),
    ({"scope": "agentos-brain-x/../sshd"}, "agentos-brain-"),
    ({"scope": "agentos-brain-x", "limits": {"memory_mb": 1}}, "memory_mb"),
    ({"scope": "agentos-brain-x", "limits": {"cpu_quota_pct": 10**9}}, "cpu_quota_pct"),
    ({"scope": "agentos-brain-x", "limits": {"Delegate": 1}}, "not allowed"),
    ({"scope": "agentos-brain-x", "limits": {"memory_mb": True}}, "memory_mb"),
    ({"limits": {"memory_mb": 512}}, "need a scope"),
    ({"rlimits": {"nproc": 10}}, "not allowed"),
    ({"rlimits": {"core": -1}}, "non-negative"),
    ({"detach": True}, "brains always stay attached"),
    ({"detach": "yes", "identity": "caller"}, "true or false"),
])
def test_anything_outside_the_schema_is_refused(bad, why):
    with pytest.raises(RunasError, match=why) as e:
        parse_request(req(**bad))
    assert e.value.code == "invalid"


def test_only_the_caller_identity_may_detach():
    assert parse_request(req(identity="caller", detach=True)).detach


# ---- identities and code ------------------------------------------------------------------------

def test_identities_refuse_root_one_user_and_a_shared_group():
    check_identities(CALLER, BRAIN, groups_of=lambda u: {u.gid})
    for caller, brain in ((Identity("root", 0, 0), BRAIN), (CALLER, Identity("root", 0, 0)),
                          (CALLER, Identity("b", 991, 0)), (CALLER, CALLER)):
        with pytest.raises(RunasError):
            check_identities(caller, brain, groups_of=lambda u: {u.gid})
    with pytest.raises(RunasError, match="group"):
        check_identities(CALLER, BRAIN, groups_of=lambda u: {u.gid, CALLER.gid})


def _st(uid: int, mode: int):
    return SimpleNamespace(st_uid=uid, st_mode=mode)


def test_code_must_be_root_owned_and_not_writable_by_others():
    tree = {Path("/"): _st(0, stat.S_IFDIR | 0o755), Path("/opt"): _st(0, stat.S_IFDIR | 0o755),
            Path("/opt/agentos"): _st(0, stat.S_IFDIR | 0o755),
            Path("/opt/agentos/runas.py"): _st(0, stat.S_IFREG | 0o644),
            Path("/tmp"): _st(0, stat.S_IFDIR | 0o1777)}
    check_code_owner([Path("/opt/agentos/runas.py")], lstat=tree.__getitem__)
    check_code_owner([Path("/tmp")], lstat=tree.__getitem__)  # sticky, root-owned: fine
    for path, bad in ((Path("/opt/agentos"), _st(990, stat.S_IFDIR | 0o755)),
                      (Path("/opt/agentos/runas.py"), _st(0, stat.S_IFREG | 0o666)),
                      (Path("/opt"), _st(0, stat.S_IFDIR | 0o775))):
        with pytest.raises(RunasError, match="other than root"):
            check_code_owner([Path("/opt/agentos/runas.py")], lstat={**tree, path: bad}.__getitem__)


# ---- the server over a real socket (as the test's own user, no systemd) ------------------------

class FakeSystem(runas.System):
    """Spawns as the test's own user; records what root would have done."""

    def __init__(self, *, systemd: bool = False, scope_fails: bool = False):
        self.systemd = systemd
        self.scope_fails = scope_fails
        self.spawned: list[dict] = []
        self.scopes: list[tuple[str, int, dict]] = []
        self.killed_scopes: list[str] = []
        self.rlimits: list[tuple[int, str, int]] = []

    def has_systemd(self) -> bool:
        return self.systemd

    def has_namespaces(self) -> bool:
        return False

    def spawn(self, argv, *, env, user, fds, gate, rlimits, isolate):
        proc = subprocess.Popen(argv, env=env, cwd="/", stdin=fds[0], stdout=fds[1], stderr=fds[2],
                                pass_fds=(gate,), start_new_session=True, close_fds=True)
        proc.returncode = 0
        self.spawned.append({"argv": argv, "user": user.name, "pid": proc.pid})
        for name, value in rlimits.items():
            self.set_rlimit(proc.pid, name, value)
        return runas.Child(proc.pid)

    def start_scope(self, unit, pids, limits):
        if self.scope_fails:
            raise RunasError("scope_failed", f"could not start {unit}.scope")
        self.scopes.append((unit, pids[0], dict(limits)))

    def kill_scope(self, unit):
        self.killed_scopes.append(unit)

    def set_rlimit(self, pid, name, value):
        self.rlimits.append((pid, name, value))

    if not hasattr(os, "waitid"):  # macOS: no waitid(WNOWAIT), so reap at once (Linux uses the real one)
        def exited(self, pid, timeout):
            deadline = time.monotonic() + timeout
            while True:
                try:
                    done, status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    return -1
                if done:
                    return os.waitstatus_to_exitcode(status)
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.05)

        def killpg(self, pid, sig):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, sig)


@pytest.fixture
def served():
    """A Server on a short socket path (macOS caps unix paths near 104 bytes)."""
    made: list[tuple[Server, Path]] = []

    def start(system=None, *, caller_uid=None):
        d = Path(tempfile.mkdtemp(prefix="runas", dir="/tmp"))
        me = os.getuid()
        caller = Identity("me", me if caller_uid is None else caller_uid, os.getgid())
        server = Server(caller=caller, brain=BRAIN, socket_path=d / "s" / "runas.sock",
                        system=system or FakeSystem())
        server.bind()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        made.append((server, d))
        return server

    yield start
    for server, d in made:
        server.stop()
        shutil.rmtree(d, ignore_errors=True)


def _pipes():
    r, w = os.pipe()
    return r, w


def _run(server, body, *, stdin=b""):
    out_r, out_w = _pipes()
    in_r, in_w = _pipes()
    os.write(in_w, stdin)
    os.close(in_w)
    try:
        proc = runas.request(server.socket_path, body, (in_r, out_w, out_w))
    finally:
        os.close(in_r)
        os.close(out_w)
    return proc, out_r


def _read_all(fd):
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    os.close(fd)
    return b"".join(chunks)


def test_attached_program_runs_in_its_cwd_with_only_the_given_env(served):
    server = served()
    proc, out = _run(server, {"identity": "brain", "argv": ["/bin/sh", "-c", 'pwd; echo "$A:$HOME"; cat'],
                              "env": {"A": "1", "PATH": "/usr/bin:/bin"}, "cwd": "/tmp", "rlimits": {"core": 0}},
                     stdin=b"from stdin\n")
    assert proc.wait(10) == 0
    text = _read_all(out).decode()
    assert text.split("\n")[1:3] == ["1:", "from stdin"]
    assert text.split("\n")[0] in ("/tmp", "/private/tmp")
    assert server.system.spawned[0]["user"] == "agentos-brain"
    assert server.system.rlimits == [(proc.pid, "core", 0)]
    proc.close()


def test_exit_codes_and_signals_come_back(served):
    server = served()
    proc, out = _run(server, {"identity": "brain", "argv": ["/bin/sh", "-c", "exit 7"]})
    assert proc.wait(10) == 7
    proc.close()
    os.close(out)
    proc, out = _run(server, {"identity": "brain", "argv": ["/bin/sleep", "30"]})
    proc.signal_group(signal.SIGTERM)
    assert proc.wait(10) == -signal.SIGTERM
    proc.close()
    os.close(out)


def test_closing_the_connection_kills_the_program_and_its_scope(served):
    system = FakeSystem(systemd=True)
    server = served(system)
    proc, out = _run(server, {"identity": "brain", "argv": ["/bin/sleep", "30"], "scope": "agentos-brain-t1-01-cli",
                              "limits": {"memory_mb": 512}})
    assert proc.scope == "agentos-brain-t1-01-cli.scope"
    assert system.scopes == [("agentos-brain-t1-01-cli", proc.pid, {"memory_mb": 512})]
    pid = proc.pid
    proc.close()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (pid in server._live or "agentos-brain-t1-01-cli" not in
                                            system.killed_scopes):
        time.sleep(0.05)
    assert "agentos-brain-t1-01-cli" in system.killed_scopes and pid not in server._live
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    os.close(out)


def test_a_scope_that_cannot_start_means_the_program_never_runs(served):
    server = served(FakeSystem(systemd=True, scope_fails=True))
    marker = Path(tempfile.mkdtemp(prefix="runas", dir="/tmp")) / "ran"
    with pytest.raises(RunasError) as e:
        _run(server, {"identity": "brain", "argv": ["/usr/bin/touch", str(marker)], "scope": "agentos-brain-x"})
    assert e.value.code == "scope_failed"
    time.sleep(0.3)
    assert not marker.exists()  # the gate never opened
    shutil.rmtree(marker.parent)


def test_detached_caller_programs_outlive_the_connection(served):
    server = served()
    proc, out = _run(server, {"identity": "caller", "argv": ["/bin/sh", "-c", "sleep 0.5; echo done"],
                              "detach": True})
    assert server.system.spawned[0]["user"] == "me"
    proc.close()
    assert _read_all(out) == b"done\n"


def test_another_uid_is_refused(served):
    server = served(caller_uid=os.getuid() + 1)
    with pytest.raises(RunasError) as e:
        _run(server, {"identity": "brain", "argv": ["/usr/bin/true"]})
    assert e.value.code == "forbidden"
    assert server.system.spawned == []


def test_a_malformed_request_gets_an_error_not_a_crash(served):
    server = served()
    with pytest.raises(RunasError) as e:
        _run(server, {"identity": "brain", "argv": ["/usr/bin/true"], "user": "root"})
    assert e.value.code == "invalid"
    proc, out = _run(server, {"identity": "brain", "argv": ["/usr/bin/true"]})  # still serving
    assert proc.wait(10) == 0
    proc.close()
    os.close(out)


def test_serve_refuses_unless_root():
    if os.geteuid() == 0:
        pytest.skip("runs as root")
    out = subprocess.run([sys.executable, "-I", "-S", runas.__file__, "check", "--caller", "nobody",
                          "--brain-user", "nobody"], capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 78 and "runs as root" in out.stderr


def test_the_gate_works_with_a_two_digit_fd(served):
    """dash refuses `<&12` (single-digit fds only): the gate runs in bash, and a busy helper's gate
    pipe is well past fd 9."""
    server = served()
    held = [os.pipe() for _ in range(8)]
    try:
        proc, out = _run(server, {"identity": "brain", "argv": ["/bin/sh", "-c", "echo ok"]})
        assert proc.wait(10) == 0 and _read_all(out) == b"ok\n"
        proc.close()
    finally:
        for r, w in held:
            os.close(r)
            os.close(w)


# ---- a hostile or broken caller never takes the helper down ------------------------------------

def _raw(server, payload: bytes, fds: list[int], *, timeout: float = 10) -> bytes:
    """Send `payload` as the request body (length-prefixed) with `fds`; the helper's first line."""
    import socket
    import struct

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(server.socket_path))
    data = struct.pack(">I", len(payload)) + payload
    sent = socket.send_fds(sock, [data[:1024]], fds)
    if data[sent:]:  # an empty send to a helper that already answered and closed is EPIPE on macOS
        sock.sendall(data[sent:])
    try:
        return sock.makefile("rb").readline()
    finally:
        sock.close()


def _all_closed(r: int, timeout: float = 5) -> bool:
    """EOF on `r` once every copy of the pipe's write end (ours and any the helper kept) is closed."""
    import select

    ready, _, _ = select.select([r], [], [], timeout)
    return bool(ready) and os.read(r, 1) == b""


@pytest.mark.parametrize("payload", [b"[" * 200_000, b'{"argv": ' + b"[" * 50 + b"]" * 50 + b"}", b"[]", b"\xff"])
def test_a_bad_body_is_refused_and_its_fds_are_closed(served, payload):
    """Deep JSON raised RecursionError before the received fds were owned: they leaked."""
    server = served()
    r, w = os.pipe()
    try:
        line = _raw(server, payload, [w, w, w])
        os.close(w)
        assert b'"invalid"' in line
        assert _all_closed(r)
    finally:
        os.close(r)
    assert runas.too_deep(b"[" * 5, 4) and not runas.too_deep(b'["[[[[[["]', 4)


def test_a_stalled_caller_loses_its_slot(served, monkeypatch):
    import socket
    import struct

    monkeypatch.setattr(runas, "REQUEST_TIMEOUT", 0.5)
    server = served()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(server.socket_path))
    sock.sendall(struct.pack(">I", 100) + b"{")  # promises 100 bytes, sends 1
    t0 = time.monotonic()
    line = sock.makefile("rb").readline()
    assert b"in time" in line and time.monotonic() - t0 < 4
    sock.close()


def test_accept_running_out_of_fds_backs_off_instead_of_dying(served):
    import errno

    server = served()
    real = server._sock

    class Flaky:
        def __init__(self):
            self.fails = 3

        def settimeout(self, t):
            real.settimeout(t)

        def accept(self):
            if self.fails:
                self.fails -= 1
                raise OSError(errno.EMFILE, "Too many open files")
            return real.accept()

    server._stop.set()  # the fixture's loop: let it go, and run one over the flaky socket
    time.sleep(0.7)
    server._stop.clear()
    server._sock = Flaky()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, out = _run(server, {"identity": "brain", "argv": ["/usr/bin/true"]})
    assert proc.wait(10) == 0 and server._sock.fails == 0
    proc.close()
    os.close(out)


def test_a_crashed_helpers_brains_are_swept_by_the_next_one(served):
    """A helper that dies (no stop, no sweep) leaves a record; the next start kills what it names."""
    system = FakeSystem(systemd=True)
    server = served(system)
    proc, out = _run(server, {"identity": "brain", "argv": ["/bin/sleep", "30"], "scope": "agentos-brain-t9-01-cli"})
    recorded = json.loads((server.state_dir / f"{proc.pid}.json").read_text())
    assert recorded["pid"] == proc.pid and recorded["scope"] == "agentos-brain-t9-01-cli"
    # a new helper on the same runtime directory, as systemd restarts it
    again = Server(caller=server.caller, brain=BRAIN, socket_path=server.socket_path.parent / "again.sock",
                   system=FakeSystem(systemd=True), state_dir=server.state_dir)
    again.bind()
    assert proc.wait(10) == -signal.SIGKILL
    assert again.system.killed_scopes == ["agentos-brain-t9-01-cli"] and not list(server.state_dir.glob("*.json"))
    proc.close()
    os.close(out)


def test_a_program_that_ends_is_forgotten(served):
    server = served()
    proc, out = _run(server, {"identity": "brain", "argv": ["/usr/bin/true"]})
    assert proc.wait(10) == 0
    proc.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and list(server.state_dir.glob("*.json")):
        time.sleep(0.05)
    assert not list(server.state_dir.glob("*.json"))
    os.close(out)


def test_limits_without_systemd_are_refused_not_dropped(served):
    server = served(FakeSystem(systemd=False))
    with pytest.raises(RunasError) as e:
        _run(server, {"identity": "brain", "argv": ["/usr/bin/true"], "scope": "agentos-brain-x",
                      "limits": {"memory_mb": 512}})
    assert e.value.code == "no_scope" and server.system.spawned == []
    proc, out = _run(server, {"identity": "brain", "argv": ["/usr/bin/true"], "scope": "agentos-brain-x"})
    assert proc.wait(10) == 0 and proc.scope is None  # a scope alone is a request for tidiness: dropped
    proc.close()
    os.close(out)


@pytest.mark.parametrize("bad", [{"env": {"PATH\n": "x"}}, {"scope": "agentos-brain-x\n"}])
def test_names_match_whole_not_up_to_a_newline(bad):
    with pytest.raises(RunasError, match="plain variable|agentos-brain-"):
        parse_request(req(**bad))


def test_whoami_names_the_brain_user_and_spawn_reports_its_uid(served):
    server = served()
    info = runas.whoami(server.socket_path)
    assert info["brain"]["name"] == "agentos-brain" and info["brain"]["uid"] == BRAIN.uid
    assert info["caller"]["uid"] == os.getuid() and info["scopes"] is False
    proc, out = _run(server, {"identity": "brain", "argv": ["/usr/bin/true"]})
    assert proc.uid == BRAIN.uid
    proc.close()
    os.close(out)


def test_a_program_started_as_another_uid_than_expected_is_refused_and_killed(served):
    server = served()
    out_r, out_w = _pipes()
    with pytest.raises(RunasError) as e:
        runas.request(server.socket_path, {"identity": "brain", "argv": ["/bin/sleep", "30"]},
                      (out_w, out_w, out_w), expect_uid=BRAIN.uid + 1)
    assert e.value.code == "identity"
    os.close(out_w)
    assert _all_closed(out_r, timeout=10)  # the sleep was killed: nothing holds the pipe
    os.close(out_r)


def test_a_brain_user_in_a_privileged_group_is_refused():
    forbidden = {0: "root", 27: "sudo", 999: "docker"}
    check_identities(CALLER, BRAIN, groups_of=lambda u: {u.gid}, forbidden=forbidden)
    for extra in (27, 999, 0):
        with pytest.raises(RunasError, match="privileged group"):
            check_identities(CALLER, BRAIN, groups_of=lambda u, g=extra: {u.gid, g}, forbidden=forbidden)
    with pytest.raises(RunasError, match="root"):
        check_identities(CALLER, Identity("b", 991, 0), groups_of=lambda u: {u.gid}, forbidden=forbidden)


def test_code_paths_cover_pth_files_on_sys_path(tmp_path, monkeypatch):
    site = tmp_path / "site-packages"
    site.mkdir()
    (site / "_evil.pth").write_text("import os\n")
    monkeypatch.setattr(sys, "path", [*sys.path, str(site)])
    paths = runas._code_paths()
    assert site.resolve() in paths and (site / "_evil.pth").resolve() in paths


def test_the_unit_runs_the_helper_without_site():
    unit = (Path(__file__).resolve().parents[2] / "deploy" / "agentos-runas.service").read_text()
    for line in unit.splitlines():
        if line.startswith(("ExecStart=", "ExecStartPre=", "ExecStopPost=")):
            assert " -I -S -B " in line and "src/agentos/runas.py" in line and " -m " not in line, line
    assert "ExecStopPost=" in unit and "RuntimeDirectoryPreserve=yes" in unit


# ---- the brain's env goes through a file descriptor, never an argv --------------------------

def _load(env: dict[str, str], *program: str) -> bytes:
    fd = runas.env_fd(env)
    try:
        out = subprocess.run(["/usr/bin/env", "-i", "/bin/bash", "-c", runas.env_load(fd), "agentos-env", *program],
                             pass_fds=(fd,), capture_output=True, check=True, timeout=10)
    finally:
        os.close(fd)
    return out.stdout


def test_ns_chain_carries_no_env_and_names_the_fd():
    chain = runas.ns_chain(1000, 1000, 7, {"core": 0})
    assert chain[-6:] == [runas.TOOLS["env"], "-i", runas.TOOLS["bash"], "-c", runas.env_load(7), "agentos-env"]
    assert "<&7" in runas.env_load(7) and "exec 7<&-" in runas.env_load(7)
    assert not any("=" in a and not a.startswith("--") for a in chain if a != runas.env_load(7))


def test_the_loader_gives_the_program_exactly_the_env_and_closes_the_fd():
    env = {"PATH": "/usr/bin:/bin", "TOKEN": "a b\nc 'q' \"d\" $x `y` \\z", "EMPTY": "", "LD_PRELOAD": "/x.so",
           "UID": "7"}
    gate = ["/bin/bash", "-c", 'cd / && exec "$@"', "gate", "/usr/bin/env", "-0"]
    got = dict(kv.split("=", 1) for kv in _load(env, *gate).decode().split("\0") if kv)
    expect = {k: v for k, v in env.items() if k != "UID"}  # a name bash keeps read-only is skipped
    assert {k: got[k] for k in expect} == expect
    assert "UID" not in got
    assert set(got) - set(expect) <= {"PWD", "OLDPWD", "SHLVL", "_"}



def test_the_loader_closes_the_env_fd_before_the_program_runs():
    fd = runas.env_fd({"PATH": "/usr/bin:/bin"})
    try:
        out = subprocess.run(["/usr/bin/env", "-i", "/bin/bash", "-c", runas.env_load(fd), "agentos-env",
                              "/bin/bash", "-c", f'if : <&{fd}; then echo open; else echo closed; fi 2>/dev/null'],
                             pass_fds=(fd,), capture_output=True, check=True, timeout=10, text=True)
    finally:
        os.close(fd)
    assert out.stdout.strip() == "closed"


def test_env_fd_is_close_on_exec_and_reads_from_the_start():
    fd = runas.env_fd({"A": "1", "B": "two"})
    try:
        assert not os.get_inheritable(fd)
        assert os.read(fd, 100) == b"A=1\0B=two\0"
    finally:
        os.close(fd)


def test_names_the_loader_could_use_reach_the_program_untouched():
    env = {"PATH": "/usr/bin:/bin", "f": "1", "kv": "zz", "g": "0</dev/null; touch /tmp/agentos-never",
           "IFS": "x", "POSIXLY_CORRECT": "1", "SHLVL": "3"}
    got = dict(kv.split("=", 1) for kv in _load(env, "/usr/bin/env", "-0").decode().split("\0") if kv)
    assert {k: got.get(k) for k in ("f", "kv", "g", "IFS", "POSIXLY_CORRECT")} == \
        {k: env[k] for k in ("f", "kv", "g", "IFS", "POSIXLY_CORRECT")}
    assert runas.LOADER_VAR not in got


def test_the_loader_variable_is_refused_as_an_env_name():
    with pytest.raises(ValueError):
        runas.env_fd({runas.LOADER_VAR: "x"})
    with pytest.raises(RunasError):
        parse_request({"identity": "brain", "argv": ["/bin/true"], "env": {runas.LOADER_VAR: "x"}})


def test_undecodable_bytes_in_a_value_go_through_as_bytes():
    raw = b"a\xff\xfeb"
    fd = runas.env_fd({"V": raw.decode("utf-8", "surrogateescape")})
    try:
        assert os.read(fd, 100) == b"V=" + raw + b"\0"
    finally:
        os.close(fd)


def test_env_fd_refuses_what_the_helper_would_refuse():
    for bad in ({"A\0B": "x"}, {"BAD-NAME": "x"}, {"HOME": "/x\0INJECTED=yes"}):
        with pytest.raises(ValueError):
            runas.env_fd(bad)
