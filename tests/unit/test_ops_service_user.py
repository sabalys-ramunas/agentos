"""openajar migrate-service-user: root to a service user and back, with a rollback on every failure."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_ops_support import FakeHost, make_home
from typer.testing import CliRunner

from openajar.ops import service_user
from openajar.ops.service_user import SwitchError, SwitchPlan, SystemSteps, run_rollback, run_switch

# ---- the order of steps (fake steps) --------------------------------------------------------


class Steps:
    def __init__(self, *, fail: set[str] = frozenset(), healthy: list[bool] | None = None, saved: bool = False):
        self.fail = set(fail)
        self.health = list(healthy if healthy is not None else [True])
        self.calls: list[str] = []
        self.state = {"user": "root", "owner": "0:0", "unit": "[Service]\n"} if saved else None

    def _do(self, name: str, value=None):
        self.calls.append(name)
        if name in self.fail:
            raise SwitchError(f"{name} broke")
        return value

    def preflight(self, plan): return self._do("preflight")

    def save_state(self, plan):
        self.state = self._do("save", {"user": "root", "owner": "0:0", "unit": "[Service]\n"})
        return self.state

    def load_state(self, plan): return self._do("load", self.state)
    def clear_state(self, plan): return self._do("clear")
    def drain(self, plan): return self._do("drain", {"closed": ["personal/reddit"]})
    def stop(self, plan): return self._do("stop")
    def install(self, plan): return self._do("install")
    def start(self, plan): return self._do("start")
    def restore(self, plan, state): return self._do("restore")

    def verify(self, plan, user):
        self.calls.append(f"verify:{user}")
        ok = self.health.pop(0) if self.health else True
        return ok, "fine" if ok else "MCP does not answer"


def plan(tmp_path: Path | None = None, **kw) -> SwitchPlan:
    return SwitchPlan(prefix=Path("/opt/agentos"), home=Path("/var/lib/agentos"), **kw)


def quiet(_: str) -> None:
    pass


def test_happy_path_order():
    steps = Steps()
    result = run_switch(plan(), steps, log=quiet)
    assert result.ok and result.state == "switched" and not result.rolled_back
    assert steps.calls == ["preflight", "save", "drain", "stop", "install", "start", "verify:agentos"]


def test_preflight_or_save_failure_changes_nothing():
    for broken in ("preflight", "save"):
        steps = Steps(fail={broken})
        result = run_switch(plan(), steps, log=quiet)
        assert result.state == "unchanged" and not result.rolled_back and f"{broken} broke" in result.error
        assert "stop" not in steps.calls and "install" not in steps.calls


def test_stop_failure_starts_it_again_and_changes_nothing():
    steps = Steps(fail={"stop"})
    result = run_switch(plan(), steps, log=quiet)
    assert result.state == "unchanged" and "could not stop" in result.error
    assert steps.calls[-1] == "start" and "install" not in steps.calls


def test_unhealthy_service_user_rolls_back_to_root():
    steps = Steps(healthy=[False, True])
    result = run_switch(plan(), steps, log=quiet)
    assert not result.ok and result.rolled_back and result.state == "rolled_back"
    assert steps.calls == ["preflight", "save", "drain", "stop", "install", "start", "verify:agentos",
                           "stop", "restore", "start", "verify:root"]
    assert "not healthy as agentos" in result.error
    assert "clear" not in steps.calls  # the saved state stays: it is what a later --rollback reads


def test_failed_install_rolls_back():
    steps = Steps(fail={"install"})
    result = run_switch(plan(), steps, log=quiet)
    assert result.state == "rolled_back" and steps.calls[-3:] == ["restore", "start", "verify:root"]


def test_a_rollback_that_fails_its_check_reports_broken():
    steps = Steps(healthy=[False, False])
    result = run_switch(plan(), steps, log=quiet)
    assert result.state == "broken" and not result.ok


def test_rollback_needs_a_saved_state():
    steps = Steps()
    result = run_rollback(plan(), steps, log=quiet)
    assert result.state == "unchanged" and "nothing to roll back" in result.error
    assert steps.calls == ["load"]


def test_a_failed_restore_still_starts_agentosd_and_keeps_the_state():
    """agentosd is started even when the restore fails: left stopped is the worst outcome. Both errors
    are reported and the saved state stays, so `--rollback` can be run again."""
    steps = Steps(fail={"restore"}, saved=True)
    result = run_rollback(plan(), steps, log=quiet)
    assert not result.ok and result.state == "broken" and "restore broke" in result.error
    assert steps.calls == ["load", "drain", "stop", "restore", "start"]
    assert "clear" not in steps.calls
    steps = Steps(fail={"restore", "start"}, saved=True)
    result = run_rollback(plan(), steps, log=quiet)
    assert result.state == "broken" and "restore broke" in result.error and "start broke" in result.error


def test_rollback_restores_and_forgets_the_state_only_when_healthy():
    steps = Steps(saved=True)
    result = run_rollback(plan(), steps, log=quiet)
    assert result.ok and result.state == "rolled_back"
    assert steps.calls == ["load", "drain", "stop", "restore", "start", "verify:root", "clear"]
    steps = Steps(saved=True, healthy=[False])
    result = run_rollback(plan(), steps, log=quiet)
    assert result.state == "broken" and "clear" not in steps.calls and "saved state is kept" in result.error


# ---- the real steps against a fake host -----------------------------------------------------


class Host(FakeHost):
    """FakeHost that records every command and answers the ones a switch runs."""

    def __init__(self) -> None:
        super().__init__()
        self.ran: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.euid_value = 0
        self.commands["systemctl show"] = (0, "LoadState=loaded\nActiveState=active\nUser=\n")
        for cmd in ("systemctl stop", "systemctl start", "systemctl daemon-reload", "chown -R", "systemctl disable"):
            self.commands[cmd] = (0, "")
        # the confinement check, run as the service user through agentos-runas
        self.commands["runuser -u"] = (0, json.dumps({"ok": True, "reason": "the brain (uid 991) cannot reach "
                                                            "any of 23 secrets"}))

    def run(self, argv, timeout=10.0, env=None):
        self.ran.append(list(argv))
        self.envs.append(env)
        if argv[0] == "bash":
            return 0, "==> Installing agentosd.service (hardened) as agentos\n"
        return super().run(argv, timeout=timeout, env=env)


@pytest.fixture
def node(tmp_path):
    cfg = make_home(tmp_path)
    prefix = tmp_path / "opt"
    for f in (".venv/bin/openajar", "deploy/install-node.sh", "deploy/agentosd-hardened.service",
              "deploy/agentos-runas.service"):
        (prefix / f).parent.mkdir(parents=True, exist_ok=True)
        (prefix / f).write_text("")
    etc = tmp_path / "etc"
    (etc / "systemd" / "system").mkdir(parents=True)
    (etc / "logrotate.d").mkdir()
    (etc / "systemd" / "system" / "agentosd.service").write_text("[Service]\nExecStart=openajar node serve\n")
    (etc / "logrotate.d" / "agentos").write_text("su root root\n")
    me = SimpleNamespace(pw_uid=os.getuid(), pw_dir="/var/lib/agentos", pw_shell="/usr/sbin/nologin")
    brain = SimpleNamespace(pw_uid=os.getuid() + 1, pw_dir="/var/lib/agentos-brain", pw_shell="/usr/sbin/nologin")
    users = {"agentos": me, "root": me, "agentos-brain": brain}
    groups = {"agentos-brain": {"agentos-brain"}}
    host = Host()
    steps = SystemSteps(host, log=quiet, users=users.get, sleep=lambda s: None,
                        groups=lambda u: groups.get(u, set()))
    return SimpleNamespace(plan=SwitchPlan(prefix=prefix, home=cfg.home, etc=etc, health_timeout=0),
                           host=host, steps=steps, users=users, cfg=cfg, groups=groups)


def brains_settings(node, **kw) -> None:
    from openajar.brains.settings import BrainsSettings, save_settings

    save_settings(node.cfg.home, BrainsSettings(**kw))


def test_preflight_passes_on_a_root_node(node):
    node.steps.preflight(node.plan)


def test_preflight_passes_with_a_brain_user_and_without_one_yet(node):
    """The brain user no longer blocks the switch: agentos-runas starts brains as it. One that does
    not exist yet is fine too (the installer creates it)."""
    node.steps.preflight(node.plan)
    del node.users["agentos-brain"]
    node.steps.preflight(node.plan)


def _home_group(n) -> str:
    """A group that owns something in OPENAJAR_HOME (here: the test user's own group)."""
    import grp

    gid = os.lstat(n.plan.home / "node.toml").st_gid
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


@pytest.mark.parametrize("change, message", [
    (lambda n: brains_settings(n, run_as=""), "no user of their own"),
    (lambda n: brains_settings(n, confinement="off"), "confinement check is off"),
    (lambda n: brains_settings(n, run_as="agentos"), "cannot be agentos"),
    (lambda n: brains_settings(n, run_as="root"), "cannot be root"),
    (lambda n: n.users.update({"agentos-brain": SimpleNamespace(pw_uid=0, pw_dir="/", pw_shell="/bin/false")}),
     "uid 0"),
    (lambda n: n.users.update({"agentos-brain": SimpleNamespace(pw_uid=1001, pw_dir="/home/b", pw_shell="/bin/sh")}),
     "login account"),
    (lambda n: n.groups["agentos-brain"].add("agentos"), "in the agentos group"),
    (lambda n: n.groups["agentos-brain"].add("docker"), "in the group docker"),
    (lambda n: n.groups["agentos-brain"].update({"adm", "systemd-journal"}), "groups adm, systemd-journal"),
    (lambda n: n.groups["agentos-brain"].add(_home_group(n)), "owning files in"),
    (lambda n: (n.plan.prefix / "deploy" / "agentos-runas.service").unlink(), "predates agentos-runas"),
])
def test_preflight_refuses_brains_that_could_end_up_unconfined(node, change, message):
    change(node)
    with pytest.raises(SwitchError, match=message):
        node.steps.preflight(node.plan)


@pytest.mark.parametrize("change, message", [
    (lambda n: setattr(n.host, "euid_value", 1000), "needs root"),
    (lambda n: n.host.commands.update({"systemctl show": (0, "LoadState=loaded\nUser=agentos\n")}),
     "already runs as agentos"),
    (lambda n: n.host.commands.update({"systemctl show": (0, "LoadState=not-found\n")}), "not installed"),
    (lambda n: (n.plan.prefix / "deploy" / "agentosd-hardened.service").unlink(), "agentosd-hardened.service"),
    (lambda n: setattr(n.plan, "user", "root"), "cannot be root"),
])
def test_preflight_refuses_what_would_break_or_get_worse(node, change, message):
    change(node)
    with pytest.raises(SwitchError, match=message):
        node.steps.preflight(node.plan)
    assert not any(argv[:2] in (["systemctl", "stop"], ["chown", "-R"]) or argv[0] == "bash"
                   for argv in node.host.ran)


@pytest.mark.parametrize("home, shell, refused", [
    ("/var/lib/agentos", "/usr/sbin/nologin", False),  # what the installer creates
    ("/var/lib/agentos", "/bin/false", False),
    ("/home/ada", "/usr/sbin/nologin", True),  # a person's home
    ("/var/lib/agentos", "/bin/bash", True),  # a login shell
])
def test_preflight_refuses_an_existing_login_account_as_the_service_user(node, home, shell, refused):
    """The vault would be handed to a person's account: an existing user with a home under /home or a login
    shell is refused, and a service account the installer made is not."""
    node.users["agentos"] = SimpleNamespace(pw_uid=1001, pw_dir=home, pw_shell=shell)
    if refused:
        with pytest.raises(SwitchError, match="login account"):
            node.steps.preflight(node.plan)
    else:
        node.steps.preflight(node.plan)


def test_preflight_refuses_a_home_the_hardened_unit_hides(node):
    node.plan.home = Path("/home/someone/openajar")
    with pytest.raises(SwitchError, match="ProtectHome"):
        node.steps.preflight(node.plan)


def test_preflight_refuses_isolated_env_lanes(node):
    from openajar.env.models import EnvLane, IsolationProfile

    lane = EnvLane(id="dev/box", profile=IsolationProfile())
    db = sqlite3.connect(node.cfg.home / "env.db")
    db.execute("CREATE TABLE lanes (id TEXT PRIMARY KEY, spec TEXT NOT NULL)")
    db.execute("INSERT INTO lanes VALUES (?, ?)", (lane.id, lane.model_dump_json()))
    db.commit()
    db.close()
    assert lane.isolated
    with pytest.raises(SwitchError, match="dev/box"):
        node.steps.preflight(node.plan)


def test_preflight_refuses_when_the_env_lanes_cannot_be_read(node):
    """A database it cannot read, or a lane spec it cannot parse, could hide an isolated lane: refuse."""
    (node.cfg.home / "env.db").write_bytes(b"not a database" * 100)
    with pytest.raises(SwitchError, match="cannot read"):
        node.steps.preflight(node.plan)


def test_preflight_refuses_a_lane_spec_it_cannot_parse(node):
    db = sqlite3.connect(node.cfg.home / "env.db")
    db.execute("CREATE TABLE lanes (id TEXT PRIMARY KEY, spec TEXT NOT NULL)")
    db.execute("INSERT INTO lanes VALUES (?, ?)", ("dev/broken", '{"id": '))
    db.commit()
    db.close()
    with pytest.raises(SwitchError, match="dev/broken"):
        node.steps.preflight(node.plan)


def test_a_rollback_refuses_before_it_stops_anything_when_the_unit_file_is_gone(node):
    """A rollback puts the saved unit back; it does not create one, so it refuses before agentosd is touched."""
    node.steps.save_state(node.plan)
    node.plan.unit_path.unlink()
    result = run_rollback(node.plan, node.steps, log=quiet)
    assert result.state == "unchanged" and "is missing" in result.error
    assert not any(argv[:2] == ["systemctl", "stop"] for argv in node.host.ran)
    assert not node.plan.unit_path.exists() and node.plan.state_path.exists()


def test_restore_never_creates_a_missing_unit_file(node):
    state = node.steps.save_state(node.plan)
    node.plan.unit_path.unlink()
    with pytest.raises(SwitchError, match="is missing"):
        node.steps.restore(node.plan, state)
    assert not node.plan.unit_path.exists()


def test_save_state_keeps_the_unit_logrotate_and_owner_privately(node):
    state = node.steps.save_state(node.plan)
    path = node.plan.state_path
    assert path.stat().st_mode & 0o777 == 0o600
    saved = json.loads(path.read_text())
    assert saved == state and saved["unit"].startswith("[Service]") and saved["logrotate"] == "su root root\n"
    st = node.cfg.home.stat()
    assert saved["owner"] == f"{st.st_uid}:{st.st_gid}" and saved["user"] == "root"
    with pytest.raises(SwitchError, match="run --rollback first"):
        node.steps.save_state(node.plan)


@pytest.mark.parametrize("content", ["{not json", "[]", '{"user": "root"}'])
def test_a_corrupt_rollback_state_is_a_clear_refusal_not_a_traceback(node, content):
    node.plan.state_path.write_text(content)
    result = run_rollback(node.plan, node.steps, log=quiet)
    assert result.state == "unchanged" and "rollback state" in result.error
    assert not any(argv[:2] == ["systemctl", "stop"] for argv in node.host.ran)
    assert node.plan.state_path.read_text() == content  # kept: nothing was changed


def test_save_state_creates_the_file_private_from_the_start(node, monkeypatch):
    """The unit and the owner are never in a file other users can read, not even for a moment: the file is
    created 0600 (a leftover from a crash is replaced, its old mode is not kept)."""
    stale = node.plan.state_path.with_suffix(".tmp")
    stale.write_text("left by a crash")
    stale.chmod(0o644)
    created: list[int] = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *args, **kwargs):
        if os.fspath(path) == str(stale):
            created.append(mode)
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(service_user.os, "open", spy)
    node.steps.save_state(node.plan)
    assert created == [0o600]
    assert node.plan.state_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(node.plan.state_path.read_text())["unit"].startswith("[Service]")


def test_install_runs_the_prefixes_installer_with_the_hardened_unit(node):
    with pytest.raises(SwitchError, match="no brain user"):
        node.steps.install(node.plan)  # preflight names it
    brains_settings(node, run_as="brainz")
    node.users["brainz"] = node.users["agentos-brain"]
    node.steps.preflight(node.plan)
    node.steps.install(node.plan)
    argv, env = node.host.ran[-1], node.host.envs[-1]
    # the installer creates, and agentos-runas serves, the user brains settings name: not its default
    assert env["OPENAJAR_BRAIN_USER"] == "brainz"
    assert argv[:2] == ["bash", str(node.plan.prefix / "deploy" / "install-node.sh")]
    assert {"--systemd", "--no-restart", "--skip-apt", "--hardened"} <= set(argv)
    assert argv[argv.index("--user") + 1] == "agentos"
    assert env["INSTALL_PREFIX"] == str(node.plan.prefix) and env["OPENAJAR_HOME"] == str(node.plan.home)
    assert env.get("HOME") == os.environ.get("HOME")  # root's uv and Camoufox are found through it


def test_restore_puts_the_unit_and_owner_back(node):
    state = node.steps.save_state(node.plan)
    node.plan.unit_path.write_text("[Service]\nUser=agentos\n")
    node.plan.logrotate_path.write_text("su agentos agentos\n")
    node.steps.restore(node.plan, state)
    assert node.plan.unit_path.read_text() == state["unit"]
    assert node.plan.logrotate_path.read_text() == "su root root\n"
    assert ["chown", "-R", state["owner"], str(node.plan.home)] in node.host.ran
    assert node.host.ran[-1] == ["systemctl", "daemon-reload"]


def test_verify_wants_the_user_mcp_and_a_private_home(node):
    node.host.commands["systemctl show"] = (0, "LoadState=loaded\nUser=agentos\n")
    ok, detail = node.steps.verify(node.plan, "agentos")
    assert ok, detail
    ok, detail = node.steps.verify(node.plan, "root")
    assert not ok and "runs as agentos, not root" in detail
    node.users["agentos"] = SimpleNamespace(pw_uid=os.getuid() + 1)
    ok, detail = node.steps.verify(node.plan, "agentos")
    assert not ok and "not agentos" in detail
    node.users["agentos"] = SimpleNamespace(pw_uid=os.getuid())
    node.host.http_answers["anon"] = 200  # MCP without a token must be refused
    ok, _ = node.steps.verify(node.plan, "agentos")
    assert not ok
    node.host.commands["systemctl is-active"] = (3, "failed\n")
    ok, detail = node.steps.verify(node.plan, "agentos")
    assert not ok and "failed" in detail


def test_verify_wants_the_helper_up_and_brains_confined(node):
    node.host.commands["systemctl show"] = (0, "LoadState=loaded\nUser=agentos\n")
    ok, detail = node.steps.verify(node.plan, "agentos")
    assert ok and "brains confined" in detail, detail
    argv = next(a for a in node.host.ran if a[0] == "runuser")
    assert argv[:4] == ["runuser", "-u", "agentos", "--"] and argv[-3:] == ["brains", "confinement", "--json"]
    node.host.commands["runuser -u"] = (1, json.dumps({"ok": False, "reason": "the brain can reach node.toml"}))
    ok, detail = node.steps.verify(node.plan, "agentos")
    assert not ok and "not confined: the brain can reach node.toml" in detail
    node.host.commands["runuser -u"] = (1, "Traceback: boom")
    ok, detail = node.steps.verify(node.plan, "agentos")
    assert not ok and "did not answer" in detail
    node.host.commands["runuser -u"] = (0, json.dumps({"ok": True, "reason": "fine"}))
    real = node.host.run
    node.host.run = lambda argv, **kw: (3, "inactive\n") if argv[-1] == "agentos-runas.service" else real(argv, **kw)
    ok, detail = node.steps.verify(node.plan, "agentos")
    assert not ok and "agentos-runas.service is inactive" in detail
    node.host.run = real
    node.host.commands["systemctl show"] = (0, "LoadState=loaded\nUser=\n")
    node.host.ran.clear()
    ok, _ = node.steps.verify(node.plan, "root")  # back on root: no helper, no check through it
    assert ok and not any(a[0] == "runuser" for a in node.host.ran)


def test_a_rollback_removes_the_helper_the_switch_installed(node):
    state = node.steps.save_state(node.plan)
    assert state["runas_unit"] is None
    node.plan.runas_path.write_text("[Service]\nExecStart=openajar.runas serve\n")
    node.steps.restore(node.plan, state)
    assert not node.plan.runas_path.exists()
    assert ["systemctl", "disable", "--now", "agentos-runas.service"] in node.host.ran


def test_a_rollback_keeps_a_helper_that_would_not_stop_and_says_so(node):
    state = node.steps.save_state(node.plan)
    node.plan.runas_path.write_text("[Service]\nExecStart=runas.py serve\n")
    node.host.commands["systemctl disable"] = (1, "Failed to stop agentos-runas.service: timed out")
    with pytest.raises(SwitchError, match="would not stop .*left installed"):
        node.steps.restore(node.plan, state)
    assert node.plan.runas_path.exists()  # systemd can still stop it
    assert node.plan.unit_path.read_text() == state["unit"] and node.host.ran[-1] == ["systemctl", "daemon-reload"]


def test_verify_wants_the_helper_to_serve_the_brain_user_brains_settings_name(node):
    node.host.commands["systemctl show"] = (0, "LoadState=loaded\nUser=agentos\n")
    node.plan.brain_user = "agentos-brain"
    node.plan.runas_path.write_text("ExecStart=runas.py serve --caller openajar --brain-user someone-else\n")
    ok, detail = node.steps.verify(node.plan, "agentos")
    assert not ok and "starts brains as someone-else, not agentos-brain" in detail
    node.plan.runas_path.write_text("ExecStart=runas.py serve --caller openajar --brain-user agentos-brain\n")
    ok, detail = node.steps.verify(node.plan, "agentos")
    assert ok, detail


def test_a_rollback_puts_back_a_helper_that_was_there_before(node):
    node.plan.runas_path.write_text("[Service]\n# the old one\n")
    state = node.steps.save_state(node.plan)
    node.plan.runas_path.write_text("[Service]\n# the new one\n")
    node.steps.restore(node.plan, state)
    assert node.plan.runas_path.read_text() == "[Service]\n# the old one\n"
    assert not any(a[:2] == ["systemctl", "disable"] for a in node.host.ran)


def test_an_old_rollback_state_leaves_the_helper_alone(node):
    state = node.steps.save_state(node.plan)
    del state["runas_unit"]
    node.plan.runas_path.write_text("[Service]\n")
    node.steps.restore(node.plan, state)
    assert node.plan.runas_path.exists()


def test_a_switch_and_a_rollback_end_to_end_on_the_fake_host(node, monkeypatch):
    """The whole command: the unit changes hands, the state file outlives the switch, and the
    rollback puts the original unit back and forgets the state."""
    original = node.plan.unit_path.read_text()

    def install(plan):
        plan.unit_path.write_text("[Service]\nUser=agentos\n")
        plan.runas_path.write_text(f"[Service]\nExecStart=runas.py serve --caller openajar --brain-user {plan.brain_user}\n")
        node.host.commands["systemctl show"] = (0, "LoadState=loaded\nUser=agentos\n")

    monkeypatch.setattr(node.steps, "install", install)
    result = run_switch(node.plan, node.steps, log=quiet)
    assert result.ok and result.state == "switched", result.error
    assert node.plan.state_path.exists() and "User=agentos" in node.plan.unit_path.read_text()

    real_restore = node.steps.restore

    def restore(plan, state):
        real_restore(plan, state)
        node.host.commands["systemctl show"] = (0, "LoadState=loaded\nUser=\n")

    monkeypatch.setattr(node.steps, "restore", restore)
    result = run_rollback(node.plan, node.steps, log=quiet)
    assert result.ok and result.state == "rolled_back", result.error
    assert node.plan.unit_path.read_text() == original and not node.plan.state_path.exists()


def test_the_cli_command_is_mounted_and_asks_first(tmp_path, monkeypatch):
    from openajar.cli import app

    cfg = make_home(tmp_path)
    monkeypatch.setenv("OPENAJAR_HOME", str(cfg.home))
    calls = []
    monkeypatch.setattr(service_user, "run_switch", lambda plan, steps, log: calls.append(plan) or
                        service_user.SwitchResult(ok=True, state="switched"))
    result = CliRunner().invoke(app, ["migrate-service-user"], input="n\n")
    assert result.exit_code == 1 and not calls  # declined: nothing ran
    result = CliRunner().invoke(app, ["migrate-service-user", "--yes", "--user", "agentos"])
    assert result.exit_code == 0, result.output
    assert calls[0].user == "agentos" and calls[0].home == cfg.home and "--rollback" in result.output
