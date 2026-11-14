"""Ops on a real node: doctor against a dev install, backup and restore of a real lane profile,
the installer with --user agentos in a dev prefix, and the units under systemd-analyze verify.

Run on the node:  AGENTOS_TEST_SLOT=21 scripts/node-test.sh ops tests/node/test_ops_node.py
Uses the ops agent's ranges (displays :210-:219, ports 7510-7519) and directories under
the directory that holds this checkout, the dev root (override with AGENTOS_TEST_OPS_ROOT). Never touches /var/lib/agentos,
/opt/agentos or the agentosd service; creates the `agentos` user only when it does not exist yet,
and deletes it again.
"""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

import pytest

from agentos.core.config import NodeConfig, load_config, save_config
from agentos.core.models import LaneSpec, LaneState
from agentos.core.runtime import Runtime

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node"),
]

CHECKOUT = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ.get("AGENTOS_TEST_OPS_ROOT", CHECKOUT.parent))
AGE = Path(os.environ.get("AGENTOS_TEST_AGE", ROOT / "tools" / "pkg" / "usr" / "bin" / "age"))
PASSPHRASE = "node test passphrase, long enough"
MCP_PORT = 7510
LANE_RANGES = {"display_base": 214, "vnc_base": 7511, "viewer_base": 7513, "max_slots": 2}
DEV_RANGES = {"display_base": 210, "vnc_base": 7515, "viewer_base": 7517, "max_slots": 2}
TEST_USER = os.environ.get("AGENTOS_TEST_USER", "agentos")


def _safe(path: Path) -> Path:
    real = path.resolve()
    root = ROOT.resolve()
    assert root != Path("/") and str(real).startswith(f"{root}/"), f"refusing to work outside {root}: {real}"
    return real


@pytest.fixture(scope="module")
def work():
    d = _safe(ROOT) / f"nt-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(d.with_name(d.name + ".rollback"), ignore_errors=True)


def run_installer(prefix: Path, home: Path, *flags: str, init_args: str = "") -> subprocess.CompletedProcess:
    env = {**os.environ, "INSTALL_PREFIX": str(prefix), "AGENTOS_HOME": str(home), "AGENTOS_INIT_ARGS": init_args,
           "PATH": f"{Path.home() / '.local/bin'}:{os.environ['PATH']}"}
    return subprocess.run(["bash", str(CHECKOUT / "deploy" / "install-node.sh"), *flags], env=env,
                          capture_output=True, text=True, timeout=900, check=False)


@pytest.fixture(scope="module")
def dev_install(work):
    prefix, home = work / "prefix", work / "home"
    proc = run_installer(prefix, home, "--skip-apt", "--skip-browser",
                         init_args=f"--name ops-dev --mcp-port {MCP_PORT} --display-base 210 --vnc-base 7515 "
                                   "--viewer-base 7517 --max-warm-lanes 2")
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    cfg = load_config(home)
    cfg.max_slots = DEV_RANGES["max_slots"]
    save_config(cfg)
    return prefix, home


def ops(prefix: Path, home: Path, *args: str, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run([str(prefix / ".venv" / "bin" / "python"), "-m", "agentos.ops", *args],
                          env={**os.environ, "AGENTOS_HOME": str(home)}, capture_output=True, text=True,
                          timeout=timeout, check=False)


def port_open(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ---- doctor ---------------------------------------------------------------------------------

def test_doctor_against_a_dev_install_with_its_daemon(dev_install):
    prefix, home = dev_install
    assert not port_open(MCP_PORT), f"port {MCP_PORT} is already in use"
    daemon = subprocess.Popen([str(prefix / ".venv" / "bin" / "agentos"), "node", "serve"],
                              env={**os.environ, "AGENTOS_HOME": str(home)}, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 60
        while not port_open(MCP_PORT):
            assert daemon.poll() is None, "the dev daemon exited"
            assert time.monotonic() < deadline, "the dev daemon did not listen in time"
            time.sleep(0.3)
        proc = ops(prefix, home, "doctor", "--json", "--unit", "agentosd-ops-test-none")
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(30)
        except subprocess.TimeoutExpired:
            os.killpg(daemon.pid, signal.SIGKILL)
            daemon.wait(5)
    report = json.loads(proc.stdout)
    status = {c["id"]: c["status"] for c in report["checks"]}
    print(json.dumps({c["id"]: [c["status"], c["detail"]] for c in report["checks"]}, indent=1))
    for cid in ("config", "os", "python", "packages", "camoufox", "displays", "mcp", "permissions", "disk",
                "clock", "proxies"):
        assert status[cid] == "pass", (cid, report)
    assert status["service"] == "warn"  # the dev install has no unit
    assert status["migrations"] == "warn" and status["backups"] == "warn"
    token = load_config(home).mcp_token
    assert token and token not in proc.stdout
    assert proc.returncode == (0 if report["ok"] else 1)
    assert not port_open(MCP_PORT)


# ---- backup and restore of a real lane ---------------------------------------------------------

def lane_config(home: Path) -> NodeConfig:
    cfg = NodeConfig(home=home, name="ops-lanes", mcp_token="node-test-token", self_check=False, **LANE_RANGES)
    if not (home / "node.toml").exists():
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        save_config(cfg)
    return cfg


def age_decrypt(archive: Path, out: Path, passphrase: str) -> None:
    """Decrypt with the stock age binary; it reads the passphrase from a terminal, which `script` gives it."""
    cmd = " ".join(shlex.quote(str(a)) for a in (AGE, "-d", "-o", out, archive))
    proc = subprocess.run(["script", "-qec", cmd, "/dev/null"], input=passphrase + "\n", capture_output=True,
                          text=True, timeout=120, check=False)
    assert proc.returncode == 0 and out.exists(), proc.stdout + proc.stderr


async def test_backup_pauses_a_warm_real_lane_and_restore_comes_up_logged_in(work):
    from agentos.node.engine_camoufox import browser_pids_under
    from agentos.node.lanes import build_lane_manager
    from agentos.ops import backup
    from agentos.ops.service import HealthService

    home, restored = work / "lanehome", work / "restored"
    cfg = lane_config(home)
    manager = build_lane_manager(cfg)
    await manager.start()
    service = HealthService(Runtime(config=cfg, lanes=manager), first_delay=3600)
    await service.start()
    expires = int(time.time()) + 30 * 86400
    try:
        await manager.create(LaneSpec(id="ops/real"))
        async with manager.using("ops/real") as page:
            await page.context.add_cookies([{"name": "agentos_session", "value": "logged-in-42",
                                             "domain": "example.test", "path": "/", "expires": expires,
                                             "httpOnly": True, "secure": True, "sameSite": "Lax"}])
        assert manager.get("ops/real").state == LaneState.WARM
        result = await asyncio.to_thread(
            backup.create_backup, home, work / "backups", PASSPHRASE, node="ops-lanes", log_n=14,
            coordinator=backup.DaemonCoordinator(home))
        assert [(lane["id"], lane["copied"]) for lane in result.lanes] == [("ops/real", "paused")]
        for _ in range(100):  # warmed again in the background after the pause
            if manager.get("ops/real").state == LaneState.WARM:
                break
            await asyncio.sleep(0.3)
        assert manager.get("ops/real").state == LaneState.WARM
    finally:
        await service.stop()
        await manager.shutdown()
    pinned = cfg.fingerprint_path("ops/real").read_bytes()

    contents = await asyncio.to_thread(backup.verify_backup, result.path, PASSPHRASE)
    assert "agentos.db" in contents.sqlite_checked
    if AGE.exists():  # the stock age tool opens our archives
        plain = work / "plain.tar.gz"
        await asyncio.to_thread(age_decrypt, result.path, plain, PASSPHRASE)
        with tarfile.open(plain) as tar:
            names = tar.getnames()
        assert names[0] == "agentos-backup/format.json" and names[-1] == "agentos-backup/manifest.json"
        assert "home/lanes/ops/real/profile/cookies.sqlite" in names

    r = await asyncio.to_thread(backup.restore_backup, result.path, PASSPHRASE, restored)
    assert r.lanes == ["ops/real"] and r.permission_problems == []
    for p in [restored, *restored.rglob("*")]:
        assert stat.S_IMODE(p.stat().st_mode) == (0o700 if p.is_dir() else 0o600), p
    rcfg = lane_config(restored)
    assert rcfg.fingerprint_path("ops/real").read_bytes() == pinned
    with sqlite3.connect(rcfg.profile_dir("ops/real") / "cookies.sqlite") as db:
        assert db.execute("SELECT value FROM moz_cookies WHERE name='agentos_session'").fetchone() == ("logged-in-42",)

    # the restored node comes up with the same lane, the same device and its session
    manager2 = build_lane_manager(load_config(restored).model_copy(update={"self_check": False, **LANE_RANGES}))
    await manager2.start()
    try:
        assert [i.spec.id for i in manager2.list()] == ["ops/real"]
        async with manager2.using("ops/real") as page:
            cookies = await page.context.cookies("https://example.test/")
            ua = await page.evaluate("navigator.userAgent")
        assert {c["name"]: c["value"] for c in cookies}.get("agentos_session") == "logged-in-42"
        assert ua == json.loads(pinned)["navigator.userAgent"]
    finally:
        await manager2.shutdown()
    leftover = browser_pids_under(work)
    assert not leftover, f"browsers left running: {leftover}"


# ---- the installer with a service user, and the units --------------------------------------------

@pytest.fixture
def service_user():
    try:
        pwd.getpwnam(TEST_USER)
        pytest.skip(f"user {TEST_USER} already exists; this test never touches an existing user")
    except KeyError:
        pass
    yield TEST_USER
    subprocess.run(["userdel", TEST_USER], check=False, capture_output=True)
    subprocess.run(["groupdel", TEST_USER], check=False, capture_output=True)
    with pytest.raises(KeyError):
        pwd.getpwnam(TEST_USER)


def verify_unit(path: Path) -> None:
    proc = subprocess.run(["systemd-analyze", "verify", str(path)], capture_output=True, text=True, timeout=60,
                          check=False)
    ours = [line for line in (proc.stdout + proc.stderr).splitlines() if path.name in line]
    assert proc.returncode == 0 and not ours, proc.stdout + proc.stderr


def test_installer_with_a_service_user_in_a_dev_prefix(dev_install, work, service_user):
    prefix, _ = dev_install
    home = work / "userhome"
    unit = work / "agentosd.service"
    proc = run_installer(prefix, home, "--skip-apt", "--skip-browser", "--user", service_user, "--hardened",
                         "--render-unit", str(unit), init_args="--name ops-user --mcp-port 7519")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-3000:]
    pw = pwd.getpwnam(service_user)
    assert pw.pw_shell == "/usr/sbin/nologin" and Path(pw.pw_dir) == home
    for p in [home, *home.rglob("*")]:
        st = p.stat()
        assert st.st_uid == pw.pw_uid, p
        assert not st.st_mode & 0o077, p
    assert stat.S_IMODE((home / "node.toml").stat().st_mode) == 0o600
    # a dev prefix under /root is not reachable for the user; the installer says so (and refuses with --systemd)
    assert "cannot reach" in out
    text = unit.read_text()
    assert f"User={service_user}\n" in text and f"Group={service_user}\n" in text
    assert f'ReadWritePaths="{home}"\n' in text
    assert f'ExecStart="{prefix}/.venv/bin/agentos" node serve\n' in text
    assert f'Environment="XDG_CACHE_HOME={home}/.cache"\n' in text
    assert "NoNewPrivileges=yes" in text and "ProtectSystem=strict" in text and "PrivateTmp=yes" in text
    hardened = work / "agentosd-hardened.service"
    shutil.copy(unit, hardened)
    verify_unit(hardened)

    plain = work / "plain" / "agentosd.service"
    plain.parent.mkdir()
    proc = run_installer(prefix, home, "--skip-apt", "--skip-browser", "--user", service_user, "--render-unit",
                         str(plain))
    assert proc.returncode == 0, proc.stderr[-2000:]
    text = plain.read_text()
    assert text.count(f"User={service_user}\n") == 1 and f'Environment="HOME={home}"\n' in text
    verify_unit(plain)

    # with --systemd an install the user cannot reach is refused (--render-unit keeps systemd untouched anyway)
    proc = run_installer(prefix, home, "--skip-apt", "--skip-browser", "--user", service_user, "--systemd",
                         "--render-unit", str(unit))
    assert proc.returncode != 0 and "cannot reach" in proc.stderr
    proc = run_installer(prefix, home, "--skip-apt", "--skip-browser", "--user", service_user, "--hardened",
                         "--systemd", "--render-unit", str(unit))
    assert proc.returncode != 0 and "ProtectHome" in proc.stderr
    shutil.rmtree(home, ignore_errors=True)


def test_units_pass_systemd_analyze(dev_install, work):
    prefix, home = dev_install
    unit = work / "units" / "agentosd.service"
    unit.parent.mkdir()
    proc = run_installer(prefix, home, "--skip-apt", "--skip-browser", "--render-unit", str(unit))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "User=" not in unit.read_text()
    verify_unit(unit)
    for name in ("agentos-backup.service", "agentos-backup.timer"):
        dest = work / "units" / name
        dest.write_text((CHECKOUT / "deploy" / name).read_text().replace("/opt/agentos", str(prefix))
                        .replace("/var/lib/agentos", str(home)))
        verify_unit(dest)


def test_bad_installer_flags(dev_install, work):
    prefix, home = dev_install
    proc = run_installer(prefix, home, "--hardened", "--skip-apt", "--skip-browser")
    assert proc.returncode != 0 and "--hardened needs --user" in proc.stderr
    proc = run_installer(prefix, home, "--user", "Bad Name")
    assert proc.returncode != 0


# ---- upgrade of a dev install ------------------------------------------------------------------

def test_upgrade_a_dev_install(dev_install, work):
    prefix, home = dev_install
    proc = ops(prefix, home, "upgrade", "--source", str(CHECKOUT), "--prefix", str(prefix), "--no-service",
               "--skip-apt", "--skip-browser", "--yes", "--json", timeout=900)
    result = json.loads(proc.stdout)
    assert proc.returncode == 0 and result["ok"] and result["state"] == "upgraded", proc.stderr[-3000:]
    steps = [s["step"] for s in result["steps"]]
    assert steps == ["preflight", "snapshot", "install", "migrate", "health"]
    assert any(a["package"] == "node" for a in result["migrate"]["applied"])
    assert (prefix.with_name(prefix.name + ".rollback") / ".venv" / "bin" / "agentos").exists()
    again = ops(prefix, home, "migrate", "status", "--json")
    assert all(not s["pending"] for s in json.loads(again.stdout))


def test_root_keeps_a_service_user_home_owned_by_that_user(work, service_user):
    """Root runs backups, migrations, rollbacks, refreshes and restores on a home the service user owns;
    everything it writes there must stay the service user's, or the service can no longer use it."""
    from agentos.ops import backup, migrate
    from agentos.stealth import fingerprint

    subprocess.run(["useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin",
                    service_user], check=True)
    pw = pwd.getpwnam(service_user)
    home = work / "owned-home"
    cfg = lane_config(home)
    lane = cfg.lane_dir("ops/owned")
    cfg.profile_dir("ops/owned").mkdir(parents=True)
    (cfg.profile_dir("ops/owned") / "cookies.sqlite").write_bytes(b"cookies")
    cfg.fingerprint_path("ops/owned").write_text(json.dumps({"navigator.userAgent": "rv:150.0 Firefox/150.0"}))
    with sqlite3.connect(home / "agentos.db") as db:
        db.execute("CREATE TABLE lanes (id TEXT PRIMARY KEY, spec TEXT NOT NULL, last_used TEXT, exit_ip TEXT)")
    subprocess.run(["chown", "-R", f"{service_user}:{service_user}", str(home)], check=True)
    subprocess.run(["chmod", "-R", "go-rwx", str(home)], check=True)

    def owned(path: Path) -> bool:
        return path.stat().st_uid == pw.pw_uid

    result = fingerprint.refresh(cfg.fingerprint_path("ops/owned"), "ops/owned", 158)
    assert owned(cfg.fingerprint_path("ops/owned")) and owned(result.backup)

    applied = migrate.migrate(home)
    assert applied.applied and owned(applied.backup_dir) and owned(applied.backup_dir / "agentos.db")
    assert owned(home / "backups") and owned(home / "agentos.db")
    migrate.restore_databases(applied.backup_dir, home)  # the upgrade's rollback
    assert owned(home / "agentos.db")

    made = backup.create_backup(home, home / "backups", PASSPHRASE, node="owned", log_n=12)
    assert owned(made.path) and owned(home / "backups")
    restored = backup.restore_backup(made.path, PASSPHRASE, home, force=True)
    assert restored.moved_aside is not None
    for p in [home, *home.rglob("*")]:
        assert owned(p), p
    assert lane.exists()
    shutil.rmtree(home, ignore_errors=True)
    shutil.rmtree(restored.moved_aside, ignore_errors=True)
