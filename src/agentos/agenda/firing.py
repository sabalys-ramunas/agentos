"""Firing an approved entry when it is due, whose authority the run carries (a standing grant, the
approval, a freeze), and following the run to its end.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime

from agentos.agenda.models import (
    ActionKind,
    Entry,
    EntryState,
    action_hash,
)
from agentos.agenda.rules import CONNECTOR_RETRY, OWNER_GRANT, PERSON_VIAS
from agentos.agenda.timeline import run_proof
from agentos.core.errors import (
    AgentosError,
    PolicyDenied,
)
from agentos.core.models import RunStatus
from agentos.runner.records import SUCCEEDED, TERMINAL, Run
from agentos.runner.util import clip_text

log = logging.getLogger("agentos.agenda")


class FiringMixin:
    """tick(), firing and following of AgendaService (agentos.agenda.service)."""

    async def tick(self, stats: dict[str, int] | None = None) -> dict[str, int]:
        """One pass, from the runner's scheduler loop: decisions, due entries, running ones, and
        windows that closed."""
        out = {"fired": 0, "finished": 0, "expired": 0, "waiting": 0, "cancelled": 0}
        self.sync()
        try:
            out["cancelled"] = await self.sweep_revoked()
        except Exception:
            log.exception("agenda: the sweep for revoked grants failed")
        now = self.clock.now()
        for e in self.store.list(states=[EntryState.DRAFT, EntryState.PROPOSED]):
            if e.when.not_after is not None and now > e.when.not_after:
                why = ("nobody approved it before its window closed" if e.state == EntryState.PROPOSED
                       else "it was never proposed, and its window closed")
                out["expired"] += self._finish(e, EntryState.EXPIRED, why)
        for e in self.store.list(states=[EntryState.SCHEDULED]):
            try:
                await self._fire_due(e, out)
            except Exception:
                log.exception("agenda entry %s could not fire", e.id)
        for e in self.store.list(states=[EntryState.RUNNING]):
            try:
                await self._follow(e, out)
            except Exception:
                log.exception("agenda entry %s: following its run failed", e.id)
        if stats is not None:
            stats["fired"] = stats.get("fired", 0) + out["fired"]
        return out

    def _finish(self, e: Entry, state: EntryState, reason: str | None, *, proof: dict | None = None) -> int:
        self._spend_authority(e, f"the entry is {state.value}")
        prev, now = e.state, self.clock.now()
        e.state, e.reason, e.finished_at, e.updated_at, e.retry_at = state, reason, now, now, None
        if proof is not None:
            e.proof = proof
        e.note(now, state.value, **({"reason": clip_text(reason, 200)} if reason else {}))
        return int(self.store.put(e, expect={prev}))

    def key(self, e: Entry) -> str:
        """The idempotency key of an entry's one run for one approved action."""
        return f"agenda:{e.id}:{(e.approved_hash or '')[7:23]}"

    async def _fire_due(self, e: Entry, out: dict[str, int]) -> None:
        """Start the run of an approved entry when it is due, or say why it waits, is skipped or ends.
        Each check below can only hold it back; none can make it run sooner."""
        now = self.clock.now()
        if self._outside_window(e, now, out):
            return
        if e.approved_hash != action_hash(e.action, e.when):  # never runs what nobody let run
            prev = e.state
            e.state, e.reason, e.approved_hash = EntryState.PROPOSED, "it changed after it was approved", None
            self.store.put(e, expect={prev})
            await self.propose(e.id)
            return
        frozen = self._frozen(e)
        if frozen is not None:  # an explicit stop: nothing planned there runs, whoever approved it
            out["finished"] += self._finish(e, EntryState.FAILED, frozen)
            return
        standing = self._standing(e.grant_id)
        if standing["state"] == "revoked":
            out["finished"] += self._cancel_revoked(e, standing["reason"])
            return
        if e.action.kind == ActionKind.ROUTE:
            self._fire_route(e, now)
            out["fired"] += 1
            return
        if self._paced_out(e, now, out):
            return
        run = self._submit_entry(e, standing, now, out)
        if run is None:
            return
        prev = e.state
        e.state, e.run_id, e.retry_at, e.updated_at = EntryState.RUNNING, run.id, None, now
        e.reason = None
        e.note(now, "fired", run_id=run.id, **({"under": e.authority["approver"].get("id")} if e.authority else {}))
        if self.store.put(e, expect={prev}):
            out["fired"] += 1
            if e.authority:
                self._audit_authority(e, run.id)
        if run.terminal:
            self._run_finished(run)

    def _outside_window(self, e: Entry, now: datetime, out: dict[str, int]) -> bool:
        """Not yet (before its window, or waiting to try again), or never (its window closed)."""
        start = e.when.not_before
        if start is not None and start > now:
            return True
        if e.retry_at is not None and e.retry_at > now:
            out["waiting"] += 1
            return True
        end = e.when.not_after
        if end is not None and now > end:
            why = "its window closed before it ran (the node was down or busy at the time)"
            if e.retry_at is not None:
                why = f"its window closed while it waited: {e.reason or 'the lane was not ready'}"
            out["expired"] += self._finish(e, EntryState.SKIPPED if e.retry_at else EntryState.EXPIRED, why)
            return True
        return False

    def _paced_out(self, e: Entry, now: datetime, out: dict[str, int]) -> bool:
        """The lane's posting pace: wait for its next slot, or skip when that is past the window."""
        end = e.when.not_after
        paced = self._pace(e.action.effect, e.action.acts_on, now)
        if paced is None:
            return False
        nxt, words = paced
        if nxt is None or (end is not None and nxt > end):
            out["finished"] += self._finish(e, EntryState.SKIPPED, f"the lane's posting pace kept it out of its "
                                            f"window: {words}")
            return True
        self._wait(e, nxt, f"waiting for the lane's pace: {words}")
        out["waiting"] += 1
        return True

    def _submit_entry(self, e: Entry, standing: dict[str, str], now: datetime, out: dict[str, int]) -> Run | None:
        """Queue the entry's run under the grant it carries. None when it could not start: a connector's
        pace or a broken connector makes it wait (or skips it past its window), anything else fails it."""
        from agentos.runner.connectors.surfaces import ConnectorBroken, PaceLimited

        end = e.when.not_after
        via = (e.approval or {}).get("via")
        try:
            grant_id = self._run_grant(e, standing, now)
        except PolicyDenied as exc:  # the drafter's grant expired, and the approver no longer stands behind it
            why = getattr(exc, "detail", None) or exc.message
            out["finished"] += self._finish(e, EntryState.FAILED, f"it could not start: the grant it was drafted "
                                            f"under expired ({standing['reason']}), and {why}")
            return None
        try:
            run, _ = self.runner.submit(
                flow_id=e.action.target, version=e.action.version, params=dict(e.action.inputs),
                lane=None if e.action.surface else e.action.lane, surface=e.action.surface,
                idempotency_key=self.key(e), principal=e.principal or {"kind": "flow", "id": f"agenda:{e.id}"},
                source=f"agenda:{e.id}", trigger={"kind": "agenda", "entry": e.id, "slot": now.isoformat()},
                authorized=via in PERSON_VIAS, grant_id=grant_id)
        except PaceLimited as exc:  # a connector's own pace on its surface
            retry = exc.retry_at or now + CONNECTOR_RETRY
            if end is not None and retry > end:
                out["finished"] += self._finish(e, EntryState.SKIPPED, f"the connector's pace kept it out of its "
                                                f"window: {exc.message}")
            else:
                self._wait(e, retry, f"waiting for the connector's pace: {exc.message}")
            return None
        except ConnectorBroken as exc:
            if end is not None and now + CONNECTOR_RETRY > end:
                out["finished"] += self._finish(e, EntryState.SKIPPED, f"held: {exc.message}")
            else:
                self._wait(e, now + CONNECTOR_RETRY, f"held: {exc.message}")
            return None
        except AgentosError as exc:  # the grant was revoked, the flow is gone, the inputs went stale
            out["finished"] += self._finish(e, EntryState.FAILED, f"it could not start: {exc.code}: {exc.message}")
            return None
        return run

    # ------------------------------------------------------------ whose authority a run carries

    def _standing(self, grant_id: str | None) -> dict[str, str]:
        """policy.grant_standing for the drafter's grant; live for the owner or without a policy."""
        fn = getattr(self._svc("policy"), "grant_standing", None)
        if not callable(fn) or not grant_id or grant_id == OWNER_GRANT:
            return {"state": "live", "reason": "live"}
        return fn(grant_id)

    def _frozen(self, e: Entry) -> str | None:
        """Why a freeze stops this entry, in words, or None."""
        freeze = self._svc("freeze")
        found = getattr(freeze, "frozen_for", None)
        rec = found(e.action.acts_on) if callable(found) else None
        if rec is None:
            return None
        where = {"mesh": "the whole mesh", "node": "this node"}.get(rec.scope, f"{rec.scope} {rec.target}")
        why = f": {rec.reason}" if getattr(rec, "reason", None) else ""
        return (f"{e.action.acts_on or e.workspace} is frozen ({where}{why}); a freeze stops everything planned "
                "there, so it did not run")

    def _run_grant(self, e: Entry, standing: dict[str, str], now: datetime) -> str | None:
        """The grant the run goes under: the drafter's, unless it has merely expired and a person
        approved this exact action, in which case a grant for this one run derived from that person's
        authority (see the module docstring). Raises PolicyDenied when the approver no longer may."""
        policy = self._svc("policy")
        ap = e.approval or {}
        approver = ap.get("approver")
        if (standing["state"] != "expired" or policy is None or ap.get("via") != "person" or not approver
                or ap.get("hash") != e.approved_hash or not callable(getattr(policy, "approved_run_grant", None))):
            return e.grant_id  # the drafter's grant: live, or refused by the runner as before
        a = e.authority
        if a and a.get("hash") == e.approved_hash and self._standing(a.get("grant_id"))["state"] == "live":
            problem = policy.approver_problem(approver, e.action.effect, e.action.acts_on or "", e.workspace)
            if problem:
                raise PolicyDenied(problem)
            return a["grant_id"]  # minted on an earlier tick (a restart before the run was recorded)
        g = policy.approved_run_grant(
            approver=approver, entry=e.id, flow_id=e.action.target, version=e.action.version,
            lane=e.action.lane or e.action.acts_on or "", target=e.action.acts_on or "", params=dict(e.action.inputs),
            key=self.key(e), effect=e.action.effect,
            not_before=e.when.not_before.timestamp() if e.when.not_before else None,
            not_after=e.when.not_after.timestamp() if e.when.not_after else None, drafted_under=e.grant_id)
        prev = e.state
        e.authority = {"grant_id": g.id, "approver": approver, "hash": e.approved_hash, "at": now.isoformat(),
                       "why": standing["reason"]}
        e.note(now, f"the grant it was drafted under expired; it runs under {approver.get('id')}'s approval",
               authority=g.id)
        self.store.put(e, expect={prev})
        return g.id

    def _spend_authority(self, e: Entry, why: str) -> None:
        """Revoke the one-run grant an entry's run went under, once that run is over (or will not be)."""
        gid = (e.authority or {}).get("grant_id")
        policy = self._svc("policy")
        if not gid or policy is None or (e.authority or {}).get("spent"):
            return
        try:
            policy.revoke(gid, by="agenda", reason=f"used: {why}")
        except AgentosError:
            pass  # already revoked (a member removed, a freeze), or gone
        e.authority = {**(e.authority or {}), "spent": True}

    def _audit_authority(self, e: Entry, run_id: str) -> None:
        """One audit row naming both: the agent that drafted the entry and the person whose approval
        the run went under (agentos.policy.traces, its own fields)."""
        traces = self._svc("traces")
        if traces is None or not e.authority:
            return
        from agentos.policy.traces import TraceEvent

        approver = e.authority.get("approver") or {}
        drafter = e.principal.get("id") or "the owner"
        try:
            traces.write(TraceEvent(
                kind="agenda_run", tool="agenda", principal=e.principal.get("id"),
                principal_kind=e.principal.get("kind"), grant_id=e.authority.get("grant_id"),
                grant_path=f"/{e.authority.get('grant_id')}/",
                lane=e.action.acts_on, workspace=e.workspace, effect=e.action.effect, run_id=run_id,
                summary=clip_text(f"agenda entry {e.id} drafted by {drafter} ran under the approval of "
                                  f"{approver.get('id')}: the grant it was drafted under had expired", 200),
                args={"entry": e.id, "drafted_by": {k: e.principal.get(k) for k in ("kind", "id", "owner")},
                      "drafted_under": e.grant_id, "approved_by": approver,
                      "authority": e.authority.get("grant_id"), "approval_id": (e.approval or {}).get("id")}))
        except Exception:
            log.warning("agenda entry %s: could not write the audit row for its run", e.id, exc_info=True)

    def _cancel_revoked(self, e: Entry, why: str) -> int:
        prev, now = e.state, self.clock.now()
        self._withdraw(e)
        self._spend_authority(e, "the grant it was drafted under was revoked")
        e.state, e.finished_at, e.updated_at, e.retry_at = EntryState.CANCELLED, now, now, None
        e.reason = clip_text(f"{why}, so it can never run; nothing ran", 300)
        e.note(now, "cancelled: the grant it was drafted under was revoked")
        return int(self.store.put(e, expect={prev}))

    async def sweep_revoked(self) -> int:
        """Cancel the entries a revoked grant drafted that have not run (draft, proposed, scheduled),
        withdrawing a waiting request, so nothing waits that can never run. A running entry that goes
        under a person's approval is stopped too; any other running entry's run is refused by the
        runner when it re-checks its grant. Expired grants are left alone: a person's approval may
        still carry the run (see the module docstring). Returns how many were cancelled."""
        fn = getattr(self._svc("policy"), "grant_standing", None)
        if not callable(fn):
            return 0
        seen: dict[str, dict[str, str]] = {}
        n = 0
        for e in self.store.list(states=[EntryState.DRAFT, EntryState.PROPOSED, EntryState.SCHEDULED,
                                         EntryState.RUNNING]):
            if not e.grant_id or e.grant_id == OWNER_GRANT:
                continue
            st = seen.get(e.grant_id) or seen.setdefault(e.grant_id, fn(e.grant_id))
            if st["state"] != "revoked":
                continue
            if e.state == EntryState.RUNNING:
                if not e.authority or not e.run_id:
                    continue
                self._spend_authority(e, "the grant it was drafted under was revoked")
                self.store.put(e, expect={EntryState.RUNNING})
                with contextlib.suppress(AgentosError):
                    await self.runner.cancel(e.run_id, by="agenda")
                continue
            n += self._cancel_revoked(e, st["reason"])
        return n

    def _wait(self, e: Entry, until: datetime, why: str) -> None:
        prev, now = e.state, self.clock.now()
        first = e.retry_at is None or e.reason != why
        e.retry_at, e.reason, e.updated_at = until, why, now
        if first:
            e.note(now, why, until=until.isoformat())
        self.store.put(e, expect={prev})

    def _fire_route(self, e: Entry, now: datetime) -> None:
        events = self._events()
        r = events.get_route(e.action.target)
        text = e.title + (f"\n\n{e.notes}" if e.notes else "")
        event, _ = events.emit({"source": "cron", "kind": "cron.fired", "workspace": r.workspace,
                                "lane": e.action.lane, "dedupe_key": self.key(e),
                                "facts": {"agenda_entry": e.id, "slot": (e.when.not_before or now).isoformat()},
                                "content": {"text": text, "origin": f"agenda entry {e.id}"}}, route_id=r.id)
        prev = e.state
        e.state, e.event_id, e.updated_at, e.reason = EntryState.RUNNING, event.id, now, None
        e.note(now, "fired", event_id=event.id)
        self.store.put(e, expect={prev})

    # ------------------------------------------------------------ following

    async def _follow(self, e: Entry, out: dict[str, int]) -> None:
        if e.action.kind == ActionKind.ROUTE:
            out["finished"] += self._follow_route(e)
            return
        run = self.runner.store.get_run(e.run_id) if e.run_id else None
        if run is None:
            out["finished"] += self._finish(e, EntryState.FAILED, "its run is gone from this node")
            return
        if run.terminal:
            out["finished"] += self._run_finished(run)
            return
        end, now = e.when.not_after, self.clock.now()
        if end is not None and now > end and run.status in (RunStatus.QUEUED, RunStatus.WAITING_APPROVAL):
            why = (run.error or {}).get("message") if run.status == RunStatus.QUEUED else "nobody approved its run"
            # the entry first: the cancelled run's finish hook then leaves it as skipped
            out["finished"] += self._finish(e, EntryState.SKIPPED, f"its window closed before its run could start: "
                                            f"{why or 'the lane was not ready'}")
            await self.runner.cancel(run.id, by="agenda")
            return
        summary = self.runner._summary(run)
        if summary != e.reason:
            prev = e.state
            e.reason, e.updated_at = summary, now
            self.store.put(e, expect={prev})

    def _run_finished(self, run: Run) -> int:
        """A run ended (runner on_finished, or the tick): its entry takes its outcome and proof."""
        if not (run.source or "").startswith("agenda:") or run.status not in TERMINAL:
            return 0
        e = self.store.by_run(run.id)
        if e is None or e.state != EntryState.RUNNING:
            return 0
        proof = run_proof(run, self.runner)
        if run.status in SUCCEEDED:
            return self._finish(e, EntryState.DONE, self.runner._summary(run), proof=proof)
        if run.status == RunStatus.CANCELLED:
            return self._finish(e, EntryState.CANCELLED, "its run was cancelled", proof=proof)
        err = run.error or {}
        why = f"{err.get('code', run.status)}: {err.get('message', '')}".strip(": ")
        return self._finish(e, EntryState.FAILED, clip_text(why, 300), proof=proof)

    def _follow_route(self, e: Entry) -> int:
        events = self._events()
        deliveries = events.deliveries_for(e.event_id) if e.event_id else []
        if not deliveries:
            return self._finish(e, EntryState.FAILED, "the trigger took no delivery (it is turned off, or no longer "
                                "takes timed events)")
        if not all(d.state.terminal for d in deliveries):
            return 0
        proof = {"event_id": e.event_id, "deliveries": [
            {"id": d.id, "state": d.state.value, "target": d.target_kind, "ref": d.target_ref, "outcome": d.outcome,
             "reason": d.reason} for d in deliveries]}
        states = {d.state.value for d in deliveries}
        if "done" in states:
            return self._finish(e, EntryState.DONE, "the trigger delivered it", proof=proof)
        if states == {"dropped"}:
            why = next((d.reason for d in deliveries if d.reason), "a control dropped it")
            return self._finish(e, EntryState.SKIPPED, f"the trigger's controls dropped it: {why}", proof=proof)
        why = next((d.reason for d in deliveries if d.reason), "its delivery failed")
        return self._finish(e, EntryState.FAILED, why, proof=proof)
