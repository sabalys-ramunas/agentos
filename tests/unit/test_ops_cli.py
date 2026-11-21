"""The ops CLI: mounting under the root command, doctor --json, backups, migrate, refresh-fingerprint."""

from __future__ import annotations

import json

import pytest
import typer
from test_ops_support import PASSPHRASE, PROXY_PASS, TOKEN, FakeHost, make_home
from typer.testing import CliRunner

from openajar.ops import backup, cli, cli_common, doctor

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    cfg = make_home(tmp_path, lanes={"personal/reddit": 150, "work/site": None})
    monkeypatch.setenv("OPENAJAR_HOME", str(cfg.home))
    real_create = backup.create_backup
    monkeypatch.setattr(backup, "create_backup", lambda *a, **kw: real_create(*a, **{**kw, "log_n": 10}))
    return cfg


@pytest.fixture
def pwfile(tmp_path):
    p = tmp_path / "pw"
    p.write_text(PASSPHRASE + "\n")
    p.chmod(0o600)
    return p


def test_mount_adds_the_documented_names():
    root, lane = typer.Typer(), typer.Typer()

    @root.callback()
    def main() -> None:
        pass

    root.add_typer(lane, name="lane")
    cli.mount(root, lane_group=lane)
    out = runner.invoke(root, ["--help"]).output
    for name in ("doctor", "backup", "migrate", "upgrade", "health"):
        assert name in out
    assert "refresh-fingerprint" in runner.invoke(root, ["lane", "--help"]).output
    assert "restore" in runner.invoke(root, ["backup", "--help"]).output
    r = runner.invoke(root, ["lane", "refresh-fingerprint", "a/b"])
    assert "pass --keep-device" in r.output
    assert "--offline" in runner.invoke(root, ["doctor", "--help"]).output
    assert "--no-service" in runner.invoke(root, ["upgrade", "--help"]).output


def test_mount_table_entries_are_typer_apps():
    for attr in ("doctor_app", "backup_app", "migrate_app", "upgrade_app", "health_app", "lane_app"):
        assert isinstance(getattr(cli, attr), typer.Typer), attr


def test_doctor_json_exit_code_and_no_secrets(home, monkeypatch):
    real = doctor.DoctorContext
    monkeypatch.setattr(cli.doctor, "DoctorContext", lambda **kw: real(host=FakeHost(), **kw))
    r = runner.invoke(cli.app, ["doctor", "--json"])
    data = json.loads(r.output)
    assert r.exit_code == (0 if data["ok"] else 1)
    assert TOKEN not in r.output and PROXY_PASS not in r.output
    ids = {c["id"] for c in data["checks"]}
    assert {"config", "camoufox", "lanes", "proxy.lt1", "permissions"} <= ids  # per lane: openajar lane check
    r = runner.invoke(cli.app, ["doctor", "--only", "disk,clock"])
    assert r.exit_code == 0 and "Clock sync" in r.output and "fix:" not in r.output
    r = runner.invoke(cli.app, ["doctor", "--only", "bogus"])
    assert r.exit_code == 1 and "unknown check" in r.output


def test_backup_create_verify_restore_through_the_cli(home, pwfile, tmp_path):
    r = runner.invoke(cli.app, ["backup", "create", "--passphrase-file", str(pwfile), "--keep", "2"])
    assert r.exit_code == 0, r.output
    assert "Backup written:" in r.output and "personal/reddit (cold)" in r.output
    assert "on the node's own disk" in r.output
    (archive,) = backup.list_archives(home.home / "backups")
    r = runner.invoke(cli.app, ["backup", "verify", str(archive.path), "--passphrase-file", str(pwfile), "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["ok"] is True
    r = runner.invoke(cli.app, ["backup", "list", "--json"])
    assert json.loads(r.output)[0]["node"] == "test-node"
    target = tmp_path / "new-home"
    r = runner.invoke(cli.app, ["backup", "restore", str(archive.path), "--to", str(target),
                                "--passphrase-file", str(pwfile)])
    assert r.exit_code == 0, r.output
    assert "every directory 700, every file 600" in r.output
    r = runner.invoke(cli.app, ["backup", "restore", str(archive.path), "--to", str(target),
                                "--passphrase-file", str(pwfile)])
    assert r.exit_code == 1 and "not empty" in r.output
    assert "—" not in r.output


def test_passphrase_file_rules(home, tmp_path, monkeypatch):
    loose = tmp_path / "loose"
    loose.write_text(PASSPHRASE)
    loose.chmod(0o644)
    r = runner.invoke(cli.app, ["backup", "create", "--passphrase-file", str(loose)])
    assert r.exit_code == 1 and "readable by others" in r.output
    r = runner.invoke(cli.app, ["backup", "create"])  # no terminal under CliRunner
    assert r.exit_code == 1 and "--passphrase-file" in r.output
    short = tmp_path / "short"
    short.write_text("tiny")
    short.chmod(0o600)
    monkeypatch.setenv(cli_common.PASSPHRASE_ENV, str(short))
    r = runner.invoke(cli.app, ["backup", "create"])
    assert r.exit_code == 1 and "at least" in r.output


def test_wrong_passphrase_through_the_cli(home, pwfile, tmp_path):
    runner.invoke(cli.app, ["backup", "create", "--passphrase-file", str(pwfile)])
    (archive,) = backup.list_archives(home.home / "backups")
    other = tmp_path / "other"
    other.write_text("another long passphrase")
    other.chmod(0o600)
    r = runner.invoke(cli.app, ["backup", "verify", str(archive.path), "--passphrase-file", str(other)])
    assert r.exit_code == 1 and "bad_archive: wrong passphrase" in r.output


def test_prune_through_the_cli(home, pwfile):
    r = runner.invoke(cli.app, ["backup", "prune", "--keep", "1"])
    assert r.exit_code == 0 and "Nothing to remove" in r.output


def test_refresh_fingerprint(home):
    r = runner.invoke(cli.app, ["refresh-fingerprint", "personal/reddit", "--version", "158"])
    assert r.exit_code == 1 and "pass --keep-device" in r.output
    r = runner.invoke(cli.app, ["refresh-fingerprint", "personal/reddit", "--keep-device", "--version", "158",
                                "--dry-run"])
    assert r.exit_code == 0 and "Would change personal/reddit: Firefox 150 -> 158" in r.output
    r = runner.invoke(cli.app, ["refresh-fingerprint", "personal/reddit", "--keep-device", "--version", "158",
                                "--json"])
    data = json.loads(r.output)
    assert data["changed_keys"] == ["navigator.userAgent"] and data["old_version"] == 150
    fp = json.loads(home.fingerprint_path("personal/reddit").read_text())
    assert "Firefox/158.0" in fp["navigator.userAgent"]
    r = runner.invoke(cli.app, ["refresh-fingerprint", "work/site", "--keep-device", "--version", "158"])
    assert r.exit_code == 1 and "pinned on first launch" in r.output
    r = runner.invoke(cli.app, ["refresh-fingerprint", "Bad Lane", "--keep-device"])
    assert r.exit_code == 1


def test_migrate_status_and_run(home):
    r = runner.invoke(cli.app, ["migrate", "status", "--json"])
    status = json.loads(r.output)
    assert status["databases"][0]["pending"] == ["lanes table"]
    assert {ln["lane"] for ln in status["lanes"]} >= {"personal/reddit"} and status["lane_format"] == 1
    assert all(not ln["stamped"] and ln["pending"] for ln in status["lanes"])
    r = runner.invoke(cli.app, ["migrate", "status"])
    assert "lane       personal/reddit" in r.output and "not stamped yet" in r.output
    r = runner.invoke(cli.app, ["migrate", "run", "--dry-run"])
    assert "Would apply" in r.output and "lane personal/reddit: baseline" in r.output
    r = runner.invoke(cli.app, ["migrate", "run", "--json"])
    data = json.loads(r.output)
    assert data["ok"] and data["applied"][0]["package"] == "node" and data["backup_dir"]
    assert {a["lane"] for a in data["lanes"]["applied"]} >= {"personal/reddit"} and data["lanes_backup_dir"]
    assert data["backup_dir"] != data["lanes_backup_dir"]
    r = runner.invoke(cli.app, ["migrate", "run"])
    assert "Nothing to migrate" in r.output
    r = runner.invoke(cli.app, ["migrate", "status"])
    assert "not stamped" not in r.output and "current" in r.output


def test_health_check_without_a_daemon(home, monkeypatch):
    from openajar.ops import health

    real = health.HealthMonitor
    monkeypatch.setattr(health, "HealthMonitor", lambda cfg, **kw: real(cfg, host=FakeHost(), **kw))
    r = runner.invoke(cli.app, ["health", "check", "--offline", "--json"])
    data = json.loads(r.output)
    assert data["note"].startswith("run without the daemon")
    assert any(f["kind"] == "drift" and f["level"] == "warn" for f in data["findings"])
    r = runner.invoke(cli.app, ["health", "status"])
    assert r.exit_code == 1 and "ops socket does not answer" in r.output


def test_commands_need_an_initialized_home(tmp_path, monkeypatch, pwfile):
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "nothing"))
    r = runner.invoke(cli.app, ["backup", "create", "--passphrase-file", str(pwfile)])
    assert r.exit_code == 1 and "openajar node init" in r.output


def test_refresh_fingerprint_of_a_device_lane_lists_every_lane_and_needs_a_yes(home):
    from openajar.core.models import DeviceSpec
    from openajar.node.store import LaneStore

    store = LaneStore(home.db_path)
    store.add_device(DeviceSpec(name="me-pc", proxy_label="lt1"))
    spec = store.require("personal/reddit").spec
    store.update_spec(spec.model_copy(update={"device": "me-pc"}))
    pin = home.device_pin_path("me-pc")
    pin.parent.mkdir(parents=True)
    pin.write_bytes(home.fingerprint_path("personal/reddit").read_bytes())
    r = runner.invoke(cli.app, ["refresh-fingerprint", "personal/reddit", "--keep-device", "--version", "158"])
    assert r.exit_code == 1 and "device me-pc" in r.output and "personal/reddit" in r.output and "--yes" in r.output
    assert "Firefox/150.0" in json.loads(pin.read_text())["navigator.userAgent"]  # nothing changed
    r = runner.invoke(cli.app, ["refresh-fingerprint", "personal/reddit", "--keep-device", "--version", "158",
                                "--yes"])
    assert r.exit_code == 0, r.output
    assert "Firefox/158.0" in json.loads(pin.read_text())["navigator.userAgent"]
    assert home.fingerprint_path("personal/reddit").read_bytes() == pin.read_bytes()
