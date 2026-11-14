"""Flow execution against a FlowDriver, with every step journaled before and after it runs.

Resume rules (after a crash, a restart, a retry or an approval):
- a step journaled `done` or `skipped` is never run again; its saved values, branch choice
  and readback result are restored from the journal;
- a step journaled `started` (in flight when the process died) runs again only if it is
  idempotent; otherwise the run stops as partial and asks a human to decide;
- `failed` and `pending` steps run.

A human holding the lane (HumanInControl, AgentPaused: take over or stop agent) stops the run
before the step's input; the step goes back to pending (like approval_pending) and the run
defers until they hand it back, without counting toward giving up.

What is shown (the step journal, proof, errors) never holds the value of a param the flow
does not mark `show`: it reads `${name}`. Resume state that needs the values verbatim (the
page URL, saved values, a resolved readback for a later re-check) goes to the store's
private step_state.

Connector flows (docs/design/connectors.md 4.5):
- assert_identity (and a preflight's acting_as check) compares who the run acts as with the
  binding; another id fails the run as wrong_account / wrong_surface and holds the lane
  (Outcome.hold_reason, applied by the runner through the lane manager's hold);
- the preflight runs right before the commit step; a mismatch fails the run with nothing
  posted and runs the connector's discard steps (best effort) to close the composer;
- once the commit step (connector.commit.step) has acted, a failure is partial, never failed;
- extract reads typed, capped records; for_each runs read-only steps per item;
- read_json reads the same records from a JSON response the page itself made. Its watch starts
  before the navigate it follows and lives in memory only, so a read that did not finish loads that
  page again on resume. The body stays in the driver and in `_read_json`: only declared fields leave;
- what a connector run read from a site is in no problem text: an error, a readback's `found` and the
  step journal say what kind of value came and the field, never the value, and a saved value that a
  template carried into a URL, a locator or a check reads `${name}` there. Saved values stay in the
  private step state;
- on success the connector's outputs are mapped from the saved values and checked against
  the output schema into Run.output; any problem fails the run as output_contract_violation;
- a locator that no longer resolves (element_not_found, ambiguous_locator), or a read_json answer of
  the wrong shape, while nothing has published fails the run as connector_contract_broken (`cause`
  keeps the code), which marks the operation broken on its surface (connectors.health);
- a read_json that met a bad moment (a 5xx, nothing in time, a slow body) says nothing of the contract
  on its own: it retries as any transient failure does, and fails under its own code. A retry its
  pace does not allow ends the run instead (runner._connector_gate).
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

from agentos.core.errors import (
    AgentPaused,
    ApprovalPending,
    EngineError,
    HumanInControl,
    LaneBusy,
    LaneQuarantined,
    PolicyDenied,
)
from agentos.core.models import RunStatus, Signal
from agentos.core.signals import RUN_SIGNAL_CODES
from agentos.runner import signed_out
from agentos.runner.driver import (
    AmbiguousLocator,
    DriverError,
    DriverFactory,
    DriverTimeout,
    ElementNotFound,
    FlowDriver,
    JsonResponse,
    ResponseWatch,
    parse_json,
)
from agentos.runner.flows import FlowService
from agentos.runner.models import (
    ACTION_TYPES,
    JSON_ROOT,
    TRY_SOURCE,
    AssertIdentityStep,
    BranchStep,
    ExtractField,
    ExtractStep,
    FlowDefinition,
    ForEachStep,
    IdentityCheck,
    JsonField,
    Locator,
    NavigateStep,
    PreflightStep,
    ReadbackStep,
    ReadJsonStep,
    Run,
    StepRecord,
    StepStatus,
    is_idempotent,
    iter_steps,
    may_publish,
    static_path,
    step_at,
    url_pattern,
    watchers,
)
from agentos.runner.proof import ResolvedCheck, run_check
from agentos.runner.store import RunnerStore
from agentos.runner.util import (
    TEMPLATE_RE,
    Clock,
    Scrubber,
    TemplateError,
    clip,
    clip_text,
    host_in,
    norm_lines,
    render,
    url_problem,
)

log = logging.getLogger("agentos.runner")

HOLDS = {HumanInControl.code: "human", AgentPaused.code: "paused"}  # a human holds the lane

NEEDS_YOU = {
    Signal.CAPTCHA: "the page shows a captcha. Solve it in the lane's live view (lane_view), then run again",
    Signal.CHECKPOINT: ("the site asks you to confirm it's you. Answer it in the lane's live view (lane_view), "
                        "then run again"),
    Signal.LOGGED_OUT: "the lane is logged out. Log in through the live view (lane_view), then run again",
    Signal.BLOCKED: "the site is blocking this lane. Check it in the live view (lane_view)",
}

SIGNAL_CODES = RUN_SIGNAL_CODES  # the run's error code
# connectors.health.CONTRACT_CAUSES
CONTRACT_CAUSES = frozenset({"element_not_found", "ambiguous_locator", "json_status", "json_invalid", "json_too_large"})
IDENTITY_CODES = {"account": "wrong_account"}  # any other acting_as kind: wrong_surface
# what a URL names before the site resolves it to the account's own id (LinkedIn's /in/me/)
SELF_ALIASES = frozenset({"me"})
OUTPUT_CAP = 64 * 1024  # bytes of JSON in Run.output["data"]
MAX_FIELD_CHARS = 20_000
MAX_JSON_INTEGER = 10**18  # what an extract's integer holds
WATCH_POLL_S = 0.1
BODY_WAIT_S = 25.0  # past `timeout_ms`, for a response that came and whose body is being read: the driver stops at 20
READ_KINDS = frozenset({"url", "attribute", "text", "value"})  # a readback whose `found` is what the site said
NOT_READ_RE = re.compile(r"no element at the locator|\d+ matches; ")  # proof.evaluate's own words for such a check
MIN_READ_CHARS = 8  # a saved text shorter than this is hidden from nothing: it would eat ordinary words
TOLD = ("message", "expected", "found", "locator", "locator_text", "check", "check_text", "note", "url")  # of a failure


@dataclass
class Outcome:
    action: Literal["finish", "retry", "defer", "park", "decide", "requeue", "cancel"]
    status: RunStatus | None = None
    proof: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    approval_id: str | None = None
    reason: str | None = None
    recheck: bool = False
    path: str | None = None
    hold: str | None = None  # defer without counting: "human", "paused", "connector_broken", "pace", "lane_pace"
    until: datetime | None = None  # defer until then (a connector's pace)
    hold_reason: str | None = None  # hold the lane for a human (wrong account): the hold's reason
    signal: Signal | None = None  # report this to the lane, as a page's signal is: it rests or holds the lane
    output: dict[str, Any] | None = None  # a connector run's typed result (Run.output)
    paced: dict[str, Any] | None = None  # a defer for the lane's posting pace: rule, next_at, moved_to (AUD-13)


class Journal:
    """The durable step log of one run. Each call is its own committed transaction."""

    def __init__(self, store: RunnerStore, run_id: str, clock: Clock):
        self.store, self.run_id, self.clock = store, run_id, clock

    def load(self) -> dict[str, StepRecord]:
        """The journal, with each step's private resume state (raw url and saved values)
        merged back over its shown result."""
        private = self.store.step_states(self.run_id)
        records = {}
        for r in self.store.steps(self.run_id):
            resume = {k: v for k, v in (private.get(r.path) or {}).items() if k in ("url", "vars")}
            if resume and r.result is not None:
                r.result = {**r.result, **resume}
            records[r.path] = r
        return records

    def start(self, path: str, idx: int, type_: str) -> None:
        self.store.step_start(self.run_id, path, idx, type_, self.clock.now())

    def done(self, path: str, result: dict[str, Any], private: dict[str, Any] | None = None) -> None:
        if private:
            self.store.put_step_state(self.run_id, path, private)
        self.store.step_end(self.run_id, path, StepStatus.DONE, self.clock.now(), result=result)

    def failed(self, path: str, error: dict[str, Any], result: dict[str, Any] | None = None) -> None:
        self.store.step_end(self.run_id, path, StepStatus.FAILED, self.clock.now(), result=result, error=error)

    def skip(self, path: str, result: dict[str, Any] | None = None) -> None:
        self.store.step_set(self.run_id, path, StepStatus.SKIPPED, result=result)

    def reset(self, path: str) -> None:
        self.store.step_set(self.run_id, path, StepStatus.PENDING)

    def cancel_requested(self) -> bool:
        return self.store.cancel_requested(self.run_id)


class _Stop(Exception):
    def __init__(self, why: Literal["cancel", "drain"]):
        super().__init__(why)
        self.why = why


class RunControl:
    def __init__(self, journal: Journal, stopping: Callable[[], bool], cancelled: Callable[[], bool]):
        self.journal, self._stopping, self._cancelled = journal, stopping, cancelled

    def check(self) -> None:
        if self._cancelled() or self.journal.cancel_requested():
            raise _Stop("cancel")
        if self._stopping():
            raise _Stop("drain")


class StepFailure(Exception):
    def __init__(self, code: str, message: str, *, acted: bool | None = False, transient: bool = False,
                 locator: Locator | None = None, expected: Any = None, found: Any = None,
                 approval_id: str | None = None, readback: dict[str, Any] | None = None,
                 hold: str | None = None, signal: Signal | None = None):
        super().__init__(message)
        self.code, self.message, self.acted, self.transient = code, message, acted, transient
        self.locator, self.expected, self.found = locator, expected, found
        self.approval_id, self.readback = approval_id, readback
        self.hold = hold  # the lane hold this failure calls for ("needs_human:wrong_account")
        self.signal = signal  # what the site said, when no page did: the lane's hold policy decides
        self.path = ""
        self.index = 0
        self.step_type = ""
        self.screenshot: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return clip({
            "code": self.code, "message": self.message, "step": self.path, "index": self.index,
            "type": self.step_type,
            "locator": self.locator.model_dump(exclude_none=True) if self.locator else None,
            "locator_text": self.locator.describe() if self.locator else None,
            "expected": self.expected, "found": self.found, "screenshot": self.screenshot,
            "acted": self.acted, "transient": self.transient,
            "repair": ("Fix the step with flow_save (it creates a new version), then run again."
                       if self.code in ("element_not_found", "ambiguous_locator", "readback_failed") else None),
        }, max_str=500)


@dataclass
class _State:
    run: Run
    flow: FlowDefinition
    records: dict[str, StepRecord]
    scope: dict[str, Any]
    driver: FlowDriver | None = None
    readbacks: list[dict[str, Any]] = field(default_factory=list)  # as shown (scrubbed)
    writes_done: int = 0  # acted steps flagged writes: true
    acted: int = 0  # acted input steps that can publish (any of them makes a failure partial)
    last_url: str | None = None
    owner_saved: bool = False
    # a learned operation's run (saved_by kind `learned`): its commit scope is strict (FlowCommit.strict)
    strict_scope: bool = False
    scrub: Scrubber = field(default_factory=Scrubber)
    labels: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # save_as -> untrusted/private fields
    truncated: set[str] = field(default_factory=set)  # save_as names whose values were capped
    journal: Journal | None = None
    watches: dict[str, ResponseWatch] = field(default_factory=dict)  # read_json step path -> its open watch
    picked: set[str] = field(default_factory=set)  # read_json step paths whose page a caller's param chose

    @property
    def meta(self) -> dict[str, Any]:
        return self.flow.connector or {}

    @property
    def commit_label(self) -> str | None:
        return (self.meta.get("commit") or {}).get("step")

    def unread(self, failure: dict[str, Any]) -> dict[str, Any]:
        """A failure of a connector run with what the run saved taken out of its text: a template can
        carry a saved value into a URL, a locator or a check, and from there into the failure."""
        saved = {k: _texts(v) for k, v in self.scope["vars"].items()} if self.flow.connector is not None else {}
        reads = Scrubber({k: v for k, v in saved.items() if v})
        return {**failure, **{k: reads(failure[k]) for k in TOLD if failure.get(k) is not None}} if reads else failure

    def publishes(self, step: Any) -> bool:
        """An input step that may publish; a connector's commit step always does."""
        if step.type in ACTION_TYPES and self.commit_label and getattr(step, "label", None) == self.commit_label:
            return True
        return may_publish(step, owner_saved=self.owner_saved)


def _classify(e: BaseException, phase: str) -> StepFailure:
    """Turn a driver exception into a structured failure. phase 'find' = nothing done yet."""
    acted: bool | None = False if phase == "find" else None
    if isinstance(e, StepFailure):
        return e
    if isinstance(e, HumanInControl | AgentPaused):
        # the lane's gate raises these before any input of the step, so nothing happened
        return StepFailure(e.code, e.message, acted=False, transient=True)
    if isinstance(e, ElementNotFound):
        return StepFailure("element_not_found", e.message, acted=False, found="no match")
    if isinstance(e, AmbiguousLocator):
        return StepFailure("ambiguous_locator", e.message, acted=False, found=f"{e.count} matches")
    if isinstance(e, ApprovalPending):
        return StepFailure("approval_pending", e.message, acted=False, approval_id=e.approval_id)
    if isinstance(e, PolicyDenied):
        return StepFailure("denied", e.message, acted=False)
    if isinstance(e, DriverTimeout | TimeoutError):
        return StepFailure("timeout", str(e) or "timed out", acted=acted, transient=True)
    if isinstance(e, LaneBusy | LaneQuarantined | EngineError):
        return StepFailure(e.code, e.message, acted=acted, transient=True)
    if isinstance(e, DriverError):
        return StepFailure(e.code, e.message, acted=acted, transient=e.transient)
    if isinstance(e, ConnectionError | OSError):
        return StepFailure("connection", f"{type(e).__name__}: {e}", acted=acted, transient=True)
    if isinstance(e, TemplateError):
        return StepFailure("missing_param", e.message, acted=False)
    return StepFailure("driver_error", f"{type(e).__name__}: {e}", acted=acted)


async def _guard(coro: Any, phase: str, locator: Locator | None = None) -> Any:
    try:
        return await coro
    except StepFailure:
        raise
    except Exception as e:  # every driver failure becomes structured
        f = _classify(e, phase)
        f.locator = f.locator or locator
        if f.code in ("element_not_found", "ambiguous_locator") and locator is not None:
            f.expected = f"exactly one {locator.describe()}"
        raise f from e


_MATCH_PARAM = re.compile(r"\$\{([a-z][a-z0-9_]{0,40})\}")


def _match_for(match: dict[str, str], params: dict[str, Any]) -> dict[str, str]:
    """A strict run's `match` patterns with each `${param}` replaced by the run's value of it,
    escaped (a learned save: `^${value}$` becomes exactly this run's value). A param the run does
    not have raises: the run does not start rather than send an unpinned save."""
    def sub(m: re.Match[str]) -> str:
        if m.group(1) not in params or params[m.group(1)] is None:
            raise ValueError(f"the commit request pins {m.group(1)}, which this run has no value for")
        return re.escape(str(params[m.group(1)]))

    return {path: _MATCH_PARAM.sub(sub, rx) for path, rx in match.items()}


def learned_flow(fv: Any) -> bool:
    """Was this flow version installed by the node from a learned profile edit (saved_by kind
    `learned`, written only by the node's own installer)? Its runs are strictly scoped."""
    saved = getattr(fv, "saved_by", None)
    return isinstance(saved, dict) and saved.get("kind") == "learned"


class FlowExecutor:
    def __init__(self, flows: FlowService, driver_factory: Callable[[], DriverFactory | None], *,
                 clock: Clock, rng: random.Random | None = None, shots_dir: Path | None = None,
                 ref_base: Path | None = None, poll_s: float = 0.5):
        self.flows = flows
        self._factory = driver_factory
        self.clock = clock
        self.rng = rng or random.Random()
        self.shots_dir = shots_dir
        self.ref_base = ref_base or (shots_dir.parent if shots_dir else None)
        self.poll_s = poll_s

    # ------------------------------------------------------------ entry

    async def execute(self, run: Run, journal: Journal, control: RunControl) -> Outcome:
        fv = self.flows.get(run.flow_id or "", run.flow_version)  # runs pin their version
        flow = fv.definition
        records = journal.load()
        for rec in records.values():
            if rec.status == StepStatus.STARTED:
                step = step_at(flow.steps, rec.path)
                if is_idempotent(step):
                    journal.reset(rec.path)
                else:
                    return self.in_flight_outcome(rec.path, step.type)
        records = journal.load()
        state = _State(run=run, flow=flow, records=records,
                       scope={**run.params, "vars": {}, "trigger": run.trigger or {}},
                       owner_saved=fv.owner_saved, scrub=self.flows.scrubber(fv, run.params), journal=journal,
                       strict_scope=learned_flow(fv))
        factory = self._factory()
        if factory is None:
            return Outcome("finish", RunStatus.FAILED, error={
                "code": "unavailable", "message": "no flow driver is configured on this node"})
        outcome: Outcome | None = None
        try:
            async with factory(run.lane or "") as driver:
                state.driver = driver
                await self._scope_commit(state)
                outcome = await self._run_all(state, journal, control)
        except Exception as e:  # noqa: BLE001 - every session failure becomes an outcome
            if outcome is None:
                outcome = self._session_error(e, state)
            # else: the lane failed to close cleanly after the work was done
        return self._shown(outcome, state)

    @staticmethod
    async def _scope_commit(state: _State) -> None:
        """A connector run tells its driver which commit request it declared (connector.commit.request,
        None for a read), so the lane's egress guard refuses any other commit or write the run's page
        sends (agentos.node.egress.FlowCommit). A driver that cannot scope (the fakes) is left as is;
        one that cannot bind its route raises, and the run does not start. A learned operation's run
        is scoped strictly (`strict` in the request: FlowCommit.strict), a read's included."""
        fn = getattr(state.driver, "scope_commit", None)
        if fn is None or state.flow.connector is None:
            return
        request = (state.meta.get("commit") or {}).get("request")
        if state.strict_scope:
            request = {**(request or {}), "strict": True}
            if request.get("match"):  # a learned save pins its field to this run's own value
                request["match"] = _match_for(request["match"], getattr(state, "scope", None) or {})
        await fn(state.flow.id, request)

    @staticmethod
    def _shown(outcome: Outcome, state: _State) -> Outcome:
        """Hidden param values out of everything the run record shows."""
        if state.scrub:
            outcome.error = state.scrub(outcome.error)
            outcome.proof = state.scrub(outcome.proof)
            outcome.reason = state.scrub(outcome.reason)
        return outcome

    @staticmethod
    def in_flight_outcome(path: str, type_: str) -> Outcome:
        return Outcome("decide", RunStatus.PARTIAL, path=path, error={
            "code": "in_flight_unknown", "step": path, "type": type_,
            "message": (f"step {path} ({type_}) was running when the node stopped. It may or may not "
                        "have happened, and it is not safe to repeat. Check the lane, then decide: "
                        "retry the step, skip it (it happened), or fail the run.")})

    def _session_error(self, e: Exception, state: _State) -> Outcome:
        if isinstance(e, HumanInControl | AgentPaused):
            return Outcome("defer", reason=e.message, hold=HOLDS[e.code])
        if isinstance(e, LaneBusy):
            return Outcome("defer", reason=f"lane busy: {e.message}")
        if isinstance(e, LaneQuarantined):
            return Outcome("defer", reason=f"lane quarantined: {e.message}")
        if isinstance(e, PolicyDenied):
            return Outcome("finish", self._final_status(state), error={"code": "denied", "message": e.message})
        if isinstance(e, ApprovalPending):
            return Outcome("park", approval_id=e.approval_id, reason=e.message)
        f = _classify(e, "find")
        err = f.to_dict()
        err["message"] = f"could not open the lane: {err['message']}"
        if f.transient:
            return Outcome("retry", self._final_status(state), error=err, proof=self._proof(state))
        return Outcome("finish", self._final_status(state), error=err, proof=self._proof(state))

    # ------------------------------------------------------------ traversal

    async def _run_all(self, state: _State, journal: Journal, control: RunControl) -> Outcome:
        try:
            await self._reposition(state)
            await self._run_list(state.flow.steps, "", state, journal, control)
        except _Stop as s:
            if s.why == "drain":
                return Outcome("requeue", reason="the node is stopping; the run resumes after restart")
            return Outcome("cancel", RunStatus.CANCELLED, proof=self._proof(state), error={
                "code": "cancelled", "message": "cancelled", "writes_done": state.writes_done})
        except StepFailure as f:
            return await self._failure_outcome(f, state)
        finally:
            for watch in state.watches.values():  # a run that stopped early: no listener or body outlives it
                watch.close()
            state.watches.clear()
        return await self._completion_outcome(state)

    async def _reposition(self, state: _State) -> None:
        """On resume, put the page back where the last completed step left it."""
        done = [r for r in state.records.values()
                if r.status in (StepStatus.DONE, StepStatus.SKIPPED) and r.result and r.result.get("url")]
        if not done or state.flow.steps[0].type == "navigate" and "0" not in state.records:
            return
        pending_top = [i for i, _ in enumerate(state.flow.steps)
                       if str(i) not in state.records
                       or state.records[str(i)].status not in (StepStatus.DONE, StepStatus.SKIPPED)]
        if pending_top and (state.flow.steps[pending_top[0]].type in ("navigate", "read_json")
                            or watchers(state.flow.steps, pending_top[0])):
            return  # that step loads the page anyway (a read_json's navigate runs again, see _recheck)
        order = {path: i for i, (path, _) in enumerate(iter_steps(state.flow.steps))}

        def when(r: StepRecord) -> tuple[int, list[int]]:  # execution order, not wall time
            return order.get(static_path(r.path), -1), [int(k) for k in re.findall(r"\.each(\d+)\.", r.path)]

        last = max(done, key=when)
        url = last.result["url"]
        try:
            if await state.driver.current_url() != url:
                await state.driver.navigate(url)
        except Exception:  # the next step reports its own failure
            log.debug("reposition to %s failed", state.scrub(url), exc_info=True)

    async def _run_list(self, steps: list[Any], prefix: str, state: _State, journal: Journal,
                        control: RunControl) -> None:
        for i, step in enumerate(steps):
            path = f"{prefix}{i}"
            rec = state.records.get(path)
            if rec and rec.status in (StepStatus.DONE, StepStatus.SKIPPED) and not self._recheck(steps, i, prefix, state):
                self._apply(step, path, rec.result or {}, state)
            else:
                control.check()
                journal.start(path, i, step.type)
                try:
                    try:
                        self._watch(steps, i, prefix, state)
                        result = await self._run_step(step, path, state, control)
                    except (StepFailure, _Stop):
                        raise
                    except Exception as e:
                        raise _classify(e, "act") from e
                except StepFailure as f:
                    f.path, f.index, f.step_type = path, int(path.split(".")[0]), step.type
                    if f.acted and getattr(step, "writes", False):
                        state.writes_done += 1
                    if f.acted and state.publishes(step):
                        state.acted += 1
                    if f.readback:
                        state.readbacks.append(state.unread(state.scrub(f.readback)))
                    paused = f.code == "approval_pending" or (f.code in HOLDS and f.acted is not True)
                    if not paused:
                        f.screenshot = await self._screenshot(state, path)
                    if paused:
                        journal.reset(path)  # it did not happen: it runs on resume
                    else:
                        journal.failed(path, state.unread(state.scrub(f.to_dict())), result={"acted": f.acted})
                    raise
                check = result.pop("_check", None)
                shown = state.scrub(result)
                private = None
                if state.scrub or state.flow.connector is not None:
                    private = {k: result[k] for k in ("url", "vars") if k in result}
                    if check is not None:
                        private["check"] = check
                if state.flow.connector is not None:  # what the site said stays out of what is shown
                    shown = {k: v for k, v in shown.items() if k != "vars"}
                journal.done(path, shown, private=private)
                result = {**shown, **{k: result[k] for k in ("url", "vars") if k in result}}
                self._apply(step, path, result, state)
                rec = StepRecord(run_id=state.run.id, path=path, status=StepStatus.DONE, result=result)
            if isinstance(step, BranchStep):
                taken = (rec.result or {}).get("taken", "else")
                branch = step.then if taken == "then" else step.else_
                await self._run_list(branch, f"{path}.{taken}.", state, journal, control)
            elif isinstance(step, ForEachStep):
                items = self._loop_items(step, state)
                n = min(int((rec.result or {}).get("iterations", len(items))), len(items))
                try:
                    for k in range(n):
                        state.scope["vars"][step.as_] = items[k]
                        await self._run_list(step.steps, f"{path}.each{k}.", state, journal, control)
                finally:
                    state.scope["vars"].pop(step.as_, None)

    @staticmethod
    def _recheck(steps: list[Any], i: int, prefix: str, state: _State) -> bool:
        """A preflight done before a restart or a retry runs again unless the step after it (the
        commit) may have acted: what it checked may have changed, and the commit never trusts it.
        So does the load a read_json still waits on, with the identity check after it: a watch lives
        in memory, so the page is visited again. That is a new visit of the run, never a replay."""
        step = steps[i]
        if isinstance(step, NavigateStep | AssertIdentityStep):
            ends = [state.records.get(f"{prefix}{j}") for j in watchers(steps, i)]
            return any(r is None or r.status not in (StepStatus.DONE, StepStatus.SKIPPED) for r in ends)
        if not isinstance(step, PreflightStep):
            return False
        nxt = state.records.get(f"{prefix}{i + 1}")
        if nxt is None or nxt.status == StepStatus.PENDING:
            return True
        return nxt.status == StepStatus.FAILED and (nxt.result or {}).get("acted") is False

    def _apply(self, step: Any, path: str, result: dict[str, Any], state: _State) -> None:
        if result.get("url"):
            state.last_url = result["url"]
        if result.get("vars"):
            self._save_vars(step, path, result["vars"], state)
        for name, labels in (result.get("labels") or {}).items():
            state.labels[name] = labels
        for name in result.get("truncated_vars") or []:
            state.truncated.add(name)
        if result.get("readback"):
            state.readbacks.append(result["readback"])
        if result.get("acted") and getattr(step, "writes", False):
            state.writes_done += 1
        if result.get("acted") and state.publishes(step):
            state.acted += 1

    @staticmethod
    def _save_vars(step: Any, path: str, saved: dict[str, Any], state: _State) -> None:
        """Saved values; inside a for_each they collect over the iterations (an extract's
        items are appended, anything else becomes a list per iteration)."""
        scope = state.scope["vars"]
        if ".each" not in path:
            scope.update(saved)
            return
        for name, value in saved.items():
            if isinstance(step, ExtractStep | ReadJsonStep) and isinstance(value, dict):
                have = scope.get(name) if isinstance(scope.get(name), dict) else {
                    "items": [], "count": 0, "total": 0, "truncated": False}
                scope[name] = {"items": [*have["items"], *value.get("items", [])],
                               "count": have["count"] + value.get("count", 0),
                               "total": have["total"] + value.get("total", 0),
                               "truncated": bool(have["truncated"] or value.get("truncated"))}
            else:
                have = scope.get(name)
                scope[name] = [*(have if isinstance(have, list) else []), value]

    # ------------------------------------------------------------ steps

    def _value(self, step: Any, state: _State, literal: str) -> str:
        if step.param is not None:
            value = state.scope.get(step.param)
            if value is None:
                raise StepFailure("missing_param", f"param {step.param!r} has no value", acted=False)
            return str(value)
        try:
            return render(getattr(step, literal), state.scope)
        except TemplateError as e:
            raise StepFailure("missing_param", e.message, acted=False) from e

    def _loc(self, locator: Locator, state: _State) -> Locator:
        try:
            loc = locator.render(state.scope)
        except TemplateError as e:
            raise StepFailure("missing_param", e.message, acted=False, locator=locator) from e
        _check_patterns(loc)
        return loc

    def _url(self, template: str, state: _State) -> str:
        try:
            url = render(template, state.scope, url=True)
        except TemplateError as e:
            raise StepFailure("missing_param", e.message, acted=False) from e
        problem = url_problem(template, url, state.flow.domain)
        if problem:
            raise StepFailure("url_outside_domain", f"refused to load it: {problem}", acted=False,
                              expected=f"a URL inside {state.flow.domain or 'the flow domain'}", found=url)
        return url

    async def _run_step(self, step: Any, path: str, state: _State, control: RunControl) -> dict[str, Any]:
        d = state.driver
        t = step.type
        if t == "navigate":
            url = self._url(step.url, state)
            nav = await _guard(d.navigate(url), "act")
            self._check_signal(nav.signal, expected=f"the page at {url}")
            return {"url": nav.url, "status": nav.status, "signal": str(nav.signal)}
        if t == "pause":
            ms = self.rng.randint(step.min_ms, step.max_ms)
            remaining = ms / 1000
            while remaining > 0:
                control.check()
                chunk = min(remaining, 1.0)
                await self.clock.sleep(chunk)
                remaining -= chunk
            return {"slept_ms": ms}
        if t == "wait_for":
            loc = self._loc(step.locator, state)
            if step.state == "visible":
                await _guard(d.find(loc, timeout_ms=step.timeout_ms), "find", loc)
            elif step.state == "gone":
                await self._wait_gone(d, loc, step.timeout_ms)
            out: dict[str, Any] = {"url": await d.current_url()}
            if step.save_as:
                st = await _guard(d.read_state(loc), "find", loc)
                out["vars"] = {step.save_as: clip(st.model_dump(), max_str=300)}
            return out
        if t == "readback":
            rb = await self._readback(step, None, path, state)
            if rb["passed"] is False:
                raise StepFailure("readback_failed", f"readback failed: {rb['check_text']}", acted=False,
                                  locator=rb["_locator"], expected=rb["expected"], found=rb["found"],
                                  readback=_public_rb(rb))
            out = {"url": await d.current_url(), "readback": _public_rb(rb), "_check": rb["_check"]}
            if step.save_as and step.locator is not None:
                st = await _guard(d.read_state(self._loc(step.locator, state)), "find", step.locator)
                out["vars"] = {step.save_as: clip(st.model_dump(), max_str=300)}
            elif step.save_as:  # a url readback saves the page it proved (a permalink: `post.url`)
                # found only when it proved it: one that could not be checked (passed None) saves the
                # page as not found, so missing proof never reads as a proved permalink
                proved = rb["passed"] is True
                out["vars"] = {step.save_as: {"url": out["url"], "found": proved, "count": int(proved)}}
            return out
        if t == "branch":
            return {"taken": await self._branch(step, path, state)}
        if t == "assert_identity":
            return {"url": await d.current_url(), "identity": await self._identity(step, state)}
        if t == "preflight":
            return {"url": await d.current_url(), "preflight": await self._preflight(step, state)}
        if t == "extract":
            return await self._extract(step, state)
        if t == "read_json":
            return await self._read_json(step, path, state, control)
        if t == "for_each":
            items = self._loop_items(step, state)
            return {"iterations": len(items)}
        if t == "scroll_to":
            loc = self._loc(step.locator, state)
            el = await _guard(d.find(loc, timeout_ms=step.timeout_ms), "find", loc)
            await _guard(d.scroll_to(el), "act", loc)
            return {"url": await d.current_url()}
        # actions: the inline readback is resolved (params filled in, patterns checked) before acting
        loc = self._loc(step.locator, state) if step.locator is not None else None
        check = self._resolve(step.readback, loc, path, state) if step.readback is not None else None
        el = await _guard(d.find(loc, timeout_ms=step.timeout_ms), "find", loc) if loc is not None else None
        if t == "click":
            await _guard(d.click(el), "act", loc)
        elif t == "type":
            await _guard(d.type(el, self._value(step, state, "text")), "act", loc)
        elif t == "fill":
            await _guard(d.fill(el, self._value(step, state, "text")), "act", loc)
        elif t == "select":
            await _guard(d.select(el, self._value(step, state, "option")), "act", loc)
        elif t == "upload":
            await _guard(d.upload(el, self._value(step, state, "param")), "act", loc)
        elif t == "press":
            await _guard(d.press(step.key, el), "act", loc)
        else:  # pragma: no cover - the model validates step types
            raise StepFailure("invalid", f"unknown step type {t}")
        out = {"acted": True, "url": await _guard(d.current_url(), "act", loc)}
        if check is not None:
            try:
                rb = await self._run_resolved(check, state)
            except Exception as e:
                raise StepFailure("readback_error", f"the action ran, but its readback could not run: "
                                  f"{type(e).__name__}: {e}", acted=True) from e
            if rb["passed"] is False:
                raise StepFailure("readback_failed", f"the action ran, but its readback failed: {rb['check_text']}",
                                  acted=True, locator=rb["_locator"], expected=rb["expected"],
                                  found=rb["found"], readback=_public_rb(rb))
            out["readback"] = _public_rb(rb)
            out["_check"] = rb["_check"]
        return out

    def _check_signal(self, signal: Signal, *, expected: str) -> None:
        if signal == Signal.RATE_LIMITED:
            raise StepFailure("rate_limited", "the site is rate limiting this lane; retrying later",
                              acted=False, transient=True, expected=expected, found=str(signal))
        if signal in NEEDS_YOU:
            raise StepFailure(SIGNAL_CODES[signal], f"needs you: {NEEDS_YOU[signal]}", acted=False,
                              expected=expected, found=str(signal))

    async def _wait_gone(self, d: FlowDriver, loc: Locator, timeout_ms: int) -> None:
        deadline = self.clock.monotonic() + timeout_ms / 1000
        while True:
            st = await _guard(d.read_state(loc), "find", loc)
            if st.count == 0:
                return
            if self.clock.monotonic() >= deadline:
                raise StepFailure("timeout", f"{loc.describe()} did not go away", acted=False, transient=True,
                                  locator=loc, expected="no match", found=f"{st.count} matches")
            await self.clock.sleep(self.poll_s)

    def _resolve(self, spec: Any, default_locator: Locator | None, path: str, state: _State) -> ResolvedCheck:
        try:
            loc = spec.locator.render(state.scope) if spec.locator is not None else default_locator
            cond = spec.condition.render(state.scope)
        except (TemplateError, ValueError) as e:
            raise StepFailure("missing_param", str(e), acted=False) from e
        url = self._url(spec.url, state) if spec.url else None
        if loc is None and cond.kind != "url":
            raise StepFailure("invalid", "the readback has no target", acted=False)
        if loc is not None:
            _check_patterns(loc)
        if cond.matches is not None:
            _check_pattern(cond.matches, "the readback's pattern")
        return ResolvedCheck(path=path, locator=loc if cond.kind != "url" else None, condition=cond,
                             url=url, timeout_ms=spec.timeout_ms)

    async def _readback(self, spec: Any, default_locator: Locator | None, path: str, state: _State) -> dict:
        return await self._run_resolved(self._resolve(spec, default_locator, path, state), state)

    async def _run_resolved(self, check: ResolvedCheck, state: _State) -> dict:
        """Run a check. The result is what is shown (hidden params read `${name}`); `_check`
        keeps the resolved check verbatim for a later re-check (private step state)."""
        result = await run_check(state.driver, check, self.clock, poll_s=self.poll_s)
        data = clip(result.model_dump(mode="json", exclude={"check"}), max_str=500)
        if state.flow.connector is not None:
            data["found"] = _found(check, result.found, result.passed)
        raw = check.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        data["check"] = raw
        data["check_text"] = check.describe()
        data = state.scrub(data)
        data["_locator"] = check.locator
        data["_check"] = raw
        return data

    async def _branch(self, step: BranchStep, path: str, state: _State) -> str:
        c = step.if_
        if c.param is not None:
            value = state.scope.get(c.param)
            if c.is_set is not None:
                ok = (value not in (None, "", [], {})) == c.is_set
            elif c.equals is not None:
                ok = value == c.equals
            else:
                ok = bool(value)
            return "then" if ok else "else"
        spec = ReadbackStep(locator=c.locator, condition=c.condition, timeout_ms=0) if c.locator \
            else ReadbackStep(condition=c.condition, timeout_ms=0)
        rb = await self._readback(spec, None, path, state)
        if rb["passed"] is None:
            raise StepFailure("check_unknown", f"the branch condition could not be checked: {rb.get('note')}",
                              acted=False, transient=True, locator=rb["_locator"], expected=rb["expected"])
        return "then" if rb["passed"] else "else"

    # ------------------------------------------------------------ connector steps

    async def _identity(self, check: IdentityCheck, state: _State) -> dict[str, Any]:
        """Who the page acts as, compared with `expect`. Raises wrong_account / wrong_surface
        (with a lane hold) for another id, identity_unknown when none can be read."""
        d = state.driver
        try:
            expect = render(check.expect, state.scope).strip()
        except TemplateError as e:
            raise StepFailure("missing_param", e.message, acted=False) from e
        if not expect:
            raise StepFailure("missing_param", "the identity to expect is empty", acted=False)
        kind = check.acting_as
        seen: str | None = None
        source = check.source
        if check.source == "cookie":
            fn = getattr(d, "account", None)
            if fn is None:
                raise StepFailure("unsupported", "this node's flow driver cannot read the account identity",
                                  acted=False)
            site = check.site or state.meta.get("site")
            ident = await _guard(fn(site, state.flow.domain), "find")
            if ident:
                seen, source = str(ident.get("user_id") or "") or None, ident.get("source") or "cookie"
            where = f"the identity cookie of {site or state.flow.domain or 'the site'}"
        elif check.source == "url":
            # an alias (LinkedIn's /in/me/) is not an id yet: wait until the site redirected it to
            # the account's own URL; one that never resolves is unknown, never another account
            deadline = self.clock.monotonic() + check.timeout_ms / 1000
            while ((m := re.search(check.pattern or "", await d.current_url())) is None
                   or m.group(1).casefold() in SELF_ALIASES) and self.clock.monotonic() < deadline:
                await self.clock.sleep(self.poll_s)
            seen = m.group(1) if m and m.group(1).casefold() not in SELF_ALIASES else None
            where = "the page URL"
        else:
            loc = self._loc(check.locator, state)
            await _guard(d.find(loc, timeout_ms=check.timeout_ms), "find", loc)
            st = await _guard(d.read_state(loc), "find", loc)
            if not st.found:
                code = "ambiguous_locator" if st.ambiguous else "element_not_found"
                raise StepFailure(code, f"the element whose link names the identity: {st.count} matches",
                                  acted=False, locator=loc, expected=f"exactly one {loc.describe()}",
                                  found=f"{st.count} matches")
            m = re.search(check.pattern or "", st.attributes.get("href") or "")
            seen = m.group(1) if m else None
            where = f"the href of {loc.describe()}"
        if not seen:
            raise StepFailure("identity_unknown", f"could not read who this run acts as from {where}; the page "
                              "may have changed or the lane is signed out. Nothing was done.", acted=False,
                              expected=f"{kind} {expect}", found="no id")
        if seen.casefold() != expect.casefold():
            code = IDENTITY_CODES.get(kind, "wrong_surface")
            what = "account" if kind == "account" else kind.replace("_", " ")
            raise StepFailure(code, f"the lane acts as another {what} than {expect}. Nothing was done and the lane "
                              "is held: check it in the live view (lane_view), then hand it back.", acted=False,
                              expected=expect, found=f"another {what}", hold=f"needs_human:{code}")
        return {"acting_as": kind, "id": seen, "source": source, "status": "match"}

    async def _preflight(self, step: PreflightStep, state: _State) -> list[dict[str, Any]]:
        d = state.driver
        deadline = self.clock.monotonic() + step.timeout_ms / 1000
        while True:
            results: list[dict[str, Any]] = []
            for i, c in enumerate(step.checks):
                if c.kind == "acting_as":
                    continue
                loc = self._loc(c.locator, state)
                st = await _guard(d.read_state(loc), "find", loc)
                if c.kind == "value_equals":
                    want = state.scope.get(c.param)
                    if want is None:
                        raise StepFailure("missing_param", f"param {c.param!r} has no value", acted=False)
                    ok = st.found and _norm(st.value or "") == _norm(str(want))
                    found = (("the value it expected" if ok else f"another value of {len(st.value or '')} characters")
                             if st.found else f"{st.count} matches" + (" (ambiguous)" if st.ambiguous else ""))
                    results.append({"check": i, "kind": c.kind, "locator": loc.describe(), "passed": ok,
                                    "expected": f"${{{c.param}}}", "found": found})
                elif c.kind == "text_differs":
                    want = render(c.text or "", state.scope)
                    # exactly one element (nth never picks one of several), and its text is not
                    # the value the edit sets: an edit to the value already there is a no-op the
                    # site may still record, so it stops here. `same` marks that case alone (one
                    # element, read, showing exactly that value): the run ends already_current
                    one = st.found and not st.ambiguous and st.count == 1
                    shown = st.lines if c.lines else st.text
                    norm = norm_lines if c.lines else _norm
                    same = one and shown is not None and norm(shown) == norm(want)
                    ok = one and shown is not None and not same
                    found = (clip_text(shown or "", 200) if one
                             else f"{st.count} matches" + (" (ambiguous)" if st.count > 1 else ""))
                    results.append({"check": i, "kind": c.kind, "locator": loc.describe(), "passed": ok,
                                    "expected": f"not {c.text}", "found": found, "same": same})
                else:
                    ok = st.count == c.equals
                    results.append({"check": i, "kind": c.kind, "locator": loc.describe(), "passed": ok,
                                    "expected": c.equals, "found": st.count})
            if all(r["passed"] for r in results) or self.clock.monotonic() >= deadline:
                break
            await self.clock.sleep(self.poll_s)
        bad = [r for r in results if not r["passed"]]
        # every other check held, and each failed text_differs found its one element showing
        # exactly the value: nothing to do, which is neither a mismatch nor a broken connector
        current = bool(bad) and all(r.get("same") for r in bad)
        for r in results:
            r.pop("same", None)
        if bad:
            shown = "; ".join(f"{r['kind']} {r['locator']}: expected {r['expected']}, found {r['found']}" for r in bad)
            expected = [{"kind": r["kind"], "locator": r["locator"], "expected": r["expected"]} for r in bad]
            if current:
                raise StepFailure("already_current", f"the page already shows the value this edit sets, so "
                                  f"nothing was saved: {shown}", acted=False, expected=expected,
                                  found=[r["found"] for r in bad])
            why = ("the page could not be read as this edit expects (the element is missing or not alone), so "
                   "nothing was saved" if any(r["kind"] == "text_differs" for r in bad) else
                   "the composer does not hold exactly what was approved, so nothing was posted")
            raise StepFailure("preflight_mismatch", f"{why}: {shown}", acted=False, expected=expected,
                              found=[r["found"] for r in bad])
        for i, c in enumerate(step.checks):
            if c.kind == "acting_as":
                results.append({"check": i, "kind": c.kind, "passed": True, **await self._identity(c.identity(), state)})
        return results

    async def _extract(self, step: ExtractStep, state: _State) -> dict[str, Any]:
        d = state.driver
        fn = getattr(d, "extract_items", None)
        if fn is None:
            raise StepFailure("unsupported", "this node's flow driver cannot extract records", acted=False)
        loc = self._loc(step.items, state)
        fields = {name: f.model_copy(update={"locator": self._loc(f.locator, state)}) if f.locator else f
                  for name, f in step.fields.items()}
        pick = {"pick": step.pick} if step.pick else {}  # a driver from before `pick` still reads a plain extract
        rows, total = await _guard(fn(loc, fields, step.max_items, **pick), "find", loc)
        url = await d.current_url()
        items: list[dict[str, Any]] = []
        problems: list[str] = []
        clipped = total > step.max_items or len(rows) > step.max_items
        for k, row in enumerate(rows[: step.max_items]):
            rec: dict[str, Any] = {}
            for name, f in step.fields.items():
                value, cut, problem = _typed(row.get(name), f, url)
                if problem:
                    problems.append(f"item {k} {name}: {problem}")
                clipped = clipped or cut
                rec[name] = value
            if step.drop_empty and all(v is None for v in rec.values()):
                total -= 1
                continue
            items.append(rec)
        if problems:
            raise StepFailure("output_contract_violation", "extracted values do not match the connector's types (the "
                              f"site may have changed shape): {'; '.join(problems[:5])}", acted=False, locator=loc,
                              found=problems[:10])
        saved = {"items": items, "count": len(items), "total": total, "truncated": clipped}
        labels = {"untrusted": sorted(n for n, f in step.fields.items() if f.untrusted),
                  "private": sorted(n for n, f in step.fields.items() if f.private)}
        out: dict[str, Any] = {"url": url, "extracted": len(items), "total": total,
                               "vars": {step.save_as: saved}, "labels": {step.save_as: labels}}
        if clipped:
            out["truncated_vars"] = [step.save_as]
        return out

    def _watch(self, steps: list[Any], i: int, prefix: str, state: _State) -> None:
        """Before a navigate loads, start listening for each read_json that watches it."""
        if not isinstance(steps[i], NavigateStep):
            return
        for j in watchers(steps, i):
            fn = getattr(state.driver, "watch_json", None)
            if fn is None:
                raise StepFailure("unsupported", "this node's flow driver cannot read a page's JSON", acted=False)
            state.watches[f"{prefix}{j}"] = fn(_on_site(steps[j].url, state.flow.domain))
            if _picked_by_caller(steps[i].url, state):
                state.picked.add(f"{prefix}{j}")

    async def _read_json(self, step: ReadJsonStep, path: str, state: _State, control: RunControl) -> dict[str, Any]:
        """A failure names the step's own pattern and a status, nothing of the response: its URL can
        hold a token, and its body is only for the declared fields."""
        d = state.driver
        watch = state.watches.pop(path, None)
        if watch is None:
            raise StepFailure("invalid", "read_json has no page load to watch: it follows its navigate", acted=False)
        try:
            deadline = self.clock.monotonic() + step.timeout_ms / 1000
            while not watch.responses and self.clock.monotonic() < deadline + (BODY_WAIT_S if _reading(watch) else 0):
                control.check()
                await self.clock.sleep(WATCH_POLL_S)
            seen = next(iter(watch.responses), None)
            if seen is None and _reading(watch):
                seen = JsonResponse(step.url, 0, problem="slow")
        finally:
            watch.close()
        self._check_signal(await _guard(d.signal(), "find"), expected=f"a JSON response on {step.url}")
        rows = _json_rows(_json_body(seen, step, picked=path in state.picked), step)
        url = await d.current_url()
        fields = {name: (f, ExtractField(attr="value", format=f.format, host=f.host, max_chars=f.max_chars))
                  for name, f in step.fields.items()}
        items: list[dict[str, Any]] = []
        problems: list[str] = []
        clipped = len(rows) > step.max_items
        for k, row in enumerate(rows[: step.max_items]):
            if not isinstance(row, dict):
                problems.append(f"item {k}: is {_json_kind(row)}, not an object")
                continue
            rec: dict[str, Any] = {}
            for name, (f, shape) in fields.items():
                value, cut, problem = _json_value(row, f, shape, url)
                if problem:
                    problems.append(f"item {k} {name}: {problem}")
                clipped = clipped or cut
                rec[name] = value
            items.append(rec)
        if problems:
            raise StepFailure("output_contract_violation", "the JSON does not match the connector's types (the site may "
                              f"have changed shape): {'; '.join(problems[:5])}", acted=False, found=problems[:10])
        saved = {"items": items, "count": len(items), "total": len(rows), "truncated": clipped}
        labels = {"untrusted": sorted(n for n, f in step.fields.items() if f.untrusted),
                  "private": sorted(n for n, f in step.fields.items() if f.private)}
        out: dict[str, Any] = {"url": url, "extracted": len(items), "total": len(rows),
                               "vars": {step.save_as: saved}, "labels": {step.save_as: labels}}
        if clipped:
            out["truncated_vars"] = [step.save_as]
        return out

    @staticmethod
    def _loop_items(step: ForEachStep, state: _State) -> list[Any]:
        value = state.scope["vars"].get(step.over)
        items = value.get("items") if isinstance(value, dict) and "items" in value else value
        if not isinstance(items, list):
            raise StepFailure("invalid", f"for_each over {step.over!r}: it holds no list", acted=False)
        return items[: step.max_items]

    async def _discard(self, state: _State) -> str | None:
        """Run the connector's discard steps (close the composer without saving), best effort.
        None when there are none."""
        try:
            steps = state.flow.discard_steps()
        except ValueError:
            return "skipped: the discard steps are invalid"
        if not steps or state.driver is None:
            return None
        try:
            for i, step in enumerate(steps):
                await self._run_step(step, f"discard.{i}", state, _NoControl())
        except Exception as e:  # noqa: BLE001 - the failure that stopped the run is what matters
            msg = e.message if isinstance(e, StepFailure) else f"{type(e).__name__}: {e}"
            return f"failed: {clip_text(state.scrub(msg), 200)}"
        return "done"

    def _output(self, state: _State) -> tuple[dict[str, Any] | None, list[str]]:
        """Run.output for a connector run: outputs mapped from the saved values, checked
        against the output schema, labelled and capped (docs/design/connectors.md 4.10)."""
        from agentos.runner.connectors.outputs import outputs_from_saved

        meta = state.meta
        data, problems = outputs_from_saved(meta, state.scope["vars"])
        if problems:
            return None, problems
        untrusted, private = _labels(meta.get("outputs") or {}, state.labels)
        truncated = any(path.split(".", 1)[0] in state.truncated for path in (meta.get("outputs") or {}).values())
        data, capped = _cap(data)
        if data is None:
            return None, [f"data: over the {OUTPUT_CAP // 1024} KB cap with nothing to trim"]
        output = {
            "data": data, "untrusted_fields": untrusted, "private_fields": private,
            "truncated": truncated or capped,
            "source": {"connector": meta.get("connector"), "operation": meta.get("operation"),
                       "version": meta.get("version"), "contract": meta.get("contract"),
                       "flow_version": state.run.flow_version, "surface": state.run.surface, "lane": state.run.lane,
                       "read_at": self.clock.now().isoformat()},
        }
        return state.scrub(output), []

    async def _screenshot(self, state: _State, path: str) -> str | None:
        if self.shots_dir is None or state.driver is None:
            return None
        try:
            data = await state.driver.screenshot()
        except Exception:  # noqa: BLE001 - a missing screenshot must not hide the real failure
            return None
        rel = Path(state.run.id) / f"step-{path.replace('.', '_')}-a{state.run.attempts}.png"
        target = self.shots_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        try:
            return str(target.relative_to(self.ref_base)) if self.ref_base else str(target)
        except ValueError:
            return str(target)

    # ------------------------------------------------------------ outcomes

    @staticmethod
    def _final_status(state: _State) -> RunStatus:
        """partial once anything may have been published (a flagged write, or any acted input
        step the owner did not mark `publishes: false`), so a re-run is never a blind repeat."""
        return RunStatus.PARTIAL if state.writes_done or state.acted else RunStatus.FAILED

    def _proof(self, state: _State, screenshot: str | None = None) -> dict[str, Any]:
        return {"readbacks": state.readbacks, "url": state.scrub(state.last_url), "writes": state.writes_done,
                "acted": state.acted, "screenshot": screenshot}

    async def _completion_outcome(self, state: _State) -> Outcome:
        shot = await self._screenshot(state, "final")
        proof = self._proof(state, shot)
        output = None
        if state.flow.connector is not None:
            output, problems = self._output(state)
            if problems:
                return Outcome("finish", self._final_status(state), proof=proof, error={
                    "code": "output_contract_violation", "problems": state.scrub(problems[:10]),
                    "message": ("the run's result does not match the connector's output contract (the site may "
                                f"have changed shape): {state.scrub('; '.join(problems[:5]))}")})
        outcome = self._verdict(state, proof)
        if output is not None and not state.readbacks and not state.acted and not state.writes_done:
            # a read operation: the typed output, read from the page and checked against the
            # contract, is the proof
            outcome.status, outcome.reason = RunStatus.SUCCEEDED_VERIFIED, None
        if state.meta.get("signed_out") and outcome.status == RunStatus.SUCCEEDED_VERIFIED:
            proof["public"] = await self._signed_out(state)
            if not signed_out.passed(proof["public"]):
                outcome.status, outcome.recheck = RunStatus.SUCCEEDED_UNVERIFIED, True
                outcome.reason = signed_out.reason(proof["public"])
        outcome.output = output
        return outcome

    async def _signed_out(self, state: _State) -> dict[str, Any]:
        """The AUD-22 check of a published object, after its live readback passed (see
        agentos.runner.signed_out). The url and text go to the private step state for re-checks."""
        check, problem = signed_out.resolve(state.meta["signed_out"], state.scope, state.flow.domain)
        if check is None:
            return state.scrub(signed_out.record(None, note=problem, clock=self.clock))
        if state.journal is not None:
            state.journal.store.put_step_state(state.run.id, signed_out.CHECK_PATH, check)
        return state.scrub(await signed_out.run(state.driver, check, self.clock))

    @staticmethod
    def _verdict(state: _State, proof: dict[str, Any]) -> Outcome:
        rbs = state.readbacks
        if not rbs:
            return Outcome("finish", RunStatus.SUCCEEDED_UNVERIFIED, proof=proof,
                           reason="the flow declares no readback, so nothing confirms the effect")
        unknown = [rb for rb in rbs if rb.get("passed") is None]
        if unknown:
            # a later re-check can only run readbacks that know which page to load
            recheck = all(rb.get("check", {}).get("url") for rb in unknown)
            notes = "; ".join(rb.get("note") or "could not be checked" for rb in unknown)
            return Outcome("finish", RunStatus.SUCCEEDED_UNVERIFIED, proof=proof, recheck=recheck,
                           reason=f"actions reported ok, but {len(unknown)} readback(s) could not be checked: {notes}")
        return Outcome("finish", RunStatus.SUCCEEDED_VERIFIED, proof=proof)

    async def _failure_outcome(self, f: StepFailure, state: _State) -> Outcome:
        err = state.unread(f.to_dict())
        if f.code in ("preflight_mismatch", "already_current") and state.acted == 0:
            discarded = await self._discard(state)
            if discarded:
                err["discard"] = discarded
        proof = state.unread(self._proof(state, f.screenshot))
        if f.hold:
            return Outcome("finish", self._final_status(state), error=err, proof=proof, hold_reason=f.hold)
        if f.signal is not None:  # never a retry: the lane is resting or waits for a person
            return Outcome("finish", self._final_status(state), error=err, proof=proof, signal=f.signal)
        if f.code == "approval_pending":
            return Outcome("park", approval_id=f.approval_id, reason=f.message, proof=proof)
        if f.code in HOLDS and f.acted is not True:
            return Outcome("defer", reason=f.message, hold=HOLDS[f.code])
        status = self._final_status(state)
        if f.code == "denied":
            return Outcome("finish", status, error=err, proof=proof)
        if state.flow.connector is not None and f.code in CONTRACT_CAUSES and not f.transient and status == RunStatus.FAILED:
            # the site changed shape before anything could publish: the operation is broken,
            # not the run; health holds its schedules and tells the owner once
            err = {**err, "code": "connector_contract_broken", "cause": f.code,
                   "message": f"the site no longer matches the connector at step {f.path} ({f.step_type}), so "
                              f"nothing was posted: {err.get('message')}"}
            return Outcome("finish", status, error=err, proof=proof)
        if state.run.source == TRY_SOURCE and f.code == "rate_limited":  # a try must not go back to a site that said stop
            return Outcome("finish", status, error=err, proof=proof)
        step = step_at(state.flow.steps, f.path)
        if f.transient and (f.acted is False or is_idempotent(step)):
            return Outcome("retry", status, error=err, proof=proof)
        if f.acted is None and not is_idempotent(step):
            err["message"] = (f"step {f.path} ({f.step_type}) failed while acting, so it may or may not have "
                              f"happened: {f.message}. Check the lane, then retry the step, skip it, or fail the run.")
            return Outcome("decide", RunStatus.PARTIAL, error=err, proof=proof, path=f.path)
        return Outcome("finish", status, error=err, proof=proof)


def _found(check: ResolvedCheck, found: Any, passed: bool | None) -> Any:
    """What a connector's readback says it found: never a value the site gave."""
    if check.condition.kind not in READ_KINDS or found is None or NOT_READ_RE.match(str(found)):
        return found
    return "the value it expected" if passed else "another value"


def _texts(value: Any) -> list[str]:
    """The long enough strings in a saved value."""
    if isinstance(value, str):
        return [value] if len(value) >= MIN_READ_CHARS else []
    if isinstance(value, dict):
        return [t for v in value.values() for t in _texts(v)]
    return [t for v in value for t in _texts(v)] if isinstance(value, list) else []


def _public_rb(rb: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in rb.items() if not k.startswith("_")}


def _check_pattern(pattern: str, what: str) -> None:
    try:
        re.compile(pattern)
    except re.error as e:
        raise StepFailure("invalid_pattern", f"{what} is not a valid regex once params are filled in: {e}",
                          acted=False) from e


def _check_patterns(loc: Locator) -> None:
    """A regex (a name or an attr) built from a param is checked before the driver sees it."""
    for pattern, what in loc.regexes():
        _check_pattern(pattern, what)


class _NoControl:
    def check(self) -> None:
        pass


def _norm(text: str) -> str:
    return " ".join(text.split())


def _typed(raw: str | None, f: ExtractField, base_url: str) -> tuple[Any, bool, str | None]:
    """(value, clipped, problem) for one extracted field. A value that fails its pattern or
    format is a problem (the site changed shape); a URL outside `host` is dropped (None)."""
    if raw is None:
        return None, False, None
    value = raw if f.attr is not None else _norm(raw)
    if f.capture:
        m = re.search(f.capture, value)
        if m is None or m.group(1) is None:  # an optional group can match nothing
            return None, False, None
        value = m.group(1)
    if f.format == "uri" or f.host:
        value = urljoin(base_url, value.strip())
        try:
            parts = urlsplit(value)
            ok_url = parts.scheme in ("http", "https") and bool(parts.hostname)
        except ValueError:
            ok_url, parts = False, None
        if f.host and not (ok_url and parts and parts.scheme == "https" and host_in(parts.hostname or "", f.host)):
            return None, False, None
        if not ok_url:
            return None, False, "is not a URL"
    if f.pattern and re.search(f.pattern, value) is None:
        return None, False, "does not match its pattern"
    if f.format == "date-time":
        try:
            datetime.fromisoformat(value.strip())
        except ValueError:
            return None, False, "is not a date-time"
    elif f.format == "integer":
        if not re.fullmatch(r"[+-]?\d{1,18}", value.strip()):
            return None, False, "is not an integer"
        return int(value), False, None
    elif f.format == "number":
        try:
            return float(value.strip()), False, None
        except ValueError:
            return None, False, "is not a number"
    limit = min(f.max_chars or MAX_FIELD_CHARS, MAX_FIELD_CHARS)
    if len(value) > limit:
        return value[:limit], True, None
    return value, False, None


def _on_site(pattern: str, domain: str | None) -> Callable[[str], bool]:
    """Which response URLs a read_json takes: https under the flow's domain, whatever its pattern says,
    then the pattern. Runs on the event loop for every response, so it stays one pass over the URL."""
    rx = url_pattern(pattern)

    def wanted(url: str) -> bool:
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
        except ValueError:
            return False
        return parts.scheme == "https" and bool(domain) and host_in(host, domain or "") and rx.matches(url)

    return wanted


def _reading(watch: ResponseWatch) -> bool:
    """A response has come and its body is on its way, on a driver that can say so."""
    return bool(getattr(watch, "reading", 0))


def _picked_by_caller(url: str, state: _State) -> bool:
    """Does a param a caller gave fill this URL? Then the page is theirs, not the endpoint's."""
    from agentos.runner.connectors.health import given_params

    given = given_params(state.flow, state.run.params)
    return any(m.group(1) in given for m in TEMPLATE_RE.finditer(url))


def _json_body(seen: JsonResponse | None, step: ReadJsonStep, *, picked: bool = False) -> Any:
    """The parsed body of the response a read_json waited for, or the failure it comes to. `picked`: a
    caller's param chose the page, so a 401 may be theirs alone and must not hold the lane."""
    expected = f"a 2xx JSON response on {step.url}"
    if seen is None:
        raise StepFailure("json_not_seen", f"the page made no request on {step.url} within {step.timeout_ms} ms",
                          acted=False, transient=True, expected=expected, found="no response")
    if seen.status == 304 and not seen.body:  # the page got its data, and the same page would answer the same
        raise StepFailure("json_unreadable", "the site answered 304, so the page used its own copy and no body reached "
                          "the browser", acted=False, expected=expected, found="status 304 with no body")
    if seen.problem == "slow":
        raise StepFailure("json_slow", f"the body of the response on {step.url} did not finish in time", acted=False,
                          transient=True, expected=expected, found="a body that did not finish")
    if seen.problem == "unreadable":
        raise StepFailure("json_unreadable", "the browser no longer held the response's body", acted=False, transient=True)
    if seen.problem == "too_large":
        raise StepFailure("json_too_large", f"the response on {step.url} is too large to read", acted=False,
                          expected=expected, found="over the size cap")
    if seen.status == 429:  # the site's own stop, as a rate limited page is: the lane rests, nothing retries
        raise StepFailure("rate_limited", "the site is rate limiting this lane", acted=False, signal=Signal.RATE_LIMITED,
                          expected=expected, found="status 429")
    if seen.status == 401 and not picked:  # the session ended, not the contract. A 403 can be one caller's params
        raise StepFailure(SIGNAL_CODES[Signal.LOGGED_OUT], f"needs you: {NEEDS_YOU[Signal.LOGGED_OUT]}", acted=False,
                          signal=Signal.LOGGED_OUT, expected=expected, found="status 401")
    if not 200 <= seen.status < 300 and seen.status != 304:  # a 5xx is a bad moment: it says nothing of the contract alone
        raise StepFailure("json_status", f"the response on {step.url} was {seen.status}", acted=False,
                          transient=seen.status >= 500, expected=expected, found=f"status {seen.status}")
    if not seen.body:  # only a repeat says the site changed, and health counts that, so going back says nothing
        raise StepFailure("json_empty", f"the response on {step.url} had no body", acted=False, expected=expected,
                          found=f"status {seen.status} with no body")
    try:
        return parse_json(seen.body)
    except ValueError:  # its text can quote the body
        raise StepFailure("json_invalid", f"the response on {step.url} is not JSON", acted=False,
                          expected=expected, found="not JSON") from None


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    return {bool: "a boolean", int: "a number", float: "a number", str: "a string", list: "a list"}.get(type(value), "an object")


def _json_at(root: Any, path: str) -> tuple[Any, bool]:
    """(the value at a dotted path, whether the body has that shape). A key that is not there is
    None, and so is what lies under a null; a part that is neither an object nor null is another shape."""
    cur = root
    for part in path.split("."):
        if cur is None:
            return None, True
        if not isinstance(cur, dict):
            return None, False
        cur = cur.get(part)
    return cur, True


def _json_rows(body: Any, step: ReadJsonStep) -> list[Any]:
    rows = body if step.items == JSON_ROOT else _json_at(body, step.items)[0]
    if not isinstance(rows, list):
        raise StepFailure("output_contract_violation", f"the JSON at {step.items} is {_json_kind(rows)}, not a list (the "
                          "site may have changed shape)", acted=False, expected="a list", found=_json_kind(rows))
    return rows


def _finite(raw: float) -> bool:
    try:
        return math.isfinite(raw)
    except OverflowError:  # an integer past what a float holds
        return False


def _json_value(row: dict[str, Any], f: JsonField, shape: ExtractField, base_url: str) -> tuple[Any, bool, str | None]:
    """(value, clipped, problem) for one read_json field. Its declared type or a problem, never a
    conversion, and a problem says what kind of value came, not what it said."""
    raw, fits = _json_at(row, f.path)
    if not fits:
        return None, False, f"{f.path} is not there: a part of it is not an object"
    if raw is None:
        return None, False, None
    wrong = f"is {_json_kind(raw)}, not {'an' if f.type == 'integer' else 'a'} {f.type}"
    if f.type == "boolean":
        return (raw, False, None) if isinstance(raw, bool) else (None, False, wrong)
    if f.type == "integer":
        fits = type(raw) is int and abs(raw) < MAX_JSON_INTEGER
        return (raw, False, None) if fits else (None, False, wrong if type(raw) is not int else "is out of range")
    if f.type == "number":
        fits = type(raw) in (int, float) and _finite(raw)
        return (raw, False, None) if fits else (None, False, wrong if type(raw) not in (int, float) else "is out of range")
    if not isinstance(raw, str):
        return None, False, wrong
    try:
        raw.encode()
    except UnicodeEncodeError:  # a lone surrogate, which no answer of the node can carry
        return None, False, "is not valid text"
    value, cut, problem = _typed(raw, shape, base_url)
    return value, cut, f"does not fit its format ({f.format})" if problem else None


def _labels(outputs: dict[str, str], labels: dict[str, dict[str, list[str]]]) -> tuple[list[str], list[str]]:
    """untrusted_fields and private_fields as output paths ('items[].body') from the extract
    labels of the saved values the outputs map."""
    untrusted: list[str] = []
    private: list[str] = []
    for name, path in sorted(outputs.items()):
        root, _, rest = path.partition(".")
        lab = labels.get(root)
        if not lab:
            continue
        prefix = f"{name}.items[]." if rest == "" else f"{name}[]." if rest == "items" else None
        if prefix is None:
            continue
        untrusted += [prefix + fld for fld in lab.get("untrusted", [])]
        private += [prefix + fld for fld in lab.get("private", [])]
    return untrusted, private


def _size(value: Any) -> int:
    return len(json.dumps(value, default=str, separators=(",", ":")).encode())


def _cap(data: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    """Trim the longest lists until the data fits OUTPUT_CAP: (data, trimmed), or (None, _)."""
    if _size(data) <= OUTPUT_CAP:
        return data, False
    data = json.loads(json.dumps(data, default=str))
    while _size(data) > OUTPUT_CAP:
        lists = [v for v in data.values() if isinstance(v, list) and v]
        lists += [v["items"] for v in data.values() if isinstance(v, dict) and isinstance(v.get("items"), list)
                  and v["items"]]
        if not lists:
            return None, True
        max(lists, key=len).pop()
    return data, True
