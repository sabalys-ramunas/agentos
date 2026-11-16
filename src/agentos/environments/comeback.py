"""Coming back to an environment: the saved services started again and checked listening, the
environment found by its repository alone, and what a pause costs in words.

- `start_services`: on a resume (and after a snapshot) each service a save holds (a lane_job_start
  command that ran, with the port it served) is started again, and the node waits up to
  SERVICE_WAIT_S in all for those ports to listen. `resumed.services` says for each one whether it
  runs and listens, and gives a ready `restart` call for one that did not come back.
- `by_repo`: environment_open(repo=X) with no title. The newest open or saved environment of the
  family for that repository, when there is exactly one; several are a choice the agent makes (each
  title is listed with the call that takes it); none: a new one titled after the repository.
- `pause_words`: what a stopped environment costs while it waits, and how long a resume takes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from agentos.core.errors import AgentosError, InvalidRequest

log = logging.getLogger("agentos.environments")

SERVICE_WAIT_S = 45.0     # all the saved services' ports together, at most, on a resume
POLL_S = 1.0


class EnvironmentChoice(InvalidRequest):
    """environment_open(repo=X) with no title found several environments for X."""
    code = "environment_choice"


def restart_call(cmd: str, cwd: str | None) -> str:
    """The lane_job_start call that starts a service again, ready to send."""
    args = f"cmd={json.dumps(cmd)}" + (f", cwd={json.dumps(cwd)}" if cwd else "")
    return f"lane_job_start({args})"


async def start_services(mgr: Any, env: Any, services: list[dict], *, wait_s: float = 0.0) -> list[dict]:
    """Start each saved service; with `wait_s`, wait (all together, at most that long) for the ports
    they serve to listen. One entry per service: cmd, cwd, port, running, listening (None when it names
    no port), job_id, and for one that did not come back `restart` and `why`."""
    envs = mgr.envs()
    out: list[dict] = []
    for s in services:
        row: dict = {"cmd": s["cmd"], "cwd": s.get("cwd"), "port": s.get("port"), "running": False}
        try:
            job = await envs.job_start(env.terminal_lane, s["cmd"], cwd=s.get("cwd"), by="agentos (resume)",
                                       grant_id=env.grant_id)
            row["running"] = job.state == "running"
            row["job_id"] = job.id
            if not row["running"]:
                row["why"] = f"it {job.state} at once"
        except Exception as e:  # noqa: BLE001 - the resume says it did not start, and how to start it
            row["why"] = str(getattr(e, "message", e))[:200] or type(e).__name__
            log.warning("environment %s: service %r did not start: %s", env.id, s["cmd"], row["why"])
        out.append(row)
    waiting = {r["port"] for r in out if r["running"] and r.get("port")}
    listening: set[int] = set()
    if waiting and wait_s > 0:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while True:
            with contextlib.suppress(AgentosError, Exception):
                listening = {p["port"] for p in await mgr.ports(env) if p.get("port")}
            if waiting <= listening or loop.time() >= deadline:
                break
            await asyncio.sleep(POLL_S)
        _still_running(envs, env.terminal_lane, out)
    for r in out:
        if r.get("port") and r["running"] and wait_s > 0:
            r["listening"] = r["port"] in listening
            if not r["listening"]:
                r["why"] = (f"it runs, but port {r['port']} did not listen within {int(wait_s)} s; "
                            "lane_job_read(id) shows its output")
        if not r["running"]:
            r["restart"] = restart_call(r["cmd"], r.get("cwd"))
    return out


def _still_running(envs: Any, lane: str, rows: list[dict]) -> None:
    """A service that exited while the ports were awaited does not run (a crash at start)."""
    with contextlib.suppress(Exception):
        jobs = {j.id: j for j in envs.job_list(lane)}
        for r in rows:
            j = jobs.get(r.get("job_id") or "")
            if r["running"] and j is not None and j.state != "running":
                r["running"], r["why"] = False, f"it {j.state} while starting; lane_job_read(\"{j.id}\") shows why"


def lost_jobs(services: list[dict]) -> list[str]:
    """`running_jobs_lost`: the saved services, in words, when none of them came back."""
    if not services or any(s.get("running") for s in services):
        return []
    return [s["cmd"] + (f" (port {s['port']})" if s.get("port") else "") for s in services]


# ---------------------------------------------------------------- the repository alone
def _repo_title(repo: str) -> str:
    name = repo.rstrip("/").rsplit("/", 1)[-1] or repo
    return name[:80]


def by_repo(mgr: Any, family: str | None, repo: str, *, saves_of: str, reaches: Any = None) -> str:
    """The title environment_open(repo=X) with no title uses: the one open or saved environment of
    this family for `repo` (newest first), or the repository's name when there is none. Raises
    EnvironmentChoice, listing them, when there are several. `family` None: the owner (every open
    one); `saves_of`: whose saves; `reaches(env)`: the caller may use that open one."""
    found: dict[str, dict] = {}
    for e in mgr.open_for_family(family):
        if e.repo == repo and e.state in ("starting", "ready") and (reaches is None or reaches(e)):
            found.setdefault(e.title.lower(), {"title": e.title, "state": "open",
                                               "at": e.last_activity().isoformat()})
    for m in mgr.saves().all(saves_of):
        if (m.repo or None) == repo:
            found.setdefault(m.title_key, {"title": m.title, "state": "saved", "at": m.saved_at.isoformat()})
    rows = sorted(found.values(), key=lambda r: r["at"], reverse=True)
    if len(rows) == 1:
        return rows[0]["title"]
    if not rows:
        return _repo_title(repo)
    listed = "; ".join(f"\"{r['title']}\" ({r['state']}, {r['at'][:16].replace('T', ' ')} UTC)" for r in rows[:8])
    raise EnvironmentChoice(
        f"{len(rows)} environments work on {repo}: {listed}. Pick one by its title: "
        f"environment_open(title=\"{rows[0]['title']}\") takes the newest; a new title opens a new one")


# ---------------------------------------------------------------- what a pause costs
def resume_time(source: str, provider: str = "") -> str:
    if provider == "docker":
        return "in seconds"
    return "in about a minute" if source == "snapshot" else "in a few minutes (a fresh machine, then the files)"


def pause_words(cost_eur_month: float | None, *, snapshot: bool, provider: str = "") -> str:
    """"saved: about EUR 0.05 a month, resumes in about a minute"."""
    if not cost_eur_month:
        cost = "costs nothing while it waits" if not snapshot else "costs under a cent a month"
    else:
        cost = f"about EUR {cost_eur_month:.2f} a month" if cost_eur_month >= 0.01 else "under a cent a month"
    return f"saved: {cost}, resumes {resume_time('snapshot' if snapshot else 'files', provider)}"
