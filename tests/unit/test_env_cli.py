import json
import sys

import pytest
from typer.testing import CliRunner

from agentos.env import manager
from agentos.env.cli import app
from agentos.env.host import FakeHost
from agentos.env.sandbox import DirectLauncher

runner = CliRunner()


@pytest.fixture
def home(tmp_path):
    return str(tmp_path / "home")


@pytest.fixture
def isolating(monkeypatch):
    """A node that can isolate (FakeHost), so sandboxed lanes can be created here."""
    monkeypatch.setattr(manager, "default_mechanisms", lambda paths, settings: (FakeHost(), DirectLauncher()))


@pytest.fixture
def not_isolating(monkeypatch):
    """A hardened node: agentosd is not root, so no sandbox."""
    from agentos.env.host import NullHost
    reason = manager.NOT_ROOT.format(user="agentos")
    monkeypatch.setattr(manager, "default_mechanisms", lambda paths, settings: (NullHost(reason), DirectLauncher()))


def run(home, *args):
    return runner.invoke(app, ["--home", home, *args])


def test_create_list_exec_remove(home, isolating):
    r = run(home, "create", "personal/bench", "--preset", "workbench", "--can-read-node-secrets")
    assert r.exit_code == 0, r.output
    assert "created terminal lane personal/bench" in r.output and "No isolation" in r.output
    assert "node token" in r.output
    r = run(home, "create", "personal/box", "--network", "isolated", "--allow", "example.com", "--memory-mb", "256")
    assert r.exit_code == 0, r.output
    r = run(home, "list")
    assert "personal/bench" in r.output and "personal/box" in r.output and "net=isolated" in r.output
    data = json.loads(run(home, "list", "--json").output)
    assert {d["lane"]["id"] for d in data} == {"personal/bench", "personal/box"}
    r = run(home, "exec", "personal/bench", "--", "echo", "from cli")
    assert r.exit_code == 0 and "from cli" in r.output, r.output
    r = run(home, "exec", "personal/bench", "--", "exit 4")
    assert r.exit_code == 4
    r = run(home, "remove", "personal/box", "--yes")
    assert r.exit_code == 0 and "removed" in r.output


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="on Linux the node can isolate")
def test_isolated_lanes_need_a_linux_node(home):
    r = run(home, "create", "personal/box")
    assert r.exit_code == 1 and "Linux node" in r.output and "nothing was created" in r.output


def test_a_workbench_lane_needs_the_flag_that_names_the_risk(home, not_isolating):
    r = run(home, "create", "personal/bench", "--preset", "workbench")
    assert r.exit_code == 1, r.output
    assert "--can-read-node-secrets" in r.output and "the vault" in r.output and "node token" in r.output
    assert "Nothing was created" in r.output
    assert "no terminal or service lanes" in run(home, "list").output
    r = run(home, "create", "personal/bench", "--preset", "workbench", "--can-read-node-secrets")
    assert r.exit_code == 0, r.output
    assert "as root" not in r.output and "node token" in r.output


def test_a_hardened_node_refuses_lanes_it_can_never_run(home, not_isolating):
    r = run(home, "create", "personal/svc", "--preset", "service", "--kind", "service", "--cmd", "sleep 30")
    assert r.exit_code == 1 and "no_sandbox" in r.output, r.output
    assert "runs it as agentos" in r.output and "migrate-service-user --rollback" in r.output
    assert "--can-read-node-secrets" in r.output and "the vault" in r.output
    assert "no terminal or service lanes" in run(home, "list").output
    r = run(home, "create", "personal/box")
    assert r.exit_code == 1 and "no_sandbox" in r.output


def test_runtime_owned_names_a_fix_that_fits(home, isolating, monkeypatch):
    from agentos.env.manager import EnvManager, RuntimeOwned

    def owned(self):
        raise RuntimeOwned("agentosd is running terminal and service lanes")
    monkeypatch.setattr(EnvManager, "_acquire_lock", owned)
    run(home, "create", "personal/box")
    r = run(home, "reset", "personal/box")
    assert r.exit_code == 1
    assert "vault init" not in r.output and "stop agentosd first" in r.output, r.output


def test_errors_are_clean(home, isolating):
    r = run(home, "create", "personal/x", "--preset", "account")
    assert r.exit_code == 1 and "account preset" in r.output
    r = run(home, "create", "personal/svc", "--cmd", "sleep 1", "--network", "shared")
    assert r.exit_code == 1 and "never share" in r.output


def test_services_and_images_commands(home, isolating):
    r = run(home, "create", "personal/svc", "--cmd", "echo hi; sleep 30", "--health-cmd", "true")
    assert r.exit_code == 0 and "created service lane" in r.output, r.output
    r = run(home, "services", "start", "personal/svc", "--ttl", "60")
    assert r.exit_code == 0 and "wanted running" in r.output
    r = run(home, "services", "status", "personal/svc")
    assert "desired running" in r.output
    r = run(home, "services", "stop", "personal/svc")
    assert "wanted stopped" in r.output
    r = run(home, "images", "list")
    assert "dev-box" in r.output and "placeholder" in r.output


def test_remove_and_doctor_go_through_a_running_agentosd(home, isolating, monkeypatch):
    """QA 2026-11-10, finding 30: with agentosd running, the env runtime is its; the CLI asks it."""
    from agentos.env import cli
    from agentos.env.manager import EnvManager
    calls = []

    def daemon(method, path, *, person=False):
        calls.append((method, path, person))
        return {"removed": "personal/box"} if method == "DELETE" else {"isolation": True, "warm": []}

    def owned(self):
        raise AssertionError("the CLI ran the env runtime itself while agentosd owns it")

    run(home, "create", "personal/box")
    monkeypatch.setattr(cli, "_daemon", daemon)
    monkeypatch.setattr(EnvManager, "_acquire_lock", owned)
    r = run(home, "remove", "personal/box", "--yes")
    assert r.exit_code == 0 and "removed personal/box" in r.output, r.output
    r = run(home, "doctor")
    assert r.exit_code == 0 and "isolation: True" in r.output, r.output
    assert calls == [("DELETE", "/personal/box", True), ("GET", "/doctor", False)]
