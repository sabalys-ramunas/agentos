"""`openajar runs|flows|schedules|watches ...` on this node.

The CLI works on the node's runner database directly. It queues runs; the node daemon
executes them (it polls the queue), so `flows run` works whether or not an agent is
connected. Whoever runs the CLI has a shell on the node, so CLI runs count as the owner's:
they carry the owner grant, and flows saved here are owner-saved (a step may say
"publishes": false).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from openajar.core import catalog
from openajar.core.config import home_from_env
from openajar.core.errors import InvalidRequest, OpenajarError, PolicyDenied
from openajar.core.models import Principal, RunStatus
from openajar.core.runtime import Runtime
from openajar.runner import flowcli as _flowcli
from openajar.runner.flows import lint
from openajar.runner.runner import RunnerService
from openajar.runner.service import build

app = typer.Typer(no_args_is_help=True, help="Runs, flows, schedules and page watches on this node.")
runs_app = typer.Typer(no_args_is_help=True, help="Runs: list, show, cancel, decide.")
flows_app = typer.Typer(no_args_is_help=True, help="Saved flows: list, show, save, run, remove; and flows as files "
                        "in git: pull, diff, push, validate, test.")
schedules_app = typer.Typer(no_args_is_help=True, help="Cron schedules and webhook triggers.")
watches_app = typer.Typer(no_args_is_help=True, help="Page watches that start a flow when a page changes.")
app.add_typer(runs_app, name="runs")
app.add_typer(flows_app, name="flows")
app.add_typer(schedules_app, name="schedules")
app.add_typer(watches_app, name="watches")
apps = {"runs": runs_app, "flows": flows_app, "schedules": schedules_app, "watches": watches_app}

OWNER = Principal(kind="human", id="owner", owner="owner", grant_id="owner")  # root on the node acts as the owner
ParamOpt = Annotated[list[str] | None, typer.Option("--param", "-p", help="key=value (JSON values ok)")]
ParamsFileOpt = Annotated[Path | None, typer.Option("--params-file", help="a JSON file of params")]


def _service() -> RunnerService:
    from openajar.policy.ask_first import open_store

    home = home_from_env()
    runtime = Runtime()
    runtime.register("ask_first", open_store(home))  # flows show their "Ask me first"
    return build(runtime, home=home)


def _out(data: Any, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(data, indent=2, default=str))


def _fail(e: OpenajarError) -> None:
    typer.echo(catalog.cli_error(e.code, e.message), err=True)
    raise typer.Exit(1)


def _params(pairs: list[str] | None, file: Path | None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if file:
        params.update(json.loads(file.read_text()))
    for pair in pairs or []:
        if "=" not in pair:
            raise InvalidRequest(f"--param needs key=value, got {pair!r}")
        k, v = pair.split("=", 1)
        try:
            params[k] = json.loads(v)
        except ValueError:
            params[k] = v
    return params


def _run_line(d: dict[str, Any]) -> str:
    when = (d.get("finished_at") or d.get("started_at") or d["created_at"])[:19].replace("T", " ")
    return f"{d['id']}  {d['status']:<20}  {when}  {d.get('flow') or d['kind']:<32}  {d.get('lane') or '-'}"


# ---------------------------------------------------------------- runs


@runs_app.command("list")
def runs_list(status: str = typer.Option(None, help="comma-separated statuses"),
              flow: str = typer.Option(None), lane: str = typer.Option(None),
              limit: int = typer.Option(30), as_json: bool = typer.Option(False, "--json")) -> None:
    """List recent runs with their honest status."""
    svc = _service()
    try:
        st = [RunStatus(s) for s in status.split(",")] if status else None
    except ValueError:
        _fail(InvalidRequest(f"unknown status in {status!r}"))
    rows = [svc.describe(r) for r in svc.list(status=st, flow_id=flow, lane=lane, limit=limit)]
    if as_json:
        return _out(rows, True)
    if not rows:
        typer.echo("No runs.")
    for d in rows:
        typer.echo(_run_line(d))


@runs_app.command("show")
def runs_show(run_id: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Show a run: its status, proof, error and step-by-step log."""
    svc = _service()
    try:
        d = svc.describe(svc.get(run_id), steps=True)
    except OpenajarError as e:
        _fail(e)
    if as_json:
        return _out(d, True)
    typer.echo(_run_line(d))
    typer.echo(f"  {d['summary']}")
    if d.get("error"):
        err = d["error"]
        typer.echo(f"  error: {err.get('code')}: {err.get('message')}")
        if err.get("locator_text"):
            typer.echo(f"  locator: {err['locator_text']}  expected: {err.get('expected')}  found: {err.get('found')}")
        if err.get("screenshot"):
            typer.echo(f"  screenshot: {err['screenshot']}")
    for rb in (d.get("proof") or {}).get("readbacks", []):
        mark = {True: "pass", False: "FAIL", None: "unchecked"}[rb.get("passed")]
        typer.echo(f"  readback {rb.get('path')}: {mark}: {rb.get('check_text') or rb.get('expected')} "
                   f"(found {rb.get('found')})")
    for s in d.get("steps", []):
        typer.echo(f"  step {s['path']:<10} {s['type']:<10} {s['status']}")


@runs_app.command("cancel")
def runs_cancel(run_id: str) -> None:
    """Cancel a queued, waiting or running run."""
    svc = _service()
    try:
        run = asyncio.run(svc.cancel(run_id, by="owner (cli)"))
    except OpenajarError as e:
        _fail(e)
    if run.status == RunStatus.RUNNING:
        typer.echo(f"{run_id}: cancel requested; the run stops at its next step.")
    else:
        typer.echo(f"{run_id}: {run.status}")


@runs_app.command("decide")
def runs_decide(run_id: str, decision: str = typer.Argument(..., help="retry, skip or fail")) -> None:
    """Resolve a partial run that asked for a decision."""
    svc = _service()
    try:
        run = asyncio.run(svc.decide(run_id, decision, by="owner (cli)"))
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"{run_id}: {run.status}")


# ---------------------------------------------------------------- flows


@flows_app.command("list")
def flows_list(prefix: str = typer.Option(None),
               lane: str = typer.Option(None, help="Only flows that run on this lane (bound to it or to a pattern "
                                                   "that covers it)."),
               as_json: bool = typer.Option(False, "--json")) -> None:
    """List saved flows (the current version of each)."""
    svc = _service()
    rows = svc.flows.catalog(prefix, limit=10_000, lane=lane)["flows"] if lane else \
        [svc.flows.summary(fv) for fv in svc.flows.list(prefix)]
    if as_json:
        return _out(rows, True)
    if not rows:
        typer.echo("No flows saved.")
    for r in rows:
        last = r["last_run"]["status"] if r["last_run"] else "never run"
        how = "asks first" if r.get("ask_first") else "runs alone"
        typer.echo(f"{r['id']:<36} {r['version']}  {r['effect']:<7} {r['lane']:<24} {how:<10}  {last}")


@flows_app.command("show")
def flows_show(flow_id: str, version: str = typer.Option(None)) -> None:
    """Print a flow's definition as JSON."""
    svc = _service()
    try:
        fv = svc.flows.get(flow_id, version)
    except OpenajarError as e:
        _fail(e)
    typer.echo(json.dumps({"version": fv.version, **fv.definition.canonical()}, indent=2))


@flows_app.command("versions")
def flows_versions(flow_id: str) -> None:
    """List every saved version of a flow."""
    svc = _service()
    for fv in svc.flows.versions(flow_id):
        typer.echo(f"{fv.version}  {fv.created_at.isoformat()[:19]}  {fv.created_by or '-'}")


@flows_app.command("save")
def flows_save(file: Annotated[Path, typer.Argument(exists=True, readable=True)]) -> None:
    """Save a flow definition from a JSON file as a new version."""
    svc = _service()
    try:
        fv, created = svc.flows.save(json.loads(file.read_text()), by=OWNER)
    except ValueError as e:
        _fail(InvalidRequest(f"{file} is not valid JSON: {e}"))
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"{fv.id} {fv.version} {'saved' if created else 'unchanged (same content)'}")
    for w in lint(fv.definition, effect=fv.effect):
        typer.echo(f"  warning: {w}")


@flows_app.command("run")
def flows_run(flow_id: str, param: ParamOpt = None, params_file: ParamsFileOpt = None, on: str = typer.Option(None),
              wait: float = typer.Option(0.0, help="seconds to wait for the daemon to finish it"),
              key: str = typer.Option(None, "--idempotency-key")) -> None:
    """Queue a run of a saved flow; the node daemon executes it."""
    svc = _service()
    try:
        run, created = svc.submit(flow_id=flow_id, params=_params(param, params_file), lane=on,
                                  idempotency_key=key, principal=OWNER, source="cli", authorized=True)
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"{run.id} {'queued' if created else 'already exists: ' + str(run.status)}")
    deadline = time.monotonic() + wait
    while wait and time.monotonic() < deadline:
        run = svc.get(run.id)
        if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
            break
        time.sleep(0.5)
    if wait:
        typer.echo(f"{run.id} {run.status}: {svc.describe(run)['summary']}")


@flows_app.command("ask-first")
def flows_ask_first(
    words: Annotated[list[str] | None, typer.Argument(help="FLOW_ID [on|off], or with --account just on|off")] = None,
    account: Annotated[str | None, typer.Option("--account", help="a lane id: ask first for what agents do "
                                                "there outside any routine")] = None,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Ask me first: a routine (or an account) whose commits wait for you. Off by default: routines run
    on their own. With no arguments, list every setting. Payments and irreversible steps always ask."""
    from openajar.policy.ask_first import inside_an_agent, open_store

    store = open_store(home_from_env())
    args = list(words or [])
    chosen = True
    try:
        if account is not None:
            if len(args) > 1:
                raise InvalidRequest("with --account, give only on or off")
            scope, ident, state = "lane", account, args[0] if args else None
        elif not args:
            rows = [s.public() for s in store.list()]
            if as_json:
                return _out(rows, True)
            if not rows:
                typer.echo("Every routine and account runs on its own (no Ask me first set).")
            for r in rows:
                how = "asks first" if r["ask_first"] else "runs alone"
                typer.echo(f"{r['scope']:<5} {r['id']:<40} {how:<10}  set by {r['set_by']} at {r['set_at']}")
            return None
        elif len(args) > 2:
            raise InvalidRequest("give a flow id and on or off")
        else:
            scope, ident, state = "flow", args[0], args[1] if len(args) > 1 else None
        if state is None:
            s = store.get(scope, ident)
            on, chosen = store.is_on(scope, ident), s is not None
        else:
            if state not in ("on", "off"):
                raise InvalidRequest(f"{state!r}: say on or off")
            agent = inside_an_agent()
            if agent and state == "off":
                raise PolicyDenied("this is running inside a lane or a brain task, and only a person turns Ask me "
                                   "first off. Run it in a shell on the node, outside any lane. Nothing was changed.")
            s = store.set(scope, ident, state == "on", by="agent:cli" if agent else "owner:cli", person=not agent)
            on = s.ask_first
    except OpenajarError as e:
        _fail(e)
    if as_json:
        return _out(s.public() if s else {"scope": scope, "id": ident, "ask_first": on, "chosen": False}, True)
    what = f"routine {ident}" if scope == "flow" else f"account {ident}"
    if scope == "lane" and not chosen and not on:
        typer.echo(f"{what}: no choice made yet. It runs on its own, except that words the browser worker wrote "
                   "still ask you. Say on or off to choose.")
        return None
    typer.echo(f"{what}: " + ("asks you first before it posts, sends or changes anything" if on
                              else "runs on its own (payments and things that cannot be undone still ask)"))
    return None


@flows_app.command("remove")
def flows_remove(flow_id: str) -> None:
    """Delete a flow and all its versions (runs keep their records). A workflow (routine) id works too:
    the two share ids (docs/WORKFLOWS.md), so it removes that workflow."""
    svc = _service()
    if svc.flows.delete(flow_id):
        typer.echo(f"removed {flow_id}")
        return
    if svc.workflows.store.state(flow_id) is None:
        _fail(InvalidRequest(f"no flow or workflow {flow_id!r} on this node (see `openajar routines flows list` and "
                             "`openajar routines workflows list`)"))
    from openajar.events.service import build as build_events
    from openajar.runner.workflows.service import Actor

    build_events(svc.workflows.runtime)  # a workflow's route lives in the events store
    try:
        svc.workflows.delete(flow_id, Actor.owner("cli"))
    except OpenajarError as exc:
        _fail(exc)
    typer.echo(f"removed workflow {flow_id}")


# ---------------------------------------------------------------- schedules


def _schedule_line(d: dict[str, Any]) -> str:
    what = (d.get("cron") or {}).get("cron") or ("webhook" if d.get("webhook") else "watch")
    nxt = d.get("next_run", "")[:16].replace("T", " ") if d.get("next_run") else ""
    # A workflow's schedule has no flow and no lane: it starts a route (finding 32 of 2026-11-10).
    target = d.get("flow_id") or (f"workflow {d['route_id']}" if d.get("route_id") else "-")
    return f"{d['id']}  {'on ' if d.get('enabled') else 'off'}  {target:<32} {d.get('lane') or '-':<24} {what} {nxt}"


@schedules_app.command("list")
def schedules_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """List cron schedules and webhook triggers."""
    svc = _service()
    rows = [svc.scheduler.describe(s) for s in svc.scheduler.list() if s.kind != "watch"]
    if as_json:
        return _out(rows, True)
    if not rows:
        typer.echo("No schedules.")
    for d in rows:
        typer.echo(_schedule_line(d))


@schedules_app.command("show")
def schedules_show(schedule_id: str) -> None:
    """Show a schedule as JSON."""
    svc = _service()
    try:
        typer.echo(json.dumps(svc.scheduler.describe(svc.scheduler.get(schedule_id)), indent=2, default=str))
    except OpenajarError as e:
        _fail(e)


@schedules_app.command("add")
def schedules_add(flow: str, cron: str = typer.Option(None, help="cron expression, e.g. '0 9 * * 1-5'"),
                  tz: str = typer.Option(None, "--tz", help="timezone (default Europe/Vilnius)"),
                  missed: str = typer.Option(None, help="skip or catch_up_once"),
                  jitter: int = typer.Option(None, help="random delay up to N seconds"),
                  webhook: bool = typer.Option(False, help="a webhook trigger instead of cron"),
                  accept_params: bool = typer.Option(False, help="webhook body may carry params"),
                  param: ParamOpt = None, params_file: ParamsFileOpt = None, on: str = typer.Option(None),
                  deadman: int = typer.Option(None, help="alert if no successful run for N minutes")) -> None:
    """Add a cron schedule or a webhook trigger for a flow."""
    svc = _service()
    try:
        s, extras = svc.scheduler.add(
            flow=flow, params=_params(param, params_file), on=on, cron=cron, timezone=tz, missed=missed,
            jitter_s=jitter, webhook={"accept_params": accept_params} if webhook else None,
            deadman_minutes=deadman, principal=OWNER)
    except OpenajarError as e:
        _fail(e)
    typer.echo(_schedule_line(svc.scheduler.describe(s)))
    if extras:
        typer.echo(f"  url path: {extras['url_path']}")
        typer.echo(f"  secret (shown once): {extras['secret']}")
        typer.echo(f"  sign: {extras['signature']}")


@schedules_app.command("remove")
def schedules_remove(schedule_id: str) -> None:
    """Remove a schedule."""
    svc = _service()
    try:
        svc.scheduler.remove(schedule_id)
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"removed {schedule_id}")


@schedules_app.command("enable")
def schedules_enable(schedule_id: str) -> None:
    """Turn a schedule on (it does not replay the time it was off)."""
    svc = _service()
    try:
        svc.scheduler.set_enabled(schedule_id, True)
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"{schedule_id} on")


@schedules_app.command("disable")
def schedules_disable(schedule_id: str) -> None:
    """Turn a schedule off."""
    svc = _service()
    try:
        svc.scheduler.set_enabled(schedule_id, False)
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"{schedule_id} off")


# ---------------------------------------------------------------- watches


@watches_app.command("list")
def watches_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """List page watches."""
    svc = _service()
    rows = [svc.scheduler.describe(s) for s in svc.scheduler.list("watch")]
    if as_json:
        return _out(rows, True)
    if not rows:
        typer.echo("No watches.")
    for d in rows:
        w = d["watch"]
        last = d["state"].get("last_value", "(no read yet)")
        typer.echo(f"{d['id']}  {'on ' if d['enabled'] else 'off'}  {d['flow_id']:<32} every {w['every_s']}s  "
                   f"{w['url']}  last: {str(last)[:40]}")


@watches_app.command("add")
def watches_add(flow: str, url: str = typer.Option(...), role: str = typer.Option(None), name: str = typer.Option(None),
                within_role: str = typer.Option(None), within_name: str = typer.Option(None),
                css: str = typer.Option(None), locator_json: str = typer.Option(None, "--locator"),
                read: str = typer.Option("text", help="text or count"), every: int = typer.Option(300),
                debounce: int = typer.Option(0), on: str = typer.Option(None),
                param: ParamOpt = None, deadman: int = typer.Option(None)) -> None:
    """Watch an element's text or count on a page; start the flow when it changes."""
    svc = _service()
    if locator_json:
        locator = json.loads(locator_json)
    else:
        locator = {k: v for k, v in (("role", role), ("name", name), ("css", css)) if v}
        if within_role:
            locator["within"] = {k: v for k, v in (("role", within_role), ("name", within_name)) if v}
    try:
        s, _ = svc.scheduler.add(flow=flow, on=on, params=_params(param, None), deadman_minutes=deadman,
                                 watch={"url": url, "locator": locator, "read": read, "every_s": every,
                                        "debounce_s": debounce}, principal=OWNER)
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"{s.id} watching {url} every {every}s")


@watches_app.command("remove")
def watches_remove(watch_id: str) -> None:
    """Remove a page watch."""
    schedules_remove(watch_id)


# ---------------------------------------------------------------- flows as files (openajar.runner.flowcli)

_flowcli.register(flows_app)
