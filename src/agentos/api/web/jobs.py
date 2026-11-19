"""Runs: commands started with `agentos run` (terminal_job_start), as the app's Runs pages read them.

    GET  /api/orgs/{org}/jobs?lane=&state=&limit=        every run the caller may see, newest first
    GET  /api/orgs/{org}/jobs/{id}?stdout_offset=&stderr_offset=&wait_ms=
                                                        one run, with its output after those offsets
    POST /api/orgs/{org}/jobs/{id}/cancel               stop it

Named `/jobs` so they never meet `/runs`, which are routine runs. The node has no HTTP route for jobs, so
these read the env manager (agentos.env) themselves and decide who may: reading needs `see` and a cancel
`operate` (a member, in the lane's workspace), and every run is checked against the caller's grant, as
terminal_jobs and terminal_job_read check it. A cancel is authorized as terminal_job_cancel authorizes it.

What a command printed is data: it is returned as text, capped per call. The test totals are read from
the runner's own summary line (agentos.env.testsummary) and marked as read from the output; the git branch
and commit are what `agentos run` said about the tree it sent. Where neither is known, the field is left
out. There are no run events on the live stream: the app polls these routes while a run is going.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from starlette.requests import Request
from starlette.routing import BaseRoute

from agentos.api.app import Api, access_of
from agentos.api.errors import ApiError
from agentos.api.web.base import holder_names, org_route
from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Effect
from agentos.env import execs, testsummary

COMMAND_CAP = 2000
NOTE_CAP = 300
LIST_LIMIT = 200
READ_BYTES = 65536            # of each stream, per call
WAIT_MS_MAX = 10_000
TAIL_BYTES = 65536            # of each stream, read for the test summary
STATES = ("running", "passed", "failed", "cancelled", "timed_out", "lost")
NO_TERMINALS = "This computer runs no terminal lanes, so it has no runs. Runs start with `agentos run`."
OUT_OF_SCOPE = "This run is in a lane outside your grant."

# A finished run's test summary never changes: read once, kept by id (and its output size, to be sure).
_summaries: dict[tuple[str, int, int], dict[str, Any] | None] = {}
_SUMMARIES_KEPT = 2000


def manager(api: Api) -> Any | None:
    """The env manager (agentos.env.manager.EnvManager), through the panel's adapter when it has one."""
    envs = api.optional("envs")
    mgr = getattr(envs, "envs", envs)
    return mgr if mgr is not None and callable(getattr(mgr, "job_list", None)) else None


def state_of(info: Any) -> str:
    """A job's end as the app reads it: an exit of 0 passed, any other exit or a signal failed."""
    if info.state == "exited":
        return "passed" if info.exit_code == 0 and not info.signal else "failed"
    return info.state


def cap(text: Any, n: int) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def started_by(by: str | None, holders: Mapping[str, str]) -> str | None:
    """The owner and a member in words; an agent by its own name. A grant id is never shown."""
    if not by:
        return None
    if by in holders:
        return holders[by]
    if by.startswith(("grant_", "g_")):
        return "An agent"
    return by


def job_out(info: Any, node: str, holders: Mapping[str, str], tests: dict[str, Any] | None) -> dict[str, Any]:
    workspace, _, name = info.lane.partition("/")
    sync = info.sync or None
    out: dict[str, Any] = {
        "id": info.id,
        "lane": info.lane,
        "workspace": workspace,
        "lane_name": name,
        "node": node,
        "command": cap(info.cmd, COMMAND_CAP),
        "cwd": info.cwd,
        "state": state_of(info),
        "exit_code": info.exit_code,
        "signal": info.signal,
        "oom_killed": bool(info.oom_killed),
        "started_at": info.started_at.isoformat(),
        "finished_at": info.finished_at.isoformat() if info.finished_at else None,
        "duration_s": round(info.duration_s, 3) if info.duration_s is not None else None,
        "timeout_s": info.timeout_s,
        "started_by": started_by(info.by, holders),
        "stdout_bytes": info.stdout_bytes,
        "stderr_bytes": info.stderr_bytes,
        "dropped_bytes": (info.stdout_dropped or 0) + (info.stderr_dropped or 0),
        "sync": {k: sync.get(k) for k in ("dest", "files", "changed", "deleted", "bytes")} if sync else None,
        "note": cap(info.note, NOTE_CAP),
    }
    source = getattr(info, "source", None)
    if isinstance(source, dict) and (source.get("branch") or source.get("commit")):
        out["source"] = {"branch": source.get("branch"), "commit": source.get("commit"),
                         "dirty": source.get("dirty"), "told_by": "client"}
    if tests is not None:
        out["tests"] = tests
    return out


async def tests_of(mgr: Any, info: Any) -> dict[str, Any] | None:
    """The test summary at the end of a run's output, or None. Kept once the run has ended."""
    key = (info.id, info.stdout_bytes, info.stderr_bytes)
    if info.state != "running" and key in _summaries:
        return _summaries[key]
    try:
        job = mgr.job(info.id)
        out_from = max(0, job.kept("stdout") - TAIL_BYTES)
        err_from = max(0, job.kept("stderr") - TAIL_BYTES)
        _info, out, err = await mgr.job_read(info.id, stdout_offset=out_from, stderr_offset=err_from,
                                             max_bytes=TAIL_BYTES)
    except (NotFound, OSError):
        return None
    found = testsummary.read(out.decode("utf-8", "replace") + "\n" + err.decode("utf-8", "replace"))
    if info.state != "running":
        if len(_summaries) >= _SUMMARIES_KEPT:
            _summaries.clear()
        _summaries[key] = found
    return found


def _int(raw: str | None, name: str, default: int, lo: int, hi: int) -> int:
    if raw is None or raw == "":
        return default
    if not raw.lstrip("-").isdigit():
        raise InvalidRequest(f"{name} is a number.")
    return max(lo, min(int(raw), hi))


def routes(api: Api) -> list[BaseRoute]:
    def find(request: Request, mgr: Any, action: str) -> Any:
        """The run in the path, refused as not found when it is outside the caller's grant (so its
        lane is not told)."""
        job = mgr.job(request.path_params["id"])
        access = access_of(request)
        if not access.lane_allowed(job.info.lane):
            access.trace(action, status="denied", lane=job.info.lane, summary="out of scope")
            raise ApiError("denied", OUT_OF_SCOPE)
        return job

    async def listing(request: Request) -> dict[str, Any]:
        mgr = manager(api)
        if mgr is None:
            return {"available": False, "reason": NO_TERMINALS, "jobs": []}
        q = request.query_params
        lane = q.get("lane") or None
        state = q.get("state") or None
        if state is not None and state not in STATES:
            raise InvalidRequest(f"state is one of {', '.join(STATES)}.")
        limit = _int(q.get("limit"), "limit", 50, 1, LIST_LIMIT)
        access = access_of(request)
        holders = await holder_names(request)
        rows = []
        for info in mgr.job_list(lane):
            if not access.lane_allowed(info.lane) or (state is not None and state_of(info) != state):
                continue
            rows.append(job_out(info, api.node_name, holders, await tests_of(mgr, info)))
            if len(rows) >= limit:
                break
        return {"available": True, "jobs": rows}

    async def one(request: Request) -> dict[str, Any]:
        mgr = manager(api)
        if mgr is None:
            raise NotFound(NO_TERMINALS)
        job = find(request, mgr, "job.read")
        await access_of(request).authorize(Effect.READ, action="job.read", lane_id=job.info.lane)
        q = request.query_params
        out_at = _int(q.get("stdout_offset"), "stdout_offset", 0, 0, 1 << 40)
        err_at = _int(q.get("stderr_offset"), "stderr_offset", 0, 0, 1 << 40)
        wait_ms = _int(q.get("wait_ms"), "wait_ms", 0, 0, WAIT_MS_MAX)
        info, out, err = await mgr.job_read(job.info.id, stdout_offset=out_at, stderr_offset=err_at,
                                            wait_s=wait_ms / 1000, max_bytes=READ_BYTES)
        if info.state == "running":  # never cut a character in two: the rest comes with the next read
            out, _ = execs._trim_utf8_end(out)
            err, _ = execs._trim_utf8_end(err)
        next_out, next_err = out_at + len(out), err_at + len(err)
        more = job.kept("stdout") > next_out or job.kept("stderr") > next_err
        holders = await holder_names(request)
        return {**job_out(info, api.node_name, holders, await tests_of(mgr, info)),
                "output": {"stdout": out.decode("utf-8", "replace"), "stderr": err.decode("utf-8", "replace"),
                           "stdout_offset": out_at, "stderr_offset": err_at,
                           "next_stdout_offset": next_out, "next_stderr_offset": next_err, "more": more}}

    async def cancel(request: Request) -> dict[str, Any]:
        mgr = manager(api)
        if mgr is None:
            raise NotFound(NO_TERMINALS)
        job = find(request, mgr, "job.cancel")
        access = access_of(request)
        lane = job.info.lane
        access.require("operate", lane.partition("/")[0], action="job.cancel", lane=lane)
        from agentos.env import exposure

        facts = exposure.facts(mgr.get(lane)) if callable(getattr(mgr, "get", None)) and mgr.has(lane) else None
        await access.authorize(Effect.EXEC, action="job.cancel", lane_id=lane,
                               summary=f"Cancel job {job.info.id} in lane {lane}: {cap(job.info.cmd, 300)}",
                               content={"cancel": job.info.id}, facts=facts)
        info = await mgr.job_cancel(job.info.id)
        access.done("job.cancel", lane=lane, summary=f"cancelled job {info.id}")
        return job_out(info, api.node_name, await holder_names(request), None)

    return [
        org_route(api, "/jobs", listing, ["GET"], cap="see"),
        org_route(api, "/jobs/{id}", one, ["GET"], cap="see"),
        org_route(api, "/jobs/{id}/cancel", cancel, ["POST"], cap="operate"),
    ]
