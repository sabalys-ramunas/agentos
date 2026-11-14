"""The events service (runtime service "event_queue"): emit, routes, the dispatcher loop.

    build(runtime)            -> EventService, registered as "event_queue"
    emit(event, route_id=)    -> (event, created): idempotent on (source, dedupe_key)
    add_route / update_route / remove_route / set_enabled / resume_route   owner only
    propose_route(...)        -> an approval card; approving it creates the route as written
    owner_message(...)        -> an owner.message event (urgent)
    delivery_finished(...)    -> an agent lane reports a turn's end (AgentsPort contract)

The daemon builds it after the runner and brains, and starts it after them: start() wires
the sources (run.finished, lane holds, approvals decided) and runs the dispatcher every
`tick_s` seconds, or at once when an event arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from agentos.core.config import home_from_env
from agentos.core.errors import InvalidRequest, NotFound, PolicyDenied, ServiceUnavailable
from agentos.core.models import Effect, Principal
from agentos.events.dispatcher import Dispatcher, EventSettings
from agentos.events.models import (
    AgentTarget,
    BrainTarget,
    Delivery,
    DeliveryState,
    Event,
    FlowTarget,
    Route,
    WorkflowTarget,
)
from agentos.events.ports import SERVICE_NAME
from agentos.events.store import EventStore
from agentos.events.targets import Finish, Targets, proposed_flow_problem
from agentos.policy.approval_binding import ActionBinding, action_binding_problem, is_legacy_hash
from agentos.policy.approvals import ask_for_action
from agentos.policy.confirm import ConfirmLevel
from agentos.runner.flows import validation_message
from agentos.runner.owner import is_owner
from agentos.runner.util import Clock, new_id

log = logging.getLogger("agentos.events")

DB_PATH = ("events", "events.db")
OWNER = {"kind": "human", "id": "owner", "owner": "owner"}
OWNER_GRANT = "owner"
ROUTE_FIELDS = {"name", "enabled", "match", "target", "instruction", "grant", "controls", "priority"}


def matches(route: Route, event: Event) -> bool:
    m = route.match
    if m.source != event.source or m.workspace != event.workspace:
        return False
    if m.kind is not None:
        if m.kind.endswith(".*"):
            if not event.kind.startswith(m.kind[:-1]):
                return False
        elif m.kind != event.kind:
            return False
    if m.lane is not None and (event.lane is None or not fnmatchcase(event.lane, m.lane)):
        return False
    if m.schedule_id is not None and event.facts.get("schedule_id") != m.schedule_id:
        return False
    return all(event.facts.get(k) == v for k, v in m.where.items())


def event_public(e: Event, *, content: bool = False) -> dict[str, Any]:
    """An event as shown: facts and labels; content only as its size unless asked for."""
    d = e.model_dump(mode="json", exclude={"content"})
    if e.content is not None:
        d["content"] = ({"text": e.content.text, "origin": e.content.origin, "truncated": e.content.truncated}
                        if content else {"chars": len(e.content.text), "origin": e.content.origin,
                                         "truncated": e.content.truncated})
    return d


def delivery_public(d: Delivery) -> dict[str, Any]:
    return d.model_dump(mode="json", exclude={"notified"})


def match_words(match: Any, workspace: str) -> str:
    """What a route wakes on, in one line, with every filter it has (QA 2026-11-10 A-9): a card that
    says "run events in scratch" for a match on failed runs of one lane reads far broader than it is."""
    words = f"{match.kind} events" if match.kind else f"{match.source} events"
    words += f" arrive in {workspace}"
    if match.lane:
        words += f" on lane {match.lane}"
    if match.where:
        words += " where " + " and ".join(f"{k} is {v}" for k, v in sorted(match.where.items()))
    if match.schedule_id:
        words += f" from schedule {match.schedule_id}"
    return words

class EventService:
    def __init__(self, runtime: Any, *, home: Path | None = None, clock: Clock | None = None,
                 settings: EventSettings | None = None, tick_s: float = 2.0, store: EventStore | None = None):
        cfg = runtime.get("config") if runtime is not None else None
        home = Path(home or (cfg.home if cfg is not None else home_from_env()))
        self.runtime = runtime
        self.clock = clock or Clock()
        self.store = store or EventStore(home.joinpath(*DB_PATH))
        self.targets = Targets(runtime, self.clock)
        self.dispatcher = Dispatcher(self.store, self.targets, runtime=runtime, clock=self.clock, settings=settings)
        self.tick_s = tick_s
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._sources: Any = None

    @property
    def settings(self) -> EventSettings:
        return self.dispatcher.settings

    def _svc(self, name: str) -> Any:
        return self.runtime.get(name) if self.runtime is not None else None

    # ------------------------------------------------------------ emit

    def emit(self, event: Event | dict[str, Any], *, route_id: str | None = None) -> tuple[Event, bool]:
        now = self.clock.now()
        if isinstance(event, dict):
            data = {k: v for k, v in event.items() if v is not None}
            data.setdefault("id", new_id("ev"))
            data.setdefault("occurred_at", now)
            data["received_at"] = now
            try:
                event = Event.model_validate(data)
            except ValidationError as e:
                raise InvalidRequest(f"invalid event: {validation_message(e)}") from e
        stored, created = self.store.insert_event(event)
        if not created:
            return stored, False
        routes = [r for r in self.store.list_routes(workspace=stored.workspace, source=stored.source, enabled_only=True)
                  if matches(r, stored)]
        if route_id is not None and all(r.id != route_id for r in routes):
            r = self.store.get_route(route_id)
            if (r is not None and r.enabled and r.match.source == stored.source and r.workspace == stored.workspace):
                routes.append(r)
        for r in routes:
            try:
                self.dispatcher.offer(r, stored, now)
            except Exception:
                log.exception("route %s could not take event %s", r.id, stored.id)
        if stored.source != "owner":
            self.dispatcher.storm_check(now, [r.id for r in routes])
        self.wake()
        return stored, True

    def owner_message(self, workspace: str, text: str, *, lane: str | None = None,
                      principal: Principal | dict | None = None) -> Event:
        """The owner typed a message (panel composer, `agentos send`, agent_send): an urgent
        owner.message event with the text as owner-labelled content."""
        p = principal.model_dump() if isinstance(principal, Principal) else (principal or {})
        if p and not is_owner(p) and p.get("kind") != "human":
            raise PolicyDenied("only the owner sends owner messages")
        if not text or not text.strip():
            raise InvalidRequest("the message is empty")
        eid = new_id("ev")
        ev, _ = self.emit({"id": eid, "source": "owner", "kind": "owner.message", "workspace": workspace, "lane": lane,
                           "dedupe_key": f"owner:{eid}", "content": {"text": text.strip(), "origin": "the owner"},
                           "facts": {"by": p.get("id") or "owner"}, "priority": "urgent"})
        return ev

    def find_event(self, *, source: str, schedule_id: str, delivery: str) -> Event | None:
        if source != "webhook":
            return None
        return self.store.find_webhook(schedule_id, delivery)

    def delivery_finished(self, delivery_id: str, outcome: Literal["done", "failed"], *,
                          status: str | None = None, detail: str | None = None) -> None:
        d = self.store.get_delivery(delivery_id)
        if d is None:
            raise NotFound(f"delivery {delivery_id!r} not found")
        if d.state != DeliveryState.DELIVERED:
            return  # already finished (a duplicate report after a restart)
        kind = "done" if outcome == "done" else "retry"
        if status in ("cancelled",):
            kind = "done"
        self.dispatcher.finish(d, Finish(kind, status or outcome, detail), self.clock.now())
        self.wake()

    # ------------------------------------------------------------ routes

    def get_route(self, route_id: str) -> Route:
        r = self.store.get_route(route_id)
        if r is None:
            raise NotFound(f"trigger {route_id!r} not found")
        return r

    def list_routes(self, workspace: str | None = None) -> list[Route]:
        return self.store.list_routes(workspace=workspace)

    @staticmethod
    def _owner(principal: Principal | dict | None) -> dict[str, Any]:
        """Routes are owner-written: the human owner, or a call from inside the node (CLI)."""
        p = principal.model_dump() if isinstance(principal, Principal) else dict(principal or {})
        if p and not is_owner(p):
            raise PolicyDenied("only the owner writes triggers; an agent may propose one with trigger_propose")
        return {k: p[k] for k in ("kind", "id", "owner") if k in p} or dict(OWNER)

    def _build(self, data: dict[str, Any], *, rid: str, principal: dict[str, Any], created_by: str,
               now: datetime, extra: dict[str, Any] | None = None) -> Route:
        unknown = set(data) - ROUTE_FIELDS
        if unknown:
            raise InvalidRequest(f"unknown trigger field(s): {', '.join(sorted(unknown))}")
        try:
            return Route.model_validate({**data, "id": rid, "created_by": created_by, "principal": principal,
                                         "created_at": now, "updated_at": now, **(extra or {})})
        except ValidationError as e:
            raise InvalidRequest(f"invalid trigger: {validation_message(e)}") from e

    def _pin_version(self, route: Route) -> None:
        """A flow target that names no version names the one newest now."""
        t = route.target
        flows = self._svc("flows")
        if isinstance(t, FlowTarget) and t.version is None and flows is not None:
            t.version = flows.get(t.flow).version

    def _check(self, route: Route) -> Route:
        """The creator's grant must cover what the route reaches, now (again at delivery)."""
        t = route.target
        if isinstance(t, WorkflowTarget):
            return route  # each step checks its own grant, right before it acts
        policy = self._svc("policy")
        ctx = None
        if policy is not None:
            who = Principal(**route.principal, grant_id=route.created_by)
            ctx = policy.context_for_grant(route.created_by, principal=who, tool="trigger_save")
        if isinstance(t, FlowTarget):
            flows, runner = self._svc("flows"), self._svc("runner")
            if flows is None:
                raise ServiceUnavailable("flows are not available on this node")
            fv = flows.get(t.flow, t.version)
            lane = flows.resolve_lane(fv, t.on)
            if lane.split("/", 1)[0] != route.workspace:
                raise InvalidRequest(f"flow {t.flow} runs on {lane}, outside the trigger's workspace")
            t.on = lane
            if runner is not None and runner.scheduler is not None:
                runner.scheduler._validate_params(fv, dict(t.params))
            problem = proposed_flow_problem(route, fv)
            if problem is not None:
                raise InvalidRequest(problem)
            if ctx is not None:
                ctx.precheck(fv.effect, lane_id=lane)
            return route
        for lane in route.grant.lanes:
            if isinstance(t, BrainTarget) and any(ch in lane for ch in "*?["):
                raise InvalidRequest("a brain trigger's grant names lane ids, not patterns")
            if ctx is not None and not any(ch in lane for ch in "*?["):
                ctx.precheck(route.grant.ceiling, lane_id=lane)
        if ctx is not None:
            if isinstance(t, BrainTarget):
                ctx.precheck(Effect.BRAIN)
            if isinstance(t, AgentTarget):
                ctx.precheck(Effect.READ, lane_id=t.lane)
        return route

    def add_route(self, data: dict[str, Any], *, principal: Principal | dict | None = None,
                  grant_id: str | None = None) -> Route:
        p = self._owner(principal)
        now = self.clock.now()
        route = self._check(self._build(data, rid=new_id("rt"), principal=p, created_by=grant_id or OWNER_GRANT,
                                        now=now))
        self.store.put_route(route)
        return route

    def update_route(self, route_id: str, patch: dict[str, Any], *, principal: Principal | dict | None = None) -> Route:
        self._owner(principal)
        old = self.get_route(route_id)
        base = old.model_dump(mode="json", by_alias=True, include=ROUTE_FIELDS)
        built = self._build({**base, **patch}, rid=old.id, principal=old.principal, created_by=old.created_by,
                            now=old.created_at, extra={"state": old.state, "proposed_by": old.proposed_by})
        if built.proposed_by is not None:  # the owner's edit of a proposed trigger approves the version newest now
            self._pin_version(built)
        route = self._check(built)
        route.updated_at = self.clock.now()
        self.store.put_route(route)
        return route

    def remove_route(self, route_id: str, *, principal: Principal | dict | None = None) -> bool:
        self._owner(principal)
        if not self.store.delete_route(route_id):
            raise NotFound(f"trigger {route_id!r} not found")
        return True

    def set_enabled(self, route_id: str, enabled: bool, *, principal: Principal | dict | None = None) -> Route:
        self._owner(principal)
        r = self.get_route(route_id)
        r.enabled, r.updated_at = enabled, self.clock.now()
        self.store.put_route(r)
        return r

    def resume_route(self, route_id: str, *, principal: Principal | dict | None = None) -> Route:
        """The owner resumes a route the breaker (or the owner) paused."""
        self._owner(principal)
        r = self.get_route(route_id)
        self.dispatcher.resume(r, self.clock.now())
        self.wake()
        return self.get_route(route_id)

    # ------------------------------------------------------------ proposals (agents never write routes)

    def propose_route(self, data: dict[str, Any], *, principal: Principal, grant_id: str | None,
                      grant_path: str | None) -> dict[str, Any]:
        """An agent's proposal: validated, shown in full on an approval card, created only when
        the owner approves it (the owner's grant, as written)."""
        approvals = self._svc("approvals")
        if approvals is None or not callable(getattr(approvals, "create_action", None)):
            raise ServiceUnavailable("this node has no approvals service, so a trigger cannot be proposed")
        now = self.clock.now()
        built = self._build(data, rid=new_id("rt"), principal=dict(OWNER), created_by=OWNER_GRANT, now=now,
                            extra={"proposed_by": principal.model_dump()})
        if isinstance(built.target, WorkflowTarget):
            # a workflow carries its own trigger, and workflow_propose shows it on a card with the version a
            # yes turns on; a route beside it would wake whatever version is live, under no card's ceiling
            raise InvalidRequest(f"a trigger cannot be proposed for workflow {built.target.workflow}: a workflow "
                                 "carries its own trigger. Put the trigger in the workflow (workflow_save) and "
                                 "call workflow_propose; the person's yes turns on exactly the version on its card")
        self._pin_version(built)  # the card names the flow version it runs, and approving binds it
        route = self._check(built)
        body = route.model_dump(mode="json", by_alias=True, include=ROUTE_FIELDS)
        t = route.target
        what = {"flow": f"run flow {getattr(t, 'flow', '')} version {getattr(t, 'version', '')}", "brain": "start a brain task",
                "agent": f"wake agent lane {getattr(t, 'lane', '')}"}[t.kind]
        summary = (f'Create trigger "{route.name}": when {match_words(route.match, route.workspace)}, '
                   f"{what}, ceiling {route.grant.ceiling.value}")
        ap = ask_for_action(
            approvals, _proposal_binding(body, route.id, route.target_lane, summary), principal,
            "An agent proposed this trigger. Only you create triggers; approving creates it exactly as shown.",
            grant_id=grant_id, grant_path=grant_path,
            floor=ConfirmLevel.SIGNED_IN)  # it acts as the owner from now on: never answered in the agent's chat
        self.store.put_proposal(ap.id, "pending", now, {"route": body, "id": route.id, "summary": summary,
                                                       "proposed_by": principal.model_dump()})
        return {"approval_id": ap.id, "trigger_id": route.id, "summary": summary, "state": "pending"}

    def check_proposals(self) -> int:
        approvals = self._svc("approvals")
        if approvals is None:
            return 0
        n = 0
        for aid, doc in self.store.pending_proposals():
            try:
                ap = approvals.get(aid)
            except NotFound:
                self.store.put_proposal(aid, "gone", self.clock.now(), doc)
                continue
            state = getattr(ap, "state", None)
            if state == "approved" and (problem := self._proposal_problem(ap, doc)) is not None:
                # the stored proposal is not what the owner approved: nothing is created
                log.warning("approved trigger proposal %s was not created: the approval %s", aid, problem)
                self.store.put_proposal(aid, "mismatch", self.clock.now(),
                                        {**doc, "error": f"the approval {problem}"})
                continue
            if state == "approved" and approvals.consume(aid):
                try:
                    now = self.clock.now()
                    route = self._check(self._build(doc["route"], rid=doc["id"], principal=dict(OWNER),
                                                    created_by=OWNER_GRANT, now=now,
                                                    extra={"proposed_by": doc.get("proposed_by")}))
                    self.store.put_route(route)
                    self.store.put_proposal(aid, "created", now, doc)
                    n += 1
                except Exception as e:  # noqa: BLE001 - the card said what it would create; record why not
                    log.warning("approved trigger proposal %s could not be created: %s", aid, e)
                    self.store.put_proposal(aid, "invalid", self.clock.now(), {**doc, "error": str(e)[:300]})
            elif state in ("denied", "expired", "consumed"):
                self.store.put_proposal(aid, state, self.clock.now(), doc)
        return n

    def _proposal_problem(self, ap: Any, doc: dict[str, Any]) -> str | None:
        """What is wrong with approving the stored proposal `doc` under approval `ap`, or None: the
        route it would create must be the one the card bound (action_binding_problem)."""
        try:
            lane = self._build(doc["route"], rid=doc["id"], principal=dict(OWNER), created_by=OWNER_GRANT,
                               now=self.clock.now(), extra={"proposed_by": doc.get("proposed_by")}).target_lane
        except Exception:  # noqa: BLE001 - creating it fails the same way, and records why
            return None
        binding = _proposal_binding(doc["route"], doc["id"], lane, str(doc.get("summary") or ""))
        legacy: tuple[str, ...] = ()
        if getattr(ap, "binding", None) is None and is_legacy_hash(getattr(ap, "content_hash", None)):
            # a card made before ActionBinding, still pending at the upgrade: read for one release
            legacy = (hashlib.sha256(json.dumps(doc["route"], indent=2, sort_keys=True).encode()).hexdigest(),)
        return action_binding_problem(ap, binding, legacy=legacy)

    # ------------------------------------------------------------ reading

    def list_events(self, *, source: str | None = None, workspace: str | None = None,
                    since: datetime | None = None, limit: int = 100) -> list[Event]:
        return self.store.list_events(source=source, workspace=workspace, since=since, limit=min(limit, 500))

    def deliveries_for(self, event_id: str) -> list[Delivery]:
        return self.store.list_deliveries(event_id=event_id, limit=50)

    def describe_route(self, r: Route) -> dict[str, Any]:
        out = r.public()
        waiting = self.store.list_deliveries(states=[DeliveryState.PENDING, DeliveryState.WAITING], route_id=r.id,
                                             limit=50)
        out["waiting"] = len(waiting)
        out["next_reason"] = waiting[0].reason if waiting else None
        last = self.store.list_deliveries(route_id=r.id, limit=1)
        out["last_delivery"] = delivery_public(last[0]) if last else None
        return out

    # ------------------------------------------------------------ lifecycle

    def wake(self) -> None:
        self._wake.set()

    async def tick(self) -> dict[str, int]:
        self.check_proposals()
        return await self.dispatcher.tick()

    def recover(self) -> int:
        """After a restart: a delivery handed to a target that never recorded it is offered
        again (at least once; targets dedupe on the delivery key)."""
        n = 0
        now = self.clock.now()
        for d in self.store.list_deliveries(states=[DeliveryState.DELIVERED], limit=1000):
            if d.target_ref is None and d.target_kind != "agent":
                d.attempts = max(0, d.attempts - 1)
                d.note(now, DeliveryState.PENDING, "the node restarted before the target recorded it")
                d.due_at = now
                route = self.store.get_route(d.route_id)
                self.store.save_delivery(d, target_lane=route.target_lane if route else None,
                                         expect=[DeliveryState.DELIVERED])
                n += 1
        return n

    async def start(self) -> None:
        from agentos.events.sources import Sources

        self._stopping = False
        self.recover()
        self._sources = Sources(self)
        self._sources.install()
        self._task = asyncio.create_task(self._loop(), name="events")

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except Exception:
                log.exception("events tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.tick_s)
            self._wake.clear()

    async def stop(self) -> None:
        self._stopping = True
        if self._sources is not None:
            await self._sources.uninstall()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def close(self) -> None:
        self.store.close()


def build(runtime: Any, **options: Any) -> EventService:
    """Composition-root hook: the node's events layer, registered as "event_queue"."""
    svc = EventService(runtime, **options)
    if runtime is not None:
        runtime.register(SERVICE_NAME, svc)
    return svc


def _proposal_binding(body: dict[str, Any], route_id: str, lane: str | None, summary: str) -> ActionBinding:
    """What a trigger proposal's card binds: the route body as shown, under its id."""
    return ActionBinding(effect="commit", tool="trigger_propose", lane=lane, content=body, key=route_id,
                         summary=summary)
