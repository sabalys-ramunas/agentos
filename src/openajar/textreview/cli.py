"""`openajar text-review ...`: the weekly agent text review, run now or read back.

    openajar text-review run [--json]
    openajar text-review list [--json]
    openajar text-review show <id> [--json]
    openajar text-review settings [--json]

Every command refuses with plugin_disabled while the plugin is off. `run` also refuses inside a
lane or a brain task: it spends money on a brain, and only a person at the node starts that.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Annotated, Any

import typer

from openajar.core import catalog
from openajar.core.errors import OpenajarError
from openajar.core.tools import TOOL_CATALOG
from openajar.mcp.providers import ProviderCatalog
from openajar.plugins.errors import OnlyAPerson
from openajar.textreview import review
from openajar.textreview.bundle import Runs
from openajar.textreview.service import gate, inputs
from openajar.textreview.settings import PLUGIN, inbox_dir, load_settings, settings_path

app = typer.Typer(no_args_is_help=True, help="Agent text review: a weekly proposal of clearer text for agents, "
                                             "for a person to review. It never changes the node.")

# A test sets this. By default: the node config, its vault and brains, and the plugin registry.
runtime_factory: Callable[[], Any] | None = None

LANE_ENV, BRAIN_TASK_ENV = "OPENAJAR_LANE", "OPENAJAR_BRAIN_TASK"  # set by the lane and brain runners
NONE = "–"
JsonOpt = Annotated[bool, typer.Option("--json", help="Print it as JSON.")]


def _runtime() -> Any:
    runtime = runtime_factory() if runtime_factory is not None else _cli_runtime()
    if not runtime.has(TOOL_CATALOG):  # the text review reads the tools every build has
        runtime.register(TOOL_CATALOG, ProviderCatalog(runtime))
    return runtime


def _cli_runtime() -> Any:
    from openajar.commands.base import plugins_cli_runtime
    from openajar.plugins.service import build

    runtime = plugins_cli_runtime()
    if not runtime.has("plugins"):
        build(runtime)
    return runtime


def _fail(exc: OpenajarError) -> typer.Exit:
    typer.echo(catalog.cli_error(exc.code, exc.message), err=True)
    return typer.Exit(1)


def _on() -> Any:
    """The runtime, once the plugin is known to be on."""
    try:
        runtime = _runtime()
        gate(runtime.get("plugins"), in_daemon=False)
    except OpenajarError as exc:
        raise _fail(exc) from None
    return runtime


@contextlib.contextmanager
def _action_log(runtime: Any) -> Generator[Any]:
    """The node's action log, read where the CLI runs. None when it does not exist yet: the run
    then has only the inbox to go on."""
    if runtime.get("traces") is not None:
        yield runtime.get("traces")
        return
    path = Path(runtime.require("config").home) / "traces.db"
    if not path.exists():
        yield None
        return
    from openajar.policy.traces import TraceStore

    store = TraceStore(path, busy_timeout_ms=5000)
    try:
        yield store
    finally:
        store.close()


def _person_only() -> None:
    where = (f"brain task {os.environ[BRAIN_TASK_ENV]}" if os.environ.get(BRAIN_TASK_ENV) else
             f"lane {os.environ[LANE_ENV]}" if os.environ.get(LANE_ENV) else None)
    if where:
        raise OnlyAPerson(f"this is running inside {where}, and only a person starts a text review, which spends "
                          "on a brain. Run it in a shell on the node, outside any lane.")


def _summary(meta: dict[str, Any], folder: Path) -> None:
    counts = meta.get("counts") or {}
    typer.echo(f"{meta['id']}: {meta['status']}" + (f" ({meta['reason']})" if meta.get("reason") else ""))
    typer.echo(f"evidence: {counts.get('calls', 0)} calls in {counts.get('sessions', 0)} sessions, "
               f"{counts.get('probe_tasks', 0)} probe tasks")
    typer.echo(f"brain: {meta.get('brain') or NONE}, ${meta.get('cost_usd', 0):.2f} spent, "
               f"{meta.get('changes', 0)} changes")
    typer.echo(f"bundle: {folder}")


@app.command("run")
def run_now(as_json: JsonOpt = False) -> None:
    """Run a review now, whatever the schedule says. It reads the action log and the inbox, asks the
    brain on the review job, and writes a bundle. It changes nothing on the node."""
    runtime = _on()
    try:
        _person_only()
        with _action_log(runtime) as traces:
            meta = asyncio.run(review.run(inputs(runtime, in_daemon=False, trigger="cli", traces=traces)))
    except OpenajarError as exc:
        raise _fail(exc) from None
    folder = Runs(runtime.require("config").home).root / meta["id"]
    if as_json:
        typer.echo(json.dumps({**meta, "bundle": str(folder)}, indent=2))
        return
    _summary(meta, folder)
    typer.echo(f"Read it: openajar text-review show {meta['id']}")


@app.command("list")
def list_runs(as_json: JsonOpt = False) -> None:
    """Every review on this node, newest first."""
    runtime = _on()
    runs = Runs(runtime.require("config").home).list()
    if as_json:
        typer.echo(json.dumps({"runs": runs}, indent=2))
        return
    if not runs:
        typer.echo("No reviews yet. The first runs on the schedule, or now with `openajar text-review run`.")
        return
    rows = [[r["id"], r.get("status", "?"), r.get("trigger", "?"), str((r.get("counts") or {}).get("calls", 0)),
             str(r.get("changes", 0)), f"${r.get('cost_usd', 0):.2f}"] for r in runs]
    headers = ["ID", "STATUS", "BY", "CALLS", "CHANGES", "COST"]
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    for row in (headers, *rows):
        typer.echo("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())


@app.command("show")
def show_run(run_id: Annotated[str, typer.Argument(metavar="ID")], as_json: JsonOpt = False) -> None:
    """One review: what changed and why, and where its bundle is."""
    runtime = _on()
    runs = Runs(runtime.require("config").home)
    try:
        folder = runs.path(run_id)
        meta = runs.meta(run_id)
    except OpenajarError as exc:
        raise _fail(exc) from None
    if as_json:
        typer.echo(json.dumps({**meta, "bundle": str(folder),
                               "files": sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())},
                              indent=2))
        return
    typer.echo((folder / "CHANGES.md").read_text().rstrip())
    typer.echo("")
    typer.echo(f"bundle: {folder}")


@app.command("settings")
def show_settings(as_json: JsonOpt = False) -> None:
    """When reviews run, what they read and what they may spend."""
    runtime = _on()
    home = runtime.require("config").home
    settings = load_settings(home)
    if as_json:
        typer.echo(json.dumps(settings.model_dump(), indent=2))
        return
    typer.echo(f"Settings: {settings_path(home)}")
    typer.echo(f"Schedule: every {settings.interval_days:g} days, reading the last {settings.window_days:g} days of "
               f"the action log; a review needs at least {settings.min_calls} calls")
    typer.echo(f"Limits: ${settings.max_usd:.2f} a run, {settings.timeout_s:.0f} s an answer, "
               f"{settings.max_changes} changes asked for")
    typer.echo(f"Left out: subjects {', '.join(settings.skip_subjects) or NONE}; workspaces "
               f"{', '.join(settings.skip_workspaces) or NONE}")
    typer.echo(f"Lanebench reports: {inbox_dir(home, settings)}")
    typer.echo(f"Which brain reviews: openajar plugins show {PLUGIN}")
