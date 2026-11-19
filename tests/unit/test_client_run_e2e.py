"""`agentos run` end to end: the CLI, McpSession over HTTP, a real agentosd with the real policy
service (grants, first-use approvals) and the env manager on the fake host (commands run here).

The connection is a local one (no tunnel), holding an agent grant as `agentos connect` would make it."""

from __future__ import annotations

import subprocess
import threading
import time

import pytest
from test_client_fakes import NodeServer
from typer.testing import CliRunner

from agentos.cli import app
from agentos.client.state import Connection, ConnectionStore, Paths
from agentos.client.tokens import FileStore
from agentos.core.models import Effect
from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager
from agentos.env.paths import EnvSettings
from agentos.env.sandbox import DirectLauncher

TOKEN = "node-token-for-the-run-test"


@pytest.fixture
def node(tmp_path):
    holder: dict = {}

    def setup(runtime):
        mgr = EnvManager(tmp_path / "env-home", runtime=runtime, host=FakeHost(), launcher=DirectLauncher(),
                         use_lock=False, settings=EnvSettings(proxy_port=0, max_timeout_s=60))
        runtime.register("envs", mgr)
        holder["mgr"] = mgr

    server = NodeServer(tmp_path / "node-home", TOKEN, setup=setup).start()
    server.mgr = holder["mgr"]
    yield server
    server.on_loop(server.mgr.shutdown())
    server.stop()


def connect(node, home, monkeypatch, specials: dict) -> Connection:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    _, token = node.runtime.policy.issue(subject={"id": "claude-code@testmac"}, workspaces=["*"],
                                         ceiling=Effect.COMMIT, specials=specials)
    paths = Paths(home)
    conn = Connection(name="mynode", ssh_host="localhost", remote_port=node.port, local_port=node.port,
                      token_store="file", service="none", credential="agent_grant",
                      agent_specials=sorted(specials))
    ConnectionStore(paths).save(conn)
    FileStore(paths).put("mynode", token)
    return conn


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "check.sh").write_text("#!/bin/sh\nfor i in 1 2 3; do echo line $i; done\necho warn >&2\nexit 5\n")
    (root / "check.sh").chmod(0o755)
    monkeypatch.chdir(root)
    return root


def approve_when_asked(node, stop: threading.Event) -> list[str]:
    """The owner, in the panel: approves every pending approval as it appears."""
    seen: list[str] = []

    def loop():
        while not stop.is_set():
            for ap in node.runtime.policy.approvals.list(state="pending"):
                node.runtime.policy.approvals.decide(ap.id, "approve", by="owner")
                seen.append(ap.summary)
            time.sleep(0.05)

    threading.Thread(target=loop, daemon=True).start()
    return seen


def test_run_over_the_tunnel_with_a_grant(node, repo, tmp_path, monkeypatch):
    connect(node, tmp_path / "laptop", monkeypatch, {"exec": {}})
    stop = threading.Event()
    seen = approve_when_asked(node, stop)
    try:
        r = CliRunner().invoke(app, ["run", "--", "./check.sh"])
    finally:
        stop.set()
    assert r.exit_code == 5, r.output
    assert r.stdout == "line 1\nline 2\nline 3\n"
    assert "warn" in r.stderr and "agentos run: exit 5" in r.stderr
    # creating the lane is the first exec in it, so a person approved it; the run after was routine
    assert "waiting for approval: Create sandbox terminal dev/run-proj" in r.stderr
    assert len(seen) == 1 and "dev/run-proj" in seen[0]
    assert (node.mgr.paths.workdir("dev/run-proj") / "src/proj/check.sh").exists()

    r = CliRunner().invoke(app, ["run", "--detach", "--", "sh", "-c", "echo bg; sleep 0.3"])
    assert r.exit_code == 0, r.output
    jid = next(line.split()[2] for line in r.stderr.splitlines() if line.startswith("started job"))
    r = CliRunner().invoke(app, ["run", "--follow", jid])
    assert r.exit_code == 0 and r.stdout == "bg\n"
    r = CliRunner().invoke(app, ["run", "--list"])
    assert jid in r.stdout, (r.output, r.stderr)


def test_a_grant_without_exec_is_told_before_anything_runs(node, repo, tmp_path, monkeypatch):
    connect(node, tmp_path / "laptop", monkeypatch, {})
    r = CliRunner().invoke(app, ["run", "--", "true"])
    assert r.exit_code == 1 and "no exec special" in r.stderr
    assert not node.mgr.list()
