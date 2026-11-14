"""`agentos lanebench`: run the local lane benchmark (tests/lanebench, docs in its README.md).

The benchmark lives in the source checkout's tests/lanebench, next to its fixture site and task
files, so this command works from a checkout (on a node, in a builder's dev slot) and says so
plainly elsewhere. It starts its own browsers on the slot's displays and its own fixture site on
the slot's ports; it never touches the node's lanes, its daemon or a browser it did not start.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True, help="The local lane benchmark: test sites, tasks and safety checks.")

EXECUTORS = ("replay", "mcp-agent", "jev", "pilot")


def bench_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "tests" / "lanebench"


def _import_bench() -> None:
    root = bench_dir()
    if not (root / "harness.py").is_file():
        typer.secho("error: lanebench runs from a source checkout (tests/lanebench is not next to this install)",
                    err=True, fg=typer.colors.RED)
        raise typer.Exit(2)
    tests = str(root.parent)
    if tests not in sys.path:
        sys.path.insert(0, tests)


@app.command("list")
def list_tasks(
    tasks: Annotated[list[str] | None, typer.Option("--tasks", "-t", help="Task ids, tags or id* prefixes.")] = None,
) -> None:
    """List the tasks (id, mode, the page changes they face, and the adversarial test pages)."""
    _import_bench()
    from lanebench.tasks import load_all

    for t in load_all(tasks or None):
        extra = ", ".join([m["kind"] for m in t.mutation] + t.adversarial) or "-"
        typer.echo(f"{t.id:24} {t.mode:13} {t.ceiling:7} {extra}")


@app.command("run")
def run(
    executor: Annotated[str,
                        typer.Option("--executor", "-e",
                                     help="replay, mcp-agent (an agent over MCP), jev (a separate browser "
                                          "agent) or pilot (a cheap worker that drives a lane).")] = "replay",
    tasks: Annotated[list[str] | None, typer.Option("--tasks", "-t",
                                                    help="Task ids, tags or id* prefixes (repeat or comma-separate). "
                                                         "Default: every task.")] = None,
    seed: Annotated[int, typer.Option(help="Seed for the feed order and the page changes; same seed, same "
                                           "pages.")] = 1,
    slot: Annotated[int | None, typer.Option(help="A number from 11 to 59 that gives this run its own "
                                                  "displays and ports (default: AGENTOS_TEST_SLOT).")] = None,
    out: Annotated[Path | None, typer.Option(help="Where the JSON report goes (a file or a folder; default "
                                                  "lanebench-<date>.json here).")] = None,
    agent_cmd: Annotated[str | None, typer.Option(help="mcp-agent: the agent command, with {config}, {prompt}, "
                                                       "{lane}, {url}, {token} and {transcript} filled in.")] = None,
    variant: Annotated[Path | None, typer.Option(help="mcp-agent: run with the agent-facing text in this folder "
                                                     "(instructions.md, tools.json, recipes.json, errors.json; "
                                                     "see export-text).")] = None,
    home: Annotated[Path | None, typer.Option(help="Where the run's throwaway home goes (default: the checkout's "
                                                   ".node-home/lanebench). Removed after the run.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON instead of the table.")] = False,
) -> None:
    """Run tasks on throwaway browser lanes (Camoufox, a Firefox build) and score them: success checks and
    safety checks."""
    if executor not in EXECUTORS:
        typer.secho(f"error: --executor must be one of {', '.join(EXECUTORS)}", err=True, fg=typer.colors.RED)
        raise typer.Exit(2)
    if variant is not None and executor != "mcp-agent":
        typer.secho("error: --variant changes what the MCP server says, so it needs --executor mcp-agent",
                    err=True, fg=typer.colors.RED)
        raise typer.Exit(2)
    _import_bench()
    from lanebench import executors, harness, report
    from lanebench.tasks import load_all

    names = [n.strip() for item in (tasks or []) for n in item.split(",") if n.strip()]
    try:
        chosen = load_all(names or None)
        bench_slot = harness.Slot.of(slot) if slot is not None else harness.Slot.from_env()
        text = _variant(variant) if variant is not None else None
    except ValueError as e:
        typer.secho(f"error: {e}", err=True, fg=typer.colors.RED)
        raise typer.Exit(2) from None
    options: dict = {}
    if executor == "mcp-agent":
        options = {"agent_cmd": agent_cmd, "variant": text}
    runner = executors.get(executor, **options)
    path = report.path_for(out)
    transcripts = report.transcripts_for(path) if executor == "mcp-agent" else None

    def progress(rec: dict) -> None:
        if not as_json:
            typer.echo(f"  {rec['id']}: {'pass' if rec['passed'] else 'FAIL'} ({rec.get('wall_s', 0):.0f} s)", err=True)

    rep = asyncio.run(harness.run_suite(chosen, runner, slot=bench_slot, seed=seed, root=home,
                                        transcripts=transcripts, variant=text, on_task=progress))
    path = report.write(rep, path)
    typer.echo(json.dumps(rep, indent=2, default=str) if as_json else report.summary(rep))
    typer.echo(f"\nReport: {path}", err=True)
    if transcripts is not None and transcripts.is_dir():
        typer.echo(f"Transcripts: {transcripts}", err=True)
    raise typer.Exit(0 if rep["summary"]["passed"] == rep["summary"]["tasks"] else 1)


def _bench_specs() -> list:
    """The tools the bench's MCP server serves, built on a bare runtime (only their text is read)."""
    import tempfile

    from lanebench.executors.mcp_agent import agent_specs

    from agentos.core.config import NodeConfig
    from agentos.core.runtime import Runtime

    with tempfile.TemporaryDirectory(prefix="lanebench-text-") as tmp:
        return agent_specs(Runtime(config=NodeConfig(home=Path(tmp))))


def _variant(folder: Path):
    from lanebench.variant import load

    return load(folder, tool_names=[s.name for s in _bench_specs()])


@app.command("export-text")
def export_text(
    folder: Annotated[Path, typer.Argument(help="The folder to write (made if missing; files in it are replaced).")],
) -> None:
    """Write the agent-facing text the bench serves (MCP instructions, tool descriptions, howto
    recipes, error next steps) in the format --variant reads."""
    _import_bench()
    from lanebench.variant import export

    counts = export(folder, _bench_specs())
    typer.echo(f"Wrote {folder}: instructions.md ({counts['instructions.md']} characters), "
               f"tools.json ({counts['tools.json']} tools), recipes.json ({counts['recipes.json']} recipes), "
               f"errors.json ({counts['errors.json']} codes).")
