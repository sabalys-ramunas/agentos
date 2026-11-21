"""`openajar freeze ...` and `openajar unfreeze ...` on the node.

    openajar freeze lane <ws/name> --reason "..."
    openajar freeze workspace <ws> --reason "..."
    openajar freeze node [--reason "..."]
    openajar freeze status
    openajar unfreeze lane|workspace|node|mesh [<target>]

A freeze goes to the running daemon (so in-flight calls stop at once). When the daemon does not
answer, the freeze is written straight to $OPENAJAR_HOME/freeze: the daemon reads it within a
second, or at its next start before anything runs.

Unfreezing is the owner at the node's shell: it deletes the freeze file, which the daemon notices
within a second. It never goes over HTTP, so no token (an agent's included) can unfreeze. It
restores nothing: grants stay revoked, paused schedules and triggers stay paused.

A node freeze revokes every agent grant. A lane or workspace freeze stops every agent there,
whatever its grant, and revokes only the grants that reach nothing outside it.
"""

from __future__ import annotations

import json
import time
from typing import Annotated, Any

import typer

from openajar.core.config import NodeConfig, config_path, home_from_env, load_config
from openajar.policy.freeze_store import FreezeRecord, FreezeStore, scope_key

app = typer.Typer(no_args_is_help=True, help="Freeze a lane, a workspace or this node: every agent stops at once.")
unfreeze_app = typer.Typer(no_args_is_help=True, help="Lift a freeze (owner only; nothing is restored).")

TIMEOUT = 15.0
ReasonOpt = Annotated[str, typer.Option("--reason", "-r", help="Why, in one sentence (it is kept in the "
                                                               "action log).")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print the full report as JSON.")]


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    return typer.Exit(code)


def _config() -> NodeConfig:
    home = home_from_env()
    if not config_path(home).exists():
        raise _fail(f"no node.toml in {home}; run `openajar node init` first (or set OPENAJAR_HOME)")
    try:
        return load_config(home)
    except Exception as exc:  # noqa: BLE001  (a broken node.toml must not stop a freeze: use the home alone)
        typer.secho(f"warning: node.toml could not be read ({type(exc).__name__}); freezing through the files",
                    err=True, fg=typer.colors.YELLOW)
        return NodeConfig(home=home)


def _url(cfg: NodeConfig, path: str) -> str:
    host = cfg.mcp_host if cfg.mcp_host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{cfg.mcp_port}{path}"


def _daemon(cfg: NodeConfig, method: str, path: str, body: dict | None = None) -> dict | None:
    """The daemon's answer, or None when it is not reachable (or too old to have the route)."""
    import httpx

    if not cfg.mcp_token:
        return None
    try:
        response = httpx.request(method, _url(cfg, path), json=body, timeout=TIMEOUT,
                                 headers={"Authorization": f"Bearer {cfg.mcp_token}"})
    except httpx.HTTPError:
        return None
    try:
        data = response.json()
    except ValueError:
        data = None
    error = data.get("error") if isinstance(data, dict) else None
    if response.status_code == 404 and not isinstance(error, dict):
        return None
    if isinstance(error, dict):
        if response.status_code >= 500:
            return None  # the daemon is broken: the file still freezes
        raise _fail(f"{error.get('code', 'error')}: {error.get('message', '')}".rstrip(": "))
    if response.status_code >= 400 or not isinstance(data, dict):
        return None
    return data


def _offline_freeze(cfg: NodeConfig, scope: str, target: str, reason: str) -> dict[str, Any]:
    store = FreezeStore(cfg.home)
    rec = FreezeRecord(scope, target, reason, "owner (node cli)", time.time(), via="cli")
    path = store.path(scope_key(scope, target))
    already = path.exists()
    if not already:
        rec.durable, rec.detail_saved = store.put(rec)
        store.journal({"event": "freeze", "freeze_id": rec.id, "scope": scope, "target": target, "reason": reason,
                       "by": rec.by, "via": "cli", "daemon": "not reachable"})
    return {"scope": scope, "target": target, "freeze_id": rec.id, "already_frozen": already,
            "durable": rec.durable, "offline": True}


def _do_freeze(scope: str, target: str | None, reason: str, as_json: bool) -> None:
    cfg = _config()
    reason = " ".join(reason.split())
    if not reason:
        raise _fail("give a reason with --reason")
    report = _daemon(cfg, "POST", "/api/freeze", {"scope": scope, "target": target, "reason": reason})
    if report is None:
        name = target if scope != "node" else (cfg.name or "node")
        report = _offline_freeze(cfg, scope, name or "", reason)
        if not report["durable"]:
            raise _fail("the disk refused the freeze file and agentosd is not reachable: nothing is frozen. Stop "
                        "agentosd (systemctl stop agentosd) or free disk space.")
        typer.secho("agentosd did not answer, so the freeze was written to the node's freeze files. agentosd "
                    "applies it within a second, or at its next start before anything runs.",
                    fg=typer.colors.YELLOW)
    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return
    where = report["target"] if report["scope"] != "node" else "this node"
    word = "was already frozen" if report.get("already_frozen") else "is frozen"
    typer.echo(f"{where} {word} ({report.get('freeze_id', '')}).")
    if report.get("offline"):
        return
    stopped = report.get("stopped") or {}
    revoked = report.get("revoked") or {}
    n = len(revoked.get("grants", [])) + len(revoked.get("cascades_to", []))
    typer.echo(f"  stopped {stopped.get('calls', 0)} call(s) in flight"
               + (f" in {stopped['ms']:.0f} ms" if stopped.get("calls") else ""))
    typer.echo(f"  revoked {n} grant(s) and {len(revoked.get('standing', []))} standing approval(s)")
    if report.get("scope") in ("lane", "workspace"):
        typer.echo("  (only grants that reach nothing else; wider grants keep working elsewhere and take no "
                   "call here until you unfreeze)")
    for g in revoked.get("grants", []):
        typer.echo(f"    {g['id']}  {g.get('subject', '')}  {g.get('note') or ''}".rstrip())
    typer.echo(f"  cancelled {len(report.get('runs_cancelled', []))} run(s) and "
               f"{len(report.get('brain_tasks_cancelled', []))} brain task(s); paused "
               f"{len(report.get('schedules_paused', []))} schedule(s) and "
               f"{len(report.get('triggers_paused', []))} trigger(s)")
    if report.get("lanes_warm"):
        typer.echo(f"  kept warm for inspection: {', '.join(report['lanes_warm'])}")
    for err in report.get("errors", []):
        typer.secho(f"  warning: {err}", fg=typer.colors.YELLOW)
    if report.get("warning"):
        typer.secho(f"  warning: {report['warning']}", fg=typer.colors.YELLOW)
    if stopped.get("note"):
        typer.secho(f"  note: {stopped['note']}", fg=typer.colors.YELLOW)
    typer.echo("Unfreeze with `openajar unfreeze ...` on this node. Nothing comes back by itself: grant agents "
               "again afterwards.")


@app.command("lane")
def freeze_lane(lane_id: Annotated[str, typer.Argument(help="workspace/name")], reason: ReasonOpt,
                as_json: JsonOpt = False) -> None:
    """Freeze one lane."""
    _do_freeze("lane", lane_id, reason, as_json)


@app.command("workspace")
def freeze_workspace(workspace: Annotated[str, typer.Argument(help="the workspace id")], reason: ReasonOpt,
                     as_json: JsonOpt = False) -> None:
    """Freeze every lane of a workspace."""
    _do_freeze("workspace", workspace, reason, as_json)


@app.command("node")
def freeze_node(target: Annotated[str | None, typer.Argument(help="this node's name (optional)")] = None,
                reason: ReasonOpt = "frozen from the node's shell", as_json: JsonOpt = False) -> None:
    """Freeze this whole node."""
    _do_freeze("node", target, reason, as_json)


@app.command("status")
def freeze_status(as_json: JsonOpt = False) -> None:
    """What is frozen on this node."""
    cfg = _config()
    data = _daemon(cfg, "GET", "/api/freeze")
    if data is None:
        records, problem = FreezeStore(cfg.home).load()
        data = {"frozen": [r.public() for r in records.values()], "problem": problem, "offline": True}
    if as_json:
        typer.echo(json.dumps(data, indent=2, default=str))
        return
    if data.get("offline"):
        typer.secho("agentosd did not answer; this is what the freeze files say.", fg=typer.colors.YELLOW)
    if data.get("problem"):
        typer.secho(f"problem: {data['problem']} (the whole node counts as frozen)", fg=typer.colors.RED)
    frozen = data.get("frozen") or []
    if not frozen:
        typer.echo("Nothing is frozen.")
        return
    for r in frozen:
        flags = [] if r.get("durable", True) else ["in memory only"]
        if not r.get("readable", True):
            flags.append("record unreadable")
        extra = f" [{', '.join(flags)}]" if flags else ""
        typer.echo(f"{r['scope']:<9} {r['target']:<32} since {r.get('at') or '?'} by {r.get('by')}: "
                   f"{r.get('reason')}{extra}")
    for lane in data.get("lanes") or []:
        typer.echo(f"  lane {lane['lane']} ({lane['state']}) frozen by {lane['by']}")


def _do_unfreeze(scope: str, target: str | None) -> None:
    cfg = _config()
    if scope == "node":
        target = cfg.name or "node"
    elif scope == "mesh":
        target = "mesh"
    if not target:
        raise _fail(f"name the {scope} to unfreeze")
    store = FreezeStore(cfg.home)
    try:
        key = scope_key(scope, target)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    path = store.path(key)
    if not path.exists():
        typer.echo(f"{target} is not frozen on disk.")
        _still(cfg, scope, target)
        return
    try:
        store.remove(key)
    except OSError as exc:
        raise _fail(f"could not delete {path} ({exc.strerror or exc}); it stays frozen") from None
    store.journal({"event": "unfreeze", "scope": scope, "target": target, "by": "owner (node cli)", "via": "cli"})
    typer.echo(f"Unfrozen: {scope} {target}. agentosd lifts it within a second.")
    typer.echo("Nothing was restored: revoked grants stay revoked, and paused schedules and triggers stay paused. "
               "Grant agents again and resume what you want back.")
    _still(cfg, scope, target)


def _still(cfg: NodeConfig, scope: str, target: str) -> None:
    records, _ = FreezeStore(cfg.home).load()
    ws = target.split("/", 1)[0] if scope in ("lane", "workspace") else None
    wider = [r for k, r in records.items() if k in ("mesh", "node") or (ws and k == f"workspace.{ws}" and
                                                                         scope == "lane")]
    for r in wider:
        typer.secho(f"Still frozen by {r.scope} {r.target}: {r.reason}", fg=typer.colors.YELLOW)


@unfreeze_app.command("lane")
def unfreeze_lane(lane_id: Annotated[str, typer.Argument(help="workspace/name")]) -> None:
    """Unfreeze one lane."""
    _do_unfreeze("lane", lane_id)


@unfreeze_app.command("workspace")
def unfreeze_workspace(workspace: Annotated[str, typer.Argument(help="the workspace id")]) -> None:
    """Unfreeze a workspace."""
    _do_unfreeze("workspace", workspace)


@unfreeze_app.command("node")
def unfreeze_node() -> None:
    """Unfreeze this node."""
    _do_unfreeze("node", None)


@unfreeze_app.command("mesh")
def unfreeze_mesh() -> None:
    """Lift the controller's freeze on this node only (the controller sends it again while it stands)."""
    _do_unfreeze("mesh", None)

