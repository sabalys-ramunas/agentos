"""What an environment shows its caller and the panel: ports, repositories and pushes, work not
saved yet, the summary, the detail and the links. A mixin of EnvironmentManager."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from agentos.core import clock
from agentos.core.app_paths import lane_viewer_path
from agentos.core.errors import AgentosError
from agentos.environments.models import (
    WORKSPACE,
    Environment,
)
from agentos.environments.probes import CHECK_TIMEOUT_S, GIT_STATE, main_repo, parse_git_state, parse_ports

UNSAVED_LIST_MAX = 50


class Views:
    """The read side of EnvironmentManager (it reads self.store, self.vms() and self.envs())."""

    # ================================================================ read
    async def ports(self, env: Environment) -> list[dict]:
        try:
            res = await self.envs().exec(env.terminal_lane, "ss -ltnpH 2>/dev/null || ss -ltnp",
                                         timeout_s=CHECK_TIMEOUT_S)
        except AgentosError:
            return []
        return parse_ports(res.stdout)

    async def git_repos(self, env: Environment) -> list[dict] | None:
        """Every repository under /work, the cloned one and any other the agent made: None when the
        machine does not answer."""
        try:
            res = await self.envs().exec(env.terminal_lane, GIT_STATE, timeout_s=CHECK_TIMEOUT_S)
        except AgentosError:
            return None
        if res.exit_code not in (0, None) and not res.stdout:
            return None
        return parse_git_state(res.stdout)

    def pushes(self, env: Environment) -> list[dict]:
        """What the git gateway pushed from this environment (its push traces), newest first."""
        policy = self._get("policy")
        traces = getattr(policy, "traces", None)
        out: list[dict] = []
        if traces is not None:
            with contextlib.suppress(Exception):
                events, _ = traces.query(kind="git_push", lane=env.terminal_lane, limit=50)
                for ev in events:
                    args = ev.args if isinstance(ev.args, dict) else {}
                    if args.get("result") != "pushed":
                        continue
                    at = datetime.fromtimestamp(ev.at or 0, UTC)
                    for ref in args.get("refs") or []:
                        name = str(ref.get("ref") or "")
                        commit = str(ref.get("new") or "").split(":", 1)[-1]
                        out.append({"branch": name.removeprefix("refs/heads/"), "commit": commit[:12], "at": clock.iso_or_none(at, z=True)})
        known = {(p["branch"], p["commit"]) for p in out}
        for p in env.pushed:
            if (p.branch, p.commit[:12]) not in known:
                out.append({"branch": p.branch, "commit": p.commit[:12], "at": clock.iso_or_none(p.at, z=True)})
        return out

    @staticmethod
    def unsaved_of(repos: list[dict] | None) -> dict | None:
        """{dirty_files, unpushed, stashes} of every repository (each named by its folder when
        there are several), or None when all of it is committed and on a remote. A repository git
        could not read counts as unsaved (`unreadable`)."""
        if not repos:
            return None
        many = len(repos) > 1
        dirty = [f"{r['dir']}/{f}" if many else f for r in repos for f in r["dirty_files"]]
        unpushed = [f"{r['dir']}: {c}" if many else c for r in repos for c in r["unpushed"]]
        stashes = [f"{r['dir']}: {s}" if many else s for r in repos for s in r.get("stashes") or []]
        unreadable = [r["dir"] for r in repos if r.get("error")]
        if not (dirty or unpushed or stashes or unreadable) and not any(
                r["dirty"] or r["ahead"] or r.get("stash_count") for r in repos):
            return None
        out = {"dirty_files": dirty[:UNSAVED_LIST_MAX], "unpushed": unpushed[:UNSAVED_LIST_MAX],
               "stashes": stashes[:UNSAVED_LIST_MAX]}
        if unreadable:
            out["unreadable"] = unreadable[:UNSAVED_LIST_MAX]
        return out

    @staticmethod
    def unsaved_words(unsaved: dict) -> str:
        """What unsaved_of found, for an error or a notification."""
        parts = []
        if unsaved.get("dirty_files"):
            parts.append("changed or new files: " + ", ".join(unsaved["dirty_files"][:10]))
        if unsaved.get("unpushed"):
            parts.append("commits not on a remote: " + "; ".join(unsaved["unpushed"][:10]))
        if unsaved.get("stashes"):
            parts.append("stashes: " + "; ".join(unsaved["stashes"][:5]))
        if unsaved.get("unreadable"):
            parts.append("repositories git cannot read: " + ", ".join(unsaved["unreadable"][:5]))
        return "; ".join(parts) or "changes git counts but did not list"

    def summary(self, env: Environment) -> dict:
        """EnvSummary of the contract (docs/design/environments.md)."""
        now = self._clock()
        return {"id": env.id, "title": env.title, "state": env.shown_state(now), "repo": env.repo,
                "branch": env.branch, "workdir": env.workdir, "created_at": clock.iso_or_none(env.created_at, z=True),
                "ready_at": clock.iso_or_none(env.ready_at, z=True), "last_call_at": clock.iso_or_none(env.last_call_at, z=True),
                "closed_at": clock.iso_or_none(env.closed_at, z=True), "expires_at": clock.iso_or_none(env.expires_at, z=True),
                "minutes_left": max(0, int((env.expires_at - now).total_seconds() // 60)) if env.open else None,
                "machine": env.machine.model_dump(), "price_hourly_eur": env.price_hourly_eur,
                "cost_eur": self.cost_now(env), "agent": {"name": env.agent, "client": env.client},
                "lanes": env.lanes, "closed_reason": env.closed_reason, "error": env.error,
                "error_code": env.error_code}

    def _commands(self, env: Environment) -> list[dict]:
        out = []
        envs = self._get("envs")
        if envs is not None and callable(getattr(envs, "job_list", None)):
            with contextlib.suppress(Exception):
                for j in envs.job_list(env.terminal_lane):
                    out.append({"id": j.id, "cmd": self._scrub(j.cmd, max_len=500), "state": j.state,
                                "started_at": clock.iso_or_none(j.started_at, z=True), "exit_code": j.exit_code})
        jobs = {self._scrub(r["cmd"]) for r in out}  # as record_call writes a summary
        for i, c in enumerate(env.calls):
            summary = self._scrub(c.summary)
            if c.tool == "lane_exec":
                out.append({"id": f"exec-{i}", "cmd": summary, "state": "exited" if c.ok else "failed",
                            "started_at": clock.iso_or_none(c.at, z=True), "exit_code": None})
            elif c.tool == "lane_job_start" and c.ok and summary not in jobs:
                # a job whose record went with its lane (the machine was deleted, or the node restarted)
                out.append({"id": f"start-{i}", "cmd": summary, "state": "ended", "started_at": clock.iso_or_none(c.at, z=True),
                            "exit_code": None})
        out.sort(key=lambda r: r["started_at"] or "", reverse=True)
        return out[:50]

    def _viewer(self, env: Environment) -> dict | None:
        lanes = self._get("lanes")
        if lanes is None:
            return None
        try:
            if lanes.view(env.browser_lane) is None:
                return None
        except Exception:  # noqa: BLE001
            return None
        return {"path": lane_viewer_path(WORKSPACE, f"{env.id}-browser")}

    async def detail(self, env: Environment, *, calls: int = 50) -> dict:
        """EnvDetail of the contract, and what environment() returns to an agent."""
        out = self.summary(env)
        repos = await self.git_repos(env) if env.open and env.state != "starting" and env.vm_id else None
        main = main_repo(repos, env.workdir)
        out["ports"] = await self.ports(env) if env.state in ("ready",) else []
        out["git"] = {"branch": (main or {}).get("branch") or env.branch, "ahead": (main or {}).get("ahead", 0),
                      "dirty": (main or {}).get("dirty", 0), "pushed": self.pushes(env)}
        out["branch"] = out["git"]["branch"]  # the branch checked out now, not the one it was cloned on
        out["calls"] = [{"at": clock.iso_or_none(c.at, z=True), "tool": c.tool, "summary": self._scrub(c.summary), "ok": c.ok}
                        for c in reversed(env.calls[-calls:])]
        out["commands"] = self._commands(env)
        out["viewer"] = self._viewer(env)
        out["unsaved"] = self.unsaved_of(repos)
        return out

    def links(self, env: Environment) -> dict:
        """The links an agent gives the human: `browser`, the browser's live view on the node itself
        (agentos.environments.tools fills it with a signed take-over link once the browser lane
        exists). No page of the web app: a node from the public repository has none."""
        return {"browser": None}
