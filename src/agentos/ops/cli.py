"""Ops commands: doctor, backup, migrate, upgrade, health, and lane refresh-fingerprint.

`app` holds them all (mounted as `agentos ops ...` by the feature package contract). The root
CLI should also call `mount(root_app, lane_group=lane_app)`, which adds the documented names:

  agentos doctor [--json] [--offline] [--only a,b]
  agentos backup create|verify|restore|list|prune
  agentos migrate status|run
  agentos upgrade --source DIR
  agentos health check|status
  agentos lane refresh-fingerprint <lane> --keep-device
  agentos lane check <lane> | --all [--json] [--offline]
  agentos lane health <lane> | --all [--days N] [--fresh] [--json]

Passphrases are read from --passphrase-file (or AGENTOS_BACKUP_PASSPHRASE_FILE), or asked on
the terminal; they are never accepted on the command line, where `ps` would show them.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from agentos.core import age, catalog
from agentos.core.config import config_path, home_from_env, load_config
from agentos.core.errors import AgentosError
from agentos.core.models import parse_lane_id
from agentos.keepalive.cli import keepalive_cmd
from agentos.node import lanefmt
from agentos.ops import backup, doctor, logs, migrate
from agentos.ops.layout import default_backup_dir, ops_socket
from agentos.ops.system import Host
from agentos.stealth import fingerprint

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False,
                  help="Operations: doctor, backups, migrations, upgrades and health.")
backup_app = typer.Typer(no_args_is_help=True, help="Encrypted backups of AGENTOS_HOME (profiles, vault, databases).")
migrate_app = typer.Typer(no_args_is_help=True, help="Versioned migrations of the databases and the lane directories.")
health_app = typer.Typer(no_args_is_help=True, help="Node health: exit IPs, proxies, disk, RAM, browser drift.")

PASSPHRASE_ENV = "AGENTOS_BACKUP_PASSPHRASE_FILE"


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    raise typer.Exit(code)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except AgentosError as e:
        typer.secho(catalog.cli_error(e.code, e.message), err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    except (migrate.MigrationError, lanefmt.LaneFormatError, age.AgeError) as e:
        _fail(str(e))


def _home() -> Path:
    return home_from_env()


def _node_name(home: Path) -> str:
    if not config_path(home).exists():
        _fail(f"no node.toml in {home}; run `agentos node init` first (or set AGENTOS_HOME)")
    return load_config(home).name


def _emit(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _mib(n: int) -> str:
    return f"{n / 1024**2:.1f} MiB"


# ---- passphrases ----------------------------------------------------------------------

def read_secret_file(path: Path, what: str = "passphrase file") -> str:
    """A secret from a file only its owner can read (chmod 600), without its final newline."""
    try:
        st = os.stat(path)
    except OSError:
        _fail(f"cannot read the {what} {path}")
    if st.st_mode & 0o077:
        _fail(f"the {what} {path} is readable by others (run: chmod 600 {path})")
    text = path.read_text().rstrip("\r\n")
    if not text:
        _fail(f"the {what} {path} is empty")
    return text


def read_passphrase(file: Path | None, *, confirm: bool, prompt: str = "Backup passphrase: ",
                    env: str = PASSPHRASE_ENV) -> str:
    path = file or (Path(os.environ[env]) if os.environ.get(env) else None)
    if path is not None:
        return read_secret_file(path)
    if not sys.stdin.isatty():
        _fail(f"no terminal to ask for the passphrase on; pass --passphrase-file or set {env}")
    first = getpass.getpass(prompt)
    if confirm:
        if len(first) < backup.MIN_PASSPHRASE:
            _fail(f"the passphrase must be at least {backup.MIN_PASSPHRASE} characters")
        if getpass.getpass("Repeat the passphrase: ") != first:
            _fail("the passphrases do not match")
    return first


PassFile = Annotated[Path | None, typer.Option("--passphrase-file", help="File holding the passphrase (chmod 600).")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON.")]


# ---- doctor -----------------------------------------------------------------------------

def doctor_cmd(
    as_json: JsonOpt = False,
    offline: Annotated[bool, typer.Option("--offline", help="Skip checks that go through the network.")] = False,
    only: Annotated[str | None, typer.Option(help="Comma-separated check names, for example disk,proxies.")] = None,
    unit: Annotated[str, typer.Option(help="systemd unit of the daemon.")] = "agentosd",
) -> None:
    """Check this node and print what is wrong and how to fix it (read-only, never prints secrets).

    On your computer (no node.toml, but `agentos connect` connections) it checks the connections:
    a connection whose agents still hold the node token is a warning."""
    ctx = doctor.DoctorContext(home=_home(), unit=unit, network=not offline)
    names = [n.strip() for n in only.split(",") if n.strip()] if only else None
    if names is None and not config_path(ctx.home).exists() and doctor.client_connections(ctx):
        names = doctor.CLIENT_CHECKS
        if not as_json:
            typer.echo("This computer is not a node; checking its connections (agentos connect).")
    try:
        report = doctor.run(ctx, names)
    except ValueError as e:
        _fail(str(e))
    if as_json:
        _emit(report.to_dict())
    else:
        typer.echo(doctor.format_text(report, color=sys.stdout.isatty()))
    raise typer.Exit(0 if report.ok else 1)


# ---- backup -----------------------------------------------------------------------------

Target = Annotated[str | None, typer.Option("--to", help="Directory or s3://bucket/prefix (default: "
                                                          "AGENTOS_HOME/backups).")]


def _is_s3(target: str | None) -> bool:
    return bool(target) and str(target).startswith("s3://")


def _s3(target: str, home: Path):
    from agentos.ops.backup.s3 import S3Target

    return S3Target(target, home=home)


@backup_app.command("create")
def backup_create(
    to: Target = None,
    passphrase_file: PassFile = None,
    keep: Annotated[int, typer.Option(help="Keep this many backups of this node (0 keeps all).")] = 7,
    allow_live: Annotated[bool, typer.Option("--allow-live", help="Copy a warm lane while it runs when no daemon "
                                                                   "can pause it.")] = False,
    include_caches: Annotated[bool, typer.Option("--include-caches", help="Keep browser caches too.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Write one encrypted snapshot of AGENTOS_HOME: profiles, fingerprints, vault, secrets, databases."""
    home = _home()
    node = _node_name(home)
    passphrase = read_passphrase(passphrase_file, confirm=True)
    coordinator = backup.DaemonCoordinator(home)
    with _errors():
        if _is_s3(to):
            remote = _s3(str(to), home)
            outgoing = default_backup_dir(home) / "outgoing"
            result = backup.create_backup(home, outgoing, passphrase, node=node, keep=None, coordinator=coordinator,
                                          allow_live=allow_live, include_caches=include_caches)
            try:
                remote.put(result.path.name, result.path)
                if remote.head(result.path.name) != result.size:
                    raise backup.BackupError("the uploaded size does not match; the upload is incomplete")
                result.location = remote.location(result.path.name)
                if keep:
                    result.removed = _prune_s3(remote, node, keep)
            finally:
                result.path.unlink(missing_ok=True)
        else:
            dest = Path(to) if to else default_backup_dir(home)
            result = backup.create_backup(home, dest, passphrase, node=node, keep=keep or None,
                                          coordinator=coordinator, allow_live=allow_live,
                                          include_caches=include_caches)
    if as_json:
        _emit(result.to_dict())
        return
    lanes = ", ".join(f"{lane['id']} ({lane['copied']})" for lane in result.lanes) or "none"
    typer.echo(f"Backup written: {result.location or result.path}")
    typer.echo(f"  {result.files} files, {_mib(result.bytes)} before compression, {_mib(result.size)} encrypted")
    typer.echo(f"  lanes: {lanes}")
    for w in result.warnings:
        typer.secho(f"  warning: {w}", fg=typer.colors.YELLOW)
    if result.skipped:
        typer.echo(f"  skipped {len(result.skipped)} symlink(s) or special file(s); see --json")
    if result.removed:
        typer.echo(f"  removed {len(result.removed)} older backup(s) (keeping {keep})")
    typer.echo("Keep the passphrase safe: without it this backup cannot be opened.")
    if not _is_s3(to) and Path(to or default_backup_dir(home)).resolve().is_relative_to(home.resolve()):
        typer.echo("This backup is on the node's own disk. Copy it elsewhere too, or use --to s3://... .")


def _prune_s3(remote, node: str, keep: int) -> list[str]:
    ours = []
    for obj in remote.list():
        parsed = backup.parse_name(obj.key)
        if parsed and parsed[0] == backup.safe_node(node):
            ours.append((parsed[1], obj.key))
    removed = []
    for _, key in sorted(ours)[:-keep]:
        remote.delete(key)
        removed.append(remote.location(key))
    return removed


@contextmanager
def _local_archive(archive: str, home: Path) -> Iterator[Path]:
    if not _is_s3(archive):
        yield Path(archive)
        return
    remote_dir, _, name = archive.rpartition("/")
    remote = _s3(remote_dir, home)
    with tempfile.TemporaryDirectory(prefix="agentos-restore-") as tmp:
        os.chmod(tmp, 0o700)
        local = Path(tmp) / name
        remote.get(name, local)
        yield local


@backup_app.command("verify")
def backup_verify(
    archive: Annotated[str, typer.Argument(help="A backup file, or s3://bucket/prefix/<file>.")],
    passphrase_file: PassFile = None,
    as_json: JsonOpt = False,
) -> None:
    """Decrypt and read a whole backup: every checksum, the manifest, and each database's integrity."""
    passphrase = read_passphrase(passphrase_file, confirm=False)
    with _errors(), _local_archive(archive, _home()) as path:
        contents = backup.verify_backup(path, passphrase)
    summary = contents.summary()
    if as_json:
        _emit({"ok": True, **summary})
        return
    typer.echo(f"OK: {archive}")
    typer.echo(f"  node {summary['node']}, made {summary['created_at']} by agentos {summary['agentos_version']}")
    typer.echo(f"  {summary['files']} files ({_mib(summary['bytes'])}), checksums match the manifest")
    typer.echo(f"  lanes: {', '.join(lane['id'] for lane in summary['lanes']) or 'none'}")
    typer.echo(f"  databases intact: {', '.join(summary['sqlite_checked']) or 'none'}")


@backup_app.command("restore")
def backup_restore(
    archive: Annotated[str, typer.Argument(help="A backup file, or s3://bucket/prefix/<file>.")],
    to: Annotated[Path | None, typer.Option("--to", help="Home to restore into (default AGENTOS_HOME).")] = None,
    force: Annotated[bool, typer.Option("--force", help="Move a non-empty home aside and restore anyway.")] = False,
    owner: Annotated[str | None, typer.Option(help="Give the restored files to this user (root only), for "
                                                   "example agentos.")] = None,
    passphrase_file: PassFile = None,
    as_json: JsonOpt = False,
) -> None:
    """Restore a backup onto an empty home (the node comes up with the same lanes, still logged in)."""
    home = to or _home()
    passphrase = read_passphrase(passphrase_file, confirm=False)
    with _errors(), _local_archive(archive, home) as path:
        result = backup.restore_backup(path, passphrase, home, force=force, owner=owner)
    if as_json:
        _emit(result.to_dict())
        return
    typer.echo(f"Restored {archive} into {result.home}")
    typer.echo(f"  node {result.node}, backup made {result.created_at}; {result.files} files ({_mib(result.bytes)})")
    typer.echo(f"  lanes: {', '.join(result.lanes) or 'none'}")
    if result.moved_aside:
        typer.echo(f"  the previous home is kept at {result.moved_aside}")
    if result.permission_problems:
        for p in result.permission_problems:
            typer.secho(f"  permissions: {p}", fg=typer.colors.YELLOW)
    else:
        typer.echo("  permissions: every directory 700, every file 600")
    typer.echo("Next: agentos doctor, then start agentosd (sudo systemctl start agentosd).")


@backup_app.command("list")
def backup_list(to: Target = None, as_json: JsonOpt = False) -> None:
    """Backups in a directory or an S3 prefix, oldest first."""
    home = _home()
    with _errors():
        if _is_s3(to):
            remote = _s3(str(to), home)
            rows = [{"path": remote.location(o.key), "size": o.size, "created_at": parsed[1].isoformat(),
                     "node": parsed[0]} for o in remote.list() if (parsed := backup.parse_name(o.key))]
            rows.sort(key=lambda r: r["created_at"])
        else:
            rows = [a.to_dict() for a in backup.list_archives(Path(to) if to else default_backup_dir(home))]
    if as_json:
        _emit(rows)
        return
    if not rows:
        typer.echo("No backups. Make one with `agentos backup create`.")
        return
    for r in rows:
        typer.echo(f"{r['created_at']}  {_mib(r['size']).rjust(10)}  {r['path']}")


@backup_app.command("prune")
def backup_prune(
    keep: Annotated[int, typer.Option(help="How many of this node's backups to keep.")],
    to: Target = None,
) -> None:
    """Delete all but the newest KEEP backups of this node (and stale partial files)."""
    home = _home()
    node = _node_name(home)
    with _errors():
        if _is_s3(to):
            removed = _prune_s3(_s3(str(to), home), node, keep)
        else:
            removed = [str(p) for p in backup.prune(Path(to) if to else default_backup_dir(home), keep, node)]
    typer.echo(f"Removed {len(removed)} file(s)." if removed else "Nothing to remove.")
    for r in removed:
        typer.echo(f"  {r}")


# ---- migrate --------------------------------------------------------------------------

@migrate_app.command("status")
def migrate_status(as_json: JsonOpt = False) -> None:
    """Schema version of each package's database and the format of each lane, and what is pending."""
    with _errors():
        statuses = migrate.status(_home())
        lanes = lanefmt.lane_status(_home(), browser_pids=Host().browser_pids)
    if as_json:
        _emit({"databases": [s.to_dict() for s in statuses], "lanes": [s.to_dict() for s in lanes],
               "lane_format": lanefmt.LATEST})
        return
    for s in statuses:
        state = ("missing" if not s.exists else "NEWER than this agentos" if s.newer
                 else f"{len(s.pending)} pending" if s.pending else "current")
        typer.echo(f"{s.package:<10} {s.db:<16} v{s.current} of v{s.latest}  {state}")
    for ln in lanes:
        state = (f"broken: {ln.error}" if ln.error else "NEWER than this agentos" if ln.newer
                 else "not stamped yet" if not ln.stamped
                 else f"{len(ln.pending)} pending" if ln.pending else "current")
        busy = f" (in use: {ln.in_use})" if ln.in_use else ""
        typer.echo(f"lane       {ln.lane:<27} v{ln.current} of v{ln.latest}  {state}{busy}")


@migrate_app.command("run")
def migrate_run(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would run.")] = False,
    no_backup: Annotated[bool, typer.Option("--no-backup", help="Skip the databases' pre-migration backup.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Apply pending migrations: the databases (backed up first), then every cold lane's format
    (what a step changes is backed up first; a warm lane is left for the next run)."""
    result: migrate.MigrateResult | None = None
    try:
        result = migrate.migrate(_home(), backup=not no_backup, dry_run=dry_run)
        lanes = lanefmt.migrate_lanes(_home(), dry_run=dry_run, browser_pids=Host().browser_pids)
    except (migrate.MigrationError, lanefmt.LaneFormatError) as e:
        if as_json:
            # backup_dir is always the databases' (the upgrade's rollback restores it)
            db_backup = result.backup_dir if result is not None else getattr(e, "backup_dir", None)
            lane_backup = e.backup_dir if isinstance(e, lanefmt.LaneFormatError) else None
            _emit({"ok": False, "error": str(e), "backup_dir": str(db_backup) if db_backup else None,
                   "lanes_backup_dir": str(lane_backup) if lane_backup else None})
            raise typer.Exit(1) from None
        _fail(str(e))
    if as_json:
        _emit({"ok": True, "dry_run": dry_run, **result.to_dict(), "lanes": lanes.to_dict(),
               "lanes_backup_dir": str(lanes.backup_dir) if lanes.backup_dir else None})
        return
    if dry_run:
        pending = [f"{s.package} {s.db}: {', '.join(s.pending)}" for s in result.statuses if s.pending]
        pending += [f"lane {s.lane}: {', '.join(s.pending)}" + (f" (in use now: {s.in_use})" if s.in_use else "")
                    for s in lanes.statuses if s.pending]
        typer.echo("Would apply:\n  " + "\n  ".join(pending) if pending else "Nothing to migrate.")
        return
    if not result.applied and not lanes.applied and not lanes.skipped:
        typer.echo("Nothing to migrate: every database and lane is current.")
        return
    for package, version, name in result.applied:
        typer.echo(f"applied {package} {version}: {name}")
    if result.backup_dir:
        typer.echo(f"The databases before this run are in {result.backup_dir}")
    for lane, version, name in lanes.applied:
        typer.echo(f"applied lane {lane} format {version}: {name}")
    if lanes.backup_dir:
        typer.echo(f"What the lane steps changed is backed up in {lanes.backup_dir}")
    for lane, why in lanes.skipped:
        typer.echo(f"skipped lane {lane}: {why}. Close it and run `agentos migrate run` again.")


# ---- upgrade --------------------------------------------------------------------------

def upgrade_cmd(
    source: Annotated[Path, typer.Option(help="The new agentos checkout (after git pull).", exists=True,
                                         file_okay=False)] = Path("."),
    prefix: Annotated[Path | None, typer.Option(help="Install prefix (default: this agentos's own).")] = None,
    no_service: Annotated[bool, typer.Option("--no-service", help="A dev install: no systemd.")] = False,
    user: Annotated[str | None, typer.Option(help="Run agentosd as this system user (installer --user).")] = None,
    hardened: Annotated[bool, typer.Option("--hardened", help="Install the hardened unit.")] = False,
    unit: Annotated[str, typer.Option(help="systemd unit.")] = "agentosd",
    health_timeout: Annotated[float, typer.Option(help="Seconds to wait for the new version.")] = 90.0,
    skip_apt: Annotated[bool, typer.Option("--skip-apt", help="Pass --skip-apt to the installer.")] = False,
    skip_browser: Annotated[bool, typer.Option("--skip-browser", help="Pass --skip-browser to the installer.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Drain lanes, install the new code, migrate, restart, and roll back if it is not healthy."""
    from agentos.ops import upgrade

    home = _home()
    _node_name(home)
    if prefix is None:
        venv = Path(sys.prefix)
        prefix = venv.parent if venv.name == ".venv" else venv
    plan = upgrade.UpgradePlan(source=source.resolve(), prefix=prefix.resolve(), home=home, unit=unit,
                               service=not no_service, user=user, hardened=hardened, health_timeout=health_timeout,
                               installer_flags=[f for f, on in (("--skip-apt", skip_apt), ("--skip-browser", skip_browser))
                                                if on])
    if not yes:
        from agentos.ops.restart import confirm_line, open_lanes, warn_held

        lanes = open_lanes(home)
        warn_held(lanes)
        what = f"stop {unit}, " if plan.service else ""
        typer.confirm(f"Upgrade {plan.prefix} from {plan.source}? This will {what}drain lanes, install, migrate "
                      f"and restart.{confirm_line(lanes)}", abort=True)
    log = (lambda m: typer.echo(m, err=True)) if as_json else typer.echo
    result = upgrade.run_upgrade(plan, upgrade.SystemSteps(log=log), log=log)
    if as_json:
        _emit(result.to_dict())
    elif result.ok:
        typer.echo(f"Upgraded. The previous version is kept at {plan.snapshot_dir} for a manual rollback.")
    elif result.state == "rolled_back":
        typer.secho(f"Rolled back to the previous version: {result.error}", fg=typer.colors.YELLOW)
    elif result.state == "unchanged":
        typer.secho(f"Nothing changed: {result.error}", fg=typer.colors.YELLOW)
    else:
        typer.secho(f"The node is not healthy after the upgrade and the rollback: {result.error}. "
                    f"The previous code is in {plan.snapshot_dir}; see journalctl -u {unit}.", fg=typer.colors.RED)
    raise typer.Exit(0 if result.ok else 1)


# ---- service user ---------------------------------------------------------------------

def migrate_service_user_cmd(
    user: Annotated[str, typer.Option(help="The system user agentosd runs as after the switch.")] = "agentos",
    rollback: Annotated[bool, typer.Option("--rollback", help="Undo an earlier switch: back to root.")] = False,
    prefix: Annotated[Path | None, typer.Option(help="Install prefix (default: this agentos's own).")] = None,
    unit: Annotated[str, typer.Option(help="systemd unit.")] = "agentosd",
    health_timeout: Annotated[float, typer.Option(help="Seconds to wait for agentosd after the start.")] = 90.0,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Run agentosd as its own user with the hardened unit instead of root (or --rollback)."""
    from agentos.ops import service_user

    home = _home()
    _node_name(home)
    if prefix is None:
        venv = Path(sys.prefix)
        prefix = venv.parent if venv.name == ".venv" else venv
    plan = service_user.SwitchPlan(prefix=prefix.resolve(), home=home, user=user, unit=unit,
                                   health_timeout=health_timeout)
    if not yes:
        from agentos.ops.restart import confirm_line, open_lanes, warn_held

        lanes = open_lanes(home)
        warn_held(lanes)
        what = (f"Put {unit} back on root and give {home} back to its old owner?" if rollback else
                f"Switch {unit} from root to the user {user} with the hardened unit? Warm lanes close, "
                f"{home} is given to {user}, and {unit} restarts.")
        typer.confirm(what + confirm_line(lanes), abort=True)
    log = (lambda m: typer.echo(m, err=True)) if as_json else typer.echo
    steps = service_user.SystemSteps(log=log)
    result = (service_user.run_rollback if rollback else service_user.run_switch)(plan, steps, log=log)
    if as_json:
        _emit(result.to_dict())
    elif result.ok and rollback:
        typer.echo(f"{unit} runs as root again.")
    elif result.ok:
        typer.echo(f"{unit} runs as {user} with the hardened unit. To undo: sudo agentos migrate-service-user "
                   f"--rollback (the saved state is {plan.state_path}).")
    elif result.state == "rolled_back":
        typer.secho(f"Rolled back to root: {result.error}", fg=typer.colors.YELLOW)
    elif result.state == "unchanged":
        typer.secho(f"Nothing changed: {result.error}", fg=typer.colors.YELLOW)
    else:
        typer.secho(f"{unit} is not healthy: {result.error}. The saved state is {plan.state_path}; "
                    f"see journalctl -u {unit}.", fg=typer.colors.RED)
    raise typer.Exit(0 if result.ok else 1)


# ---- health ---------------------------------------------------------------------------

def _print_health(report: dict[str, Any]) -> None:
    typer.echo(f"health {report.get('level')} at {report.get('at')}")
    for f in report.get("findings", []):
        typer.echo(f"  {f['level'].upper():<8} {f['title']}: {f['detail']}")


@health_app.command("check")
def health_check(
    offline: Annotated[bool, typer.Option("--offline", help="Skip proxy checks.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Run the health checks now (through the daemon when it runs, so warm lanes are checked too)."""
    home = _home()
    _node_name(home)
    resp = None if offline else Host().unix_request(ops_socket(home), {"op": "check"}, timeout=120)
    if resp and resp.get("ok"):
        report = resp["status"]
    else:
        import asyncio

        from agentos.ops.health import HealthMonitor

        report = asyncio.run(HealthMonitor(load_config(home)).run_once(proxies=not offline, lanes=False))
        report["note"] = "run without the daemon: warm lanes were not checked"
    if as_json:
        _emit(report)
        return
    _print_health(report)
    if report.get("note"):
        typer.echo(f"  ({report['note']})")


@health_app.command("status")
def health_status(as_json: JsonOpt = False) -> None:
    """The daemon's last health report."""
    resp = Host().unix_request(ops_socket(_home()), {"op": "status"}, timeout=10)
    if not resp or not resp.get("ok"):
        _fail("the daemon's ops socket does not answer (is agentosd running with the health service?)")
    if as_json:
        _emit(resp["status"])
        return
    _print_health(resp["status"])


# ---- lane refresh-fingerprint ------------------------------------------------------------

def refresh_fingerprint_cmd(
    lane: Annotated[str, typer.Argument(help="workspace/name.")],
    keep_device: Annotated[bool, typer.Option("--keep-device", help="Change only the browser version fields "
                                                                    "(required).")] = False,
    version: Annotated[int | None, typer.Option(help="Firefox major version (default: the installed "
                                                     "Camoufox's).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the change without writing it.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="For a lane on a device: change the device's pin, for every "
                                                    "lane on it.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Bump the browser version in a lane's pinned fingerprint to the installed Camoufox, keeping
    every other pinned value (navigator.userAgent and headers.User-Agent change; nothing else).
    For a lane on a device the change is the device's, so it needs --yes, after the list of every
    lane it affects."""
    try:
        parse_lane_id(lane)
    except ValueError as e:
        _fail(str(e))
    if not keep_device:
        _fail("pass --keep-device: only the browser version changes and the lane stays the same device. A new "
              "device (deleting fingerprint.json) makes sites see a new computer.")
    home = _home()
    cfg = load_config(home)
    from agentos.ops.layout import lane_pin_path, lane_specs

    specs = lane_specs(home)
    device = specs[lane].device if lane in specs else None
    path = lane_pin_path(home, lane, specs)  # a lane on a device: the device's pin (AUD-02)
    if not path.exists():
        _fail(f"{lane} has no pinned fingerprint yet ({path}); it is pinned on first launch")
    host = Host()
    if version is None:
        cache = home / ".cache"
        info = host.camoufox(cache if (cache / "camoufox").exists() else None)
        version = fingerprint.browser_major(info.get("verstr"))
        if version is None:
            _fail(f"cannot tell the installed Camoufox version ({info.get('error', 'unknown')}); pass --version")
    on_device = sorted(i for i, spec in specs.items() if device and spec.device == device)
    if device and not dry_run and not yes:
        try:
            would = fingerprint.refresh(path, lane, version, dry_run=True)
        except (OSError, ValueError, TypeError) as e:
            _fail(f"cannot read {path}: {type(e).__name__}")
        if would.changed:
            _fail(f"{lane} is on device {device}, so this changes the device's pin, for every lane on it: "
                  f"{', '.join(on_device)} (Firefox {would.old} to {would.new}; nothing else). Nothing changed. "
                  f"If you want it for all of them: agentos lane refresh-fingerprint {lane} --keep-device --yes")
    try:
        result = fingerprint.refresh(path, lane, version, dry_run=dry_run)
    except (OSError, ValueError, TypeError) as e:
        _fail(f"cannot update {path}: {type(e).__name__}")
    running = bool(host.browser_pids(cfg.profile_dir(lane)))
    if device and result.changed and not dry_run:
        raw = path.read_bytes()
        for other in on_device:  # each lane on the device keeps a copy of its pin
            if cfg.lane_dir(other).exists():
                fingerprint.write_private(cfg.fingerprint_path(other), raw)
        running = running or any(host.browser_pids(cfg.profile_dir(other)) for other in on_device)
    if as_json:
        _emit({**result.to_dict(), "running": running})
        return
    if not result.changed:
        typer.echo(f"{lane} already announces Firefox {version}; nothing to change.")
        return
    verb = "Would change" if dry_run else "Changed"
    if device:
        typer.echo(f"{lane} is on device {device}: the change is the device's, for {', '.join(on_device)}.")
    typer.echo(f"{verb} {lane}: Firefox {result.old} -> {result.new} in {', '.join(result.changed)}; every "
               "other pinned value is unchanged.")
    if result.backup:
        typer.echo(f"  the old fingerprint is kept at {result.backup}")
    if running and not dry_run:
        typer.echo("  the lane is warm: the change takes effect the next time it warms (lane_close, then open it).")


# ---- lane check ---------------------------------------------------------------------------

def lane_check_cmd(
    lane: Annotated[str | None, typer.Argument(help="workspace/name; leave out with --all.")] = None,
    all_lanes: Annotated[bool, typer.Option("--all", help="Check every lane on this node.")] = False,
    offline: Annotated[bool, typer.Option("--offline", help="Do not probe proxies (no network at all).")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Is this lane safe to use right now? A checklist per lane: proxy, exit IP and its location,
    WebRTC and DNS leaks, profile and pin, signed in (from the cookies already in the profile), the
    expected account, and any hold. Never warms a lane, opens a page or contacts an account's site.
    Exits 1 when any lane fails a check."""
    from agentos.stealth.lanecheck import LaneChecker, LaneReport, format_report

    if (lane is None) == (not all_lanes):
        _fail("name one lane (agentos lane check biz-a/instagram) or pass --all")
    if lane is not None:
        try:
            parse_lane_id(lane)
        except ValueError as e:
            _fail(str(e))
    home = _home()
    _node_name(home)
    payload: dict[str, Any] = {"op": "lane_check", **({"lane": lane} if lane else {})}
    resp = None if offline else Host().unix_request(ops_socket(home), payload, timeout=120)
    note = None
    if resp and resp.get("ok"):
        reports = resp["reports"]
    elif resp and isinstance(resp.get("error"), dict) and resp["error"].get("code") != "invalid":
        _fail(f"{resp['error'].get('code')}: {resp['error'].get('message')}")
    else:
        import asyncio

        checker = LaneChecker(load_config(home), network=not offline, write_cache=False)
        with _errors():
            found = asyncio.run(checker.check(lane) if lane else checker.check_all())
        reports = [r.to_dict() for r in (found if isinstance(found, list) else [found])]
        note = ("--offline: every lane was read from disk as cold, and no proxy was probed" if offline
                else "agentosd is not running: every lane was read from disk as cold")
    if as_json:
        _emit({"lanes": reports, "ok": all(r["level"] != "fail" for r in reports),
               **({"note": note} if note else {})})
    else:
        if not reports:
            typer.echo("No lanes. Create one with `agentos lane create <workspace/name>`.")
        color = sys.stdout.isatty()
        for i, r in enumerate(reports):
            if i:
                typer.echo("")
            typer.echo(format_report(LaneReport.from_dict(r), color=color))
        if note:
            typer.echo(f"\n({note})")
    raise typer.Exit(1 if any(r["level"] == "fail" for r in reports) else 0)


# ---- lane health --------------------------------------------------------------------------

def lane_health_cmd(
    lane: Annotated[str | None, typer.Argument(help="workspace/name; leave out with --all.")] = None,
    all_lanes: Annotated[bool, typer.Option("--all", help="Every lane on this node.")] = False,
    days: Annotated[int, typer.Option("--days", min=1, max=365, help="Count challenges and quarantines over "
                                                                     "this many days.")] = 30,
    fresh: Annotated[bool, typer.Option("--fresh", help="Run the lane check now instead of using its last "
                                                        "run.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """One grade per lane from the checks agentos already runs: Looks normal, Check this or Stop, with
    countable facts and each finding's fix. It reports consistency, leaks and the challenges sites
    actually showed. It cannot see a site's own risk model, the exit IP's reputation or whether a
    site's terms allow automation (docs/RESPONSIBLE-USE.md). Exits 1 when any lane grades Stop."""
    from agentos.stealth import health
    from agentos.stealth.lanecheck import LaneChecker

    if (lane is None) == (not all_lanes):
        _fail("name one lane (agentos lane health biz-a/instagram) or pass --all")
    if lane is not None:
        try:
            parse_lane_id(lane)
        except ValueError as e:
            _fail(str(e))
    home = _home()
    payload: dict[str, Any] = {"op": "lane_health", "days": days, "fresh": fresh, **({"lane": lane} if lane else {})}
    resp = Host().unix_request(ops_socket(home), payload, timeout=120)  # agentosd (or agentos dev up) knows best
    note = None
    if resp and resp.get("ok"):
        found = resp["lanes"]
    elif resp and isinstance(resp.get("error"), dict) and resp["error"].get("code") != "invalid":
        _fail(f"{resp['error'].get('code')}: {resp['error'].get('message')}")
    else:
        import asyncio

        _node_name(home)
        checker = LaneChecker(load_config(home), write_cache=False)
        with _errors():
            made = asyncio.run(health.gather(checker, lane, days=days, fresh=True) if lane
                               else health.gather_all(checker, days=days, fresh=True))
        found = [h.to_dict() for h in (made if isinstance(made, list) else [made])]
        note = "agentosd is not running: every lane was read from disk as cold, and checked now"
    if as_json:
        _emit({"lanes": found, "days": days, "cannot_see": list(health.CANNOT_SEE), "docs": health.DOCS,
               **({"note": note} if note else {})})
    else:
        if not found:
            typer.echo("No lanes. Create one with `agentos lane create <workspace/name>`.")
        for i, h in enumerate(found):
            if i:
                typer.echo("")
            typer.echo(health.format_health(h))
        if found:
            typer.echo(f"\nWhat this cannot see: {' '.join(health.CANNOT_SEE)} See {health.DOCS}.")
        if note:
            typer.echo(f"\n({note})")
    raise typer.Exit(1 if any(h["grade"] == health.STOP for h in found) else 0)


# ---- wiring -----------------------------------------------------------------------------
# Typer apps for the root CLI's mount table (module, attr, command):
#   doctor_app -> "doctor", backup_app -> "backup", migrate_app -> "migrate",
#   upgrade_app -> "upgrade", health_app -> "health"; and lane_app is added to the root's
#   `lane` group without a name (`lane_app.add_typer(ops_cli.lane_app)`), so its command
#   becomes `agentos lane refresh-fingerprint`. `mount()` does all of that in one call.

app.command("doctor")(doctor_cmd)
app.add_typer(backup_app, name="backup")
app.add_typer(migrate_app, name="migrate")
app.command("upgrade")(upgrade_cmd)
app.command("migrate-service-user")(migrate_service_user_cmd)
app.add_typer(health_app, name="health")
app.command("refresh-fingerprint")(refresh_fingerprint_cmd)
app.command("lane-check")(lane_check_cmd)
app.command("lane-health")(lane_health_cmd)

def logs_cmd(
    lane: Annotated[str | None, typer.Option(help="Only this lane (a glob works: 'biz-a/*').")] = None,
    grant: Annotated[str | None, typer.Option(help="Only lines of calls made with this grant id.")] = None,
    run: Annotated[str | None, typer.Option(help="Only lines of this run.")] = None,
    workspace: Annotated[str | None, typer.Option(help="Only this workspace.")] = None,
    request: Annotated[str | None, typer.Option(help="Only this request id (rq_...).")] = None,
    since: Annotated[str | None, typer.Option(help="From this long ago (10m, 2h, 1d) or an ISO time.")] = None,
    level: Annotated[str, typer.Option(help="The lowest level shown: debug, info, warn or error.")] = "info",
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep printing new lines.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="One JSON object per line, as stored.")] = False,
    source: Annotated[str, typer.Option(help="auto (journald when agentosd runs under systemd), journal or "
                                             "file.")] = "auto",
    unit: Annotated[str, typer.Option(help="The systemd unit to read from journald.")] = logs.UNIT,
) -> None:
    """agentosd's log, filtered on the ids every line carries (lane, grant, run, workspace, request)."""
    from agentos.core.logs import human_line

    try:
        query = logs.Query(lane=lane, grant=grant, run=run, workspace=workspace, request=request,
                           since=logs.parse_since(since) if since else None, level=logs.parse_level(level))
    except ValueError as e:
        _fail(str(e), 2)
    if source not in ("auto", "journal", "file"):
        _fail("--source is auto, journal or file", 2)
    use_journal = source == "journal" or (source == "auto" and logs.journal_available(unit))
    if use_journal:
        if shutil.which("journalctl") is None:
            _fail("journalctl is not installed here; use --source file")
        lines = logs.journal_lines(unit, query.since, follow)
    else:
        home = home_from_env()
        if not follow and not logs.log_files(home):
            _fail(f"no log in {logs.log_dir(home)} and no journal for {unit}; is agentosd running here?")
        lines = logs.file_lines(home, follow)
    try:
        for line in logs.select(lines, query):
            typer.echo(json.dumps(line, ensure_ascii=False) if as_json else human_line(line))
    except (KeyboardInterrupt, BrokenPipeError):
        return


doctor_app = typer.Typer(invoke_without_command=True, help="Check this node and say how to fix what is wrong.")
doctor_app.callback()(doctor_cmd)
upgrade_app = typer.Typer(invoke_without_command=True, help="Drain, install, migrate, restart; roll back on failure.")
upgrade_app.callback()(upgrade_cmd)
# ---- stealth -------------------------------------------------------------------------------

def lane_audit_cmd(as_json: JsonOpt = False) -> None:
    """Does every lane pin one consistent, ordinary device, and do any two lanes pin the same one?
    Reads the pinned fingerprints on disk only: no browser starts and nothing is sent (safe on a
    node with live lanes). Exits 1 when a pin fails. docs/design/stealth.md lists the rules."""
    from agentos.stealth import stealth

    home = _home()
    _node_name(home)
    cache = home / ".cache"
    installed = fingerprint.browser_major(Host().camoufox(cache if (cache / "camoufox").exists() else None)
                                          .get("verstr"))
    findings, count = stealth.audit_pins(home, installed_major=installed)
    level = stealth.worst(findings)
    if as_json:
        _emit({"level": level, "pins": count, "installed_firefox": installed,
               "findings": [f.to_dict() for f in findings]})
    else:
        typer.echo(f"{count} pinned lanes, installed Firefox {installed or 'unknown'}: {level}")
        for line in stealth.format_findings(findings):
            typer.echo(line)
    if level == "fail":
        raise typer.Exit(1)


def stealth_check_cmd(
    slot: Annotated[int, typer.Option(help="Test slot (11 to 59): displays :S0 to :S9, VNC ports 5000 + 10S, viewer "
                                           "ports 6000 + 10S, as scripts/node-test.sh hands them out.")],
    lanes: Annotated[int, typer.Option(min=1, max=8, help="Scratch lanes to open.")] = 3,
    relaunches: Annotated[int, typer.Option(min=0, help="Relaunches of each lane after the first.")] = 2,
    os_names: Annotated[str, typer.Option("--os", help="Identity OSes to cycle over the lanes, comma separated "
                                                       "(windows, macos, linux); empty: the node default, lane_os (Linux).")] = "",
    soak_minutes: Annotated[float, typer.Option("--soak-minutes", help="Cycle the lanes for this long, killing a "
                                                                      "browser now and then (0: no soak).")] = 0.0,
    kill_every: Annotated[float, typer.Option("--kill-every", help="Seconds between kills in the soak.")] = 60.0,
    public: Annotated[bool, typer.Option("--public", help="Also visit public fingerprint test pages (CreepJS, "
                                                          "Sannysoft, BrowserScan, Pixelscan) from the first "
                                                          "lane; screenshots go in --out.")] = False,
    proxy: Annotated[str | None, typer.Option(help="A test proxy for every scratch lane, host:port[:user[:pass]] "
                                                   "(never a production lane's).")] = None,
    min_available_mb: Annotated[int, typer.Option("--min-available-mb", help="Stop before memory available on "
                                                                             "this machine falls under this.")] = 2048,
    out: Annotated[Path | None, typer.Option(help="Folder for report.json, report.txt, every dump and the "
                                                  "public pages' screenshots.")] = None,
    home_root: Annotated[Path | None, typer.Option("--home-root", help="Where the throwaway AGENTOS_HOME is made "
                                                                       "(default: the system temp folder).")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Open scratch lanes (no account) in a throwaway home on a test slot and check, the way a page's
    own script sees them: automation traces, one consistent device, the same device on every launch,
    no two lanes alike, no WebRTC leak; plus memory per warm lane, wake times and, with --soak-minutes,
    lanes cycling while browsers are killed. Never touches this node's own lanes, home or ports.
    Exits 1 when a check fails. docs/design/stealth.md says what each finding means."""
    import asyncio

    from agentos.cli import parse_proxy
    from agentos.stealth import stealthcheck

    if not sys.platform.startswith("linux"):
        _fail("the stealth check runs on a Linux node (it starts Xvfb and Camoufox)")
    if shutil.which("Xvfb") is None:
        _fail("Xvfb is not installed")
    root = home_root or Path(tempfile.gettempdir())
    try:  # every node on this machine (agentosd's home, AGENTOS_HOME, /var/lib/agentos) keeps its slots
        test_slot = stealthcheck.slot(slot, stealthcheck.node_configs(), home=root)
    except ValueError as e:
        _fail(str(e))
    names = [n.strip() for n in os_names.split(",") if n.strip()]
    unknown = sorted(set(names) - {"windows", "macos", "linux"})
    if unknown:
        _fail(f"unknown OS {', '.join(unknown)}: use windows, macos or linux")
    test_proxy = parse_proxy("stealth-test", proxy) if proxy else None
    home = Path(tempfile.mkdtemp(prefix=stealthcheck.SCRATCH_PREFIX, dir=root))
    stealthcheck.claim_home(home)
    opts = stealthcheck.Options(lanes=lanes, relaunches=relaunches, os_names=names, public=public,
                                soak_minutes=soak_minutes, kill_every=kill_every,
                                min_available_mb=min_available_mb, out=out)
    lab = stealthcheck.NodeLab(home, test_slot, proxy=test_proxy)

    def say(line: str) -> None:
        if as_json:
            return
        with contextlib.suppress(OSError):  # a closed terminal must not stop the lanes from closing
            typer.echo(line, err=True)

    try:
        with _errors():
            report = asyncio.run(stealthcheck.run_until_stopped(lab, opts, say=say))
    except stealthcheck.Stopped as e:
        say(f"{e}; the scratch lanes are closed")
        raise typer.Exit(128 + e.signum) from None
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if as_json:
        _emit(report.to_dict())
    else:
        typer.echo(stealthcheck.format_report(report))
    if report.level == "fail":
        raise typer.Exit(1)


def redteam_cmd(
    local: Annotated[bool, typer.Option("--local", help="Plain Camoufox on this machine (macOS works): the agent "
                                                        "input path, not the node's pins, Xvfb or proxy.")] = False,
    slot: Annotated[int | None, typer.Option(help="Node test slot (11 to 59) for scratch lanes with the "
                                                  "production engine and pins (Linux node).")] = None,
    lanes: Annotated[int, typer.Option(min=1, max=8, help="Lanes, each its own device.")] = 1,
    runs: Annotated[int, typer.Option(min=1, max=50, help="Visits per lane (pooled detectors want 3 or more).")] = 3,
    os_names: Annotated[str, typer.Option("--os", help="Identity OSes to cycle over the lanes, comma separated "
                                                       "(windows, macos, linux); empty: the default.")] = "",
    humans: Annotated[list[Path] | None, typer.Option("--humans", help="Human baseline sessions (files or folders "
                                                                       "from --serve) to compare with and to "
                                                                       "check the detectors against.")] = None,
    serve: Annotated[int | None, typer.Option("--serve", help="Serve the detector site on this port for a person "
                                                              "to record a baseline; Ctrl-C saves it.")] = None,
    host: Annotated[str, typer.Option(help="Address for --serve (0.0.0.0 to reach it from another device).")]
    = "127.0.0.1",
    probe: Annotated[bool | None, typer.Option("--probe/--no-probe", help="Read the stealth probe page in each "
                                                                         "lane (default: on a node, off with "
                                                                         "--local, where the window is not "
                                                                         "placed as the engine places it).")]
    = None,
    gauntlet: Annotated[bool, typer.Option("--gauntlet", help="Instead of our detector site: one scratch lane "
                                                              "through --proxy-file visits public bot-detection "
                                                              "panels and sites behind Cloudflare, DataDome, Akamai "
                                                              "and F5 (docs/design/redteam.md).")] = False,
    targets: Annotated[str, typer.Option(help="Gauntlet target kinds or names, comma separated: panel, tls, protected, "
                                              "content (default: all).")] = "",
    proxy_file: Annotated[Path | None, typer.Option("--proxy-file", help="A file holding host:port[:user[:pass]] "
                                                                        "(kept out of the command line).")] = None,
    no_record: Annotated[bool, typer.Option("--no-record", help="Do not append to bench/redteam.jsonl.")] = False,
    out: Annotated[Path | None, typer.Option(help="Folder for report.json, report.txt and every session.")] = None,
    home_root: Annotated[Path | None, typer.Option("--home-root", help="Where the node's throwaway AGENTOS_HOME is "
                                                                       "made (default: the system temp folder).")]
    = None,
    as_json: JsonOpt = False,
) -> None:
    """Try to catch our own lanes as bots. Lanes visit a detector site the way an agent drives them
    (navigate, snapshot, act), and about 30 detectors judge what a vendor's sensor and server would
    see: keystroke and mouse dynamics, scrolling, focus, navigation and headers, claimed versus
    measured hardware, the stealth probe, and lanes linked to each other. Exits 1 when a detector
    catches a visit. docs/design/redteam.md says what each detector means."""
    import asyncio

    from agentos.stealth.redteam import runner
    from agentos.stealth.redteam.server import DetectorServer

    if serve is not None:
        _redteam_serve(serve, host, humans[0] if humans else Path("bench/redteam-humans"))
        return
    if local == (slot is not None):
        _fail("pick one: --local (Camoufox on this machine) or --slot S (scratch lanes on this node)")
    names = [n.strip() for n in os_names.split(",") if n.strip()]
    unknown = sorted(set(names) - {"windows", "macos", "linux"})
    if unknown:
        _fail(f"unknown OS {', '.join(unknown)}: use windows, macos or linux")
    opts = runner.Options(lanes=lanes, runs=runs, os_names=names, humans=runner.load_humans(humans or []),
                          probe=(not local) if probe is None else probe, out=out)

    def say(line: str) -> None:
        if not as_json:
            with contextlib.suppress(OSError):
                typer.echo(line, err=True)

    test_proxy = None
    if proxy_file is not None:
        from agentos.cli import parse_proxy
        try:
            test_proxy = parse_proxy("redteam-test", proxy_file.read_text().strip().splitlines()[0])
        except (OSError, IndexError) as e:
            _fail(f"cannot read the proxy file: {e}")
    if gauntlet and test_proxy is None:
        _fail("the gauntlet visits public sites: give it a proxy (--proxy-file), never the node's own IP")
    if not gauntlet and test_proxy is not None:
        _fail("our detector site is on loopback, which a proxied lane cannot reach; use --proxy-file with --gauntlet")
    home: Path | None = None
    if local:
        lab: Any = runner.LocalLab(proxy=test_proxy)
    else:
        from agentos.stealth import stealthcheck
        if not sys.platform.startswith("linux") or shutil.which("Xvfb") is None:
            _fail("--slot runs on a Linux node with Xvfb; use --local elsewhere")
        root = home_root or Path(tempfile.gettempdir())
        try:
            test_slot = stealthcheck.slot(slot, stealthcheck.node_configs(), home=root)
        except ValueError as e:
            _fail(str(e))
        home = Path(tempfile.mkdtemp(prefix=stealthcheck.SCRATCH_PREFIX, dir=root))
        stealthcheck.claim_home(home)
        lab = runner.NodeBench(stealthcheck.NodeLab(home, test_slot, proxy=test_proxy))
    if gauntlet:
        kinds = {k.strip() for k in targets.split(",") if k.strip()} or None
        try:
            with _errors():
                result = asyncio.run(runner.run_gauntlet(lab, kinds=kinds, out=out, say=say))
        finally:
            if home is not None:
                shutil.rmtree(home, ignore_errors=True)
        if as_json:
            _emit({k: v for k, v in result.items() if k != "text"})
        else:
            typer.echo(result["text"])
        return
    try:
        with _errors():
            report = asyncio.run(runner.run(lab, opts, say=say, server=DetectorServer().start()))
    finally:
        if home is not None:
            shutil.rmtree(home, ignore_errors=True)
    changes = [] if no_record else runner.compare(runner.record(report), report)
    if as_json:
        _emit({**report.to_dict(), "changes": changes})
    else:
        typer.echo(runner.format_report(report))
        if changes:
            typer.echo("\nsince the last run:\n  " + "\n  ".join(changes))
    if report.summary()["outcome"] == "detected":
        raise typer.Exit(1)


def _redteam_serve(port: int, host: str, folder: Path) -> None:
    """Serve the detector site for a person; save each human visit as a baseline on Ctrl-C."""
    import signal
    import time as _time

    from agentos.stealth.redteam import detectors
    from agentos.stealth.redteam.server import DetectorServer

    server = DetectorServer(host, port).start()
    sid = f"human-{_time.strftime('%Y%m%d-%H%M%S')}"
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    typer.echo(f"Open http://{shown}:{port}/?run={sid}&mode=human in your own browser and follow the yellow "
               "box. Press Ctrl-C here when you are back on the home page.")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()
    _time.sleep(0.5)
    server.stop()
    folder.mkdir(parents=True, exist_ok=True)
    saved = 0
    for name in server.store.ids():
        data = server.store.session(name)
        if not data or not data.get("pages"):
            continue
        data["human"] = True
        (folder / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False))
        saved += 1
        typer.echo(f"\nsaved {folder / (name + '.json')}; what the detectors say about this person:")
        for v in detectors.judge(data):
            if v.level != detectors.SKIPPED:
                typer.echo(f"  {v.level:10} {v.id:24} {v.detail}")
    if not saved:
        typer.echo("nothing was recorded (did the page load from this server?)")


lane_app = typer.Typer()
lane_app.command("redteam")(redteam_cmd)
lane_app.command("refresh-fingerprint")(refresh_fingerprint_cmd)
lane_app.command("check")(lane_check_cmd)
lane_app.command("health")(lane_health_cmd)
lane_app.command("audit")(lane_audit_cmd)
lane_app.command("stealth-check")(stealth_check_cmd)
lane_app.command("keepalive")(keepalive_cmd)


def mount(root: typer.Typer, lane_group: typer.Typer | None = None) -> None:
    """Add the ops commands to the root `agentos` CLI under their documented names (`lane_group`
    is the root's `lane` Typer)."""
    root.add_typer(doctor_app, name="doctor")
    root.add_typer(backup_app, name="backup")
    root.add_typer(migrate_app, name="migrate")
    root.add_typer(upgrade_app, name="upgrade")
    root.command("migrate-service-user")(migrate_service_user_cmd)
    root.add_typer(health_app, name="health")
    root.command("logs")(logs_cmd)
    if lane_group is not None:
        lane_group.add_typer(lane_app)
