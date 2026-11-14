"""What a definition may do, checked from where its data comes from (docs/WORKFLOWS.md).

`check` gives two lists. `problems` break a rule and refuse the save, finished or not: untrusted
data in a place that decides, a reference that looks forward, a send with no owner's list.
`missing` is what a person has not finished yet, and only stops it being turned on.

Run time checks the same rules again on the values themselves (`engine.py`): an operation can label
a field untrusted when it runs, and a manual run's values depend on who gave them.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from croniter import croniter

from agentos.core.models import Effect
from agentos.runner.models import SendsSpec
from agentos.runner.util import TEMPLATE_RE
from agentos.runner.workflows.models import (
    MAX_BRANCH_DEPTH,
    MAX_LOOP_DEPTH,
    MAX_STEPS,
    MAX_WAIT_DAYS,
    MAX_WATCH_MINUTES,
    MIN_WATCH_MINUTES,
    REF_RE,
    TAG_PREFIX,
    TRIGGER,
    UNIT_SECONDS,
    WHOLE_REF_RE,
    ActionStep,
    AskStep,
    BranchStep,
    LoopStep,
    NotifyStep,
    StopIfRepliedStep,
    Trigger,
    WaitStep,
    WorkflowDefinition,
    walk,
)


@dataclass(frozen=True)
class Field:
    key: str
    label: str = ""
    type: str = "text"
    untrusted: bool | None = False  # None: known only at run time (a manual run's values)
    rows: bool = False  # a list the node built of rows from outside: what is in it stays untrusted, and a loop may go through it

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "label": self.label or self.key.replace("_", " ").capitalize(),
                               "type": self.type}
        out = {**out, "rows": True} if self.rows else out
        return {**out, "untrusted": True} if self.untrusted else out


@dataclass(frozen=True)
class Issue:
    target: str
    code: str
    words: str

    def public(self) -> dict[str, str]:
        return {"target": self.target, "code": self.code, "words": self.words}


@dataclass
class Operation:
    """What a step's `operation` resolves to. `id` is what the step holds."""
    id: str
    flow_id: str
    on: str | None
    name: str
    lane: str
    effect: Effect
    params: dict[str, Any] = field(default_factory=dict)  # name -> the param's JSON schema
    required: frozenset[str] = frozenset()  # the params a step must fill in before it can run
    produces: list[Field] = field(default_factory=list)
    sends: SendsSpec | None = None
    targets: frozenset[str] = frozenset()  # params that name a target: trusted data only
    tags: tuple[str, ...] = ()
    builtin: bool = False
    version: str | None = None  # what the rest of this was read from: the version a step then runs
    not_to_people: bool = False  # a person said so; an agent's mark never reaches here
    site_api: bool = False  # an endpoint of a site API the owner made
    output: dict[str, Any] = field(default_factory=dict)  # an endpoint's output schema
    http: dict[str, Any] = field(default_factory=dict)    # its cache_s, expect and field labels
    pace: dict[str, Any] = field(default_factory=dict)

    @property
    def acts(self) -> bool:
        return self.effect not in (Effect.READ, Effect.DRAFT)

    @property
    def workspace(self) -> str:
        return self.lane.split("/", 1)[0]


class Resolver(Protocol):
    def operation(self, text: str) -> Operation | None: ...
    def list_workspace(self, list_id: str) -> str | None: ...
    def mailbox(self, mailbox_id: str) -> bool: ...
    def site_problem(self, op: Operation, params: dict[str, Any]) -> str | None: ...


# What each trigger hands the steps after it. A webhook's and a manual run's keys are the sender's.
TRIGGER_FIELDS: dict[str, list[Field]] = {
    "schedule": [Field("when", "When it started", "date")],
    "webhook": [Field("body", "What arrived", "text", True)],
    "email": [Field("from", "Who wrote", "text", True), Field("subject", "Subject", "text", True),
              Field("body", "The email", "text", True), Field("id", "Which message", "text", True),
              Field("in_reply_to_run", "The run it answers")],  # the node's own finding: `replies.py`
    "page_change": [Field("url", "The page"), Field("count", "How many", "number"),
                    Field("what", "What changed", "text", True)],
    "site_change": [Field("added", "New rows", "list", True, rows=True),
                    Field("removed", "Rows that went", "list", True, rows=True),
                    Field("changed", "Rows that changed", "list", True, rows=True),
                    Field("count", "How many rows", "number"), Field("count_added", "How many are new", "number"),
                    Field("count_removed", "How many went", "number"), Field("count_changed", "How many changed", "number"),
                    Field("truncated", "More changed than it carries", "yes_no")],
    "manual": [],
}
OPEN_TRIGGERS = {"webhook": True, "manual": None}  # any key, and whether it is untrusted


def row_schema(op: Operation | None, name: str | None) -> dict[str, Any] | None:
    """The fields of one row of the output `name`, as the endpoint declares them, or None."""
    schema = ((op.output.get("properties") or {}).get(name) or {}) if op is not None and name else {}
    fields = (schema.get("items") or {}).get("properties") if schema.get("type") == "array" else None
    return fields if isinstance(fields, dict) else None


def private_fields(op: Operation | None, name: str | None) -> set[str]:
    prefix = f"{name}[]."
    return {p[len(prefix):] for p in ((op.http if op is not None else {}).get("private_fields") or []) if p.startswith(prefix)}


def row_fields(fields: dict[str, Any]) -> list[Field]:
    """What a loop over a site_change trigger's rows reads, keyed `<list>_<field>`: all of it is the site's."""
    out = [Field(f"{name}_row_key", "Which row", "text", True) for name in ("added", "removed", "changed")]
    out.append(Field("changed_what_changed", "Which fields changed", "list", True))
    for f in fields:
        out += [Field(f"added_{f}", f, "text", True), Field(f"removed_{f}", f, "text", True),
                Field(f"changed_{f}", f, "text", True), Field(f"changed_was_{f}", f"{f} before", "text", True)]
    return out


class Scope:
    """What a step can read: the trigger, earlier steps on its path, and the loops around it."""

    def __init__(self, trigger: Trigger, rows: list[Field] | None = None):
        self.fields: dict[str, dict[str, Field]] = {TRIGGER: {f.key: f for f in [*TRIGGER_FIELDS[trigger.kind], *(rows or [])]}}
        self.open = {TRIGGER: OPEN_TRIGGERS[trigger.kind]} if trigger.kind in OPEN_TRIGGERS else {}
        if trigger.kind == "site_change" and rows is None:  # no endpoint chosen yet: what a row holds is not known
            self.open[TRIGGER] = True

    def child(self) -> Scope:
        out = Scope.__new__(Scope)
        out.fields, out.open = {k: dict(v) for k, v in self.fields.items()}, dict(self.open)
        return out

    def add(self, source: str, fields: list[Field]) -> None:
        self.fields[source] = {f.key: f for f in fields}

    def lookup(self, source: str, key: str) -> Field | None:
        known = self.fields.get(source)
        if known is None:
            return None
        if key in known:
            return known[key]
        return Field(key, key, "text", self.open[source]) if source in self.open else None


def trigger_scope(defn: WorkflowDefinition, resolver: Resolver) -> Scope:
    t = defn.trigger
    if t.kind != "site_change":
        return Scope(t)
    fields = row_schema(resolver.operation(t.operation) if t.operation else None, t.list)
    return Scope(t, None if fields is None else row_fields(fields))


def item_fields(source: dict[str, Field], list_key: str) -> list[Field]:
    """One item of a list: the fields keyed `<list>_<field>`, plus `value` for a list of plain values."""
    prefix = f"{list_key}_"
    parent = source[list_key]
    named = [Field(f.key[len(prefix):], f.label, f.type, f.untrusted) for f in source.values() if f.key.startswith(prefix)]
    return [*named, Field("value", "This one", "text", parent.untrusted)]


def blank(value: Any) -> bool:
    """An input nobody filled in: missing, empty or only spaces."""
    return value is None or value == [] or (isinstance(value, str) and not value.strip())


def param_label(op: Operation, name: str) -> str:
    """A param in plain words: its title when the schema has one, else its name read as words."""
    schema = op.params.get(name)
    title = schema.get("title") if isinstance(schema, dict) else None
    return str(title) if title else name.replace("_", " ")


def words_of(items: list[str]) -> str:
    """`a`, `a and b`, `a, b and c`."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


class _Checker:
    def __init__(self, defn: WorkflowDefinition, resolver: Resolver):
        self.defn, self.resolver = defn, resolver
        self.problems: list[Issue] = []
        self.missing: list[Issue] = []
        self.counted: list[tuple[Any, Operation]] = []  # commit steps the daily limit covers, as far as a save can tell
        self.mail_steps: set[str] = set()  # steps a reply can answer: mail.send, or a step still to be chosen
        self.reading: list[tuple[str, str, str]] = []  # (kind, step id, label) in reading order: a step that acts, or asks

    def problem(self, target: str, code: str, words: str) -> None:
        self.problems.append(Issue(target, code, words))

    def unfinished(self, target: str, words: str) -> None:
        self.missing.append(Issue(target, "unfinished", words))

    # ---- references ----
    def _literal(self, target: str, value: Any, what: str) -> None:
        if isinstance(value, str) and (REF_RE.search(value) or TEMPLATE_RE.search(value)):
            self.problem(target, "must_be_literal", f"{what} is written out, and cannot come from a value.")

    def _known(self, target: str, scope: Scope, source: str, key: str) -> Field | None:
        found = scope.lookup(source, key)
        if found is None:
            self.problem(target, "bad_reference",
                         f"{{{{{source}.{key}}}}} is not something this step can read: it can only look back.")
        return found

    def _fills(self, target: str, scope: Scope, text: str) -> bool:
        """Every reference in a text is known. True when any of them is untrusted."""
        hit = False
        for m in REF_RE.finditer(text):
            found = self._known(target, scope, m.group(1), m.group(2))
            hit = hit or bool(found and found.untrusted)
        return hit

    def _decides(self, target: str, scope: Scope, ref: str, what: str, *, rows: bool = False) -> Field | None:
        """`ref` picks what happens: one whole reference, and never to anything untrusted. `rows`: a
        loop may go through a list the node built, because that list is not what the site wrote: each
        row it holds is still untrusted."""
        m = WHOLE_REF_RE.match(ref)
        if m is None:
            self.problem(target, "must_be_reference", f"{what} is one value from an earlier step.")
            return None
        found = self._known(target, scope, m.group(1), m.group(2))
        if found is not None and found.untrusted and not (rows and found.rows):
            self.problem(target, "untrusted_decides",
                         f"{what} cannot use {{{{{m.group(1)}.{m.group(2)}}}}}: it came from outside, so it can "
                         "fill text but never decide what happens.")
        return found

    # ---- the trigger ----
    def trigger(self) -> None:
        t, target = self.defn.trigger, TRIGGER
        for value in (t.cron, t.lane, t.from_, t.subject, t.url, t.timezone, t.operation, t.list, t.key):
            self._literal(target, value, "How it starts")
        if not self.defn.workspace:
            self.unfinished(target, "Choose a business.")
        ws = self.defn.workspace
        if t.lane and ws and t.lane.split("/", 1)[0] != ws:
            self.problem(target, "outside_workspace", f"{t.lane} is not in {ws}.")
        if t.kind == "schedule":
            if not t.cron:
                self.unfinished(target, "Finish how it starts.")
            elif not TEMPLATE_RE.search(t.cron) and not REF_RE.search(t.cron) and not croniter.is_valid(t.cron):
                self.problem(target, "bad_trigger", f"{t.cron!r} is not a schedule.")
        elif t.kind == "email":
            if not t.lane:
                self.unfinished(target, "Finish how it starts.")
            elif not self.resolver.mailbox(t.lane):
                self.problem(target, "bad_trigger", f"There is no mailbox {t.lane} on this computer.")
        elif t.kind == "page_change":
            if not t.url or not t.lane:
                self.unfinished(target, "Finish how it starts.")
            elif not t.url.lower().startswith(("https://", "http://")):
                self.problem(target, "bad_trigger", "A page to watch needs an http or https address.")
        elif t.kind == "site_change":
            self.site_change(t)

    def site_change(self, t: Trigger) -> None:
        """What a watch reads, on what, how often. A person fixes these, and none of them waits for a
        run to find out: an endpoint that acts is never a watch, whatever else is unfinished."""
        target, ws = TRIGGER, self.defn.workspace
        self._params_literal(t.params)
        if not (t.operation and t.list and t.key and t.every_minutes is not None):
            self.unfinished(target, "Finish how it starts.")
        text = t.operation
        if not text or REF_RE.search(text) or TEMPLATE_RE.search(text):
            return
        op = self.resolver.operation(text)
        if op is None:
            self.problem(target, "unknown_operation", f"There is no {text} on this computer.")
            return
        if not op.site_api:
            self.problem(target, "not_a_site_api", f"{op.name} is not a Site API endpoint, so it cannot be watched.")
            return
        if op.effect != Effect.READ:
            self.problem(target, "not_a_read", f"{op.name} is a {op.effect.value} endpoint: only a read can be watched, "
                         "because checking must never act on the site.")
            return
        if ws and op.workspace != ws:
            self.problem(target, "outside_workspace", f"{text} is not in {ws}.")
        self._watched_rows(t, op)
        problem = self.resolver.site_problem(op, t.params or {})
        if problem:
            self.problem(target, "bad_trigger", problem)
        if t.every_minutes is not None:
            self._watch_pace(t, op)

    def _params_literal(self, value: Any) -> None:
        if isinstance(value, dict):
            for v in value.values():
                self._params_literal(v)
        elif isinstance(value, list):
            for v in value:
                self._params_literal(v)
        else:
            self._literal(TRIGGER, value, "How it starts")

    def _watched_rows(self, t: Trigger, op: Operation) -> None:
        """`list` is a list this endpoint gives, and `key` a plain field of its rows."""
        if not t.list:
            return
        fields = row_schema(op, t.list)
        if fields is None:
            self.problem(TRIGGER, "bad_trigger", f"{op.name} gives no list called {t.list}.")
            return
        if not t.key:
            return
        if t.key not in fields:
            self.problem(TRIGGER, "bad_trigger", f"A row of {t.list} has no field {t.key}.")
            return
        kinds = (fields[t.key] or {}).get("type") or "string"
        if not set([kinds] if isinstance(kinds, str) else kinds) <= {"string", "integer", "number", "null"}:
            self.problem(TRIGGER, "bad_trigger", f"{t.key} is not a plain value, so it cannot say which row is which.")
        if t.key in private_fields(op, t.list):
            self.problem(TRIGGER, "bad_trigger", f"{t.key} is a private field. A key is shown in what changed, so choose another.")

    def _watch_pace(self, t: Trigger, op: Operation) -> None:
        """How often: not faster than the floor, than the answer is cached for, or than half of the pace."""
        every = int(t.every_minutes or 0)
        floor = max(MIN_WATCH_MINUTES, math.ceil(int(op.http.get("cache_s") or 0) / 60))
        if every < floor:
            self.problem(TRIGGER, "too_often", f"Check at most every {floor} minutes: a site answer is kept for that long.")
        elif every > MAX_WATCH_MINUTES:
            self.problem(TRIGGER, "bad_trigger", "Check at least once a week.")
        for name, span, words in (("per_hour", 60, "an hour"), ("per_day", 1440, "a day")):
            limit = op.pace.get(name)
            if limit and span / max(every, 1) > limit / 2:
                self.problem(TRIGGER, "too_often", f"Checking every {every} minutes would use more than half of what "
                             f"{op.name} may run in {words} ({limit}). Check less often, or raise its pace.")
        gap = op.pace.get("min_gap_minutes")
        if gap and every < gap:
            self.problem(TRIGGER, "too_often", f"{op.name} runs at most once every {gap} minutes, so it cannot be checked "
                         f"every {every}.")

    # ---- steps ----
    def shape(self) -> None:
        seen: set[str] = set()
        count = 0
        for step in walk(self.defn.steps):
            count += 1
            if step.id in seen:
                self.problem(step.id, "duplicate_step", f"Two steps are called {step.id}.")
            seen.add(step.id)
        if count > MAX_STEPS:
            self.problem(TRIGGER, "too_many_steps", f"A workflow has at most {MAX_STEPS} steps.")
        if not self.defn.steps:
            self.unfinished(TRIGGER, "Add a first step.")

    def steps(self, steps: list[Any], scope: Scope, loops: int = 0, branches: int = 0) -> None:
        for step in steps:
            if isinstance(step, ActionStep):
                self.action(step, scope, loops)
            elif isinstance(step, BranchStep):
                self.branch(step, scope, loops, branches)
            elif isinstance(step, LoopStep):
                self.loop(step, scope, loops, branches)
            elif isinstance(step, WaitStep):
                if step.amount <= 0:
                    self.unfinished(step.id, f'Finish "{step.label}".')
                elif step.amount * UNIT_SECONDS[step.unit] > MAX_WAIT_DAYS * 86_400:
                    self.problem(step.id, "wait_too_long", f"A wait is at most {MAX_WAIT_DAYS} days.")
            elif isinstance(step, AskStep):
                if not step.question.strip():
                    self.unfinished(step.id, f'Finish "{step.label}".')
                self._fills(step.id, scope, step.question)
                self.reading.append(("ask", step.id, step.label))
                scope.add(step.id, [Field("answer", "Their answer", "yes_no")])
            elif isinstance(step, NotifyStep):
                if not step.message.strip():
                    self.unfinished(step.id, f'Finish "{step.label}".')
                self._fills(step.id, scope, step.message)
            elif isinstance(step, StopIfRepliedStep):
                self.stop_if_replied(step, scope)

    def stop_if_replied(self, step: StopIfRepliedStep, scope: Scope) -> None:
        """`after` is a step id, written out. Only the node's own mail has an id a reply can name."""
        if not step.after:
            self.unfinished(step.id, f'Finish "{step.label}".')
            return
        self._literal(step.id, step.after, "Which message it looks at")
        if step.after not in scope.fields or step.after == TRIGGER:
            self.problem(step.id, "bad_reference", f"{step.after} is not something this step can read: it can only look back.")
        elif step.after not in self.mail_steps:
            self.problem(step.id, "not_a_mail_send", f"{step.after} does not send mail from a mailbox, so no reply can be matched to it.")

    def action(self, step: ActionStep, scope: Scope, loops: int = 0) -> None:
        text = step.operation
        if not text:
            self.unfinished(step.id, f'Finish "{step.label}".')
            for value in step.inputs.values():
                if isinstance(value, str):
                    self._fills(step.id, scope, value)
            scope.add(step.id, [])  # what it gives is not known yet: checked again once it is chosen
            scope.open[step.id] = None
            self.mail_steps.add(step.id)
            return
        self._literal(step.id, text, "What a step does")
        if text.startswith(TAG_PREFIX):
            self.problem(step.id, "unknown_operation", f'Choose what "{step.label}" does.')
            return
        if REF_RE.search(text) or TEMPLATE_RE.search(text):
            return
        op = self.resolver.operation(text)
        if op is None:
            self.problem(step.id, "unknown_operation", f"There is no {text} on this computer.")
            return
        if self.defn.workspace and op.workspace not in (self.defn.workspace, "*"):
            self.problem(step.id, "outside_workspace", f"{text} is not in {self.defn.workspace}.")
        if op.builtin:
            self.mail_steps.add(step.id)
        inner = scope
        if op.sends is not None:  # each message knows who it is for
            inner = scope.child()
            inner.add("person", [Field("email", "Their address"), Field("name", "Their name")])
        self.inputs(step, op, inner)
        self.required(step, op)
        self.send(step, op, scope)
        if op.acts:
            self.reading.append(("act", step.id, step.label))
        if op.sends is None and op.acts and not op.not_to_people and (loops or self._fed_from_a_list(step, scope)):
            self.counted.append((step, op))
        scope.add(step.id, op.produces)

    @staticmethod
    def _fed_from_a_list(step: ActionStep, scope: Scope) -> bool:
        for value in step.inputs.values():
            if isinstance(value, list):
                return True
            for m in REF_RE.finditer(value) if isinstance(value, str) else ():
                found = scope.lookup(m.group(1), m.group(2))
                if found is not None and found.type == "list":
                    return True
        return False

    def inputs(self, step: ActionStep, op: Operation, scope: Scope) -> None:
        for name, value in step.inputs.items():
            if op.sends is not None and name == op.sends.to:
                self.problem(step.id, "to_is_in_send", "Who a message goes to is set by the list, not by a field.")
                continue
            if op.params and name not in op.params:
                self.problem(step.id, "unknown_input", f"{op.name} has no field {name}.")
            if not isinstance(value, str):
                continue
            if self._fills(step.id, scope, value) and name in op.targets:
                self.problem(step.id, "untrusted_decides",
                             f"{name} says where {op.name} acts, so it cannot use a value from outside.")

    def required(self, step: ActionStep, op: Operation) -> None:
        """A param the operation needs is filled in: a caption, an image. Until then the step is unfinished."""
        names = [name for name in op.params if name in op.required and blank(step.inputs.get(name))]
        if names:
            self.missing.append(Issue(step.id, "input_missing",
                                      f'Fill in {words_of([param_label(op, name) for name in names])} for "{step.label}".'))

    def posts_before_asking(self) -> None:
        """When the routine asks a person, a step that acts comes after the ask. One placed before it would run
        before anyone has said yes, so it is unfinished until it is moved."""
        asks = [item for item in self.reading if item[0] == "ask"]
        if not asks:
            return
        for kind, step_id, label in self.reading:
            if kind == "ask":
                return
            self.missing.append(Issue(step_id, "acts_before_asking",
                                      f'"{label}" would run before anyone is asked. Move it after "{asks[0][2]}".'))

    def send(self, step: ActionStep, op: Operation, scope: Scope) -> None:
        if op.sends is None:
            if step.send is not None:
                self.problem(step.id, "send_only_for_senders", f"{op.name} does not send to people.")
            return
        if step.send is None or not step.send.list:
            self.unfinished(step.id, f'Choose who "{step.label}" sends to.')
        else:
            self._literal(step.id, step.send.list, "The list")
            ws = self.resolver.list_workspace(step.send.list)
            if ws is None:
                self.problem(step.id, "unknown_list", "A message goes only to a list the owner supplied here.")
            elif self.defn.workspace and ws != self.defn.workspace:
                self.problem(step.id, "outside_workspace", f"{step.send.list} is not in {self.defn.workspace}.")
        if step.send is not None and step.send.to:
            self._decides(step.id, scope, step.send.to, "Who it goes to")

    def branch(self, step: BranchStep, scope: Scope, loops: int, branches: int) -> None:
        if branches + 1 > MAX_BRANCH_DEPTH:
            self.problem(step.id, "too_deep", f"Branches go at most {MAX_BRANCH_DEPTH} deep.")
        self._literal(step.id, step.when.right, "What it is compared with")
        if not step.when.left:
            self.unfinished(step.id, f'Finish "{step.label}".')
        else:
            self._decides(step.id, scope, step.when.left, "What it checks")
        self.steps(step.then, scope.child(), loops, branches + 1)
        self.steps(step.otherwise, scope.child(), loops, branches + 1)

    def loop(self, step: LoopStep, scope: Scope, loops: int, branches: int) -> None:
        if loops + 1 > MAX_LOOP_DEPTH:
            self.problem(step.id, "too_deep", f"Loops go at most {MAX_LOOP_DEPTH} deep.")
        inner = scope.child()
        if not step.over:
            self.unfinished(step.id, f'Finish "{step.label}".')
        else:
            found = self._decides(step.id, scope, step.over, "What it goes through", rows=True)
            m = WHOLE_REF_RE.match(step.over)
            if found is not None and m is not None:
                if found.type != "list" and found.untrusted is False:
                    self.problem(step.id, "not_a_list", f"{{{{{m.group(1)}.{m.group(2)}}}}} is not a list.")
                else:
                    inner.add(step.id, item_fields({**scope.fields.get(m.group(1), {}), found.key: found}, found.key))
                    if m.group(1) in scope.open:
                        inner.open[step.id] = scope.open[m.group(1)]
        inner.fields.setdefault(step.id, {})
        self.steps(step.body, inner, loops + 1, branches)


# Rules a version must meet to be turned on, added after routines were already running. A version that is
# live keeps running without them (check_runnable), so an upgrade never stops a routine someone relies on.
TURN_ON_ONLY = frozenset({"input_missing", "acts_before_asking"})


def check(defn: WorkflowDefinition, resolver: Resolver) -> tuple[list[Issue], list[Issue]]:
    """(problems, missing). Problems refuse the save; missing only stops it being turned on."""
    c = _Checker(defn, resolver)
    c.shape()
    c.trigger()
    c.steps(defn.steps, trigger_scope(defn, resolver))
    c.posts_before_asking()
    return c.problems, c.missing


def counted_steps(defn: WorkflowDefinition, resolver: Resolver) -> list[tuple[Any, Operation]]:
    """Commit steps that count against the daily limit though they declare nothing: in a loop, or fed
    from a list. Run time counts on the values too, since a webhook's lists are known only then."""
    c = _Checker(defn, resolver)
    c.steps(defn.steps, trigger_scope(defn, resolver))
    return c.counted


def operations_in(defn: WorkflowDefinition, operation: Callable[[str], Operation | None]) -> list[tuple[Any, Operation]]:
    """Each action step that resolves, with its operation."""
    out = []
    for step in walk(defn.steps):
        if isinstance(step, ActionStep) and step.operation:
            op = operation(step.operation)
            if op is not None:
                out.append((step, op))
    return out
