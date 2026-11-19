"""The `agentos run` command (agentos.client.run has the steps). Mounted at the top level with the
other client commands, so `agentos run -- make check` works on the computer that ran `agentos connect`."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any

import typer

from agentos.client import run as runs
from agentos.client.errors import ClientError
from agentos.client.mcp_http import McpError, McpSession
from agentos.client.ops import Client, explain, reconnect_hint
from agentos.client.state import Connection

READ_TIMEOUT_S = 60.0  # one HTTP request: a long poll is 15 s, an approval wait 120 s (that one gets more)

EPILOG = """\
Examples:

  agentos run -- make check

  agentos run --node mynode -- uv run pytest -q tests/unit -k policy

  agentos run --allow-host pypi.org --allow-host files.pythonhosted.org -- uv sync

  agentos run --detach -- make check      (then agentos run --follow JOB)

The lane's network is off unless --allow-host names hosts, when it is made: a command that installs
packages needs its index (for uv and pip: pypi.org and files.pythonhosted.org; for npm:
registry.npmjs.org). Your connection's grant needs the exec special
(`agentos connect HOST --special exec --special memory_read`).
"""


def say(line: str) -> None:
    typer.echo(line, err=True)


def fail(exc: ClientError) -> typer.Exit:
    typer.secho(f"error: {exc.message}", err=True, fg=typer.colors.RED)
    for line in exc.details:
        typer.echo(line, err=True)
    if exc.hint:
        typer.echo(f"  {exc.hint}", err=True)
    return typer.Exit(1)


class _Session:
    """An McpSession whose approval waits get a longer HTTP timeout than the other calls."""

    def __init__(self, session: McpSession, conn: Connection):
        self.session = session
        self.conn = conn

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        self.session.timeout = runs.APPROVAL_WAIT_S + 30 if tool == "approval_wait" else READ_TIMEOUT_S
        try:
            return self.session.call(tool, arguments)
        except McpError as exc:
            raise ClientError(f"{self.conn.name}: {explain(exc, self.conn)}", reconnect_hint(exc, self.conn)) from None


def open_session(c: Client, conn: Connection) -> McpSession:
    token = c.tokens(conn.token_store).get(conn.name)
    if not token:
        raise ClientError(f"no token for {conn.name} in the {conn.token_store} store",
                          f"run `agentos connect {conn.ssh_host} --name {conn.name}` again")
    session = McpSession(conn.local_port, token, timeout=READ_TIMEOUT_S, host=conn.host)
    try:
        session.initialize()
    except McpError as exc:
        raise ClientError(f"{conn.name}: {explain(exc, conn)}", reconnect_hint(exc, conn)) from None
    return session


def check_exec(conn: Connection) -> None:
    """A grant without the exec special cannot run commands (the node hides the tools): say so first."""
    if conn.credential == "agent_grant" and "exec" not in (conn.agent_specials or []):
        specials = " ".join(f"--special {s}" for s in sorted({*conn.agent_specials, "exec"}))
        raise ClientError(f"the agents' grant of {conn.name} cannot run commands (it has no exec special)",
                          f"give it one: agentos connect {conn.ssh_host} --name {conn.name} {specials}")


def run_command(
    ctx: typer.Context,
    command: Annotated[list[str] | None, typer.Argument(
        help="The command, after --. One quoted word runs as a shell line (\"make check && make lint\").",
        show_default=False)] = None,
    node: Annotated[str | None, typer.Option("--node", help="Connection name (default: the only one).")] = None,
    lane: Annotated[str | None, typer.Option(
        "--lane", help="The terminal lane, workspace/name (default: WORKSPACE/run-REPO).", show_default=False)]
    = None,
    workspace: Annotated[str, typer.Option("--workspace", help="Workspace of the default lane.")]
    = runs.DEFAULT_WORKSPACE,
    fresh: Annotated[bool, typer.Option(
        "--fresh", help="Wipe the synced directory first, with what earlier runs left there (.venv, "
                        "node_modules, build output).")] = False,
    detach: Annotated[bool, typer.Option("--detach", help="Start it and return; follow it later.")] = False,
    follow: Annotated[str | None, typer.Option(
        "--follow", metavar="JOB", help="Stream a job's output from the start until it ends. Ctrl-C stops "
                                        "following and leaves it running.", show_default=False)] = None,
    logs: Annotated[str | None, typer.Option("--logs", metavar="JOB", help="Print a job's output so far.",
                                             show_default=False)] = None,
    cancel: Annotated[str | None, typer.Option("--cancel", metavar="JOB", help="Stop a running job.",
                                               show_default=False)] = None,
    list_jobs: Annotated[bool, typer.Option("--list", help="The jobs on the node, newest first.")] = False,
    no_sync: Annotated[bool, typer.Option("--no-sync", help="Run in the lane's copy as it is, without sending "
                                                            "this tree.")] = False,
    allow_host: Annotated[list[str] | None, typer.Option(
        "--allow-host", help="A host the command may reach (repeatable), through the lane's egress forwarder. "
                             "Applies when the lane is made.", show_default=False)] = None,
    image: Annotated[str | None, typer.Option(
        "--image", help="A base image the node built, for a new lane (for example dev-box@1).", show_default=False)]
    = None,
    memory: Annotated[int, typer.Option("--memory", min=64, help="Memory of a new lane, in MB.")]
    = runs.DEFAULT_MEMORY_MB,
    cpus: Annotated[float, typer.Option("--cpus", min=0.1, help="CPUs of a new lane.")] = runs.DEFAULT_CPUS,
    disk: Annotated[int, typer.Option("--disk", min=64, help="Disk of a new lane, in MB (its files, .venv "
                                                            "included).")] = runs.DEFAULT_DISK_MB,
    recreate: Annotated[bool, typer.Option(
        "--recreate", help="Delete the lane and make it again (new --allow-host, --image or size). Asks for "
                           "approval: deleting cannot be undone.")] = False,
    timeout: Annotated[float | None, typer.Option(
        "--timeout", min=1, help="Stop the command after this many seconds (default: the node's maximum, "
                                 "an hour unless env.toml says otherwise).", show_default=False)] = None,
) -> None:
    """Run one command on a node against this working tree, and stream its output here. Sends the git
    working tree (tracked files, and untracked ones .gitignore keeps) into a terminal lane on the node,
    only what changed since the last run, then runs the command there in the same directory as here.
    stdout and stderr come back as they are written, and agentos run exits with the command's exit code.
    Ctrl-C cancels the command on the node. Status lines go to stderr.

    The lane keeps what the command made (.venv, node_modules, caches) for the next run. --fresh wipes it.

    It runs under the connection's agents' grant: a step that needs approval says what waits, and waits.
    """
    words = list(command or [])
    modes = [m for m, on in (("--follow", follow), ("--logs", logs), ("--cancel", cancel), ("--list", list_jobs))
             if on]
    if len(modes) > 1:
        raise fail(ClientError(f"choose one of {', '.join(modes)}"))
    if modes and words:
        raise fail(ClientError(f"{modes[0]} takes no command"))
    if not modes and not words:
        typer.echo(ctx.get_help())
        raise typer.Exit(2)
    if lane is None and not runs.WORKSPACE_RE.match(workspace):
        raise fail(ClientError(f"--workspace {workspace!r} is not a workspace name (lowercase, digits, dashes)"))
    c = Client(out=say)
    try:
        conn = c.pick(node)
        check_exec(conn)
        session = open_session(c, conn)
    except ClientError as exc:
        raise fail(exc) from None
    run = runs.Run(_Session(session, conn), say=say, needs_you=c.needs_you_url(conn))
    try:
        if list_jobs:
            raise typer.Exit(show_jobs(run, lane))
        if logs:
            raise typer.Exit(show_logs(run, logs))
        if cancel:
            job = run.cancel(cancel)
            say(f"job {cancel}: {runs.ended_words(job)}")
            return
        if follow:
            raise typer.Exit(follow_job(run, follow, cancel_on_interrupt=False))
        raise typer.Exit(start_run(run, conn, words, lane=lane, workspace=workspace, fresh=fresh, detach=detach,
                                   no_sync=no_sync, allow_hosts=list(allow_host or []), image=image, memory=memory,
                                   cpus=cpus, disk=disk, recreate=recreate, timeout=timeout))
    except runs.Interrupted:
        say("stopped waiting for the approval; nothing ran")
        raise typer.Exit(130) from None
    except KeyboardInterrupt:
        say("\nstopped before the command's output came back; `agentos run --list` shows whether it started")
        raise typer.Exit(130) from None
    except ClientError as exc:
        raise fail(exc) from None
    finally:
        session.close()


def start_run(run: runs.Run, conn: Connection, words: list[str], *, lane: str | None, workspace: str, fresh: bool,
              detach: bool, no_sync: bool, allow_hosts: list[str], image: str | None, memory: int, cpus: float,
              disk: int, recreate: bool, timeout: float | None) -> int:
    t0 = time.monotonic()
    cwd = Path.cwd()
    try:
        root: Path | None = runs.repo_root(cwd)
    except ClientError:
        if not (no_sync and lane):
            raise
        root = None  # --no-sync in a named lane: no tree to send, so none is needed
    if lane is None:
        if root is None:
            raise ClientError("outside a git working tree, name the lane with --lane")
        lane = runs.default_lane(root, workspace)
    cmd = runs.command_line(words)
    say(f"agentos run on {conn.name}, lane {lane}")
    run.ensure_lane(lane, allow_hosts=allow_hosts, image=image, memory_mb=memory, cpus=cpus, disk_mb=disk,
                    recreate=recreate)
    dest = runs.dest_for(root) if root is not None else None
    rel = runs.relative_cwd(root, cwd) if root is not None else None
    workdir = f"{dest}/{rel}" if dest and rel else dest
    if no_sync:
        say("not syncing (--no-sync): the lane's copy runs as it is")
        sync_dest = None
    else:
        assert root is not None and dest is not None
        cache = runs.HashCache(runs.cache_path(root))
        tree = runs.build_tree(root, runs.list_files(root), cache)
        stats = run.upload(lane, tree)
        cache.save()
        plural = "" if stats.sent_chunks == 1 else "s"
        say(f"tree: {stats.files:,} files ({runs.human_bytes(stats.bytes)}); sent {stats.sent_chunks} new "
            f"chunk{plural} ({runs.human_bytes(stats.sent_bytes)})")
        if stats.skipped:
            shown = ", ".join(stats.skipped[:5]) + (f" and {len(stats.skipped) - 5} more" if len(stats.skipped) > 5
                                                    else "")
            say(f"note: skipped what is not a file or symlink (a submodule?): {shown}")
        sync_dest = dest
    source = runs.git_source(root) if sync_dest is not None and root is not None else None
    job = run.start(lane, cmd, cwd=workdir, dest=sync_dest, clean=fresh, timeout_s=timeout, source=source)
    synced = job.get("sync") or {}
    if synced:
        say(f"synced into {synced.get('dest')}: {synced.get('changed', 0)} changed, {synced.get('deleted', 0)} "
            f"deleted" + (", wiped first (--fresh)" if synced.get("clean") else ""))
    jid = job["id"]
    if timeout and isinstance(job.get("timeout_s"), int | float) and job["timeout_s"] < timeout:
        say(f"note: the node stops it after {int(job['timeout_s'])}s, its maximum (max_timeout_s in env.toml)")
    if detach:
        say(f"started job {jid} in {lane}: {cmd}")
        say(f"  follow it:  agentos run --follow {jid}" + node_flag(conn))
        say(f"  its output: agentos run --logs {jid}" + node_flag(conn))
        say(f"  stop it:    agentos run --cancel {jid}" + node_flag(conn))
        return 0
    say(f"$ {cmd}" + (f"   (in {workdir})" if workdir else ""))
    return follow_job(run, jid, cancel_on_interrupt=True, t0=t0)


def node_flag(conn: Connection) -> str:
    try:
        several = len(Client(out=say).connections()) > 1
    except ClientError:
        several = True
    return f" --node {conn.name}" if several else ""


def follow_job(run: runs.Run, job: str, *, cancel_on_interrupt: bool, t0: float | None = None) -> int:
    """Stream until the job ends. Ctrl-C cancels it (an attached run) or stops following (--follow)."""
    try:
        last = run.follow(job)
    except KeyboardInterrupt:
        if not cancel_on_interrupt:
            say(f"\nstopped following; job {job} keeps running. Stop it with: agentos run --cancel {job}")
            return 130
        say(f"\ncancelling job {job} (Ctrl-C again leaves it to the node)")
        try:
            run.cancel(job)
            last = run.follow(job, wait_ms=2000)  # what it wrote before it stopped, and how it ended
        except KeyboardInterrupt:
            return 130
    footer(job, last, t0)
    return runs.exit_code_of(last)


def footer(job: str, info: dict, t0: float | None) -> None:
    took = info.get("duration_s")
    took_words = runs.human_duration(took) if isinstance(took, int | float) else (
        runs.human_duration(runs.elapsed_since(t0)) if t0 is not None else "?")
    extra = []
    if info.get("stdout_dropped") or info.get("stderr_dropped"):
        extra.append("the node kept only the first part of the output")
    if info.get("note") and info.get("state") not in ("cancelled", "timed_out"):
        extra.append(info["note"])
    tail = f"; {'; '.join(extra)}" if extra else ""
    say(f"agentos run: {runs.ended_words(info)} in {took_words} (job {job}){tail}")


def show_logs(run: runs.Run, job: str) -> int:
    last = run.follow(job, until_done=False)
    if last.get("state") == "running":
        say(f"job {job} is still running; follow it with: agentos run --follow {job}")
        return 0
    footer(job, last, None)
    return runs.exit_code_of(last)


def show_jobs(run: runs.Run, lane: str | None) -> int:
    jobs = run.raw("terminal_jobs", {"on": lane} if lane else {})
    if not jobs:
        typer.echo("No jobs." + ("" if lane else " Start one with: agentos run -- <command>"))
        return 0
    rows = [("JOB", "TERMINAL", "STATE", "EXIT", "STARTED", "TOOK", "COMMAND")]
    for j in jobs:
        took = j.get("duration_s")
        code = j.get("exit_code")
        rows.append((j["id"], j.get("terminal") or j["lane"], j["state"], "-" if code is None else str(code),
                     (j.get("started_at") or "")[:19].replace("T", " "),
                     runs.human_duration(took) if isinstance(took, int | float) else "-",
                     (j.get("cmd") or "")[:60]))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(r, widths, strict=True)).rstrip())
    return 0
