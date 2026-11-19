"""`agentos plugins ...`: which workers this node has, and what a person turns on.

    agentos plugins list [--json]
    agentos plugins show <id> [--json]
    agentos plugins enable <id>
    agentos plugins disable <id>
    agentos plugins assign <id> <job> <brain>
    agentos plugins unassign <id> <job>
    agentos plugins lane <lane> [on|off] [--json]

Every change goes in the node's action log, and `show` reads it back. This is the only way to turn a plugin on. It writes $AGENTOS_HOME/plugins.db; the daemon reads what
to load when it starts.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Generator
from typing import Annotated, Any

import typer

from agentos.core import catalog
from agentos.core.config import load_config
from agentos.core.errors import AgentosError, InvalidRequest
from agentos.core.runtime import Runtime
from agentos.plugins import admin
from agentos.plugins.errors import ChangeNotRecorded
from agentos.plugins.models import Change, ChangeLog
from agentos.plugins.service import LANE_WORKER, PluginService, PluginView, build

app = typer.Typer(no_args_is_help=True, help="Plugins: the workers this node can run, and who does their jobs.")

# The composition root (agentos.cli) sets this, so `assign` can read the brains without this
# package importing them. By default the runtime has the node config alone.
runtime_factory: Callable[[], Runtime] | None = None

# It sets this too: the node's action log, which this package cannot import either. Without one, a
# change is refused, because it could not be recorded.
change_log_factory: Callable[[Runtime], ChangeLog] | None = None

NONE = "–"
HISTORY_SHOWN = 20
RESTART = "sudo systemctl restart agentosd"
JsonOpt = Annotated[bool, typer.Option("--json", help="Print it as JSON.")]


def _service() -> PluginService:
    runtime = runtime_factory() if runtime_factory is not None else Runtime(config=load_config())
    return runtime.get("plugins") or build(runtime)


def _run(action: Callable[[PluginService], Any]) -> Any:
    try:
        return action(_service())
    except AgentosError as exc:
        typer.echo(catalog.cli_error(exc.code, exc.message), err=True)
        raise typer.Exit(1) from None


class NoLog:
    """No action log in this build: every record fails, so `disable` alone still goes through."""

    def record(self, change: Change) -> None:
        raise ChangeNotRecorded("this build has no action log")

    def read(self, plugin: str, limit: int) -> list[dict[str, Any]]:
        raise ChangeNotRecorded("this build has no action log")

    def close(self) -> None:
        pass


@contextlib.contextmanager
def _change_log(service: PluginService) -> Generator[ChangeLog]:
    log = change_log_factory(service.runtime) if change_log_factory is not None else NoLog()
    try:
        yield log
    finally:
        with contextlib.suppress(Exception):
            log.close()


def _change(action: Callable[[PluginService, ChangeLog], Any]) -> Any:
    def go(service: PluginService) -> Any:
        with _change_log(service) as log:
            return action(service, log)

    return _run(go)


def _history(service: PluginService, plugin_id: str) -> tuple[list[dict[str, Any]], str | None]:
    """The newest changes first, and why they could not be read. `show` works either way."""
    try:
        with _change_log(service) as log:
            return log.read(plugin_id, HISTORY_SHOWN), None
    except AgentosError as exc:
        return [], exc.message
    except Exception as exc:  # noqa: BLE001  (a person may not be able to open the log; the rest still shows)
        return [], type(exc).__name__


def _warn_if_broken(service: PluginService) -> None:
    problem = service.state().problem
    if problem:
        typer.echo(f"warning: {problem}. Nothing is on until it is repaired.", err=True)


def _table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    for row in (headers, *rows):
        typer.echo("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())


def _jobs(view: PluginView) -> str:
    return ", ".join(f"{job['job']}: {job['brain'] or NONE}" for job in view.jobs) or NONE


def _why(view: PluginView) -> str:
    if view.failure:
        return view.failure
    unmet = view.unmet
    if not unmet:
        return NONE
    return unmet[0] if len(unmet) == 1 else f"{unmet[0]} (and {len(unmet) - 1} more)"


@app.command("list")
def list_plugins(as_json: JsonOpt = False) -> None:
    """Every worker in this build: its status, who does its jobs, and why it cannot run."""
    def show(service: PluginService) -> None:
        views = service.views()
        if as_json:
            typer.echo(json.dumps({"plugins": [v.to_dict() for v in views], "problem": service.state().problem},
                                  indent=2))
            return
        _warn_if_broken(service)
        _table([[v.id, v.status, v.stage, _jobs(v), _why(v)] for v in views],
               ["ID", "STATUS", "STAGE", "JOBS", "WHY"])

    _run(show)


@app.command("show")
def show_plugin(plugin_id: Annotated[str, typer.Argument(metavar="ID")], as_json: JsonOpt = False) -> None:
    """One worker in full: each requirement with its reason, each job with its brain, and who changed it."""
    def show(service: PluginService) -> None:
        view = service.view(plugin_id)
        history, unreadable = _history(service, plugin_id)
        if as_json:
            typer.echo(json.dumps({**view.to_dict(), "history": history, "history_problem": unreadable}, indent=2))
            return
        _warn_if_broken(service)
        typer.echo(f"{view.name} ({view.id})")
        typer.echo(view.description)
        typer.echo(f"status: {view.status}")
        typer.echo(f"stage: {view.stage}")
        typer.echo(f"lanes: {', '.join(view.lane_kinds) or NONE}")
        typer.echo(f"off on lanes: {', '.join(view.lanes_off) or NONE}")
        if view.failure:
            typer.echo(f"failed: {view.failure}")
        typer.echo("requirements:")
        for check in view.checks:
            typer.echo(f"  {'met' if check.ok else 'not met'}: {check.reason}")
        typer.echo("jobs:")
        for job in view.jobs:
            needs = f"optional, needs {job['needs']}" if job["optional"] else f"needs {job['needs']}"
            typer.echo(f"  {job['job']}: {job['brain'] or NONE} ({needs}). {job['description']}")
            if job["optional"]:
                typer.echo(f"    without it: {job['without']}")
        typer.echo("history:")
        if unreadable:
            typer.echo(f"  could not be read: {unreadable}")
        elif not history:
            typer.echo(f"  {NONE}")
        for row in history:
            typer.echo(f"  {row['at']}  {row['principal']}  {row['summary']}")

    _run(show)


@app.command("enable")
def enable(plugin_id: Annotated[str, typer.Argument(metavar="ID")]) -> None:
    """Turn a worker on. Refused, with the exact reasons, until every requirement is met."""
    _change(lambda service, log: admin.enable(service, plugin_id, log))
    typer.echo(f"{plugin_id} is enabled. It loads the next time agentosd starts: `{RESTART}`.")


@app.command("disable")
def disable(plugin_id: Annotated[str, typer.Argument(metavar="ID")]) -> None:
    """Turn a worker off."""
    _change(lambda service, log: admin.disable(service, plugin_id, log))
    typer.echo(f"{plugin_id} is disabled. Its tools refuse calls from now on. Anything it started keeps "
               f"running until agentosd next starts: `{RESTART}`.")


@app.command("lane")
def lane_switch(
    lane: Annotated[str, typer.Argument(metavar="LANE", help="A lane id, for example personal/reddit.")],
    state: Annotated[str | None, typer.Argument(metavar="[on|off]", help="Leave it out to see the switch.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """The browser worker on one lane: on by default. Off stops browser_task there, and agents and a person
    can still drive the lane directly."""
    def change(service: PluginService, log: ChangeLog) -> None:
        if state not in ("on", "off"):
            raise InvalidRequest(f"{state!r}: say on or off")
        admin.set_lane(service, lane, state == "on", log)

    if state is not None:
        _change(change)
    service = _run(lambda found: found)
    _warn_if_broken(service)
    on = lane not in service.state().off_on(LANE_WORKER) and service.state().problem is None
    if as_json:
        typer.echo(json.dumps({"lane": lane, "plugin": LANE_WORKER, "on": on}, indent=2))
    elif on:
        typer.echo(f"{lane}: the browser worker is on here. browser_task can run, if {LANE_WORKER} is enabled on this node.")
    else:
        typer.echo(f"{lane}: the browser worker is off here. browser_task is refused, and any run stops at its next step. "
                   "Agents can still use the browser tools, and a person can take over.")


@app.command("assign")
def assign(plugin_id: Annotated[str, typer.Argument(metavar="ID")], job: str, brain: str) -> None:
    """Give one of a worker's jobs to a brain. A brain that cannot do the job is refused."""
    _change(lambda service, log: admin.assign(service, plugin_id, job, brain, log))
    typer.echo(f"The {job} job of {plugin_id} now goes to {brain}.")


@app.command("unassign")
def unassign(plugin_id: Annotated[str, typer.Argument(metavar="ID")], job: str) -> None:
    """Take a job away from its brain. `show` says what the worker does without it."""
    def clear(service: PluginService, log: ChangeLog) -> tuple[str | None, str]:
        brain = admin.unassign(service, plugin_id, job, log)
        found = service.get(plugin_id).job(job)
        return brain, found.without if found and found.optional else (
            f"{plugin_id} does not load until a brain does this job.")

    brain, after = _change(clear)
    typer.echo(f"The {job} job of {plugin_id} had no brain." if brain is None else
               f"{brain} no longer does the {job} job of {plugin_id}. {after}")
