"""`agentos device`: one fixed browser fingerprint and one proxy shared by a company's lanes."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Annotated

import typer

from agentos.commands.base import (
    ProxyOpt,
    check_proxy,
    daemon_request,
    fail,
    fail_coded,
    initialized_config,
    run_manager,
    valid_lane_id,
)
from agentos.core.errors import AgentosError

device_app = typer.Typer(no_args_is_help=True,
                         help="Devices: one fixed browser fingerprint and one proxy shared by a company's "
                              "lanes.")


def _show_pin(pin: dict | None) -> str:
    if not pin:
        return "not pinned yet (its first lane's first launch pins it where the proxy exits)"
    parts = [pin.get("os") or "?", pin.get("screen") or "?", f"Firefox {pin.get('firefox') or '?'}",
             pin.get("timezone") or "?", pin.get("locale") or "?"]
    where = f", pinned from {pin['pinned_exit_ip']} ({pin.get('pinned_country') or '?'})" \
        if pin.get("pinned_exit_ip") else ""
    return " · ".join(str(p) for p in parts) + where


@device_app.command("create")
def device_create(
    name: Annotated[str, typer.Argument(help="A short name, for example biz-a-pc.")],
    proxy: ProxyOpt = None,
    from_lane: Annotated[str | None, typer.Option("--from-lane", help="Take this lane's fixed fingerprint "
                                                                      "and proxy (the lane joins the "
                                                                      "device, unchanged).",
                                                  callback=lambda v: valid_lane_id(v) if v else v)] = None,
    note: Annotated[str | None, typer.Option(help="What the device is for.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="With --from-lane: go ahead although other lanes of their own "
                                                    "leave through the same proxy.")] = False,
) -> None:
    """Make a device: one browser fingerprint (the details a site sees of its computer) and one proxy for a
    company's lanes, like one office PC with a browser profile per account. Decide this before the first
    logins. One device per proxy: a proxy that another device or a lane of its own uses is refused (make
    the device --from-lane that lane instead)."""
    if proxy is None and from_lane is None:
        raise fail("give the device its proxy (--proxy <label>), or make it from a lane its site already knows "
                   "(--from-lane <workspace/name>)")
    cfg = initialized_config()
    check_proxy(cfg, proxy)
    info = daemon_request(cfg, "POST", "/api/browser-devices", {"name": name, "proxy_label": proxy,
                                                                "from_lane": from_lane, "note": note,
                                                                "confirm": yes}, absent_ok=True,
                          person=yes)  # --yes puts a second computer on accounts' exit: a person's call
    if not isinstance(info, dict):  # agentosd is not running: in this process, under the same rules
        info = run_manager(lambda manager: manager.create_device(name, proxy_label=proxy, from_lane=from_lane,
                                                                 note=note, confirm=yes, by="owner"))
    typer.echo(f"Created device {info['name']} (proxy {info['proxy'] or 'none'}).")
    if from_lane:
        typer.echo(f"  It took {from_lane}'s fingerprint, so {from_lane} stays the computer its site knows.")
        typer.echo(f"  Add the company's other lanes: agentos lane create <workspace/name> --device {name}")
    else:
        typer.echo(f"  Add lanes: agentos lane create <workspace/name> --device {name}. The first launch pins it.")


@device_app.command("list")
def device_list(as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False) -> None:
    """Devices on this node, their proxy and the lanes on each."""

    async def devices(manager):
        return manager.devices()

    found = run_manager(devices)
    if as_json:
        typer.echo(json.dumps(found, indent=1))
        return
    if not found:
        typer.echo("No devices. Make one per company: agentos device create <name> --proxy <label> "
                   "(docs/PROXIES.md).")
        return
    rows = [("DEVICE", "PROXY", "PINNED", "LANES")]
    for d in found:
        rows.append((d["name"], d["proxy"] or "-", "yes" if d["pinned"] else "no",
                     ", ".join(lane["id"] for lane in d["lanes"]) or "-"))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())


@device_app.command("show")
def device_show(
    name: Annotated[str, typer.Argument(help="Device name.")],
    offline: Annotated[bool, typer.Option("--offline", help="Do not probe the proxy (no network at all).")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """One device: its fixed browser fingerprint, its lanes, and its proxy's health (the proxy answers, its
    exit is where it was, and that place matches the device's fingerprint)."""
    from agentos.stealth.lanecheck import LaneChecker, LaneReport, format_report

    async def show(manager):
        return manager.device_info(name)

    info = run_manager(show)
    cfg = initialized_config()
    try:
        report = asyncio.run(LaneChecker(cfg, network=not offline, write_cache=False).check_device(name))
        health = report.to_dict()
    except AgentosError as exc:
        raise fail_coded(exc.code, exc.message) from None
    if as_json:
        typer.echo(json.dumps({**info, "health": health}, indent=1))
        return
    typer.echo(f"Device {info['name']}" + (f" ({info['note']})" if info.get("note") else ""))
    typer.echo(f"  proxy  {info['proxy'] or 'none'}")
    typer.echo(f"  pin    {_show_pin(info['pin'])}")
    if info.get("from_lane"):
        typer.echo(f"  from   {info['from_lane']}")
    typer.echo("  lanes  " + (", ".join(f"{lane['id']} ({lane['state']})" for lane in info["lanes"]) or "none"))
    typer.echo("")
    typer.echo(format_report(LaneReport.from_dict(health), color=sys.stdout.isatty()))


@device_app.command("remove")
def device_remove(
    name: Annotated[str, typer.Argument(help="Device name.")],
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask.")] = False,
) -> None:
    """Remove a device no lane uses. Its pin moves to AGENTOS_HOME/trash, nothing is erased."""
    if not yes:
        typer.confirm(f"Remove device {name}? Its pin moves to the trash folder.", abort=True)
    done = daemon_request(initialized_config(), "DELETE", f"/api/browser-devices/{name}", absent_ok=True)
    if isinstance(done, dict):
        typer.echo(f"Removed device {name}." + (f" Its files are in {done['trash']}." if done.get("trash") else ""))
        return
    moved = run_manager(lambda manager: manager.remove_device(name, by="owner"))
    typer.echo(f"Removed device {name}." + (f" Its files are in {moved}." if moved else ""))
