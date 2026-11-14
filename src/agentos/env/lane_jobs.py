"""Background jobs in a lane: start one, read its output as it runs, cancel it, list them. A mixin
of EnvManager."""

from __future__ import annotations

import asyncio
import re

from agentos.core.errors import (
    InvalidRequest,
    LaneBusy,
    PolicyDenied,
)
from agentos.env import sync as syncs
from agentos.env.jobs import Job, new_job_id
from agentos.env.models import (
    EnvLane,
    JobInfo,
)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")  # a job's environment variable names


class Jobs:
    """EnvManager's job commands (they read self.jobs, self.store and self.warm)."""

    # =============================================================== jobs
    def _job_argv(self, lane: EnvLane, cmd: str, cwd: str | None) -> list[str]:
        if not lane.policy.exec:
            raise PolicyDenied(f"commands are off in {lane.preset.value} lanes")
        if not isinstance(cmd, str) or not cmd.strip():
            raise InvalidRequest("empty command")
        if cwd is not None:
            cwd = syncs.check_cwd(cwd, remote=lane.remote is not None)
            if lane.policy.signed_only:
                raise InvalidRequest(f"locked lane {lane.id} runs allowlisted programs only, without a cwd")
            # plain names (check_cwd), so it is safe as one shell word
            return self.command_argv(lane, f"cd '{cwd}' && {cmd}")
        return self.command_argv(lane, cmd)

    def check_job(self, lane_id: str, cmd: str, *, cwd: str | None = None, env: dict[str, str] | None = None,
                  timeout_s: float | None = None) -> float:
        """What job_start would refuse, before anyone is asked to approve it. Returns the timeout."""
        lane = self.store.get_lane(lane_id)
        self._job_argv(lane, cmd, cwd)
        for k, v in (env or {}).items():
            if not isinstance(k, str) or not _ENV_NAME_RE.match(k) or not isinstance(v, str) or "\x00" in v \
                    or len(v) > 4096:
                raise InvalidRequest(f"env: {str(k)[:64]!r} is not a variable name with a short text value")
        if len(env or {}) > 32:
            raise InvalidRequest("env takes at most 32 variables")
        self.jobs.check_room(lane_id)
        return min(float(timeout_s or self.settings.max_timeout_s), self.settings.max_timeout_s)

    async def job_start(self, lane_id: str, cmd: str, *, cwd: str | None = None, env: dict[str, str] | None = None,
                        timeout_s: float | None = None, sync: dict | None = None, by: str | None = None,
                        grant_id: str | None = None, source: dict | None = None) -> JobInfo:
        """Start `cmd` in the background (after applying `sync`, the output of sync_prepare). Its output
        is logged on the node; read it with job_read, from any offset, while it runs and after."""
        timeout = self.check_job(lane_id, cmd, cwd=cwd, env=env, timeout_s=timeout_s)
        lane = self.store.get_lane(lane_id)
        argv = self._job_argv(lane, cmd, cwd)
        applied = None
        if sync is not None:
            async with self._sync_lock(lane_id):
                busy = [j for j in self.jobs.running_in(lane_id) if (j.info.sync or {}).get("dest") == sync["dest"]]
                if busy:
                    raise LaneBusy(f"job {busy[0].info.id} still runs in {sync['dest']} of {lane_id}; a sync now "
                                   "would change its files under it. Wait for it, or cancel it with lane_job_cancel")
                applied = await self.sync_apply(lane_id, sync)
                self.jobs.check_room(lane_id)
                rt = await self.warm(lane_id)
                info = JobInfo(id=new_job_id(), lane=lane_id, cmd=cmd, cwd=cwd, timeout_s=timeout, sync=applied,
                               by=by, source=source)
                job_env = {**(await self.git_env(rt, grant_id)), **(env or {})}
                job = await self.jobs.start(rt, argv, info=info, env=job_env or None, on_end=self._job_ended)
        else:
            rt = await self.warm(lane_id)
            self.jobs.check_room(lane_id)
            info = JobInfo(id=new_job_id(), lane=lane_id, cmd=cmd, cwd=cwd, timeout_s=timeout, by=by, source=source)
            job_env = {**(await self.git_env(rt, grant_id)), **(env or {})}
            job = await self.jobs.start(rt, argv, info=info, env=job_env or None, on_end=self._job_ended)
        await self.store.run(self.store.touch, lane_id)
        await asyncio.to_thread(self.jobs.prune, lane_id)
        return job.info.model_copy()

    async def _job_ended(self, job: Job) -> None:
        if self.store.has_lane(job.info.lane):
            await self.store.run(self.store.touch, job.info.lane)  # the idle timer starts when the job ends

    def job(self, job_id: str) -> Job:
        return self.jobs.get(job_id)

    async def job_read(self, job_id: str, *, stdout_offset: int = 0, stderr_offset: int = 0, wait_s: float = 0,
                       max_bytes: int = 65536) -> tuple[JobInfo, bytes, bytes]:
        job = self.jobs.get(job_id)
        out, err = await self.jobs.read(job, stdout_offset, stderr_offset, wait_s=wait_s, max_bytes=max_bytes)
        info = job.info.model_copy()
        if job.running:  # what the command wrote so far (the meta has it once it ended)
            info.stdout_bytes = job.out.total if job.out else info.stdout_bytes
            info.stderr_bytes = job.err.total if job.err else info.stderr_bytes
        return info, out, err

    async def job_cancel(self, job_id: str) -> JobInfo:
        job = self.jobs.get(job_id)
        await self.jobs.cancel(job)
        return job.info.model_copy()

    def job_list(self, lane_id: str | None = None) -> list[JobInfo]:
        return [i.model_copy() for i in self.jobs.infos(lane_id)]
