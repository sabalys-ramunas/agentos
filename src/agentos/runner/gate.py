"""The gate right before a run acts: its grant again, the approval it carries, the pace of a
real account, ask first, and the connector's surface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agentos.core.aio import maybe_await
from agentos.core.errors import (
    ApprovalPending,
    NotFound,
    PolicyDenied,
)
from agentos.core.models import Effect, RunKind, RunStatus
from agentos.runner.execution import Outcome
from agentos.runner.owner import TRY_SOURCE
from agentos.runner.records import Run
from agentos.runner.run_rules import COMMIT_EFFECT_NAMES, bind_workflow, is_person, workflow_of


class GateMixin:
    """_gate() and the pace checks of RunnerService (agentos.runner.runner)."""

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
        """What an approval of this queued run binds (agentos.policy.approval_binding.ActionBinding), made
        the way its card was: the content the policy gate binds for any run. Raises PolicyDenied when a surface's binding changed."""
        from agentos.policy.approval_binding import ActionBinding

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
        which names the flow or connector it runs) must still cover exactly
        what the run will do (action_binding_problem), else it fails and nothing is done."""
        from types import SimpleNamespace

        from agentos.policy.approval_binding import action_binding_problem

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
        problem = "is gone" if ap is None else action_binding_problem(ap, binding)
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
                                 trial=run.source == TRY_SOURCE, person=is_person(run.principal))
        except ConnectorBroken as e:
            return Outcome("defer", reason=e.message, hold="connector_broken")
        except PaceLimited as e:
            if again:
                proof = run.proof or {}
                return Outcome("finish", RunStatus.PARTIAL if proof.get("acted") or proof.get("writes") else RunStatus.FAILED,
                               error={k: v for k, v in (run.error or {}).items() if k != "retrying"})
            return Outcome("defer", reason=e.message, hold="pace", until=e.retry_at)
        return None
