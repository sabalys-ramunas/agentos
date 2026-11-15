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

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentos.agenda.firing import FiringMixin
from agentos.agenda.models import (
    EDITABLE,
    FINISHED,
    Action,
    ActionKind,
    Entry,
    EntryState,
    action_hash,
)
from agentos.agenda.rules import (
    APPROVAL_MAX_DAYS,
    AT_GRACE,
    COMMIT_EFFECTS,
    DEFAULT_REQUEST_S,
    OWNER_GRANT,
    SERVICE_NAME,
    TOOL,
    AgendaStateError,
    clean_notes,
    clean_title,
    grant_of,
    iso,
    parse_when,
    principal_dict,
    ts_of,
)
from agentos.agenda.store import DB_NAME, AgendaStore
from agentos.agenda.timeline import TimelineMixin
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
from agentos.core.models import Effect, Principal, parse_lane_id
from agentos.runner.names import is_connector_id
from agentos.runner.owner import is_owner
from agentos.runner.util import Clock, clip, clip_text, new_id

log = logging.getLogger("agentos.agenda")



SECRET_WORDS = ("password", "passphrase", "secret", "token", "api_key", "private_key", "cookie", "otp", "totp",
                "pin", "code")


def _secret_name(name: str) -> bool:
    """An input named like a secret stays hidden even from the agent that gave it (it never needs one
    back, and a read back would put it in that agent's context again)."""
    k = str(name).lower()
    return any(k == w or k.endswith(f"_{w}") or k.startswith(f"{w}_") for w in SECRET_WORDS)

class AgendaService(FiringMixin, TimelineMixin):
    """The agenda. Here: wiring, reading, resolving an action, writing and approvals decided.
    Firing and following are in firing.py, the timeline in timeline.py."""

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

    def describe(self, e: Entry, *, drafter: str | None = None) -> dict[str, Any]:
        """The entry as shown. `drafter`: the caller's grant id; the grant that drafted it reads back
        the inputs it gave (QA 2026-11-15 P2-8), every other reader sees only those the flow shows."""
        a = e.action
        d: dict[str, Any] = {
            "id": e.id, "type": "entry", "workspace": e.workspace, "title": e.title, "notes": e.notes,
            "state": e.state.value, "reason": e.reason,
            "action": {"kind": a.kind.value, "target": a.target, "version": a.version,
                       "inputs": clip(self._shown_inputs(a, own=bool(drafter) and drafter == e.grant_id),
                                      max_str=300), "on": a.on, "lane": a.lane,
                       **({"surface": a.surface} if a.surface else {}), "effect": a.effect},
            "lane": a.lane, "kind": a.kind.value, "effect": a.effect,
            "when": {"kind": e.when.kind, "not_before": iso(e.when.not_before),
                     "not_after": iso(e.when.not_after), "words": e.when.words()},
            "at": iso(e.when.not_before),
            "approval": dict(e.approval) if e.approval else None,
            "approved": bool(e.approved_hash) and e.approved_hash == action_hash(e.action, e.when),
            "run_id": e.run_id, "event_id": e.event_id, "retry_at": iso(e.retry_at),
            "proof": e.proof, "history": e.history[-10:],
            "drafted_by": {k: e.principal.get(k) for k in ("kind", "id") if e.principal.get(k)},
            "grant_id": e.grant_id, "authority": dict(e.authority) if e.authority else None,
            "created_at": iso(e.created_at), "updated_at": iso(e.updated_at),
            "finished_at": iso(e.finished_at), "next": self._next_words(e),
        }
        return d

    def _shown_inputs(self, a: Action, *, own: bool = False) -> dict[str, Any]:
        """Inputs as shown: values a flow does not mark `show` read "(hidden)", like a run's params.
        `own`: the drafting grant reads back what it gave, except anything named like a secret."""
        if a.kind == ActionKind.ROUTE:
            return dict(a.inputs)
        if own:
            return {k: "(hidden)" if _secret_name(k) else v for k, v in a.inputs.items()}
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
        p = principal_dict(principal)
        grant_id = grant_id or p.get("grant_id") or (OWNER_GRANT if not p.get("id") or is_owner(p) else None)
        now = self.clock.now()
        a = self._action(action, grant_id=grant_id, principal=p)
        ws = self._workspace(a)
        if workspace is not None and workspace != ws:
            raise InvalidRequest(f"the action acts in workspace {ws}, not {workspace}")
        e = Entry(id=new_id("ag"), workspace=ws, title=clean_title(title), notes=clean_notes(notes), action=a,
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
                e.title = clean_title(title)
            if notes is not None:
                e.notes = clean_notes(notes)
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
            e.title = clean_title(title)
        if notes is not None:
            e.notes = clean_notes(notes)
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
        if ctx is not None and grant_of(ctx) != e.grant_id:
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
        """Bring a proposed entry up to date with its approval card: approved (with an edit, maybe),
        denied, or expired. 1 when the entry changed."""
        ap_id = (e.approval or {}).get("id")
        approvals = self._svc("approvals")
        if e.state != EntryState.PROPOSED or not ap_id or approvals is None:
            return 0
        now = self.clock.now()
        ap, state = _card_state(approvals, ap_id, now)
        if state == "pending":
            return 0
        prev = e.state
        if state in ("approved", "consumed"):
            if state == "approved" and hasattr(approvals, "consume") and not approvals.consume(ap_id):
                fresh = approvals.get(ap_id)
                if getattr(fresh, "state", None) != "consumed":
                    return 0
            problem = self._approved(e, ap, now)
            if problem:
                e.state, e.reason, e.approval = EntryState.DRAFT, problem, {**e.approval, "state": "approved"}
                e.note(now, "the approved edit cannot run", reason=problem)
                return int(self.store.put(e, expect={prev}))
        elif state == "denied":
            by = getattr(ap, "decided_by", None) or "a person"
            note = getattr(ap, "note", None)
            e.state = EntryState.DRAFT
            e.reason = f"{by} denied it" + (f": {clip_text(note, 200)}" if note else "") + \
                ". Change it before proposing it again"
            e.approval = {**e.approval, "state": "denied", "by": by}
            e.note(now, "denied", by=by)
        else:  # expired, or withdrawn
            self._expired(e, now)
        e.updated_at = now
        return int(self.store.put(e, expect={prev}))

    def _approved(self, e: Entry, ap: Any, now: datetime) -> str | None:
        """A person approved the card: the entry is scheduled under the hash they approved, with their
        edit applied. The problem, when the edit cannot run (the entry is not changed then)."""
        edit = getattr(ap, "edit", None)
        if isinstance(edit, dict) and isinstance(edit.get("params"), dict):
            problem = self._apply_edit(e, edit["params"], getattr(ap, "content_digest", None))
            if problem:
                return problem
        by = getattr(ap, "decided_by", None) or "a person"
        e.state, e.reason, e.approved_hash = EntryState.SCHEDULED, None, action_hash(e.action, e.when)
        e.approval = {**e.approval, "via": "person", "state": "approved", "by": by,
                      "at": ts_of(getattr(ap, "decided_at", None)) or now.isoformat(), "hash": e.approved_hash}
        approver_of = getattr(self._svc("policy"), "approver_of", None)
        approver = approver_of(getattr(ap, "decided_grant", None)) if callable(approver_of) else None
        if approver:  # the person, and the grant they decided with, bound to the hash they approved
            e.approval["approver"] = approver
        if isinstance(edit, dict) and edit.get("params"):
            e.approval["edited"] = sorted(edit["params"])
        e.note(now, f"approved by {by}" + (" with an edit" if isinstance(edit, dict) and edit else ""))
        return None

    def _expired(self, e: Entry, now: datetime) -> None:
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

    def _apply_edit(self, e: Entry, params: dict[str, Any], digest: str | None) -> str | None:
        """The person approved with an edit: the entry runs what they approved. Returns a problem in
        words when the edited inputs cannot run or do not match what they approved."""
        from agentos.policy.approval_binding import content_digest

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


    # ------------------------------------------------------------ the timeline


def _card_state(approvals: Any, ap_id: str, now: datetime) -> tuple[Any, str]:
    """The approval card and its state; a card gone or past its expiry is expired."""
    try:
        ap = approvals.get(ap_id)
        state = str(getattr(ap, "state", None) or (ap.get("state") if isinstance(ap, dict) else ""))
    except NotFound:
        ap, state = None, "expired"
    expires = getattr(ap, "expires_at", None)
    if state == "pending" and isinstance(expires, int | float) and expires <= now.timestamp():
        state = "expired"
    return ap, state


def build(runtime: Any, *, home: Path | None = None, clock: Clock | None = None) -> AgendaService:
    """Composition-root hook: the agenda, registered as "agenda" and ticked by the runner's scheduler."""
    cfg = runtime.get("config") if runtime is not None else None
    home = Path(home or (cfg.home if cfg is not None else home_from_env()))
    svc = AgendaService(AgendaStore(home / DB_NAME), runtime, clock=clock)
    svc.attach()
    if runtime is not None:
        runtime.register(SERVICE_NAME, svc)
    return svc
