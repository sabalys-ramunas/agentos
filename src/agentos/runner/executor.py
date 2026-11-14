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

import logging
import random
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentos.core.driver import DriverFactory, FlowDriver
from agentos.core.errors import (
    AgentPaused,
    ApprovalPending,
    HumanInControl,
    LaneBusy,
    LaneQuarantined,
    PolicyDenied,
)
from agentos.core.locator import Locator
from agentos.core.models import RunStatus, Signal
from agentos.core.template import TemplateError, render
from agentos.runner import signed_out
from agentos.runner.connector_steps import ConnectorStepsMixin
from agentos.runner.execution import (
    CONTRACT_CAUSES,
    HOLDS,
    NEEDS_YOU,
    SIGNAL_CODES,
    ExecState,
    Journal,
    Outcome,
    RunControl,
    StepFailure,
    StopRun,
    check_pattern,
    check_patterns,
    classify,
    connector_found,
    guarded,
    learned_flow,
    match_for,
    public_readback,
)
from agentos.runner.flows import FlowService
from agentos.runner.owner import TRY_SOURCE
from agentos.runner.proof import ResolvedCheck, run_check
from agentos.runner.records import Run, StepRecord, StepStatus
from agentos.runner.steps import (
    AssertIdentityStep,
    BranchStep,
    ExtractStep,
    ForEachStep,
    NavigateStep,
    PreflightStep,
    ReadbackStep,
    ReadJsonStep,
    is_idempotent,
    iter_steps,
    static_path,
    step_at,
    watchers,
)
from agentos.runner.util import Clock, clip, url_problem

log = logging.getLogger("agentos.runner")


class FlowExecutor(ConnectorStepsMixin):
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
        state = ExecState(run=run, flow=flow, records=records,
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
    async def _scope_commit(state: ExecState) -> None:
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
                request["match"] = match_for(request["match"], getattr(state, "scope", None) or {})
        await fn(state.flow.id, request)

    @staticmethod
    def _shown(outcome: Outcome, state: ExecState) -> Outcome:
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

    def _session_error(self, e: Exception, state: ExecState) -> Outcome:
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
        f = classify(e, "find")
        err = f.to_dict()
        err["message"] = f"could not open the lane: {err['message']}"
        if f.transient:
            return Outcome("retry", self._final_status(state), error=err, proof=self._proof(state))
        return Outcome("finish", self._final_status(state), error=err, proof=self._proof(state))

    # ------------------------------------------------------------ traversal

    async def _run_all(self, state: ExecState, journal: Journal, control: RunControl) -> Outcome:
        try:
            await self._reposition(state)
            await self._run_list(state.flow.steps, "", state, journal, control)
        except StopRun as s:
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

    async def _reposition(self, state: ExecState) -> None:
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

    async def _run_list(self, steps: list[Any], prefix: str, state: ExecState, journal: Journal,
                        control: RunControl) -> None:
        for i, step in enumerate(steps):
            path = f"{prefix}{i}"
            rec = state.records.get(path)
            if rec and rec.status in (StepStatus.DONE, StepStatus.SKIPPED) and not self._recheck(steps, i, prefix, state):
                self._apply(step, path, rec.result or {}, state)
            else:
                rec = await self._run_journaled(steps, i, prefix, state, journal, control)
            await self._descend(step, path, rec, state, journal, control)

    async def _run_journaled(self, steps: list[Any], i: int, prefix: str, state: ExecState, journal: Journal,
                             control: RunControl) -> StepRecord:
        """Run one step, journaled as started before it acts and as done or failed after."""
        step, path = steps[i], f"{prefix}{i}"
        control.check()
        journal.start(path, i, step.type)
        try:
            try:
                self._watch(steps, i, prefix, state)
                result = await self._run_step(step, path, state, control)
            except (StepFailure, StopRun):
                raise
            except Exception as e:
                raise classify(e, "act") from e
        except StepFailure as f:
            await self._step_failed(f, step, path, state, journal)
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
        return StepRecord(run_id=state.run.id, path=path, status=StepStatus.DONE, result=result)

    async def _step_failed(self, f: StepFailure, step: Any, path: str, state: ExecState, journal: Journal) -> None:
        """Count what a failed step may have done and journal it: a step that waits for an approval or
        for a person holding the lane did not happen, so it goes back to pending and runs on resume."""
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

    async def _descend(self, step: Any, path: str, rec: StepRecord, state: ExecState, journal: Journal,
                       control: RunControl) -> None:
        """A branch runs the arm it took; a for_each runs its steps once per item."""
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
    def _recheck(steps: list[Any], i: int, prefix: str, state: ExecState) -> bool:
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

    def _apply(self, step: Any, path: str, result: dict[str, Any], state: ExecState) -> None:
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
    def _save_vars(step: Any, path: str, saved: dict[str, Any], state: ExecState) -> None:
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

    def _value(self, step: Any, state: ExecState, literal: str) -> str:
        if step.param is not None:
            value = state.scope.get(step.param)
            if value is None:
                raise StepFailure("missing_param", f"param {step.param!r} has no value", acted=False)
            return str(value)
        try:
            return render(getattr(step, literal), state.scope)
        except TemplateError as e:
            raise StepFailure("missing_param", e.message, acted=False) from e

    def _loc(self, locator: Locator, state: ExecState) -> Locator:
        try:
            loc = locator.render(state.scope)
        except TemplateError as e:
            raise StepFailure("missing_param", e.message, acted=False, locator=locator) from e
        check_patterns(loc)
        return loc

    def _url(self, template: str, state: ExecState) -> str:
        try:
            url = render(template, state.scope, url=True)
        except TemplateError as e:
            raise StepFailure("missing_param", e.message, acted=False) from e
        problem = url_problem(template, url, state.flow.domain)
        if problem:
            raise StepFailure("url_outside_domain", f"refused to load it: {problem}", acted=False,
                              expected=f"a URL inside {state.flow.domain or 'the flow domain'}", found=url)
        return url

    async def _run_step(self, step: Any, path: str, state: ExecState, control: RunControl) -> dict[str, Any]:
        d = state.driver
        t = step.type
        if t == "navigate":
            url = self._url(step.url, state)
            nav = await guarded(d.navigate(url), "act")
            self._check_signal(nav.signal, expected=f"the page at {url}")
            return {"url": nav.url, "status": nav.status, "signal": str(nav.signal)}
        if t == "pause":
            return await self._pause(step, control)
        if t == "wait_for":
            return await self._wait_for(step, state)
        if t == "readback":
            return await self._readback_step(step, path, state)
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
            el = await guarded(d.find(loc, timeout_ms=step.timeout_ms), "find", loc)
            await guarded(d.scroll_to(el), "act", loc)
            return {"url": await d.current_url()}
        return await self._action(step, path, state)

    async def _pause(self, step: Any, control: RunControl) -> dict[str, Any]:
        """A random pause between min_ms and max_ms, in pieces of a second so a stop lands soon."""
        ms = self.rng.randint(step.min_ms, step.max_ms)
        remaining = ms / 1000
        while remaining > 0:
            control.check()
            chunk = min(remaining, 1.0)
            await self.clock.sleep(chunk)
            remaining -= chunk
        return {"slept_ms": ms}

    async def _wait_for(self, step: Any, state: ExecState) -> dict[str, Any]:
        d = state.driver
        loc = self._loc(step.locator, state)
        if step.state == "visible":
            await guarded(d.find(loc, timeout_ms=step.timeout_ms), "find", loc)
        elif step.state == "gone":
            await self._wait_gone(d, loc, step.timeout_ms)
        out: dict[str, Any] = {"url": await d.current_url()}
        if step.save_as:
            st = await guarded(d.read_state(loc), "find", loc)
            out["vars"] = {step.save_as: clip(st.model_dump(), max_str=300)}
        return out

    async def _readback_step(self, step: Any, path: str, state: ExecState) -> dict[str, Any]:
        """A readback step on its own: it fails when what it reads is not so."""
        d = state.driver
        rb = await self._readback(step, None, path, state)
        if rb["passed"] is False:
            raise StepFailure("readback_failed", f"readback failed: {rb['check_text']}", acted=False,
                              locator=rb["_locator"], expected=rb["expected"], found=rb["found"],
                              readback=public_readback(rb))
        out = {"url": await d.current_url(), "readback": public_readback(rb), "_check": rb["_check"]}
        if step.save_as and step.locator is not None:
            st = await guarded(d.read_state(self._loc(step.locator, state)), "find", step.locator)
            out["vars"] = {step.save_as: clip(st.model_dump(), max_str=300)}
        elif step.save_as:  # a url readback saves the page it proved (a permalink: `post.url`)
            # found only when it proved it: one that could not be checked (passed None) saves the
            # page as not found, so missing proof never reads as a proved permalink
            proved = rb["passed"] is True
            out["vars"] = {step.save_as: {"url": out["url"], "found": proved, "count": int(proved)}}
        return out

    async def _action(self, step: Any, path: str, state: ExecState) -> dict[str, Any]:
        """An input step. Its inline readback is resolved (params filled in, patterns checked) before it
        acts, and run after: the action ran either way, so a failed readback fails with acted=True."""
        d = state.driver
        t = step.type
        loc = self._loc(step.locator, state) if step.locator is not None else None
        check = self._resolve(step.readback, loc, path, state) if step.readback is not None else None
        el = await guarded(d.find(loc, timeout_ms=step.timeout_ms), "find", loc) if loc is not None else None
        if t == "click":
            await guarded(d.click(el), "act", loc)
        elif t == "type":
            await guarded(d.type(el, self._value(step, state, "text")), "act", loc)
        elif t == "fill":
            await guarded(d.fill(el, self._value(step, state, "text")), "act", loc)
        elif t == "select":
            await guarded(d.select(el, self._value(step, state, "option")), "act", loc)
        elif t == "upload":
            await guarded(d.upload(el, self._value(step, state, "param")), "act", loc)
        elif t == "press":
            await guarded(d.press(step.key, el), "act", loc)
        else:  # pragma: no cover - the model validates step types
            raise StepFailure("invalid", f"unknown step type {t}")
        out = {"acted": True, "url": await guarded(d.current_url(), "act", loc)}
        if check is not None:
            try:
                rb = await self._run_resolved(check, state)
            except Exception as e:
                raise StepFailure("readback_error", f"the action ran, but its readback could not run: "
                                  f"{type(e).__name__}: {e}", acted=True) from e
            if rb["passed"] is False:
                raise StepFailure("readback_failed", f"the action ran, but its readback failed: {rb['check_text']}",
                                  acted=True, locator=rb["_locator"], expected=rb["expected"],
                                  found=rb["found"], readback=public_readback(rb))
            out["readback"] = public_readback(rb)
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
            st = await guarded(d.read_state(loc), "find", loc)
            if st.count == 0:
                return
            if self.clock.monotonic() >= deadline:
                raise StepFailure("timeout", f"{loc.describe()} did not go away", acted=False, transient=True,
                                  locator=loc, expected="no match", found=f"{st.count} matches")
            await self.clock.sleep(self.poll_s)

    def _resolve(self, spec: Any, default_locator: Locator | None, path: str, state: ExecState) -> ResolvedCheck:
        try:
            loc = spec.locator.render(state.scope) if spec.locator is not None else default_locator
            cond = spec.condition.render(state.scope)
        except (TemplateError, ValueError) as e:
            raise StepFailure("missing_param", str(e), acted=False) from e
        url = self._url(spec.url, state) if spec.url else None
        if loc is None and cond.kind != "url":
            raise StepFailure("invalid", "the readback has no target", acted=False)
        if loc is not None:
            check_patterns(loc)
        if cond.matches is not None:
            check_pattern(cond.matches, "the readback's pattern")
        return ResolvedCheck(path=path, locator=loc if cond.kind != "url" else None, condition=cond,
                             url=url, timeout_ms=spec.timeout_ms)

    async def _readback(self, spec: Any, default_locator: Locator | None, path: str, state: ExecState) -> dict:
        return await self._run_resolved(self._resolve(spec, default_locator, path, state), state)

    async def _run_resolved(self, check: ResolvedCheck, state: ExecState) -> dict:
        """Run a check. The result is what is shown (hidden params read `${name}`); `_check`
        keeps the resolved check verbatim for a later re-check (private step state)."""
        result = await run_check(state.driver, check, self.clock, poll_s=self.poll_s)
        data = clip(result.model_dump(mode="json", exclude={"check"}), max_str=500)
        if state.flow.connector is not None:
            data["found"] = connector_found(check, result.found, result.passed)
        raw = check.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        data["check"] = raw
        data["check_text"] = check.describe()
        data = state.scrub(data)
        data["_locator"] = check.locator
        data["_check"] = raw
        return data

    async def _branch(self, step: BranchStep, path: str, state: ExecState) -> str:
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


    # ------------------------------------------------------------ outcomes

    @staticmethod
    def _final_status(state: ExecState) -> RunStatus:
        """partial once anything may have been published (a flagged write, or any acted input
        step the owner did not mark `publishes: false`), so a re-run is never a blind repeat."""
        return RunStatus.PARTIAL if state.writes_done or state.acted else RunStatus.FAILED

    def _proof(self, state: ExecState, screenshot: str | None = None) -> dict[str, Any]:
        return {"readbacks": state.readbacks, "url": state.scrub(state.last_url), "writes": state.writes_done,
                "acted": state.acted, "screenshot": screenshot}

    async def _completion_outcome(self, state: ExecState) -> Outcome:
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

    async def _signed_out(self, state: ExecState) -> dict[str, Any]:
        """The AUD-22 check of a published object, after its live readback passed (see
        agentos.runner.signed_out). The url and text go to the private step state for re-checks."""
        check, problem = signed_out.resolve(state.meta["signed_out"], state.scope, state.flow.domain)
        if check is None:
            return state.scrub(signed_out.record(None, note=problem, clock=self.clock))
        if state.journal is not None:
            state.journal.store.put_step_state(state.run.id, signed_out.CHECK_PATH, check)
        return state.scrub(await signed_out.run(state.driver, check, self.clock))

    @staticmethod
    def _verdict(state: ExecState, proof: dict[str, Any]) -> Outcome:
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

    async def _failure_outcome(self, f: StepFailure, state: ExecState) -> Outcome:
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


