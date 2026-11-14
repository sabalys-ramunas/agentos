"""How brain programs start: directly (root drops to the brain user) or through agentos-runas, the
brain's own files reached as the brain, and a whole shim run through the helper."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import threading
from pathlib import Path

import pytest
from test_runas import FakeSystem

from agentos import runas
from agentos.brains import fakecli, launch
from agentos.brains.shim import Shim
from agentos.runas import Identity, Server


@pytest.fixture
def helper():
    """A runas Server for the test's own user (the 'brain' identity is the test user too)."""
    d = Path(tempfile.mkdtemp(prefix="launch", dir="/tmp"))
    me = Identity("me", os.getuid(), os.getgid())
    system = FakeSystem()
    server = Server(caller=me, brain=me, socket_path=d / "r.sock", system=system)
    server.bind()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.stop()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(params=["direct", "helper"])
def launcher(request, helper):
    if request.param == "direct":
        return launch.DirectLauncher()
    return launch.HelperLauncher(helper.socket_path)


def test_run_returns_code_output_and_feeds_stdin(launcher):
    code, out, err = launch.run(launcher, ["/bin/sh", "-c", "cat; echo oops >&2; exit 3"], input=b"hello")
    assert (code, out, err) == (3, b"hello", b"oops\n")


def test_run_kills_what_outlives_its_timeout(launcher):
    code, _, _ = launch.run(launcher, ["/bin/sleep", "30"], timeout=0.5)
    assert code == -9


def test_brain_files_are_private_to_the_brain(launcher, tmp_path):
    files = launch.BrainFiles(launcher)
    base = tmp_path / "brain" / "tasks" / "t1"
    files.mkdirs(base / "work", base / "home")
    assert stat.S_IMODE((base / "home").stat().st_mode) == 0o700
    files.write(base / "home" / "agentos-mcp.json", '{"token": "x"}', 0o600)
    assert stat.S_IMODE((base / "home" / "agentos-mcp.json").stat().st_mode) == 0o600
    assert files.read(base / "home" / "agentos-mcp.json") == b'{"token": "x"}'
    assert files.read(base / "home" / "missing") is None
    src = tmp_path / "stage"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "a.txt").write_text("A")
    (src / "README").write_text("R")
    files.unpack(src, base / "work")
    assert (base / "work" / "pkg" / "a.txt").read_text() == "A" and (base / "work" / "README").read_text() == "R"
    files.remove(base / "home" / "agentos-mcp.json")
    assert not (base / "home" / "agentos-mcp.json").exists()


def test_a_fifo_the_brain_planted_is_replaced_not_written_into(launcher, tmp_path):
    files = launch.BrainFiles(launcher)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    os.mkfifo(home / ".codex" / "config.toml")
    files.write(home / ".codex" / "config.toml", "model = 'x'\n", 0o600)  # would block on the FIFO
    assert stat.S_ISREG(os.lstat(home / ".codex" / "config.toml").st_mode)
    assert (home / ".codex" / "config.toml").read_text() == "model = 'x'\n"
    assert os.listdir(home / ".codex") == ["config.toml"]


def test_brain_files_report_a_failure(launcher, tmp_path):
    (tmp_path / "file").write_text("")
    with pytest.raises(launch.BrainFilesError, match="as the brain user failed"):
        launch.BrainFiles(launcher).mkdirs(tmp_path / "file" / "sub")


def test_from_spec_and_describe():
    assert isinstance(launch.from_spec(None), launch.DirectLauncher)
    helper = launch.from_spec({"kind": "helper", "socket": "/run/agentos-runas/runas.sock", "brain_uid": 991})
    assert isinstance(helper, launch.HelperLauncher) and helper.identity == "brain" and not helper.detach
    assert helper.expect_uid == 991
    assert launch.describe(helper) == "agentos-runas (/run/agentos-runas/runas.sock, uid 991)"
    with pytest.raises(ValueError, match="brain's uid"):  # the CLI must run as the uid the probe passed for
        launch.from_spec({"kind": "helper", "socket": "/run/agentos-runas/runas.sock"})
    assert launch.describe(launch.DirectLauncher({"uid": 991, "gid": 991})) == "direct drop to uid 991"
    assert launch.describe(launch.DirectLauncher()) == "direct (same user)"


def test_an_unreachable_helper_is_a_clear_error(tmp_path):
    with pytest.raises(runas.RunasError, match="cannot reach the agentos-runas helper") as e:
        launch.run(launch.HelperLauncher(tmp_path / "nope.sock"), ["/usr/bin/true"])
    assert e.value.code == "unavailable"


def _shim_launch(tmp_path: Path, launcher_spec: dict | None, prompt: str) -> tuple[Path, dict]:
    bins = fakecli.install(tmp_path / "bin")
    adir = tmp_path / "attempt"
    work = tmp_path / "work"
    adir.mkdir()
    work.mkdir()
    (adir / "prompt.txt").write_text(prompt)
    return adir, {
        "task_id": "t1", "attempt": 1, "brain": "c", "runner": "claude",
        "argv": [bins["claude"], "-p", "--output-format", "stream-json", "--verbose"], "cwd": str(work),
        "env": {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, "stdin_file": str(adir / "prompt.txt"),
        "run_as": None, "launcher": launcher_spec, "unit": None,
        "limits": {"max_seconds": 60, "stall_seconds": 30, "grace_seconds": 0.5},
        "rlimits": {"core": 0, "nofile": 4096}}


def test_a_shim_runs_the_cli_through_the_helper(helper, tmp_path):
    spec = {"kind": "helper", "socket": str(helper.socket_path), "scope": "agentos-brain-t1-01-cli",
            "brain_uid": os.getuid()}
    adir, data = _shim_launch(tmp_path, spec, "say hi")
    assert Shim(adir, data, {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-test"}, "redact": []}).run() == 0
    status = json.loads((adir / "status.json").read_text())
    assert status["outcome"] == "succeeded", status
    assert helper.system.rlimits and {r[1] for r in helper.system.rlimits} == {"core", "nofile"}
    dump = json.loads((tmp_path / "work" / "fake-dump.json").read_text())
    assert dump["env"].get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat-test" and dump["prompt"].strip() == "say hi"


def test_a_shim_cancel_reaches_the_cli_through_the_helper(helper, tmp_path):
    spec = {"kind": "helper", "socket": str(helper.socket_path), "brain_uid": os.getuid()}
    adir, data = _shim_launch(tmp_path, spec, "FAKE:hang")
    shim = Shim(adir, data, {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-test"}})
    threading.Timer(1.0, lambda: setattr(shim, "cancel_requested", True)).start()
    assert shim.run() == 0
    status = json.loads((adir / "status.json").read_text())
    assert status["outcome"] == "cancelled", status


def test_a_refused_spawn_finishes_the_attempt(tmp_path):
    adir, data = _shim_launch(tmp_path, {"kind": "helper", "socket": str(tmp_path / "gone.sock"), "brain_uid": 991}, "hi")
    assert Shim(adir, data, {}).run() == 0
    status = json.loads((adir / "status.json").read_text())
    assert status["outcome"] == "spawn_failed" and "agentos-runas" in status["reason"]

