"""agentos doctor: every check against a fake host, the report formats, and no secrets anywhere."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from test_ops_support import PROXY_PASS, PROXY_USER, TOKEN, FakeHost, GiB, fingerprint, make_home

from agentos.ops import doctor
from agentos.ops.doctor import FAIL, PASS, SKIP, WARN, DoctorContext


def ctx_for(tmp_path: Path, host: FakeHost | None = None, **kw) -> DoctorContext:
    cfg = make_home(tmp_path, **kw)
    return DoctorContext(home=cfg.home, host=host or FakeHost(), prefix=Path("/opt/agentos"))


def by_id(results, cid):
    return next(r for r in results if r.id == cid)


def test_config_missing_and_ok(tmp_path):
    home = tmp_path / "empty"
    home.mkdir()
    r = doctor.check_config(DoctorContext(home=home, host=FakeHost()))[0]
    assert r.status == FAIL and "agentos node init" in r.fix
    assert doctor.check_config(ctx_for(tmp_path))[0].status == PASS


def test_os_checks(tmp_path):
    ctx = ctx_for(tmp_path)
    assert doctor.check_os(ctx)[0].status == PASS
    ctx.host.platform_info.update(system="Darwin", machine="arm64")
    assert doctor.check_os(ctx)[0].status == WARN
    ctx.host.platform_info.update(system="Linux", machine="x86_64", version_id="22.04")
    assert "only Ubuntu 24.04" in doctor.check_os(ctx)[0].detail
    ctx.host.platform_info.update(version_id="24.04", machine="riscv64")
    assert doctor.check_os(ctx)[0].status == WARN


def test_python_and_venv(tmp_path):
    ctx = ctx_for(tmp_path)
    assert doctor.check_python(ctx)[0].status == PASS
    ctx.host.py.update(prefix="/usr", base_prefix="/usr")
    assert doctor.check_python(ctx)[0].status == WARN
    ctx.host.py.update(version=(3, 11, 9))
    r = doctor.check_python(ctx)[0]
    assert r.status == FAIL and "install-node.sh" in r.fix


def test_packages_missing_required_and_optional(tmp_path):
    ctx = ctx_for(tmp_path)
    assert doctor.check_packages(ctx)[0].status == PASS
    ctx.host.packages = {"x11vnc": False, "xvfb": False, "bubblewrap": False}
    results = doctor.check_packages(ctx)
    main = by_id(results, "packages")
    assert main.status == FAIL
    assert main.fix == "sudo apt-get install -y --no-install-recommends xvfb x11vnc"
    assert by_id(results, "packages.bubblewrap").status == WARN


def test_xclip_missing_is_a_warning_with_the_install_command(tmp_path):
    """Only copying out of a lane's live view needs xclip, and `agentos upgrade` runs no apt: doctor is
    how the owner of an older node learns of it, so it warns and names the command, and never fails."""
    ctx = ctx_for(tmp_path)
    ctx.host.packages = {"xclip": False}
    results = doctor.check_packages(ctx)
    assert by_id(results, "packages").status == PASS
    xclip = by_id(results, "packages.xclip")
    assert xclip.status == WARN and xclip.fix == "sudo apt-get install -y xclip"
    assert "copies" in xclip.detail and "\u2014" not in xclip.detail


def test_packages_without_dpkg_look_for_binaries(tmp_path, monkeypatch):
    ctx = ctx_for(tmp_path)
    monkeypatch.setattr(ctx.host, "dpkg_installed", lambda pkgs: None)
    ctx.host.binaries.discard("x11vnc")
    results = doctor.check_packages(ctx)
    assert "x11vnc" in by_id(results, "packages").detail


def test_camoufox_missing(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.host.camoufox_info = {"error": "no Camoufox browser installed under /x"}
    r = doctor.check_camoufox(ctx)[0]
    assert r.status == FAIL and "python -m camoufox fetch" in r.fix


def test_camoufox_links_to_the_lane_check_instead_of_checking_lanes(tmp_path):
    ctx = ctx_for(tmp_path, lanes={"a/same": 158, "a/old": 150, "a/new": 160, "a/unpinned": None})
    results = doctor.check_camoufox(ctx)
    assert results[0].status == PASS and "158.0.1-beta.41" in results[0].detail
    assert [r.id for r in results] == ["camoufox", "camoufox.model", "lanes"]  # no per-lane fingerprint rows
    lanes = by_id(results, "lanes")
    assert lanes.status == PASS and "agentos lane check --all" in lanes.detail and "4 lane(s)" in lanes.detail


def test_a_missing_or_unreadable_fingerprint_model_fails_with_the_fix(tmp_path):
    """QA 2026-11-10 finding 6: doctor passed while no lane could start."""
    ctx = ctx_for(tmp_path)
    assert by_id(doctor.check_camoufox(ctx), "camoufox.model").status == PASS
    ctx.host.fpgen_info = {"error": "cannot read /opt/agentos/.venv/lib/fpgen/data/values.json",
                           "dir": "/opt/agentos/.venv/lib/fpgen/data"}
    r = by_id(doctor.check_camoufox(ctx), "camoufox.model")
    assert r.status == FAIL and "no lane can start" in r.detail and "ensure_fpgen_model()" in r.fix
    assert "—" not in r.detail + r.fix


def test_root_checks_the_model_as_the_user_that_owns_the_home(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.host.euid_value = 0
    ctx.host.user_name = lambda uid: "agentos"
    doctor.check_camoufox(ctx)
    assert ctx.host.fpgen_users[-1] == "agentos"


def test_a_tmpfs_tmp_with_room_is_not_a_warning(tmp_path):
    """QA 2026-11-10 finding 8: 2.4 GiB free of 3.8 GiB on a tmpfs /tmp was a WARN about backups."""
    ctx = ctx_for(tmp_path)
    ctx.host.same_fs = False
    ctx.host.disk["/tmp"] = (int(3.8 * GiB), int(1.4 * GiB), int(2.4 * GiB))
    assert by_id(doctor.check_disk(ctx), "disk.tmp").status == WARN  # on a disk: under 5 GiB
    ctx.host.fstypes["/tmp"] = "tmpfs"
    r = by_id(doctor.check_disk(ctx), "disk.tmp")
    assert r.status == PASS and "tmpfs" in r.detail
    ctx.host.disk["/tmp"] = (int(3.8 * GiB), int(3.75 * GiB), int(0.05 * GiB))
    r = by_id(doctor.check_disk(ctx), "disk.tmp")
    assert r.status == FAIL and "backup" not in r.fix


def test_displays_free_taken_and_too_few(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.host.ports = set()
    assert doctor.check_displays(ctx)[0].status == PASS
    ctx.host.displays = {210}
    r = doctor.check_displays(ctx)[0]
    assert r.status == WARN and "slot 0 (:210)" in r.detail
    ctx.host.ports = {7516, 7517}  # viewer slots 1 and 2
    r = doctor.check_displays(ctx)[0]
    assert r.status == FAIL and "fewer than max_warm_lanes 2" in r.detail
    # the same slots held by agentosd's own warm lanes are fine
    ctx.host.procs = [(10, ["Xvfb", ":210", "-screen", "0", "1920x1080x24"])]
    assert doctor.check_displays(ctx)[0].status == PASS


def test_displays_ignore_the_old_vnc_port_range(tmp_path):
    """x11vnc listens on a unix socket now; a port in the old vnc range is not ours to judge."""
    ctx = ctx_for(tmp_path)
    ctx.host.ports = {7512, 7513, 7514}
    assert doctor.check_displays(ctx)[0].status == PASS


def test_displays_fail_while_an_x11vnc_of_ours_listens_on_tcp(tmp_path):
    """A lane warmed by a version before the unix socket: any local user can drive it until a restart."""
    ctx = ctx_for(tmp_path)
    ctx.host.ports = set()
    ctx.host.procs = [(11, ["x11vnc", "-display", ":211", "-rfbport", "7513", "-localhost", "-nopw"]),
                      (12, ["x11vnc", "-display", ":212", "-rfbport", "0", "-rfbportv6", "-1", "-unixsock",
                            "/h/vnc/display-212.sock"]),
                      (13, ["x11vnc", "-display", ":99", "-rfbport", "5900"])]  # not one of our displays
    r = doctor.check_displays(ctx)[0]
    assert r.status == FAIL and ":211 (pid 11)" in r.detail
    assert ":212" not in r.detail and ":99" not in r.detail and "agentos node restart" in r.fix
    ctx.host.procs = ctx.host.procs[1:]
    assert doctor.check_displays(ctx)[0].status == PASS
    # -rfbport 0 alone still listens on IPv6 [::]:5900
    ctx.host.procs = [(14, ["x11vnc", "-display", ":212", "-rfbport", "0", "-unixsock", "/h/vnc/display-212.sock"])]
    assert doctor.check_displays(ctx)[0].status == FAIL


def test_service_states(tmp_path):
    ctx = ctx_for(tmp_path)
    results = doctor.check_service(ctx)
    assert results[0].status == PASS and "as agentos" in results[0].detail
    assert len(results) == 1
    ctx.host.commands["systemctl show"] = (0, ("LoadState=loaded\nActiveState=failed\nSubState=failed\n"
                                               "UnitFileState=disabled\nUser=\n"))
    results = doctor.check_service(ctx)
    assert by_id(results, "service").status == FAIL
    assert "journalctl -u agentosd" in by_id(results, "service").fix
    assert by_id(results, "service.enabled").status == WARN
    assert by_id(results, "service.user").fix.startswith("sudo agentos migrate-service-user")
    ctx.host.commands["systemctl show"] = (0, "LoadState=not-found\nActiveState=inactive\n")
    assert doctor.check_service(ctx)[0].status == WARN
    ctx.host.binaries.discard("systemctl")
    assert doctor.check_service(ctx)[0].status == SKIP


def test_mcp_answers_with_token_and_ends_its_session(tmp_path):
    ctx = ctx_for(tmp_path)
    r = doctor.check_mcp(ctx)[0]
    assert r.status == PASS
    methods = [m for m, _, _ in ctx.host.http_calls]
    assert methods == ["POST", "POST", "DELETE"]
    anon_headers = ctx.host.http_calls[0][2]
    assert "Authorization" not in anon_headers and anon_headers["Host"] == "127.0.0.1:7510"
    assert TOKEN not in r.detail


def test_mcp_failures(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.host.http_answers = {"anon": 200, "authed": 200}
    assert "without a token" in doctor.check_mcp(ctx)[0].detail
    ctx.host.http_answers = {"anon": 401, "authed": 401}
    assert "rejects the node.toml token" in doctor.check_mcp(ctx)[0].detail
    ctx.host.ports = set()
    r = doctor.check_mcp(ctx)[0]
    assert r.status == FAIL and "nothing listens" in r.detail  # the unit is active, so this is a failure
    ctx.host.commands["systemctl show"] = (0, "ActiveState=inactive\n")
    assert doctor.check_mcp(ctx)[0].status == WARN


def test_permissions_pass_and_fail(tmp_path):
    ctx = ctx_for(tmp_path)
    assert doctor.check_permissions(ctx)[0].status == PASS
    (ctx.home / "node.toml").chmod(0o644)
    (ctx.home / "secrets").chmod(0o755)
    fp = ctx.home / "lanes" / "personal" / "reddit" / "fingerprint.json"
    fp.chmod(0o640)
    r = doctor.check_permissions(ctx)[0]
    assert r.status == FAIL
    assert f"chmod 600 {ctx.home / 'node.toml'}" in r.fix
    assert f"chmod 700 {ctx.home / 'secrets'}" in r.fix
    assert any("fingerprint.json is 640" in p for p in r.data["problems"])


def test_public_keys_in_secrets_may_be_readable_not_writable(tmp_path):
    ctx = ctx_for(tmp_path)
    pub = ctx.home / "secrets" / "policy.pub"
    pub.write_text("public key")
    pub.chmod(0o644)
    assert doctor.check_permissions(ctx)[0].status == PASS
    pub.chmod(0o664)
    assert doctor.check_permissions(ctx)[0].status == FAIL
    key = ctx.home / "secrets" / "policy.key"
    pub.chmod(0o644)
    key.write_text("private")
    key.chmod(0o644)
    assert "policy.key is 644 (want 600)" in doctor.check_permissions(ctx)[0].detail


def test_permissions_symlinked_secret(tmp_path):
    ctx = ctx_for(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    os.symlink(target, ctx.home / "secrets" / "extra.json")
    r = doctor.check_permissions(ctx)[0]
    assert r.status == FAIL and "is a symlink" in r.detail


def make_vault_db(home: Path, mode: str = "keyfile") -> None:
    db = sqlite3.connect(home / "vault.db")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE entries (id TEXT PRIMARY KEY)")
    db.executemany("INSERT INTO meta VALUES (?, ?)", [("mode", mode), ("wrapped_key", "AAAA"), ("vault_id", "v")])
    db.execute("INSERT INTO entries VALUES ('e1')")
    db.commit()
    db.close()
    (home / "vault.db").chmod(0o600)


def test_vault_states(tmp_path):
    ctx = ctx_for(tmp_path)
    r = doctor.check_vault(ctx)[0]
    assert r.status == WARN and r.fix == "agentos vault init"
    make_vault_db(ctx.home)
    results = doctor.check_vault(ctx)
    assert by_id(results, "vault.key").status == FAIL  # keyfile mode without secrets/vault.key
    assert "seal state is unknown" in by_id(results, "vault").detail
    ctx.host.sockets["vault.sock"] = {"ok": True, "status": {"state": "sealed"}}
    assert by_id(doctor.check_vault(ctx), "vault").fix == "agentos vault unseal"
    ctx.host.sockets["vault.sock"] = {"ok": True, "status": {"state": "unsealed"}}
    (ctx.home / "secrets" / "vault.key").write_text("k")
    results = doctor.check_vault(ctx)
    assert [r.status for r in results] == [PASS]
    assert "1 entries" in results[0].detail


def test_vault_check_leaves_no_files(tmp_path):
    ctx = ctx_for(tmp_path)
    make_vault_db(ctx.home)
    with sqlite3.connect(ctx.home / "vault.db") as db:
        db.execute("PRAGMA journal_mode=WAL")
    before = sorted(p.name for p in ctx.home.iterdir())
    doctor.check_vault(ctx)
    doctor.check_migrations(ctx)
    assert sorted(p.name for p in ctx.home.iterdir()) == before


def test_disk_levels(tmp_path):
    ctx = ctx_for(tmp_path)
    assert doctor.check_disk(ctx)[0].status == PASS
    ctx.host.default_disk = (40 * GiB, 37 * GiB, 3 * GiB)
    assert doctor.check_disk(ctx)[0].status == WARN
    ctx.host.default_disk = (40 * GiB, 39.5 * GiB, int(0.5 * GiB))
    r = doctor.check_disk(ctx)[0]
    assert r.status == FAIL and "agentos backup prune" in r.fix
    ctx.host.same_fs = False
    assert {r.id for r in doctor.check_disk(ctx)} == {"disk.home", "disk.tmp"}


def test_clock(tmp_path):
    ctx = ctx_for(tmp_path)
    assert doctor.check_clock(ctx)[0].status == PASS
    ctx.host.commands["timedatectl show"] = (0, "NTP=no\nNTPSynchronized=no\n")
    assert doctor.check_clock(ctx)[0].fix == "sudo timedatectl set-ntp true"
    ctx.host.commands["timedatectl show"] = (0, "NTP=yes\nNTPSynchronized=no\n")
    assert doctor.check_clock(ctx)[0].status == WARN
    ctx.host.binaries.discard("timedatectl")
    assert doctor.check_clock(ctx)[0].status == SKIP


def test_memory_versus_max_warm_lanes(tmp_path):
    ctx = ctx_for(tmp_path)
    r = doctor.check_memory(ctx)[0]
    assert r.status == PASS and r.data["recommended"] == 5
    ctx.config.max_warm_lanes = 8
    r = doctor.check_memory(ctx)[0]
    assert r.status == WARN and "set max_warm_lanes = 5" in r.fix
    ctx.config.max_warm_lanes = 2
    ctx.host.mem = {"MemTotal": 4 * GiB, "MemAvailable": int(0.2 * GiB), "SwapTotal": 2 * GiB,
                    "SwapFree": int(0.5 * GiB)}
    results = doctor.check_memory(ctx)
    assert by_id(results, "memory").status == WARN and by_id(results, "memory.swap").status == WARN
    ctx.host.mem = None
    assert doctor.check_memory(ctx)[0].status == SKIP


def test_lane_caps_systemd_refuses_are_reported(tmp_path):
    ctx = ctx_for(tmp_path)
    assert doctor.check_lane_limits(ctx)[0].status == SKIP  # no caps asked for
    ctx.config.lane_memory_max_mb, ctx.config.lane_cpu_max_pct = 2048, 150
    r = doctor.check_lane_limits(ctx)[0]  # no warm lane, and agentosd runs as agentos (hardened)
    assert r.status == WARN and "runs as agentos" in r.detail and "root" in r.fix
    profile = str(ctx.config.profile_dir("personal/reddit"))
    ctx.host.procs = [(51, ["/c/camoufox-bin", "-profile", profile]),
                      (52, ["/c/camoufox-bin", "-contentproc", "-profile", profile]),
                      (53, ["/c/camoufox-bin", "-profile", str(ctx.config.profile_dir("work/site"))])]
    ctx.host.cgroups = {51: "/system.slice/agentosd.service", 53: "/system.slice/agentosd.service"}
    r = doctor.check_lane_limits(ctx)[0]
    assert r.status == WARN and "2 of 2 warm lanes run without them" in r.detail
    ctx.host.cgroups = {51: "/agentos.slice/agentos-lanes.slice/agentos-lane-personal--reddit-51.scope",
                        53: "/agentos.slice/agentos-lanes.slice/agentos-lane-work--site-53.scope"}
    r = doctor.check_lane_limits(ctx)[0]
    assert r.status == PASS and "2 warm lanes" in r.detail


def test_the_default_lane_memory_cap_is_checked_only_where_lanes_get_scopes(tmp_path):
    ctx = ctx_for(tmp_path)
    r = doctor.check_lane_limits(ctx)[0]  # agentosd runs as agentos: no scopes, and that is not a problem
    assert r.status == SKIP and "runs as agentos" in r.detail
    ctx.host.commands["systemctl show"] = (0, "User=\nMemoryMax=3GB\nMemoryHigh=infinity\n")
    r = doctor.check_lane_limits(ctx)[0]
    assert r.status == SKIP and "memory cap of its own" in r.detail
    ctx.host.commands["systemctl show"] = (0, "User=\nMemoryMax=infinity\nMemoryHigh=infinity\n")
    r = doctor.check_lane_limits(ctx)[0]
    assert r.status == PASS and "memory 1536 MB (the default)" in r.detail
    profile = str(ctx.config.profile_dir("personal/reddit"))
    ctx.host.procs = [(51, ["/c/camoufox-bin", "-profile", profile])]
    ctx.host.cgroups = {51: "/system.slice/agentosd.service"}
    r = doctor.check_lane_limits(ctx)[0]  # as root under an uncapped unit a bare browser is worth a look
    assert r.status == WARN and "1 of 1 warm lanes run without them" in r.detail
    ctx.config.lane_memory_max_mb = 0
    assert doctor.check_lane_limits(ctx)[0].status == SKIP
    ctx.config.lane_memory_max_mb, ctx.config.lane_scopes = None, False
    assert doctor.check_lane_limits(ctx)[0].status == SKIP
    ctx.config.lane_scopes = True
    ctx.host.binaries.discard("systemctl")
    r = doctor.check_lane_limits(ctx)[0]
    assert r.status == SKIP and "systemd is not available" in r.detail


def test_proxies_ok_mismatch_unreachable_and_auth(tmp_path):
    ctx = ctx_for(tmp_path)
    r = doctor.check_proxies(ctx)[0]
    assert r.status == PASS and "exit IP 203.0.113.7 matches" in r.detail
    ctx.host.exit_ips["203.0.113.7"] = "198.51.100.9"
    r = doctor.check_proxies(ctx)[0]
    assert r.status == FAIL and "quarantined" in r.detail
    ctx.host.exit_ips["203.0.113.7"] = OSError(f"Tunnel connection failed: 407 for {PROXY_USER}:{PROXY_PASS}")
    r = doctor.check_proxies(ctx)[0]
    assert r.status == FAIL and "proxy add lt1" in r.fix
    assert PROXY_PASS not in r.detail and PROXY_USER not in r.detail
    ctx.host.tcp_errors["203.0.113.7"] = ConnectionRefusedError(111, "Connection refused")
    assert "cannot connect" in doctor.check_proxies(ctx)[0].detail
    ctx.network = False
    assert doctor.check_proxies(ctx)[0].status == SKIP


def test_proxy_host_name_resolving_to_exit(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.host.dns["203.0.113.7"] = {"203.0.113.7", "203.0.113.8"}
    ctx.host.exit_ips["203.0.113.7"] = "203.0.113.8"
    assert doctor.check_proxies(ctx)[0].status == PASS


def test_migrations_pending_current_and_newer(tmp_path):
    from agentos.ops import migrate

    ctx = ctx_for(tmp_path)
    assert doctor.check_migrations(ctx)[0].status == WARN
    migrate.migrate(ctx.home, backup=False)
    assert doctor.check_migrations(ctx)[0].status == PASS
    with sqlite3.connect(ctx.home / "agentos.db") as db:
        db.execute("INSERT INTO schema_version VALUES ('node', 9, 'future', 'now')")
    assert doctor.check_migrations(ctx)[0].status == FAIL


def test_backups_none_old_and_fresh(tmp_path):
    ctx = ctx_for(tmp_path)
    assert doctor.check_backups(ctx)[0].status == WARN
    d = ctx.home / "backups"
    d.mkdir(mode=0o700)
    (d / "agentos-test-node-20261101T000000Z.tar.gz.age").write_bytes(b"x")
    ctx.host.now_value = 1_793_000_000.0  # 2026-10-26 + 30 days or so; older than a week
    from datetime import UTC, datetime
    ctx.host.now_value = datetime(2026, 11, 20, tzinfo=UTC).timestamp()
    assert doctor.check_backups(ctx)[0].status == WARN
    ctx.host.now_value = datetime(2026, 11, 2, tzinfo=UTC).timestamp()
    assert doctor.check_backups(ctx)[0].status == PASS


def test_run_isolates_a_crashing_check(tmp_path, monkeypatch):
    ctx = ctx_for(tmp_path)

    def boom(_ctx):
        raise RuntimeError(f"leak {TOKEN}")

    monkeypatch.setitem(doctor.CHECKS, "clock", boom)
    report = doctor.run(ctx)
    clock = by_id(report.results, "clock")
    assert clock.status == FAIL and TOKEN not in clock.detail
    assert len(report.results) > 10


def test_run_only_and_unknown(tmp_path):
    ctx = ctx_for(tmp_path)
    report = doctor.run(ctx, ["disk", "clock"])
    assert {r.id for r in report.results} == {"disk", "clock"}
    with pytest.raises(ValueError):
        doctor.run(ctx, ["nope"])


def test_full_report_has_no_secrets_and_valid_json(tmp_path):
    host = FakeHost()
    host.exit_ips["203.0.113.7"] = OSError(f"boom {PROXY_USER}:{PROXY_PASS}")
    ctx = ctx_for(tmp_path, host=host)
    report = doctor.run(ctx)
    text = doctor.format_text(report)
    blob = json.dumps(report.to_dict())
    for secret in (TOKEN, PROXY_PASS, PROXY_USER):
        assert secret not in text and secret not in blob
    data = json.loads(blob)
    assert data["summary"]["fail"] >= 1 and data["ok"] is False
    assert {"id", "title", "status", "detail", "fix", "data"} <= set(data["checks"][0])
    assert "—" not in text  # no em dashes in what a user reads
    assert "fix: " in text


def test_fingerprint_drift_is_left_to_the_lane_check(tmp_path):
    ctx = ctx_for(tmp_path, lanes={"a/b": 150})
    path = ctx.home / "lanes" / "a" / "b" / "fingerprint.json"
    path.write_text(json.dumps(fingerprint(150)))
    report = doctor.run(ctx, ["camoufox"])
    assert report.ok and report.counts[WARN] == 0  # agentos lane check reports the drift per lane


# ---- workspace memory files -------------------------------------------------------------------

def test_workspace_memory_files_are_checked(tmp_path):
    from agentos.memory import Caller
    from agentos.memory.fakes import fake_memory
    from agentos.ops.doctor import DoctorContext, check_workspace_memory

    ctx = DoctorContext(home=tmp_path)
    assert check_workspace_memory(ctx)[0].status == "skip"
    m = fake_memory(tmp_path)
    m.service.write(Caller.owner(), "biz-a", {"type": "fact", "title": "a fact"})
    m.service.close()
    [ok] = check_workspace_memory(ctx)
    assert ok.status == "pass" and "1 workspace file" in ok.detail
    (tmp_path / "memory" / "biz-b.db").write_bytes(b"not a database" * 100)
    [bad] = check_workspace_memory(ctx)
    assert bad.status == "fail" and "biz-b.db" in bad.detail and "—" not in bad.detail
