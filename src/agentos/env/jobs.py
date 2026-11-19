"""Background commands in a lane (terminal_job_start): start, read the output from any offset, cancel.

terminal_exec waits for its command and keeps 64 KB of output; a test suite runs for half an hour and
prints megabytes, and the person who started it wants to see it as it goes, or to close the laptop
and come back. A job is a workload like an exec, but its stdout and stderr go to two log files on
the node (`<lane dir>/logs/jobs/<id>.out` and `.err`, outside the lane), and a reader asks for what
came after the offset it already has, waiting up to a few seconds for more (a long poll). A job runs
until it exits, its timeout, a cancel, or the lane going cold, whoever is connected.

Each job's meta is a JSON file beside its logs, so a finished job can still be read after the node
restarts; one that was running then is marked `lost`. The newest `jobs_keep_per_lane` finished jobs
of a lane are kept.

On a VM (a lane with `remote`) a job does not live in the node's ssh connection: a restart or an upgrade
of the agentos service killed every dev server on every VM (field report 2026-11-16). It runs detached
there (agentos.vms.ssh.start_script), its output in files on the VM, and the node follows those files
over an ssh of its own, a "follower" it may drop and open again from the offsets it has. After each
follower ends the node asks how the job stands: still running (the connection dropped: follow again),
ended (read the rest, its exit status, then forget its files there), or gone (the machine restarted:
lost). On shutdown the node only drops its followers (`detach_all`) and keeps the meta `running`; when
it starts again it reattaches (`attach`) and the same job id and offsets read on. A cancel, the timeout
and a lane going cold end the job's whole process group on the VM as before (its pgid file).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentos.core.errors import LaneBusy, NotFound
from agentos.core.models import utcnow
from agentos.env import oom
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
OOM_EVERY_S = 10.0     # a running job on a VM: its kernel's oom kills are asked for at most this often
OUT_OF_REACH_S = 180.0  # a detached job whose machine does not answer this long: lost (it may still run there)
START_REACH_S = 20.0    # the same for the ssh that starts a job: it may never have started
DRAIN_TRIES = 2         # followers after a job ended, to read what the last one did not
ATTACH_WAIT_S = 30.0    # a cancel of a job that waits for its reattach after a restart


def new_job_id() -> str:
    return JOB_ID_PREFIX + secrets.token_hex(5)


def valid_job_id(job_id: str) -> bool:
    rest = job_id[len(JOB_ID_PREFIX):] if isinstance(job_id, str) and job_id.startswith(JOB_ID_PREFIX) else ""
    return len(rest) == 10 and all(c in "0123456789abcdef" for c in rest)


class LogSink:
    """One stream's log file: appended as output arrives, kept up to `cap` bytes, the rest counted."""

    def __init__(self, path: Path, cap: int, *, total: int = 0):
        """A sink that goes on with a log the node has already (a reattached job): what is on disk is kept,
        and `total` (what the command wrote, also past the cap) is at least that."""
        self.path = path
        self.cap = cap
        try:
            self.kept = path.stat().st_size
        except OSError:
            self.kept = 0
        self.total = max(total, self.kept)
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
    oom_asked: float | None = None          # loop time of the last oom probe (a job on a VM)
    oom_task: asyncio.Task | None = None    # the probe in flight
    signs: oom.Signs = field(default_factory=oom.Signs)  # its output showed a kill ("Killed", exit code 137)
    pending: bool = False                   # running on its VM, not yet reattached after the node restarted
    detaching: bool = False                 # the node stops: drop the follower, the job runs on
    attached: asyncio.Event = field(default_factory=asyncio.Event)  # reattached (or given up)

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

    def add_oom(self, kills: list[dict] | None) -> bool:
        """Keep the oom kills a probe found (agentos.env.oom). True when one of them is new."""
        if not kills:
            return False
        before = len(self.info.oom or [])
        self.info.oom = oom.merge(self.info.oom, kills)
        if any(k.get("in_job") for k in self.info.oom):
            self.info.oom_killed = True
        return len(self.info.oom) > before

    @property
    def on_vm(self) -> bool:
        return self.rt is not None and self.rt.lane.remote is not None

    @property
    def wid(self) -> str | None:
        """Its workload id: the name of its files on a VM."""
        return self.info.vm_wid or (self.sp.wid if self.sp is not None else None)

    async def pause(self, secs: float) -> None:
        """Sleep, but wake for a cancel, a lane stopping or the node stopping."""
        end = asyncio.get_running_loop().time() + secs
        while asyncio.get_running_loop().time() < end and not (self.cancel_requested or self.stop_note
                                                                 or self.detaching):
            await asyncio.sleep(0.2)


@dataclass
class _Follow:
    """Where following a detached job stands (JobTable._run_detached)."""
    first: bool                      # no answer from the machine yet since the ssh that started it
    deadline: float                  # epoch: its timeout
    timed_out: bool = False
    stopped: bool = False            # the node ended it (cancel, timeout, its lane stopping)
    rc: int | None = None
    ended_at: float | None = None
    lost: str | None = None
    drains: int = 0
    out_of_reach: float | None = None
    start_rc: int | None = None      # the exit status of the ssh that started it


class JobTable:
    """Every job this node knows: running ones in memory, finished ones read back from disk."""

    def __init__(self, paths: EnvPaths, settings: EnvSettings):
        self.paths = paths
        self.settings = settings
        self.jobs: dict[str, Job] = {}
        self.pending: list[Job] = []   # running on a VM when the node stopped: to reattach (`attach`)

    # ------------------------------------------------------------------ disk
    def load(self, lane_ids: list[str], remote: set[str] | frozenset[str] = frozenset()) -> int:
        """At start: read every lane's job metas; a job that was running when the node stopped is
        lost (its processes went with the node), unless it ran detached on a VM lane (`remote`): that
        one is pending, for the manager to reattach. Returns how many were marked lost."""
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
                if info.state == "running" and info.vm_wid and lane_id in remote:
                    job.pending = True
                    self.pending.append(job)
                elif info.state == "running":
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
                           "per terminal; terminal_jobs shows them)")
        return job

    def running_in(self, lane_id: str) -> list[Job]:
        return [j for j in self.jobs.values() if j.info.lane == lane_id and j.running]

    def infos(self, lane_id: str | None = None) -> list[JobInfo]:
        jobs = [j.info for j in self.jobs.values() if lane_id is None or j.info.lane == lane_id]
        return sorted(jobs, key=lambda i: i.started_at, reverse=True)

    def check_room(self, lane_id: str) -> None:
        if len(self.running_in(lane_id)) >= self.settings.max_jobs_per_lane:
            raise LaneBusy(f"lane {lane_id} already runs {self.settings.max_jobs_per_lane} jobs; wait for one, or "
                           "cancel it with terminal_job_cancel")
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
        wl = Workload(new_wid("j"), argv, rt.base_env(env), oom_record=True)
        detached = rt.detaches_jobs
        if detached:  # on a VM: it runs there on its own, the ssh only follows it (the module's docstring)
            info.vm_wid = wl.wid
            wl.detach, wl.detach_start, wl.detach_timeout_s = (0, 0), True, info.timeout_s
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
        run = self._run_detached(job, on_end, fresh=True) if detached else self._run(job, on_end)
        job.task = asyncio.create_task(run, name=f"env-job-{info.id}")
        job.attached.set()
        return job

    def attach(self, rt: LaneRuntime, job: Job, *, on_end) -> None:
        """After the node restarted: follow a pending job on its VM again, from the offsets on disk."""
        cap = self.settings.job_log_max_bytes
        job.out = LogSink(job.log_path("stdout"), cap, total=job.info.stdout_bytes)
        job.err = LogSink(job.log_path("stderr"), cap, total=job.info.stderr_bytes)
        job.rt, job.pending = rt, False
        if job in self.pending:
            self.pending.remove(job)
        job.task = asyncio.create_task(self._run_detached(job, on_end, fresh=False), name=f"env-job-{job.info.id}")
        job.attached.set()

    def give_up(self, job: Job, note: str) -> None:
        """A pending job that cannot be reattached: lost, with why."""
        info = job.info
        info.state, info.note = "lost", note
        info.finished_at = info.finished_at or utcnow()
        job.pending = False
        if job in self.pending:
            self.pending.remove(job)
        with contextlib.suppress(Exception):
            job.save()
        job.attached.set()

    async def detach_all(self, timeout: float = 5.0) -> int:
        """The node stops: drop the follower of every detached job and keep it `running` in its meta, so
        the next start reattaches; the jobs run on on their VMs. Returns how many."""
        jobs = [j for j in self.jobs.values() if j.running and j.info.vm_wid and j.task and not j.task.done()]
        for job in jobs:
            job.detaching = True
            if job.sp is not None:
                _kill_local(job.sp)
        if jobs:
            _, late = await asyncio.wait([j.task for j in jobs if j.task], timeout=timeout)
            for t in late:
                t.cancel()
            if late:
                await asyncio.wait(late, timeout=2)
        return len(jobs)

    async def _follow(self, job: Job, rt: LaneRuntime) -> Spawned:
        wl = Workload(job.info.vm_wid or new_wid("j"), ["true"], {}, detach=(job.out.total, job.err.total))
        return await rt.spawn(wl, kind="job", stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                              stderr=asyncio.subprocess.PIPE)

    async def _run_detached(self, job: Job, on_end, *, fresh: bool) -> None:
        """A job on a VM (the module's docstring): follow it until it ended there, through dropped
        connections; then its exit status, what the kernel killed, and its files there go."""
        rt = job.rt
        assert rt is not None and job.out is not None and job.err is not None
        st = _Follow(first=fresh, deadline=job.info.started_at.timestamp() + job.info.timeout_s)
        gone = False
        try:
            while not job.detaching:
                if (job.cancel_requested or job.stop_note) and job.sp is None and not st.stopped:
                    st.stopped = True  # nobody ended it through a follower of this node: by its pgid file
                    await rt.stop_detached(job.info.vm_wid or "", CANCEL_GRACE_S)
                began, follower_rc = await self._follow_once(job, rt, st)
                if st.first and st.start_rc is None:
                    st.start_rc = follower_rc
                if job.detaching:
                    break
                if job.cancel_requested or job.stop_note:
                    st.stopped = True
                if await self._judge(job, rt, st, await rt.job_status(job.info.vm_wid or ""), began):
                    break
            gone = job.detaching
        except asyncio.CancelledError:
            gone = True
            raise
        finally:
            if gone or job.detaching:
                self._keep_running(job)
            else:
                await self._end_detached(job, rt, on_end, rc=st.rc, ended_at=st.ended_at, lost=st.lost,
                                         timed_out=st.timed_out, stopped=st.stopped)

    async def _follow_once(self, job: Job, rt: LaneRuntime, st: _Follow) -> tuple[float, int | None]:
        """One follower: its output into the logs until it ends (the job ended, or the connection dropped);
        the timeout ends the job. Returns when it began and its exit status (None: no ssh started)."""
        loop = asyncio.get_running_loop()
        began = loop.time()
        if job.sp is None:
            try:
                job.sp = await self._follow(job, rt)
            except Exception as e:  # noqa: BLE001 (no ssh to start: the same as no answer)
                log.debug("job %s: no follower: %s", job.info.id, e)
                return began, None
        sp = job.sp
        proc = sp.proc
        assert job.out is not None and job.err is not None
        readers = asyncio.gather(self._pump(job, proc.stdout, job.out), self._pump(job, proc.stderr, job.err))
        try:
            try:
                await wait_exit(proc, None if st.timed_out else max(0.0, st.deadline - time.time()))
            except TimeoutError:
                st.timed_out = st.stopped = True
                await rt.terminate(sp, grace=CANCEL_GRACE_S)
            try:  # the follower ended: its pipes close with it
                await asyncio.wait_for(asyncio.shield(readers), 5)
            except TimeoutError:
                readers.cancel()
        except asyncio.CancelledError:
            readers.cancel()
            raise
        finally:
            _kill_local(sp)
            with contextlib.suppress(Exception):
                await rt.release(sp)
            job.sp = None
        return began, proc.returncode

    async def _judge(self, job: Job, rt: LaneRuntime, st: _Follow, status: dict | None, began: float) -> bool:
        """After a follower: how the job stands on its VM (status_script). True when it is over for this
        node (ended, gone, lost or out of reach while it is being ended); False to follow it again."""
        loop = asyncio.get_running_loop()
        assert job.out is not None and job.err is not None
        if status is None:  # the machine did not answer
            if st.stopped:
                return True
            st.out_of_reach = st.out_of_reach or loop.time()
            waited = loop.time() - st.out_of_reach
            if st.first and waited > START_REACH_S:
                st.lost = "the node could not reach the machine over ssh to start it"
                return True
            if waited > OUT_OF_REACH_S:
                st.lost = f"the node could not reach the machine for {int(OUT_OF_REACH_S)} s; the job may still run there"
                return True
            st.timed_out = st.timed_out or time.time() > st.deadline
            await job.pause(min(10.0, 1.0 + waited / 6))
            return False
        st.out_of_reach = None
        if status["gone"]:
            if st.first:  # it never started (its cwd, say): what the start printed and its exit status
                st.rc = st.start_rc
            else:
                st.lost = "its files are gone from the machine (it restarted, or was rebuilt)"
            return True
        st.first = False
        if status["alive"]:
            if time.time() > st.deadline and not st.timed_out:
                st.timed_out = st.stopped = True
                await rt.stop_detached(job.info.vm_wid or "", CANCEL_GRACE_S)
            elif loop.time() - began < 2:
                await job.pause(1.0)  # a follower that ends at once is no reason to spin
            return False
        if (status["out"] > job.out.total or status["err"] > job.err.total) and st.drains < DRAIN_TRIES:
            st.drains += 1  # it ended, and the last follower did not bring all of it
            return False
        st.rc, st.ended_at = status["rc"], status["ended_at"]
        st.timed_out = st.timed_out or status["timed_out"]
        return True

    def _keep_running(self, job: Job) -> None:
        """The node stops while the job runs on its VM: its meta stays `running`, with what was read."""
        if job.sp is not None:
            _kill_local(job.sp)
            job.sp = None
        info = job.info
        if job.out is not None and job.err is not None:
            info.stdout_bytes, info.stderr_bytes = job.out.total, job.err.total
            job.out.close()
            job.err.close()
        job.out = job.err = None
        job.rt = None
        job.pending = True
        with contextlib.suppress(Exception):
            job.save()

    async def _end_detached(self, job: Job, rt: LaneRuntime, on_end, *, rc: int | None, ended_at: float | None,
                            lost: str | None, timed_out: bool, stopped: bool) -> None:
        info = job.info
        assert job.out is not None and job.err is not None
        if rc is not None:
            info.exit_code, info.signal = exit_status(rt, rc)
        elif stopped and not lost:
            info.exit_code, info.signal = None, "SIGTERM"
        end = ended_at if ended_at and ended_at >= info.started_at.timestamp() else time.time()
        info.duration_s = round(end - info.started_at.timestamp(), 3)
        if not lost and not job.stop_note and (info.exit_code != 0 or info.signal or job.signs.seen):
            with contextlib.suppress(Exception):
                job.add_oom(await asyncio.wait_for(
                    rt.oom_kills(info.vm_wid or "", elapsed_s=info.duration_s, final=True), oom.PROBE_TIMEOUT_S + 2))
        if not lost:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(rt.job_forget(info.vm_wid or ""), 25)
        info.finished_at = utcnow()
        info.stdout_bytes, info.stderr_bytes = job.out.total, job.err.total
        info.stdout_dropped, info.stderr_dropped = job.out.dropped, job.err.dropped
        if lost:
            info.state, info.note = "lost", lost
        elif job.cancel_requested:
            info.state, info.note = "cancelled", "cancelled with terminal_job_cancel"
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

    async def _pump(self, job: Job, stream: asyncio.StreamReader | None, sink: LogSink) -> None:
        if stream is None:
            return
        while True:
            data = await stream.read(65536)
            if not data:
                return
            sink.feed(data)
            job.signs.feed(data)
            await job.notify()

    async def _run(self, job: Job, on_end) -> None:
        rt, sp = job.rt, job.sp
        assert rt is not None and sp is not None and job.out is not None and job.err is not None
        loop = asyncio.get_running_loop()
        start = loop.time()
        gone = False  # the node is stopping: nothing more is asked of the VM
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
            gone = True
            raise
        finally:
            with contextlib.suppress(Exception):
                await rt.release(sp)
            info = job.info
            info.exit_code, info.signal = exit_status(rt, proc.returncode)
            with contextlib.suppress(Exception):
                info.oom_killed = rt.stat().get("oom_kills", 0) > oom_before
            info.duration_s = round(loop.time() - start, 3)
            # what the kernel killed, before the read that reports the end returns (the state is set below),
            # when the end or the output shows a kill; and the VM's start record goes
            if not gone and not job.stop_note and job.on_vm and (info.exit_code != 0 or info.signal or job.signs.seen):
                with contextlib.suppress(Exception):
                    job.add_oom(await asyncio.wait_for(
                        rt.oom_kills(sp.wid, elapsed_s=info.duration_s, final=True), oom.PROBE_TIMEOUT_S + 2))
            info.finished_at = utcnow()
            info.stdout_bytes, info.stderr_bytes = job.out.total, job.err.total
            info.stdout_dropped, info.stderr_dropped = job.out.dropped, job.err.dropped
            if job.cancel_requested:
                info.state, info.note = "cancelled", "cancelled with terminal_job_cancel"
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
        if not job.running:
            return
        if job.pending:  # the node just restarted: its reattach ends it (Job.cancel_requested)
            job.cancel_requested = True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(job.attached.wait(), ATTACH_WAIT_S)
        if job.task is None or job.rt is None:
            return
        job.cancel_requested = True
        if job.sp is not None:
            await job.rt.terminate(job.sp, grace=CANCEL_GRACE_S)
        if job.task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(job.task), 30 if job.info.vm_wid else 10)

    def probe_oom(self, job: Job) -> asyncio.Task | None:
        """A running job on a VM: ask its kernel for oom kills in the background, at most every
        OOM_EVERY_S. Returns the probe in flight (its result is True when it found a new kill)."""
        if not job.running or not job.on_vm:
            return None
        if job.oom_task is not None and not job.oom_task.done():
            return job.oom_task
        now = asyncio.get_running_loop().time()
        if job.oom_asked is not None and now - job.oom_asked < OOM_EVERY_S:
            return None
        rt, wid = job.rt, job.wid
        if rt is None or not wid:
            return None
        job.oom_asked = now

        async def ask() -> bool:
            elapsed = (utcnow() - job.info.started_at).total_seconds()
            found = job.add_oom(await rt.oom_kills(wid, elapsed_s=elapsed))
            if found:
                with contextlib.suppress(Exception):
                    job.save()
                await job.notify()
            return found

        job.oom_task = asyncio.create_task(ask(), name=f"env-job-oom-{job.info.id}")
        return job.oom_task

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
        `until_exit`, for the job to end). On a VM the wait also ends when the kernel killed one of
        its processes for memory (agentos.env.oom): the job may never print again."""
        max_bytes = max(1, min(int(max_bytes), READ_MAX_BYTES))
        stdout_offset, stderr_offset = max(0, int(stdout_offset)), max(0, int(stderr_offset))
        kills = len(job.info.oom or [])

        def ready() -> bool:
            if not job.running or len(job.info.oom or []) > kills:
                return True
            return not until_exit and (job.kept("stdout") > stdout_offset or job.kept("stderr") > stderr_offset)

        self.probe_oom(job)
        if wait_s > 0 and not ready():
            loop = asyncio.get_running_loop()
            end = loop.time() + wait_s
            async with job.changed:
                while not ready() and (left := end - loop.time()) > 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(job.changed.wait_for(ready), min(left, OOM_EVERY_S))
                    self.probe_oom(job)
        if job.running and (job.info.exit_code is not None or job.info.signal) and job.task is not None:
            # it is ending (an oom kill found at its end can wake this read first): let the end finish,
            # so the read says exited, not running with an exit code (seen live on Hetzner, 2026-11-16)
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(job.task), 30)
        out = await asyncio.to_thread(_read_at, job.log_path("stdout"), stdout_offset, max_bytes)
        err = await asyncio.to_thread(_read_at, job.log_path("stderr"), stderr_offset, max_bytes)
        if oom.signs(err) and len(job.info.oom or []) == kills:
            await self._ask_oom_now(job)  # the shell says a process was killed: say why in this read
        return out, err

    async def _ask_oom_now(self, job: Job) -> None:
        """Ask the kernel now, whenever it was last asked, and wait for the answer (a few seconds
        at most), so the read that shows a shell's "Killed" line also says what killed it."""
        if job.oom_task is None or job.oom_task.done():
            job.oom_asked = None
        task = self.probe_oom(job)
        if task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), oom.PROBE_TIMEOUT_S + 2)


def _read_at(path: Path, offset: int, n: int) -> bytes:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(n)
    except FileNotFoundError:
        return b""


def _kill_local(sp: Spawned) -> None:
    """End the node's own ssh of a workload (its follower), not what it runs on the VM."""
    if sp.proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(sp.proc.pid, signal.SIGKILL)
