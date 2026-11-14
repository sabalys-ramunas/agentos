"""`agentos workspace ...` on this node: workspaces, templates, bulk lanes, the login queue, and moving a
workspace to another node (export and import, see agentos.workspaces.transfer.export).

Works on the node's files directly, the way `agentos lane` does, so it needs no running daemon. New lanes
are made by the node's own lane manager. Whoever runs this has a shell on the node, so its changes count as
the owner's. `apply` shows the plan and asks before it creates anything, unless --yes is given.
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from agentos.core import catalog
from agentos.core.config import NodeConfig, home_from_env, load_config
from agentos.core.errors import AgentosError
from agentos.core.runtime import Runtime
from agentos.workspaces.errors import WorkspaceConflict
from agentos.workspaces.models import Plan
from agentos.workspaces.planner import approval_summary, refusal
from agentos.workspaces.service import WorkspaceService, build

app = typer.Typer(no_args_is_help=True,
                  help="Workspaces: one per business or person, lanes from templates, and the login queue.")

JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")]
WorkspacesOpt = Annotated[list[str] | None, typer.Option(
    "--workspace", "-w", help="A workspace to apply to. Repeat for several.")]
ProxyOpt = Annotated[list[str] | None, typer.Option(
    "--proxy", help="WORKSPACE=LABEL: the proxy for that workspace's lanes. Repeatable.")]
LaneProxyOpt = Annotated[list[str] | None, typer.Option(
    "--lane-proxy", help="LANE=LABEL (or LANE=none): the proxy for one lane. Repeatable.")]
OverrideOpt = Annotated[bool, typer.Option(
    "--override-proxy-rules", help="Create lanes the proxy rules refuse. Needs --reason, and is recorded.")]
ReasonOpt = Annotated[str | None, typer.Option("--reason", help="Why the proxy rules are overridden.")]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Do not ask before creating lanes.")]
OWNER = "owner"


def _fail(message: str, code: str = "error") -> NoReturn:
    typer.secho(catalog.cli_error(code, message), err=True, fg=typer.colors.RED)
    raise typer.Exit(1)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except AgentosError as exc:
        _fail(exc.message, exc.code)


def _config() -> NodeConfig:
    home = home_from_env()
    try:
        return load_config(home)
    except tomllib.TOMLDecodeError as exc:
        _fail(f"{home / 'node.toml'} is not valid TOML ({exc}); fix or remove it", "invalid")
    except ValueError:
        _fail(f"{home / 'node.toml'} has invalid settings; run `agentos node init` to see them", "invalid")


def _lane_manager(cfg: NodeConfig) -> Any:
    """The node's own lane manager, the one `agentos lane` uses. Imported here so that `--help` stays
    fast and the browser stack loads only when a command needs it."""
    from agentos.node import daemon

    return daemon.build_lane_manager(cfg)


def _service() -> WorkspaceService:
    cfg = _config()
    try:
        lanes = _lane_manager(cfg)
    except ModuleNotFoundError as exc:
        _fail(f"missing Python module {exc.name}; run deploy/install-node.sh (or `uv sync`)", "unavailable")
    return build(Runtime(config=cfg, lanes=lanes), register=False)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    cells = [headers, *rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    lines = ["  ".join(text.ljust(width) for text, width in zip(row, widths)).rstrip() for row in cells]
    return "\n".join(lines)


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split())


def _dump(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _pairs(values: list[str] | None, what: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for item in values or []:
        key, sep, label = item.partition("=")
        if not sep or not key or not label:
            _fail(f"{what} takes NAME=LABEL, for example acme=lt1; got {item!r}", "invalid")
        out[key] = None if label == "none" else label
    return out


def _workspace_map(values: list[str] | None, proxies: list[str] | None,
                   lane_proxies: list[str] | None) -> tuple[list[str], dict[str, str | None], dict[str, str | None]]:
    workspaces = [ws.strip() for ws in values or [] if ws.strip()]
    if not workspaces:
        _fail("name at least one workspace with --workspace (-w)", "invalid")
    return workspaces, _pairs(proxies, "--proxy"), _pairs(lane_proxies, "--lane-proxy")


def _print_plan(plan: Plan) -> None:
    rows = [[step.lane, step.platform, step.proxy or "none", step.action,
             _one_line("; ".join(step.reasons or step.overridden or step.warnings))]
            for step in plan.steps]
    typer.echo(_table(["LANE", "PLATFORM", "PROXY", "ACTION", "DETAIL"], rows))
    counts = plan.public()["counts"]
    typer.echo(f"\n{counts['create']} to create, {counts['exists']} already there and matching, "
               f"{counts['conflict']} conflicts")


# ---- workspaces ---------------------------------------------------------------------

@app.command("list")
def list_workspaces(as_json: JsonOpt = False) -> None:
    """Workspaces with their readiness (for example "18 of 29 ready") and health."""
    with _errors():
        views = _service().list_workspaces()
    if as_json:
        return _dump([view.model_dump(mode="json") for view in views])
    if not views:
        return typer.echo("No workspaces yet. Create one with: agentos workspace create <id>")
    rows = [[v.id, v.name, v.summary, v.health, "name from lane ids" if v.derived else ""] for v in views]
    typer.echo(_table(["ID", "NAME", "READY", "HEALTH", "NOTE"], rows))


@app.command("create")
def create_workspace(workspace_id: str, name: str | None = typer.Option(None, help="Display name (default: the id)."),
                     color: str | None = typer.Option(None, help="#rrggbb, for the panel."),
                     proxy: str | None = typer.Option(None, help="Default proxy label for new lanes."),
                     notes: str | None = typer.Option(None, help="Free text about this workspace.")) -> None:
    """Create a workspace. Lanes from templates go into a workspace that exists."""
    with _errors():
        view = _service().create_workspace(workspace_id, name=name, color=color, default_proxy=proxy, notes=notes)
    typer.echo(f"created workspace {view.id} ({view.name})")


@app.command("show")
def show_workspace(workspace_id: str, as_json: JsonOpt = False) -> None:
    """One workspace with its lanes, readiness, login URL and note."""
    with _errors():
        view = _service().get_workspace(workspace_id)
    if as_json:
        return _dump(view.model_dump(mode="json"))
    typer.echo(f"{view.id}: {view.name}")
    typer.echo(f"  {view.summary}, health {view.health}")
    typer.echo(f"  default proxy: {view.default_proxy or 'none'}   color: {view.color or 'none'}")
    if view.notes:
        typer.echo(f"  notes: {_one_line(view.notes)}")
    if view.lanes:
        rows = [[lane.name, lane.readiness.value, lane.platform, lane.proxy or "none", lane.lane_state,
                 lane.login_url or "", _one_line(lane.note)] for lane in view.lanes]
        typer.echo("")
        typer.echo(_table(["LANE", "READINESS", "PLATFORM", "PROXY", "STATE", "LOGIN URL", "NOTE"], rows))


@app.command("update")
def update_workspace(workspace_id: str, name: str | None = typer.Option(None),
                     color: str | None = typer.Option(None), no_color: bool = typer.Option(False, "--no-color"),
                     proxy: str | None = typer.Option(None, help="New default proxy label."),
                     no_proxy: bool = typer.Option(False, "--no-proxy", help="Clear the default proxy."),
                     notes: str | None = typer.Option(None)) -> None:
    """Change a workspace. Changing its default proxy never moves existing lanes."""
    if (proxy is not None and no_proxy) or (color is not None and no_color):
        _fail("pick one of --proxy and --no-proxy (and of --color and --no-color)", "invalid")
    changes: dict[str, Any] = {}
    if name is not None:
        changes["name"] = name
    if color is not None:
        changes["color"] = color
    if no_color:
        changes["color"] = None
    if proxy is not None:
        changes["default_proxy"] = proxy
    if no_proxy:
        changes["default_proxy"] = None
    if notes is not None:
        changes["notes"] = notes
    with _errors():
        view = _service().update_workspace(workspace_id, changes)
    typer.echo(f"updated workspace {view.id}")


@app.command("delete")
def delete_workspace(workspace_id: str, yes: YesOpt = False,
                     with_memory: Annotated[bool, typer.Option(
                         "--with-memory", help="also erase the workspace's memory (export it first)")] = False) -> None:
    """Delete a workspace. Refused while it still has lanes, and while it has memory unless
    --with-memory."""
    if not yes:
        extra = (" Its memory is erased too; export it first with `agentos memory export "
                 f"{workspace_id} --md DIR` if you want to keep it." if with_memory else "")
        typer.confirm(f"Delete workspace {workspace_id}?{extra}", abort=True)
    with _errors():
        _service().delete_workspace(workspace_id, with_memory=with_memory)
    typer.echo(f"deleted workspace {workspace_id}" + (" and its memory" if with_memory else ""))


# ---- templates, plan and apply -----------------------------------------------------------

@app.command("templates")
def list_templates(as_json: JsonOpt = False) -> None:
    """Templates you can apply: the built-in ones and the valid files in $AGENTOS_HOME/templates."""
    with _errors():
        templates, problems = _service().templates()
    if as_json:
        return _dump({"templates": [t.model_dump(mode="json") for t in templates],
                      "problems": [p.model_dump(mode="json") for p in problems]})
    rows = [[t.id, str(len(t.surfaces)), ", ".join(s.name for s in t.surfaces), t.title] for t in templates]
    typer.echo(_table(["ID", "SURFACES", "LANES", "TITLE"], rows))
    if problems:
        typer.echo("\nNot usable (fix the file, then list again):")
        for problem in problems:
            typer.echo(f"  {problem.file}: {problem.message}")


@app.command("plan")
def plan_template(template: str, workspaces: WorkspacesOpt = None, proxy: ProxyOpt = None,
                  lane_proxy: LaneProxyOpt = None, override_proxy_rules: OverrideOpt = False,
                  reason: ReasonOpt = None, as_json: JsonOpt = False) -> None:
    """Show what applying a template would do, without creating anything. Exits 1 when there are conflicts."""
    names, per_ws, per_lane = _workspace_map(workspaces, proxy, lane_proxy)
    with _errors():
        plan = _service().plan(template, names, proxy_per_workspace=per_ws, proxy_per_lane=per_lane,
                               override_proxy_rules=override_proxy_rules, reason=reason)
    if as_json:
        _dump(plan.public())
    else:
        _print_plan(plan)
    if plan.conflicts:
        raise typer.Exit(1)


@app.command("apply")
def apply_template(template: str, workspaces: WorkspacesOpt = None, proxy: ProxyOpt = None,
                   lane_proxy: LaneProxyOpt = None, override_proxy_rules: OverrideOpt = False,
                   reason: ReasonOpt = None, yes: YesOpt = False) -> None:
    """Create a template's lanes in workspaces. Shows the plan first. Safe to run again: it only creates
    what is missing, and a partial run says exactly what it made."""
    names, per_ws, per_lane = _workspace_map(workspaces, proxy, lane_proxy)
    with _errors():
        service = _service()
        plan = service.plan(template, names, proxy_per_workspace=per_ws, proxy_per_lane=per_lane,
                            override_proxy_rules=override_proxy_rules, reason=reason)
    _print_plan(plan)
    if plan.conflicts:
        _fail(refusal(plan), WorkspaceConflict.code)
    if not plan.creates:
        typer.echo("\nNothing to create: every lane already exists and matches.")
        return
    typer.echo(f"\n{approval_summary(plan)}")
    if not yes:
        typer.confirm(f"Create {len(plan.creates)} {'lane' if len(plan.creates) == 1 else 'lanes'}?", abort=True)
    with _errors():
        result = asyncio.run(service.execute(plan, by=OWNER))
    if result.created:
        typer.echo(f"created: {', '.join(result.created)}")
    if result.existing:
        typer.echo(f"already there: {', '.join(result.existing)}")
    for item in result.overridden:
        typer.echo(f"override recorded for {item['lane']}: {'; '.join(item['rules'])}")
    for warning in dict.fromkeys(result.warnings):
        typer.echo(f"warning: {warning}")
    if result.failed:
        typer.secho(f"stopped at {result.failed['lane']} ({result.failed['code']}): {result.failed['message']}",
                    err=True, fg=typer.colors.RED)
        typer.echo(f"still to do: {', '.join(result.remaining)}. Run the same command again to finish.")
        raise typer.Exit(1)
    typer.echo("done: every lane exists and matches the plan. Lanes start as needs_login.")


# ---- the login queue -------------------------------------------------------------------

@app.command("queue")
def show_queue(workspace: str | None = typer.Argument(None), as_json: JsonOpt = False) -> None:
    """Lanes the owner still has to log into or fix, with their login URLs. Attention first."""
    with _errors():
        queue = _service().queue(workspace)
    if as_json:
        return _dump(queue.model_dump(mode="json"))
    for ws in queue.workspaces:
        typer.echo(f"{ws.id}: {ws.summary}")
    typer.echo(f"overall: {queue.summary}")
    if queue.lanes:
        rows = [[lane.id, lane.readiness.value, lane.login_url or "no login URL", _one_line(lane.note)]
                for lane in queue.lanes]
        typer.echo("")
        typer.echo(_table(["LANE", "STATE", "LOGIN URL", "NOTE"], rows))


@app.command("ready")
def mark_ready(lane: str) -> None:
    """Mark a lane as logged in and ready."""
    with _errors():
        view = _service().set_readiness(lane, "ready", by=OWNER)
    typer.echo(f"{view.id} is ready")


@app.command("unready")
def mark_unready(lane: str, attention: bool = typer.Option(False, "--attention",
                                                         help="The lane needs attention (give --note)."),
                 note: str | None = typer.Option(None, help="What is wrong, for --attention.")) -> None:
    """Mark a lane as needing login, or with --attention as needing attention."""
    state = "needs_attention" if attention else "needs_login"
    with _errors():
        view = _service().set_readiness(lane, state, note=note, by=OWNER)
    typer.echo(f"{view.id} is {view.readiness.value}")


@app.command("overrides")
def show_overrides(limit: int = typer.Option(20, min=1, max=500)) -> None:
    """The proxy-rule overrides that were applied, newest first."""
    with _errors():
        records = _service().overrides(limit)
    if not records:
        return typer.echo("No overrides recorded.")
    rows = [[str(r.id), r.at, r.by, r.lane, r.proxy or "none", _one_line(r.reason), _one_line(r.rules[0])]
            for r in records]
    typer.echo(_table(["ID", "AT", "BY", "LANE", "PROXY", "REASON", "FIRST RULE"], rows))


# ---- export and import (agentos.workspaces.transfer.export, agentos.workspaces.transfer.import_) -------------------------

EXPORT_PASSPHRASE_ENV = "AGENTOS_EXPORT_PASSPHRASE_FILE"
ExportPassOpt = Annotated[Path | None, typer.Option(
    "--passphrase-file", help=f"File holding the export passphrase (chmod 600; or set {EXPORT_PASSPHRASE_ENV}).")]
VaultPassOpt = Annotated[Path | None, typer.Option(
    "--vault-passphrase-file", help="File holding this node's vault passphrase, for a passphrase vault (chmod 600).")]


def _mib(n: int) -> str:
    return f"{n / 1024**2:.1f} MiB"


def _prepare_vault(src: Any, passphrase_file: Path | None, why: str) -> None:
    """Make the CLI's vault match the node: sealed when the daemon's vault is sealed (the owner's call),
    unsealed with the passphrase for a passphrase vault (asked, or from --vault-passphrase-file)."""
    import getpass
    import sys

    from agentos.ops.cli_common import read_secret_file
    from agentos.vault import control
    from agentos.vault.errors import VaultError

    vault = src.vault
    try:
        header = vault.store.header()
    except VaultError:
        return  # the plan reports the vault as unreadable
    if header is None:
        return
    try:
        daemon = control.request(vault.socket_path, {"op": "status"}, timeout=5)
    except (OSError, ValueError):
        daemon = None
    if daemon and daemon.get("ok") and (daemon.get("status") or {}).get("state") == "sealed":
        vault.seal()  # sealed on purpose: an export or import does not open it behind the owner's back
        return
    if header.mode != "passphrase":
        return
    if passphrase_file is not None:
        secret = read_secret_file(passphrase_file, "vault passphrase file")
    elif sys.stdin.isatty():
        secret = getpass.getpass(f"Vault passphrase ({why}): ")
    else:
        return  # stays sealed, and the export or plan says so
    with _errors():
        asyncio.run(vault.unseal(secret))


@app.command("export")
def export_cmd(
    workspace_id: str,
    to: Annotated[Path, typer.Option("--to", help="The file to write (.agentos), or a directory for a dated name.")],
    passphrase_file: ExportPassOpt = None,
    recipient: Annotated[list[str] | None, typer.Option(
        "--recipient", "-r", help="Encrypt to this age public key (age1...) instead of a passphrase. Repeatable.")] = None,
    with_proxies: Annotated[bool, typer.Option(
        "--with-proxies", help="Also carry the lanes' proxy hosts and credentials (encrypted, like everything).")] = False,
    vault_passphrase_file: VaultPassOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Write one workspace into one encrypted file: lanes (profiles and fixed fingerprints), the workspace
    record and readiness, its vault entries, flows, schedules and grant definitions. Running lanes are
    paused for the copy and started again. Nothing here is changed or deleted."""
    from datetime import UTC, datetime

    from agentos.ops.cli_common import read_passphrase
    from agentos.workspaces.transfer.export import export_name, export_workspace
    from agentos.workspaces.transfer.sources import node_sources

    cfg = _config()
    recipients = [r for r in recipient or [] if r.strip()] or None
    passphrase = None if recipients else read_passphrase(passphrase_file, confirm=True, prompt="Export passphrase: ",
                                                         env=EXPORT_PASSPHRASE_ENV)
    dest = to / export_name(workspace_id, datetime.now(UTC)) if to.is_dir() else to
    try:
        lanes = _lane_manager(cfg)
    except ModuleNotFoundError as exc:
        _fail(f"missing Python module {exc.name}; run deploy/install-node.sh (or `uv sync`)", "unavailable")
    src = node_sources(cfg, lanes, create=False)
    try:
        _prepare_vault(src, vault_passphrase_file, "to read the workspace's entries")
        with _errors():
            result = asyncio.run(export_workspace(src, workspace_id, dest, passphrase=passphrase,
                                                  recipients=recipients, with_proxies=with_proxies))
    finally:
        src.close()
    if as_json:
        return _dump(result.to_dict())
    c = result.counts
    typer.echo(f"Exported workspace {result.workspace} to {result.path} ({_mib(result.size)}, encrypted)")
    lanes_text = ", ".join(f"{lane['id']} ({'paused, warm again' if lane['copied'] == 'paused' else 'cold'})"
                           for lane in result.lanes) or "none"
    typer.echo(f"  lanes: {lanes_text}")
    typer.echo(f"  {c['profile_files']} profile files ({_mib(c['profile_bytes'])}), {c['fingerprints']} pinned "
               f"fingerprints, {c['vault_entries']} vault entries, {c['flows']} flows ({c['flow_versions']} versions), "
               f"{c['schedules']} schedules, {c.get('agenda_entries', 0)} agenda entries, {c['grants']} grant "
               f"definitions, {c['proxies']} proxy labels")
    typer.echo("  not in the file: grant tokens (definitions only; re-issue them on import with --reissue-grants), "
               "webhook secrets" + ("" if with_proxies else ", proxy hosts and credentials (labels only; "
                                                            "--with-proxies carries them)"))
    for item in result.not_exported:
        typer.echo(f"  left out: {item['lane']} ({item['why']})")
    for note in result.notes:
        typer.echo(f"  note: {note}")
    for warning in result.warnings:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)
    typer.echo("Keep the passphrase (or the age identity) safe: without it the file cannot be opened.")
    typer.echo("The lanes stay on this node. Never run them here and on the importing node at the same time.")


def _print_import_plan(plan: Any) -> None:
    src = plan.source
    typer.echo(f"Import workspace {plan.workspace_from} from node {src.get('node')} (host {src.get('hostname')}), "
               f"exported {src.get('created_at')} by agentos {src.get('agentos_version')}")
    if plan.workspace_to != plan.workspace_from:
        typer.echo(f"  as workspace {plan.workspace_to}")
    if plan.lanes:
        rows = [[lane["to"], lane["proxy"] or ("-" if not lane["proxy_from"] else "MISSING"),
                 "pinned" if lane["fingerprint"] else "not yet", str(lane["files"]), lane["readiness"]]
                for lane in plan.lanes]
        typer.echo("")
        typer.echo(_table(["LANE", "PROXY", "FINGERPRINT", "FILES", "READINESS"], rows))
    typer.echo("")
    for p in plan.proxies:
        typer.echo(f"proxy {p['label']}: {p['action']}" + (f" (-> {p['to']})" if p["to"] and p["to"] != p["label"] else ""))
    v = plan.vault
    if v.get("entries"):
        typer.echo(f"vault: {v['entries']} entries into this node's vault ({v.get('state')})")
    for f in plan.flows:
        typer.echo(f"flow {f['to']}: {f['versions']} versions, {f['action']}")
    if plan.schedules:
        on = sum(1 for s in plan.schedules if s["enabled"])
        typer.echo(f"schedules: {len(plan.schedules)} ({on} turned on)")
    if plan.agenda:
        typer.echo(f"agenda: {len(plan.agenda)} entries, in as drafts (approvals do not travel; propose them again)")
    for g in plan.grants:
        typer.echo(f"grant {g['from']} for {g['subject']} (ceiling {g['ceiling']}): {g['action'].replace('_', ' ')}")
    for warning in plan.warnings:
        typer.echo("")
        typer.secho(f"WARNING: {warning}", fg=typer.colors.YELLOW, bold=True)
    for note in plan.notes:
        typer.echo(f"note: {note}")
    for blocker in plan.blockers:
        typer.secho(f"blocked: {blocker}", fg=typer.colors.RED)


@app.command("import")
def import_cmd(
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="An export (.agentos).")],
    rename_to: Annotated[str | None, typer.Option(
        "--as", help="Bring the workspace in under this new id (lanes, flows, vault bindings and grants follow).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Verify the file and print the plan; change nothing.")] = False,
    reissue_grants: Annotated[bool, typer.Option(
        "--reissue-grants", help="Mint new grants from the exported definitions; the new tokens are printed once.")] = False,
    allow_missing_proxy: Annotated[bool, typer.Option(
        "--allow-missing-proxy", help="Create lanes whose proxy this node lacks, with a placeholder that fails closed.")] = False,
    map_proxy: Annotated[list[str] | None, typer.Option(
        "--map-proxy", help="OLD=NEW: use this node's proxy NEW for lanes that used OLD. Repeatable.")] = None,
    on_vault_conflict: Annotated[str, typer.Option(
        "--on-vault-conflict", help="A vault entry with the same name here: error, rename or skip.")] = "error",
    enable_schedules: Annotated[bool, typer.Option(
        "--enable-schedules", help="Keep schedules on that were on (default: they come in off).")] = False,
    passphrase_file: ExportPassOpt = None,
    identity: Annotated[Path | None, typer.Option(
        "--identity", "-i", help="the age key file (the one with its AGE-SECRET-KEY-1... line) for a file "
                                 "encrypted to you.")] = None,
    vault_passphrase_file: VaultPassOpt = None,
    yes: YesOpt = False,
    as_json: JsonOpt = False,
) -> None:
    """Import a workspace export: verify every checksum, plan, then add everything as one unit (rolled back if
    any step fails). Existing lanes are never replaced; lanes come in not running (they start on first
    use)."""
    from agentos.core import age
    from agentos.ops.cli_common import read_passphrase, read_secret_file
    from agentos.workspaces.transfer.import_ import ImportOptions, apply_import, plan_import
    from agentos.workspaces.transfer.read import read_export, staging_dir
    from agentos.workspaces.transfer.sources import node_sources

    cfg = _config()
    proxy_map: dict[str, str] = {}
    for item in map_proxy or []:
        old, sep, new = item.partition("=")
        if not sep or not old or not new:
            _fail(f"--map-proxy takes OLD=NEW, for example lt1=de1; got {item!r}", "invalid")
        proxy_map[old] = new
    identities = None
    passphrase = None
    if identity is not None:
        try:
            identities = age.read_identities(read_secret_file(identity, "identity file"))
        except age.BadKey as exc:
            _fail(str(exc), "invalid")
    else:
        passphrase = read_passphrase(passphrase_file, confirm=False, prompt="Export passphrase: ",
                                     env=EXPORT_PASSPHRASE_ENV)
    options = ImportOptions(rename_to=rename_to, reissue_grants=reissue_grants, allow_missing_proxy=allow_missing_proxy,
                            proxy_map=proxy_map, vault_conflict=on_vault_conflict, enable_schedules=enable_schedules)
    with _errors():
        contents = read_export(file, staging_dir(cfg.home, "import"), passphrase=passphrase, identities=identities)
    src = None
    try:
        try:
            lanes = _lane_manager(cfg)
        except ModuleNotFoundError as exc:
            _fail(f"missing Python module {exc.name}; run deploy/install-node.sh (or `uv sync`)", "unavailable")
        src = node_sources(cfg, lanes, create=True, policy=reissue_grants and bool(contents.grants))
        if contents.vault:
            _prepare_vault(src, vault_passphrase_file, "to seal the imported entries")
        with _errors():
            plan = asyncio.run(plan_import(contents, src, options))
        if as_json and (dry_run or not plan.ok):
            _dump(plan.to_dict())
        elif not as_json:
            _print_import_plan(plan)
        if not plan.ok:
            if not as_json:
                typer.secho("Nothing was changed.", err=True, fg=typer.colors.RED)
            raise typer.Exit(1)
        if dry_run:
            if not as_json:
                typer.echo("\nDry run: nothing was changed.")
            return
        if not yes:
            typer.confirm(f"\nImport {len(plan.lanes)} lanes into workspace {plan.workspace_to} on this node?",
                          abort=True)
        with _errors():
            result = asyncio.run(apply_import(plan, contents, src))
    finally:
        contents.discard()
        if src is not None:
            src.close()
    if as_json:
        return _dump({"plan": plan.to_dict(), "result": result.to_dict()})
    on = sum(1 for s in result.schedules if s["enabled"])
    typer.echo(f"\nImported workspace {result.workspace}: {len(result.lanes)} lanes (cold), {result.vault['added']} "
               f"vault entries, {len(result.flows)} flows, {len(result.schedules)} schedules ({on} on)")
    if result.proxies_added:
        typer.echo(f"  proxies added: {', '.join(result.proxies_added)}")
    if result.agenda:
        typer.echo(f"  agenda: {len(result.agenda)} entries in as drafts. Approvals do not travel between nodes: "
                   "propose each one again with `agentos agenda propose <id>` once the source copy is cancelled")
    if result.grants:
        typer.echo("Re-issued grants. Each token is shown only now; give it to its agent:")
        for g in result.grants:
            typer.echo(f"  {g['id']}  {g['subject']}  {g['token']}")
    if result.webhooks:
        typer.echo("Webhooks have new URLs and secrets, shown only now; update the senders:")
        for w in result.webhooks:
            typer.echo(f"  {w['schedule']}  {w['url_path']}  secret {w['secret']}")
    typer.echo("Next: check the proxies (agentos proxy list), then open one lane at a time and see it is still "
               "logged in.")
