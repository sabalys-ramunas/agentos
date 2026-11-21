"""Workflows on a node: saving them, turning them on, starting runs, and what people read.

`build(runner, runtime)` makes the service and registers it as runtime service `workflows`. Saving
goes through the checks in `check.py`. Turning one on makes the events route and schedule that start
it. Who is a person is decided by the layer that took the call (the panel, the CLI, a controller's
signed message) and arrives as `Actor.person`; an agent's tool call is never one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from openajar.core.errors import InvalidRequest, NotFound, OpenajarError, PolicyDenied, ServiceUnavailable
from openajar.core.models import Effect, Principal, RunKind, RunStatus
from openajar.runner.connectors.models import BINDING_PARAMS
from openajar.runner.flows import validation_message
from openajar.runner.names import is_connector_id, lane_matches
from openajar.runner.owner import answered_in_person
from openajar.runner.records import ACTIVE, StepStatus
from openajar.runner.run_rules import OWNER_GRANT
from openajar.runner.steps import iter_steps
from openajar.runner.util import Clock
from openajar.runner.workflows import leads as leads_module
from openajar.runner.workflows import sitechange
from openajar.runner.workflows import templates as template_data
from openajar.runner.workflows.check import (
    TRIGGER_FIELDS,
    TURN_ON_ONLY,
    Field,
    Issue,
    Operation,
    check,
    operations_in,
    param_label,
)
from openajar.runner.workflows.engine import WorkflowExecutor
from openajar.runner.workflows.mail import MAIL_OPERATION, MailService, MailTransport
from openajar.runner.workflows.models import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowVersion,
)
from openajar.runner.workflows.replies import Replies
from openajar.runner.workflows.sends import Sends
from openajar.runner.workflows.store import WorkflowStore
from openajar.runner.workflows.views import ViewsMixin

log = logging.getLogger("openajar.runner")

SERVICE_NAME = "workflows"
MAIL_POLL_S = 60.0
FIELD_KEY_RE = re.compile(r"^[\w-]{1,60}$")
MAX_WEBHOOK_KEYS = 200
SOURCES = {"schedule": "cron", "webhook": "webhook", "email": "mail", "page_change": "watch", "site_change": "watch"}
ORIGIN_KINDS = {"webhook": "webhook", "mail": "email", "watch": "web_page"}
CAUSES = {"cron": "On schedule", "webhook": "A webhook arrived", "mail": "An email arrived", "watch": "A page changed"}
WHOLE_PAGE = {"css": "body"}
MAX_BACKLOG_SCAN = 1000
TURN_ON = "Turn it on"


class WorkflowInvalid(InvalidRequest):
    """A definition broke a rule, or is not finished. `problems` says where, for a person."""

    def __init__(self, message: str, problems: list[Issue]):
        super().__init__(message)
        self.problems = [p.public() for p in problems]


def required_params(flow: Any) -> frozenset[str]:
    """The params a step must be given. The hidden binding params come from the surface, and a send's address
    comes from its list, so neither is something a step fills in."""
    send_to = flow.sends.to if flow.sends is not None else None
    return frozenset(name for name in flow.params.get("required") or () if name not in BINDING_PARAMS and name != send_to)


@dataclass
class Actor:
    """Who is calling. `person` is set by the layer that knows: never from a request body."""
    principal: dict[str, Any] = field(default_factory=dict)
    grant_id: str | None = None
    person: bool = False
    by: str = "owner"

    @classmethod
    def owner(cls, by: str = "owner") -> Actor:
        return cls({"kind": "human", "id": "owner", "owner": "owner"}, OWNER_GRANT, True, by)

    def saved_by(self) -> dict[str, Any]:
        keep = {k: v for k, v in self.principal.items() if k in ("kind", "id", "owner")}
        return {**keep, "grant_id": self.grant_id, "person": self.person, "by": self.by}


def for_an_agent(row: dict[str, Any]) -> dict[str, Any]:
    """A list, or a workflow's row, for an agent: a list's size, and never how many on it are
    suppressed, which would tell it who is on the list as it suppresses them one at a time."""
    if "send_limits" in row:
        row = {**row, "send_limits": [for_an_agent(x) for x in row["send_limits"]]}
    return {k: v for k, v in row.items() if k != "suppressed"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "workflow"


def _url_params(fv: Any) -> set[str]:
    """Params a flow uses in an address: what fills them says where it goes."""
    from openajar.core.template import TEMPLATE_RE

    found: set[str] = set()

    def scan(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                scan(v, k)
        elif isinstance(value, list):
            for v in value:
                scan(v, key)
        elif isinstance(value, str) and key == "url":
            found.update(m.group(1).split(".")[0] for m in TEMPLATE_RE.finditer(value))

    for _, step in iter_steps(fv.definition.steps):
        scan(step.model_dump(mode="json", by_alias=True, exclude_none=True))
    return found


def _produced(fv: Any, connector: bool) -> list[Field]:
    """What an operation gives the steps after it: a connector's outputs (always untrusted), else what
    the flow's operation block declares (untrusted unless the owner saved it)."""
    if connector:
        return [Field(k, k, "text", True) for k in (fv.definition.connector or {}).get("outputs") or {}]
    block = fv.definition.operation
    return [Field(f.key, f.label or "", f.type, f.untrusted or not fv.owner_saved)
            for f in (block.produces if block else [])]


class WorkflowService(ViewsMixin):
    """Routines: saving, switching on and off, wiring triggers, runs and retries. What people
    read (cards, the day, run views) is in views.py."""

    def __init__(self, runner: Any, *, runtime: Any = None, clock: Clock | None = None,
                 transport: MailTransport | None = None, public_base: Any = None):
        self.runner, self.runtime = runner, runtime
        self.flows = runner.flows
        self.clock = clock or runner.clock
        self.store = WorkflowStore(runner.store)
        self.sends = Sends(self.store, self.clock, timezone=self._timezone, public_base=public_base or self._public_base)
        self.mail = MailService(self.store, vault=self._svc("vault"), transport=transport)
        self.replies = Replies(self.store, self.clock)
        self.leads = leads_module.Leads(self)
        self.executor = WorkflowExecutor(self)
        self.site_changes = sitechange.SiteChanges(self)
        self._polled_at: float | None = None
        self._polls: dict[str, asyncio.Task[None]] = {}  # a mailbox being read now
        runner.register_executor(RunKind.WORKFLOW, self.executor)
        runner.on_finished.append(self.on_run_finished)
        if runner.scheduler is not None:
            runner.scheduler.tick_hooks.append(self.tick)
            runner.scheduler.fire_checks.append(self.site_changes.check_fire)

    def _svc(self, name: str) -> Any:
        return self.runtime.get(name) if self.runtime is not None else None

    def _timezone(self) -> str | None:
        policy = self._svc("policy")
        return getattr(getattr(getattr(policy, "cfg", None), "meters", None), "timezone", None)

    def _public_base(self) -> str | None:
        """Where an unsubscribe link points: the controller, which relays it. A node has no public address."""
        channel = self._svc("channel")
        fn = getattr(channel, "unsubscribe_base", None)
        return fn() if callable(fn) else None

    # ---- what a step's operation means ----
    def operation(self, text: str) -> Operation | None:
        """What a step names (`flow_id` or `flow_id@on`) as an Operation, or None when there is none
        to run there: the built-in mail send, a saved flow on its lane, or a connector on a surface."""
        flow_id, _, on = text.partition("@")
        if flow_id == MAIL_OPERATION:
            return self._mail_operation(text, on)
        fv = self.flows.store.get_flow(flow_id)
        if fv is None:
            return None
        d = fv.definition
        connector = is_connector_id(fv.id)
        if connector or d.lane_is_pattern:
            if not on or (not connector and not lane_matches(d.lane, on)):
                return None
        elif on and on != d.lane:
            return None
        block = d.operation
        meta = d.connector or {}
        return Operation(id=text, flow_id=fv.id, on=on or None, name=(block.name if block else None) or d.title or fv.id,
                         lane=on or d.lane, effect=fv.effect, params=dict(d.params.get("properties", {})),
                         required=required_params(d), produces=_produced(fv, connector), sends=d.sends,
                         targets=frozenset(block.targets if block else ()) | _url_params(fv),
                         tags=tuple(block.tags if block else ()), version=fv.version,
                         not_to_people=bool(block and block.not_to_people and fv.owner_saved),
                         site_api=connector and (fv.saved_by or {}).get("kind") == "local", output=meta.get("output") or {},
                         http=meta.get("http") or {}, pace=meta.get("pace") or {})

    def _mail_operation(self, text: str, on: str) -> Operation | None:
        """The built-in send from a connected mailbox."""
        box = self.mail.get(on) if on else None
        if box is None:
            return None
        from openajar.runner.flowdoc import SendsSpec

        return Operation(id=text, flow_id=MAIL_OPERATION, on=on, name="Send an email", lane=on, effect=Effect.COMMIT,
                         params={"subject": {"type": "string", "show": True}, "body": {"type": "string", "show": True}},
                         sends=SendsSpec(), tags=("mail.send",), builtin=True,
                         produces=[Field("sent", "Sent", "number"), Field("skipped", "Skipped", "number"),
                                   Field("failed", "Failed", "number"), Field("in_doubt", "In doubt", "number")])

    def site_problem(self, op: Operation, params: dict[str, Any]) -> str | None:
        """Why a watch cannot read this endpoint with these params on its surface, or None."""
        try:
            self.runner.connector_target(self.flows.store.get_flow(op.flow_id), op.on, params)
        except OpenajarError as e:
            return e.message
        return None

    def trigger_operation(self, defn: WorkflowDefinition) -> Operation | None:
        """What a site_change trigger reads, for the same checks as a step."""
        t = defn.trigger
        return self.operation(t.operation) if t.kind == "site_change" and t.operation else None

    def watches_on(self, flow_id: str) -> list[tuple[str, Any]]:
        """(routine, trigger) of each watch that is on and reads this flow. One that is off reads nothing,
        and turning it on checks the endpoint again."""
        found = []
        for state in self.store.states():
            wv = self.store.version(state.id, state.live) if state.enabled and state.live else None
            t = wv.definition.trigger if wv is not None else None
            if t is not None and t.kind == "site_change" and (t.operation or "").partition("@")[0] == flow_id:
                found.append((wv.definition.name, t))
        return found

    def list_workspace(self, list_id: str) -> str | None:
        info = self.store.list_info(list_id)
        return info["workspace"] if info else None

    def mailbox(self, mailbox_id: str) -> bool:
        return self.mail.get(mailbox_id) is not None

    # ---- saving ----
    def _parse(self, data: Any) -> WorkflowDefinition:
        if not isinstance(data, dict):
            raise InvalidRequest("a workflow definition is a JSON object")
        try:
            return WorkflowDefinition.model_validate(data)
        except ValidationError as e:
            raise InvalidRequest(f"invalid workflow: {validation_message(e)}") from e

    def _switches(self) -> Any:
        return self._svc("ask_first")

    def _state(self, flow_id: str) -> WorkflowState:
        state = self.store.state(flow_id)
        if state is None:
            raise NotFound(f"workflow {flow_id!r} is not saved on this node")
        return state

    def get_version(self, flow_id: str, version: str | None = None) -> WorkflowVersion:
        state = self._state(flow_id)
        wv = self.store.version(flow_id, version or state.head)
        if wv is None:
            raise NotFound(f"workflow {flow_id!r} has no version {version}")
        return wv

    def check_steps_in_grant(self, defn: WorkflowDefinition, grant_id: str | None, principal: dict[str, Any]) -> None:
        """Every step inside the grant it would run under, now. Each is checked again before it acts."""
        ops = [op for _, op in operations_in(defn, self.operation)]
        watched = self.trigger_operation(defn)
        for op in [*ops, *([watched] if watched else [])]:
            self.runner.check_grant(grant_id, principal, effect=op.effect, lane=op.on or op.lane)

    def _inherit_ask_first(self, flow_id: str, defn: WorkflowDefinition, actor: Actor) -> None:
        """A workflow an agent saves over a person's "ask first" starts asking too, with the reason."""
        switches = self._switches()
        if switches is None:
            return
        watched = self.trigger_operation(defn)
        for op in [*(op for _, op in operations_in(defn, self.operation)), *([watched] if watched else [])]:
            for scope, ident in (("flow", op.flow_id), ("lane", op.on or op.lane)):
                s = switches.get(scope, ident)
                if s is not None and s.ask_first and s.by_person:
                    why = f"a person set {ident} to ask first, so a workflow an agent saves over it starts the same way"
                    switches.set("flow", flow_id, True, by=actor.by, person=False, why=why)
                    return

    def save(self, data: Any, actor: Actor, *, flow_id: str | None = None, ask_first: bool | None = None,
             replaces: Callable[[str | None, dict[str, Any]], bool] | None = None,
             live: bool = True) -> dict[str, Any]:
        """Save a new version. A person's save goes live on a workflow that is on, when `live` (the
        person may turn it on: the owner, or a member with standing who may decide); otherwise it is
        saved and waits for someone who may. An agent's waits for a person. A rule broken refuses the
        save whole; unfinished is fine until it is turned on.
        `replaces(workspace, saved_by)`: may this caller save over the workflow already at that id (its
        workspace, and who saved its head)? Refused as a workflow that is not there when it may not."""
        defn = self._parse(data)
        wid = flow_id or defn.id or f"{defn.workspace or 'draft'}.routine.{slug(defn.name)}"
        if defn.id and flow_id and defn.id != flow_id:
            raise InvalidRequest(f"this definition is {defn.id}, not {flow_id}")
        defn = defn.model_copy(update={"id": wid})
        self._parse(defn.model_dump(mode="json", by_alias=True))
        if self.flows.store.get_flow(wid) is not None:
            raise InvalidRequest(f"{wid} is a saved flow; a workflow takes another id")
        problems, _ = check(defn, self)
        if problems:
            raise WorkflowInvalid(problems[0].words, problems)
        if ask_first is False and not actor.person:
            raise PolicyDenied(f"only a person turns Ask me first off for {wid}")
        state = self.store.state(wid)
        if state is not None and replaces is not None:
            head = self.store.version(wid, state.head)
            if head is None or not replaces(head.definition.workspace, dict(head.saved_by or {})):
                raise PolicyDenied(f"{wid} is taken by a workflow outside your grant; save under another id")
        self.check_steps_in_grant(defn, actor.grant_id, actor.principal)
        version = defn.content_hash()
        known = self.store.version(wid, version)
        if known is None:
            revision = (state.revision + 1) if state is not None else 1
            known = WorkflowVersion(definition=defn, version=version, revision=revision, created_at=self.clock.now(),
                                    saved_by=actor.saved_by())
            self.store.put_version(known)
        elif actor.person and not known.by_person:  # a person vouches for what someone else saved first
            self.store.set_saved_by(wid, version, actor.saved_by())
            known = known.model_copy(update={"saved_by": actor.saved_by()})
        if state is None:
            state = WorkflowState(id=wid, head=version, revision=known.revision, updated_at=self.clock.now())
        state.head, state.revision, state.updated_at = version, max(state.revision, known.revision), self.clock.now()
        secret = None
        if actor.person and state.enabled and live:
            state.live = version
            secret = self._wire(state, known)
        self.store.put_state(state)
        if not actor.person:
            self._inherit_ask_first(wid, defn, actor)
        switches = self._switches()
        if ask_first is True and switches is not None:
            switches.set("flow", wid, True, by=actor.by, person=actor.person)
        return {**self.public(wid), **({"secret": secret} if secret else {})}

    def delete(self, flow_id: str, actor: Actor) -> bool:
        if not actor.person:
            raise PolicyDenied("only a person deletes a workflow")
        state = self._state(flow_id)
        self._unwire(state)
        self.store.delete(flow_id)
        return True

    def discard(self, flow_id: str, actor: Actor, *, own: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        """An agent takes back a draft it saved, as if it had never saved it: only one that was never on,
        never proposed and never ran, every version of it saved by the caller's own grant family (`own`).
        Anything else a person already saw or something already did, so only a person deletes it."""
        state = self._state(flow_id)
        person = f"only a person deletes it now: openajar routines workflows remove {flow_id}"
        if state.enabled or state.live or state.proposal or state.route_id or state.schedule_id:
            raise PolicyDenied(f"{flow_id} was turned on or proposed to a person, so {person}")
        if self.runner.store.list_runs(flow_id=flow_id, limit=1):
            raise PolicyDenied(f"{flow_id} has runs, so {person}")
        versions = self.store.versions(flow_id)
        if not versions or any(v.by_person or not own(v.saved_by) for v in versions):
            raise PolicyDenied(f"{flow_id} was not saved by you alone, so {person}")
        self._unwire(state)
        self.store.delete(flow_id)
        return {"id": flow_id, "discarded": True, "by": actor.by}

    # ---- turning it on ----
    def enable(self, flow_id: str, actor: Actor, *, version: str | None = None) -> dict[str, Any]:
        if not actor.person:
            raise PolicyDenied("only a person turns a workflow on; an agent asks with workflow_propose")
        state = self._state(flow_id)
        wv = self.get_version(flow_id, version)
        problems, missing = check(wv.definition, self)
        if problems or missing:
            raise WorkflowInvalid("It is not finished yet.", [*problems, *missing])
        state.enabled, state.live, state.updated_at = True, wv.version, self.clock.now()
        secret = self._wire(state, wv)
        self.store.put_state(state)
        return {**self.public(flow_id), **({"secret": secret} if secret else {})}

    def disable(self, flow_id: str, actor: Actor) -> dict[str, Any]:
        """Anyone may stop one: stopping is the safe direction. A watch forgets what it read, so turning
        it on again starts from what the site says then, not from all that changed while it was off."""
        state = self._state(flow_id)
        state.enabled, state.updated_at = False, self.clock.now()
        self._switch(state, False)
        self.store.put_state(state)
        self.site_changes.forget(state)
        return self.public(flow_id)

    def propose(self, flow_id: str, actor: Actor) -> dict[str, Any]:
        """An agent asks a person to turn it on: the whole definition goes on the card."""
        approvals = self._svc("approvals")
        if approvals is None:
            raise ServiceUnavailable("asking a person needs the policy service on this node")
        wv = self.get_version(flow_id)
        _, missing = check(wv.definition, self)
        if missing:
            raise WorkflowInvalid("It is not finished yet.", missing)
        p = actor.principal
        requester = Principal(**{k: v for k, v in p.items() if k in Principal.model_fields})
        question = (f"An agent asks you to turn on the workflow {wv.definition.name} ({flow_id}, {wv.version}). "
                    f"Definition: {json.dumps(wv.definition.canonical())[:1500]}")
        self.runner.check_pending(approvals, actor.grant_id)
        ap = approvals.create_question(question=question, options=[TURN_ON, "Leave it off"], requester=requester,
                                       grant_id=actor.grant_id,
                                       grant_path=self.runner._grant_path(actor.grant_id) if actor.grant_id else None,
                                       workspace=wv.definition.workspace)
        state = self._state(flow_id)
        state.proposal = {"approval_id": ap.id, "version": wv.version}
        self.store.put_state(state)
        return {"approval_id": ap.id, "status": "pending"}

    def check_proposals(self) -> int:
        """A person answered an agent's card: yes turns on exactly the version on the card. A webhook
        is the exception, because its secret is shown once, to the person turning it on."""
        approvals = self._svc("approvals")
        if approvals is None:
            return 0
        done = 0
        for state in self.store.states():
            if not state.proposal:
                continue
            try:
                ap = approvals.get(state.proposal["approval_id"])
            except NotFound:
                ap = None
            if ap is not None and ap.state == "pending":
                continue
            version = state.proposal["version"]
            state.proposal = None
            self.store.put_state(state)
            done += 1
            if ap is None or (ap.answer or "") != TURN_ON:
                continue
            wv = self.store.version(state.id, version)
            if wv is None:
                continue
            if not answered_in_person(ap, relayed=True):
                self.runner._notify(wv.definition.name, "Only a person turns a workflow on. It stays off.",
                                    data={"workflow": state.id})
                continue
            if wv.definition.trigger.kind == "webhook":
                self.runner._notify(wv.definition.name, "Turn it on in the panel: its webhook secret is shown once, to you.",
                                    level="info", data={"workflow": state.id})
                continue
            try:
                self.enable(state.id, Actor.owner(by=str(ap.decided_by or "a person")), version=version)
            except (WorkflowInvalid, PolicyDenied, NotFound) as e:
                self.runner._notify(wv.definition.name, f"It could not be turned on: {e.message}", data={"workflow": state.id})
        return done

    def _trigger_kwargs(self, wv: WorkflowVersion) -> dict[str, Any]:
        t = wv.definition.trigger
        if t.kind == "schedule":
            return {"cron": t.cron, "timezone": t.timezone}
        if t.kind == "webhook":
            return {"webhook": True}
        if t.kind == "site_change":  # no version: a watch reads what is active, as a site API only runs that
            flow_id, _, on = (t.operation or "").partition("@")
            return {"flow": flow_id, "on": on, "params": t.params or {}, "every": {"every_s": int(t.every_minutes or 0) * 60}}
        watch = t.watch
        locator = watch.locator.model_dump(mode="json", by_alias=True, exclude_none=True) if watch and watch.locator else WHOLE_PAGE
        return {"on": t.lane, "watch": {"url": t.url, "locator": locator, "read": watch.read if watch else "text",
                                        "every_s": watch.every_s if watch else 300}}

    def _wire(self, state: WorkflowState, wv: WorkflowVersion) -> str | None:
        """The route and schedule that start the live version. Returns a webhook's secret, once."""
        t = wv.definition.trigger
        scheduler, events = self.runner.scheduler, self._svc("event_queue")
        source = SOURCES.get(t.kind)
        trigger = json.dumps(wv.definition.canonical()["trigger"], sort_keys=True)
        if state.route_id and state.wired == trigger:
            self._switch(state, True)
            return None
        keep_hook = t.kind == "webhook" and state.schedule_id is not None and state.route_id is not None
        if keep_hook:  # the sender keeps its secret across an edit of the words
            state.wired = trigger
            self._switch(state, True)
            return None
        self._unwire(state)
        if t.kind != "site_change":
            self.store.drop_watch(state.id)
        if source is None:
            state.wired = trigger
            return None
        if events is None or scheduler is None:
            raise ServiceUnavailable("triggers need the events service on this node")
        ws = wv.definition.workspace or ""
        grant = wv.saved_by.get("grant_id") or OWNER_GRANT
        match: dict[str, Any] = {"source": source, "workspace": ws}
        if t.kind in ("email", "site_change"):
            match["where"] = {"workflow": state.id}
        route = events.add_route({"name": wv.definition.name[:80], "match": match,
                                  "target": {"kind": "workflow", "workflow": state.id},
                                  "controls": {"concurrency": 20, "max_attempts": 5}}, grant_id=grant)
        state.route_id, state.wired = route.id, trigger
        if t.kind == "email":
            return None
        principal = {k: v for k, v in wv.saved_by.items() if k in ("kind", "id", "owner")}
        if t.kind == "site_change":  # the schedule reads; what it found reaches the route as an event of its own
            state.schedule_id = scheduler.add(principal=principal, grant_id=grant, **self._trigger_kwargs(wv))[0].id
            return None
        s, extras = scheduler.add(route=route.id, principal=principal, grant_id=grant, **self._trigger_kwargs(wv))
        state.schedule_id = s.id
        route.match.schedule_id = s.id
        events.store.put_route(route)
        secret = extras.get("secret")
        if secret:
            state.secret_hint = f"...{secret[-4:]}"
        return secret

    def _switch(self, state: WorkflowState, on: bool) -> None:
        scheduler, events = self.runner.scheduler, self._svc("event_queue")
        if state.schedule_id and scheduler is not None:
            scheduler.set_enabled(state.schedule_id, on)
        if state.route_id and events is not None:
            events.set_enabled(state.route_id, on)

    def _unwire(self, state: WorkflowState) -> None:
        scheduler, events = self.runner.scheduler, self._svc("event_queue")
        if state.schedule_id and scheduler is not None:
            scheduler.remove(state.schedule_id)
        if state.route_id and events is not None:
            events.remove_route(state.route_id)
        state.schedule_id = state.route_id = state.wired = None

    # ---- starting runs ----
    @staticmethod
    def trigger_data(event: Any) -> dict[str, Any]:
        """A run's trigger data from an event. Only what the node itself observed stays trusted."""
        facts, content = dict(event.facts), event.content
        text = content.text if content is not None else ""
        values: dict[str, Any]
        outside: list[str] = []
        host = urlsplit(str(facts.get("url") or "")).hostname
        if event.kind == sitechange.EVENT_KIND:
            return {"trigger": sitechange.trigger_values(event), "untrusted": list(sitechange.LISTS),
                    "rows": list(sitechange.LISTS), "by": "trigger", "cause": sitechange.CAUSE,
                    "origin": {"kind": ORIGIN_KINDS["watch"], "host": host}}
        if event.source == "cron":
            values = {"when": facts.get("slot")}
        elif event.source == "watch":
            values = {"url": facts.get("url"), "count": facts.get("count"), "what": text}
            outside = ["what"]
        elif event.source == "mail":
            values = {**(content.fields or {}), "body": text} if content is not None else {"body": ""}
            outside = sorted(values)
            values["in_reply_to_run"] = facts.get("in_reply_to_run")  # the node matched it; nothing the sender wrote says so
        else:
            values = {"body": text}
            try:
                parsed = json.loads(text) if text else None
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                values.update({k: v for k, v in list(parsed.items())[:MAX_WEBHOOK_KEYS] if FIELD_KEY_RE.match(k)})
            outside = sorted(values)
        data = {"trigger": values, "untrusted": outside, "by": "trigger", "cause": CAUSES.get(event.source, "It started"),
                "origin": {"kind": ORIGIN_KINDS.get(event.source, "webhook"), "host": host}}
        if event.source == "cron":  # a schedule's fire: nobody asked, so a denial does not refuse it unasked
            data["scheduled"] = True
        return data

    def start_from_event(self, flow_id: str, route: Any, delivery: Any, event: Any) -> Any:
        state = self._state(flow_id)
        if not state.enabled or state.live is None:
            raise InvalidRequest(f"workflow {flow_id} is off")
        wv = self.get_version(flow_id, state.live)
        principal = {k: v for k, v in wv.saved_by.items() if k in ("kind", "id", "owner")}
        trigger = {"kind": "event", "route": route.id, "delivery": delivery.id, "events": [event.id],
                   "depth": event.cause.depth}
        if event.source == "cron":  # the node's own schedule fired: started_by_schedule reads this, never params
            trigger["scheduled"] = True
        run, _ = self.runner.submit(kind=RunKind.WORKFLOW, flow_id=flow_id, version=wv.version, params=self.trigger_data(event),
                                    idempotency_key=f"event:{route.id}:{event.id}", principal=principal,
                                    source=f"route:{route.id}", grant_id=wv.saved_by.get("grant_id") or OWNER_GRANT,
                                    trigger=trigger)
        return run

    def check_runnable(self, flow_id: str) -> None:
        """Refuse a run that could not start (unfinished), before anything else is made for it. The rules
        that only gate turning a version on (TURN_ON_ONLY) do not stop a version that is already running."""
        wv = self.get_version(flow_id)
        problems, missing = check(wv.definition, self)
        missing = [issue for issue in missing if issue.code not in TURN_ON_ONLY]
        if problems or missing:
            raise WorkflowInvalid("It is not finished yet.", [*problems, *missing])

    def run(self, flow_id: str, actor: Actor, *, values: dict[str, Any] | None = None, key: str | None = None,
            approval_id: str | None = None) -> dict[str, Any]:
        """Run it now. A person's run uses the grant of whoever saved it, so it never widens; an
        agent's uses the agent's own grant. Each step is authorized all the same. `approval_id`: the
        yes the run waits for before it starts (a member who may not decide here started it)."""
        self.check_runnable(flow_id)
        wv = self.get_version(flow_id)
        if values is not None and not isinstance(values, dict):
            raise InvalidRequest("values is a JSON object of name: value")
        values = dict(values or {})
        kind = wv.definition.trigger.kind  # run by hand, nothing changed
        blank = sitechange.EMPTY if kind == "site_change" else {"in_reply_to_run": None} if kind == "email" else {}
        grant = (wv.saved_by.get("grant_id") or OWNER_GRANT) if actor.person else actor.grant_id
        principal = {k: v for k, v in wv.saved_by.items() if k in ("kind", "id", "owner")} if actor.person else actor.principal
        if not actor.person:
            self.check_steps_in_grant(wv.definition, grant, principal)
        params = {"trigger": {**blank, **values, "when": self.clock.now().isoformat()},
                  "untrusted": [] if actor.person else sorted(values), "by": "person" if actor.person else "agent",
                  "cause": "Started by a person" if actor.person else "Started by an agent",
                  "origin": {"kind": "agent_input", "host": None}}
        run, _ = self.runner.submit(kind=RunKind.WORKFLOW, flow_id=flow_id, version=wv.version, params=params,
                                    idempotency_key=key, principal=principal, source="workflow:run", grant_id=grant,
                                    approval_id=approval_id)
        return self.run_view(run)

    def retry(self, flow_id: str, run_id: str, actor: Actor, *, force: bool = False) -> dict[str, Any]:
        run = self.runner.get(run_id)
        if run.kind != RunKind.WORKFLOW or run.flow_id != flow_id:
            raise NotFound(f"{run_id} is not a run of {flow_id}")
        if run.status not in (RunStatus.FAILED, RunStatus.PARTIAL):
            raise InvalidRequest(f"this run is {run.status}: only a failed or partial run can be retried")
        if not actor.person and run.grant_id != actor.grant_id:
            raise PolicyDenied("an agent retries only a run under its own grant")
        store = self.runner.store
        states = store.step_states(run.id)
        records = store.steps(run.id)
        stuck = [r for r in records if r.status == StepStatus.FAILED or (r.status == StepStatus.STARTED and r.type not in ("loop", "branch"))]
        for r in stuck:
            state = states.get(r.path) or {}
            if (r.result or {}).get("acted") and "people" not in state and not (force and actor.person):
                raise InvalidRequest("retry_may_repeat: this step may already have acted, so a retry could do it twice. "
                                     "A person can retry it anyway with force.")
        now = self.clock.now()
        for r in stuck:
            if r.status == StepStatus.STARTED:
                store.step_end(run.id, r.path, StepStatus.FAILED, now, error=run.error)
            state = states.get(r.path)
            if state is not None:  # authorized again, like any new attempt
                for k in ("authorized", "auto", "approval_id", "doubted", "rendered", "sent_by"):
                    state.pop(k, None)
                store.put_step_state(run.id, r.path, state)
        prev = run.status
        run.status, run.finished_at, run.error, run.decision, run.attempts = RunStatus.QUEUED, None, None, None, 0
        run.not_before, run.authorized = None, False
        # a retry is someone asking again, so the denial cooldown holds for it (not for a schedule's fire)
        run.params = {k: v for k, v in {**run.params, "by": "retry"}.items() if k != "scheduled"}
        if isinstance(run.trigger, dict) and "scheduled" in run.trigger:
            run.trigger = {k: v for k, v in run.trigger.items() if k != "scheduled"}
        if not store.save_run(run, expect={prev}):
            raise InvalidRequest("this run just changed; look at it again")
        self.runner._emit(run)
        self.runner._wake.set()
        return self.run_view(run)

    def hook_backlog(self, schedule: Any) -> int:
        """What a webhook's sender has waiting and not started yet. A run already under way, even one
        in a long wait, is not a backlog."""
        events = self._svc("event_queue")
        if events is None or not schedule.route_id:
            return 0
        route = events.store.get_route(schedule.route_id)
        waiting = events.store.count_deliveries(route_id=schedule.route_id)  # the open ones (pending, waiting)
        flow_id = getattr(route.target, "workflow", None) if route is not None else None
        if flow_id:
            queued = self.runner.store.list_runs(status=RunStatus.QUEUED, flow_id=flow_id, limit=MAX_BACKLOG_SCAN)
            waiting += sum(1 for r in queued if r.started_at is None)
        return waiting

    def on_run_finished(self, run: Any) -> None:
        """A child ended: its workflow looks again now. A cancelled workflow takes its children with it."""
        store = self.runner.store
        if run.kind == RunKind.WORKFLOW:
            if run.status == RunStatus.CANCELLED:
                for child in store.list_runs(status=ACTIVE, limit=500):
                    if child.parent == run.id:
                        self.runner.store.set_cancel_requested(child.id)
                        if child.status != RunStatus.RUNNING:
                            child.status, child.finished_at = RunStatus.CANCELLED, self.clock.now()
                            child.error = {"code": "cancelled", "message": "its workflow was cancelled"}
                            store.save_run(child)
            return
        parent = store.get_run(run.parent) if run.parent else None
        if parent is not None and parent.kind == RunKind.WORKFLOW and parent.status == RunStatus.QUEUED:
            parent.not_before = None
            store.save_run(parent, expect={RunStatus.QUEUED})
            self.runner._wake.set()

    # ---- mail: the node dials out ----
    async def tick(self) -> None:
        """Every MAIL_POLL_S, each mailbox once: new mail starts the workflows it matches, and an
        unsubscribe is read from the same poll, so a mailbox needs nothing to reach it. A watch's
        finished check is looked at on every tick. The tick starts a read and never waits for it,
        so a slow server holds up only its own mailbox."""
        self.check_proposals()
        self.leads.check_cards()
        self.site_changes.tick()
        at = self.clock.monotonic()
        if self._polled_at is not None and at - self._polled_at < MAIL_POLL_S:
            return
        self._polled_at = at
        for box in self.mail.list():
            if box.id not in self._polls:  # one read of a mailbox at a time, however long it takes
                self._polls[box.id] = asyncio.create_task(self._poll_alone(box), name=f"mail:{box.id}")

    async def _poll_alone(self, box: Any) -> None:
        try:
            await self.mail.poll_due(box, self.poll_mailbox, approvals=self._svc("approvals"))
        finally:
            self._polls.pop(box.id, None)

    async def poll_mailbox(self, box: Any) -> int:
        key = f"mail_uid:{box.id}"
        seen = self.runner.store.meta(key, "")
        got, highest = await self.mail.fetch(box, int(seen) if seen else None)
        fired = 0
        events = self._svc("event_queue")
        for message in got:
            if self.sends.unsubscribe_from_subject(message.subject):
                continue
            answers = self.replies.note(box, message)
            for state in self.store.states():
                if not state.enabled or not state.live or not state.route_id:
                    continue
                wv = self.store.version(state.id, state.live)
                t = wv.definition.trigger if wv else None
                if t is None or t.kind != "email" or t.lane != box.id or events is None:
                    continue
                if t.from_ and t.from_.lower() not in message.sender.lower():
                    continue
                if t.subject and t.subject.lower() not in message.subject.lower():
                    continue
                if t.unseen_only and message.seen:
                    continue
                _, created = events.emit({
                    "source": "mail", "kind": "mail.received", "workspace": wv.definition.workspace, "lane": box.id,
                    "dedupe_key": f"mail:{state.id}:{box.id}:{message.uid}",
                    "facts": {"workflow": state.id, "mailbox": box.id, "uid": message.uid,
                              **({"in_reply_to_run": answers} if answers else {})},
                    "content": {"text": message.body, "origin": f"mail to {box.id}",
                                "fields": {"from": message.sender, "subject": message.subject, "id": str(message.uid)}}},
                    route_id=state.route_id)
                fired += int(created)
        self.runner.store._exec("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(highest)))
        return fired

    # ---- what people read ----

    # ---- the catalog ----
    def catalog(self, *, workspace: str | None = None, allowed: Any = None, person: bool = False,
                covers: Any = None) -> dict[str, Any]:
        """`allowed(lane)` filters operations; `covers(workspace)` filters lists (a list is the workspace's)."""
        operations: list[dict[str, Any]] = []
        seen = allowed or (lambda lane: True)
        seen_ws = covers or (lambda ws: seen(f"{ws}/0"))

        def add(op: Operation | None) -> None:
            if op is None or not seen(op.on or op.lane) or (workspace and op.workspace != workspace):
                return
            params = {name: {**(schema if isinstance(schema, dict) else {}), **({"required": True} if name in op.required else {}),
                             **({"label": param_label(op, name)} if isinstance(schema, dict) and schema.get("title") else {})}
                      for name, schema in op.params.items()}
            operations.append({"id": op.id, "name": op.name, "lane": op.on or op.lane, "workspace": op.workspace,
                               "effect": op.effect.value, "params": params, "produces": [f.public() for f in op.produces],
                               "sends": op.sends is not None, "not_to_people": op.not_to_people, "tags": list(op.tags)})

        for box in self.mail.list():
            add(self.operation(f"{MAIL_OPERATION}@{box.id}"))
        for fv in self.flows.list():
            if is_connector_id(fv.id):
                surfaces = getattr(self.runner, "surfaces", None)
                for s in (surfaces.list() if surfaces is not None else []):
                    add(self.operation(f"{fv.id}@{s.id}"))
            elif not fv.definition.lane_is_pattern:
                add(self.operation(fv.id))
        lists = [x for x in self.sends.lists(workspace) if seen_ws(x["workspace"])]
        return {"operations": operations, "lists": lists if person else [for_an_agent(x) for x in lists], "templates": self.templates(),
                "trigger_fields": {k: [f.public() for f in v] for k, v in TRIGGER_FIELDS.items()}}

    @staticmethod
    def templates() -> list[dict[str, Any]]:
        return template_data.TEMPLATES

    def from_template(self, template_id: str, workspace: str | None) -> dict[str, Any]:
        """A definition to save: each `tag:` takes what the workspace has, or waits for a choice."""
        found = next((t for t in template_data.TEMPLATES if t["id"] == template_id), None)
        if found is None:
            raise NotFound(f"there is no template {template_id}")
        ops = self.catalog(workspace=workspace)["operations"]
        made = template_data.fill(found, workspace, lambda tag: next((o["id"] for o in ops if tag in o["tags"]), None))
        boxes = [b.id for b in self.mail.list() if b.id.split("/", 1)[0] == workspace]
        if made["trigger"]["kind"] == "email" and len(boxes) == 1:
            made["trigger"]["lane"] = boxes[0]
        return made


def build(runner: Any, runtime: Any = None, **options: Any) -> WorkflowService:
    service = WorkflowService(runner, runtime=runtime, **options)
    runner.workflows = service
    if runtime is not None:
        runtime.register(SERVICE_NAME, service)
    return service
