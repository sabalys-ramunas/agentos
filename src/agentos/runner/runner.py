"""The durable runner: a SQLite-backed run queue with a step journal.

- One run per lane at a time, a per-node limit, and a fair queue across workspaces (a busy
  workspace never starves another).
- Transient failures retry with exponential backoff and jitter; PolicyDenied never retries.
- Every run carries the grant that created it (`grant_id`). With a policy service, the grant
  is checked when the run is queued and again right before it runs: a run whose grant was
  revoked or expired fails (grant_revoked, grant_expired) and never acts. A run nobody
  authorized yet (schedules, webhooks, the panel API) is authorized at dispatch under that
  grant, for the flow's effective effect.
- ApprovalPending parks a run in waiting_approval; it resumes when the approvals service
  (polled through the runtime, or pushed via on_approval) says approved, and consumes the
  approval: one approval releases exactly one run, a second run parked on it asks again.
- A human holding the lane (take over, stop agent) defers the run without counting toward
  giving up; hand back (a lane control listener) makes it due at once.
- A crash leaves the journal intact. recover() requeues interrupted runs, or stops them as
  partial with a decision request when an in-flight step is not safe to repeat.
- succeeded_unverified runs with re-checkable readbacks are re-checked later.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agentos.core.driver import DriverFactory, FlowDriver
from agentos.core.errors import (
    LaneBusy,
    LaneQuarantined,
    NotFound,
    ServiceUnavailable,
)
from agentos.core.models import RunKind, RunStatus
from agentos.runner import signed_out
from agentos.runner.approvals import ApprovalMixin
from agentos.runner.dispatch import DispatchMixin
from agentos.runner.execution import Outcome
from agentos.runner.executor import FlowExecutor
from agentos.runner.flows import FlowService
from agentos.runner.gate import GateMixin
from agentos.runner.memory_summary import write_summary
from agentos.runner.proof import ResolvedCheck, run_check
from agentos.runner.records import Run, StepStatus
from agentos.runner.run_rules import RunExecutor, with_fix
from agentos.runner.steps import is_idempotent, step_at
from agentos.runner.store import RunnerStore
from agentos.runner.submit import SubmitMixin
from agentos.runner.util import Clock, Scrubber, clip

log = logging.getLogger("agentos.runner")

class RunnerService(SubmitMixin, ApprovalMixin, GateMixin, DispatchMixin):
    """The runner. Here: wiring, reads, re-checks and the lifecycle. Queueing is in submit.py,
    approvals in approvals.py, the gate before a run acts in gate.py, dispatch in dispatch.py."""

    def __init__(self, store: RunnerStore, flows: FlowService, *, runtime: Any = None,
                 driver_factory: DriverFactory | None = None, clock: Clock | None = None,
                 rng: random.Random | None = None, max_concurrent: int = 2, max_attempts: int = 3,
                 backoff_base_s: float = 30.0, backoff_max_s: float = 1800.0, max_deferrals: int = 40,
                 approval_timeout_s: float = 7 * 86_400, recheck_delays_s: tuple[float, ...] = (300, 1800, 7200),
                 shots_dir: Path | None = None, ref_base: Path | None = None, poll_s: float = 0.5,
                 dispatch_interval_s: float = 1.0, approvals_interval_s: float = 15.0):
        self.store, self.flows, self.runtime = store, flows, runtime
        self.clock = clock or Clock()
        self.rng = rng or random.Random()
        self._driver_factory = driver_factory
        self.max_concurrent, self.max_attempts = max_concurrent, max_attempts
        self.backoff_base_s, self.backoff_max_s, self.max_deferrals = backoff_base_s, backoff_max_s, max_deferrals
        self.approval_timeout_s = approval_timeout_s
        self.recheck_delays_s = recheck_delays_s
        self.dispatch_interval_s, self.approvals_interval_s = dispatch_interval_s, approvals_interval_s
        self.executors: dict[RunKind, RunExecutor] = {
            RunKind.FLOW: FlowExecutor(flows, self.driver_factory, clock=self.clock, rng=self.rng,
                                       shots_dir=shots_dir, ref_base=ref_base, poll_s=poll_s),
        }
        self._active: dict[str, asyncio.Task] = {}
        self._lanes: set[str] = set()
        self._cancel: set[str] = set()
        self._served: dict[str, int] = {}
        self._twin_locks: dict[tuple, asyncio.Lock] = {}  # one identical keyless call at a time (open_twin)
        self._serial = 0
        self._wake = asyncio.Event()
        self._changed: dict[str, list[asyncio.Event]] = {}  # waiters per run id, only while waiting
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._loops: list[asyncio.Task] = []
        self._stopping = False
        self.on_finished: list[Callable[[Run], Any]] = []
        self.scheduler: Any = None  # agentos.runner.schedules.Scheduler, attached by build()
        self.surfaces: Any = None  # agentos.runner.connectors.surfaces.SurfaceService, attached by build()
        self.site_apis: Any = None  # agentos.runner.siteapis.service.SiteApiService, attached by build()
        self.health: Any = None  # agentos.runner.connectors.health.ConnectorHealth, attached by build()
        self._background: set[asyncio.Task] = set()
        self._control_listener = False

    # ------------------------------------------------------------ wiring

    def driver_factory(self) -> DriverFactory | None:
        if self._driver_factory is not None:
            return self._driver_factory
        return self.runtime.get("flow_driver") if self.runtime is not None else None

    def set_driver_factory(self, factory: DriverFactory) -> None:
        self._driver_factory = factory

    def register_executor(self, kind: RunKind | str, executor: RunExecutor) -> None:
        self.executors[RunKind(kind)] = executor

    def subscribe(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """callback("run", public_run_dict) on every status change (SSE, traces)."""
        self._listeners.append(callback)

    def _emit(self, run: Run) -> None:
        for ev in self._changed.get(run.id, ()):
            ev.set()
        payload = self.describe(run)
        for cb in list(self._listeners):
            try:
                cb("run", payload)
            except Exception:
                log.exception("run listener failed")
        if run.terminal:
            write_summary(self.runtime, run, self.flows)  # memory: one run summary per finished run
            for cb in list(self.on_finished):
                try:
                    cb(run)
                except Exception:
                    log.exception("run finished hook failed")

    def _service(self, name: str) -> Any:
        return self.runtime.get(name) if self.runtime is not None else None

    # ------------------------------------------------------------ submit and read


    def get(self, run_id: str) -> Run:
        run = self.store.get_run(run_id)
        if run is None:
            raise NotFound(f"run {run_id!r} not found")
        return run

    def list(self, **filters: Any) -> list[Run]:
        return self.store.list_runs(**filters)

    def lane_run_times(self, lane_id: str, since: datetime) -> list[tuple[datetime, str]]:
        """(created_at, status) of the runs on a lane since a moment, for the lane's history."""
        return self.store.lane_run_times(lane_id, since)

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        out = []
        for rec in self.store.steps(run_id):
            item: dict[str, Any] = {"path": rec.path, "type": rec.type, "status": str(rec.status),
                                    "attempts": rec.attempts,
                                    "started_at": rec.started_at.isoformat() if rec.started_at else None,
                                    "finished_at": rec.finished_at.isoformat() if rec.finished_at else None}
            res = rec.result or {}
            if res.get("readback"):
                rb = res["readback"]
                item["readback"] = {"passed": rb.get("passed"), "expected": rb.get("expected"),
                                    "found": rb.get("found"), "note": rb.get("note")}
            if res.get("taken"):
                item["taken"] = res["taken"]
            if rec.error:
                item["error"] = rec.error
            out.append(item)
        return clip(out, max_str=300, max_items=200)

    def describe(self, run: Run, *, steps: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": run.id, "kind": str(run.kind), "status": str(run.status), "lane": run.lane,
            **({"surface": run.surface} if run.surface else {}),
            "flow": run.flow_id, "version": run.flow_version, "params": self._shown_params(run),
            "attempts": run.attempts, "source": run.source, "schedule_id": run.schedule_id,
            "parent": run.parent, "idempotency_key": run.idempotency_key,
            "created_at": run.created_at.isoformat(),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "not_before": run.not_before.isoformat() if run.not_before and not run.terminal else None,
            "proof": clip(run.proof, max_str=300) if run.proof else None,
            "error": with_fix(clip(run.error, max_str=500)) if run.error else None,
            **({"output": run.output} if run.output is not None else {}),  # capped and typed by the executor
            **({"from_outside": origins[0].get("kind")} if (origins := (run.trigger or {}).get("origins")) else {}),
            "cost": run.cost, "approval_id": run.approval_id,
            "decision_needed": run.decision.get("needed") if run.needs_decision else None,
            "recheck_at": run.recheck_at.isoformat() if run.recheck_at else None,
            "cancel_requested": run.cancel_requested,
            "principal": {k: run.principal.get(k) for k in ("kind", "id", "thread") if run.principal.get(k)},
            "grant_id": run.grant_id,
            "summary": self._summary(run),
        }
        if steps:
            d["steps"] = self.steps(run.id)
        return d

    def _shown_params(self, run: Run) -> dict[str, Any]:
        """A run's params as shown: values the flow does not mark `show` read "(hidden)"."""
        if run.kind == RunKind.WORKFLOW:
            return {}  # what arrived: an email, a webhook. Its steps show it, marked and clipped
        if run.kind != RunKind.FLOW:
            return clip(run.params, max_str=200)
        shown = self.flows.shown_params_of(run.flow_id, run.flow_version)
        return clip({k: v if k in shown else "(hidden)" for k, v in run.params.items()}, max_str=200)

    @staticmethod
    def _summary(run: Run) -> str:
        s = run.status
        rbs = (run.proof or {}).get("readbacks") or []
        passed = sum(1 for rb in rbs if rb.get("passed") is True)
        if s == RunStatus.SUCCEEDED_VERIFIED:
            return f"done and verified: {passed} readback(s) of the target's own state passed"
        if s == RunStatus.SUCCEEDED_UNVERIFIED:
            note = (run.proof or {}).get("note") or "a readback could not be checked"
            return f"actions reported ok, not verified: {note}"
        if s == RunStatus.WAITING_APPROVAL:
            return "waiting for the owner to approve"
        if s == RunStatus.QUEUED:
            if run.error and run.error.get("code") == "deferred":
                return f"queued, deferred: {run.error.get('message')}"
            return "queued" + (" (retrying after a transient error)" if run.error else "")
        if s == RunStatus.RUNNING:
            return "running"
        if s == RunStatus.PARTIAL and run.needs_decision:
            return "partial: needs your decision (retry, skip or fail)"
        msg = (run.error or {}).get("message", "")
        return f"{s}: {msg}" if msg else str(s)

    async def wait(self, run_id: str, timeout: float, *, stop: Callable[[Run], bool] | None = None) -> Run:
        """Wait until the run leaves queued/running, or `stop(run)` is true (a re-queued run whose
        error the caller treats as final), or timeout; returns the latest state."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            run = self.get(run_id)
            if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING) or (stop is not None and stop(run)):
                return run
            remaining = deadline - loop.time()
            if remaining <= 0:
                return run
            ev = asyncio.Event()
            waiters = self._changed.setdefault(run_id, [])
            waiters.append(ev)
            try:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(ev.wait(), min(remaining, 0.5))
            finally:
                waiters.remove(ev)
                if not waiters and self._changed.get(run_id) is waiters:
                    del self._changed[run_id]

    # ------------------------------------------------------------ human actions

    # ------------------------------------------------------------ dispatch

    # ------------------------------------------------------------ re-checks

    async def recheck_due(self) -> list[asyncio.Task]:
        if self._stopping:
            return []
        started = []
        now = self.clock.now()
        for run in self.store.list_runs(status=RunStatus.SUCCEEDED_UNVERIFIED, recheck_before=now, limit=50):
            key = f"recheck:{run.id}"
            if key in self._active or len(self._active) >= self.max_concurrent:
                continue
            if run.lane and run.lane in self._lanes:
                continue
            if run.lane:
                self._lanes.add(run.lane)
            task = asyncio.create_task(self._recheck(run), name=key)
            self._active[key] = task
            started.append(task)
        return started

    async def _recheck(self, run: Run) -> None:
        try:
            await self._recheck_inner(run)
        except Exception:
            log.exception("re-check of %s failed", run.id)
        finally:
            self._active.pop(f"recheck:{run.id}", None)
            if run.lane:
                self._lanes.discard(run.lane)

    def _scrubber(self, run: Run) -> Any:
        if run.kind != RunKind.FLOW:
            return Scrubber(extra=self.flows.vault_scrub)
        try:
            return self.flows.scrubber(self.flows.get(run.flow_id or "", run.flow_version), run.params)
        except NotFound:
            return Scrubber(run.params, extra=self.flows.vault_scrub)  # the flow is gone: hide every param

    async def _recheck_inner(self, run: Run) -> None:
        proof = dict(run.proof or {})
        rbs = list(proof.get("readbacks") or [])
        unknown = [i for i, rb in enumerate(rbs) if rb.get("passed") is None]
        private = self.store.step_states(run.id)  # the resolved checks, values included
        public = private.get(signed_out.CHECK_PATH) if not signed_out.passed(proof.get("public")) else None
        factory = self.driver_factory()
        now = self.clock.now()
        if (unknown or public) and factory is not None:
            scrub = self._scrubber(run)
            try:
                async with factory(run.lane or "") as driver:
                    for i in unknown:
                        raw = (private.get(rbs[i].get("path") or "") or {}).get("check") or rbs[i]["check"]
                        check = ResolvedCheck.model_validate(raw)
                        res = await run_check(driver, check, self.clock)
                        rbs[i] = {**scrub(clip(res.model_dump(mode="json", exclude={"check"}), max_str=500)),
                                  "check": rbs[i]["check"], "check_text": scrub(check.describe()), "rechecked": True}
                    if public:  # AUD-22: is it visible signed out by now?
                        proof["public"] = {**scrub(await signed_out.run(driver, public, self.clock)), "rechecked": True}
            except (LaneBusy, LaneQuarantined):
                pass
            except Exception:
                log.exception("re-check could not open lane %s", run.lane)
        proof["readbacks"] = rbs
        run.rechecks += 1
        if any(rb.get("passed") is False for rb in rbs):
            run.status = RunStatus.PARTIAL if proof.get("writes") or proof.get("acted") else RunStatus.FAILED
            run.error = {"code": "readback_failed", "message": "a later re-check found the target's state "
                         "does not show the effect"}
            run.recheck_at = None
        elif all(rb.get("passed") is True for rb in rbs) and signed_out.passed(proof.get("public")):
            run.status = RunStatus.SUCCEEDED_VERIFIED
            proof["note"] = f"verified by a re-check at {now.isoformat()}"
            run.recheck_at = None
        elif run.rechecks < len(self.recheck_delays_s):
            run.recheck_at = now + timedelta(seconds=self.recheck_delays_s[run.rechecks])
        else:
            run.recheck_at = None
            why = "" if signed_out.passed(proof.get("public")) else f": {signed_out.reason(proof['public'])}"
            proof["note"] = f"still unverified after {run.rechecks} re-checks{why}"
        run.proof = proof
        if self.store.save_run(run, expect={RunStatus.SUCCEEDED_UNVERIFIED}):
            self._emit(run)

    # ------------------------------------------------------------ lane sessions (watches)

    @contextlib.asynccontextmanager
    async def lane_session(self, lane: str) -> AsyncIterator[FlowDriver]:
        """Borrow a lane for a short read (page watches), exclusive with runs."""
        if lane in self._lanes:
            raise LaneBusy(f"lane {lane} is running a flow")
        factory = self.driver_factory()
        if factory is None:
            raise ServiceUnavailable("no flow driver is configured on this node")
        self._lanes.add(lane)
        try:
            async with factory(lane) as driver:
                yield driver
        finally:
            self._lanes.discard(lane)
            self._wake.set()

    # ------------------------------------------------------------ restart and lifecycle

    def recover(self) -> dict[str, int]:
        """After a restart: requeue runs that were running, unless an in-flight step is not
        safe to repeat, in which case the run stops as partial and asks for a decision."""
        stats = {"requeued": 0, "needs_decision": 0}
        for run in self.store.list_runs(status=RunStatus.RUNNING, limit=10_000):
            if run.id in self._active:
                continue
            inflight = [r for r in self.store.steps(run.id) if r.status == StepStatus.STARTED]
            unsafe = None
            if run.kind == RunKind.FLOW and inflight:
                try:
                    steps = self.flows.get(run.flow_id or "", run.flow_version).definition.steps
                    for rec in inflight:
                        step = step_at(steps, rec.path)
                        if not is_idempotent(step):
                            unsafe = (rec.path, step.type)
                            break
                except (NotFound, KeyError, IndexError):
                    unsafe = (inflight[0].path, inflight[0].type)
            if unsafe:
                outcome = FlowExecutor.in_flight_outcome(*unsafe)
                self._apply_outcome(run.id, outcome, self.clock.monotonic())
                stats["needs_decision"] += 1
            elif run.attempts >= self.max_attempts + 2:
                # the node keeps dying during this run: stop instead of crash-looping
                self._apply_outcome(run.id, Outcome("finish", RunStatus.FAILED, error={
                    "code": "crash_loop", "message": f"the node stopped during this run {run.attempts} times"}),
                    self.clock.monotonic())
            else:
                run.status = RunStatus.QUEUED
                run.not_before = None
                if self.store.save_run(run, expect={RunStatus.RUNNING}):
                    stats["requeued"] += 1
                    self._emit(run)
        return stats

    async def run_until_idle(self, *, rechecks: bool = False, max_rounds: int = 500) -> None:
        """Dispatch and wait until nothing is due (for tests and the CLI)."""
        for _ in range(max_rounds):
            started = await self.dispatch_once()
            if rechecks:
                started += await self.recheck_due()
            pending = list(self._active.values())
            if not started and not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def start(self) -> None:
        """Start hook for the daemon: recover, then dispatch, poll approvals and tick schedules."""
        self._stopping = False
        self.recover()
        self.listen_for_lane_control()
        self._loops = [
            asyncio.create_task(self._loop(self._dispatch_tick, self.dispatch_interval_s, wake=True), name="runner"),
            asyncio.create_task(self._loop(self.poll_approvals, self.approvals_interval_s), name="runner-approvals"),
        ]
        if self.scheduler is not None:
            await self.scheduler.start()

    async def _dispatch_tick(self) -> None:
        await self.dispatch_once()
        await self.recheck_due()

    async def _loop(self, fn: Callable[[], Any], interval: float, *, wake: bool = False) -> None:
        while not self._stopping:
            try:
                await fn()
            except Exception:
                log.exception("runner loop failed")
            if wake:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), interval)
                self._wake.clear()
            else:
                await asyncio.sleep(interval)

    async def stop(self, timeout: float = 30.0) -> None:
        """Stop dispatching; running flows pause at their next step boundary and requeue."""
        self._stopping = True
        if self.scheduler is not None:
            await self.scheduler.stop()
        for t in self._loops:
            t.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        self._loops = []
        pending = list(self._active.values())
        if pending:
            _, still = await asyncio.wait(pending, timeout=timeout)
            for t in still:
                t.cancel()  # like a crash: the journal decides on restart
            await asyncio.gather(*still, return_exceptions=True)

