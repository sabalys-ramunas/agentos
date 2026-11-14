"""The agenda service (runtime service "agenda"): draft, propose, approve, fire and follow entries.

How an entry gets permission to run. Proposing asks the node's own policy, under the grant
the entry was drafted with, exactly the question its run would ask: `ctx.authorize` with the
action's effect, its lane or surface, a node-written summary and the same approval content a
flow run binds (agentos.runner.flows.approval_content, or the connector's). So the routing
table, "Ask me first", standing approvals, the untrusted-content downgrade and the commit
pace all apply, and nothing here decides on its own:

- the routing lets it run without asking: the entry is `scheduled` (via "auto"), and its run is
  authorized again when it fires, like a schedule's slot: if "Ask me first" was turned on since,
  the run asks then;
- a standing approval covers it (one use is spent now) or a matching approval was already
  given: `scheduled`, and the run goes as approved;
- a person must decide: the entry is `proposed`, with the id of a normal approval in Needs you.
  When a person approves it (the panel, `agentos approvals`, `agentos agenda approve`), the
  next sync consumes that approval and the entry is `scheduled`. An approval with an edit
  (a caption changed on the card) changes the entry's inputs to what the person approved.

How long that request waits. An ordinary request closes after the node's approval time (a day by
default), which would send an entry planned weeks ahead back to draft long before its date. So
proposing passes the request its own expiry (`ctx.authorize(approval_expires_at=...)`, see
`request_expiry`): the entry's start (its exact time, or its window's not-before) plus AT_GRACE,
never sooner than an ordinary request would close, and never later than the node's
`agenda_approval_max_days` (14 by default) after proposing. An entry for "as soon as approved" has
no start, so its request gets the whole cap. A request that closes undecided sends the entry back to
`draft` with the reason (it says when the cap, not the date, closed it). Nothing else about the
approval changes: it binds the same hash, an edit withdraws it, and the run is authorized again
when it fires.

The approval binds to `action_hash` (models.action_hash: the action, its inputs, its target
and its timing). An edit of any of those after approval sends the entry back to `proposed`
and asks again; an edit while it waits withdraws the old request first.

How an entry fires. There is no second scheduler: the runner's Scheduler ticks this service
(`Scheduler.one_offs`) on its own loop. A due entry is submitted through RunnerService.submit,
the same path a schedule's slot takes, with the idempotency key `agenda:<entry>:<hash>`, so a
restart between "submitted" and "recorded" finds the same run and never posts twice. Every run
carries the drafter's grant, checked when it is queued and again right before it runs. A trigger
entry emits one event with the same key as its dedupe key (the events layer dedupes it), and the
trigger's own controls and grant decide what it wakes.

Whose authority a run carries. Normally the drafter's grant, checked when the run is queued and
again right before it runs. An agent's grant lasts a week by default, so an entry planned further
ahead can outlive it. When that grant has merely expired and a person approved the entry (via
"person": they decided its own request, recorded at approval as `approval.approver` with the hash
they approved), the run goes under that person's authority instead, for this one action only: the
policy re-checks that the approver still may (the owner always; a member only with the very grant
they decided with, still live and still covering the effect and lane) and derives a grant for this
run alone (PolicyService.approved_run_grant: this lane, at most this effect, inside the window,
pinned to the flow, version, inputs and idempotency key, no delegation), kept as `authority` and
revoked once the run is over. The audit records both the drafting agent and the approver. An
explicit stop is never bypassed: a revoked drafter's grant cancels its not-yet-run entries (the
tick sweeps them), a freeze on the lane fails the entry, and a held lane, the pace and the egress
guard stop the run as for any run. An entry no person approved (the routing let it run, or a
standing approval covered it) has no approver and still needs the drafter's grant: a standing
approval was given ahead for a kind of action, not for this one, so it never stands in for an
agent whose grant lapsed.

The lane's posting pace and waking hours (agentos.policy.pace) apply. Before it submits a
commit, the agenda reads the lane's pace: when the pace says not yet and the next allowed time
is inside the entry's window, it waits for that time (`retry_at`) instead of failing; when the
next allowed time is past the window, the entry is `skipped`, with the pace's own words. A run
the runner itself holds back (a person holds the lane, a connector is broken, the pace closed
between check and run) is cancelled when the window closes before it starts, and the entry is
`skipped` with the run's reason. A window that passes before anything fired (the node was down,
nobody approved) makes the entry `expired`.

Proof is what the run left: its status, readbacks, final URL, screenshot ref, or, for a trigger,
the deliveries and what they started. Nothing is ever invented: an entry with no proof says so.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentos.agenda.models import (
    EDITABLE,
    FINISHED,
    MAX_NOTES,
    MAX_TITLE,
    Action,
    ActionKind,
    Entry,
    EntryState,
    When,
    action_hash,
)
from agentos.agenda.store import DB_NAME, AgendaStore
from agentos.core.config import home_from_env
from agentos.core.errors import (
    AgentosError,
    ApprovalPending,
    Conflict,
    InvalidRequest,
    NotFound,
    PolicyDenied,
    ServiceUnavailable,
)
from agentos.core.models import Effect, Principal, RunStatus, parse_lane_id
from agentos.runner.models import SUCCEEDED, TERMINAL, Run, is_connector_id, is_owner
from agentos.runner.util import Clock, clip, clip_text, new_id

log = logging.getLogger("agentos.agenda")

SERVICE_NAME = "agenda"
OWNER_GRANT = "owner"
TOOL = "agenda_propose"          # what the approval records as the asking tool
AT_GRACE = timedelta(minutes=30)  # an exact time still runs this long after, for a busy or restarted node
MAX_AHEAD = timedelta(days=366)
COMMIT_EFFECTS = frozenset({"commit", "pay", "irreversible"})  # the effects the lane's pace applies to
CONNECTOR_RETRY = timedelta(minutes=15)  # a broken connector or its own pace: look again this often
RUN_FINISHED = {"done": EntryState.DONE, "failed": EntryState.FAILED, "cancelled": EntryState.CANCELLED}
PERSON_VIAS = frozenset({"person", "standing", "approved", "owner"})  # a run that goes as approved
# services that plan timed work on lanes on their own; the timeline shows what they plan, read only
MANAGED = {"keepalive": "keepalive", "engage": "engagement"}
APPROVAL_MAX_DAYS = 14   # node.toml agenda_approval_max_days: the longest a request for an entry waits
DEFAULT_REQUEST_S = 86400  # an ordinary request's time, when the approvals service does not say


class AgendaStateError(Conflict):
    """The entry is in a state that does not allow this (approve a cancelled entry, edit a running one)."""
    code = "agenda_state"


def _utc(value: Any, name: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        when = value
    else:
        try:
            when = datetime.fromisoformat(str(value).strip())
        except ValueError:
            raise InvalidRequest(f"{name} is an ISO 8601 time like 2026-11-08T09:00:00+02:00") from None
    if when.tzinfo is None:
        raise InvalidRequest(f"{name} needs a timezone, like 2026-11-08T09:00:00+02:00")
    return when.astimezone(UTC)


def parse_when(data: Any, now: datetime) -> When:
    """`{"at": t}`, `{"not_before": t, "not_after": t}` (either may be left out), or `{}` / None
    for as soon as approved (`{"not_after": t}` gives that a deadline). Times need a timezone."""
    if data is None:
        data = {}
    if isinstance(data, str):
        data = {"at": data}
    if not isinstance(data, dict):
        raise InvalidRequest("when is an object: {at}, {not_before, not_after}, or {} for as soon as approved")
    unknown = set(data) - {"at", "not_before", "not_after", "kind"}
    if unknown:
        raise InvalidRequest(f"when takes at, not_before and not_after, not {', '.join(sorted(unknown))}")
    at = _utc(data.get("at"), "at")
    nb, na = _utc(data.get("not_before"), "not_before"), _utc(data.get("not_after"), "not_after")
    if at is not None:
        if nb is not None:
            raise InvalidRequest("give either at (an exact time) or not_before (a window), not both")
        w = When(kind="at", not_before=at, not_after=na or at + AT_GRACE)
    elif nb is not None:
        w = When(kind="window", not_before=nb, not_after=na)
    else:
        w = When(kind="asap", not_before=None, not_after=na)
    if w.not_before and w.not_after and w.not_after <= w.not_before:
        raise InvalidRequest("not_after must be later than the start")
    if w.not_before and w.not_before > now + MAX_AHEAD:
        raise InvalidRequest("an agenda entry is planned at most a year ahead")
    return w


def _principal(p: Principal | dict | None) -> dict[str, Any]:
    if isinstance(p, Principal):
        return p.model_dump()
    return dict(p or {})


def owner_principal() -> dict[str, Any]:
    return {"kind": "human", "id": "owner", "owner": "owner", "grant_id": OWNER_GRANT}


class AgendaService:
    def __init__(self, store: AgendaStore, runtime: Any, *, clock: Clock | None = None):
        self.store, self.runtime = store, runtime
        runner = self._svc("runner")
        self.clock = clock or getattr(runner, "clock", None) or Clock()

    # ------------------------------------------------------------ wiring

    def _svc(self, name: str) -> Any:
        return self.runtime.get(name) if self.runtime is not None else None

    @property
    def runner(self) -> Any:
        r = self._svc("runner")
        if r is None:
            raise ServiceUnavailable("the agenda runs actions through the runner, which this node lacks")
        return r

    @property
    def flows(self) -> Any:
        return self.runner.flows

    def _events(self) -> Any:
        events = self._svc("event_queue")
        if events is None:
            raise ServiceUnavailable("the events layer is not running on this node, so a trigger cannot be planned")
        return events

    def attach(self) -> bool:
        """Composition hook: the runner's scheduler ticks the agenda, and a finished run updates its
        entry at once (the tick would find it too)."""
        runner = self._svc("runner")
        if runner is None or getattr(runner, "scheduler", None) is None:
            return False
        hooks = runner.scheduler.one_offs
        if self not in hooks:
            hooks.append(self)
            runner.on_finished.append(self._run_finished)
        return True

    def close(self) -> None:
        runner = self._svc("runner")
        hooks = getattr(getattr(runner, "scheduler", None), "one_offs", None)
        if hooks is not None and self in hooks:
            hooks.remove(self)
        if runner is not None and self._run_finished in runner.on_finished:
            runner.on_finished.remove(self._run_finished)
        self.store.close()

    # ------------------------------------------------------------ reading

    def get(self, entry_id: str) -> Entry:
        e = self.store.get(entry_id)
        if e is None:
            raise NotFound(f"agenda entry {entry_id!r} not found")
        return e

    def list(self, *, workspace: str | None = None, states: list[EntryState] | None = None,
             start: datetime | None = None, end: datetime | None = None, limit: int = 1000) -> list[Entry]:
        return self.store.list(workspace=workspace, states=states, start=start, end=end, limit=limit)

    def describe(self, e: Entry) -> dict[str, Any]:
        a = e.action
        d: dict[str, Any] = {
            "id": e.id, "type": "entry", "workspace": e.workspace, "title": e.title, "notes": e.notes,
            "state": e.state.value, "reason": e.reason,
            "action": {"kind": a.kind.value, "target": a.target, "version": a.version,
                       "inputs": clip(self._shown_inputs(a), max_str=300), "on": a.on, "lane": a.lane,
                       **({"surface": a.surface} if a.surface else {}), "effect": a.effect},
            "lane": a.lane, "kind": a.kind.value, "effect": a.effect,
            "when": {"kind": e.when.kind, "not_before": _iso(e.when.not_before),
                     "not_after": _iso(e.when.not_after), "words": e.when.words()},
            "at": _iso(e.when.not_before),
            "approval": dict(e.approval) if e.approval else None,
            "approved": bool(e.approved_hash) and e.approved_hash == action_hash(e.action, e.when),
            "run_id": e.run_id, "event_id": e.event_id, "retry_at": _iso(e.retry_at),
            "proof": e.proof, "history": e.history[-10:],
            "drafted_by": {k: e.principal.get(k) for k in ("kind", "id") if e.principal.get(k)},
            "grant_id": e.grant_id, "authority": dict(e.authority) if e.authority else None,
            "created_at": _iso(e.created_at), "updated_at": _iso(e.updated_at),
            "finished_at": _iso(e.finished_at), "next": self._next_words(e),
        }
        return d

    def _shown_inputs(self, a: Action) -> dict[str, Any]:
        """Inputs as shown: values a flow does not mark `show` read "(hidden)", like a run's params."""
        if a.kind == ActionKind.ROUTE:
            return dict(a.inputs)
        try:
            shown = self.flows.shown_params_of(a.target, a.version)
        except AgentosError:
            return {k: "(hidden)" for k in a.inputs}
        return {k: v if k in shown else "(hidden)" for k, v in a.inputs.items()}

    def _next_words(self, e: Entry) -> str:
        s = e.state
        if s == EntryState.DRAFT:
            return "a draft: propose it to ask for approval (agenda_propose)"
        if s == EntryState.PROPOSED:
            until = (e.approval or {}).get("expires_at")
            return "waiting for a person to approve it in Needs you" + (
                f" (the request stays open until {until[:16].replace('T', ' ')} UTC)" if until else "")
        if s == EntryState.SCHEDULED:
            if e.retry_at:
                return f"scheduled: waiting for the lane's pace until {e.retry_at.isoformat(timespec='minutes')}"
            return f"scheduled: runs {e.when.words()}"
        if s == EntryState.RUNNING:
            return "running: see run_id for its progress"
        return {EntryState.DONE: "done: see proof", EntryState.FAILED: "failed: see reason",
                EntryState.SKIPPED: "skipped: see reason; edit it to plan it again",
                EntryState.EXPIRED: "expired: its window passed; edit it to plan it again",
                EntryState.CANCELLED: "cancelled"}[s]

    # ------------------------------------------------------------ resolving an action

    def _action(self, data: Any, *, grant_id: str | None, principal: dict[str, Any]) -> Action:
        """Check and resolve what an entry runs, and that its grant may do it on that lane now
        (scope and ceiling only; approval comes when it is proposed)."""
        if not isinstance(data, dict):
            raise InvalidRequest("action is an object: {flow, params, on} or {trigger, on}")
        unknown = set(data) - {"flow", "connector", "trigger", "params", "on", "version"}
        if unknown:
            raise InvalidRequest(f"action takes flow (or connector, or trigger), params, on and version, not "
                                 f"{', '.join(sorted(unknown))}")
        named = [k for k in ("flow", "connector", "trigger") if data.get(k)]
        if len(named) != 1:
            raise InvalidRequest("an action names exactly one of: flow (a saved flow id), connector (a connector "
                                 "operation like connector.instagram.post_photo) or trigger (a trigger id rt_...)")
        params = data.get("params")
        if params is not None and not isinstance(params, dict):
            raise InvalidRequest("params must be a JSON object of name: value")
        params = dict(params or {})
        on = data.get("on") or None
        if named[0] == "trigger":
            return self._route_action(str(data["trigger"]), on, params, grant_id=grant_id, principal=principal)
        target = str(data[named[0]])
        if named[0] == "connector" and not is_connector_id(target):
            target = "connector." + target
        fv = self.flows.get(target, data.get("version") or None)
        if is_connector_id(fv.id):
            t = self.runner.connector_target(fv, on, params)
            self.runner.check_grant(grant_id, principal, effect=fv.effect, lane=t.surface.id)
            return Action(kind=ActionKind.CONNECTOR, target=fv.id, version=fv.version, inputs=t.caller_params,
                          on=t.surface.id, lane=t.lane, surface=t.surface.id, effect=fv.effect.value)
        lane = self.flows.resolve_lane(fv, on)
        params = self.flows.validate_params(fv, params)
        self.runner.check_grant(grant_id, principal, effect=fv.effect, lane=lane)
        return Action(kind=ActionKind.FLOW, target=fv.id, version=fv.version, inputs=params,
                      on=on, lane=lane, effect=fv.effect.value)

    def _route_action(self, route_id: str, on: str | None, params: dict[str, Any], *, grant_id: str | None,
                      principal: dict[str, Any]) -> Action:
        events = self._events()
        r = events.get_route(route_id)
        if r.match.source != "cron":
            raise InvalidRequest(f"trigger {route_id} wakes on {r.match.source} events; the agenda fires triggers "
                                 "that wake on timed (cron) events, like a schedule does")
        if params:
            raise InvalidRequest("a trigger takes no params; it fills its target from the event's facts. Put what "
                                 "the agent should know in the entry's notes")
        lane = on or (r.target.lane if r.target.kind == "agent" else None) or (
            r.target.on if r.target.kind == "flow" else None)
        if lane is not None:
            try:
                ws, _ = parse_lane_id(lane)
            except ValueError as e:
                raise InvalidRequest(str(e)) from None
            if ws != r.workspace:
                raise InvalidRequest(f"lane {lane} is not in the trigger's workspace {r.workspace}")
        effect = Effect.BRAIN
        if r.target.kind == "flow":
            effect = self.flows.get(r.target.flow, r.target.version).effect
        self.runner.check_grant(grant_id, principal, effect=Effect.READ if lane else None, lane=lane)
        return Action(kind=ActionKind.ROUTE, target=r.id, inputs={}, on=on, lane=lane, effect=effect.value)

    def _workspace(self, action: Action) -> str:
        if action.kind == ActionKind.ROUTE:
            return self._events().get_route(action.target).workspace
        return (action.acts_on or "").split("/", 1)[0]

    def _authorization(self, e: Entry) -> tuple[Effect, str | None, str | None, str, Any]:
        """(effect, lane or surface, domain, summary, content): what proposing asks the policy. The
        content is what the run would bind, so the approval card shows the real post or form."""
        a = e.action
        head = f"Agenda: {clip_text(e.title, 80)!r}, {e.when.words()}. "
        if a.kind == ActionKind.ROUTE:
            r = self._events().get_route(a.target)
            what = {"flow": "runs flow", "brain": "wakes a brain", "agent": "wakes the agent on"}[r.target.kind]
            detail = getattr(r.target, "flow", None) or getattr(r.target, "lane", None) or "the router's pick"
            summary = head + f"Fire trigger {r.id} ({clip_text(r.name, 60)}), which {what} {detail}."
            content = {"trigger": r.id, "workspace": r.workspace, "target": r.target.kind, "lane": a.lane,
                       "title": e.title}
            return Effect(a.effect), a.lane, None, summary, content
        fv = self.flows.get(a.target, a.version)
        if a.kind == ActionKind.CONNECTOR:
            t = self.runner.connector_target(fv, a.surface, a.inputs)
            if t.lane != a.lane:
                raise PolicyDenied(f"the binding of surface {a.surface} changed since this entry was drafted (it now "
                                   f"runs on {t.lane}); edit the entry to plan it again")
            summary, content = self.runner.surfaces.approval(fv, t)
            return fv.effect, a.surface, fv.definition.domain, head + summary, content
        lane = a.lane or ""
        return (fv.effect, lane, fv.definition.domain, head + self.flows.run_summary(fv, lane, a.inputs),
                self.flows.approval_content(fv, lane, a.inputs))

    # ------------------------------------------------------------ writing

    def draft(self, *, title: str, action: dict[str, Any], when: dict[str, Any] | str | None = None,
              notes: str | None = None, workspace: str | None = None, principal: Principal | dict | None = None,
              grant_id: str | None = None) -> Entry:
        """A new entry in `draft`. The grant (default: the principal's, or the owner's for the owner
        or an anonymous call from inside the node) must cover the action's lane and effect now, or
        PolicyDenied is raised and nothing is stored. Every later fire runs under it."""
        p = _principal(principal)
        grant_id = grant_id or p.get("grant_id") or (OWNER_GRANT if not p.get("id") or is_owner(p) else None)
        now = self.clock.now()
        a = self._action(action, grant_id=grant_id, principal=p)
        ws = self._workspace(a)
        if workspace is not None and workspace != ws:
            raise InvalidRequest(f"the action acts in workspace {ws}, not {workspace}")
        e = Entry(id=new_id("ag"), workspace=ws, title=_title(title), notes=_notes(notes), action=a,
                  when=parse_when(when, now), principal=p, grant_id=grant_id, created_at=now, updated_at=now)
        if e.when.not_after is not None and e.when.not_after <= now:
            raise InvalidRequest("that window has already passed")
        e.note(now, "drafted", by=p.get("id"))
        self.store.insert(e)
        return e

    async def update(self, entry_id: str, *, title: str | None = None, notes: str | None = None,
                     action: dict[str, Any] | None = None, when: dict[str, Any] | str | None = None,
                     by: str | None = None, ctx: Any = None) -> Entry:
        """Edit an entry. Title and notes change only what a person reads. A new action, new inputs
        or a new time change what runs: an approved (scheduled) or waiting (proposed) entry is asked
        again, and a finished one that did not run (failed, skipped, expired) goes back to draft.
        A running, done or cancelled entry cannot be edited."""
        e = self.get(entry_id)
        prev = e.state
        if action is None and when is None:
            if prev == EntryState.RUNNING:
                raise AgendaStateError(f"entry {e.id} is running; wait for it to finish")
            if title is not None:
                e.title = _title(title)
            if notes is not None:
                e.notes = _notes(notes)
            e.updated_at = self.clock.now()
            if not self.store.put(e, expect={prev}):
                raise Conflict(f"entry {e.id} changed while it was being edited; read it again")
            return e
        if prev not in EDITABLE:
            raise AgendaStateError(f"entry {e.id} is {prev.value}; only an entry that has not run (draft, proposed, "
                                   "scheduled) or did not run (failed, skipped, expired) can be changed")
        now = self.clock.now()
        if action is not None:
            merged = self._action_patch(e.action, action)
            e.action = self._action(merged, grant_id=e.grant_id, principal=e.principal)
            if self._workspace(e.action) != e.workspace:
                raise InvalidRequest("an edit cannot move an entry to another workspace; draft a new one")
        if when is not None:
            e.when = parse_when(when, now)
        if title is not None:
            e.title = _title(title)
        if notes is not None:
            e.notes = _notes(notes)
        changed = e.approved_hash is None or action_hash(e.action, e.when) != e.approved_hash
        if prev in FINISHED:
            e.state, e.reason, e.run_id, e.event_id, e.proof, e.finished_at = (EntryState.DRAFT, None, None, None,
                                                                               None, None)
            e.approved_hash, e.approval, e.retry_at = None, None, None
        e.updated_at = now
        e.note(now, "edited", by=by)
        if prev in (EntryState.PROPOSED, EntryState.SCHEDULED) and changed:
            self._withdraw(e)
            self._spend_authority(e, "the entry was edited")
            e.approved_hash, e.retry_at, e.approval = None, None, None
            e.state, e.reason = EntryState.PROPOSED, "edited after it was proposed, so it is asked again"
            if not self.store.put(e, expect={prev}):
                raise Conflict(f"entry {e.id} changed while it was being edited; read it again")
            return await self.propose(e.id, ctx=ctx, by=by)
        if not self.store.put(e, expect={prev}):
            raise Conflict(f"entry {e.id} changed while it was being edited; read it again")
        return e

    @staticmethod
    def _action_patch(a: Action, patch: dict[str, Any]) -> dict[str, Any]:
        """An action edit: new params (merged over the old ones only when the target stays), a new
        lane, or a whole new target."""
        if not isinstance(patch, dict):
            raise InvalidRequest("action is an object: {flow, params, on} or {trigger, on}")
        if any(k in patch for k in ("flow", "connector", "trigger")):
            return patch
        key = "trigger" if a.kind == ActionKind.ROUTE else ("connector" if a.kind == ActionKind.CONNECTOR else "flow")
        out: dict[str, Any] = {key: a.target, "on": patch.get("on", a.on)}
        if a.kind != ActionKind.ROUTE:
            params = patch.get("params")
            if params is not None and not isinstance(params, dict):
                raise InvalidRequest("params must be a JSON object of name: value")
            out["params"] = {**a.inputs, **(params or {})}
            out["version"] = patch.get("version", a.version)
        return out

    async def propose(self, entry_id: str, *, ctx: Any = None, by: str | None = None) -> Entry:
        """Ask for permission to run it (see the module docstring). `ctx` is the caller's own policy
        context when an agent proposes over MCP (its untrusted-content state then counts). Without
        one, or when the caller acts under another grant than the entry's, the policy is asked under
        the entry's grant: the one every fire runs under."""
        e = self.get(entry_id)
        prev = e.state
        if prev == EntryState.SCHEDULED and e.approved_hash == action_hash(e.action, e.when):
            return e
        if prev not in (EntryState.DRAFT, EntryState.PROPOSED):
            raise AgendaStateError(f"entry {e.id} is {prev.value}; only a draft can be proposed")
        now = self.clock.now()
        if e.when.not_after is not None and e.when.not_after <= now:
            raise InvalidRequest(f"entry {e.id}'s window has passed; edit its time first")
        h = action_hash(e.action, e.when)
        if prev == EntryState.PROPOSED and (e.approval or {}).get("hash") == h and (e.approval or {}).get("id"):
            return e  # already waiting for this exact action
        policy = self._svc("policy")
        if policy is None:  # no policy service: every call on this node runs as the owner
            e.state, e.reason, e.approved_hash = EntryState.SCHEDULED, None, h
            e.approval = {"via": "owner", "hash": h, "at": now.isoformat(), "by": "owner"}
            e.note(now, "scheduled: this node has no policy service, so every call runs as the owner")
            return self._save(e, prev)
        effect, target, domain, summary, content = self._authorization(e)
        if ctx is not None and _grant_of(ctx) != e.grant_id:
            ctx = None  # another grant proposes it: ask under the grant every fire will run under
        if ctx is None:
            who = Principal(**e.principal) if e.principal.get("id") else None
            ctx = policy.context_for_grant(e.grant_id, principal=who, tool=TOOL)
        at = self._first_open(e, effect, target, now)
        closes, capped = self.request_expiry(e, now)
        try:
            d = await ctx.authorize(effect, lane_id=target, domain=domain, summary=summary, content=content,
                                    approval_expires_at=closes.timestamp(),
                                    **({"at": at.timestamp()} if at > now else {}))
        except ApprovalPending as pending:
            closes = self._closes(pending.approval_id) or closes  # what the request really holds
            e.state, e.reason = EntryState.PROPOSED, "waiting for a person to approve it"
            e.approval = {"id": pending.approval_id, "state": "pending", "hash": h, "asked_at": now.isoformat(),
                          "expires_at": closes.isoformat(), "capped": capped}
            e.updated_at = now
            e.note(now, "proposed: an approval is waiting in Needs you", approval_id=pending.approval_id, by=by,
                   open_until=closes.isoformat())
            return self._save(e, prev)
        ids = getattr(d, "pace_ids", None) or []
        if ids and hasattr(policy, "pace_forget"):
            policy.pace_forget(ids)  # the run claims the lane's pace when it fires, at the time it goes
        route = getattr(d, "route", "auto")
        via = {"auto": "auto", "standing": "standing", "approved": "approved"}.get(route, route)
        e.state, e.reason, e.approved_hash = EntryState.SCHEDULED, None, h
        e.approval = {"via": via, "hash": h, "at": now.isoformat(), "by": getattr(d, "via", None) or route,
                      **({"standing": d.via} if via == "standing" else {}),
                      **({"id": d.via} if via == "approved" else {})}
        e.updated_at = now
        how = {"auto": "the routing lets it run without asking (it is authorized again when it fires)",
               "standing": f"standing approval {getattr(d, 'via', '')} covers it",
               "approved": "a matching approval was already given"}.get(via, via)
        e.note(now, f"scheduled: {how}", by=by)
        return self._save(e, prev)

    def max_days(self) -> int:
        """node.toml `agenda_approval_max_days`, or APPROVAL_MAX_DAYS without a node config."""
        days = getattr(self._svc("config"), "agenda_approval_max_days", None)
        return int(days) if isinstance(days, int) and days >= 1 else APPROVAL_MAX_DAYS

    def request_expiry(self, e: Entry, now: datetime) -> tuple[datetime, bool]:
        """(when the request for this entry closes, whether the cap set it). The entry's start plus
        AT_GRACE (the slack an exact time already has), never sooner than an ordinary request closes
        (so a near entry waits as long as before), never later than max_days() after `now`. An
        "as soon as approved" entry has no start: the cap."""
        cap = now + timedelta(days=self.max_days())
        if e.when.not_before is None:
            return cap, True
        ordinary = getattr(self._svc("approvals"), "approval_ttl_s", None)
        floor = now + timedelta(seconds=ordinary if isinstance(ordinary, int | float) else DEFAULT_REQUEST_S)
        want = max(e.when.not_before + AT_GRACE, floor)
        return (cap, True) if want > cap else (want, False)

    def _closes(self, approval_id: str) -> datetime | None:
        try:
            ap = self._svc("approvals").get(approval_id)
        except (AgentosError, AttributeError):
            return None
        t = getattr(ap, "expires_at", None)
        return datetime.fromtimestamp(t, UTC) if isinstance(t, int | float) else None

    def _first_open(self, e: Entry, effect: Effect, target: str | None, now: datetime) -> datetime:
        """The first time inside the window the lane's pace lets it post, for the approval to read the
        pace at (ctx.authorize `at`). Refuses a window the pace leaves no room in."""
        start = max(now, e.when.not_before or now)
        verdict = self._pace(effect.value, target, start)
        if verdict is None:
            return start
        nxt, words = verdict
        if nxt is None or (e.when.not_after is not None and nxt > e.when.not_after):
            raise InvalidRequest(f"the lane's posting pace leaves no time inside this entry's window: {words}. "
                                 "Pick another time or a wider window")
        return nxt

    def _pace(self, effect: str, target: str | None, at: datetime) -> tuple[datetime | None, str] | None:
        """(when the lane may next post, the pace's words) when its pace or waking hours refuse a
        publish at `at`; None when it may go (or the action does not publish)."""
        policy = self._svc("policy")
        if effect not in COMMIT_EFFECTS or not target or policy is None or not hasattr(policy, "pace_verdict"):
            return None
        from agentos.policy import pace as pacing

        lane = policy.pace_lane(target) if hasattr(policy, "pace_lane") else target
        pace, zone, v = policy.pace_verdict(lane, at.timestamp())
        if pace is None or v is None or v.rule is None:
            return None
        hit = pacing.hit(lane, pace, zone, v, at.timestamp())
        nxt = datetime.fromtimestamp(v.next_ts, UTC) if v.next_ts is not None else None
        return nxt, str(hit.get("explain") or v.rule)

    async def approve(self, entry_id: str, *, by: str = "owner", decider: Any = None,
                      refuse_family: str | None = None, via: str | None = None, in_person: bool = False,
                      note: str | None = None) -> Entry:
        """A person approves the entry: through the same approval decision as Needs you (who may
        decide, four eyes, a token never approving its own family's request all apply). A draft is
        proposed first, under its own grant; one the routing lets run needs no decision."""
        e = self.get(entry_id)
        if e.state == EntryState.DRAFT:
            e = await self.propose(e.id, by=by)
        if e.state == EntryState.SCHEDULED:
            return e
        if e.state != EntryState.PROPOSED or not (e.approval or {}).get("id"):
            raise AgendaStateError(f"entry {e.id} is {e.state.value}; only a proposed entry waits for approval")
        approvals = self._svc("approvals")
        if approvals is None:
            raise ServiceUnavailable("approvals need the policy service on this node")
        kw: dict[str, Any] = {"by": by, "note": note}
        if decider is not None:
            kw.update(decider=decider, via=via, in_person=in_person)
        elif refuse_family is not None:
            kw["refuse_family"] = refuse_family
        approvals.decide(e.approval["id"], "approve", **kw)
        self._sync_one(self.get(e.id))
        return self.get(e.id)

    async def cancel(self, entry_id: str, *, by: str | None = None, reason: str | None = None) -> Entry:
        """Call it off. A waiting approval is withdrawn; a run that has not finished is cancelled
        (a running flow stops at its next step)."""
        e = self.get(entry_id)
        prev = e.state
        if prev == EntryState.CANCELLED:
            return e
        if prev in FINISHED:
            raise AgendaStateError(f"entry {e.id} is already {prev.value}")
        now = self.clock.now()
        if prev == EntryState.RUNNING and e.run_id:
            try:
                await self.runner.cancel(e.run_id, by=by)
            except NotFound:
                pass
        self._withdraw(e)
        self._spend_authority(e, "the entry was cancelled")
        e.state, e.finished_at, e.updated_at = EntryState.CANCELLED, now, now
        e.reason = clip_text(reason, 300) if reason else f"cancelled by {by or 'the owner'}"
        e.note(now, "cancelled", by=by)
        return self._save(e, prev)

    def _withdraw(self, e: Entry) -> None:
        """End a request that no longer matches the entry (it was edited or cancelled)."""
        ap = (e.approval or {}).get("id")
        if not ap or (e.approval or {}).get("state") != "pending":
            return
        withdraw = getattr(self._svc("approvals"), "withdraw", None)
        if callable(withdraw):
            try:
                withdraw(ap)
            except AgentosError:
                log.warning("could not withdraw approval %s of agenda entry %s", ap, e.id)
                return
            # the entry's copy follows: a cancelled entry never shows its card as pending (QA 2026-11-10 A-12)
            e.approval = {**(e.approval or {}), "state": "withdrawn"}

    def _save(self, e: Entry, prev: EntryState) -> Entry:
        e.updated_at = self.clock.now()
        if not self.store.put(e, expect={prev}):
            raise Conflict(f"entry {e.id} changed while this was being done; read it again")
        return e

    # ------------------------------------------------------------ approvals decided

    def sync(self) -> int:
        """Pick up decisions on the approvals proposed entries wait for. Returns how many moved."""
        n = 0
        for e in self.store.list(states=[EntryState.PROPOSED]):
            try:
                n += self._sync_one(e)
            except Exception:
                log.exception("agenda entry %s: reading its approval failed", e.id)
        return n

    def _sync_one(self, e: Entry) -> int:
        ap_id = (e.approval or {}).get("id")
        approvals = self._svc("approvals")
        if e.state != EntryState.PROPOSED or not ap_id or approvals is None:
            return 0
        now = self.clock.now()
        try:
            ap = approvals.get(ap_id)
            state = str(getattr(ap, "state", None) or (ap.get("state") if isinstance(ap, dict) else ""))
        except NotFound:
            ap, state = None, "expired"
        expires = getattr(ap, "expires_at", None)
        if state == "pending" and isinstance(expires, int | float) and expires <= now.timestamp():
            state = "expired"
        if state == "pending":
            return 0
        prev = e.state
        if state in ("approved", "consumed"):
            if state == "approved" and hasattr(approvals, "consume") and not approvals.consume(ap_id):
                fresh = approvals.get(ap_id)
                if getattr(fresh, "state", None) != "consumed":
                    return 0
            edit = getattr(ap, "edit", None)
            if isinstance(edit, dict) and isinstance(edit.get("params"), dict):
                problem = self._apply_edit(e, edit["params"], getattr(ap, "content_digest", None))
                if problem:
                    e.state, e.reason, e.approval = EntryState.DRAFT, problem, {**e.approval, "state": "approved"}
                    e.note(now, "the approved edit cannot run", reason=problem)
                    return int(self.store.put(e, expect={prev}))
            by = getattr(ap, "decided_by", None) or "a person"
            e.state, e.reason, e.approved_hash = EntryState.SCHEDULED, None, action_hash(e.action, e.when)
            e.approval = {**e.approval, "via": "person", "state": "approved", "by": by,
                          "at": _ts(getattr(ap, "decided_at", None)) or now.isoformat(), "hash": e.approved_hash}
            approver_of = getattr(self._svc("policy"), "approver_of", None)
            approver = approver_of(getattr(ap, "decided_grant", None)) if callable(approver_of) else None
            if approver:  # the person, and the grant they decided with, bound to the hash they approved
                e.approval["approver"] = approver
            if isinstance(edit, dict) and edit.get("params"):
                e.approval["edited"] = sorted(edit["params"])
            e.note(now, f"approved by {by}" + (" with an edit" if isinstance(edit, dict) and edit else ""))
        elif state == "denied":
            by = getattr(ap, "decided_by", None) or "a person"
            note = getattr(ap, "note", None)
            e.state = EntryState.DRAFT
            e.reason = f"{by} denied it" + (f": {clip_text(note, 200)}" if note else "") + \
                ". Change it before proposing it again"
            e.approval = {**e.approval, "state": "denied", "by": by}
            e.note(now, "denied", by=by)
        else:  # expired, or withdrawn
            e.state = EntryState.DRAFT
            e.reason = "nobody decided before the approval request expired; propose it again"
            if e.approval.get("capped") and e.when.not_before is not None:
                e.reason = (f"nobody decided within the {self.max_days()} days a request for an agenda entry stays "
                            "open (agenda_approval_max_days); propose it again closer to its date")
            elif e.approval.get("capped"):
                e.reason = (f"nobody decided within the {self.max_days()} days a request for an agenda entry stays "
                            "open (agenda_approval_max_days); propose it again")
            e.approval = {**e.approval, "state": "expired"}
            e.note(now, "the approval request expired")
        e.updated_at = now
        return int(self.store.put(e, expect={prev}))

    def _apply_edit(self, e: Entry, params: dict[str, Any], digest: str | None) -> str | None:
        """The person approved with an edit: the entry runs what they approved. Returns a problem in
        words when the edited inputs cannot run or do not match what they approved."""
        from agentos.policy.approvals import content_digest

        before = e.action
        try:
            if before.kind == ActionKind.ROUTE:
                return "a trigger has no inputs to edit"
            key = "connector" if before.kind == ActionKind.CONNECTOR else "flow"
            e.action = self._action({key: before.target, "version": before.version, "on": before.on,
                                     "params": {**before.inputs, **params}},
                                    grant_id=e.grant_id, principal=e.principal)
            content = self._authorization(e)[4]
        except AgentosError as exc:
            e.action = before
            return f"the approved edit cannot run: {exc.message}"
        if digest is None or content_digest(content) != digest:
            e.action = before
            return "the edited content does not match what the approver approved"
        return None

    # ------------------------------------------------------------ firing

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
        now = self.clock.now()
        start = e.when.not_before
        if start is not None and start > now:
            return
        if e.retry_at is not None and e.retry_at > now:
            out["waiting"] += 1
            return
        end = e.when.not_after
        if end is not None and now > end:
            why = "its window closed before it ran (the node was down or busy at the time)"
            if e.retry_at is not None:
                why = f"its window closed while it waited: {e.reason or 'the lane was not ready'}"
            out["expired"] += self._finish(e, EntryState.SKIPPED if e.retry_at else EntryState.EXPIRED, why)
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
        paced = self._pace(e.action.effect, e.action.acts_on, now)
        if paced is not None:
            nxt, words = paced
            if nxt is None or (end is not None and nxt > end):
                out["finished"] += self._finish(e, EntryState.SKIPPED, f"the lane's posting pace kept it out of its "
                                                f"window: {words}")
                return
            self._wait(e, nxt, f"waiting for the lane's pace: {words}")
            out["waiting"] += 1
            return
        from agentos.runner.connectors.surfaces import ConnectorBroken, PaceLimited

        via = (e.approval or {}).get("via")
        try:
            grant_id = self._run_grant(e, standing, now)
        except PolicyDenied as exc:  # the drafter's grant expired, and the approver no longer stands behind it
            why = getattr(exc, "detail", None) or exc.message
            out["finished"] += self._finish(e, EntryState.FAILED, f"it could not start: the grant it was drafted "
                                            f"under expired ({standing['reason']}), and {why}")
            return
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
            return
        except ConnectorBroken as exc:
            if end is not None and now + CONNECTOR_RETRY > end:
                out["finished"] += self._finish(e, EntryState.SKIPPED, f"held: {exc.message}")
            else:
                self._wait(e, now + CONNECTOR_RETRY, f"held: {exc.message}")
            return
        except AgentosError as exc:  # the grant was revoked, the flow is gone, the inputs went stale
            out["finished"] += self._finish(e, EntryState.FAILED, f"it could not start: {exc.code}: {exc.message}")
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

    # ------------------------------------------------------------ the timeline

    def timeline(self, *, start: datetime, end: datetime, workspace: str | None = None, lane: str | None = None,
                 kind: str | None = None, states: list[str] | None = None, visible: Any = None,
                 limit: int = 500, managed: bool = True) -> dict[str, Any]:
        """One timeline for [start, end): one-off entries, the projected fires of recurring cron
        schedules (computed, never stored), the runs that already happened, with their proof, and
        (unless `managed` is False) what other services plan on their own (see _managed).
        `visible(lane or surface, workspace) -> bool` limits it to what the caller may see. Every item has `type` (entry,
        projected, run, managed), `at`, `workspace`, `lane`, `kind` (flow, connector, route; keepalive or
        engage for a managed item) and `state`. `managed_by` says, per service, what it can and cannot show."""
        if end <= start:
            raise InvalidRequest("the range's end must be after its start")
        if end - start > timedelta(days=93):
            raise InvalidRequest("a timeline covers at most 93 days at a time")
        items: list[dict[str, Any]] = []
        for e in self.store.list(workspace=workspace, start=start, end=end, limit=limit):
            items.append(self.describe(e))
        items += self._projected(start, end, workspace)
        items += self._past_runs(start, end, workspace, limit)
        about: dict[str, Any] = {}
        if managed:
            got, about = self._managed(start, end, workspace)
            items += got

        def sees(target: str | None, ws: str | None) -> bool:
            return visible is None or visible(target, ws)

        for info in about.values():  # the lanes a service skipped or paused are the caller's to see only
            for key in ("skipped", "paused"):
                if key in info:
                    info[key] = [x for x in info[key] if sees(x.get("lane"), (x.get("lane") or "").split("/")[0])]

        def keep(it: dict[str, Any]) -> bool:
            if not sees(it.get("surface") or it.get("lane"), it.get("workspace")):
                return False
            if lane and it.get("lane") != lane and it.get("surface") != lane:
                return False
            if kind and it.get("kind") != kind:
                return False
            return not states or it.get("state") in states
        items = [it for it in items if keep(it)]
        items.sort(key=lambda it: (it.get("at") or it.get("created_at") or "", it.get("id") or ""))
        truncated = len(items) > limit
        return {"start": start.isoformat(), "end": end.isoformat(), "workspace": workspace,
                "items": items[:limit], "truncated": truncated,
                "counts": _counts(items), "managed_by": about}

    def _managed(self, start: datetime, end: datetime, workspace: str | None) -> tuple[list[dict], dict[str, Any]]:
        """What keepalive and engagement plan on their own, from each service's `planned(start, end)`
        (built from its own planner, never planned again here). Read only: an item has no approval and
        cannot be cancelled here; `manage` says where it is managed. A service that is not running, or
        cannot say, adds no items and a note saying so; nothing is guessed."""
        items: list[dict[str, Any]] = []
        about: dict[str, Any] = {}
        for name, label in MANAGED.items():
            planned = getattr(self._svc(name), "planned", None)
            if not callable(planned):
                about[name] = {"available": False, "note": f"{label} is not running here, so its plans are not shown"}
                continue
            try:
                got = planned(start, end, workspace=workspace)
            except AgentosError as exc:  # it says why (agentosd is not running, for the CLI)
                about[name] = {"available": False, "note": exc.message}
                continue
            except Exception as exc:  # noqa: BLE001 - another service's trouble never breaks the agenda
                log.warning("agenda: %s could not say what it plans: %s", name, exc)
                about[name] = {"available": False, "note": f"{label} could not say what it plans now"}
                continue
            about[name] = {"available": True, **{k: v for k, v in got.items() if k != "items"}}
            for it in got.get("items") or []:
                at = it.get("at")
                items.append({**it, "id": f"{name}:{it.get('lane')}@{at}", "type": "managed", "source": name,
                              "managed_by": name, "kind": name, "state": "planned",
                              "title": it.get("title") or label})
        return items, about

    def _projected(self, start: datetime, end: datetime, workspace: str | None) -> list[dict[str, Any]]:
        runner = self._svc("runner")
        sched = getattr(runner, "scheduler", None)
        if sched is None:
            return []
        from agentos.runner.models import ScheduleKind
        from agentos.runner.schedules import _zone, cron_slots

        now = self.clock.now()
        lo = max(start, now)
        out: list[dict[str, Any]] = []
        for s in sched.list("cron"):
            if not s.enabled or s.kind != ScheduleKind.CRON or s.cron is None:
                continue
            ws = _schedule_workspace(s, self)
            if workspace is not None and ws != workspace:
                continue
            try:
                slots = cron_slots(s.cron.cron, _zone(s.cron.timezone), lo, end, limit=3000)
            except AgentosError:
                continue
            kind = "route" if s.route_id else ("connector" if s.flow_id and is_connector_id(s.flow_id) else "flow")
            name, routine = self._route_name(s.route_id) if s.route_id else (s.flow_id, None)
            for slot in slots[:200]:
                if slot >= end:
                    continue
                item = {"id": f"{s.id}@{slot.isoformat()}", "type": "projected", "schedule_id": s.id,
                        "at": slot.isoformat(), "workspace": ws, "lane": s.lane,
                        **({"surface": s.surface} if s.surface else {}), "kind": kind,
                        "target": s.flow_id or s.route_id, "state": "projected", "cron": s.cron.cron,
                        "timezone": s.cron.timezone, "title": f"{name or s.route_id} ({' '.join(s.cron.cron.split())})",
                        **({"workflow": routine} if routine else {})}
                paced = sched.paced_slot(s, slot) if s.flow_id else None
                if paced is not None:
                    item["paced"] = paced
                out.append(item)
        return out

    def _route_name(self, route_id: str) -> tuple[str | None, str | None]:
        """(the name a person gave it, the workflow it starts) of a route schedule, so the agenda shows
        "Tester A tick routine", not rt_f599109119d5 (QA 2026-11-10 A-8)."""
        try:
            route = self._events().get_route(route_id)
        except AgentosError:
            return None, None
        target = getattr(route, "target", None)
        return route.name, getattr(target, "workflow", None)

    def _past_runs(self, start: datetime, end: datetime, workspace: str | None, limit: int) -> list[dict[str, Any]]:
        runner = self._svc("runner")
        if runner is None:
            return []
        out = []
        for run in runner.store.list_runs(workspace=workspace, limit=max(limit, 200)):
            at = run.started_at or run.created_at
            if not start <= at < end or (run.trigger or {}).get("kind") == "agenda":
                continue  # an entry's own run is shown as the entry
            state = ("done" if run.status in SUCCEEDED else "running" if run.status not in TERMINAL
                     else "cancelled" if run.status == RunStatus.CANCELLED else "failed")
            out.append({"id": run.id, "type": "run", "run_id": run.id, "schedule_id": run.schedule_id,
                        "at": at.isoformat(), "workspace": run.workspace, "lane": run.lane,
                        **({"surface": run.surface} if run.surface else {}),
                        "kind": "connector" if run.surface else "flow", "target": run.flow_id,
                        "state": state, "status": str(run.status), "title": run.flow_id or str(run.kind),
                        "source": run.source, "proof": run_proof(run, runner) if run.terminal else None})
        return out


def run_proof(run: Run, runner: Any) -> dict[str, Any]:
    """What a finished run left as proof: its status and summary, readbacks of the target's own
    state, the final URL and screenshot ref, whether a connector returned an output. Only what the
    run recorded; an empty field means the run left none."""
    p = run.proof or {}
    rbs = [{"passed": rb.get("passed"), "expected": rb.get("expected"), "found": rb.get("found")}
           for rb in (p.get("readbacks") or []) if isinstance(rb, dict)]
    out: dict[str, Any] = {"run_id": run.id, "status": str(run.status), "summary": runner._summary(run),
                           "readbacks": clip(rbs, max_str=200, max_items=20), "url": p.get("url"),
                           "screenshot": p.get("screenshot"),
                           "finished_at": run.finished_at.isoformat() if run.finished_at else None}
    if run.output is not None:
        out["output"] = "the connector returned a typed result; read it with run_status"
    if not rbs and not p.get("url") and run.output is None:
        out["note"] = "the run recorded no readback, page or output; its status is the only proof"
    return out


def _schedule_workspace(s: Any, svc: AgendaService) -> str | None:
    target = s.surface or s.lane
    if target:
        return target.split("/", 1)[0]
    if s.route_id:
        try:
            return svc._events().get_route(s.route_id).workspace
        except AgentosError:
            return None
    return None


def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[it.get("state") or "?"] = out.get(it.get("state") or "?", 0) + 1
    return out


def _grant_of(ctx: Any) -> str | None:
    gid = getattr(getattr(ctx, "grant", None), "id", None)
    return gid or getattr(getattr(ctx, "principal", None), "grant_id", None)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _ts(t: Any) -> str | None:
    return datetime.fromtimestamp(t, UTC).isoformat() if isinstance(t, int | float) else None


def _title(title: Any) -> str:
    t = " ".join(str(title or "").split())
    if not t:
        raise InvalidRequest("an agenda entry needs a title")
    if len(t) > MAX_TITLE:
        raise InvalidRequest(f"a title is at most {MAX_TITLE} characters")
    return t


def _notes(notes: Any) -> str | None:
    if notes is None:
        return None
    n = str(notes).strip()
    if len(n) > MAX_NOTES:
        raise InvalidRequest(f"notes are at most {MAX_NOTES} characters")
    return n or None


def build(runtime: Any, *, home: Path | None = None, clock: Clock | None = None) -> AgendaService:
    """Composition-root hook: the agenda, registered as "agenda" and ticked by the runner's scheduler."""
    cfg = runtime.get("config") if runtime is not None else None
    home = Path(home or (cfg.home if cfg is not None else home_from_env()))
    svc = AgendaService(AgendaStore(home / DB_NAME), runtime, clock=clock)
    svc.attach()
    if runtime is not None:
        runtime.register(SERVICE_NAME, svc)
    return svc
