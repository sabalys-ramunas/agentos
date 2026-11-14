"""`agentos upgrade`: drain, install, migrate, restart, check health, and roll back on failure.

The order, and what happens when a step fails:

  1. preflight      the source is an agentos checkout with deploy/install-node.sh; the prefix
                    has a venv; there is room for a copy of the prefix. Fails: nothing changed.
  2. snapshot       the whole prefix (code and .venv) is copied to <prefix>.rollback with rsync.
                    Fails: nothing changed.
  3. drain          every warm lane is sent cold through the ops socket (graceful: cookies are
                    flushed, in-flight tool calls finish first). No socket: the stop below
                    closes the lanes just as gracefully, only with systemd's 30 s budget.
  4. stop           systemctl stop. Fails: the upgrade stops, the service is started again.
  5. install        the new code: deploy/install-node.sh --no-restart (and --systemd, --user,
                    --hardened as given), from the source checkout into the prefix.
  6. migrate        the new code runs `python -m agentos.ops migrate run --json`; it backs the databases up
                    under AGENTOS_HOME/backups/migrations/ before it changes anything, then brings
                    every lane directory to the current format (agentos.node.lanefmt), with what a
                    step changes under AGENTOS_HOME/backups/lane-migrations/.
  7. start          systemctl start.
  8. health         within health_timeout: the unit is active and the MCP port answers 401
                    without the token and 2xx with it (a dev install: the new CLI runs).
  Any failure in 5 to 8 rolls back: stop, put the prefix snapshot back, put the pre-migration
  databases and lane files back, start, and check health again. The result says which state the node is in.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agentos.ops import migrate as migrate_mod
from agentos.ops.layout import ops_socket
from agentos.ops.system import Host

KEEP_IN_PREFIX = ("runtime", "node")  # runtimes other installers manage (kept by install-node.sh too)


class UpgradeError(Exception):
    def __init__(self, message: str, backup_dir: str | None = None, lanes_backup_dir: str | None = None):
        super().__init__(message)
        self.backup_dir = backup_dir
        self.lanes_backup_dir = lanes_backup_dir


@dataclass
class UpgradePlan:
    source: Path
    prefix: Path
    home: Path
    unit: str = "agentosd"
    service: bool = True
    user: str | None = None
    hardened: bool = False
    installer_flags: list[str] = field(default_factory=list)
    health_timeout: float = 90.0
    drain_timeout: float = 60.0

    @property
    def snapshot_dir(self) -> Path:
        return self.prefix.with_name(self.prefix.name + ".rollback")


class Steps(Protocol):
    def preflight(self, plan: UpgradePlan) -> None: ...
    def snapshot_code(self, plan: UpgradePlan) -> Path: ...
    def drain(self, plan: UpgradePlan) -> dict[str, Any]: ...
    def stop(self, plan: UpgradePlan) -> None: ...
    def install(self, plan: UpgradePlan) -> None: ...
    def migrate(self, plan: UpgradePlan) -> dict[str, Any]: ...
    def start(self, plan: UpgradePlan) -> None: ...
    def healthy(self, plan: UpgradePlan) -> tuple[bool, str]: ...
    def restore_code(self, plan: UpgradePlan, snapshot: Path) -> None: ...
    def restore_databases(self, plan: UpgradePlan, backup_dir: Path) -> list[str]: ...
    def restore_lanes(self, plan: UpgradePlan, backup_dir: Path) -> list[str]: ...


@dataclass
class UpgradeResult:
    ok: bool = False
    rolled_back: bool = False
    steps: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    migrate: dict[str, Any] | None = None
    state: str = "unchanged"  # unchanged | upgraded | rolled_back | broken

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "rolled_back": self.rolled_back, "state": self.state, "error": self.error,
                "steps": self.steps, "migrate": self.migrate}


def run_upgrade(plan: UpgradePlan, steps: Steps, log: Callable[[str], None] = print) -> UpgradeResult:
    result = UpgradeResult()

    def record(name: str, status: str, detail: str = "") -> None:
        result.steps.append({"step": name, "status": status, "detail": detail})
        log(f"{name}: {status}{f' ({detail})' if detail else ''}")

    def attempt(name: str, fn: Callable[[], Any]) -> Any:
        try:
            out = fn()
        except Exception as e:
            record(name, "failed", f"{type(e).__name__}: {e}")
            raise
        record(name, "ok")
        return out

    # 1-2: nothing has changed until the snapshot exists
    try:
        attempt("preflight", lambda: steps.preflight(plan))
        snapshot = attempt("snapshot", lambda: steps.snapshot_code(plan))
    except Exception as e:  # noqa: BLE001
        result.error = str(e)
        return result

    # 3-4
    if plan.service:
        try:
            drained = steps.drain(plan)
            record("drain", "ok", ", ".join(drained.get("closed", [])) or drained.get("note", "no warm lanes"))
        except Exception as e:  # noqa: BLE001 - systemctl stop closes lanes gracefully too
            record("drain", "skipped", f"{type(e).__name__}: {e}; the stop closes the lanes")
        try:
            attempt("stop", lambda: steps.stop(plan))
        except Exception as e:  # noqa: BLE001
            result.error = f"could not stop {plan.unit}: {e}"
            with contextlib.suppress(Exception):
                steps.start(plan)
            return result

    # 5-8, with rollback
    backup_dir: Path | None = None
    lanes_backup: Path | None = None
    failure: str | None = None
    try:
        attempt("install", lambda: steps.install(plan))
        result.migrate = attempt("migrate", lambda: steps.migrate(plan))
        if result.migrate and result.migrate.get("backup_dir"):
            backup_dir = Path(result.migrate["backup_dir"])
        if result.migrate and result.migrate.get("lanes_backup_dir"):
            lanes_backup = Path(result.migrate["lanes_backup_dir"])
        if plan.service:
            attempt("start", lambda: steps.start(plan))
        ok, detail = steps.healthy(plan)
        record("health", "ok" if ok else "failed", detail)
        if not ok:
            failure = f"the new version failed its health check: {detail}"
    except Exception as e:  # noqa: BLE001
        failure = f"{type(e).__name__}: {e}"
        if backup_dir is None and getattr(e, "backup_dir", None):  # a failed migrate made its backup first
            backup_dir = Path(e.backup_dir)  # type: ignore[attr-defined]
        if lanes_backup is None and getattr(e, "lanes_backup_dir", None):
            lanes_backup = Path(e.lanes_backup_dir)  # type: ignore[attr-defined]
    if failure is None:
        result.ok, result.state = True, "upgraded"
        return result

    # rollback
    result.error = failure
    result.rolled_back = True
    log(f"rolling back: {failure}")
    try:
        if plan.service:
            with contextlib.suppress(Exception):
                steps.stop(plan)
        attempt("rollback.code", lambda: steps.restore_code(plan, snapshot))
        if backup_dir is not None and backup_dir.exists():
            attempt("rollback.databases", lambda: steps.restore_databases(plan, backup_dir))
        if lanes_backup is not None and lanes_backup.exists():
            attempt("rollback.lanes", lambda: steps.restore_lanes(plan, lanes_backup))
        if plan.service:
            attempt("rollback.start", lambda: steps.start(plan))
        ok, detail = steps.healthy(plan)
        record("rollback.health", "ok" if ok else "failed", detail)
        result.state = "rolled_back" if ok else "broken"
    except Exception as e:  # noqa: BLE001
        result.state = "broken"
        result.error = f"{failure}; the rollback failed too: {type(e).__name__}: {e}"
    return result


# ---- the real steps ------------------------------------------------------------------------

def _rsync_excludes(plan: UpgradePlan) -> list[str]:
    out = []
    for keep in KEEP_IN_PREFIX:
        out += ["--exclude", f"/{keep}/"]
    try:
        rel = plan.home.resolve().relative_to(plan.prefix.resolve())
        out += ["--exclude", f"/{rel.as_posix()}/"]
    except ValueError:
        pass
    return out


class SystemSteps:
    def __init__(self, host: Host | None = None, log: Callable[[str], None] = print):
        self.host = host or Host()
        self.log = log

    def _run(self, argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 1800) -> str:
        proc = subprocess.run(argv, env={**os.environ, **(env or {})}, capture_output=True, text=True,
                              timeout=timeout, check=False, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-8:])
            raise UpgradeError(f"{' '.join(argv[:3])} exited {proc.returncode}: {tail}")
        return proc.stdout

    def preflight(self, plan: UpgradePlan) -> None:
        pyproject = plan.source / "pyproject.toml"
        if not pyproject.exists() or 'name = "agentos"' not in pyproject.read_text():
            raise UpgradeError(f"{plan.source} is not an agentos checkout")
        if not (plan.source / "deploy" / "install-node.sh").exists():
            raise UpgradeError(f"{plan.source} has no deploy/install-node.sh")
        if not (plan.prefix / ".venv" / "bin" / "agentos").exists():
            raise UpgradeError(f"{plan.prefix} has no agentos install (.venv/bin/agentos)")
        if plan.source.resolve() == plan.prefix.resolve():
            raise UpgradeError("the source is the install prefix itself; upgrade from a separate checkout")
        if plan.service and not self.host.which("systemctl"):
            raise UpgradeError("systemctl not found; use --no-service for a dev install")
        size = sum(f.stat().st_size for f in plan.prefix.rglob("*") if f.is_file() and not f.is_symlink())
        usage = self.host.disk_usage(plan.prefix.parent)
        if usage is not None and usage[2] < size * 2 + 512 * 1024**2:
            raise UpgradeError(f"not enough disk for the rollback copy ({size // 1024**2} MiB needed twice)")

    def snapshot_code(self, plan: UpgradePlan) -> Path:
        snap = plan.snapshot_dir
        snap.mkdir(mode=0o755, exist_ok=True)
        self._run(["rsync", "-a", "--delete", *_rsync_excludes(plan), f"{plan.prefix}/", f"{snap}/"])
        return snap

    def drain(self, plan: UpgradePlan) -> dict[str, Any]:
        resp = self.host.unix_request(ops_socket(plan.home), {"op": "drain", "timeout": plan.drain_timeout},
                                      timeout=plan.drain_timeout + 30)
        if resp is None:
            return {"closed": [], "note": "no ops socket; the stop closes the lanes"}
        if not resp.get("ok"):
            raise UpgradeError(resp.get("error", {}).get("message", "drain failed"))
        return resp

    def stop(self, plan: UpgradePlan) -> None:
        self._run(["systemctl", "stop", plan.unit], timeout=120)

    def start(self, plan: UpgradePlan) -> None:
        self._run(["systemctl", "start", plan.unit], timeout=120)

    def install(self, plan: UpgradePlan) -> None:
        argv = ["bash", str(plan.source / "deploy" / "install-node.sh"), "--no-restart"]
        if plan.service:
            argv.append("--systemd")
        if plan.user:
            argv += ["--user", plan.user]
        if plan.hardened:
            argv.append("--hardened")
        argv += plan.installer_flags
        out = self._run(argv, env={"INSTALL_PREFIX": str(plan.prefix), "AGENTOS_HOME": str(plan.home)})
        for line in out.splitlines():
            if line.startswith("==>"):
                self.log(f"  {line}")

    def migrate(self, plan: UpgradePlan) -> dict[str, Any]:
        argv = [str(plan.prefix / ".venv" / "bin" / "python"), "-m", "agentos.ops", "migrate", "run", "--json"]
        proc = subprocess.run(argv, env={**os.environ, "AGENTOS_HOME": str(plan.home)}, capture_output=True,
                              text=True, timeout=600, check=False, stdin=subprocess.DEVNULL)
        try:
            out = json.loads(proc.stdout)
        except ValueError:
            tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-5:])
            raise UpgradeError(f"agentos migrate run exited {proc.returncode} without a JSON report: {tail}") from None
        if proc.returncode != 0 or not out.get("ok", True):
            raise UpgradeError(out.get("error") or f"agentos migrate run exited {proc.returncode}",
                               backup_dir=out.get("backup_dir"), lanes_backup_dir=out.get("lanes_backup_dir"))
        return out

    def healthy(self, plan: UpgradePlan) -> tuple[bool, str]:
        from agentos.ops.doctor import DoctorContext, check_mcp

        deadline = time.monotonic() + plan.health_timeout
        detail = ""
        while True:
            if plan.service:
                rc, out = self.host.run(["systemctl", "is-active", plan.unit])
                state = out.strip()
                if state == "active":
                    results = check_mcp(DoctorContext(home=plan.home, host=self.host, unit=plan.unit))
                    if results and results[0].status == "pass":
                        return True, f"{plan.unit} active; {results[0].detail}"
                    detail = results[0].detail if results else "no MCP result"
                else:
                    detail = f"{plan.unit} is {state or 'unknown'}"
                    if state == "failed":
                        return False, detail
            else:
                rc, out = self.host.run([str(plan.prefix / ".venv" / "bin" / "agentos"), "version"])
                if rc == 0:
                    return True, f"agentos {out.strip()} runs from {plan.prefix}"
                detail = f"agentos version exited {rc}"
            if time.monotonic() > deadline:
                return False, detail
            time.sleep(2)

    def restore_code(self, plan: UpgradePlan, snapshot: Path) -> None:
        if not (snapshot / ".venv").exists():
            raise UpgradeError(f"the rollback copy {snapshot} is incomplete; restore {plan.prefix} by hand")
        self._run(["rsync", "-a", "--delete", *_rsync_excludes(plan), f"{snapshot}/", f"{plan.prefix}/"])

    def restore_databases(self, plan: UpgradePlan, backup_dir: Path) -> list[str]:
        return migrate_mod.restore_databases(backup_dir, plan.home)

    def restore_lanes(self, plan: UpgradePlan, backup_dir: Path) -> list[str]:
        from agentos.node import lanefmt
        return lanefmt.restore_lanes(backup_dir, plan.home)


def remove_snapshot(plan: UpgradePlan) -> None:
    shutil.rmtree(plan.snapshot_dir, ignore_errors=True)
