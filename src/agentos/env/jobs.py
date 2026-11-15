"""Background commands in a lane (lane_job_start): start, read the output from any offset, cancel.

lane_exec waits for its command and keeps 64 KB of output; a test suite runs for half an hour and
prints megabytes, and the person who started it wants to see it as it goes, or to close the laptop
and come back. A job is a workload like an exec, but its stdout and stderr go to two log files on
the node (`<lane dir>/logs/jobs/<id>.out` and `.err`, outside the lane), and a reader asks for what
came after the offset it already has, waiting up to a few seconds for more (a long poll). A job runs
until it exits, its timeout, a cancel, or the lane going cold, whoever is connected.

Each job's meta is a JSON file beside its logs, so a finished job can still be read after the node
restarts; one that was running then is marked `lost`. The newest `jobs_keep_per_lane` finished jobs
of a lane are kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from agentos.core.errors import LaneBusy, NotFound
from agentos.core.models import utcnow
from agentos.env.execs import exit_status
from agentos.env.lanes import LaneRuntime, Spawned, new_wid, wait_exit
from agentos.env.models import JobInfo
from agentos.env.paths import EnvPaths, EnvSettings
from agentos.env.sandbox import Workload

log = logging.getLogger("agentos.env")

JOB_ID_PREFIX = "job-"
LEFTOVER_GRACE_S = 0.5
CANCEL_GRACE_S = 5.0
READ_MAX_BYTES = 1 << 20


def new_job_id() -> str:
    return JOB_ID_PREFIX + secrets.token_hex(5)


def valid_job_id(job_id: str) -> bool:
    rest = job_id[len(JOB_ID_PREFIX):] if isinstance(job_id, str) and job_id.startswith(JOB_ID_PREFIX) else ""
    return len(rest) == 10 and all(c in "0123456789abcdef" for c in rest)


class LogSink:
    """One stream's log file: appended as output arrives, kept up to `cap` bytes, the rest counted."""

    def __init__(self, path: Path, cap: int):
        self.path = path
        self.cap = cap
        self.kept = 0
        self.total = 0
        self._f = open(path, "ab")  # noqa: SIM115 (closed by close())

    def feed(self, data: bytes) -> None:
        self.total += len(data)
        room = self.cap - self.kept
        if room > 0:
            part = data[:room]
            self._f.write(part)
            self._f.flush()
            self.kept += len(part)

    @property
    def dropped(self) -> int:
        return self.total - self.kept

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._f.close()


@dataclass
class Job:
    info: JobInfo
    dir: Path
    sp: Spawned | None = None
    rt: LaneRuntime | None = None
    task: asyncio.Task | None = None
    out: LogSink | None = None
    err: LogSink | None = None
    cancel_requested: bool = False
    stop_note: str | None = None
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)

    @property
    def running(self) -> bool:
        return self.info.state == "running"

    def log_path(self, stream: str) -> Path:
        return self.dir / f"{self.info.id}.{'out' if stream == 'stdout' else 'err'}"

    def meta_path(self) -> Path:
        return self.dir / f"{self.info.id}.json"

    def kept(self, stream: str) -> int:
        """Bytes of the stream on disk (what a reader can get)."""
        sink = self.out if stream == "stdout" else self.err
        if sink is not None:
            return sink.kept
        try:
            return self.log_path(stream).stat().st_size
        except OSError:
            return 0

    def save(self) -> None:
        if not self.dir.is_dir():  # the lane was removed with its logs
            return
        tmp = self.meta_path().with_suffix(".tmp")
        tmp.write_text(self.info.model_dump_json())
        tmp.replace(self.meta_path())

    async def notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()


class JobTable:
    """Every job this node knows: running ones in memory, finished ones read back from disk."""

    def __init__(self, paths: EnvPaths, settings: EnvSettings):
        self.paths = paths
        self.settings = settings
        self.jobs: dict[str, Job] = {}

    # ------------------------------------------------------------------ disk
    def load(self, lane_ids: list[str]) -> int:
        """At start: read every lane's job metas; a job that was running when the node stopped is
        lost (its processes went with the node). Returns how many were marked lost."""
        lost = 0
        for lane_id in lane_ids:
            d = self.paths.jobs(lane_id)
            if not d.is_dir():
                continue
            for meta in d.glob(f"{JOB_ID_PREFIX}*.json"):
                try:
                    info = JobInfo.model_validate_json(meta.read_text())
                except (OSError, ValueError):
                    continue
                job = Job(info, d)
                if info.state == "running":
                    info.state = "lost"
                    info.note = "the node stopped while it ran"
                    info.finished_at = info.finished_at or utcnow()
                    job.save()
                    lost += 1
                self.jobs[info.id] = job
        return lost

    def forget_lane(self, lane_id: str) -> None:
        for jid in [j for j, job in self.jobs.items() if job.info.lane == lane_id]:
            self.jobs.pop(jid, None)

    def prune(self, lane_id: str) -> None:
        done = sorted((j for j in self.jobs.values() if j.info.lane == lane_id and not j.running),
                      key=lambda j: j.info.started_at, reverse=True)
        for job in done[self.settings.jobs_keep_per_lane:]:
            for p in (job.meta_path(), job.log_path("stdout"), job.log_path("stderr")):
                p.unlink(missing_ok=True)
            self.jobs.pop(job.info.id, None)

    # ------------------------------------------------------------------ queries
    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id) if valid_job_id(job_id) else None
        if job is None:
            raise NotFound(f"no job {job_id} on this node (finished jobs are kept {self.settings.jobs_keep_per_lane} "
                           "per lane; lane_jobs_list shows them)")
        return job

    def running_in(self, lane_id: str) -> list[Job]:
        return [j for j in self.jobs.values() if j.info.lane == lane_id and j.running]

    def infos(self, lane_id: str | None = None) -> list[JobInfo]:
        jobs = [j.info for j in self.jobs.values() if lane_id is None or j.info.lane == lane_id]
        return sorted(jobs, key=lambda i: i.started_at, reverse=True)

    def check_room(self, lane_id: str) -> None:
        if len(self.running_in(lane_id)) >= self.settings.max_jobs_per_lane:
            raise LaneBusy(f"lane {lane_id} already runs {self.settings.max_jobs_per_lane} jobs; wait for one, or "
                           "cancel it with lane_job_cancel")
        if sum(1 for j in self.jobs.values() if j.running) >= self.settings.max_jobs:
            raise LaneBusy(f"this node already runs {self.settings.max_jobs} jobs; wait for one to finish")

    # ------------------------------------------------------------------ run
    async def start(self, rt: LaneRuntime, argv: list[str], *, info: JobInfo, env: dict[str, str] | None,
                    on_end) -> Job:
        """Spawn the workload and its log pumps; returns at once. `on_end(job)` runs after it ended."""
        d = self.paths.jobs(info.lane)
        d.mkdir(parents=True, exist_ok=True)
        job = Job(info, d)
        cap = self.settings.job_log_max_bytes
        job.out, job.err = LogSink(job.log_path("stdout"), cap), LogSink(job.log_path("stderr"), cap)
        wl = Workload(new_wid("j"), argv, rt.base_env(env))
        try:
            job.sp = await rt.spawn(wl, kind="job", stdin=asyncio.subprocess.DEVNULL,
                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except BaseException:
            job.out.close()
            job.err.close()
            raise
        job.rt = rt
        job.save()
        self.jobs[info.id] = job
        job.task = asyncio.create_task(self._run(job, on_end), name=f"env-job-{info.id}")
        return job

    async def _pump(self, job: Job, stream: asyncio.StreamReader | None, sink: LogSink) -> None:
        if stream is None:
            return
        while True:
            data = await stream.read(65536)
            if not data:
                return
            sink.feed(data)
            await job.notify()

    async def _run(self, job: Job, on_end) -> None:
        rt, sp = job.rt, job.sp
        assert rt is not None and sp is not None and job.out is not None and job.err is not None
        loop = asyncio.get_running_loop()
        start = loop.time()
        oom_before = rt.stat().get("oom_kills", 0)
        proc = sp.proc
        readers = asyncio.gather(self._pump(job, proc.stdout, job.out), self._pump(job, proc.stderr, job.err))
        timed_out = False
        try:
            try:
                await wait_exit(proc, job.info.timeout_s)
            except TimeoutError:
                timed_out = True
                await rt.terminate(sp, grace=CANCEL_GRACE_S)
            try:  # background children may hold the pipes open: a moment, then end the workload
                await asyncio.wait_for(asyncio.shield(readers), LEFTOVER_GRACE_S)
            except TimeoutError:
                await rt.kill(sp)
                try:
                    await asyncio.wait_for(readers, 2)
                except TimeoutError:
                    readers.cancel()
        except asyncio.CancelledError:
            readers.cancel()
            raise
        finally:
            with contextlib.suppress(Exception):
                await rt.release(sp)
            info = job.info
            info.exit_code, info.signal = exit_status(rt, proc.returncode)
            with contextlib.suppress(Exception):
                info.oom_killed = rt.stat().get("oom_kills", 0) > oom_before
            info.duration_s = round(loop.time() - start, 3)
            info.finished_at = utcnow()
            info.stdout_bytes, info.stderr_bytes = job.out.total, job.err.total
            info.stdout_dropped, info.stderr_dropped = job.out.dropped, job.err.dropped
            if job.cancel_requested:
                info.state, info.note = "cancelled", "cancelled with lane_job_cancel"
            elif timed_out:
                info.state, info.note = "timed_out", f"stopped after its timeout of {int(info.timeout_s)}s"
            else:
                info.state = "exited"
                if job.stop_note:
                    info.note = job.stop_note
            job.out.close()
            job.err.close()
            job.out = job.err = None
            job.sp, job.rt = None, None
            with contextlib.suppress(Exception):
                job.save()
            await job.notify()
            with contextlib.suppress(Exception):
                await on_end(job)

    async def cancel(self, job: Job) -> None:
        if not job.running or job.sp is None or job.rt is None:
            return
        job.cancel_requested = True
        await job.rt.terminate(job.sp, grace=CANCEL_GRACE_S)
        if job.task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(job.task), 10)

    def mark_stopping(self, lane_id: str, note: str) -> None:
        for job in self.running_in(lane_id):
            job.stop_note = note

    async def wait_lane(self, lane_id: str, timeout: float = 10.0) -> None:
        """Let the jobs of a lane that is going cold write their meta (their workloads were killed)."""
        tasks = [j.task for j in self.jobs.values() if j.info.lane == lane_id and j.task and not j.task.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    # ------------------------------------------------------------------ read
    async def read(self, job: Job, stdout_offset: int, stderr_offset: int, *, wait_s: float,
                   max_bytes: int, until_exit: bool = False) -> tuple[bytes, bytes]:
        """Output after the given offsets, waiting up to `wait_s` for some while the job runs (with
        `until_exit`, for the job to end)."""
        max_bytes = max(1, min(int(max_bytes), READ_MAX_BYTES))
        stdout_offset, stderr_offset = max(0, int(stdout_offset)), max(0, int(stderr_offset))

        def ready() -> bool:
            if not job.running:
                return True
            return not until_exit and (job.kept("stdout") > stdout_offset or job.kept("stderr") > stderr_offset)

        if wait_s > 0 and not ready():
            async with job.changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(job.changed.wait_for(ready), wait_s)
        return (await asyncio.to_thread(_read_at, job.log_path("stdout"), stdout_offset, max_bytes),
                await asyncio.to_thread(_read_at, job.log_path("stderr"), stderr_offset, max_bytes))


def _read_at(path: Path, offset: int, n: int) -> bytes:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(n)
    except FileNotFoundError:
        return b""
