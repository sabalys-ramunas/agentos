"""Dispatch: picking due runs fairly across workspaces, one per lane, holding a run while a
person has the lane, executing it and applying its outcome (retries with backoff and jitter).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from openajar.core import logs
from openajar.core.aio import maybe_await
from openajar.core.errors import (
    LaneNotFound,
    OpenajarError,
)
from openajar.core.models import LaneState, RunKind, RunStatus, Signal
from openajar.core.pauses import pause_why
from openajar.runner.execution import Journal, Outcome, RunControl
from openajar.runner.owner import TRY_SOURCE
from openajar.runner.records import TERMINAL, Run
from openajar.runner.run_rules import HOLD_BACKOFF_MAX_S, HOLD_MESSAGES
from openajar.runner.util import clip

log = logging.getLogger("openajar.runner")


class DispatchMixin:
    """dispatch_once(), execution and outcomes of RunnerService (openajar.runner.runner)."""

    def lane_busy(self, lane: str) -> bool:
        return lane in self._lanes

    @property
    def active_count(self) -> int:
        return len(self._active)

    def _backoff(self, n: int) -> float:
        base = min(self.backoff_max_s, self.backoff_base_s * (2 ** max(0, n - 1)))
        return base * (0.5 + self.rng.random() / 2)

    def _lane_ready(self, run: Run) -> bool:
        lanes = self._service("lanes")
        if lanes is None or not run.lane:
            return True
        try:
            info = lanes.get(run.lane)
        except LaneNotFound as e:
            run.status, run.finished_at = RunStatus.FAILED, self.clock.now()
            run.error = {"code": "lane_not_found", "message": e.message}
            if self.store.save_run(run, expect={RunStatus.QUEUED}):
                self._emit(run)
            return False
        except Exception:  # noqa: BLE001 - let the driver report it
            return True
        if getattr(info, "state", None) == LaneState.QUARANTINED:
            reason = getattr(info, "quarantine_reason", None) or "failed its self-check"
            self._defer(run, f"lane {run.lane} is quarantined: {reason}", expect=RunStatus.QUEUED)
            return False
        held, control = self._held(lanes, run.lane, info)
        if held:
            words = (f"{pause_why(run.lane, control)} The run continues after that." if held == "paused"
                     else HOLD_MESSAGES[held].format(lane=run.lane))
            self._defer(run, words, expect=RunStatus.QUEUED, hold=held)
            return False
        return True

    @staticmethod
    def _held(lanes: Any, lane: str, info: Any = None) -> tuple[str | None, Any]:
        """The lane's control state when agents are held off it ("human" or "paused"), else None, and
        its control (who paused it and why, openajar.core.pauses)."""
        try:
            fn = getattr(lanes, "control", None)
            control = fn(lane) if callable(fn) else getattr(info, "control", None)
        except Exception:  # noqa: BLE001 - no control record: nothing to enforce here
            return None, None
        st = getattr(control, "state", None)
        st = str(getattr(st, "value", st)) if st is not None else None
        return (st if st in HOLD_MESSAGES else None), control

    def _defer(self, run: Run, reason: str, *, expect: RunStatus, hold: str | None = None,
               until: datetime | None = None, paced: dict[str, Any] | None = None) -> None:
        """Requeue with backoff (or until `until`). A hold (a human has the lane, a broken
        connector, its pace) never counts toward giving up."""
        now = self.clock.now()
        run.deferrals += 1
        if hold:
            run.held += 1
        counted = run.deferrals - run.held
        if not hold and counted > self.max_deferrals:
            run.status, run.finished_at = RunStatus.FAILED, now
            run.error = {"code": "lane_unavailable", "message": f"gave up after {counted - 1} deferrals: {reason}"}
        else:
            delay = self._backoff(run.deferrals)
            run.status = RunStatus.QUEUED
            run.not_before = now + timedelta(seconds=min(delay, HOLD_BACKOFF_MAX_S) if hold else delay)
            if until is not None and until > now:
                run.not_before = until
            run.error = {"code": "deferred", "message": reason, "deferrals": run.deferrals,
                         **({"control": hold} if hold else {}), **({"paced": paced} if paced else {})}
        if self.store.save_run(run, expect={expect}):
            self._emit(run)

    def on_lane_control(self, lane_id: str, old: Any, new: Any) -> int:
        """Lane control listener (LaneManager.add_control_listener): when agents get a lane
        back, the runs a take over deferred become due now instead of after their backoff."""
        def state(c: Any) -> str | None:
            st = getattr(c, "state", None)
            return str(getattr(st, "value", st)) if st is not None else None

        if state(new) != "agent" or state(old) == "agent":
            return 0
        n = 0
        for run in self.store.list_runs(status=RunStatus.QUEUED, lane=lane_id, limit=500):
            control = (run.error or {}).get("control")
            if run.not_before is not None and control and control not in ("lane_pace", "pace", "approved_slot"):
                run.not_before = None
                if self.store.save_run(run, expect={RunStatus.QUEUED}):
                    n += 1
                    self._emit(run)
        if n:
            self._wake.set()
        return n

    def listen_for_lane_control(self) -> bool:
        """Register on_lane_control with the lanes service, once. True if registered."""
        add = getattr(self._service("lanes"), "add_control_listener", None)
        if self._control_listener or not callable(add):
            return self._control_listener
        add(self.on_lane_control)
        self._control_listener = True
        return True

    async def dispatch_once(self) -> list[asyncio.Task]:
        if self._stopping:
            return []
        capacity = self.max_concurrent - len(self._active)
        if capacity <= 0:
            return []
        now = self.clock.now()
        busy = set(self._lanes)
        by_ws: dict[str, list[Run]] = {}
        # per workspace, so a long backlog in one (or on one busy lane) never hides another's runs
        for ws in self.store.queued_workspaces(now, exclude_lanes=busy):
            runs = [r for r in self.store.list_runs(status=RunStatus.QUEUED, due_before=now, workspace=ws,
                                                    oldest_first=True, limit=50, exclude_lanes=busy)
                    if r.id not in self._active]
            if runs:
                by_ws[ws] = runs
        started: list[asyncio.Task] = []
        while capacity > 0 and by_ws:
            ws = min(by_ws, key=lambda w: (self._served.get(w, 0), by_ws[w][0].created_at))
            pick = next((r for r in by_ws[ws] if not (r.lane and r.lane in self._lanes)), None)
            if pick is None:
                del by_ws[ws]
                continue
            by_ws[ws].remove(pick)
            if not by_ws[ws]:
                del by_ws[ws]
            if pick.cancel_requested:
                await self.cancel(pick.id)
                continue
            if self._retired(pick, now):
                continue
            if not self._lane_ready(pick):
                continue
            pick.status = RunStatus.RUNNING
            pick.started_at = pick.started_at or now
            pick.attempts += 1
            pick.not_before = None
            if not self.store.save_run(pick, expect={RunStatus.QUEUED}):
                continue
            self._serial += 1
            self._served[ws] = self._serial
            if pick.lane:
                self._lanes.add(pick.lane)
            task = asyncio.create_task(self._execute(pick), name=f"run:{pick.id}")
            self._active[pick.id] = task
            started.append(task)
            capacity -= 1
            self._emit(pick)
        return started

    async def _execute(self, run: Run) -> None:
        # on every log line of the run. `parent` is the run's own parent or none: an outer context
        # never lends one
        with logs.log_context(drop=("parent",), run=run.id, lane=run.lane, grant=run.grant_id, parent=run.parent):
            await self._execute_run(run)

    async def _execute_run(self, run: Run) -> None:
        t0 = self.clock.monotonic()
        try:
            try:
                outcome = await self._execute_inner(run)
            except OpenajarError as e:
                outcome = Outcome("finish", RunStatus.FAILED, error={"code": e.code, "message": e.message})
            except Exception as e:  # a bug must not leave the run running forever
                log.exception("run %s crashed", run.id)
                outcome = Outcome("finish", RunStatus.FAILED, error={
                    "code": "internal", "message": clip(self._scrubber(run)(f"{type(e).__name__}: {e}"), max_str=300)})
            if outcome.hold_reason:
                await self._hold_lane(run, outcome)
            # a page's challenge or the wrong account is a fact about the lane, whoever met it, but a draft
            # can point at any response on the domain, so what a response said holds nothing in a try
            if outcome.signal is not None and run.source != TRY_SOURCE:
                await self._report_signal(run, outcome.signal)
            try:
                await self._record(run.id, outcome, t0)
            except Exception as e:  # recording the real outcome failed for good
                log.exception("run %s: could not record its outcome", run.id)
                with contextlib.suppress(Exception):
                    self._apply_outcome(run.id, Outcome("finish", RunStatus.FAILED, error={
                        "code": "internal", "message": clip(f"could not record the outcome: {e}", max_str=300)}), t0)
        finally:
            # in-memory bookkeeping only: a real crash loses this anyway
            self._active.pop(run.id, None)
            if run.lane:
                self._lanes.discard(run.lane)
            self._cancel.discard(run.id)
            self._wake.set()

    async def _record(self, run_id: str, outcome: Outcome, t0: float, *, tries: int = 5) -> None:
        """Apply the outcome; a transient database error (locked, busy) is retried, so a run
        that succeeded is never turned into a failure by the bookkeeping after it."""
        for i in range(tries):
            try:
                self._apply_outcome(run_id, outcome, t0)
                return
            except sqlite3.OperationalError:
                if i == tries - 1:
                    raise
                log.warning("run %s: database busy while recording the outcome; retrying", run_id)
                await asyncio.sleep(0.05 * 2 ** i)

    async def _report_signal(self, run: Run, signal: Signal) -> None:
        """A site said stop with no page to show it (a JSON response): the lane hears it as it hears a
        page's signal, so its hold policy rests or holds it for every agent, flow and schedule. A lane
        that cannot be told is held for a person."""
        report = getattr(self._service("lanes"), "report_signal", None)
        try:
            if report is None:
                raise LookupError("no lane manager")
            await maybe_await(report(run.lane, signal))
        except Exception:
            log.exception("run %s: could not report %s on %s", run.id, signal, run.lane)
            await self._hold_lane(run, Outcome("finish", hold_reason=f"needs_human:{signal}"))

    async def _hold_lane(self, run: Run, outcome: Outcome) -> None:
        """A run found the lane acting as someone else: hold the lane for a human (the lane
        manager's hold, so every agent, flow and schedule stays off it) and tell the owner."""
        lanes = self._service("lanes")
        hold = getattr(lanes, "hold", None)
        if not run.lane or hold is None:
            log.warning("run %s asked to hold lane %s (%s), but there is no lane manager", run.id, run.lane,
                        outcome.hold_reason)
            return
        try:
            await maybe_await(hold(run.lane, reason=outcome.hold_reason or "needs_human"))
        except Exception:
            log.exception("run %s: could not hold lane %s", run.id, run.lane)
            return
        message = (outcome.error or {}).get("message") or outcome.hold_reason
        self._notify("A lane was held", f"Run {run.id} ({run.flow_id}) stopped on {run.lane}: {message}",
                     data={"run_id": run.id, "lane": run.lane, "hold": outcome.hold_reason})

    async def _execute_inner(self, run: Run) -> Outcome:
        run = self.get(run.id)
        executor = self.executors.get(run.kind)
        if executor is None:
            return Outcome("finish", RunStatus.FAILED, error={
                "code": "unavailable", "message": f"no executor for {run.kind} runs on this node"})
        if run.kind == RunKind.FLOW or run.grant_id:
            stop = await self._gate(run)
            if stop is not None:
                return stop
        journal = Journal(self.store, run.id, self.clock)
        control = RunControl(journal, stopping=lambda: self._stopping, cancelled=lambda: run.id in self._cancel)
        return await executor.execute(run, journal, control)

    def _apply_outcome(self, run_id: str, outcome: Outcome, t0: float) -> None:
        run = self.get(run_id)
        now = self.clock.now()
        run.cost = {**run.cost, "ms": int(run.cost.get("ms", 0) + (self.clock.monotonic() - t0) * 1000)}
        run.updated_at = now
        a = outcome.action
        if outcome.proof is not None:
            run.proof = outcome.proof
        if a == "finish":
            run.output = outcome.output
        if a in ("finish", "cancel"):
            run.status = outcome.status or RunStatus.FAILED
            run.finished_at = now
            run.error = outcome.error
            if outcome.reason and run.proof is not None:
                run.proof["note"] = outcome.reason
            elif outcome.reason:
                run.proof = {"readbacks": [], "note": outcome.reason}
            if outcome.recheck and self.recheck_delays_s:
                run.recheck_at = now + timedelta(seconds=self.recheck_delays_s[0])
        elif a == "retry":
            if run.attempts >= self.max_attempts:
                run.status = outcome.status or RunStatus.FAILED
                run.finished_at = now
                err = dict(outcome.error or {})
                err["message"] = f"{err.get('message', 'failed')} (gave up after {run.attempts} attempts)"
                run.error = err
            else:
                run.status = RunStatus.QUEUED
                run.not_before = now + timedelta(seconds=self._backoff(run.attempts))
                run.error = {**(outcome.error or {}), "retrying": True}
        elif a == "defer":
            run.attempts = max(0, run.attempts - 1)
            self._defer(run, outcome.reason or "lane unavailable", expect=RunStatus.RUNNING, hold=outcome.hold,
                        until=outcome.until, paced=outcome.paced)
            return
        elif a == "park":
            if outcome.approval_id and outcome.approval_id == run.approval_id and run.authorized:
                run.status, run.finished_at = RunStatus.FAILED, now
                run.error = {"code": "approval_loop", "message": "the approval was granted but the policy "
                             "asked for it again; check the policy service", "approval_id": run.approval_id}
            else:
                run.status = RunStatus.WAITING_APPROVAL
                run.approval_id = outcome.approval_id
                run.parked_at = now
                run.authorized = run.auto = False
                self._pace_release(run)  # a run waiting for a person holds no row; it claims one when it goes
                run.error = None
        elif a == "decide":
            run.status = RunStatus.PARTIAL
            run.finished_at = now
            run.error = outcome.error
            run.decision = {"needed": {"path": outcome.path, "options": ["retry", "skip", "fail"],
                                       "reason": (outcome.error or {}).get("message")}}
            self._notify("A run needs your decision", f"Run {run.id} ({run.flow_id} on {run.lane}) stopped "
                         f"at step {outcome.path}: it may or may not have happened. Retry, skip or fail it.",
                         data={"run_id": run.id})
        elif a == "requeue":
            run.status = RunStatus.QUEUED
            run.attempts = max(0, run.attempts - 1)
        if run.status in TERMINAL and run.status != RunStatus.SUCCEEDED_UNVERIFIED:
            run.recheck_at = None
        if self.store.save_run(run, expect={RunStatus.RUNNING}):
            self._emit(run)

    def _notify(self, title: str, body: str, *, level: str = "warning", data: dict | None = None) -> None:
        notifier = self._service("notifier")
        fn = getattr(notifier, "notify", None)
        if fn is None:
            log.warning("%s: %s", title, body)
            return

        async def send() -> None:
            try:
                await maybe_await(fn(title=title, body=body, level=level, data=data or {}))
            except Exception:
                log.exception("notifier failed")

        try:
            task = asyncio.get_running_loop().create_task(send())
        except RuntimeError:
            asyncio.run(send())
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)
