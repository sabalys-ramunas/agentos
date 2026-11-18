"""MCP tools for terminal and service lanes. Every acting tool authorizes first, with a
summary the node writes from what it will actually run (the exact command or packages)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from typing import Any

from agentos.core import progress
from agentos.core.errors import InvalidRequest
from agentos.core.models import Effect
from agentos.core.tools import ToolContext, ToolSpec
from agentos.env import execs, exposure, oom, programs
from agentos.env.models import EnvLane
from agentos.env.sync import check_sha as sync_check_sha

SUMMARY_MAX = 2000


def _mgr(ctx: ToolContext):
    return ctx.runtime.require("envs")


def _where(lane: EnvLane) -> str:
    if lane.remote is not None:
        return f"on VM lane {lane.id} (a disposable cloud machine at {lane.remote.host}, nothing of the node on it)"
    if not lane.isolated:
        return f"on the node itself as {exposure.node_user()} (workbench lane {lane.id}, no isolation)"
    if exposure.reaches_node(lane):
        return (f"in {lane.preset.value} lane {lane.id}, which shares the node's "
                f"{' and '.join(exposure.shared_dials(lane))} (it can read the vault and the node token)")
    net = lane.profile.network
    if net == "isolated":
        allow = ", ".join(lane.profile.egress_allowlist) or (
            "no hosts" if lane.policy.egress_default == "deny" else "any public host")
        net = f"egress via forwarder to {allow}"
    else:
        net = f"network {net}"
    return f"in {lane.preset.value} lane {lane.id} ({net}, filesystem {lane.profile.filesystem})"


def _facts(lane: EnvLane) -> dict:
    """What the node itself knows about the lane, for the routing table (policy/ROUTING.md): exec in an
    isolated lane asks a person on first use only, in a workbench or shared lane every time. Read from
    the node's own lane record, never from the caller. A VM lane counts as isolated: nothing of the
    node runs on it (EnvLane.contained).

    A lane that reaches the node (a workbench lane, or a sandbox sharing the node's filesystem or
    processes) also gets `runs_on_node`, the reason in words: the policy then always asks a person,
    never a standing approval, whatever the routing table says, because a command there can read the
    vault and the node token (agentos.env.exposure). Every acting tool here sends it."""
    return exposure.facts(lane)


def _grant_id(ctx: ToolContext) -> str | None:
    """The grant a command runs under: its git reaches the remotes declared for the lane, as that
    grant (agentos.git), and never anything else."""
    return getattr(getattr(ctx, "grant", None), "id", None)


def _clip(text: str) -> str:
    if len(text) <= SUMMARY_MAX:
        return text
    return text[:SUMMARY_MAX] + f" (+{len(text) - SUMMARY_MAX} more characters, hashed in the approval)"


def _sha(data: str | bytes | None) -> str | None:
    if data is None:
        return None
    b = data.encode() if isinstance(data, str) else data
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _lane_brief(info) -> dict:
    lane = info.lane
    out = {"id": lane.id, "kind": lane.kind.value, "preset": lane.preset.value, "state": info.state.value,
           "isolation": info.isolation, "network": lane.profile.network,
           "egress_allowlist": lane.profile.egress_allowlist, "filesystem": lane.profile.filesystem,
           "base_image": lane.profile.base_image, "shell": lane.policy.shell, "exec": lane.policy.exec,
           "install": lane.policy.install, "tier": lane.policy.tier, "disk": info.disk,
           "shells": info.shells, "last_used": info.last_used.isoformat() if info.last_used else None}
    if info.quarantine_reason:
        out["quarantine_reason"] = info.quarantine_reason
    if info.service:
        out["service"] = info.service.model_dump(mode="json")
    if lane.remote is not None:
        out["vm"] = {"id": lane.remote.vm_id, "host": lane.remote.host, "provider": lane.remote.provider,
                     "label": lane.remote.label}
    return out


# ------------------------------------------------------------------ lanes
async def env_lanes_list(ctx: ToolContext) -> list[dict]:
    """List terminal and service lanes on this node: preset, state, and an honest one-line
    description of the isolation each really gets."""
    await ctx.authorize(Effect.READ)
    allowed = getattr(ctx, "can_access_lane", None)  # a grant sees only the lanes it covers
    return [_lane_brief(i) for i in _mgr(ctx).list() if allowed is None or allowed(i.lane.id)]


async def env_create(ctx: ToolContext, *, on: str, preset: str = "sandbox", network: str | None = None,
                     allow_hosts: list[str] | None = None, base_image: str | None = None,
                     filesystem: str | None = None, memory_mb: int | None = None, cpus: float | None = None,
                     disk_mb: int | None = None) -> dict:
    """Create a terminal lane `workspace/name`. Agents may create sandbox (throwaway: private
    ephemeral filesystem, network off unless you pass network="isolated" with allow_hosts) or
    locked lanes. base_image pins a built image such as "dev-box@1". filesystem="overlay" keeps
    the lane's files when it goes cold (a cached .venv or node_modules); memory_mb, cpus and
    disk_mb size it, up to the node's caps for agents."""
    mgr = _mgr(ctx)
    overrides: dict[str, Any] = {}
    if network:
        overrides["network"] = network
    if allow_hosts:
        overrides["egress_allowlist"] = allow_hosts
    if base_image:
        overrides["base_image"] = base_image
    if filesystem:
        overrides["filesystem"] = filesystem
    sizes = {k: v for k, v in (("memory_mb", memory_mb), ("cpus", cpus), ("disk_mb", disk_mb)) if v is not None}
    overrides.update(sizes)
    summary = (f"Create {preset} terminal lane {on}"
               + (f", network {network}" if network else "")
               + (f", egress allowlist {', '.join(allow_hosts)}" if allow_hosts else "")
               + (f", base image {base_image}" if base_image else "")
               + (f", filesystem {filesystem}" if filesystem else "")
               + "".join(f", {k} {v:g}" for k, v in sizes.items()))
    # what create would refuse (a lane that exists, a sandbox this node cannot run) is refused before
    # anyone is asked, so no approval is spent on it (QA-2 #13). Only on a lane the grant covers: a lane
    # outside it is refused by authorize, never named as existing
    allowed = getattr(ctx, "can_access_lane", None)
    facts = None
    if allowed is None or allowed(on):
        facts = _facts(mgr.check_create(on, kind="terminal", preset=preset, overrides=overrides, by_agent=True))
    await ctx.authorize(Effect.EXEC, lane_id=on, summary=summary,
                        content={"on": on, "preset": preset, "overrides": overrides}, facts=facts)
    info = mgr.create(on, kind="terminal", preset=preset, overrides=overrides, by_agent=True)
    ctx.trace("env_create", lane=on, preset=preset)
    return _lane_brief(info)


async def env_remove(ctx: ToolContext, *, on: str) -> dict:
    """Delete a terminal or service lane and its files (its writable layer, logs and home)."""
    mgr = _mgr(ctx)
    lane = mgr.get(on)
    await ctx.authorize(Effect.IRREVERSIBLE, lane_id=on,
                        summary=f"Delete {lane.preset.value} lane {on} and everything written inside it",
                        content={"remove": on})
    await mgr.remove(on)
    return {"removed": on}


# ------------------------------------------------------------------ exec
# what lane_exec returns of stdout and stderr together by default, in characters of the JSON text the agent
# reads (a newline is two): its start and its end, where a build's or a test run's error and summary are. 16,000
# bytes came back as about 19,000 characters, and an agent that needs the middle asks for it (EXEC_CUT_WORDS)
EXEC_BYTES = 6000
EXEC_CUT_RE = re.compile(r"\[agentos: (\d+) bytes truncated here, of \d+ in all")
# a lane_exec result's fields that are left out while they hold their default (an absent key is the default)
EXEC_DEFAULTS = {"signal": None, "stdout_encoding": "utf-8", "stderr_encoding": "utf-8", "truncated": False,
                 "timed_out": False, "oom_killed": False}


async def lane_exec(ctx: ToolContext, *, on: str, cmd: str, timeout_s: float = 60,
                    stdin: str | None = None, cwd: str | None = None, max_bytes: int = EXEC_BYTES) -> dict:
    """Run one shell command inside a lane and wait for it. Returns exit_code, stdout, stderr, their
    full sizes and duration_s; other fields only when they apply. Past max_bytes (6000 characters)
    the middle is cut, and `note` says how to read it. Processes it starts end with it: use
    lane_job_start for a dev server. Effect: exec.

    Args:
        cwd: The folder to run in (on an environment's machine an absolute path like /work/shop;
            elsewhere one under the lane's home).
        max_bytes: stdout and stderr together (default 6000, at most 131072).
        timeout_s: At most 200.
    """
    mgr = _mgr(ctx)
    lane = mgr.get(on)
    mgr.check_runnable(lane)  # a sandbox this node can never run is refused before anyone is asked
    if cwd is not None:
        from agentos.env.sync import check_cwd

        cwd = check_cwd(cwd, remote=lane.remote is not None)
    budget = _exec_budget(mgr, max_bytes)
    run = f"cd '{cwd}' && {cmd}" if cwd else cmd  # plain names (check_cwd): safe as one shell word
    await ctx.authorize(Effect.EXEC, lane_id=on, summary=_clip(f"Run {_where(lane)}: {cmd}"
                                                               + (f" (in {cwd})" if cwd else "")),
                        content={"cmd": cmd, "stdin": _sha(stdin), "timeout_s": timeout_s,
                                 **({"cwd": cwd} if cwd else {})}, facts=_facts(lane))
    wanted = _seconds(timeout_s)
    capped = min(wanted, progress.LONG_CALL_S)  # one call never runs past what an MCP client waits for
    progress.step(f"running: {_clip(cmd)[:80]}")
    res = await mgr.exec(on, run, timeout_s=capped, stdin=stdin, grant_id=_grant_id(ctx), max_bytes=budget,
                         oom_check=True)
    ctx.trace("lane_exec", lane=on, exit_code=res.exit_code, duration_s=res.duration_s, timed_out=res.timed_out)
    out = {k: v for k, v in res.model_dump().items()
           if not (k == "oom" and v is None) and not (k in EXEC_DEFAULTS and v == EXEC_DEFAULTS[k])}
    notes = [cut_note(res, _exec_most(mgr)),
             exec_note(cmd, res.timed_out, wanted, capped),
             None if res.timed_out else self_kill_note(cmd, res.exit_code, res.signal),
             oom.note(res.oom, running=False, what="command")]
    note = " ".join(n for n in notes if n)
    if note:
        out["note"] = note
    return out


# `pkill -f vite` matches the command line of the shell that runs it, which holds the pattern too
SELF_MATCH_RE = re.compile(r"\b(pkill|pgrep|killall)\b[^;&|\n]*?\s(-[A-Za-z]*f[A-Za-z]*|--full)\b")
KILLED_CODES = frozenset({137, 143, 255})  # SIGKILL, SIGTERM, and ssh's own code when the remote shell died


def self_kill_note(cmd: str, exit_code: int | None, signal: str | None, *, what: str = "command") -> str | None:
    """A command that killed its own shell: an exit that means killed (255 over ssh, 143, 137, a signal)
    and pkill, pgrep or killall with -f (--full) in it, whatever it printed first (round 3: `sleep 300 &
    pkill -f "sleep 300"` printed "started", then exit 255 and nothing else)."""
    if exit_code not in KILLED_CODES and not signal:
        return None
    if not SELF_MATCH_RE.search(cmd or ""):
        return None
    how = signal or f"exit {exit_code}"
    return (f"The shell running this {what} was killed ({how}): pkill -f matched the shell's own command line, "
            "which holds the pattern too, so it killed itself before the rest ran. Use pkill -f '[v]ite' (the "
            "brackets keep the pattern from matching itself), kill a pid, or lane_job_cancel for a job.")


GIT_PUSH_RE = re.compile(r"\bgit\b[^;&|]*\bpush\b")


def _seconds(value: Any) -> float:
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        raise InvalidRequest("timeout_s is a number of seconds, like 60") from None


def exec_note(cmd: str, timed_out: bool, wanted: float, capped: float) -> str | None:
    """What a lane_exec result adds: a push that timed out may have landed; a timeout that was cut."""
    parts = []
    if timed_out and GIT_PUSH_RE.search(cmd or ""):
        parts.append("The command timed out while git push ran, so the push may have landed anyway. Do not push "
                     "again blind: check with lane_exec \"git fetch origin && git status -sb\" (or git ls-remote "
                     "origin <branch>), or call environment(): git.pushed lists the branches the node saw pushed.")
    if timed_out and wanted > capped:
        parts.append(f"timeout_s was cut to {capped:g} s: one call may not run longer. For a longer command use "
                     "lane_job_start, then lane_job_read(until_exit=true).")
    return " ".join(parts) or None


def cut_note(res: Any, most: int) -> str | None:
    """How to read what lane_exec left out of the middle of its output (`most`: the largest max_bytes)."""
    cut = sum(int(m.group(1)) for text in (res.stdout, res.stderr) for m in EXEC_CUT_RE.finditer(text or ""))
    if not cut:
        return None
    return (f"{cut} bytes of the output's middle were left out (the marker shows where). To read them, run it "
            f"again with a bigger max_bytes (at most {most}), or send the output to a file (> /tmp/out.log 2>&1) "
            "and read it with lane_file_read, whose offset is a line number.")


def _exec_budget(mgr: Any, max_bytes: Any) -> int:
    try:
        wanted = int(max_bytes)
    except (TypeError, ValueError):
        raise InvalidRequest("max_bytes is a number of characters, like 6000") from None
    return max(1000, min(wanted, _exec_most(mgr)))


def _exec_most(mgr: Any) -> int:
    """The largest max_bytes lane_exec takes: twice the node's max_output_bytes (131072 by default)."""
    return 2 * int(getattr(getattr(mgr, "settings", None), "max_output_bytes", 65536))


async def lane_install(ctx: ToolContext, *, on: str, packages: list[str], source: str = "apt") -> dict:
    """Install programs into a lane from a pinned source: apt (the node's signed apt sources,
    as the lane's root inside its overlay), pip (pypi.org) or npm (registry.npmjs.org). The
    install is visible only in this lane, unless the lane shares the node filesystem."""
    mgr = _mgr(ctx)
    lane = mgr.get(on)
    mgr.check_runnable(lane)  # a sandbox this node can never run is refused before anyone is asked
    pkgs = programs.validate(source, packages)
    hosts = programs.source_hosts(source, mgr.settings)
    shared = lane.profile.filesystem == "shared"
    effect = Effect.INSTALL_BASE if shared else Effect.INSTALL_OVERLAY
    await ctx.authorize(effect, lane_id=on, summary=programs.summary(source, pkgs, hosts, on, shared=shared),
                        content={"source": source, "packages": pkgs}, facts=_facts(lane))
    out = await mgr.install(on, pkgs, source)
    ctx.trace("lane_install", lane=on, source=source, packages=pkgs, ok=out["ok"])
    return out


# ------------------------------------------------------------------ jobs and synced trees
def _job_view(info) -> dict:
    return info.model_dump(mode="json", exclude={"vm_wid"})


def _read_view(info) -> dict:
    """A job as lane_job_read gives it: how it runs or ended, without what lane_job_start returned and
    never changes (lane, cmd, cwd, started_at, timeout_s, by, sync, source; lane_jobs_list has them),
    and without fields that hold their default (no signal, no OOM kill, nothing dropped, no note)."""
    out = {"state": info.state, "exit_code": info.exit_code}
    extra = {"signal": info.signal, "oom_killed": info.oom_killed or None, "oom": info.oom,
             "duration_s": info.duration_s, "stdout_bytes": info.stdout_bytes, "stderr_bytes": info.stderr_bytes,
             "stdout_dropped": info.stdout_dropped or None, "stderr_dropped": info.stderr_dropped or None,
             "note": info.note}
    return out | {k: v for k, v in extra.items() if v is not None}


BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,199}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")


def _source(source: Any) -> dict | None:
    """The git branch and commit of the synced tree, as the client says (agentos run reads them with git).
    Kept on the job for the person who reads it later; the node never acts on them."""
    if source is None:
        return None
    if not isinstance(source, dict) or set(source) - {"branch", "commit", "dirty"}:
        raise InvalidRequest('source is {"branch": "main", "commit": "<sha>", "dirty": false}')
    branch, commit, dirty = source.get("branch"), source.get("commit"), source.get("dirty")
    if branch is not None and (not isinstance(branch, str) or not BRANCH_RE.match(branch)):
        raise InvalidRequest("source.branch is a git branch name")
    if commit is not None and (not isinstance(commit, str) or not COMMIT_RE.match(commit)):
        raise InvalidRequest("source.commit is a git commit id in hex")
    if dirty is not None and not isinstance(dirty, bool):
        raise InvalidRequest("source.dirty is true or false")
    return {"branch": branch, "commit": commit, "dirty": dirty}


def _decode_chunk(sha: str, data: Any) -> bytes:
    if not isinstance(data, str):
        raise InvalidRequest(f"chunk {str(sha)[:12]}: the data is base64 text")
    try:
        return base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise InvalidRequest(f"chunk {str(sha)[:12]}: the data is not valid base64") from None


async def lane_upload_check(ctx: ToolContext, *, on: str, chunks: list[str]) -> dict:
    """Which of these chunks (sha256 hex of each piece of a file) the node does not hold yet for
    this lane. Upload those with lane_upload; the rest are there from an earlier sync."""
    await ctx.authorize(Effect.READ, lane_id=on)
    missing = await _mgr(ctx).upload_check(on, chunks)
    return {"lane": on, "missing": missing, "have": len(set(chunks)) - len(missing)}


async def lane_upload(ctx: ToolContext, *, on: str, chunks: dict[str, str]) -> dict:
    """Store file chunks on the node for a later sync into this lane (lane_job_start's `sync`):
    {sha256 hex: base64 data}, at most a few MB per call. Each chunk is checked against its
    name. Nothing reaches the lane, and nothing runs, until a sync applies them."""
    mgr = _mgr(ctx)
    lane = mgr.get(on)
    if not isinstance(chunks, dict) or not chunks:
        raise InvalidRequest("chunks is a non-empty map of sha256 to base64 data")
    data = {sync_check_sha(sha): _decode_chunk(sha, b64) for sha, b64 in chunks.items()}
    size = sum(len(b) for b in data.values())
    await ctx.authorize(Effect.DRAFT, lane_id=on,
                        summary=f"Store {len(data)} file chunk(s) ({size} bytes) on the node for a sync into lane {on}",
                        content={"chunks": sorted(data)}, facts=_facts(lane))
    return {"lane": on, **(await mgr.upload(on, data))}


async def lane_job_start(ctx: ToolContext, *, on: str, cmd: str, cwd: str | None = None,
                         env: dict[str, str] | None = None, timeout_s: float | None = None,
                         sync: dict[str, Any] | None = None, source: dict[str, Any] | None = None) -> dict:
    """Start a shell command in a lane in the background and return its job id at once; read its
    output with lane_job_read as it runs, stop it with lane_job_cancel. For long work (a test
    suite, a build): the output is kept on the node, so you can disconnect and come back. cwd is a
    directory under the lane's home; env adds variables; timeout_s defaults to the node's maximum.
    sync={"dest", "manifest", "clean"} first makes `dest` hold exactly the files of a manifest
    whose chunks you uploaded (lane_upload, terminals toolset), keeping what earlier commands made there (a .venv)
    unless clean is true, and is approved together with the command. source={"branch", "commit",
    "dirty"} says which git tree the sync came from (kept on the job, with a sync only)."""
    mgr = _mgr(ctx)
    told = _source(source) if sync is not None else None
    lane = mgr.get(on)
    mgr.check_runnable(lane)  # a sandbox this node can never run is refused before anyone is asked
    timeout = mgr.check_job(on, cmd, cwd=cwd, env=env, timeout_s=timeout_s)
    prepared = None
    if sync is not None:
        if not isinstance(sync, dict) or set(sync) - {"dest", "manifest", "clean"}:
            raise InvalidRequest('sync is {"dest": "src/repo", "manifest": [chunk sha256, ...], "clean": false}')
        prepared = await mgr.sync_prepare(on, dest=sync.get("dest"), manifest=sync.get("manifest"),
                                          clean=bool(sync.get("clean", False)))
    where = _where(lane) + (f", in {cwd}" if cwd else "")
    lead = ""
    if prepared:
        lead = (f"Sync {prepared['files']} files ({prepared['changed']} changed, {prepared['deleted']} deleted"
                + (", the destination wiped first" if prepared["clean"] else "") + f") into {prepared['dest']} and ")
    envs = f" with {', '.join(sorted(env))} set" if env else ""
    summary = _clip(f"{lead}{'r' if lead else 'R'}un in the background {where}{envs} for up to {int(timeout)}s: {cmd}")
    await ctx.authorize(Effect.EXEC, lane_id=on, summary=summary,
                        content={"cmd": cmd, "cwd": cwd, "env": env, "timeout_s": timeout,
                                 "sync": {k: prepared[k] for k in ("dest", "manifest", "clean")} if prepared else None},
                        facts=_facts(lane))
    info = await mgr.job_start(on, cmd, cwd=cwd, env=env, timeout_s=timeout, sync=prepared, grant_id=_grant_id(ctx),
                               by=getattr(getattr(ctx, "principal", None), "id", None), source=told)
    ctx.trace("lane_job_start", lane=on, job=info.id, synced=(info.sync or {}).get("files"))
    return _job_view(info)


JOB_READ_BYTES = 6000  # stdout and stderr together, in characters of the result as for lane_exec (EXEC_BYTES)
JOB_READ_MOST = 1 << 20
JOB_WAIT_MS = 30000
JOB_WAIT_EXIT_MS = 120000


def _job_on(on: str | None, j: Any) -> None:
    """`on` beside a job id (agents are told to pass it on every call): routing made an environment's
    handle its terminal lane; it must be the lane the job runs in."""
    if on is not None and on != j.info.lane:
        raise InvalidRequest(f"job {j.info.id} is not on the machine that on names. Pass the on of the "
                             "environment that started it, or leave on out: the job id is enough")


async def lane_job_read(ctx: ToolContext, *, job: str, on: str | None = None, stdout_offset: int = 0,
                        stderr_offset: int = 0, wait_ms: int = 1000, max_bytes: int = JOB_READ_BYTES,
                        base64_output: bool = False, until_exit: bool = False, until_port: int = 0) -> dict:
    """Read a job's output after your byte offsets, max_bytes (6000) characters, waiting up to wait_ms
    (30000) for more while it runs. until_exit=true waits for the job to end (then its start and its
    end); until_port=3000 for that port to listen or the job to end (then its end); both wait_ms up
    to 120000. Pass back next_stdout_offset and next_stderr_offset; stdout_bytes is the size so far.
    `state`: running, exited, cancelled, timed_out or lost; then exit_code (or signal). Effect: read.

    Args:
        max_bytes: stdout and stderr together (default 6000, at most 1048576).
    """
    mgr = _mgr(ctx)
    j = mgr.job(job)
    await ctx.authorize(Effect.READ, lane_id=j.info.lane)
    _job_on(on, j)
    port = _port(until_port)
    most = JOB_WAIT_EXIT_MS if until_exit or port else JOB_WAIT_MS
    wait_s = max(0, min(_int(wait_ms, "wait_ms"), most)) / 1000
    max_bytes = max(1, min(_int(max_bytes, "max_bytes"), JOB_READ_MOST))
    stdout_offset, stderr_offset = max(0, _int(stdout_offset, "stdout_offset")), max(0, _int(stderr_offset, "stderr_offset"))
    listening = None
    if port:
        progress.step(f"waiting for port {port} to listen")
        listening = await mgr.job_wait_port(job, port, wait_s) if wait_s > 0 else False
        wait_s = 0
        if not listening and not j.running:  # it ended first: the end of what it printed, with its exit code
            stdout_offset = max(stdout_offset, j.kept("stdout") - max_bytes)
            stderr_offset = max(stderr_offset, j.kept("stderr") - max_bytes)
    info, out, err = await mgr.job_read(job, stdout_offset=stdout_offset, stderr_offset=stderr_offset,
                                        wait_s=wait_s, max_bytes=max_bytes, until_exit=bool(until_exit))
    ends, cut = {}, []
    if not base64_output:  # max_bytes counts the characters the agent reads (a newline is two), as lane_exec's
        from_end = bool(port and not listening and info.state != "running")
        room = dict(zip(("stdout", "stderr"), execs.rooms(execs.escaped_len(out), execs.escaped_len(err), max_bytes),
                        strict=True))
        if until_exit and not port and info.state != "running":
            ends = {s: e for s, at, data in (("stdout", stdout_offset, out), ("stderr", stderr_offset, err))
                    if (e := await _start_and_end(mgr, j, s, at, data, room[s])) is not None}
        out, stdout_offset = _within(out, stdout_offset, room["stdout"], from_end)
        err, stderr_offset = _within(err, stderr_offset, room["stderr"], from_end)
        if info.state != "running" and not from_end:  # what one read left of an ended job, and how to get it
            cut = [f"{s} has {left} bytes more after next_{s}_offset"
                   for s, at, data in (("stdout", stdout_offset, out), ("stderr", stderr_offset, err))
                   if s not in ends and (left := j.kept(s) - at - len(data)) > 0]
    if base64_output:
        so, se, enc = base64.b64encode(out).decode(), base64.b64encode(err).decode(), "base64"
    else:
        out, _ = execs._trim_utf8_end(out) if info.state == "running" else (out, b"")
        err, _ = execs._trim_utf8_end(err) if info.state == "running" else (err, b"")
        so, se, enc = out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), "utf-8"
    res = {**_read_view(info), "stdout": so, "stderr": se,
           "next_stdout_offset": stdout_offset + len(out), "next_stderr_offset": stderr_offset + len(err)}
    if enc != "utf-8":
        res["encoding"] = enc
    if listening is not None:
        res["listening"] = listening
    words = [info.note, oom.note(info.oom, running=info.state == "running"),
             self_kill_note(info.cmd, info.exit_code, info.signal, what="job") if info.state == "exited" else None]
    for stream, (text, end, said) in ends.items():
        res[stream], res[f"next_{stream}_offset"] = text, end
        words.append(said)
    if port and not listening:
        words.append(f"Port {port} does not listen yet; call again to wait longer." if info.state == "running" else
                     f"The job ended before port {port} listened: stdout and stderr are the end of its output.")
    if cut:
        words.append(" and ".join(cut) + f": read on with the offsets, or pass a bigger max_bytes (at most "
                     f"{JOB_READ_MOST}).")
    if any(words[1:]):
        res["note"] = " ".join(w for w in words if w)
    return res


def _within(data: bytes, offset: int, room: int, from_end: bool) -> tuple[bytes, int]:
    """`data` (read at `offset`) cut to `room` characters of the result: its start, or with `from_end`
    its end (then the offset moves to where that starts), never in the middle of a character."""
    if from_end:
        start = execs.suffix_within(data, room)
        if start:
            kept = execs._trim_utf8_start(data[start:])
            return kept, offset + len(data) - len(kept)
        return data, offset
    n = execs.prefix_within(data, room)
    return (execs._trim_utf8_end(data[:n])[0], offset) if n < len(data) else (data, offset)


async def _start_and_end(mgr: Any, j: Any, stream: str, offset: int, data: bytes,
                         max_bytes: int) -> tuple[str, int, str] | None:
    """lane_job_read(until_exit) of a job that ended with more of `stream` after `offset` than one
    read: what an agent waits for is the end (the error, the summary). The start and the end as
    lane_exec keeps them (a quarter and three quarters of max_bytes), cut at line ends, with a
    marker that names the bytes skipped and the offset that reads them. (text, next offset, note),
    or None when `data` is all there is and fits. max_bytes counts characters of the result."""
    kept = j.kept(stream)
    if offset + len(data) >= kept and execs.escaped_len(data) <= max_bytes:
        return None
    head = data[:max(1, execs.prefix_within(data, max_bytes // 4))]
    cut = head.rfind(b"\n")
    head = head[:cut + 1] if cut >= 0 else execs._trim_utf8_end(head)[0]
    room = max(1, max_bytes - execs.escaped_len(head) - 100)  # 100: the marker
    raw = await mgr.job_log(j.info.id, stream, max(offset + len(head), kept - room - 1), room + 1)
    start = execs.suffix_within(raw[1:], room) + 1  # the byte before it: is the tail a line start?
    tail = raw[start:]
    if raw[start - 1:start] != b"\n":
        line = tail.find(b"\n")
        tail = tail[line + 1:] if 0 <= line < len(tail) - 1 else execs._trim_utf8_start(tail)
    skip_from, skipped = offset + len(head), kept - len(tail) - offset - len(head)
    text = head.decode("utf-8", "replace") + ("\n" if head and not head.endswith(b"\n") else "") + (
        f"[agentos: {skipped} bytes skipped here, of {kept} in all; read them with {stream}_offset={skip_from}]\n"
    ) + tail.decode("utf-8", "replace")
    return text, kept, (f"The job ended with more {stream} than max_bytes: {stream} is its start and its end, "
                        f"{skipped} bytes skipped between them (read them with {stream}_offset={skip_from}, or "
                        "pass a bigger max_bytes).")


def _int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InvalidRequest(f"{name} is a whole number") from None


def _port(value: Any) -> int:
    port = _int(value or 0, "until_port")
    if not 0 <= port <= 65535:
        raise InvalidRequest("until_port is a port number, like 3000 (0: no port)")
    return port


async def lane_job_cancel(ctx: ToolContext, *, job: str, on: str | None = None) -> dict:
    """Stop a running job: SIGTERM to every process it started, SIGKILL a few seconds later."""
    mgr = _mgr(ctx)
    j = mgr.job(job)
    if on is not None:
        await ctx.authorize(Effect.READ, lane_id=j.info.lane)
        _job_on(on, j)
    await ctx.authorize(Effect.EXEC, lane_id=j.info.lane, summary=f"Cancel job {job} in lane {j.info.lane}: "
                        + _clip(j.info.cmd), content={"cancel": job}, facts=_facts(mgr.get(j.info.lane)))
    return _job_view(await mgr.job_cancel(job))


async def lane_jobs_list(ctx: ToolContext, *, on: str | None = None) -> list[dict]:
    """Background jobs on this node (of one lane with `on`), newest first, running and finished. An
    environment's jobs show only with its on (the on your environment_open gave)."""
    if on is not None:
        await ctx.authorize(Effect.READ, lane_id=on)
    else:
        await ctx.authorize(Effect.READ)
    allowed = getattr(ctx, "can_access_lane", None)
    # one chat holds an environment (agentos.environments.picking): an agent's list without `on` names no
    # machine's jobs, since it cannot tell whose chat each is (the owner's own context is not an MCP call)
    hide_env = on is None and getattr(ctx, "mcp", True)
    return [_job_view(i) for i in _mgr(ctx).job_list(on) if (allowed is None or allowed(i.lane))
            and not (hide_env and i.lane.startswith("env/"))]


# ------------------------------------------------------------------ shells
async def shell_open(ctx: ToolContext, *, on: str, cols: int = 120, rows: int = 32) -> dict:
    """Open an interactive shell (a PTY) in a lane. The session keeps running when you
    disconnect, until it is closed or idle for a long time. Returns the session id."""
    mgr = _mgr(ctx)
    lane = mgr.get(on)
    mgr.check_runnable(lane)  # a sandbox this node can never run is refused before anyone is asked
    await ctx.authorize(Effect.EXEC, lane_id=on, summary=f"Open an interactive shell {_where(lane)}",
                        content={"shell": on}, facts=_facts(lane))
    s = await mgr.shell_open(on, cols=cols, rows=rows, term="dumb", grant_id=_grant_id(ctx))
    ctx.trace("shell_open", lane=on, session=s.id)
    return {"session": s.id, **s.info().model_dump(mode="json")}


async def shell_write(ctx: ToolContext, *, session: str, text: str) -> dict:
    """Type text into a shell session. Include "\\n" to press Enter; "\\u0003" is Ctrl-C."""
    mgr = _mgr(ctx)
    s = mgr.shell(session)
    await ctx.authorize(Effect.EXEC, lane_id=s.lane_id,
                        summary=_clip(f"Type into shell {session} in lane {s.lane_id}: {text!r}"),
                        content={"session": session, "text": text}, facts=_facts(mgr.get(s.lane_id)))
    n = await s.write(text.encode())
    return {"session": session, "written": n}


async def shell_read(ctx: ToolContext, *, session: str, wait_ms: int = 1000, max_bytes: int = 65536,
                     base64_output: bool = False) -> dict:
    """Read new output from a shell session since the last read, waiting up to wait_ms for
    some to arrive. `dropped` counts bytes lost because they scrolled out of the buffer."""
    mgr = _mgr(ctx)
    s = mgr.shell(session)
    await ctx.authorize(Effect.READ, lane_id=s.lane_id)
    data, dropped = await s.read(wait_s=max(0, min(wait_ms, 30000)) / 1000, max_bytes=max(1, min(max_bytes, 1 << 20)))
    out = base64.b64encode(data).decode() if base64_output else s.read_text(data)
    return {"session": session, "output": out, "encoding": "base64" if base64_output else "utf-8",
            "bytes": len(data), "dropped": dropped, "running": s.running, "exit_code": s.exit_code}


async def shell_close(ctx: ToolContext, *, session: str) -> dict:
    """Close a shell session and end every process it started."""
    mgr = _mgr(ctx)
    s = mgr.shell(session)
    await ctx.authorize(Effect.EXEC, lane_id=s.lane_id, summary=f"Close shell {session} in lane {s.lane_id}",
                        content={"close": session}, facts=_facts(mgr.get(s.lane_id)))
    await mgr.shell_close(session)
    return {"session": session, "closed": True}


# ------------------------------------------------------------------ services
async def service_start(ctx: ToolContext, *, on: str, ttl_s: float | None = None) -> dict:
    """Start (or keep running) a service lane's supervised command, restarted with backoff if
    it crashes. ttl_s stops it automatically after that many seconds."""
    mgr = _mgr(ctx)
    lane = mgr.get(on)
    mgr.check_runnable(lane)  # a sandbox this node can never run is refused before anyone is asked
    cmd = " ".join(lane.service.command) if lane.service else "(not a service lane)"
    ttl = f", stops after {int(ttl_s)}s" if ttl_s else ""
    await ctx.authorize(Effect.SERVICE, lane_id=on,
                        summary=_clip(f"Run service {_where(lane)}: {cmd} (restart "
                                      f"{lane.service.restart if lane.service else '-'}{ttl})"),
                        content={"service": on, "command": lane.service.command if lane.service else None,
                                 "ttl_s": ttl_s}, facts=_facts(lane))
    st = await mgr.service_start(on, ttl_s=ttl_s)
    ctx.trace("service_start", lane=on)
    return st.model_dump(mode="json")


async def service_stop(ctx: ToolContext, *, on: str) -> dict:
    """Stop a service lane's command (SIGTERM, then SIGKILL after its stop timeout)."""
    mgr = _mgr(ctx)
    # stopping runs nothing in the lane, so it carries no `runs_on_node`: stopping a service on a lane
    # that reaches the node must stay as easy as it was
    await ctx.authorize(Effect.SERVICE, lane_id=on, summary=f"Stop the service in lane {on}",
                        content={"stop": on})
    st = await mgr.service_stop(on)
    return st.model_dump(mode="json")


async def service_status(ctx: ToolContext, *, on: str) -> dict:
    """State of a service: running, backoff, failed or stopped; health, restarts, last exit."""
    await ctx.authorize(Effect.READ, lane_id=on)
    return _mgr(ctx).service_status(on).model_dump(mode="json")


async def service_logs(ctx: ToolContext, *, on: str, lines: int = 100) -> dict:
    """The last lines of a service's output (rotated log files included)."""
    await ctx.authorize(Effect.READ, lane_id=on)
    return {"lane": on, "log": _mgr(ctx).service_logs(on, lines)}


# ------------------------------------------------------------------ images
async def images_list(ctx: ToolContext) -> dict:
    """Base image templates and the versions built on this node (with content hashes)."""
    await ctx.authorize(Effect.READ)
    mgr = _mgr(ctx)
    return {"templates": [{"name": t.name, "description": t.description, "apt": t.apt, "pip": t.pip,
                           "npm": t.npm, "placeholder": t.placeholder} for t in mgr.templates().values()],
            "built": [i.model_dump(mode="json", exclude={"manifest"}) for i in mgr.images()]}


async def image_build(ctx: ToolContext, *, template: str) -> dict:
    """Build a new version of a base image from its template (a shared change: every lane
    created on the new version sees it)."""
    mgr = _mgr(ctx)
    t = mgr.templates().get(template)
    pk = []
    if t:
        for src in ("apt", "pip", "npm"):
            if getattr(t, src):
                pk.append(f"{src}: {' '.join(getattr(t, src))}")
    nxt = mgr.store.latest_version(template) + 1
    await ctx.authorize(Effect.INSTALL_BASE,
                        summary=f"Build base image {template}@{nxt} ({'; '.join(pk) or 'no packages'})",
                        content={"template": template, "version": nxt})
    info = await mgr.build_image(template)
    ctx.trace("image_build", image=info.ref, hash=info.content_hash)
    return info.model_dump(mode="json")


def tool_specs(runtime: Any) -> list[ToolSpec]:
    t = ("env",)
    return [
        ToolSpec("env_lanes_list", env_lanes_list, Effect.READ, lane_arg=None, tags=t, hidden_without="envs"),
        ToolSpec("env_create", env_create, Effect.EXEC, tags=t, hidden_without="envs"),
        ToolSpec("env_remove", env_remove, Effect.IRREVERSIBLE, tags=t, hidden_without="envs"),
        ToolSpec("lane_exec", lane_exec, Effect.EXEC, tags=(*t, "terminal"), hidden_without="envs"),
        ToolSpec("lane_install", lane_install, Effect.INSTALL_OVERLAY, tags=(*t, "programs"), hidden_without="envs"),
        ToolSpec("lane_upload_check", lane_upload_check, Effect.READ, tags=(*t, "terminal"), hidden_without="envs"),
        ToolSpec("lane_upload", lane_upload, Effect.DRAFT, tags=(*t, "terminal"), hidden_without="envs"),
        ToolSpec("lane_job_start", lane_job_start, Effect.EXEC, tags=(*t, "terminal"), hidden_without="envs"),
        ToolSpec("lane_job_read", lane_job_read, Effect.READ, lane_arg=None, tags=(*t, "terminal"),
                 hidden_without="envs"),
        ToolSpec("lane_job_cancel", lane_job_cancel, Effect.EXEC, lane_arg=None, tags=(*t, "terminal"),
                 hidden_without="envs"),
        ToolSpec("lane_jobs_list", lane_jobs_list, Effect.READ, lane_arg=None, tags=(*t, "terminal"),
                 hidden_without="envs"),
        ToolSpec("shell_open", shell_open, Effect.EXEC, tags=(*t, "terminal"), hidden_without="envs"),
        ToolSpec("shell_write", shell_write, Effect.EXEC, lane_arg=None, tags=(*t, "terminal"), hidden_without="envs"),
        ToolSpec("shell_read", shell_read, Effect.READ, lane_arg=None, tags=(*t, "terminal"), hidden_without="envs"),
        ToolSpec("shell_close", shell_close, Effect.EXEC, lane_arg=None, tags=(*t, "terminal"), hidden_without="envs"),
        ToolSpec("service_start", service_start, Effect.SERVICE, tags=(*t, "service"), hidden_without="envs"),
        ToolSpec("service_stop", service_stop, Effect.SERVICE, tags=(*t, "service"), hidden_without="envs"),
        ToolSpec("service_status", service_status, Effect.READ, tags=(*t, "service"), hidden_without="envs"),
        ToolSpec("service_logs", service_logs, Effect.READ, tags=(*t, "service"), hidden_without="envs"),
        ToolSpec("images_list", images_list, Effect.READ, lane_arg=None, tags=(*t, "programs"), hidden_without="envs"),
        ToolSpec("image_build", image_build, Effect.INSTALL_BASE, lane_arg=None, tags=(*t, "programs"),
                 hidden_without="envs"),
    ]
