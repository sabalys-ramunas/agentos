"""openajar upgrade: the order of steps, and the rollback path when the new version is unhealthy."""

from __future__ import annotations

from pathlib import Path

from openajar.ops import upgrade
from openajar.ops.upgrade import UpgradeError, UpgradePlan, run_upgrade


class Steps:
    def __init__(self, *, fail: set[str] = frozenset(), healthy: list[bool] | None = None,
                 migrate_backup: str | None = None, lanes_backup: str | None = None):
        self.fail = set(fail)
        self.health = list(healthy if healthy is not None else [True])
        self.calls: list[str] = []
        self.migrate_backup = migrate_backup
        self.lanes_backup = lanes_backup

    def _do(self, name: str, value=None):
        self.calls.append(name)
        if name in self.fail:
            raise UpgradeError(f"{name} broke", backup_dir=self.migrate_backup if name == "migrate" else None,
                               lanes_backup_dir=self.lanes_backup if name == "migrate" else None)
        return value

    def preflight(self, plan): return self._do("preflight")
    def snapshot_code(self, plan): return self._do("snapshot", Path("/opt/agentos.rollback"))
    def drain(self, plan): return self._do("drain", {"closed": ["personal/reddit"]})
    def stop(self, plan): return self._do("stop")
    def install(self, plan): return self._do("install")
    def migrate(self, plan): return self._do("migrate", {"ok": True, "backup_dir": self.migrate_backup, "applied": [],
                                                         "lanes_backup_dir": self.lanes_backup})
    def start(self, plan): return self._do("start")

    def healthy(self, plan):
        self.calls.append("healthy")
        ok = self.health.pop(0) if self.health else True
        return ok, "fine" if ok else "MCP does not answer"

    def restore_code(self, plan, snapshot): return self._do("restore_code")
    def restore_databases(self, plan, backup_dir): return self._do("restore_databases", ["openajar.db"])
    def restore_lanes(self, plan, backup_dir): return self._do("restore_lanes", ["personal/reddit"])


def plan(**kw) -> UpgradePlan:
    return UpgradePlan(source=Path("/src"), prefix=Path("/opt/agentos"), home=Path("/var/lib/agentos"), **kw)


def test_happy_path_order():
    steps = Steps()
    result = run_upgrade(plan(), steps, log=lambda m: None)
    assert result.ok and result.state == "upgraded" and not result.rolled_back
    assert steps.calls == ["preflight", "snapshot", "drain", "stop", "install", "migrate", "start", "healthy"]
    assert [s["step"] for s in result.steps][-1] == "health"


def test_dev_install_skips_the_service():
    steps = Steps()
    result = run_upgrade(plan(service=False), steps, log=lambda m: None)
    assert result.ok
    assert steps.calls == ["preflight", "snapshot", "install", "migrate", "healthy"]


def test_unhealthy_new_version_rolls_back_code_and_databases(tmp_path):
    steps = Steps(healthy=[False, True], migrate_backup=str(tmp_path))
    result = run_upgrade(plan(), steps, log=lambda m: None)
    assert not result.ok and result.rolled_back and result.state == "rolled_back"
    assert steps.calls == ["preflight", "snapshot", "drain", "stop", "install", "migrate", "start", "healthy",
                           "stop", "restore_code", "restore_databases", "start", "healthy"]
    assert "health check" in result.error


def test_rollback_puts_the_lane_files_back(tmp_path):
    (tmp_path / "db").mkdir()
    (tmp_path / "lanes").mkdir()
    steps = Steps(healthy=[False, True], migrate_backup=str(tmp_path / "db"), lanes_backup=str(tmp_path / "lanes"))
    result = run_upgrade(plan(), steps, log=lambda m: None)
    assert result.state == "rolled_back"
    assert steps.calls[-5:] == ["restore_code", "restore_databases", "restore_lanes", "start", "healthy"]
    # a migrate that failed on a lane still has its lane backup restored
    steps = Steps(fail={"migrate"}, migrate_backup=str(tmp_path / "db"), lanes_backup=str(tmp_path / "lanes"))
    run_upgrade(plan(), steps, log=lambda m: None)
    assert "restore_lanes" in steps.calls


def test_failed_install_rolls_back_without_databases():
    steps = Steps(fail={"install"})
    result = run_upgrade(plan(), steps, log=lambda m: None)
    assert result.state == "rolled_back"
    assert "restore_databases" not in steps.calls
    assert steps.calls[-3:] == ["restore_code", "start", "healthy"]


def test_failed_migration_restores_the_backup_it_made(tmp_path):
    steps = Steps(fail={"migrate"}, migrate_backup=str(tmp_path))
    result = run_upgrade(plan(), steps, log=lambda m: None)
    assert result.state == "rolled_back"
    assert "restore_databases" in steps.calls
    assert steps.calls.index("restore_code") < steps.calls.index("restore_databases")


def test_preflight_or_snapshot_failure_changes_nothing():
    for broken in ("preflight", "snapshot"):
        steps = Steps(fail={broken})
        result = run_upgrade(plan(), steps, log=lambda m: None)
        assert result.state == "unchanged" and not result.rolled_back
        assert "stop" not in steps.calls and "install" not in steps.calls


def test_drain_failure_is_not_fatal():
    steps = Steps(fail={"drain"})
    result = run_upgrade(plan(), steps, log=lambda m: None)
    assert result.ok
    assert next(s for s in result.steps if s["step"] == "drain")["status"] == "skipped"


def test_stop_failure_aborts_and_starts_again():
    steps = Steps(fail={"stop"})
    result = run_upgrade(plan(), steps, log=lambda m: None)
    assert result.state == "unchanged" and "could not stop" in result.error
    assert steps.calls[-1] == "start" and "install" not in steps.calls


def test_rollback_that_fails_its_health_check_reports_broken():
    steps = Steps(healthy=[False, False])
    result = run_upgrade(plan(), steps, log=lambda m: None)
    assert result.state == "broken" and not result.ok


def test_rollback_that_cannot_restore_reports_broken():
    steps = Steps(healthy=[False], fail={"restore_code"})
    result = run_upgrade(plan(), steps, log=lambda m: None)
    assert result.state == "broken" and "rollback failed" in result.error


def test_installer_arguments(monkeypatch):
    seen = {}

    def fake_run(self, argv, *, env=None, timeout=1800):
        seen["argv"], seen["env"] = argv, env
        return "==> Done\n"

    monkeypatch.setattr(upgrade.SystemSteps, "_run", fake_run)
    steps = upgrade.SystemSteps(log=lambda m: None)
    steps.install(plan(user="agentos", hardened=True))
    assert seen["argv"][1].endswith("deploy/install-node.sh")
    assert seen["argv"][2:] == ["--no-restart", "--systemd", "--user", "agentos", "--hardened"]
    assert seen["env"] == {"INSTALL_PREFIX": "/opt/agentos", "OPENAJAR_HOME": "/var/lib/agentos"}


def test_rsync_excludes_keep_managed_runtimes_and_an_inner_home():
    p = UpgradePlan(source=Path("/src"), prefix=Path("/root/dev"), home=Path("/root/dev/.agentos-home"))
    ex = upgrade._rsync_excludes(p)
    assert ex == ["--exclude", "/runtime/", "--exclude", "/node/", "--exclude", "/.agentos-home/"]
    assert p.snapshot_dir == Path("/root/dev.rollback")
