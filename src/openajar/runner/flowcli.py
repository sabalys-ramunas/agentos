"""`openajar flows pull|push|diff|validate|test|capture`: flows as files, reviewed and tested in git.

pull, push and diff go to a node through a connection (openajar.client.call), as an agent on this
computer would, so a push is a normal flow_save under the connection's grant and policy. validate
and test run here, offline: validate is the node's own check of a flow (plus where the file sits and
that it holds no secret), test replays each flow on its recordings (openajar.runner.flowtest).
docs/WORKFLOWS.md ("Flows in git") is the walkthrough.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Annotated, Any

import typer

from openajar.client.call import NodeOpt, UrlOpt
from openajar.client.errors import ClientError
from openajar.core.errors import InvalidRequest

DEFAULT_DIR = Path("flows")
PathsArg = Annotated[list[Path] | None, typer.Argument(help="Flow folders or files (default: ./flows).",
                                                        show_default=False)]
WorkspaceOpt = Annotated[str | None, typer.Option("--workspace", "-w", help="Only the flows of this workspace.")]
WaitOpt = Annotated[float, typer.Option("--wait", help="Seconds to wait when a save waits for a person's yes.")]


def _paths(paths: list[Path] | None) -> list[Path]:
    return list(paths) if paths else [DEFAULT_DIR]


def _fail(message: str, hint: str | None = None) -> typer.Exit:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    if hint:
        typer.echo(f"  {hint}", err=True)
    return typer.Exit(1)


def _show_bad(files: list[Any]) -> None:
    for ff in files:
        for e in ff.errors:
            typer.echo(f"{ff.path}: {e}", err=True)


# ---- validate ---------------------------------------------------------------------------------


def validate(paths: PathsArg = None, workspace: WorkspaceOpt = None,
             strict: Annotated[bool, typer.Option("--strict", help="Warnings fail too.")] = False) -> None:
    """Check flow files offline, as the node checks a flow before saving it.

    Also checks that each file is named after its flow, sits in its workspace's folder and holds no
    secret, and that the tests and recordings next to them read. Exit 1 on an error (or, with
    --strict, a warning)."""
    from openajar.runner.flowsync import load_paths
    from openajar.runner.flowtest import validate_extras

    try:
        loaded = load_paths(_paths(paths), workspace)
        extras = validate_extras(_paths(paths))
    except InvalidRequest as e:
        raise _fail(e.message) from None
    errors = warnings = 0
    for ff in loaded.files:
        for problem in ff.errors:
            typer.echo(f"{ff.path}: error: {problem}")
        for w in ff.warnings:
            typer.echo(f"{ff.path}: warning: {w}")
        errors += len(ff.errors)
        warnings += len(ff.warnings)
    for path, problem in extras:
        typer.echo(f"{path}: error: {problem}")
    errors += len(extras)
    if not loaded.files:
        raise _fail(f"no flow files in {', '.join(map(str, _paths(paths)))}",
                    "a flow file is <flow id>.yaml; `openajar flows pull` writes them")
    ok = len([f for f in loaded.files if f.ok])
    typer.echo(f"{ok} of {len(loaded.files)} flows valid, {errors} errors, {warnings} warnings")
    if errors or (strict and warnings):
        raise typer.Exit(1)


# ---- test -------------------------------------------------------------------------------------


def run_tests(paths: PathsArg = None,
         as_json: Annotated[bool, typer.Option("--json", help="Print the results as JSON.")] = False,
         verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show each run's steps.")] = False) -> None:
    """Run the flows' tests against their recordings, offline.

    Each <flow id>.test.yaml next to a flow runs it on its recordings: no browser, no account, no
    node. Exit 1 when a case fails."""
    from openajar.runner.flowtest import find_tests, run_test_file

    try:
        tests = find_tests(_paths(paths))
    except InvalidRequest as e:
        raise _fail(e.message) from None
    if not tests:
        raise _fail(f"no tests in {', '.join(map(str, _paths(paths)))}",
                    "a test is <flow id>.test.yaml next to the flow (docs/WORKFLOWS.md, Flows in git)")

    async def run_all() -> list[Any]:
        return [await run_test_file(t) for t in tests]

    results = asyncio.run(run_all())
    cases = [c for r in results for c in r.cases]
    failed = [c for c in cases if not c.passed] + [r for r in results if r.error]
    if as_json:
        typer.echo(json.dumps([{"file": str(r.path), "flow": r.flow, "error": r.error, "passed": r.passed,
                                "cases": [{"name": c.name, "passed": c.passed, "problems": c.problems,
                                           "status": c.run.get("status"), "published": c.published}
                                          for c in r.cases]} for r in results], indent=2, default=str))
    else:
        for r in results:
            typer.echo(f"{r.path} ({r.flow or '?'})")
            if r.error:
                typer.echo(f"  ERROR {r.error}")
            for c in r.cases:
                typer.echo(f"  {'PASS' if c.passed else 'FAIL'} {c.name}  ({c.run.get('status', 'did not run')})")
                for p in c.problems:
                    typer.echo(f"       {p}")
                if verbose:
                    for s in c.run.get("steps") or []:
                        typer.echo(f"       step {s['path']:<8} {s['type']:<16} {s['status']}")
        typer.echo(f"{len(cases) - len([c for c in cases if not c.passed])} of {len(cases)} cases passed"
                   + (f", {len([r for r in results if r.error])} test files could not run"
                      if any(r.error for r in results) else ""))
    if failed:
        raise typer.Exit(1)


# ---- pull, diff, push -------------------------------------------------------------------------


def pull(directory: Annotated[Path, typer.Argument(help="The flows folder.")] = DEFAULT_DIR,
         workspace: WorkspaceOpt = None, node: NodeOpt = None, url: UrlOpt = None) -> None:
    """Write the node's flows into a folder, one YAML file each.

    The layout is <workspace>/<flow id>.yaml. Files that already hold the same flow are left as they
    are. Never writes a secret: a flow that seems to hold one is refused and named."""
    from openajar.client.call import open_session
    from openajar.runner.flowsync import pull as do_pull

    try:
        with open_session(node, url) as session:
            report = do_pull(session, directory, workspace=workspace)
    except (ClientError, InvalidRequest) as e:
        raise _fail(e.message, getattr(e, "hint", None)) from None
    for p in report.written:
        typer.echo(f"wrote {p}")
    for fid, why in report.refused:
        typer.echo(f"refused {fid}: {why}", err=True)
    for p in report.only_here:
        typer.echo(f"only here {p} (the node has no such flow; `openajar flows push` saves it)")
    typer.echo(f"{len(report.written)} written, {len(report.unchanged)} unchanged, {len(report.refused)} refused")
    if report.refused:
        raise typer.Exit(1)


def _changes(paths: list[Path], workspace: str | None, session: Any, *, with_node_only: bool) -> list[Any]:
    from openajar.runner.flowsync import compare, load_paths

    loaded = load_paths(paths, workspace)
    if loaded.bad:
        _show_bad(loaded.bad)
        raise _fail(f"{len(loaded.bad)} flow files do not validate; nothing was compared",
                    "see `openajar flows validate`")
    return compare(session, loaded.files, workspace=workspace, with_node_only=with_node_only)


def _print_changes(changes: list[Any]) -> None:
    for c in changes:
        if c.status in ("new", "changed"):
            typer.echo(c.diff(), nl=False)
        elif c.status == "only_on_node":
            typer.echo(f"only on the node: {c.flow_id} (pull writes it; push never removes a flow)")


def diff(paths: PathsArg = None, workspace: WorkspaceOpt = None, node: NodeOpt = None, url: UrlOpt = None,
         exit_code: Annotated[bool, typer.Option("--exit-code", help="Exit 1 when anything differs.")] = False
         ) -> None:
    """Show how the flow files differ from the node's flows.

    A unified diff of each flow, as its file would read. A file in another layout, or with comments,
    is not a difference."""
    from openajar.client.call import open_session

    try:
        with open_session(node, url) as session:
            folders = all(p.is_dir() for p in _paths(paths))  # a folder holds all of them: show the rest too
            changes = _changes(_paths(paths), workspace, session, with_node_only=folders)
    except (ClientError, InvalidRequest) as e:
        raise _fail(e.message, getattr(e, "hint", None)) from None
    _print_changes(changes)
    differ = [c for c in changes if c.status != "same"]
    typer.echo(f"{len([c for c in changes if c.status in ('new', 'changed')])} to push, "
               f"{len([c for c in changes if c.status == 'same'])} the same"
               + (f", {len([c for c in changes if c.status == 'only_on_node'])} only on the node"
                  if any(c.status == "only_on_node" for c in changes) else ""))
    if exit_code and differ:
        raise typer.Exit(1)


def push(paths: PathsArg = None, workspace: WorkspaceOpt = None,
         yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask before saving.")] = False,
         wait: WaitOpt = 0.0, node: NodeOpt = None, url: UrlOpt = None) -> None:
    """Save the flow files that differ on the node, as new versions.

    The files are validated first and the diff is shown, then each is saved with flow_save under the
    connection's grant, as an agent's save: a save that waits for a person says so, and --wait waits
    for the yes. Never removes a flow."""
    from openajar.client.call import open_session
    from openajar.runner.flowsync import push as do_push

    try:
        with open_session(node, url) as session:
            changes = _changes(_paths(paths), workspace, session, with_node_only=False)
            todo = [c for c in changes if c.status in ("new", "changed")]
            if not todo:
                typer.echo(f"Nothing to push: {len(changes)} flows are the same on the node.")
                return
            _print_changes(todo)
            names = ", ".join(c.flow_id for c in todo)
            if not yes and not typer.confirm(f"Save {len(todo)} flows on the node ({names})?", default=False):
                typer.echo("Nothing was saved.")
                raise typer.Exit(1)
            results = do_push(session, todo, wait_s=wait, say=lambda line: typer.echo(line, err=True))
    except (ClientError, InvalidRequest) as e:
        raise _fail(e.message, getattr(e, "hint", None)) from None
    bad = 0
    for r in results:
        if r.error:
            bad += 1
            typer.echo(f"refused {r.flow_id}: {r.error}", err=True)
        elif r.pending:
            bad += 1
            typer.echo(f"waits {r.flow_id}: a person must approve the save ({r.pending.get('summary')}, approval "
                       f"{r.pending.get('approval_id')}). Approve it in the panel, then push again (or --wait).")
        else:
            typer.echo(f"saved {r.flow_id} {r.version}" + ("" if r.created else " (that version was there)"))
            for w in r.warnings:
                typer.echo(f"  warning: {w}")
    if bad:
        raise typer.Exit(1)


# ---- capture: a recording from a lane's pages ------------------------------------------------------

_LAYOUT = re.compile(r" \[(?:ref|cursor|box|active)(?:=[^\]]*)?\]| \(hint: [^)]*\)")


def outline_of(snapshot: str) -> dict[str, str]:
    """browser_snapshot's text -> {url, title, signal, outline}: the header lines it starts with, and
    the outline without refs, layout boxes or hints (what a recording keeps)."""
    head, _, body = snapshot.partition("\n\n")
    meta = {}
    for line in head.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key in ("url", "title", "signal"):
            meta[key] = value.strip()
    lines = [_LAYOUT.sub("", line).rstrip() for line in body.splitlines()]
    meta["outline"] = "\n".join(line for line in lines if line.strip()) + "\n"
    return meta


def capture(
    recording: Annotated[Path, typer.Argument(help="The recording file to add the page to (made if missing).")],
    on: Annotated[str, typer.Option("--on", help="The lane whose page to capture.")],
    goto: Annotated[str | None, typer.Option("--goto", help="Load this URL first (a page a navigate step loads).")]
    = None,
    after: Annotated[str | None, typer.Option("--after", help="The action that led to this page, as the flow "
                                              "does it: 'click button \"Post\"' or 'press Enter'.")] = None,
    param: Annotated[list[str] | None, typer.Option("--param", "-p", help="name=value: where the page shows "
                                                    "this value, the recording says ${name}.")] = None,
    node: NodeOpt = None, url: UrlOpt = None,
) -> None:
    """Add the page a lane shows now to a recording, for `openajar flows test`.

    Reads the page with browser_snapshot (and loads --goto first with browser_navigate) through the
    connection; it never clicks or types. Do the flow's actions yourself in between (in the lane's
    live view, or with `openajar call browser_act`) and capture each page they lead to with --after."""
    from openajar.client.call import call_tool, open_session
    from openajar.runner.flowfiles import RECORDING_SUFFIX, dump_yaml, load_yaml

    if not recording.name.endswith(RECORDING_SUFFIX):
        raise _fail(f"a recording's name ends in {RECORDING_SUFFIX}, not {recording.name}")
    if goto and after:
        raise _fail("a page is either loaded (--goto) or reached by an action (--after), not both")
    params = {}
    for pair in param or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise _fail(f"--param needs name=value, got {pair!r}")
        params[key] = value
    try:
        data = load_yaml(recording.read_text(encoding="utf-8"), str(recording)) if recording.exists() else None
        with open_session(node, url) as session:
            if goto:
                call_tool(session, "browser_navigate", {"on": on, "url": goto})
            text = call_tool(session, "browser_snapshot", {"on": on})
    except (ClientError, InvalidRequest) as e:
        raise _fail(e.message, getattr(e, "hint", None)) from None
    if not isinstance(text, str):
        raise _fail("browser_snapshot did not answer with an outline")
    meta = outline_of(text)
    outline = meta["outline"]
    for name, value in sorted(params.items(), key=lambda kv: -len(kv[1])):
        if len(value) >= 3:
            outline = outline.replace(value, "${" + name + "}")
    page: dict[str, Any] = {"url": meta.get("url", "")}
    if meta.get("title"):
        page["title"] = meta["title"]
    if after:
        page["after"] = after
    if meta.get("signal", "ok") != "ok":
        page["signal"] = meta["signal"].split()[0]
    page["outline"] = outline
    data = data if isinstance(data, dict) else {"format": 1, "note": f"captured from {on} with openajar flows capture"}
    data.setdefault("pages", []).append(page)
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_text(dump_yaml(data), encoding="utf-8")
    typer.echo(f"{recording}: page {len(data['pages'])} ({page['url']}), {len(outline.splitlines())} outline lines. "
               "Read it before you commit it: it holds what the page showed.")


def register(app: typer.Typer) -> None:
    for name, fn in (("pull", pull), ("diff", diff), ("push", push), ("validate", validate), ("test", run_tests),
                     ("capture", capture)):
        app.command(name)(fn)
