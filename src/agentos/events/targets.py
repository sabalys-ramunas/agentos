"""The four targets a delivery can wake: a flow, a brain task, an agent lane turn, a workflow.

Each target reports one Handoff per attempt:

  delivered  handed over; `ref` is the run, task or turn id (the outcome comes later)
  wait       not now, with the reason (no agent service yet, the agent lane is attached)
  retry      this attempt failed in a way a later attempt may not (counts toward max_attempts)
  fail       refused for good (the grant was revoked, the flow is gone): a dead letter at once

and, for flows and brain tasks, `poll` turns the target's own status into a Finish once it
has one. An agent lane reports its own end through EventsPort.delivery_finished.

Flow params come from the first event's facts only (`${facts.x}`); content never fills a
param, so page text cannot write a caption. Brain and agent targets get the wake message
(agentos.events.frame) and a grant narrowed by the route. A workflow gets the event as its
trigger's data, with outside content marked untrusted: it may fill a step, and the workflow's own
checks stop it deciding anything (docs/WORKFLOWS.md).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from agentos.agents.ports import TurnBudget, TurnGrant, TurnRequest
from agentos.core.errors import AgentosError, InvalidRequest, NotFound, PolicyDenied, ServiceUnavailable
from agentos.core.models import Effect, Principal, RunStatus
from agentos.events import frame
from agentos.events.models import AgentTarget, BrainTarget, Delivery, Event, FlowTarget, Route, WorkflowTarget
from agentos.runner.util import maybe_await, render

log = logging.getLogger("agentos.events")

# a failed run with one of these codes is the owner's decision or a permanent refusal: never retried
OWNER_ENDED = {"denied", "approval_expired", "cancelled"}
PERMANENT = {"grant_revoked", "grant_expired", "grant_unknown", "grant_not_yet_valid", "lane_not_found"}


@dataclass
class Handoff:
    kind: Literal["delivered", "wait", "retry", "fail"]
    reason: str | None = None
    ref: str | None = None
    until: datetime | None = None
    grant_id: str | None = None


@dataclass
class Finish:
    kind: Literal["done", "retry", "fail"]
    outcome: str
    reason: str | None = None


def taint_of(events: list[Event]) -> list[str]:
    return sorted({t for e in events for t in e.taint})


def creator(route: Route) -> Principal:
    """The route's creator as a principal acting under the route's grant."""
    p = {k: v for k, v in (route.principal or {}).items() if k in ("kind", "id", "owner", "thread")}
    p.setdefault("kind", "human")
    p.setdefault("id", "owner")
    return Principal(**p, grant_id=route.created_by)


def proposed_flow_problem(route: Route, fv: Any) -> str | None:
    """What keeps a trigger an agent proposed from running flow version `fv`, or None. Its card named
    one flow version and showed a ceiling: a card that named no version (made before cards did)
    covers none, and a flow that can do more than the ceiling (the stronger of its declared effect and
    its steps', FlowVersion.effect) is refused. A trigger the owner wrote is theirs as written."""
    t = route.target
    if route.proposed_by is None or not isinstance(t, FlowTarget):
        return None
    if t.version is None:
        return ("this trigger was approved before its card named a flow version, so it runs none. Edit it, or "
                "approve a new proposal, to name the version it runs")
    if fv is None:
        return f"flow {t.flow} is not on this node"
    effect, ceiling = fv.effect, route.grant.ceiling
    if effect.rank is None or ceiling.rank is None or effect.rank > ceiling.rank:
        return (f"flow {t.flow} can {effect.value}, above this trigger's ceiling of {ceiling.value}, so it "
                "does not run")
    return None


class Targets:
    def __init__(self, runtime: Any, clock: Any):
        self.runtime, self.clock = runtime, clock

    def _svc(self, name: str) -> Any:
        return self.runtime.get(name) if self.runtime is not None else None

    async def deliver(self, route: Route, d: Delivery, events: list[Event]) -> Handoff:
        t = route.target
        if isinstance(t, FlowTarget):
            return self._flow(route, t, d, events)
        if isinstance(t, BrainTarget):
            return await self._brain(route, t, d, events)
        if isinstance(t, WorkflowTarget):
            return self._workflow(route, t, d, events)
        return await self._agent(route, t, d, events)

    def poll(self, d: Delivery) -> Finish | None:
        if d.target_ref is None:
            return None
        if d.target_kind == "flow":
            return self._poll_flow(d.target_ref)
        if d.target_kind == "brain":
            return self._poll_brain(d.target_ref)
        if d.target_kind == "workflow":
            return Finish("done", "started")  # the run can wait for days; it keeps its own history
        return None  # agent lanes call delivery_finished

    # ------------------------------------------------------------ flow

    def flow_params(self, t: FlowTarget, event: Event) -> dict[str, Any]:
        """Params rendered from facts only: the scope has nothing but the event's facts."""
        scope = {"facts": dict(event.facts)}
        return {k: render(v, scope) if isinstance(v, str) and "${" in v else v for k, v in t.params.items()}

    def _flow(self, route: Route, t: FlowTarget, d: Delivery, events: list[Event]) -> Handoff:
        runner = self._svc("runner")
        if runner is None:
            return Handoff("wait", "the runner is not running on this node", until=self.clock.now() + timedelta(minutes=5))
        first = events[0]
        try:
            params = self.flow_params(t, first)
        except InvalidRequest as e:
            return Handoff("fail", f"the trigger's params name a fact this event does not have: {e.message}")
        flows = getattr(runner, "flows", None)
        if route.proposed_by is not None:  # the card it was approved on bounds what it runs
            try:
                fv = flows.get(t.flow, t.version) if flows is not None and t.version is not None else None
            except NotFound as e:
                return Handoff("fail", e.message)
            problem = proposed_flow_problem(route, fv)
            if problem is not None:
                return Handoff("fail", problem)
        trigger = {"kind": "event", "route": route.id, "delivery": d.id, "events": d.event_ids,
                   "depth": max(e.cause.depth for e in events), "facts": dict(first.facts)}
        try:
            run, _ = runner.submit(flow_id=t.flow, version=t.version, params=params, lane=t.on,
                                   idempotency_key=f"event:{d.key}:{d.attempts}", principal=route.principal or None,
                                   source=f"route:{route.id}", trigger=trigger, authorized=False,
                                   grant_id=route.created_by)
        except (PolicyDenied, NotFound, InvalidRequest) as e:
            return Handoff("fail", e.message)
        except AgentosError as e:
            return Handoff("retry", e.message)
        return Handoff("delivered", ref=run.id)

    def _poll_flow(self, run_id: str) -> Finish | None:
        runner = self._svc("runner")
        run = runner.store.get_run(run_id) if runner is not None else None
        if run is None:
            return Finish("fail", "missing", "the run is gone from this node")
        s = run.status
        if s in (RunStatus.QUEUED, RunStatus.RUNNING):
            return None
        if s == RunStatus.FAILED:
            code = (run.error or {}).get("code")
            msg = (run.error or {}).get("message") or "the run failed"
            if code in OWNER_ENDED:
                return Finish("done", s.value, msg)
            return Finish("fail" if code in PERMANENT else "retry", s.value, msg)
        return Finish("done", s.value)  # verified, unverified, partial (never repeated blindly), waiting, cancelled

    # ------------------------------------------------------------ workflow

    def _workflow(self, route: Route, t: WorkflowTarget, d: Delivery, events: list[Event]) -> Handoff:
        workflows = self._svc("workflows")
        if workflows is None:
            return Handoff("wait", "workflows are not running on this node", until=self.clock.now() + timedelta(minutes=5))
        try:
            runs = [workflows.start_from_event(t.workflow, route, d, e) for e in events]  # one run an event
        except (PolicyDenied, NotFound, InvalidRequest) as e:
            return Handoff("fail", e.message)
        except AgentosError as e:
            return Handoff("retry", e.message)
        return Handoff("delivered", ref=runs[0].id if runs else None)

    # ------------------------------------------------------------ brain

    async def _brain(self, route: Route, t: BrainTarget, d: Delivery, events: list[Event]) -> Handoff:
        brains = self._svc("brains")
        if brains is None:
            return Handoff("wait", "no brain service on this node", until=self.clock.now() + timedelta(minutes=10))
        principal = creator(route)
        g = route.grant
        labels = taint_of(events)
        policy = self._svc("policy")
        minted: dict[str, Any] | None = None
        try:
            if policy is not None:
                ctx = policy.context_for_grant(route.created_by, principal=principal, tool="events.deliver")
                ctx.precheck(Effect.BRAIN)
                minted = await self.mint(route, d, events, principal)
            now = self.clock.now()
            prompt = frame.render(route, events, attempt=d.attempts, expires_at=now + timedelta(seconds=g.ttl_s),
                                  previous=d.reason if d.attempts > 1 else None)
            from agentos.brains.models import Budget, ContextBundle

            budget = Budget(max_turns=g.budget.turns, max_minutes=g.budget.minutes, max_tokens=g.budget.tokens,
                            max_usd=g.budget.usd)
            out = await brains.submit(principal, prompt=prompt, workspace=route.workspace, lanes=list(g.lanes),
                                      brain=t.brain, budget=budget, context_bundle=ContextBundle(taint=labels),
                                      turn_grant=minted)
        except (PolicyDenied, InvalidRequest, NotFound) as e:
            await self._revoke(minted)
            return Handoff("fail", e.message)
        except AgentosError as e:
            await self._revoke(minted)
            return Handoff("retry", e.message)
        return Handoff("delivered", ref=out.get("id"), grant_id=(minted or {}).get("grant_id"))

    async def mint(self, route: Route, d: Delivery, events: list[Event], principal: Principal) -> dict[str, Any]:
        """The delivery's own grant: a child of the route creator's grant, narrowed to the
        route's lanes, ceiling, budget and TTL, with no specials and no further delegation,
        then seeded with the events' taint. Uses policy.mint_child_token and
        policy.context_for_grant (a turn-grant builder, mint_turn_token, comes with brief B3)."""
        policy = self._svc("policy")
        g = route.grant
        out = await maybe_await(policy.mint_child_token(
            principal, purpose=f"trigger {route.id} delivery {d.id}", workspace=route.workspace,
            lanes=list(g.lanes), ceiling=g.ceiling, specials=[], ttl_seconds=g.ttl_s,
            budget={"tokens": g.budget.tokens, "usd": g.budget.usd}))
        if isinstance(out, str) or not out.get("grant_id"):
            raise ServiceUnavailable("the policy service did not return a grant id for the delivery's grant")
        labels = taint_of(events)
        if labels:
            ctx = policy.context_for_grant(out["grant_id"], tool="events.deliver")
            try:  # the origin's kind names it on an approval that follows (ROUTING.md)
                ctx.taint(*labels, source=f"event {events[0].id}", kind=f"{events[0].source}_event")
            except TypeError:  # a context without origin kinds
                ctx.taint(*labels, source=f"event {events[0].id}")
        return {"token": out["token"], "grant_id": out["grant_id"], "expires_at": out.get("expires_at")}

    async def _revoke(self, minted: dict[str, Any] | None) -> None:
        policy = self._svc("policy")
        if minted and policy is not None:
            try:
                await maybe_await(policy.revoke(minted["grant_id"], by="events", reason="delivery not started"))
            except Exception:  # noqa: BLE001 - the grant expires on its own
                log.warning("could not revoke delivery grant %s", minted["grant_id"])

    def _poll_brain(self, task_id: str) -> Finish | None:
        brains = self._svc("brains")
        if brains is None:
            return None
        try:
            st = brains.status(task_id)
        except AgentosError:
            return Finish("fail", "missing", "the brain task is gone from this node")
        status = st.get("status")
        reason = st.get("status_reason")
        if status in ("succeeded_unverified", "waiting_approval", "cancelled"):
            return Finish("done", status, reason)
        if status in ("failed", "interrupted"):
            return Finish("retry", status, reason or f"the brain task {status}")
        return None

    # ------------------------------------------------------------ agent lane

    async def _agent(self, route: Route, t: AgentTarget, d: Delivery, events: list[Event]) -> Handoff:
        agents = self._svc("agents")
        now = self.clock.now()
        if agents is None:
            return Handoff("wait", "no agent lanes on this node yet", until=now + timedelta(minutes=10))
        state = agents.lane_state(t.lane)
        if state is None:
            return Handoff("fail", f"there is no agent lane {t.lane}")
        if not state.accepts_turns:
            return Handoff("wait", f"agent lane {t.lane} is {state.value}", until=now + timedelta(seconds=60))
        g = route.grant
        prompt = frame.render(route, events, attempt=d.attempts, expires_at=now + timedelta(seconds=g.ttl_s),
                              previous=d.reason if d.attempts > 1 else None)
        req = TurnRequest(lane=t.lane, delivery_id=d.id, key=f"{d.key}:{d.attempts}", route_id=route.id,
                          event_ids=d.event_ids, attempt=d.attempts, depth=max(e.cause.depth for e in events),
                          prompt=prompt, session=t.session, priority=d.priority, taint_seed=taint_of(events),
                          grant=TurnGrant(ceiling=g.ceiling, lanes=list(g.lanes), flows=list(g.flows), ttl_s=g.ttl_s,
                                          budget=TurnBudget(**g.budget.model_dump())))
        try:
            receipt = await agents.deliver(req)
        except (PolicyDenied, InvalidRequest, NotFound) as e:
            return Handoff("fail", e.message)
        except AgentosError as e:
            return Handoff("retry", e.message)
        if receipt.status.value in ("failed", "cancelled"):
            return Handoff("retry", receipt.reason or f"the agent lane refused the turn ({receipt.status.value})")
        return Handoff("delivered", ref=receipt.turn_id, reason=receipt.reason)
