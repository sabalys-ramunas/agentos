"""`agentos node restart`: restart agentosd without silently closing lanes other sessions hold.

A restart closes every open lane: its browser stops (cookies and the profile are kept, the
page is gone), every claim ends and every call in flight fails. On a node several agents share
that is someone else's work: the 2026-11-04 canary's restart closed `test/probe`, a lane another
session had open, and nobody saw it until afterwards. So every restart path the owner or an
agent can take asks first:

  agentos node restart            names the open lanes and who is on each (claims, flow and
                                  connector runs, a person in control, an agent driving it, calls
                                  in flight, live views), asks when any lane has a holder, then
                                  `systemctl restart`. `--yes` does not ask; `--check` only
                                  reports (exit 3 when a lane has a holder, for scripts).
  agentos upgrade                 names them in its confirmation (it drains and restarts).
  agentos migrate-service-user    the same.
  deploy/install-node.sh          does not restart agentosd over held lanes unless given --yes;
                                  it installs the code and says how to restart later.

A plain `systemctl restart agentosd` cannot ask anyone; agentosd then logs each lane it closes
with its holders and records a `closed_by_shutdown` lane event. Either way the holders hear:
a claim's holder gets `claim_lost` ("the node restarted") on its next call on the lane, and a
lane reopens from its persistent profile on the next call that needs it.

Who holds what comes from the running agentosd through the ops socket (`holders`). An agentosd
from before this report answers only lane states; each open lane then counts as held (it
cannot say), so the restart asks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer

from agentos.node.claims import holder_text
from agentos.ops.layout import ops_socket
from agentos.ops.system import Host

HELD_EXIT = 3  # `--check`: a lane has a holder (or an older agentosd cannot say)
HOT = ("warming", "warm", "active")


@dataclass
class OpenLanes:
    """What a restart would close. `running`: agentosd answered on its ops socket. A lane's
    `holders` is None when the agentosd is too old to report them."""
    running: bool = False
    lanes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def held(self) -> list[dict[str, Any]]:
        return [lane for lane in self.lanes if lane.get("holders") is None or lane.get("holders")]

    @property
    def idle(self) -> list[dict[str, Any]]:
        return [lane for lane in self.lanes if lane.get("holders") == []]

    def public(self) -> dict[str, Any]:
        return {"running": self.running, "lanes": self.lanes, "held": [lane["id"] for lane in self.held]}


def open_lanes(home: Path, host: Host | None = None) -> OpenLanes:
    """Ask the running agentosd which lanes are open and who is on each."""
    host = host or Host()
    sock = ops_socket(home)
    resp = host.unix_request(sock, {"op": "holders"}, timeout=15)
    if resp is None:
        return OpenLanes(running=False)
    if resp.get("ok"):
        return OpenLanes(running=True, lanes=list(resp.get("lanes") or []))
    older = host.unix_request(sock, {"op": "lanes"}, timeout=15) or {}
    lanes = [{"id": lane.get("id"), "state": lane.get("state"), "holders": None}
             for lane in older.get("lanes") or [] if lane.get("state") in HOT]
    return OpenLanes(running=True, lanes=lanes)


def describe(report: OpenLanes) -> list[str]:
    """One line per open lane, the held ones first."""
    if not report.running:
        return ["agentosd is not running here (no answer on its ops socket), so no lane is open."]
    if not report.lanes:
        return ["No lane is open: a restart closes nothing."]
    lines = []
    for lane in report.held + report.idle:
        holders = lane.get("holders")
        if holders is None:
            who = "who is on it is unknown (this agentosd is older than the holders report)"
        elif holders:
            who = "; ".join(holder_text(h) for h in holders)
        else:
            who = "idle (it only loses its page; its cookies are kept)"
        lines.append(f"  {lane['id']} ({lane.get('state')}): {who}")
    return lines


def confirm_line(report: OpenLanes) -> str:
    """The sentence a confirmation adds when a restart would close held lanes ('' when none)."""
    held = report.held
    if not held:
        return ""
    names = ", ".join(lane["id"] for lane in held)
    return (f" It closes {len(held)} lane{'s' if len(held) != 1 else ''} other sessions are using ({names}): "
            "their claims end and their pages are gone.")


def warn_held(report: OpenLanes, echo: Any = typer.echo) -> None:
    """Print the open lanes before a restart path asks (upgrade, migrate-service-user)."""
    if report.running and report.lanes:
        echo("Open lanes the restart closes:")
        for line in describe(report):
            echo(line)


def _active(host: Host, unit: str) -> bool | None:
    """Is the systemd unit active; None when there is no systemd here."""
    if not host.which("systemctl"):
        return None
    code, out = host.run(["systemctl", "is-active", unit], timeout=10)
    return code == 0 and out.strip() == "active"


def restart_command(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask, even when other sessions hold lanes.")] = False,
    check: Annotated[bool, typer.Option("--check", help="Only report the open lanes; exit 3 when one has a "
                                                         "holder.")] = False,
    unit: Annotated[str, typer.Option(help="The systemd unit.")] = "agentosd",
    as_json: Annotated[bool, typer.Option("--json", help="The report as JSON.")] = False,
) -> None:
    """Restart agentosd, after naming the open lanes and who holds them (asks when any is held)."""
    from agentos.core.config import home_from_env

    host = Host()
    report = open_lanes(home_from_env(), host)
    if as_json:
        typer.echo(json.dumps(report.public(), indent=2))
    else:
        for line in describe(report) if report.lanes or check else []:
            typer.echo(line)
    if check:
        raise typer.Exit(HELD_EXIT if report.held else 0)
    active = _active(host, unit)
    if active is None:
        typer.echo(f"error: no systemd here, so {unit} is not a service: stop agentosd (Ctrl-C, or end "
                   "`agentos node serve`) and start it again.", err=True)
        raise typer.Exit(1)
    if report.held and not yes:
        typer.confirm(f"Restart {unit}?{confirm_line(report)}", abort=True)
    code, out = host.run(["systemctl", "restart", unit], timeout=120)
    if code != 0:
        typer.echo(f"error: systemctl restart {unit} failed: {out.strip()[:300]}", err=True)
        raise typer.Exit(1)
    closed = len(report.lanes)
    typer.echo(f"Restarted {unit}." + (f" {closed} lane{'s' if closed != 1 else ''} closed; each opens again from "
                                        "its profile on its next call, and a claim's holder hears claim_lost."
                                        if closed else ""))
