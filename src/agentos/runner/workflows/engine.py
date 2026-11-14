"""The walk: a workflow run, from step to step, durably.

`WorkflowExecutor.execute` walks the definition from the top every time it runs. A step with a
finished record is not done again: its output comes back from the journal, and a branch takes the
arm it took. So a restart, a wait, a person's answer and a retry are all the same thing: walk again.
When a step has to wait (a child run, a person, a day), the run gives up its slot and comes back.

An action step is a child run, submitted unauthorized, so the runner's gate authorizes it right
before it acts: nothing here approves anything, and a workflow is never authorized as a whole. A
send step is authorized once for what it will send, then goes through the ledger address by address.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from agentos.core.aio import maybe_await
from agentos.core.errors import AgentosError, ApprovalPending, InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import Effect, Principal, RunStatus
from agentos.runner.connectors.surfaces import ConnectorBroken, PaceLimited
from agentos.runner.execution import Journal, Outcome, RunControl, StopRun
from agentos.runner.records import SUCCEEDED, TERMINAL, Run, StepRecord, StepStatus
from agentos.runner.run_rules import started_by_schedule
from agentos.runner.util import clip
from agentos.runner.workflows.check import Operation
from agentos.runner.workflows.mail import SendInDoubt, SendRefused, SendStopped, build_message
from agentos.runner.workflows.models import (
    MAX_LOOP_ITEMS,
    MAX_ROW_ACTS,
    REF_RE,
    TRIGGER,
    WHOLE_REF_RE,
    ActionStep,
    AskStep,
    BranchStep,
    LoopStep,
    NotifyStep,
    StopIfRepliedStep,
    WaitStep,
)

log = logging.getLogger("agentos.runner")

PERSON = "person"  # in a send step: who this message is for
CHILD_POLL_S = 60.0  # a run that waits for a child looks again this often, whatever wakes it first
SEND_BATCH = 25  # mails a walk sends before it gives its slot back, so a big list does not hold one
SHOWN_MAX = 4096
HIDDEN = "(hidden)"
ROW_COUNT = "list_of_rows"  # an origin's kind: the overlay's card says "a list of rows at <host>"
LEAD = "lead"  # an origin's kind: the overlay's card says "a lead"
ROW_WHY = "It runs once for each row the site gave, so the site decides how many times it acts. A person says yes first, " \
          "or a standing approval does."


class _Wait(Exception):
    def __init__(self, reason: str, until: datetime | None):
        super().__init__(reason)
        self.reason, self.until = reason, until


class _Park(Exception):
    def __init__(self, approval_id: str, reason: str):
        super().__init__(reason)
        self.approval_id, self.reason = approval_id, reason


class _Ended(Exception):
    """A person said no to an `ask` that stops."""


class _NobodyLeft(Exception):
    """Everyone a step was chasing answered: the rest of this row stops, or of the run outside a loop."""


class StepFailed(Exception):
    """`outside` says the words may quote something: a value, a child's error. Those stay on the node
    (`mirror.py`), and words the walk wrote itself do not have to."""

    def __init__(self, code: str, words: str, *, acted: bool = False, outside: bool = False):
        super().__init__(words)
        self.code, self.words, self.acted, self.outside = code, words, acted, outside


@dataclass
class Value:
    value: Any
    untrusted: bool


class Data:
    """What the steps have produced so far, and which of it came from outside."""

    def __init__(self, trigger: dict[str, Any], untrusted: list[str], rows: list[str] | None = None):
        self.values: dict[str, dict[str, Any]] = {TRIGGER: dict(trigger)}
        self.outside: dict[str, set[str]] = {TRIGGER: set(untrusted)}
        self.rows = set(rows or [])  # of the trigger's keys: lists the node built, which only a loop may go through

    def set(self, source: str, values: dict[str, Any], untrusted: list[str] | set[str]) -> None:
        self.values[source], self.outside[source] = dict(values), set(untrusted)

    def drop(self, source: str) -> None:
        self.values.pop(source, None)
        self.outside.pop(source, None)

    def get(self, source: str, key: str) -> Value:
        values = self.values.get(source)
        if values is None or key not in values:
            raise StepFailed("missing_value", f"{{{{{source}.{key}}}}} has no value in this run.")
        return Value(values[key], key in self.outside.get(source, set()))

    def fill(self, text: Any) -> Value:
        """A text with its references filled, in one pass: what a value contains is never read as a
        reference. A field that is exactly one reference keeps its type."""
        if not isinstance(text, str):
            return Value(text, False)
        whole = WHOLE_REF_RE.match(text)
        if whole:
            return self.get(whole.group(1), whole.group(2))
        outside = False

        def one(m: Any) -> str:
            nonlocal outside
            v = self.get(m.group(1), m.group(2))
            outside = outside or v.untrusted
            return "" if v.value is None else str(v.value)

        return Value(REF_RE.sub(one, text), outside)

    def decides(self, ref: str, what: str) -> Any:
        """A value that picks what happens. The save checked this; here it is checked on the value."""
        v = self.fill(ref)
        if v.untrusted:
            raise StepFailed("untrusted_decides", f"{what} would use a value from outside, so the step stopped.")
        return v.value

    def is_rows(self, ref: str) -> bool:
        whole = WHOLE_REF_RE.match(ref)
        return bool(whole and whole.group(1) == TRIGGER and whole.group(2) in self.rows)

    def over(self, ref: str) -> Any:
        """What a loop goes through. A list the node built of a site's rows may be gone through: each
        row is still from outside, because the list is, so `_item_outside` marks every key of it."""
        return self.fill(ref).value if self.is_rows(ref) else self.decides(ref, "What it goes through")


def compare(left: Any, op: str, right: str) -> bool:
    if op == "is_empty":
        return left in (None, "", [], {})
    if op == "not_empty":
        return left not in (None, "", [], {})
    if isinstance(left, bool):
        left = "yes" if left else "no"
    if op in ("more_than", "less_than"):
        try:
            a, b = float(left), float(right)
        except (TypeError, ValueError):
            raise StepFailed("not_a_number", f"Comparing {left!r} with {right!r} needs two numbers.", outside=True) from None
        return a > b if op == "more_than" else a < b
    text = "" if left is None else str(left).strip()
    if op == "contains":
        return right.strip().lower() in text.lower()
    same = text.lower() == right.strip().lower()
    return same if op == "is" else not same


def shown_value(value: Any, scrub: Any = None) -> Any:
    """A value as it may leave the node: clipped, with every string in it scrubbed of vault values."""
    kept = clip(value, max_str=SHOWN_MAX)
    if scrub is None:
        return kept
    if isinstance(kept, str):
        return scrub(kept)
    if isinstance(kept, dict):
        return {k: shown_value(v, scrub) for k, v in kept.items()}
    if isinstance(kept, list):
        return [shown_value(v, scrub) for v in kept]
    return kept


def settled(child: Run) -> str:
    """What a finished child means for the slots it held. Only a certain failure gives them back."""
    if child.status in SUCCEEDED:
        return "sent"
    return "failed" if child.status in (RunStatus.FAILED, RunStatus.CANCELLED) else "unknown"


def address_key(address: str) -> str:
    return hashlib.sha256(address.encode()).hexdigest()[:16]


class Walk:
    """One pass of one run. Everything it needs to survive a stop is in the journal."""

    def __init__(self, engine: WorkflowExecutor, run: Run, journal: Journal, control: RunControl, wv: Any):
        self.e, self.run, self.journal, self.control, self.wv = engine, run, journal, control, wv
        self.runner, self.store = engine.runner, engine.runner.store
        self.records: dict[str, StepRecord] = {r.path: r for r in self.store.steps(run.id)}
        self.states = self.store.step_states(run.id)
        params = run.params or {}
        self.data = Data(params.get("trigger") or {}, params.get("untrusted") or [], params.get("rows"))
        self.origin = params.get("origin") or {"kind": "workflow", "host": None}
        self.index = 0
        self.loops = 0  # how many loops surround the step being walked
        self.row_loops = 0  # how many of them go through a site's rows
        self.approved: str | None = None  # the approval a person just gave, for the step that waited on it
        self.action_paths: dict[str, str] = {}  # a step id on this path, to where its ledger rows are
        self.chasing: str | None = None  # after a stop_if_replied: the send whose people are all a later send may reach

    # ---- the journal ----
    def _state(self, path: str) -> dict[str, Any]:
        return self.states.setdefault(path, {})

    def _save(self, path: str, **values: Any) -> None:
        state = self._state(path)
        state.update(values)
        self.store.put_step_state(self.run.id, path, state)

    def _begin(self, path: str, step: Any) -> StepRecord | None:
        """The step's record, started if it was not. Done, it comes back as it is."""
        self.index += 1
        rec = self.records.get(path)
        if rec is None or rec.status in (StepStatus.PENDING, StepStatus.FAILED):
            self.journal.start(path, self.index, step.kind)
            self.records = {r.path: r for r in self.store.steps(self.run.id)}
            return self.records[path]
        return rec

    def _show(self, step: Any, values: dict[str, Any], hidden: set[str] | None = None) -> dict[str, Any]:
        """What may be shown: hidden values as their name, secrets scrubbed, each value clipped."""
        return {k: HIDDEN if hidden and k in hidden else shown_value(v, self.runner.flows.vault_scrub)
                for k, v in values.items()}

    def _done(self, path: str, step: Any, *, input_: dict[str, Any] | None = None, output: dict[str, Any] | None = None,
              outside: list[str] | None = None, short: str | None = None, hidden: set[str] | None = None,
              child: str | None = None, acted: bool = False, verified: bool | None = None, stopped: bool = False) -> None:
        shown = {"label": step.label, "kind": step.kind, "input": self._show(step, input_ or {}, hidden),
                 "output": self._show(step, output or {}, hidden), "short": short, "untrusted": sorted(outside or []),
                 "run_id": child, "acted": acted, "verified": verified, **({"stopped": True} if stopped else {})}
        state = self._state(path)
        state.update(out=output or {}, outside=sorted(outside or []))
        self.journal.done(path, shown, state)

    def _restore(self, path: str, step: Any) -> None:
        state = self.states.get(path) or {}
        self.data.set(step.id, state.get("out") or {}, state.get("outside") or [])

    # ---- the walk ----
    async def steps(self, steps: list[Any], prefix: str = "") -> None:
        for step in steps:
            self.control.check()
            path = f"{prefix}{step.id}"
            if isinstance(step, BranchStep):
                await self.branch(step, path)
            elif isinstance(step, LoopStep):
                await self.loop(step, path)
            elif isinstance(step, ActionStep):
                await self.action(step, path)
            elif isinstance(step, WaitStep):
                self.wait(step, path)
            elif isinstance(step, AskStep):
                await self.ask(step, path)
            elif isinstance(step, NotifyStep):
                self.notify(step, path)
            elif isinstance(step, StopIfRepliedStep):
                self.stop_if_replied(step, path)

    async def branch(self, step: BranchStep, path: str) -> None:
        rec = self._begin(path, step)
        if rec.status != StepStatus.DONE:
            left = self.data.decides(step.when.left or "", "What it checks")
            took = "then" if compare(left, step.when.op, step.when.right) else "otherwise"
            self._done(path, step, input_={"left": left, "op": step.when.op, "right": step.when.right},
                       output={"took": took}, short=f"Went {'the first' if took == 'then' else 'the other'} way")
        took = (self.states.get(path) or {}).get("out", {}).get("took", "then")
        await self.steps(step.then if took == "then" else step.otherwise, f"{path}/{took}/")

    async def loop(self, step: LoopStep, path: str) -> None:
        rec = self._begin(path, step)
        if rec.status == StepStatus.DONE:
            state = self.states.get(path) or {}
            items, marks = state.get("items") or [], state.get("marks") or []
        else:
            state = self._state(path)
            if "items" not in state:
                items = self.data.over(step.over or "")
                if not isinstance(items, list):
                    raise StepFailed("not_a_list", f"{step.over} is not a list in this run.")
                if len(items) > MAX_LOOP_ITEMS:
                    raise StepFailed("too_many_items", f"A loop goes through at most {MAX_LOOP_ITEMS} things.")
                self._save(path, items=items, marks=self._item_outside(step))
            items, marks = state["items"], state.get("marks") or []
        rows = int(self.data.is_rows(step.over or ""))
        stopped = 0
        chasing = self.chasing
        for i, item in enumerate(items):
            values = item if isinstance(item, dict) else {"value": item}
            self.data.set(step.id, {"value": item, **values}, {"value", *values} if "*" in marks else marks)
            self.loops += 1
            self.row_loops += rows
            try:
                await self.steps(step.body, f"{path}/{i}/")
            except _NobodyLeft:
                stopped += 1
            finally:
                self.loops -= 1
                self.row_loops -= rows
                self.chasing = chasing  # what one row found says nothing of the next
        self.data.drop(step.id)
        if rec.status != StepStatus.DONE:
            self._done(path, step, input_={"items": len(items)}, output={"count": len(items), **({"stopped": stopped} if stopped else {})},
                       short=f"{len(items)} done" + (f", {stopped} stopped because nobody is left to chase" if stopped else ""))

    def _item_outside(self, step: LoopStep) -> list[str]:
        """Which keys of an item came from outside: `<list>_<key>` of the list's source, or every
        key when the list itself did (`*`)."""
        m = WHOLE_REF_RE.match(step.over or "")
        if m is None:
            return []
        source, key = m.group(1), m.group(2)
        outside = self.data.outside.get(source, set())
        prefix = f"{key}_"
        return sorted({k[len(prefix):] for k in outside if k.startswith(prefix)} | ({"*"} if key in outside else set()))

    def wait(self, step: WaitStep, path: str) -> None:
        rec = self._begin(path, step)
        if rec.status == StepStatus.DONE:
            return
        now = self.e.clock.now()
        until = (self.states.get(path) or {}).get("until")
        if until is None:
            until = (now + timedelta(seconds=step.seconds)).isoformat()
            self._save(path, until=until)
        at = datetime.fromisoformat(until)
        if now < at:
            raise _Wait(f"waiting until {until}", at)
        self._done(path, step, input_={"amount": step.amount, "unit": step.unit}, short="Waited")

    def notify(self, step: NotifyStep, path: str) -> None:
        rec = self._begin(path, step)
        if rec.status == StepStatus.DONE:
            return
        message = self.data.fill(step.message)
        self.runner._notify(self.wv.definition.name, str(message.value)[:2000], level="info",
                            data={"workflow": self.run.flow_id, "run_id": self.run.id, "step": step.id})
        self._done(path, step, input_={"message": message.value}, outside=["message"] if message.untrusted else [],
                   short="Told you")

    def stop_if_replied(self, step: StopIfRepliedStep, path: str) -> None:
        """Who `after` reached and who answered it. From here on a send goes only to those still
        waiting, and with nobody left this row stops, or the run outside a loop. Reaching someone
        in doubt counts: it may have gone. The record holds counts, because a run's record reaches
        agents and the controller, and an address never does."""
        rec = self._begin(path, step)
        if rec.status == StepStatus.DONE:
            state = self.states.get(path) or {}
            self.chasing = state.get("chasing")
            if state.get("nobody_left"):
                raise _NobodyLeft
            return
        after = self.action_paths.get(step.after or "")
        if after is None:
            raise StepFailed("nothing_to_check", f"{step.after} did not send anything in this run.")
        store = self.e.sends.store
        reached = {r["address"] for r in store.ledger_rows(self.run.id, after) if r["state"] in ("sent", "unknown")}
        replied = reached & self.e.replies.replied(self.run.id, after)
        waiting = len(reached - replied)
        if replied and not waiting:
            short = "They replied, so it stops here" if len(replied) == 1 else f"All {len(replied)} replied, so it stops here"
        elif not reached:
            short = "It reached nobody, so it stops here"
        else:
            short = f"{len(replied)} replied, {waiting} still to hear from"
        self.chasing = after
        self._save(path, chasing=after, nobody_left=not waiting)
        self._done(path, step, input_={"after": step.after}, output={"replied": len(replied), "waiting": waiting}, short=short,
                   stopped=not waiting)
        if not waiting:
            raise _NobodyLeft

    def _not_chased(self, address: str) -> str | None:
        """Why a send after a stop_if_replied leaves this address out, read as it goes, so a reply
        that came while it waited for tomorrow still counts."""
        if self.chasing is None:
            return None
        if address in self.e.replies.replied(self.run.id, self.chasing):
            return "they replied"
        reached = self.e.sends.store.ledger_get(self.run.id, self.chasing, address) in ("sent", "unknown")
        return None if reached else "the first message did not reach them"

    async def ask(self, step: AskStep, path: str) -> None:
        rec = self._begin(path, step)
        if rec.status == StepStatus.DONE:
            self._restore(path, step)
            return
        approvals = self.runner._service("approvals")
        if approvals is None:
            raise StepFailed("ask_unavailable", "This node has no way to ask a person.")
        state = self._state(path)
        asked = state.get("approval_id")
        if asked is None or (rec.attempts > state.get("asked_on", 1)):
            question = self.data.fill(step.question)
            ap = approvals.create_question(
                question=str(question.value), options=["Yes", "No"], requester=self._requester(),
                grant_id=self.run.grant_id, grant_path=self.runner._grant_path(self.run.grant_id or ""),
                workspace=self.wv.definition.workspace)
            self._save(path, approval_id=ap.id, asked_on=rec.attempts, question=str(question.value),
                       question_outside=question.untrusted)
            raise _Park(ap.id, f"waiting for an answer: {step.label}")
        decided = approvals.get(asked)
        answer = str(getattr(decided, "answer", None) or "").strip().lower()
        if getattr(decided, "state", None) not in ("approved", "consumed") or answer not in ("yes", "no"):
            raise _Park(asked, f"waiting for an answer: {step.label}")
        yes = answer == "yes"
        self._done(path, step, input_={"question": state.get("question")}, output={"answer": yes},
                   outside=["question"] if state.get("question_outside") else [], short="Yes" if yes else "No")
        self._restore(path, step)
        if not yes and step.on_no == "stop":
            raise _Ended

    def _requester(self) -> Principal:
        p = self.run.principal or {}
        return Principal(**{k: v for k, v in p.items() if k in Principal.model_fields}) if p.get("id") else \
            Principal(kind="flow", id=self.run.flow_id or "workflow")

    # ---- action ----
    def _inputs(self, step: ActionStep, op: Operation) -> tuple[dict[str, Any], list[str]]:
        values, outside = {}, []
        for name, raw in step.inputs.items():
            v = self.data.fill(raw)
            values[name] = v.value
            if v.untrusted:
                outside.append(name)
                if name in op.targets:
                    raise StepFailed("untrusted_decides", f"{name} says where it acts, and a value from outside filled it.")
        return values, outside

    def _origins(self, op: Operation, outside: list[str]) -> list[dict[str, Any]]:
        """What the overlay reads. A value from outside filled this step's inputs, or the step acts
        inside a loop over a site's rows: a row's text cannot pick an input, but how many rows there
        are says how many times this acts, whatever filled it. A read there runs on its own."""
        if outside:
            return [self.origin]
        return [{**self.origin, "kind": ROW_COUNT}] if self.row_loops and op.acts else []

    def _trigger(self, op: Operation, path: str, outside: list[str]) -> dict[str, Any]:
        trigger: dict[str, Any] = {"kind": "workflow", "workflow": self.run.flow_id, "run": self.run.id, "step": path}
        if started_by_schedule(self.run):
            trigger["scheduled"] = True
        origins = self._origins(op, outside)
        return {**trigger, "origins": origins} if origins else trigger

    def _row_act(self, op: Operation, path: str) -> None:
        """One act inside a loop over a site's rows, kept in the journal so a walk counts it once. A
        run stops at the bound, so more loops or more steps in a loop do not multiply what a site can
        reach: an event already carries at most so many rows of each list."""
        if not op.acts or not self.row_loops or self._state(path).get("why"):
            return
        if sum(1 for state in self.states.values() if state.get("why")) >= MAX_ROW_ACTS:
            raise StepFailed("too_many_rows", f"This run already acted {MAX_ROW_ACTS} times for a site's rows, the most one "
                             "run may, so it stopped here.")
        self._save(path, why=ROW_WHY)

    def _own_child(self, key: str, op: Operation) -> Run | None:
        """The step's child run under `key`, or None when there is none yet. A run under that key that
        this walk did not queue, for this flow, is never taken for the step's own."""
        child = self.store.run_by_key(key)
        if child is None:
            return None
        if child.parent != self.run.id or child.flow_id != op.flow_id:
            raise StepFailed("approval_mismatch", "Another run already holds this step's place, so the step did "
                             "nothing.")
        return child

    def _submit(self, op: Operation, params: dict[str, Any], key: str, path: str, outside: list[str], *,
                authorized: bool = False, auto: bool = False) -> Run:
        try:
            child, _ = self.runner.submit(
                flow_id=op.flow_id, version=op.version, params=params, lane=op.on, idempotency_key=key, parent=self.run.id,
                principal=self.run.principal, source=f"workflow:{self.run.flow_id}", trigger=self._trigger(op, path, outside),
                authorized=authorized, auto=auto, grant_id=self.run.grant_id)
        except (ConnectorBroken, PaceLimited) as e:
            raise _Wait(e.message, getattr(e, "retry_at", None) or self.e.clock.now() + timedelta(minutes=15)) from e
        except (PolicyDenied, NotFound, InvalidRequest) as e:
            raise StepFailed(e.code, e.message, outside=not isinstance(e, PolicyDenied)) from e
        return child

    def _output(self, child: Run, op: Operation) -> tuple[dict[str, Any], set[str]]:
        """A step's output and which of it came from outside. What the operation declares, and
        what the run itself labelled, both count."""
        from agentos.runner.connectors.outputs import saved_values

        if child.output is not None:
            data = dict(child.output.get("data") or {})
            outside = set(child.output.get("untrusted_fields") or [])
        else:
            data, outside = saved_values(self.store, child.id), set()
        declared = {f.key: f for f in op.produces}
        for key in data:
            f = declared.get(key)
            if f is None or f.untrusted:
                outside.add(key)
        return data, outside

    async def action(self, step: ActionStep, path: str) -> None:
        rec = self._begin(path, step)
        self.action_paths[step.id] = path
        if rec.status == StepStatus.DONE:
            self._restore(path, step)
            return
        op = self.e.operation(step.operation or "")
        if op is None:
            raise StepFailed("unknown_operation", f"{step.operation} is not on this computer any more.")
        self._row_act(op, path)
        if op.sends is not None or op.builtin:
            await self.send(step, op, path, rec)
            return
        state = self._state(path)
        key = f"wf:{self.run.id}:{path}:{rec.attempts}"
        values, outside = self._inputs(step, op)
        child = self._own_child(key, op)
        if child is None:
            self._count_against_limit(step, op, f"{path}#{rec.attempts}")
            child = self._submit(op, values, key, path, outside)
            self._save(path, child=child.id, input=self._masked(op, values), input_outside=outside)
        if child.status not in TERMINAL:
            raise _Wait(f"waiting for {step.label}", self.e.clock.now() + timedelta(seconds=CHILD_POLL_S))
        self.e.sends.store.ledger_settle(self.run.id, f"{path}#{rec.attempts}", settled(child))
        self._finish_child(step, op, path, child, state)

    def _list_size(self, step: ActionStep) -> int:
        """The longest list that feeds this step, whatever the definition said about it."""
        size = 0
        for raw in step.inputs.values():
            if isinstance(raw, list):
                size = max(size, len(raw))
            for m in REF_RE.finditer(raw) if isinstance(raw, str) else ():
                value = self.data.get(m.group(1), m.group(2)).value
                if isinstance(value, list):
                    size = max(size, len(value))
        return size

    def _count_against_limit(self, step: ActionStep, op: Operation, ledger_path: str) -> None:
        """A commit in a loop, or fed from a list, may be mail to people that nobody declared, so it
        spends the sending account's daily limit before it acts, unless a person said it reaches
        nobody. This does not read what the operation says about itself: only a person's mark counts."""
        fed = self._list_size(step)
        if not op.acts or op.not_to_people or not (self.loops or fed):
            return
        count, sends, account = max(fed, 1), self.e.sends, op.on or op.lane
        limit = sends.limit_of(account)
        if 0 < limit < count:
            raise StepFailed("over_daily_limit", f"{step.label or op.name} would act for {count} at once, and {account} "
                             f"has a daily limit of {limit}. Loop over them, so it can carry on tomorrow.")
        got = sends.store.ledger_claim_many(self.run.id, ledger_path, count, account, sends.day(), self.e.clock.now(), limit)
        if got == "limit":
            raise _Wait(f"{account} reached its limit of {limit} today, so {step.label or 'this step'} carries on "
                        "tomorrow", sends.next_day())

    @staticmethod
    def _masked(op: Operation, values: dict[str, Any]) -> dict[str, Any]:
        """A param is shown only if its flow marks it (runner/README.md, "Params that are not shown")."""
        return {k: v if isinstance(op.params.get(k), dict) and op.params[k].get("show") else f"${{{k}}}"
                for k, v in values.items()}

    def _finish_child(self, step: ActionStep, op: Operation, path: str, child: Run, state: dict[str, Any]) -> None:
        acted = op.effect not in (Effect.READ, Effect.DRAFT)
        if child.status in SUCCEEDED:
            data, outside = self._output(child, op)
            hidden = set((child.output or {}).get("private_fields") or [])
            self._done(path, step, input_=state.get("input"), output=data, outside=sorted(outside), hidden=hidden,
                       child=child.id, acted=acted, verified=child.status == RunStatus.SUCCEEDED_VERIFIED,
                       short=self.e.child_words(child))
            self._restore(path, step)
            return
        error = child.error or {}
        message = str(error.get("message") or "the step did not work")
        if child.status == RunStatus.PARTIAL:
            raise StepFailed(str(error.get("code") or "step_partial"), message, acted=True, outside=True)
        raise StepFailed(str(error.get("code") or "step_failed"), message, outside=True)

    # ---- send ----
    async def send(self, step: ActionStep, op: Operation, path: str, rec: StepRecord) -> None:
        ws = self.wv.definition.workspace or ""
        sends, spec = self.e.sends, step.send
        if spec is None or not spec.list or sends.store.list_info(spec.list) is None or \
                sends.store.list_info(spec.list)["workspace"] != ws:
            raise StepFailed("unknown_list", "A message goes only to a list the owner supplied here.")
        account = op.on or op.lane
        mailbox = self.e.mailbox(account) if op.builtin else None
        if op.builtin and mailbox is None:
            raise StepFailed("no_mailbox", f"There is no mailbox {account} on this computer.")
        state = self._state(path)
        people = state.get("people")
        if people is None:
            people = self._recipients(spec, ws)
            self._save(path, people=people)
        # the card shows every message filled for its person, exactly as the send fills it, and the
        # approval binds those: a message filled differently at send time is not sent (_check_rendered)
        messages, outside, from_leads = self._rendered(step, op, people)
        await self._authorize_send(step, op, path, spec, account, len(people), (messages, outside), from_leads)
        # a mail still `sending` when a walk starts was cut off mid-send; a flow's is still its child's
        lost = sends.store.ledger_mark_lost(self.run.id, path) if op.builtin else 0
        if rec.attempts > 1:
            sends.store.ledger_clear_failed(self.run.id, path)
        day = sends.day()
        limit = sends.limit_of(account)
        doubts = lost
        tried = 0
        for person in people:
            self.control.check()
            address = person["address"]
            known = sends.store.ledger_get(self.run.id, path, address)
            if known in ("sent", "unknown", "skipped", "failed"):
                continue
            if sends.store.is_suppressed(ws, address):
                sends.store.ledger_mark(self.run.id, path, address, account, "skipped", day, self.e.clock.now(), "suppressed")
                continue
            # read again, not from what the run kept: someone erased or taken off since gets nothing
            if person.get("not_on_list") or sends.store.person(spec.list, address) is None:
                sends.store.ledger_mark(self.run.id, path, address, account, "skipped", day, self.e.clock.now(), "not on the list")
                continue
            why_not = self._not_chased(address)
            if why_not:
                sends.store.ledger_mark(self.run.id, path, address, account, "skipped", day, self.e.clock.now(), why_not)
                continue
            if op.builtin:
                if tried >= SEND_BATCH:
                    self._save(path, doubted=bool(doubts or state.get("doubted")))
                    raise _Wait(f"sending in batches: {step.label}", self.e.clock.now() + timedelta(seconds=1))
                tried += 1
                try:
                    doubts += await self._mail(step, op, path, person, mailbox, account, ws, limit, day)
                except SendStopped as e:
                    self._save(path, doubted=bool(doubts or state.get("doubted")))
                    reached = any(r["state"] == "sent" for r in sends.store.ledger_rows(self.run.id, path))
                    raise StepFailed(e.code, f"Nothing more was sent: {e}", acted=reached) from None
            else:
                doubts += self._flow_send(step, op, path, person, account, ws, limit, day)
        self._send_done(step, path, doubts or state.get("doubted"), spec.list, account)

    def _recipients(self, spec: Any, ws: str) -> list[dict[str, Any]]:
        from agentos.runner.workflows.sends import normal_address

        store = self.e.sends.store
        if not spec.to:
            return list(store.people(spec.list))
        raw = self.data.decides(spec.to, "Who it goes to")
        address = normal_address(raw)
        if address is None:
            raise StepFailed("bad_address", "That is not an email address.")
        found = store.person(spec.list, address)
        return [found if found else {"address": address, "name": None, "not_on_list": True}]

    def _person_inputs(self, step: ActionStep, op: Operation, person: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        self.data.set(PERSON, {"email": person["address"], "name": person.get("name") or ""}, ["name"] if person.get("lead") else [])
        try:
            return self._inputs(step, op)
        finally:
            self.data.drop(PERSON)

    def _rendered(self, step: ActionStep, op: Operation, people: list[dict[str, Any]]
                  ) -> tuple[list[dict[str, Any]], list[str], set[str]]:
        """Every message the step would send, filled for its person as the send fills it
        (`{"to", "inputs"}` each), the inputs a value from outside filled, and those only a lead's
        text filled: a lead's name is a stranger's text, so a message it fills asks first."""
        messages: list[dict[str, Any]] = []
        outside: set[str] = set()
        from_leads: set[str] = set()
        for person in people:
            values, out = self._person_inputs(step, op, person)
            messages.append({"to": person["address"], "inputs": values})
            plain = self._person_inputs(step, op, {**person, "lead": False})[1] if person.get("lead") else out
            outside.update(plain)
            from_leads.update(set(out) - set(plain))
        if not people:  # nobody to send to: the card still shows the message, as an empty field fills it
            values, out = self._person_inputs(step, op, {"address": "", "name": ""})
            outside.update(out)
        return messages, sorted(outside), from_leads - outside

    def _check_rendered(self, path: str, address: str, values: dict[str, Any], op: Operation) -> None:
        """The message about to go to `address` is the one its authorization covered, filled the same
        way and sent by the same flow version (a step that waited, for the daily limit or a child,
        resolves its operation again); else nothing more is sent (approval_mismatch)."""
        from agentos.policy.approval_binding import content_digest

        state = self._state(path)
        want = (state.get("rendered") or {}).get(address)
        if want is None or want != content_digest(values) or state.get("sent_by") != op.version:
            reached = any(r["state"] == "sent" for r in self.e.sends.store.ledger_rows(self.run.id, path))
            raise StepFailed("approval_mismatch", "This message is not the one that was approved, so it was not "
                             "sent. Nothing more was sent.", acted=reached)

    async def _authorize_send(self, step: ActionStep, op: Operation, path: str, spec: Any, account: str, count: int,
                              preview: tuple[list[dict[str, Any]], list[str]], from_leads: set[str]) -> None:
        """One authorization for what the step will send, right before it sends: it covers each
        message as filled for its person, and keeps a digest of each for the send to check."""
        from agentos.policy.approval_binding import content_digest

        state = self._state(path)
        if state.get("authorized"):
            return
        messages, outside = preview
        content = {"workflow": self.run.flow_id, "step": step.id, "operation": op.id, "list": spec.list,
                   "to": spec.to, "people": count, "messages": messages}
        if op.version is not None:  # the flow version that sends: a newer one asks again
            content["version"] = op.version
        rendered = {m["to"]: content_digest(m["inputs"]) for m in messages}
        # released by the person's approval only while it covers what the step would send now; when it
        # does not, the step asks again, for this
        if self.approved is not None and state.get("approval_id") == self.approved and \
                await self._approval_covers(self.approved, op.effect, account, content):
            self._save(path, authorized=True, auto=False, rendered=rendered, sent_by=op.version)
            return
        ctx = self.runner._grant_context(self.run.grant_id, self.run.principal)
        if ctx is None:
            self._save(path, authorized=True, auto=True, rendered=rendered, sent_by=op.version)
            return
        ctx.tool = "workflow_send"
        origins = [*self._origins(op, outside), *([{"kind": LEAD, "host": None}] if from_leads else [])]
        self.e.bind(ctx, self.run.flow_id, origins)
        ctx.scheduled = started_by_schedule(self.run)
        words = f"Send {step.label or op.name} to {count} on {spec.list}"
        try:
            decision = await ctx.authorize(op.effect, lane_id=account, summary=words, content=content)
        except ApprovalPending as e:
            self._save(path, approval_id=e.approval_id)
            raise _Park(e.approval_id, e.message) from e
        except PolicyDenied as e:
            raise StepFailed(e.code, e.message) from e
        self._save(path, authorized=True, auto=getattr(decision, "route", None) == "auto", rendered=rendered, sent_by=op.version)

    async def _approval_covers(self, approval_id: str, effect: Any, account: str, content: dict[str, Any]) -> bool:
        """Does the approval the run was released with still cover exactly this send (the policy
        gate's card, agentos.policy.approval_binding.action_binding_problem)? A node with no approvals
        service releases it as before."""
        from types import SimpleNamespace

        from agentos.policy.approval_binding import ActionBinding, action_binding_problem

        get = getattr(self.runner._service("approvals"), "get", None)
        if not callable(get):
            return True
        try:
            ap = await maybe_await(get(approval_id))
        except NotFound:
            return False
        if isinstance(ap, dict):
            ap = SimpleNamespace(**ap)
        binding = ActionBinding(effect=str(getattr(effect, "value", effect)), tool="workflow_send", lane=account,
                                content=content)
        return action_binding_problem(ap, binding) is None

    def _claim(self, path: str, address: str, account: str, limit: int, day: str, step: ActionStep) -> bool:
        sends = self.e.sends
        got = sends.store.ledger_claim(self.run.id, path, address, account, day, self.e.clock.now(), limit)
        if got == "limit":
            raise _Wait(f"{account} reached its limit of {limit} today, so {step.label or 'this step'} carries on "
                        "tomorrow", sends.next_day())
        return got == "claimed"

    def _mark(self, path: str, address: str, account: str, state: str, day: str, note: str | None = None) -> None:
        self.e.sends.store.ledger_mark(self.run.id, path, address, account, state, day, self.e.clock.now(), note)

    async def _mail(self, step: ActionStep, op: Operation, path: str, person: dict[str, Any], box: Any, account: str,
                    ws: str, limit: int, day: str) -> int:
        address = person["address"]
        unsubscribe = self.e.sends.unsubscribe_for(ws, address, mailbox=box.address)
        if unsubscribe is None:
            raise StepFailed("no_unsubscribe", "This message has no way to unsubscribe, so it was not sent.")
        values, _ = self._person_inputs(step, op, person)
        self._check_rendered(path, address, values, op)
        message = build_message(box, to=address, subject=str(values.get("subject") or ""),
                                body=str(values.get("body") or ""), unsubscribe=unsubscribe)
        if not self._claim(path, address, account, limit, day, step):
            return 0
        self.e.replies.thread(message, run_id=self.run.id, path=path, address=address, box=box)
        try:
            await self.e.mail.send(box, message, to=address)
        except SendStopped as e:
            self._mark(path, address, account, "failed", day, str(e)[:200])
            raise
        except SendRefused as e:
            self._mark(path, address, account, "failed", day, str(e)[:200])
            return 0
        except SendInDoubt as e:
            self._mark(path, address, account, "unknown", day, str(e)[:200])
            return 1
        except Exception as e:  # handed over or not, nobody can say: it is never repeated
            log.exception("run %s: mail to one address failed", self.run.id)
            self._mark(path, address, account, "unknown", day, type(e).__name__)
            return 1
        self._mark(path, address, account, "sent", day)
        return 0

    def _flow_send(self, step: ActionStep, op: Operation, path: str, person: dict[str, Any], account: str, ws: str,
                   limit: int, day: str) -> int:
        address = person["address"]
        key = f"wf:{self.run.id}:{path}:{address_key(address)}"
        child = self._own_child(key, op)
        if child is None:
            unsubscribe = self.e.sends.unsubscribe_for(ws, address, mailbox=None)
            if unsubscribe is None:
                raise StepFailed("no_unsubscribe", "This message has no way to unsubscribe, so it was not sent.")
            # still `sending` with no child: the node stopped before it was queued, so nothing left
            if self.e.sends.store.ledger_get(self.run.id, path, address) != "sending":
                self._claim(path, address, account, limit, day, step)
            values, outside = self._person_inputs(step, op, person)
            self._check_rendered(path, address, values, op)
            spec = op.sends
            values[spec.to] = address
            values[spec.body] = f"{values.get(spec.body) or ''}{unsubscribe.footer()}"
            state = self._state(path)
            child = self._submit(op, values, key, path, outside, authorized=True, auto=bool(state.get("auto")))
        if child.status not in TERMINAL:
            raise _Wait(f"sending to one person: {step.label}", self.e.clock.now() + timedelta(seconds=CHILD_POLL_S))
        if child.status in SUCCEEDED:
            self._mark(path, address, account, "sent", day)
            return 0
        if child.status == RunStatus.FAILED:
            self._mark(path, address, account, "failed", day, str((child.error or {}).get("code"))[:80])
            return 0
        self._mark(path, address, account, "unknown", day, str(child.status))
        return 1

    def _send_done(self, step: ActionStep, path: str, doubts: Any, list_id: str, account: str) -> None:
        rows = self.e.sends.store.ledger_rows(self.run.id, path)
        count = {s: sum(1 for r in rows if r["state"] == s) for s in ("sent", "skipped", "failed", "unknown")}
        output = {"sent": count["sent"], "skipped": count["skipped"], "failed": count["failed"], "in_doubt": count["unknown"]}
        replied = sum(1 for r in rows if r["note"] == "they replied")
        words = f"{count['sent']} sent" + (f", {count['skipped']} skipped" if count["skipped"] else "") + \
            (f" ({replied} replied)" if replied else "")
        if count["failed"]:
            raise StepFailed("send_failed", f"{words}, {count['failed']} failed. A retry sends only to those.",
                             acted=count["sent"] > 0)
        if doubts:
            raise StepFailed("send_in_doubt", f"{words}, and {count['unknown']} may or may not have gone. They will not "
                             "be sent again.", acted=True)
        self._done(path, step, input_={"list": list_id, "account": account}, output=output,
                   short=words + (f", {count['unknown']} in doubt" if count["unknown"] else ""), acted=count["sent"] > 0)
        self._restore(path, step)


class WorkflowExecutor:
    """Registered for RunKind.WORKFLOW. `service` is what resolves operations and holds the rest."""

    def __init__(self, service: Any):
        self.service = service
        self.runner = service.runner
        self.clock = service.clock
        self.sends = service.sends
        self.mail = service.mail
        self.replies = service.replies

    def operation(self, text: str) -> Operation | None:
        return self.service.operation(text)

    def mailbox(self, mailbox_id: str) -> Any:
        return self.mail.get(mailbox_id)

    @staticmethod
    def bind(ctx: Any, workflow: str | None, origins: list[dict[str, Any]]) -> None:
        """Tell the policy which workflow this call is a step of and what filled it: both can only
        make a call ask, never less."""
        ctx.routine = workflow
        ctx.origins = list(origins)

    def child_words(self, child: Run) -> str:
        return "Worked, and checked" if child.status == RunStatus.SUCCEEDED_VERIFIED else "Worked"

    async def execute(self, run: Run, journal: Journal, control: RunControl) -> Outcome:
        wv = self.service.store.version(run.flow_id or "", run.flow_version or "")
        if wv is None:
            return Outcome("finish", RunStatus.FAILED, error={"code": "workflow_missing",
                                                              "message": f"{run.flow_id} {run.flow_version} is gone"})
        walk = Walk(self, run, journal, control, wv)
        if run.authorized and run.approval_id:
            walk.approved = run.approval_id
            run.authorized = False
            self.runner.store.save_run(run)
        try:
            await walk.steps(wv.definition.steps)
        except _Wait as w:
            return Outcome("defer", reason=w.reason, hold="workflow", until=w.until)
        except _Park as p:
            return Outcome("park", approval_id=p.approval_id, reason=p.reason)
        except _Ended:
            return Outcome("finish", RunStatus.CANCELLED, error={"code": "stopped_by_answer", "own_words": True,
                                                                 "message": "A person answered no, so it stopped here."})
        except _NobodyLeft:
            return self._finished(run)
        except StopRun as s:
            if s.why == "cancel":
                return Outcome("cancel", RunStatus.CANCELLED, error={"code": "cancelled", "message": "cancelled", "own_words": True})
            return Outcome("requeue")
        except StepFailed as f:
            return self._failed(run, journal, f)
        except AgentosError as e:
            return self._failed(run, journal, StepFailed(e.code, e.message, outside=True))
        return self._finished(run)

    def _current(self, run: Run) -> str | None:
        started = [r for r in self.runner.store.steps(run.id) if r.status == StepStatus.STARTED]
        return started[-1].path if started else None

    def _failed(self, run: Run, journal: Journal, f: StepFailed) -> Outcome:
        path = self._current(run)
        own = {} if f.outside else {"own_words": True}
        if path is not None:
            journal.failed(path, {"code": f.code, "message": f.words, **own}, result={"note": f.words, "acted": f.acted})
        acted = f.acted or any((r.result or {}).get("acted") for r in self.runner.store.steps(run.id))
        return Outcome("finish", RunStatus.PARTIAL if acted else RunStatus.FAILED,
                       error={"code": f.code, "message": f.words, "step": path, **own})

    def _finished(self, run: Run) -> Outcome:
        records = self.runner.store.steps(run.id)
        checked = [(r.result or {}).get("verified") for r in records if r.type == "action"]
        verified = bool(checked) and all(v is True for v in checked)
        status = RunStatus.SUCCEEDED_VERIFIED if verified else RunStatus.SUCCEEDED_UNVERIFIED
        return Outcome("finish", status, output=self.service.result_of(run, records))
