"""`openajar agenda ...` on this node: the agenda's timeline, one entry, approve, cancel, propose, add.

The CLI works on the node's databases directly, like `openajar runs`. Whoever runs it has a shell
on the node, so it acts as the owner: `approve` is the owner's decision on the entry's approval (the
same one Needs you shows), and an entry added here carries the owner grant. The node daemon fires
scheduled entries; the CLI never runs one itself.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from openajar.core import catalog
from openajar.core.config import home_from_env, load_config
from openajar.core.errors import InvalidRequest, OpenajarError

app = typer.Typer(no_args_is_help=True, help="The agenda: planned actions per workspace, their approval and proof.")

BY = "owner:cli"
OWNER = {"kind": "human", "id": "owner", "owner": "owner", "grant_id": "owner"}
STATE_MARK = {"draft": "draft", "proposed": "waits", "scheduled": "sched", "running": "run",
              "done": "done", "failed": "FAIL", "skipped": "skip", "expired": "expd", "cancelled": "cancl",
              "projected": "cron", "planned": "plan"}


class DaemonPlans:
    """What keepalive plans, asked of agentosd over the ops socket (`op: planned`): the
    daemon owns the lanes its plan reads. Registered under the service's name for the CLI's agenda,
    which calls `planned(start, end)` like it would on the service itself."""

    def __init__(self, home: Path, source: str):
        self.home, self.source = home, source

    def planned(self, start: datetime, end: datetime, *, workspace: str | None = None) -> dict[str, Any]:
        from openajar.core.errors import ServiceUnavailable
        from openajar.core.layout import ops_socket
        from openajar.core.system import Host

        resp = Host().unix_request(ops_socket(self.home), {"op": "planned", "source": self.source,
                                                           "start": start.isoformat(), "end": end.isoformat(),
                                                           "workspace": workspace}, timeout=15)
        if resp is None:
            raise ServiceUnavailable(f"agentosd is not running, so what {self.source} plans is not shown")
        if not resp.get("ok"):
            raise ServiceUnavailable((resp.get("error") or {}).get("message") or f"{self.source} did not answer")
        return resp["planned"]


def _service() -> Any:
    """The agenda with the runner, flows and policy it needs, on this node's home."""
    from openajar.agenda.service import build as agenda_build
    from openajar.core.runtime import Runtime
    from openajar.policy.service import PolicyService
    from openajar.runner.service import build as runner_build

    home = home_from_env()
    cfg = load_config(home) if (home / "node.toml").exists() else None
    runtime = Runtime()
    if cfg is not None:
        runtime.register("config", cfg)
    policy = PolicyService(home, node_config=cfg, runtime=runtime)
    for name, svc in (("policy", policy), ("approvals", policy.approvals), ("traces", policy.traces),
                      ("notifier", policy.notifier), ("ask_first", policy.ask_first)):
        runtime.register(name, svc)
    runner_build(runtime, home=home)
    try:
        from openajar.events.service import build as events_build
    except ModuleNotFoundError:  # a build without the events layer still has flows on its agenda
        events_build = None
    if events_build is not None:
        events_build(runtime)  # triggers: their workspace, and planning one (never started here)
    # what keepalive plans comes from agentosd, which owns the lanes
    runtime.register("keepalive", DaemonPlans(home, "keepalive"))
    return agenda_build(runtime, home=home)


def _fail(e: OpenajarError) -> None:
    typer.echo(catalog.cli_error(e.code, e.message), err=True)
    raise typer.Exit(1)


def _time(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        _fail(InvalidRequest(f"--{name} is an ISO 8601 time like 2026-12-01T09:00:00+02:00"))
    return t if t.tzinfo else t.astimezone()  # a time without a zone is this computer's local time


def _line(d: dict[str, Any]) -> str:
    at = (d.get("at") or d.get("created_at") or "")[:16].replace("T", " ") or "as soon as approved"
    if d.get("approx"):
        at = "~" + at
    mark = STATE_MARK.get(d.get("state") or "", d.get("state") or "?")
    where = d.get("surface") or d.get("lane") or d.get("workspace") or "-"
    return f"{at:<19} {mark:<6} {d.get('kind') or '-':<9} {where:<24} {d.get('title') or d.get('target') or ''}  {d['id']}"


@app.command("list")
def agenda_list(workspace: Annotated[str | None, typer.Option(help="one workspace (default: every one)")] = None,
                start: Annotated[str | None, typer.Option("--from", help="start, ISO 8601 (default: a day ago)")] = None,
                days: Annotated[int, typer.Option(help="how many days from the start")] = 8,
                state: Annotated[str | None, typer.Option(help="comma list: draft, proposed, scheduled, ...")] = None,
                lane: Annotated[str | None, typer.Option()] = None,
                kind: Annotated[str | None, typer.Option(help="flow, connector, route or keepalive")] = None,
                managed: Annotated[bool, typer.Option(help="show what keepalive plans on its own "
                                                           "(asked of agentosd)")] = True,
                as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """The timeline: one-off entries, the upcoming fires of recurring schedules, past runs, and what
    keepalive plans on its own (read only; ~ marks an approximate time)."""
    svc = _service()
    try:
        svc.sync()
        begin = _time(start, "from") or svc.clock.now() - timedelta(days=1)
        out = svc.timeline(start=begin.astimezone(UTC), end=begin.astimezone(UTC) + timedelta(days=days),
                           workspace=workspace, lane=lane, kind=kind,
                           states=[s for s in (state or "").split(",") if s] or None, managed=managed)
    except OpenajarError as e:
        _fail(e)
    if as_json:
        typer.echo(json.dumps(out, indent=2, default=str))
        return
    if not out["items"]:
        typer.echo("Nothing on the agenda in that range.")
    for item in out["items"]:
        typer.echo(_line(item))
    if out["truncated"]:
        typer.echo("(more items than shown: narrow the range or the filters)")
    for name, about in (out.get("managed_by") or {}).items():
        if about.get("note"):
            typer.echo(f"({name}: {about['note']})")
        quiet = [*(about.get("paused") or []), *(about.get("skipped") or [])]
        for x in quiet[:5]:
            typer.echo(f"({name}: {x['lane']} has nothing planned: {x['reason']})")
        if len(quiet) > 5:
            typer.echo(f"({name}: {len(quiet) - 5} more lanes have nothing planned; --json lists them)")


@app.command("show")
def agenda_show(entry_id: str) -> None:
    """Show an entry as JSON: action, timing, state and why, approval, run and proof, history."""
    svc = _service()
    try:
        svc.sync()
        typer.echo(json.dumps(svc.describe(svc.get(entry_id)), indent=2, default=str))
    except OpenajarError as e:
        _fail(e)


def _after(d: Any, verb: str) -> None:
    typer.echo(f"{d.id} {d.state.value}" + (f": {d.reason}" if d.reason else ""))
    if verb == "propose" and d.state.value == "proposed":
        typer.echo(f"  waiting in Needs you as approval {d.approval.get('id')}; approve it with "
                   f"`openajar agenda approve {d.id}`")


@app.command("approve")
def agenda_approve(entry_id: str, note: Annotated[str | None, typer.Option()] = None) -> None:
    """Approve an entry as the owner (a draft is proposed first). The daemon runs it at its time."""
    svc = _service()
    try:
        _after(asyncio.run(svc.approve(entry_id, by=BY, note=note)), "approve")
    except OpenajarError as e:
        _fail(e)


@app.command("propose")
def agenda_propose(entry_id: str) -> None:
    """Ask for permission to run a draft, under the grant it was drafted with."""
    svc = _service()
    try:
        _after(asyncio.run(svc.propose(entry_id, by=BY)), "propose")
    except OpenajarError as e:
        _fail(e)


@app.command("cancel")
def agenda_cancel(entry_id: str, reason: Annotated[str | None, typer.Option()] = None) -> None:
    """Call off an entry: its waiting approval is withdrawn and an unfinished run cancelled."""
    svc = _service()
    try:
        _after(asyncio.run(svc.cancel(entry_id, by=BY, reason=reason)), "cancel")
    except OpenajarError as e:
        _fail(e)


@app.command("add")
def agenda_add(title: Annotated[str, typer.Option(help="what it is, in words")],
               flow: Annotated[str | None, typer.Option(help="a saved flow id")] = None,
               connector: Annotated[str | None, typer.Option(help="a connector operation, like instagram.post_photo")] = None,
               trigger: Annotated[str | None, typer.Option(help="a trigger id (rt_...)")] = None,
               on: Annotated[str | None, typer.Option(help="the lane (or the connector's surface)")] = None,
               param: Annotated[list[str] | None, typer.Option("--param", "-p", help="key=value (JSON values ok)")] = None,
               params_file: Annotated[Path | None, typer.Option("--params-file", help="a JSON file of params")] = None,
               at: Annotated[str | None, typer.Option(help="an exact time, ISO 8601")] = None,
               not_before: Annotated[str | None, typer.Option(help="the window's start")] = None,
               not_after: Annotated[str | None, typer.Option(help="the window's end")] = None,
               notes: Annotated[str | None, typer.Option()] = None,
               approve: Annotated[bool, typer.Option(help="approve it now as the owner")] = False) -> None:
    """Plan one action. Without --at or --not-before it runs as soon as it is approved."""
    params: dict[str, Any] = json.loads(params_file.read_text()) if params_file else {}
    for pair in param or []:
        if "=" not in pair:
            _fail(InvalidRequest(f"--param needs key=value, got {pair!r}"))
        k, v = pair.split("=", 1)
        try:
            params[k] = json.loads(v)
        except ValueError:
            params[k] = v
    action: dict[str, Any] = {"on": on}
    for key, value in (("flow", flow), ("connector", connector), ("trigger", trigger)):
        if value:
            action[key] = value
    if params:
        action["params"] = params
    when = {k: t.isoformat() for k, t in (("at", _time(at, "at")), ("not_before", _time(not_before, "not-before")),
                                          ("not_after", _time(not_after, "not-after"))) if t is not None}
    svc = _service()
    try:
        e = svc.draft(title=title, action=action, when=when, notes=notes, principal=OWNER)
        if approve:
            e = asyncio.run(svc.approve(e.id, by=BY))
    except OpenajarError as exc:
        _fail(exc)
    _after(e, "add")
