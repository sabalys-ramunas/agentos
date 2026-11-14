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
import json
import logging
import random
import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from agentos.core import catalog, logs
from agentos.core.errors import (
    AgentosError,
    ApprovalPending,
    InvalidRequest,
    LaneBusy,
    LaneNotFound,
    LaneQuarantined,
    NotFound,
    PolicyDenied,
    ServiceUnavailable,
)
from agentos.core.models import LANE_ID_RE, Effect, LaneState, Principal, RunStatus, Signal
from agentos.runner import signed_out
from agentos.runner.driver import DriverFactory, FlowDriver
from agentos.runner.executor import FlowExecutor, Journal, Outcome, RunControl, learned_flow
from agentos.runner.flows import FlowService
from agentos.runner.memory_summary import write_summary
from agentos.runner.models import (
    TERMINAL,
    TRY_SOURCE,
    Run,
    RunKind,
    StepStatus,
    is_connector_id,
    is_idempotent,
    is_owner,
    step_at,
)
from agentos.runner.proof import ResolvedCheck, run_check
from agentos.runner.store import RunnerStore
from agentos.runner.util import Clock, Scrubber, clip, maybe_await, new_id

log = logging.getLogger("agentos.runner")

HOLD_MESSAGES = {
    "human": "a human has control of {lane}; the run continues after they hand it back",
    "paused": "the human stopped agents on {lane}; the run continues only after they hand it back",
}

APPROVED = {"approved", "granted", "allowed"}
DENIED = {"denied", "rejected"}
EXPIRED = {"expired", "cancelled", "canceled", "revoked"}
CONSUMED = {"consumed"}  # used by another run or a retried call: ask again
OWNER_GRANT = "owner"  # agentos.policy.grants.OWNER_GRANT_ID: the node owner's grant
# a keyless call identical to a run still open hands that run back (open_twin): one that asked for an
# approval or waits for its slot however long it waits, any other one queued or running for this long
TWIN_WINDOW = timedelta(hours=1)
OPEN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL)
SAME_RUN = ("The same call already has a run that has not finished, so nothing new was started and nothing "
            "asks again. Follow it with run_status. To start another one on purpose, pass a new idempotency_key.")
GRANT_CODES = {"revoked": "grant_revoked", "expired": "grant_expired", "unknown_grant": "grant_unknown",
               "not_yet_valid": "grant_not_yet_valid",
               "frozen": "grant_revoked"}  # a freeze revoked it: the message names the freeze
GATE_FAILURES_MAX = 24     # a run whose source's gate fails this many times in a row ends
HOLD_BACKOFF_MAX_S = 300.0  # a held lane is re-checked at least this often (no lane is opened)
COMMIT_EFFECT_NAMES = frozenset({"commit", "pay", "irreversible"})  # agentos.policy.meters.COMMIT_EFFECTS
# a learned operation (saved_by kind `learned`) runs only for the profile sync and learner
# (agentos.profiles.sync / learner): their Run.source and principal, which no tool passes
LEARNED_SOURCE = "profiles"


def _with_fix(error: dict[str, Any]) -> dict[str, Any]:
    """A run's error with the catalog's next step for an agent (`fix`), as MCP error results carry it."""
    found = catalog.entry(error.get("code"))
    if found is None or "fix" in error:
        return error
    return {**error, "fix": found.agent_step(str(error.get("message") or ""))}


class RunExecutor(Protocol):
    """Executes one kind of run (flow, brain, script). Flows are built in; other packages
    register theirs with RunnerService.register_executor."""

    async def execute(self, run: Run, journal: Journal, control: RunControl) -> Outcome: ...


def _is_person(principal: dict[str, Any]) -> bool:
    return principal.get("kind") == "human" and bool(principal.get("id"))


def workflow_of(run: Run) -> str | None:
    """The workflow this run is a step of. Only the workflow engine sets a run's trigger this way."""
    trigger = run.trigger or {}
    return trigger.get("workflow") if trigger.get("kind") == "workflow" else None


SCHEDULE_TRIGGERS = frozenset({"cron", "every"})


def started_by_schedule(run: Run) -> bool:
    """Did a schedule start this run, with nobody asking: a flow's cron or interval fire, a workflow
    its schedule fired (trigger `scheduled` on its event run, set by WorkflowService.start_from_event),
    or a step of one (its trigger `scheduled`). A retry or a run by hand is not. Read from the trigger
    only, which the runner and the workflow engine set, never from a run's params: a caller writes
    those (an agent's flow_run could pass {"by": "trigger", "scheduled": true})."""
    trigger = run.trigger or {}
    if trigger.get("kind") in SCHEDULE_TRIGGERS:
        return True
    if trigger.get("kind") in ("workflow", "event"):
        return trigger.get("scheduled") is True
    return False


def bind_workflow(ctx: Any, run: Run) -> None:
    """A step of a workflow reads the workflow's own switch too, and the overlay reads what filled
    its inputs. Both can only make the call ask; neither can make it ask less. A run a schedule
    started is judged on its own: an earlier denial does not refuse it unasked (it asks again)."""
    workflow = workflow_of(run)
    if workflow:
        ctx.routine = workflow
        ctx.origins = list((run.trigger or {}).get("origins") or [])
    if started_by_schedule(run):
        ctx.scheduled = True


class RunnerService:
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
        # Run.source -> when an approved run of that source may start (None: now). The engagement
        # engine sends an approved comment to its next session, never out at the hour it was approved.
        self.approved_slot: dict[str, Callable[[Run, datetime], datetime | None]] = {}
        # Run.source -> may a run of that source start now? Asked at every dispatch, so an approved
        # run whose slot passed while the node was down or a person held the lane never goes late:
        # None goes; {"until", "reason", "hold"?} waits; {"end", "reason"} ends it (cancelled)
        self.approved_gate: dict[str, Callable[[Run, datetime], dict[str, Any] | None]] = {}
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

    def submit(self, *, flow_id: str | None = None, version: str | None = None,
               params: dict[str, Any] | None = None, lane: str | None = None, kind: RunKind | str = RunKind.FLOW,
               idempotency_key: str | None = None, parent: str | None = None,
               principal: Principal | dict[str, Any] | None = None, source: str = "tool",
               schedule_id: str | None = None, trigger: dict[str, Any] | None = None,
               authorized: bool = False, approval_id: str | None = None,
               grant_id: str | None = None, not_before: datetime | None = None,
               surface: str | None = None, auto: bool = False, pace_id: int | None = None,
               target: Any = None) -> tuple[Run, bool]:
        """Queue a run. Returns (run, created); an existing idempotency key returns the
        existing run and creates nothing (a key used for another flow or lane is refused).

        `grant_id` is the grant the run acts under; it defaults to the principal's grant, and
        to the owner's grant for the human owner or an anonymous call from inside the node.
        With a policy service the grant must be live and cover the lane and the flow's
        effective effect, or PolicyDenied is raised and nothing is queued.

        `not_before` (aware) is the earliest start: the run waits for it in the queue, and an
        approval decided earlier keeps it waiting until then.

        A connector operation (`connector.<site>.<op>`) runs on a surface: `surface` (or `lane`,
        from entry points that pass `on`) names it, and its binding decides the lane and fills
        the binding params, which the caller may never pass. The grant must cover the surface,
        the operation must not be broken there, and its pace must allow one more run. `target`: a
        binding already made, by a site API's try on a lane no surface names yet.

        `pace_id`: the lane's pace ledger row the caller's authorization wrote for this publish
        (Decision.pace_ids); a commit run with none is checked and recorded right before it runs."""
        kind = RunKind(kind)
        asked = dict(params or {}) if isinstance(params, dict) else None  # the inputs as the caller passed them
        if not_before is not None and not_before.tzinfo is None:
            raise InvalidRequest("not_before needs a timezone (an ISO 8601 time like 2026-11-08T09:00:00+02:00)")
        if params is not None and not isinstance(params, dict):
            raise InvalidRequest("params must be a JSON object of name: value")
        if idempotency_key:
            existing = self.existing_run(idempotency_key, flow_id=flow_id, lane=surface or lane, kind=kind)
            if existing:
                return existing, False
        if isinstance(principal, Principal):
            principal = principal.model_dump()
        principal = principal or {}
        if grant_id is None:
            grant_id = principal.get("grant_id") or (
                OWNER_GRANT if not principal.get("id") or is_owner(principal) else None)
        effect = None
        if kind == RunKind.FLOW:
            if not flow_id:
                raise InvalidRequest("a flow run needs a flow id")
            fv = self.flows.get(flow_id, version)
            if fv.definition.sends and (trigger or {}).get("kind") != "workflow":
                raise PolicyDenied(f"{fv.id} sends to people, so it runs only as a workflow's send step, which keeps "
                                   "the owner's list, the suppression list, the daily limit and an unsubscribe")
            if is_connector_id(fv.id):
                target = target or self.connector_target(fv, surface or lane, params, lane=lane if surface else None)
                lane, params, surface = target.lane, target.params, target.surface.id
            else:
                if surface is not None:
                    raise InvalidRequest(f"{fv.id} is a flow, not a connector operation; it runs on a lane, not a surface")
                lane = self.flows.resolve_lane(fv, lane)
                params = self.flows.validate_params(fv, params)
            version = fv.version
            effect = fv.effect
        elif lane is not None and not LANE_ID_RE.match(lane):
            raise InvalidRequest(f"invalid lane id {lane!r}")
        self.check_grant(grant_id, principal, effect=effect, lane=surface or lane)
        self._check_binding(grant_id, flow_id=flow_id, version=version, target=surface or lane,
                            key=idempotency_key, params=asked)
        if surface is not None:
            self.check_connector(fv, surface, trial=source == TRY_SOURCE, person=_is_person(principal))
            problem = self.learned_problem(fv, surface, principal, source)
            if problem:
                raise PolicyDenied(problem)
        now = self.clock.now()
        start_after = not_before if not_before is not None and not_before > now else None
        run = Run(id=new_id("run"), kind=kind, lane=lane, surface=surface, flow_id=flow_id, flow_version=version,
                  params=params or {}, idempotency_key=idempotency_key, parent=parent,
                  principal=principal, grant_id=grant_id, source=source, schedule_id=schedule_id,
                  trigger=trigger, authorized=authorized, auto=auto, approval_id=approval_id, created_at=now,
                  pace_id=pace_id if authorized else None,
                  updated_at=now,
                  status=RunStatus.WAITING_APPROVAL if approval_id else RunStatus.QUEUED,
                  parked_at=now if approval_id else None, not_before=start_after, start_after=start_after)
        run, created = self.store.insert_run(run)
        if created:
            self._emit(run)
            self._wake.set()
        return run, created

    @contextlib.asynccontextmanager
    async def twin_guard(self, *parts: Any) -> AsyncIterator[None]:
        """Hold identical keyless calls one at a time, from open_twin to submit, so two that arrive
        together cannot both make a run (and an approval) for the same post."""
        key = json.dumps([p.timestamp() if isinstance(p, datetime) else p for p in parts], sort_keys=True,
                         default=str)
        lock = self._twin_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                yield
        finally:
            if not lock.locked() and self._twin_locks.get(key) is lock:
                self._twin_locks.pop(key, None)

    async def open_twin(self, *, flow_id: str, version: str, params: dict[str, Any], grant_id: str | None,
                        lane: str | None = None, surface: str | None = None, not_before: datetime | None = None,
                        content: Any = None) -> Run | None:
        """A run a keyless call would duplicate (QA-2 #11): the same flow version, lane (or surface),
        params, grant and start, made by a tool call, and not finished yet. A retry after a timeout then
        gets the run that already waits, not a second run and a second approval. `content`: what the
        call's approval would bind, which finds a run whose params the owner edited when approving.
        A run that asked for an approval or waits for its slot counts however old it is; any other one
        queued or running counts for TWIN_WINDOW."""
        if not grant_id:
            return None
        now = self.clock.now()
        for run in self.store.list_runs(status=OPEN_STATUSES, flow_id=flow_id,
                                        lane=None if surface else lane, limit=200):
            if (run.flow_version != version or run.grant_id != grant_id
                    or run.surface != surface or (surface is None and run.lane != lane)
                    or run.source != "tool" or run.schedule_id is not None or run.parent is not None):
                continue
            asked = not_before if not_before is not None and not_before > run.created_at else None
            if run.start_after != asked:
                continue
            if not (run.approval_id is not None or run.start_after is not None
                    or now - run.created_at <= TWIN_WINDOW):
                continue
            if run.params == params or (content is not None and await self._edited_from(run, content)):
                return run
        return None

    async def _edited_from(self, run: Run, content: Any) -> bool:
        """Did the owner approve `run` with an edit, from an approval for exactly `content`?"""
        from agentos.policy.approvals import content_digest

        get = getattr(self._service("approvals"), "get", None)
        if not run.approval_id or not callable(get):
            return False
        try:
            ap = await maybe_await(get(run.approval_id))
        except Exception:  # noqa: BLE001 - an approval that is gone edited nothing
            return False
        edit = ap.get("edit") if isinstance(ap, dict) else getattr(ap, "edit", None)
        before = edit.get("digest_before") if isinstance(edit, dict) else None
        return isinstance(before, str) and before == content_digest(content)

    def existing_run(self, key: str, *, flow_id: str | None = None, lane: str | None = None,
                     kind: RunKind | str = RunKind.FLOW, params: dict[str, Any] | None = None) -> Run | None:
        """The run an idempotency key already names, or None. The same key for another flow,
        lane or kind is refused: it would hand back a run the caller did not ask for. So is one
        with other `params`, for a caller that passes them."""
        existing = self.store.run_by_key(key)
        if existing is None:
            return None
        if (existing.kind != RunKind(kind) or (flow_id is not None and existing.flow_id != flow_id)
                or (lane is not None and existing.lane is not None and lane not in (existing.lane, existing.surface))):
            # never which run: it may be on a lane the caller's grant does not reach
            raise InvalidRequest(f"idempotency key {key!r} is already used by another run; use a new key")
        if params is not None and existing.params != params:
            raise InvalidRequest(f"idempotency key {key!r} is already used by run {existing.id} for other "
                                 "arguments; use a new key")
        return existing

    # ------------------------------------------------------------ connector runs

    def connector_target(self, fv: Any, on: str | None, params: dict[str, Any] | None, *,
                         lane: str | None = None, validate: bool = True) -> Any:
        """The binding of a run of the connector operation `fv` on surface `on`
        (connectors.surfaces.Target). `lane`, when given, must be the binding's lane."""
        if self.surfaces is None:
            raise ServiceUnavailable("connector operations need the surfaces store, which this node lacks")
        self._check_active(fv)
        target = self.surfaces.target(fv, on, params, validate=validate)
        if lane is not None and lane != target.lane:
            raise InvalidRequest(f"surface {target.surface.id} runs on {target.lane}, not {lane}")
        return target

    def _check_active(self, fv: Any) -> None:
        """A site API runs what the owner activated: a staged version is a try's, and a try comes
        with its own target. Every way to pin a version comes through here, so none of them runs a
        draft nobody activated. The catalog is reviewed, and may pin any version it has."""
        head = self.store.get_flow(fv.id)
        if (fv.saved_by or {}).get("kind") == "local" and (head is None or head.version != fv.version):
            raise PolicyDenied(f"{fv.id} {fv.version} is not active: a site API runs only the version the owner "
                               "activated. Leave the version out, or try it with site_api_try")

    def check_connector(self, fv: Any, surface: str, *, exclude: str | None = None, started_only: bool = False,
                        trial: bool = False, person: bool = False) -> None:
        """Raise ConnectorBroken when the operation is broken on the surface (or a person paused its
        site API), PaceLimited when its pace allows no more runs now. `trial`: a site API's try, which
        is how a repair is checked, so health does not stop it. A pause does, unless `person`: a person
        fixing a paused API tests the fix, and an agent waits for them."""
        from agentos.runner.connectors.pace import pace_problem
        from agentos.runner.connectors.surfaces import ConnectorBroken, PaceLimited

        meta = fv.definition.connector or {}
        op = f"{meta.get('connector')}.{meta.get('operation')}"
        site_apis = self.site_apis
        if not (trial and person) and site_apis is not None and site_apis.state_of(meta.get("connector")) == "paused":
            from agentos.runner.siteapis.service import ApiPaused

            raise ApiPaused(f"a person paused {meta.get('connector')}; nothing runs until they activate it")
        broken = self.health.broken(op, surface) if self.health is not None and not trial else None
        if broken:
            raise ConnectorBroken(f"{op} is broken on {surface} since {broken.get('since')} "
                                  f"({broken.get('code')} at step {broken.get('step')}); nothing runs until a "
                                  "repaired version is installed. Do not post it another way.")
        problem = pace_problem(self.store, fv.id, surface, meta.get("pace"), self.clock.now(), exclude=exclude,
                               started_only=started_only, local=(fv.saved_by or {}).get("kind") == "local")
        if problem:
            raise PaceLimited(problem[0], retry_at=problem[1])

    def learned_problem(self, fv: Any, surface: str, principal: dict[str, Any] | None, source: str | None) -> str | None:
        """Why a run of a learned operation (agentos.profiles.learn) may not start, or None. It runs
        only for the profile sync and learner (source `profiles`, the node's own `flow` principal):
        no agent, tool, schedule or workflow runs one. A learned save (commit) runs only once it is
        verified, or, while it is still being proved, on the account it was learned on."""
        if not learned_flow(fv):
            return None
        p = principal if isinstance(principal, dict) else {}
        if source != LEARNED_SOURCE or p.get("kind") != "flow" or p.get("id") or p.get("grant_id"):
            return (f"{fv.id} was learned on this node and only the profile sync runs it: change the profile "
                    "instead (profile_update), and agentos brings every account in line")
        if fv.effect != Effect.COMMIT:
            return None
        saved = fv.saved_by or {}
        learner = getattr(self._service("profiles"), "learner", None)
        state = learner.state(saved.get("site"), saved.get("field")) if learner is not None and \
            saved.get("site") and saved.get("field") else None
        if state is None or state.get("commit") != fv.id:
            return f"{fv.id} is not a learned save the profile learner knows; it does not run"
        if state.get("verified"):
            return None
        if state.get("read_verified") and state.get("learned_on") == surface:
            return None
        return (f"{fv.id} is not proved yet: its first save runs on {state.get('learned_on') or 'the account it was '
                'learned on'}, and only after that save is read back does it run on {surface}")

    def connector_approval(self, fv: Any, run: Run) -> tuple[str, dict[str, Any]]:
        """(summary, content) of a queued connector run, as its tool would have asked."""
        target = self.connector_target(fv, run.surface, {k: v for k, v in run.params.items()
                                                         if k not in ("acts_as_id", "account_language")})
        if target.lane != run.lane or target.params != run.params:
            raise PolicyDenied(f"the binding of surface {run.surface} changed since this run was queued (lane or "
                               "acting as); queue it again")
        return self.surfaces.approval(fv, target)

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
            "error": _with_fix(clip(run.error, max_str=500)) if run.error else None,
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

    async def cancel(self, run_id: str, *, by: str | None = None) -> Run:
        run = self.get(run_id)
        if run.terminal and not run.needs_decision:
            return run
        if run.status in (RunStatus.QUEUED, RunStatus.WAITING_APPROVAL) or run.needs_decision:
            prev = run.status
            run.status = RunStatus.CANCELLED
            run.finished_at = self.clock.now()
            run.decision = None
            run.error = {"code": "cancelled", "message": f"cancelled by {by or 'the owner'}",
                         "previous_status": str(prev)}
            if self.store.save_run(run, expect={prev}):
                self._emit(run)
                return run
            run = self.get(run_id)
        if run.status == RunStatus.RUNNING:
            self.store.set_cancel_requested(run_id)
            self._cancel.add(run_id)
            run = self.get(run_id)
        return run

    async def decide(self, run_id: str, choice: str, *, by: str | None = None,
                     refuse_family: str | None = None) -> Run:
        """Resolve a partial run that asked for a decision: retry, skip (it happened) or fail.
        `refuse_family`: the root grant id of a family whose runs this caller may not decide (a
        token never decides what its grant, or one narrowed from it, started; as for approvals). A
        run whose grant is unknown (no grant_id) cannot be placed outside that family, so a token
        does not decide it either."""
        run = self.get(run_id)
        if not run.needs_decision:
            raise InvalidRequest(f"run {run_id} is not waiting for a decision")
        if refuse_family is not None:
            path = self._grant_path(run.grant_id) if run.grant_id else None
            if path is None or f"/{refuse_family}/" in path:  # a grant we cannot place: fail closed
                raise PolicyDenied("self_approval: a token cannot decide what its own grant started, or a run "
                                   "whose grant it cannot place; decide it in a signed-in panel session")
        path = run.decision["needed"]["path"]
        journal = Journal(self.store, run_id, self.clock)
        now = self.clock.now()
        if choice == "fail":
            run.status = RunStatus.FAILED
            run.decision = {"choice": "fail", "path": path, "by": by, "at": now.isoformat()}
            run.finished_at = now
        elif choice in ("retry", "skip"):
            if choice == "retry":
                journal.reset(path)
            else:
                journal.skip(path, result={"acted": True, "skipped_by": by or "owner"})
            run.status = RunStatus.QUEUED
            run.finished_at = None
            run.not_before = None
            run.decision = {"choice": choice, "path": path, "by": by, "at": now.isoformat()}
        else:
            raise InvalidRequest("decision must be retry, skip or fail")
        if not self.store.save_run(run, expect={RunStatus.PARTIAL}):
            raise InvalidRequest(f"run {run_id} changed; read it again")
        self._emit(run)
        self._wake.set()
        return run

    # ------------------------------------------------------------ approvals

    async def _approval_state(self, approvals: Any, approval_id: str) -> str | None:
        getter = getattr(approvals, "get", None)
        if not callable(getter):
            return None
        try:
            obj = await maybe_await(getter(approval_id))
        except NotFound:
            return "expired"
        except Exception:
            log.exception("approvals.get failed")
            return None
        if obj is None:
            return None
        st = obj.get("state") if isinstance(obj, dict) else getattr(obj, "state", None)
        if st is None:
            return None
        return str(getattr(st, "value", st)).lower()

    async def poll_approvals(self) -> int:
        approvals = self._service("approvals")
        changed = 0
        for run in self.store.list_runs(status=RunStatus.WAITING_APPROVAL, limit=500, oldest_first=True):
            state = None
            if approvals is not None and run.approval_id:
                state = await self._approval_state(approvals, run.approval_id)
            changed += await self._resolve_wait(run, state)
        return changed

    async def settle(self, run: Run) -> Run:
        """The run, with an approval it waits on that a person already decided taken into account now
        rather than at the next poll (a keyless retry after the approve: open_twin)."""
        approvals = self._service("approvals")
        if run.status != RunStatus.WAITING_APPROVAL or not run.approval_id or approvals is None:
            return run
        state = await self._approval_state(approvals, run.approval_id)
        if state is not None and state != "pending":
            await self._resolve_wait(run, state)
        return self.get(run.id)

    async def waiting_view(self, run: Run, *, steps: bool = False) -> dict[str, Any]:
        """describe() for an agent, after `settle`: a run a person just approved reads as queued, not
        "waiting for the owner", and a run still waiting says why a person must decide (`why`, the
        reason the owner's card gives) and that it starts on its own once approved."""
        run = await self.settle(run)
        out = self.describe(run, steps=steps)
        if run.status != RunStatus.WAITING_APPROVAL or not run.approval_id:
            return out
        getter = getattr(self._service("approvals"), "get", None)
        if callable(getter):
            try:
                ap = await maybe_await(getter(run.approval_id))
            except Exception:  # noqa: BLE001 - the reason is a courtesy; the run's state is what counts
                ap = None
            why = ap.get("reason") if isinstance(ap, dict) else getattr(ap, "reason", None)
            if why:
                out["why"] = why
        out["summary"] = "waiting for the owner to approve; once approved it starts on its own"
        return out

    async def on_approval(self, approval_id: str, state: str) -> int:
        """Push hook for the approvals service: called when a human decides."""
        n = 0
        for run in self.store.list_runs(status=RunStatus.WAITING_APPROVAL, approval_id=approval_id,
                                        oldest_first=True):
            n += await self._resolve_wait(run, state.lower())
        return n

    async def _consume(self, approval_id: str | None) -> bool | None:
        """Use the approval for this one run. None: the approvals service cannot consume (or
        there is none), so the approval releases the run as before."""
        fn = getattr(self._service("approvals"), "consume", None)
        if not approval_id or not callable(fn):
            return None
        try:
            return bool(await maybe_await(fn(approval_id)))
        except NotFound:
            return False

    async def _approval_edit(self, run: Run) -> tuple[dict[str, Any], str | None, str] | None:
        """(the edited params, the digest they must hash to, who edited) when the approval that
        releases this run was approved with an edit (agentos.policy.approvals), else None."""
        get = getattr(self._service("approvals"), "get", None)
        if not run.approval_id or not callable(get):
            return None
        try:
            ap = await maybe_await(get(run.approval_id))
        except Exception:  # noqa: BLE001 - _consume reports a missing approval
            return None
        edit = ap.get("edit") if isinstance(ap, dict) else getattr(ap, "edit", None)
        if not isinstance(edit, dict) or not isinstance(edit.get("params"), dict):
            return None
        digest = ap.get("content_digest") if isinstance(ap, dict) else getattr(ap, "content_digest", None)
        return dict(edit["params"]), digest, str(edit.get("by") or "the owner")

    def _edited_params(self, run: Run, edit: dict[str, Any], digest: str | None) -> dict[str, Any]:
        """The run's params with the approver's edit, checked against the flow's params and bound
        to the approval: the content they make must hash to the digest the approver approved."""
        from agentos.policy.approvals import content_digest

        fv = self.flows.get(run.flow_id or "", run.flow_version)
        wanted = {**run.params, **edit}
        if run.surface:
            caller = {k: v for k, v in wanted.items() if k not in ("acts_as_id", "account_language")}
            target = self.connector_target(fv, run.surface, caller)
            if target.lane != run.lane:
                raise PolicyDenied(f"the binding of surface {run.surface} changed since this run was queued")
            params, content = target.params, self.surfaces.approval(fv, target)[1]
        else:
            params = self.flows.validate_params(fv, wanted)
            content = self.flows.approval_content(fv, run.lane or "", params)
        if digest is None or content_digest(content) != digest:
            raise PolicyDenied("the edited content does not match what the approver approved")
        return params

    def _approved_slot(self, run: Run, now: datetime) -> datetime | None:
        hook = self.approved_slot.get(run.source)
        if hook is None:
            return None
        try:
            slot = hook(run, now)
        except Exception:  # a broken hook must not lose the approval: the run waits a day instead of going now
            log.exception("the %s slot for approved run %s failed", run.source, run.id)
            return now + timedelta(days=1)
        return slot if slot is not None and slot > now else None

    def _approved_ready(self, run: Run, now: datetime) -> bool:
        """A run of a source with a gate (an engagement operation, approved or not) starts only when the gate
        says so; else it moves to the time the gate names, out of any session it was adopted into
        (`parent` cleared: that session's claim is over), or ends with the gate's reason."""
        hook = self.approved_gate.get(run.source)
        if hook is None:
            return True
        prior = run.error or {}
        try:
            verdict = hook(run, now)
        except Exception:  # a broken gate never lets a run go at the wrong hour: it looks again later
            log.exception("the %s gate for run %s failed", run.source, run.id)
            failures = int(prior.get("gate_failures") or 0) + 1
            if failures >= GATE_FAILURES_MAX:
                verdict = {"end": "gate_failed", "reason": f"its time slot could not be checked {failures} times, so "
                                                           "it was not sent"}
            else:
                verdict = {"until": now + timedelta(hours=1), "gate_failures": failures,
                           "reason": "its time slot could not be checked; it is checked again in an hour"}
        if not verdict:
            return True
        if verdict.get("end"):
            run.status, run.finished_at = RunStatus.CANCELLED, now
            run.error = {"code": str(verdict["end"]), "message": str(verdict.get("reason") or "")}
        else:
            until = verdict.get("until")
            run.not_before = until if isinstance(until, datetime) and until > now else now + timedelta(hours=1)
            run.parent = None
            run.deferrals += 1
            run.held += 1  # waiting for its slot never counts toward giving up
            run.error = {"code": "deferred", "message": str(verdict.get("reason") or "it waits for its time slot"),
                         "control": "approved_slot", "slot": run.not_before.isoformat()}
            if verdict.get("hold"):
                run.error["held_since"] = prior.get("held_since") or now.isoformat()
            if verdict.get("gate_failures"):
                run.error["gate_failures"] = verdict["gate_failures"]
        if self.store.save_run(run, expect={RunStatus.QUEUED}):
            self._emit(run)
        return False

    def adopt(self, run_ids: list[str], *, parent: str) -> list[str]:
        """Queued runs that now run inside the claim `parent` names (an engagement session took them
        in). Only a run that is still queued changes; the ids that did come back."""
        out = []
        for run_id in run_ids:
            run = self.store.get_run(run_id)
            if run is None or run.status != RunStatus.QUEUED:
                continue
            run.parent = parent
            if self.store.save_run(run, expect={RunStatus.QUEUED}):
                out.append(run_id)
        return out

    def _trace_edit(self, run: Run, by: str, names: list[str]) -> None:
        write = getattr(self._service("traces"), "write", None)
        if not callable(write):
            return
        try:
            from agentos.policy.traces import TraceEvent

            write(TraceEvent(kind="approval_edit", lane=run.target, run_id=run.id, tool=run.flow_id,
                             summary=f"approved with an edit by {by}: {', '.join(names)}; the run uses the edit",
                             args={"approval_id": run.approval_id, "params": names}))
        except Exception:
            log.exception("tracing an approval edit failed")

    async def _resolve_wait(self, run: Run, state: str | None) -> int:
        now = self.clock.now()
        edited: dict[str, Any] | None = None
        editor = "the owner"
        if state in APPROVED and (found := await self._approval_edit(run)) is not None:
            edit, digest, editor = found
            try:
                edited = self._edited_params(run, edit, digest)
            except (PolicyDenied, InvalidRequest, NotFound) as e:
                run.status, run.finished_at = RunStatus.FAILED, now
                run.error = {"code": "edit_mismatch", "message": f"the approved edit cannot run: {e.message}",
                             "approval_id": run.approval_id}
                if self.store.save_run(run, expect={RunStatus.WAITING_APPROVAL}):
                    self._emit(run)
                    return 1
                return 0
        # a pilot's approval is the commit gate's: its retried click consumes it, bound to what it sends
        if state in APPROVED and run.kind != RunKind.PILOT and await self._consume(run.approval_id) is False:
            state = "consumed"  # another run (or the retried call) already used this approval
            edited = None
        slot = run.start_after if run.start_after and run.start_after > now else None  # flow_run not_before
        if state in APPROVED and slot is None:
            slot = self._approved_slot(run, now)
        if state in APPROVED:
            run.status, run.authorized, run.not_before, run.error = RunStatus.QUEUED, True, slot, None
            run.auto = False  # a person approved it: Ask me first is not asked again
            if edited is not None:
                names = sorted(k for k in edited if edited[k] != run.params.get(k))
                run.params = edited
                self._trace_edit(run, editor, names)
        elif state in CONSUMED:
            run.status, run.authorized, run.not_before = RunStatus.QUEUED, False, slot
            run.error = {"code": "approval_used", "message": "that approval released another run; this run "
                         "asks the owner again", "approval_id": run.approval_id}
        elif state in DENIED:
            run.status, run.finished_at = RunStatus.FAILED, now
            run.error = {"code": "denied", "message": "the owner denied this run", "approval_id": run.approval_id}
        elif state in EXPIRED:
            run.status, run.finished_at = RunStatus.FAILED, now
            run.error = {"code": "approval_expired", "message": "the approval expired before anyone decided",
                         "approval_id": run.approval_id}
        elif run.parked_at and (now - run.parked_at).total_seconds() > self.approval_timeout_s:
            hours = int(self.approval_timeout_s // 3600)
            run.status, run.finished_at = RunStatus.FAILED, now
            run.error = {"code": "approval_expired", "message": f"no decision within {hours} hours",
                         "approval_id": run.approval_id}
        else:
            return 0
        if self.store.save_run(run, expect={RunStatus.WAITING_APPROVAL}):
            self._emit(run)
            self._wake.set()
            return 1
        return 0

    # ------------------------------------------------------------ grants

    def _grant_context(self, grant_id: str | None, principal: dict[str, Any]) -> Any | None:
        """A ToolContext for the grant a run acts under, or None on a node with no policy
        service. Raises PolicyDenied when the grant is unknown, revoked or expired; a policy
        service that cannot resolve stored grants refuses every run (fail closed)."""
        policy = self._service("policy")
        if policy is None:
            return None
        fn = getattr(policy, "context_for_grant", None)
        if not callable(fn):
            raise PolicyDenied("the policy service on this node cannot check the grant a run carries")
        who = Principal(**principal) if principal.get("id") else None
        return fn(grant_id, principal=who, tool="flow_run")

    def check_pending(self, approvals: Any, grant_id: str | None) -> None:
        """Refuse one more card from a family with too many waiting, as human_ask does."""
        policy = self._service("policy")
        path = self._grant_path(grant_id) if grant_id and policy is not None else None
        if path and approvals.count_pending(path.strip("/").split("/")[0]) >= policy.cfg.max_pending_per_grant:
            from agentos.policy.errors import Denied

            raise Denied("too_many_pending", "too many questions and approvals are already waiting")

    def _grant_path(self, grant_id: str) -> str | None:
        """The grant's chain as `/root/.../leaf/`, or None when the policy service no longer knows
        it (revoked, expired, unknown). On a node with no policy service a grant is its own family."""
        policy = self._service("policy")
        if policy is None:
            return f"/{grant_id}/"
        lookup = getattr(policy, "grant_by_id", None)
        if not callable(lookup):
            return None
        try:
            return lookup(grant_id).path
        except AgentosError:
            return None

    def _check_binding(self, grant_id: str | None, **run: Any) -> None:
        """A grant pinned to one approved run (agentos.policy approved_run_grant, the agenda's) carries
        that run only: the policy refuses any other flow, lane, idempotency key or inputs."""
        check = getattr(self._service("policy"), "check_run_binding", None)
        if callable(check):
            check(grant_id, **run)

    def check_grant(self, grant_id: str | None, principal: dict[str, Any] | Principal | None, *,
                    effect: Any = None, lane: str | None = None) -> None:
        """Scope only (no routing, no approval): the grant is live and covers the lane and the
        effect. For queueing a run and adding a schedule; raises PolicyDenied."""
        if isinstance(principal, Principal):
            principal = principal.model_dump()
        ctx = self._grant_context(grant_id, principal or {})
        pre = getattr(ctx, "precheck", None)
        if effect is not None and callable(pre):
            pre(effect, lane_id=lane)

    @staticmethod
    def _denied(e: PolicyDenied) -> dict[str, Any]:
        reason = getattr(e, "reason", None)
        code = GRANT_CODES.get(reason or "")
        if code is None:
            return {"code": "denied", "message": e.message, **({"reason": reason} if reason else {})}
        what = {"grant_revoked": "was revoked", "grant_expired": "has expired",
                "grant_unknown": "is not known on this node",
                "grant_not_yet_valid": "is not valid yet"}[code]
        return {"code": code, "message": f"the grant this run was started under {what}, so it did not run "
                f"({e.message})"}

    async def _gate(self, run: Run) -> Outcome | None:
        """Right before a run executes: re-check its grant; authorize a flow nobody authorized
        yet for its effective effect. None to go ahead, else the outcome instead of running.
        A connector run first checks its operation's health and pace on its surface."""
        if run.surface and run.kind == RunKind.FLOW:
            try:
                stop = self._connector_gate(self.flows.get(run.flow_id or "", run.flow_version), run)
            except NotFound as e:
                return Outcome("finish", RunStatus.FAILED, error={"code": e.code, "message": e.message})
            if stop is not None:
                return stop
        try:
            if run.kind == RunKind.FLOW and run.authorized and run.approval_id and not run.auto:
                mismatch = await self._approval_mismatch(run)
                if mismatch is not None:
                    return mismatch
            ctx = self._grant_context(run.grant_id, run.principal)
            self._check_binding(run.grant_id, flow_id=run.flow_id, version=run.flow_version, target=run.target,
                                key=run.idempotency_key)
            if ctx is None or run.kind != RunKind.FLOW:
                return None  # other executors authorize their own work under the live grant
            fv = self.flows.get(run.flow_id or "", run.flow_version)
            bind_workflow(ctx, run)
            paced = self._pace_gate(fv, run)
            if paced is not None:
                return paced
            if run.authorized:
                pre = getattr(ctx, "precheck", None)
                if callable(pre):
                    pre(fv.effect, lane_id=run.target)
                if not self._asks_first_now(fv, run):
                    return self._pace_claim(fv, run)
                run.authorized = run.auto = False
                self._pace_release(run)  # it asks again: the new authorization holds its own row
            lane = run.lane or ""
            if run.surface:
                summary, content = self.connector_approval(fv, run)
            else:
                summary, content = (self.flows.run_summary(fv, lane, run.params),
                                    self.flows.approval_content(fv, lane, run.params))
            d = await ctx.authorize(fv.effect, lane_id=run.target, domain=fv.definition.domain, summary=summary,
                                    content=content)
        except PolicyDenied as e:
            if getattr(e, "code", None) == "lane_paced":  # the pace closed between the gate and the debit
                return self._pace_defer(run, e)
            return Outcome("finish", RunStatus.FAILED, error=self._denied(e))
        except ApprovalPending as e:
            return Outcome("park", approval_id=e.approval_id, reason=e.message)
        run.authorized = True
        ids = getattr(d, "pace_ids", None) or []
        run.pace_id = ids[0] if ids else None
        self.store.save_run(run, expect={RunStatus.RUNNING})
        return None

    def run_binding(self, fv: Any, run: Run) -> Any:
        """What an approval of this queued run binds (agentos.policy.approvals.ActionBinding), made
        the way its card was: engage's for a connector run (agentos.engage.adapters), and the content
        the policy gate binds for any run. Raises PolicyDenied when a surface's binding changed."""
        from agentos.policy.approvals import ActionBinding

        lane = run.lane or ""
        if run.surface:
            summary, content = self.connector_approval(fv, run)
        else:
            summary, content = (self.flows.run_summary(fv, lane, run.params),
                                self.flows.approval_content(fv, lane, run.params))
        return ActionBinding(effect=str(fv.effect), tool=run.flow_id or fv.id, lane=run.target, params=run.params, content=content,
                             key=run.idempotency_key, summary=summary)

    async def _approval_mismatch(self, run: Run) -> Outcome | None:
        """Right before a run an approval released executes: the run's own card (the policy gate's,
        which names the flow or connector it runs, or engage's, for its flow) must still cover exactly
        what the run will do (action_binding_problem), else it fails and nothing is done."""
        from types import SimpleNamespace

        from agentos.policy.approvals import action_binding_problem, is_legacy_hash

        get = getattr(self._service("approvals"), "get", None)
        if not callable(get):
            return None  # no approvals service: the run was released as before (_consume)
        try:
            ap = await maybe_await(get(run.approval_id))
        except NotFound:
            ap = None
        if isinstance(ap, dict):
            ap = SimpleNamespace(**ap)
        if ap is not None and not (getattr(ap, "flow_target", None) or getattr(ap, "tool", None) == run.flow_id):
            return None  # a step's own card (a fill, an upload): the step's retried call is checked by the gate
        binding = self.run_binding(self.flows.get(run.flow_id or "", run.flow_version), run)
        legacy: tuple[str, ...] = ()
        if getattr(ap, "binding", None) is None and is_legacy_hash(getattr(ap, "content_hash", None)):
            # an engage card still pending at the upgrade: its old hash, for one release
            caller = {k: v for k, v in run.params.items() if k not in ("acts_as_id", "account_language")}
            legacy = (binding.legacy_engage_hash(), binding.legacy_engage_hash(caller))
        problem = "is gone" if ap is None else action_binding_problem(ap, binding, legacy=legacy)
        if problem is None:
            return None
        return Outcome("finish", RunStatus.FAILED, error={
            "code": "approval_mismatch", "approval_id": run.approval_id,
            "message": f"approval {run.approval_id} {problem}: what this run would do now is not what was "
                       "approved, so nothing was done"})

    def _pace_policy(self, fv: Any, run: Run) -> Any | None:
        if fv.effect.value not in COMMIT_EFFECT_NAMES or not run.lane:
            return None
        policy = self._service("policy")
        return policy if policy is not None and hasattr(policy, "pace_verdict") else None

    def _pace_claim(self, fv: Any, run: Run) -> Outcome | None:
        """An authorized commit run that holds no pace ledger row (the owner started it, or a person
        approved it after it parked): the lane's whole pace is checked and the publish recorded in
        one transaction, so an approval never gets a run past the gap or the day's count."""
        policy = self._pace_policy(fv, run)
        if policy is None or run.pace_id is not None or not hasattr(policy, "pace_claim"):
            return None
        try:
            run.pace_id = policy.pace_claim(run.lane)
        except PolicyDenied as e:
            if getattr(e, "code", None) == "lane_paced":
                return self._pace_defer(run, e)
            raise
        self.store.save_run(run, expect={RunStatus.RUNNING})
        return None

    def _pace_release(self, run: Run) -> None:
        policy = self._service("policy")
        if run.pace_id is not None and policy is not None and hasattr(policy, "pace_forget"):
            policy.pace_forget([run.pace_id])
        run.pace_id = None

    def _pace_gate(self, fv: Any, run: Run) -> Outcome | None:
        """Right before a commit run: the lane's posting pace (agentos.policy.pace, AUD-13). A run
        that holds no ledger row yet (a schedule's slot, a run the API queued, one the owner started,
        one a person approved after it parked) waits until the lane may post again: its waking hours,
        its gap to the posts around it, its posts for the day; it is recorded when it goes. A run
        whose authorization wrote its row (Run.pace_id) only has the waking hours read again (it may
        have waited for a busy lane into the night). Never fails the run: it moves, a little after
        the time the lane may post (jitter), held without counting toward giving up."""
        policy = self._pace_policy(fv, run)
        if policy is None:
            return None
        if run.authorized and run.pace_id is not None:
            e = policy.pace_window(run.lane)
            return self._pace_defer(run, e) if e is not None else None
        pace, zone, v = policy.pace_verdict(run.lane)
        if pace is None or v is None or v.rule is None:
            return None
        from agentos.policy import pace as pacing

        return self._pace_defer(run, pacing.refuse(run.lane, pace, zone, v, policy.clock()))

    def _pace_defer(self, run: Run, e: Any) -> Outcome:
        from agentos.policy import pace as pacing

        nxt = getattr(e, "next_ts", None)
        until = None
        if nxt is not None:
            until = datetime.fromtimestamp(pacing.jitter(run.id, nxt, getattr(e, "next_until", None)), UTC)
        hit = getattr(e, "hit", {}) or {}
        paced = {"rule": hit.get("rule"), "next_at": hit.get("next_at"), "next_at_local": hit.get("next_at_local"),
                 "moved_to": until.isoformat() if until else None, "timezone": hit.get("timezone"),
                 "timezone_source": hit.get("timezone_source")}
        return Outcome("defer", reason=e.message, hold="lane_pace", until=until, paced=paced)

    def _asks_first_now(self, fv: Any, run: Run) -> bool:
        """A commit let through on its own, whose "Ask me first" was turned on while it queued: it asks
        like a new run. A run a person approved or started, or a standing approval covered (it spent
        a use), stays authorized."""
        if not run.auto or fv.effect != Effect.COMMIT:
            return False
        store = self._service("ask_first")
        if store is None:
            return False
        through = run.lane if run.surface else None
        return store.for_call(workflow_of(run) or fv.id, None) is not None or \
            store.for_call(fv.id, run.target, through) is not None

    def _connector_gate(self, fv: Any, run: Run) -> Outcome | None:
        """Right before a connector run: a broken operation or a used-up pace makes it wait
        (held, never counted toward giving up), before any approval routing. A site API's retry loads
        the page again, so its own first visit counts: it goes back only if pace allows that now,
        and otherwise the run ends with what it met, so a site in trouble is not visited more for it."""
        from agentos.runner.connectors.surfaces import ConnectorBroken, PaceLimited

        problem = self.learned_problem(fv, run.surface or "", run.principal, run.source)
        if problem:
            return Outcome("finish", RunStatus.FAILED, error={"code": "denied", "message": problem})
        again = (run.error or {}).get("retrying") and (fv.saved_by or {}).get("kind") == "local"
        try:
            self.check_connector(fv, run.surface or "", exclude=None if again else run.id, started_only=True,
                                 trial=run.source == TRY_SOURCE, person=_is_person(run.principal))
        except ConnectorBroken as e:
            return Outcome("defer", reason=e.message, hold="connector_broken")
        except PaceLimited as e:
            if again:
                proof = run.proof or {}
                return Outcome("finish", RunStatus.PARTIAL if proof.get("acted") or proof.get("writes") else RunStatus.FAILED,
                               error={k: v for k, v in (run.error or {}).items() if k != "retrying"})
            return Outcome("defer", reason=e.message, hold="pace", until=e.retry_at)
        return None

    # ------------------------------------------------------------ dispatch

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
        held = self._held(lanes, run.lane, info)
        if held:
            self._defer(run, HOLD_MESSAGES[held].format(lane=run.lane), expect=RunStatus.QUEUED, hold=held)
            return False
        return True

    @staticmethod
    def _held(lanes: Any, lane: str, info: Any = None) -> str | None:
        """The lane's control state when a human holds it ("human" or "paused"), else None."""
        try:
            fn = getattr(lanes, "control", None)
            control = fn(lane) if callable(fn) else getattr(info, "control", None)
        except Exception:  # noqa: BLE001 - no control record: nothing to enforce here
            return None
        st = getattr(control, "state", None)
        st = str(getattr(st, "value", st)) if st is not None else None
        return st if st in HOLD_MESSAGES else None

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
            if not self._approved_ready(pick, now):
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
        # on every log line of the run; `parent` also tells the flow driver which lane claim the run
        # acts inside (agentos.node.flow_driver._using: an engagement session's runs). It is the run's
        # own parent or none: an outer context never lends one
        with logs.log_context(drop=("parent",), run=run.id, lane=run.lane, grant=run.grant_id, parent=run.parent):
            await self._execute_run(run)

    async def _execute_run(self, run: Run) -> None:
        t0 = self.clock.monotonic()
        try:
            try:
                outcome = await self._execute_inner(run)
            except AgentosError as e:
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

