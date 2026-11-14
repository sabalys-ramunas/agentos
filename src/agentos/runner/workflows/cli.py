"""`agentos workflows`: a developer keeps a workflow as a file and applies it.

Whoever runs the CLI on the node is a person, as for `agentos flows ask-first`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
import yaml

from agentos.core.errors import AgentosError
from agentos.runner.cli import _service
from agentos.runner.workflows.service import Actor, WorkflowInvalid

app = typer.Typer(no_args_is_help=True, help="Workflows: a trigger, then steps. Apply, run, retry, see what happened.")
leads_app = typer.Typer(no_args_is_help=True, help="Leads an agent proposed: read them, take them, drop them, erase them.")
app.add_typer(leads_app, name="leads")
PERSON = Actor.owner("cli")


def _svc() -> Any:
    """The node's workflows, and its events store, since turning one on writes a route. The daemon
    reads both from disk, so what is changed here is what it runs."""
    from agentos.events.service import build

    runner = _service()
    build(runner.workflows.runtime)
    return runner.workflows


def _say(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _guard(fn: Any) -> Any:
    try:
        return fn()
    except WorkflowInvalid as e:
        for p in e.problems:
            typer.echo(f"{p['target']}: {p['words']}", err=True)
        raise typer.Exit(1) from e
    except AgentosError as e:
        typer.echo(e.message, err=True)
        raise typer.Exit(1) from e


def read_file(path: Path) -> dict[str, Any]:
    """A definition from a JSON or YAML file."""
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise typer.BadParameter(f"{path} does not hold a workflow")
    return data


@app.command("apply")
def apply(path: Path, enable: bool = typer.Option(False, "--enable", help="Turn it on as well.")) -> None:
    """Save a workflow from a JSON or YAML file. The same file again changes nothing."""
    svc = _svc()
    out = _guard(lambda: svc.save(read_file(path), PERSON))
    if enable:
        out = _guard(lambda: svc.enable(out["id"], PERSON))
    typer.echo(f"{out['id']} {out['version']} ({'on' if out['enabled'] else 'off'})")
    for item in out["missing"]:
        typer.echo(f"  still to do: {item['words']}")
    if out.get("secret"):
        typer.echo(f"  webhook token: {out['trigger']['hook']['token']}")
        typer.echo(f"  webhook secret, shown once: {out['secret']}")


@app.command("export")
def export(flow_id: str) -> None:
    """Print a workflow as a file you can keep in a repository."""
    wv = _guard(lambda: _svc().get_version(flow_id))
    _say(wv.definition.canonical())


@app.command("list")
def list_(as_json: bool = typer.Option(False, "--json")) -> None:
    """List workflows."""
    rows = _svc().list()
    if as_json:
        _say(rows)
        return
    if not rows:
        typer.echo("No workflows yet. Save one from a file with `agentos routines workflows apply FILE` "
                   "(`agentos routines workflows templates` lists ready ones).")
        return
    for r in rows:
        typer.echo(f"{r['id']}  {'on' if r['enabled'] else 'off'}  {r['name']}")


@app.command("run")
def run(flow_id: str, values: str = typer.Option("{}", "--values", help="A JSON object for {{trigger.x}}.")) -> None:
    """Run a workflow now. The node's daemon carries it out."""
    _say(_guard(lambda: _svc().run(flow_id, PERSON, values=json.loads(values))))


@app.command("runs")
def runs(flow_id: str, limit: int = typer.Option(20, "--limit")) -> None:
    """Recent runs, with every step."""
    _say(_guard(lambda: _svc().runs(flow_id, limit=limit)))


@app.command("retry")
def retry(flow_id: str, run_id: str,
          force: bool = typer.Option(False, "--force", help="Retry a step that may already have acted.")) -> None:
    """Retry a failed step, from that step."""
    _say(_guard(lambda: _svc().retry(flow_id, run_id, PERSON, force=force)))


@app.command("enable")
def enable(flow_id: str, version: str | None = typer.Option(None, "--version")) -> None:
    """Turn a workflow on: its trigger starts it, and this version is the one that runs."""
    out = _guard(lambda: _svc().enable(flow_id, PERSON, version=version))
    typer.echo(f"{out['id']} is on ({out['live_version']})")
    if out.get("secret"):
        typer.echo(f"  webhook secret, shown once: {out['secret']}")


@app.command("disable")
def disable(flow_id: str) -> None:
    """Turn a workflow off: nothing starts it, though you can still run it by hand."""
    _guard(lambda: _svc().disable(flow_id, PERSON))
    typer.echo(f"{flow_id} is off")


@app.command("watch")
def watch(flow_id: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """How a site_change trigger stands: what it last read, and why it stopped if it did."""
    row = _guard(lambda: _svc().public(flow_id))
    found = row.get("watch")
    if found is None:
        typer.echo(f"{flow_id} does not watch a site.", err=True)
        raise typer.Exit(1)
    if as_json:
        _say(found)
        return
    typer.echo(found["words"])
    typer.echo(f"  rows: {found['rows'] if found['rows'] is not None else 'none read yet'}")
    typer.echo(f"  last check: {found['last_checked_at'] or 'none yet'}, next: {found['next_at'] or 'none'}")


@app.command("templates")
def templates(template: str | None = typer.Argument(None), workspace: str | None = typer.Option(None, "--workspace")) -> None:
    """List the templates, or print one as a file to edit."""
    svc = _svc()
    if template is None:
        for t in svc.templates():
            typer.echo(f"{t['id']}: {t['sentence']}")
        return
    _say(_guard(lambda: svc.from_template(template, workspace)))


@leads_app.command("list")
def leads_list() -> None:
    """What waits for you, and the note for a first list of leads."""
    _say(_guard(lambda: _svc().leads.review(in_person=True)))


@leads_app.command("show")
def leads_show(batch: str) -> None:
    """Every lead of a batch, with where it came from. All of it is text from outside."""
    _say(_guard(lambda: _svc().leads.batch(batch, in_person=True)))


@leads_app.command("take")
def leads_take(batch: str, only: str | None = typer.Option(None, "--only", help="Keep only these, by commas; the rest are dropped.")) -> None:
    """Put a batch on its list: all of it, or the addresses you name."""
    take = only.split(",") if only else "all"
    _say(_guard(lambda: _svc().leads.decide(batch, take, in_person=True, by="owner:cli")))


@leads_app.command("drop")
def leads_drop(batch: str) -> None:
    """Drop a batch. The agents that proposed it stay away from those addresses for a while."""
    _say(_guard(lambda: _svc().leads.decide(batch, [], in_person=True, by="owner:cli")))


@leads_app.command("export")
def leads_export(list_id: str) -> None:
    """Each lead of a list, with its source and when it was added."""
    _say(_guard(lambda: _svc().leads.export(list_id, in_person=True)))


@leads_app.command("erase")
def leads_erase(workspace: str, email: str) -> None:
    """Erase a lead and its source. A suppression stays, and so does the record of what was sent."""
    _say(_guard(lambda: _svc().leads.erase(workspace, email, in_person=True)))


@leads_app.command("seen")
def leads_seen() -> None:
    """Say you have read the note on who answers for this mail."""
    typer.echo("Noted." if _guard(lambda: _svc().leads.acknowledge(in_person=True, by="owner:cli")) else "Already noted.")
