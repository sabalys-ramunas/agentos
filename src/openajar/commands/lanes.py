"""`openajar lane`: create, import, delete, list, and a lane's proxy and device. The group's
other commands are in lane_sites (sites, gate log, rules, pace) and lane_control (take, hand back,
stop, deny, login, view)."""

from __future__ import annotations

import contextlib
import urllib.parse
from pathlib import Path
from typing import Annotated

import typer

from openajar.commands.base import (
    DeviceOpt,
    LaneIdArg,
    ProxyOpt,
    _first_validation_error,
    check_proxy,
    daemon_request,
    fail,
    initialized_config,
    lane_api_path,
    run_manager,
)
from openajar.core.config import (
    NodeConfig,
)
from openajar.core.models import LaneSpec

lane_app = typer.Typer(no_args_is_help=True, help="Browser lanes: one logged-in account each.")


@lane_app.command("create")
def lane_create(
    lane_id: LaneIdArg,
    proxy: ProxyOpt = None,
    device: DeviceOpt = None,
    engine: Annotated[str, typer.Option(help="Browser engine.")] = "camoufox",
) -> None:
    """Create a lane with an empty profile. It stays off (not running) until first use. Log in later through
    browser_view."""
    check_proxy(initialized_config(), proxy)
    try:
        spec = LaneSpec(id=lane_id, proxy_label=proxy, engine=engine, device=device)
    except ValueError as exc:
        raise fail(_first_validation_error(exc)) from None
    info = run_manager(lambda manager: manager.create(spec))
    on = f", device {info.spec.device}" if info.spec.device else ""
    typer.echo(f"Created lane {info.spec.id} (engine {info.spec.engine}, proxy {info.spec.proxy_label or 'none'}{on})")


@lane_app.command("import")
def lane_import(
    lane_id: LaneIdArg,
    profile: Annotated[Path, typer.Option(help="Existing browser profile directory to copy in.",
                                          exists=True, file_okay=False, dir_okay=True, readable=True)],
    fingerprint: Annotated[Path | None, typer.Option(help="Browser fingerprint as JSON, to copy in.",
                                                     exists=True, file_okay=True, dir_okay=False)] = None,
    proxy: ProxyOpt = None,
    engine: Annotated[str, typer.Option(help="Browser engine.")] = "camoufox",
) -> None:
    """Create a lane from an existing logged-in profile (for example, one from the first prototype)."""
    check_proxy(initialized_config(), proxy)
    spec = LaneSpec(id=lane_id, proxy_label=proxy, engine=engine)
    info = run_manager(lambda manager: manager.import_profile(spec, profile, fingerprint))
    pinned = "with its pinned fingerprint" if fingerprint else "fingerprint pinned on first launch"
    typer.echo(f"Imported lane {info.spec.id} from {profile} ({pinned})")


@lane_app.command("delete")
def lane_delete(lane_id: LaneIdArg, yes: Annotated[bool, typer.Option("--yes", help="Do not ask.")] = False) -> None:
    """Remove a lane that is not running. Its profile and files move to OPENAJAR_HOME/trash, nothing
    is erased."""
    if not yes:
        typer.confirm(f"Remove lane {lane_id}? Its files move to the trash folder.", abort=True)
    trash = run_manager(lambda manager: manager.delete(lane_id, by="owner"))
    typer.echo(f"Removed lane {lane_id}." + (f" Its files are in {trash}." if trash else ""))


@lane_app.command("set-proxy")
def lane_set_proxy(
    lane_id: LaneIdArg,
    label: Annotated[str, typer.Argument(help="Proxy label from `openajar proxy list`, or none.")],
    yes: Annotated[bool, typer.Option("--yes", help="Go ahead although the lane's site will see a new IP.")]
    = False,
) -> None:
    """Move a lane that is not running to another proxy. The lane's sites see a new IP next time, so do this
    rarely. A lane that has launched needs --yes."""
    new = None if label.lower() == "none" else label
    if new:
        check_proxy(initialized_config(), new)
    info = run_manager(lambda manager: manager.set_proxy(lane_id, new, confirm=yes, by="owner"))
    typer.echo(f"Lane {lane_id} now uses proxy {info.spec.proxy_label or 'none'}.")


def _lane_rows_from_daemon(cfg: NodeConfig) -> list[tuple[str, ...]] | None:
    """The lanes as the running agentosd sees them (whether a browser runs is known only there), or
    None when it is not running."""
    from datetime import datetime

    listed = daemon_request(cfg, "GET", "/api/lanes", absent_ok=True)
    if not isinstance(listed, list):
        return None
    rows = []
    for lane in listed:
        last = "never"
        if lane.get("last_used"):
            try:
                last = datetime.fromisoformat(str(lane["last_used"])).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                last = str(lane["last_used"])
        proxy = lane.get("proxy") or {}
        rows.append((str(lane.get("id")), str(lane.get("state")), str(lane.get("engine") or "-"),
                     str(lane.get("device") or "own"), str(proxy.get("label") or "-"), last))
    return rows


@lane_app.command("list")
def lane_list() -> None:
    """Lanes on this node: from the running agentosd (which knows which browsers run), else from the store."""
    cfg = initialized_config()
    rows = _lane_rows_from_daemon(cfg)
    note = None
    if rows is None:
        from openajar.node.lanes.specs import _browsers_using

        async def lanes(manager):
            return manager.list()

        rows = []
        for info in run_manager(lanes):
            last = info.last_used.strftime("%Y-%m-%d %H:%M") if info.last_used else "never"
            state = info.state.value
            with contextlib.suppress(Exception):  # no process table to read here: the store's state stands
                if state == "cold" and _browsers_using(cfg.profile_dir(info.spec.id)):
                    state, note = "running", ("agentosd is not running, but a browser still runs on a lane marked "
                                              "running. Start agentosd, or stop that browser.")
            rows.append((info.spec.id, state, info.spec.engine, info.spec.device or "own",
                         info.spec.proxy_label or "-", last))
    if not rows:
        typer.echo("No lanes. Create one with `openajar lane create <workspace/name>`.")
        return
    rows = [("LANE", "STATE", "ENGINE", "DEVICE", "PROXY", "LAST USED"), *rows]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())
    if note:
        typer.echo(note)


@lane_app.command("set-device")
def lane_set_device(
    lane_id: LaneIdArg,
    device: Annotated[str, typer.Argument(help="Device from `openajar device list`, or none (the lane becomes a "
                                               "device of its own again).")],
    yes: Annotated[bool, typer.Option("--yes", help="Go ahead although the lane's site will see a new computer.")]
    = False,
) -> None:
    """Move a lane that is not running onto a device, or off it. A lane that has launched gets the device's
    fingerprint (the browser details a site sees), which is a new computer to its site: expect a re-login
    or a checkpoint (the site asks the account owner a question). That needs --yes. To share a lane's own
    fingerprint instead, make the device from it: openajar device create <name> --from-lane <lane>."""
    target = None if device.lower() == "none" else device

    async def plan(manager):
        return manager.plan_device_move(lane_id, target)

    cfg = initialized_config()
    query = "?" + urllib.parse.urlencode({"to": target or "none"})
    shown = daemon_request(cfg, "GET", lane_api_path(lane_id, "device-move") + query, absent_ok=True)
    if not isinstance(shown, dict):
        shown = run_manager(plan)
    for line in shown["changes"]:
        typer.echo(f"  {line}.")
    for line in shown["warnings"]:
        typer.echo(f"  Warning: {line}.")
    if shown["from"] == shown["to"]:
        return
    if shown["confirm"] and not yes:
        raise fail(f"Nothing changed. If you still want it: openajar lane set-device {lane_id} {device} --yes")
    moved = daemon_request(cfg, "POST", lane_api_path(lane_id, "device"), {"device": target, "confirm": yes},
                           absent_ok=True, person=True)  # a move changes the lane's exit: a person's call
    if isinstance(moved, dict):
        done = moved["plan"]
    else:  # agentosd is not running: in this process, under the same rules
        done = run_manager(lambda manager: manager.set_device(lane_id, target, confirm=yes, by="owner"))
    where = f"device {done['to']}" if done["to"] else "a device of its own"
    typer.echo(f"Lane {lane_id} is now on {where} (proxy {done['proxy']['to'] or 'none'}).")
