"""A run and its approval: a person's cancel and decision, the approvals service's answer, an edit
made on the card, and the grant checks of a waiting run.

One approval releases exactly one run; a second run parked on it asks again.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from openajar.core.aio import maybe_await
from openajar.core.errors import (
    InvalidRequest,
    NotFound,
    OpenajarError,
    PolicyDenied,
)
from openajar.core.models import Principal, RunKind, RunStatus
from openajar.runner.execution import Journal
from openajar.runner.records import Run
from openajar.runner.run_rules import APPROVED, CONSUMED, DENIED, EXPIRED, GRANT_CODES, RETIRED_SOURCES

log = logging.getLogger("openajar.runner")


class ApprovalMixin:
    """cancel(), decide(), the approval polling and waits, and the grant checks of RunnerService."""

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
        releases this run was approved with an edit (openajar.policy.approvals), else None."""
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
        from openajar.policy.approval_binding import content_digest

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

    def _retired(self, run: Run, now: datetime) -> bool:
        """A run of a retired source (RETIRED_SOURCES) ends here, cancelled, and never starts."""
        if run.source not in RETIRED_SOURCES:
            return False
        run.status, run.finished_at = RunStatus.CANCELLED, now
        run.error = {"code": "source_retired",
                     "message": f"runs from {run.source} no longer run on this node, so it was not sent"}
        if self.store.save_run(run, expect={RunStatus.QUEUED}):
            self._emit(run)
        return True

    def _trace_edit(self, run: Run, by: str, names: list[str]) -> None:
        write = getattr(self._service("traces"), "write", None)
        if not callable(write):
            return
        try:
            from openajar.policy.traces import TraceEvent

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
            from openajar.policy.errors import Denied

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
        except OpenajarError:
            return None

    def _check_binding(self, grant_id: str | None, **run: Any) -> None:
        """A grant pinned to one approved run (openajar.policy approved_run_grant, the agenda's) carries
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
