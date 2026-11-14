"""`agentos run` end to end on a node: the CLI, McpSession over HTTP to a real agentosd (in a temp
home, on a free loopback port) with the real policy and a real sandboxed env manager. The lane's
network stays off, so nothing leaves the node."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_env_node_support import CAN_RUN, needs_node, new_home
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from test_client_fakes import NodeServer

from agentos.cli import app
from agentos.client.state import Connection, ConnectionStore, Paths
from agentos.client.tokens import FileStore
from agentos.core.models import Effect
from agentos.env.manager import EnvManager
from agentos.env.paths import EnvSettings

pytestmark = [pytest.mark.node, needs_node]


@pytest.fixture
def node(tmp_path):
    if not CAN_RUN:
        pytest.skip("needs a Linux node, root and bubblewrap")
    holder: dict = {}
    home = new_home("run-e2e")

    def setup(runtime):
        mgr = EnvManager(home, runtime=runtime, settings=EnvSettings(max_timeout_s=120))
        runtime.register("envs", mgr)
        holder["mgr"] = mgr

    server = NodeServer(tmp_path / "node-home", "node-token-run-e2e", setup=setup).start()
    server.mgr = holder["mgr"]
    yield server
    server.on_loop(server.mgr.shutdown(), 60)
    server.stop()
    subprocess.run(["rm", "-rf", str(home)], check=False)


def test_agentos_run_in_a_real_sandbox(node, tmp_path, monkeypatch):
    laptop = tmp_path / "laptop"
    monkeypatch.setenv("HOME", str(laptop))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(laptop / ".cache"))
    _, token = node.runtime.policy.issue(subject={"id": "claude-code@laptop"}, workspaces=["*"],
                                         ceiling=Effect.COMMIT, specials={"exec": {}})
    paths = Paths(laptop)
    ConnectionStore(paths).save(Connection(
        name="vm", ssh_host="localhost", remote_port=node.port, local_port=node.port, token_store="file",
        service="none", credential="agent_grant", agent_specials=["exec"]))
    FileStore(paths).put("vm", token)

    repo = tmp_path / "proj"
    (repo / "tests").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text(".venv/\n")
    (repo / "tests" / "suite.sh").write_text(
        "#!/bin/sh\nfor i in 1 2 3; do echo case $i ok; sleep 0.2; done\necho 1 failed >&2\nexit 1\n")
    (repo / "tests" / "suite.sh").chmod(0o755)
    monkeypatch.chdir(repo / "tests")

    stop = threading.Event()

    def owner():  # approves the first exec in the new lane, as a person would in the panel
        while not stop.is_set():
            for ap in node.runtime.policy.approvals.list(state="pending"):
                node.runtime.policy.approvals.decide(ap.id, "approve", by="owner")
            time.sleep(0.05)

    threading.Thread(target=owner, daemon=True).start()
    try:
        r = CliRunner().invoke(app, ["run", "--disk", "256", "--memory", "512", "--cpus", "1", "--",
                                     "./suite.sh"])
    finally:
        stop.set()
    assert r.exit_code == 1, r.output
    assert r.stdout == "case 1 ok\ncase 2 ok\ncase 3 ok\n" and "1 failed" in r.stderr
    assert "creating lane dev/run-proj" in r.stderr and "agentos run: exit 1" in r.stderr

    r = CliRunner().invoke(app, ["run", "--", "sh", "-c", "pwd; mkdir -p ../.venv; ls -A .."])
    assert r.exit_code == 0, r.output
    assert r.stdout.splitlines()[0] == "/root/src/proj/tests"
    assert r.stdout.split()[1:] == [".gitignore", ".venv", "tests"]  # .git is not part of the tree
