"""The pilot's loop: read the page, ask, act through the node's tools, read again.

A decider picks and never moves anything, so code owns the loop. This is the logic measured on
lanebench as the jev executor (tests/lanebench/executors/jev.py), with what a product run needs on
top: a low-confidence pick goes up a level instead of just stopping, a commit waits for a person and
the run carries on after, and the end of a run says honestly what is known.

- Confidence gates only what has an effect. A shaky WAIT or SCROLL costs nothing; a shaky click goes
  up a level. With a rescue configured it picks from the SAME options; without one the run ends
  needs_help, saying so. Every escalation is recorded with its reason and what it cost. An entry is
  judged by the head that picked its field, and a click by its target head as well as the operation.
- A value counts as entered only when its action worked, and a form's button is not pressed while
  values for that form are still waiting.
- A secret never reaches a model: it sees a label, what it reads is masked first, and the pilot sends
  no images. `must_not_send` is a second check for a caller that holds a secret's text; a node run
  holds none, because the vault fills by name, so there it is empty.
- The page's deterministic signal beats any model: a captcha, a checkpoint or a block ends the run.
- Words just entered keep to their form until something is pressed: a control that looks the same
  elsewhere on the page is not on offer, since it sends something else.
- A commit is the node's to catch: browser_act comes back approval_pending, the run waits, and the
  same click runs again after a person approved exactly what it sends.
- A refusal counts against the progress budget, so an injected link cannot make it loop. A refusal
  that says stop (the wrong account, a limit, the lane's pace) ends the run at once.
- done_verified needs the last action's target to have changed, with nothing refused or blocked since
  the last action that worked. A page that merely changed proves nothing: a clock ticks.
- A target that changed proves the click landed, not that it landed on the right post: the item it sat
  in must hold what the request named (anchor.py), or the run ends done_unverified, saying so.
- A signed-out page stops every run but one a person asked to sign in (`login`): a run that merely
  holds a vault value never signs a lane back in on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from agentos.core.models import ActionKind
from agentos.core.signals import CHALLENGE
from agentos.node.snapshot import outline_delta
from agentos.pilot import anchor
from agentos.pilot.deciders import (
    BLOCKED,
    BUDGET,
    DENIED,
    Decider,
    DeciderFailed,
    Pick,
    Reply,
    Rescue,
    Writer,
)
from agentos.pilot.hands import ToolFailed, Tools
from agentos.pilot.models import Escalation, Request, Result, Spend, Status, Value
from agentos.pilot.page import TEXT_ROLES, Element, Page, lean, offered, parse_page
from agentos.pilot.questions import (
    DONE_SURE,
    EFFECT_OPS,
    MAX_OPTIONS,
    MIN_CONFIDENCE,
    NEEDS_OWNER,
    TARGET_ABSENT,
    TARGET_LEAD,
    TARGET_SURE,
    WAIT_SURE,
    Unreadable,
    build_questions,
    chance_of,
    checked,
    dropped,
    line_question,
    noul,
)

OUTLINE_CHARS = 24_000
HISTORY = 8
CHALLENGES = CHALLENGE
LOST_LANE = frozenset({"human_in_control", "agent_paused", "claim_lost", "quarantined"})
REFUSALS = frozenset({"egress_refused", "denied", "limit_reached", "lane_paced", "publish_unbound", "publish_changed",
                      "wrong_account", "taint_cooldown"})
HARD_STOPS = frozenset({"wrong_account", "limit_reached", "lane_paced"})  # no other click can help: stop and say so
SETTLE_S = 0.8                # a page's own save can land just after it looks finished
OP_GATED = frozenset({"CLICK", "WRITE"})  # an entry into a known field is judged by its own head, not this one

NEEDS_RESCUE = "a rescue model (agentos plugins assign browser-worker rescue <brain>), or a clearer goal"
NEEDS_PERSON = "a person, or a clearer goal"
NEEDS_TEXT = ("the text to enter: pass it in values, or assign a writing model "
              "(agentos plugins assign browser-worker write <brain>)")
NEEDS_DECIDER = "a decision brain that can answer (agentos brains list)"
NEEDS_VAULT = "a person: the vault could not fill this field"


class Stop(Exception):
    """How a run ends."""

    def __init__(self, status: Status, reason: str, *, needs: str | None = None, message: str | None = None,
                 answer: dict[str, str] | None = None, approval_id: str | None = None,
                 approval_summary: str | None = None):
        super().__init__(reason)
        self.status, self.reason, self.needs, self.message = status, reason, needs, message
        self.answer, self.approval_id, self.approval_summary = answer or {}, approval_id, approval_summary


class Unsure(Exception):
    """The normal case that goes up a level: this question's answer was too unsure to act on."""

    def __init__(self, reason: str, name: str):
        super().__init__(reason)
        self.reason, self.name = reason, name


@dataclass(frozen=True)
class Plan:
    """What one step will do. `text`: written words, kept so a retried commit sends the same ones."""
    op: str
    target: str | None = None
    value: Value | None = None
    text: str | None = None
    agree: bool = False
    why: str | None = None


@dataclass
class Held:
    """The action that waits for a person. Refs do not outlive a wait, so its element is kept by key."""
    plan: Plan
    key: tuple[str, str, str] | None


@dataclass
class State:
    """Everything a run knows, so it can stop for a person and carry on where it was."""
    n: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    entered: set[str] = field(default_factory=set)
    flipped: set[tuple[str, str, str]] = field(default_factory=set)
    files: dict[str, str] = field(default_factory=dict)
    last_change: str | None = None
    waited: bool = False
    wrote: bool = False
    stale: int = 0
    refused: str | None = None       # the code of a refusal since the page last changed
    doubt: str | None = None         # the code of a refusal or a block since the last action that worked
    effects: int = 0
    readbacks: list[dict[str, Any]] = field(default_factory=list)
    filling: str | None = None       # the container of the field last entered, until something is pressed
    escalations: list[Escalation] = field(default_factory=list)
    spend: dict[str, Spend] = field(default_factory=lambda: {"decide": Spend()})
    rows: list[dict[str, Any]] = field(default_factory=list)
    active_s: float = 0.0
    started: bool = False
    held: Held | None = None
    step_cost: dict[str, Any] = field(default_factory=dict)  # what this step has cost so far: ms, usd, model

    @property
    def usd(self) -> float:
        return sum(s.usd for s in self.spend.values())

    def job(self, name: str) -> Spend:
        return self.spend.setdefault(name, Spend())


@dataclass
class Step:
    page: Page
    pending: list[Value]
    elements: list[Element]
    questions: dict[str, Any]
    state: dict[str, Any]
    row: dict[str, Any]


class Pilot:
    def __init__(self, tools: Tools, decider: Decider, *, rescue: Rescue | None = None, writer: Writer | None = None,
                 checkpoint: Callable[[], None] | None = None, on_step: Callable[[dict[str, Any]], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.tools, self.decider, self.rescue, self.writer = tools, decider, rescue, writer
        self.checkpoint = checkpoint or (lambda: None)  # raises to stop the run: a cancel lands between steps
        self.on_step = on_step or (lambda row: None)
        self.clock, self.sleep = clock, sleep

    # ---- a run ----------------------------------------------------------------------------------

    async def run(self, req: Request, st: State | None = None) -> Result:
        """One run, or the rest of one that stopped for a person (pass its State back)."""
        st = st if st is not None else State()
        t0 = self.clock()
        try:
            await self._go(req, st, t0)
        except Stop as s:
            return self._result(st, s)
        except Unreadable as e:
            return self._result(st, Stop(Status.REFUSED, "unreadable_answer", message=str(e)))
        except DeciderFailed as e:
            return self._result(st, self._no_answer(e))
        finally:
            st.active_s += self.clock() - t0
        raise AssertionError("a run always ends with a Stop")

    async def _go(self, req: Request, st: State, t0: float) -> None:
        if not st.started:
            st.started = True
            st.files = await self._files(req)
            await self._guarded_look()
        page = await self._observe(req)
        if st.held is not None:
            self.checkpoint()
            page = await self._resume(req, st, page)
        if req.read:
            self.checkpoint()
            await self._read(req, st, page)
        while True:
            self.checkpoint()
            self._signals(req, page)
            if st.n >= req.limits.max_steps:
                raise Stop(Status.GAVE_UP, "step_budget")
            if st.active_s + self.clock() - t0 >= req.limits.wall_s:
                raise Stop(Status.GAVE_UP, "time_budget")
            if st.usd >= req.limits.max_usd:
                raise Stop(Status.GAVE_UP, "budget")
            page = await self._step(req, st, page)

    @staticmethod
    def _signals(req: Request, page: Page) -> None:
        """The page's deterministic signal, which no model outranks."""
        if page.signal in CHALLENGES:
            raise Stop(Status.HELD, f"signal_{page.signal}", message=f"the page signal is {page.signal}: a person "
                       "has to take it from here, and the pilot never solves a challenge")
        if page.signal == "logged_out" and not req.login:
            raise Stop(Status.HELD, "signal_logged_out", message="the lane is signed out and this run was not "
                       "asked to sign in, so only a person can sign it back in")

    def _result(self, st: State, s: Stop) -> Result:
        return Result(status=s.status, reason=s.reason, needs=s.needs, message=s.message, answer=s.answer,
                      approval_id=s.approval_id, approval_summary=s.approval_summary, steps=st.n, actions=st.history,
                      readbacks=st.readbacks, escalations=st.escalations, spend=st.spend, effects=st.effects,
                      rows=st.rows)

    @staticmethod
    def _no_answer(e: DeciderFailed) -> Stop:
        if e.kind == BLOCKED:
            return Stop(Status.REFUSED, "secret_in_request", message=e.message)
        if e.kind == DENIED:
            return Stop(Status.REFUSED, "decider_denied", message=e.message)
        if e.kind == BUDGET:
            return Stop(Status.GAVE_UP, "budget", message=e.message)
        return Stop(Status.GAVE_UP, "decider_unavailable", needs=NEEDS_DECIDER, message=e.message)

    # ---- reading --------------------------------------------------------------------------------

    async def _observe(self, req: Request) -> Page:
        try:
            return parse_page(str(await self.tools.call("browser_snapshot", on=req.lane)))
        except ToolFailed as e:
            raise self._cannot_go_on(e) from None

    async def _guarded_look(self) -> None:
        try:
            await self.tools.look()
        except ToolFailed as e:
            raise self._cannot_go_on(e) from None

    @staticmethod
    def _cannot_go_on(e: ToolFailed) -> Stop:
        if e.code in LOST_LANE:
            return Stop(Status.HELD, e.code, message=e.message)
        if e.code in REFUSALS:
            return Stop(Status.REFUSED, e.code, message=e.message)
        return Stop(Status.GAVE_UP, "cannot_read_page", message=f"{e.code}: {e.message}")

    async def _settle(self, req: Request) -> None:
        with contextlib.suppress(ToolFailed):
            await self.tools.call("browser_wait", on=req.lane, network_quiet_ms=300, timeout_s=3)

    async def _files(self, req: Request) -> dict[str, str]:
        if not req.files:
            return {}
        try:
            listed = await self.tools.call("browser_files", on=req.lane)
        except ToolFailed as e:
            raise self._cannot_go_on(e) from None
        inbox = {key: f["id"] for f in listed.get("inbox", []) for key in (f.get("name"), f.get("id")) if key}
        missing = [name for name in req.files if name not in inbox]
        if missing:
            raise Stop(Status.REFUSED, "file_not_in_inbox", needs="the file in the lane's inbox (browser_file_put)",
                       message=f"not in the lane's inbox: {', '.join(missing)}")
        return {name: inbox[name] for name in req.files}

    async def _read(self, req: Request, st: State, page: Page) -> None:
        """A reading run. The answer is a line of the page, picked and never written, but one pick
        with nothing to check it against is not proof, so it ends unverified."""
        lines = {f"l{i}": text for i, text in enumerate(page.lines[:MAX_OPTIONS])}
        unread = len(page.lines) - len(lines)
        if not lines:
            raise Stop(Status.GAVE_UP, "nothing_to_read")
        questions = line_question(req.safe_goal, lines)
        state = {"url": page.url, "outline": page.outline[:OUTLINE_CHARS]}
        answers = await self._ask(req, st, state, questions)
        st.n += 1
        row: dict[str, Any] = {"n": st.n, "op": "READ", **({"dropped": unread} if unread else {})}
        st.rows.append(row)
        step = Step(page, [], [], questions, state, row)
        answer = checked(answers.get("line"), lines)
        row["op_conf"], _ = chance_of(answer)
        if row["op_conf"] < (TARGET_SURE if "probabilities" in answer else MIN_CONFIDENCE):
            answer = await self._escalate(req, st, step, Unsure("low_confidence_read", "line"))
        self.on_step(row)
        raise Stop(Status.DONE_UNVERIFIED, "read_by_one_pick", answer={str(req.read): lines[answer["choice"]]},
                   message=f"only the first {MAX_OPTIONS} lines of the page were read" if unread else None)

    # ---- one step -------------------------------------------------------------------------------

    async def _ask(self, req: Request, st: State, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        reply = await self.decider.ask(state, questions, must_not_send=req.must_not_send)
        self._count(st, "decide", reply)
        return reply.answers

    @staticmethod
    def _count(st: State, job: str, reply: Reply) -> None:
        st.job(job).add(input_tokens=reply.input_tokens, output_tokens=reply.output_tokens, usd=reply.usd,
                        latency_ms=reply.latency_ms)
        Pilot._cost(st, reply.latency_ms, reply.usd, getattr(reply, "model", None))

    @staticmethod
    def _cost(st: State, ms: int, usd: float, model: str | None) -> None:
        cost = st.step_cost
        cost["ms"], cost["usd"] = cost.get("ms", 0) + (ms or 0), cost.get("usd", 0.0) + (usd or 0.0)
        cost["model"] = model or cost.get("model")

    @staticmethod
    def _book(st: State, row: dict[str, Any]) -> None:
        """This step's cost goes on its row, for a person's Technical details."""
        row.update(st.step_cost)
        st.step_cost = {}

    def _seen(self, page: Page, st: State, pending: list[Value]) -> dict[str, Any]:
        return {"url": page.url, "outline": lean(page.outline)[:OUTLINE_CHARS], "actions_taken": st.history[-HISTORY:],
                "last_change": st.last_change, "values_still_to_enter": [v.label for v in pending],
                "supplied_files": list(st.files)}

    def _frame(self, req: Request, st: State, page: Page) -> Step:
        """What one step asks about this page, before anything is sent."""
        pending = [v for v in req.values if v.key not in st.entered]
        elements = offered(page, req.sites, st.flipped, st.filling)
        cut = dropped(elements)
        if cut:  # what is cut could be the right control, and a page can flood the list to cut it
            st.rows.append({"n": st.n + 1, "op": "TOO_MANY", "dropped": cut})
            self.on_step(st.rows[-1])
            raise Stop(Status.NEEDS_HELP, "too_many_options", needs=NEEDS_PERSON,
                       message=f"this page has {cut} more controls than can be offered, so the right one might not be")
        seed = f"{req.seed}:{st.n}"
        questions = build_questions(req.safe_goal, elements, pending=pending, files=list(st.files), seed=seed,
                                    writing=req.may_write and not st.wrote)
        return Step(page, pending, elements, questions, self._seen(page, st, pending), {})

    async def _decide(self, req: Request, st: State, step: Step) -> Plan:
        """The whole decision of one step: every request it takes, and the plan they add up to. It
        touches nothing on the page, which is what lets the lab score it with no browser."""
        answers = await self._ask(req, st, step.state, step.questions)
        st.n += 1
        step.row["n"] = st.n
        st.rows.append(step.row)
        return await self._settled_plan(req, st, step, answers)

    async def _step(self, req: Request, st: State, page: Page) -> Page:
        step = self._frame(req, st, page)
        try:
            plan = await self._decide(req, st, step)
            if plan.op == "DONE":
                await self._finish(req, st, plan, step.pending)
            self.checkpoint()  # a cancel that landed while the model answered
            return await self._do(req, st, step, plan)
        finally:
            if step.row:
                self._book(st, step.row)
                self.on_step(step.row)

    async def _settled_plan(self, req: Request, st: State, step: Step, answers: dict[str, Any]) -> Plan:
        """The plan, sending each unsure question up a level as it comes. An operation goes up on its
        own: the target it needs depends on what comes back, and that target has its own confidence."""
        merged = dict(answers)
        while True:
            try:
                return self._plan(st, step, merged)
            except Unsure as unsure:
                merged[unsure.name] = await self._escalate(req, st, step, unsure)
                step.row["rescued"] = True

    def _plan(self, st: State, step: Step, answers: dict[str, Any]) -> Plan:
        """What the answers add up to. It holds every rule no question can see, and runs again on a
        rescue's picks, so a rescue passes the same gates a decider does."""
        questions, row = step.questions, step.row
        op_answer = checked(answers.get("operation"), questions["operation"]["criteria"])
        op = op_answer["choice"]
        chance, _ = chance_of(op_answer)
        done, owner = noul(answers, "done"), noul(answers, "needs_owner")
        present = noul(answers, "target_present")
        row.update(op=op, op_conf=chance, done=done, needs_owner=owner, present=present)
        if owner >= NEEDS_OWNER:
            raise Stop(Status.HELD, "needs_owner", message="only the account's owner can get past this page")
        if op == "DONE" and done >= MIN_CONFIDENCE:
            return Plan("DONE", agree=True)
        if done >= DONE_SURE:
            return Plan("DONE", why="done_answers_disagree")
        if op == "DONE":
            return Plan("DONE", why="likely_done")
        if op == "WAIT" and (chance < WAIT_SURE or st.waited):
            op = "SCROLL"  # waiting already changed nothing, or the pick was barely preferred
        elif op in OP_GATED and chance < MIN_CONFIDENCE:
            raise Unsure("low_confidence_operation", "operation")
        if op == "CLICK" and present < TARGET_ABSENT and "SCROLL" in questions["operation"]["criteria"]:
            op = "SCROLL"
        if op == "CLICK":
            op = self._click_or_more(answers, questions, op, step)
        row["ran"] = op
        if op == "CLICK":
            return Plan(op, target=self._target(answers, questions, "click_target", row))
        if op == "UPLOAD":
            return Plan(op, target=self._target(answers, questions, "upload_target", row))
        if op == "WRITE":
            return Plan(op, target=self._target(answers, questions, "write_field", row))
        if op == "TYPE":
            chosen = checked(answers.get("type_value"), questions["type_value"]["criteria"])
            value = next(v for v in step.pending if v.key == chosen["choice"])
            row["value"] = value.key
            return Plan(op, target=self._target(answers, questions, f"field_for_{value.key}", row), value=value)
        return Plan(op)

    @staticmethod
    def _click_or_more(answers: dict[str, Any], questions: dict[str, Any], op: str, step: Step) -> str:
        """Two rules no question can see. A click on the control the upload question also picked is
        that upload. A click on a button whose form still has supplied values waiting is too early."""
        pick = (answers.get("click_target") or {}).get("choice")
        if not isinstance(pick, str) or pick not in questions["click_target"]["criteria"]:
            return op
        if "upload_target" in questions and (answers.get("upload_target") or {}).get("choice") == pick:
            return "UPLOAD"
        target = step.page.by_ref(pick)
        waiting = step.pending and "type_value" in questions and target.role == "button" and target.where
        if waiting and any(e.role in TEXT_ROLES and e.where == target.where for e in step.elements):
            return "TYPE"
        return op

    @staticmethod
    def _target(answers: dict[str, Any], questions: dict[str, Any], head: str, row: dict[str, Any]) -> str:
        answer = checked(answers.get(head), questions[head]["criteria"])
        chance, second = chance_of(answer)
        row.update(target=answer["choice"], target_conf=chance, target_second=second)
        sure = TARGET_SURE if "probabilities" in answer else MIN_CONFIDENCE  # a rescue has only its own word
        if chance < sure or chance < TARGET_LEAD * second:
            raise Unsure("low_confidence_target", head)
        return answer["choice"]

    # ---- going up a level -----------------------------------------------------------------------

    async def _escalate(self, req: Request, st: State, step: Step, unsure: Unsure) -> dict[str, Any]:
        """Ask the rescue the same question, over the same options and the same state, and hand back
        a typed answer. It can decline; it cannot add anything. Only a sure pick of an offered option
        comes back, and that then passes the same gates as any other answer."""
        if self.rescue is None:
            st.escalations.append(Escalation(st.n, unsure.reason, "no_rescue"))
            raise Stop(Status.NEEDS_HELP, unsure.reason, needs=NEEDS_RESCUE,
                       message="the pick was too unsure to act on, and no rescue model is set")
        if sum(e.outcome != "no_rescue" for e in st.escalations) >= req.limits.max_escalations:
            raise Stop(Status.NEEDS_HELP, "too_many_escalations", needs=NEEDS_PERSON)
        question = step.questions[unsure.name]
        options = question["criteria"]
        record = Escalation(st.n, unsure.reason, "failed", to=self.rescue.name)
        st.escalations.append(record)
        try:
            got = await self.rescue.pick(question["instructions"], options, step.state, must_not_send=req.must_not_send,
                                         max_usd=max(req.limits.max_usd - st.usd, 0.0))
        except DeciderFailed as e:
            st.job("rescue").add(input_tokens=e.tokens, usd=e.usd)  # a bad answer is still paid for
            record.usd, record.tokens = e.usd, e.tokens
            raise (self._no_answer(e) if e.kind in (BLOCKED, DENIED, BUDGET) else
                   Stop(Status.NEEDS_HELP, "rescue_failed", needs=NEEDS_PERSON, message=e.message)) from None
        self._record(st, record, unsure, got)
        if got.choice is None:
            record.outcome = "abstained"
            raise Stop(Status.NEEDS_HELP, "rescue_abstained", needs=NEEDS_PERSON,
                       message="the rescue model said none of the options is right")
        if got.choice not in options:  # the service refuses this first; nothing rests on that here
            record.outcome = "failed"
            raise Stop(Status.NEEDS_HELP, "rescue_failed", needs=NEEDS_PERSON,
                       message="the rescue model picked something it was not offered")
        if got.confidence < MIN_CONFIDENCE:
            record.outcome = "unsure"
            raise Stop(Status.NEEDS_HELP, unsure.reason, needs=NEEDS_PERSON,
                       message="the rescue model was not sure either")
        return {"type": "choice", "choice": got.choice, "confidence": got.confidence}

    @staticmethod
    def _record(st: State, record: Escalation, unsure: Unsure, got: Pick) -> None:
        st.job("rescue").add(input_tokens=got.tokens, usd=got.usd, latency_ms=got.latency_ms)
        Pilot._cost(st, got.latency_ms, got.usd, got.model)
        record.outcome, record.to = "picked", got.model or record.to
        record.picked = {unsure.name: got.choice} if got.choice is not None else {}
        record.confidence, record.usd = got.confidence, got.usd
        record.tokens, record.latency_ms = got.tokens, got.latency_ms

    # ---- acting ---------------------------------------------------------------------------------

    async def _finish(self, req: Request, st: State, plan: Plan, pending: list[Value]) -> None:
        """How it ended, as far as anyone can know. The two done answers must agree, and what the
        goal was handed must be used, and the node must have read back what the last action did."""
        await self._settle(req)
        await self.sleep(SETTLE_S)
        if not plan.agree:
            raise Stop(Status.DONE_UNVERIFIED, plan.why or "likely_done",
                       message="the two done answers disagree, so check the result yourself")
        for why, left in (("values_not_entered", pending), ("files_not_attached", st.files)):
            if left:
                raise Stop(Status.DONE_UNVERIFIED, why, message="it looks finished, but not everything supplied was used")
        if st.doubt:
            raise Stop(Status.DONE_UNVERIFIED, "last_action_refused",
                       message=f"it looks finished, but something was refused since the last action ({st.doubt})")
        if not st.readbacks:
            raise Stop(Status.DONE_UNVERIFIED, "nothing_read_back", message="it looks finished, but no action was taken")
        if not st.readbacks[-1]["passed"]:
            raise Stop(Status.DONE_UNVERIFIED, "last_action_not_seen",
                       message="it looks finished, but the last action changed nothing the node could read")
        if not st.readbacks[-1]["confirmed"]:
            raise Stop(Status.DONE_UNVERIFIED, "only_the_page_changed",
                       message="it looks finished, but all the node saw is that the page changed")
        # a toggle that flipped, words typed or written, or a file attached on an item the request does
        # not name; a plain click there (a "more" link) moves nothing of anyone's
        astray = next((r for r in reversed(st.readbacks) if r.get("anchored") is False
                       and (r["confirmed"] or r.get("op") != "CLICK" or r is st.readbacks[-1])), None)
        if astray is not None:
            raise Stop(Status.DONE_UNVERIFIED, "target_not_in_goal",
                       message=f"the action landed, but it may be on the wrong item: {astray['anchor']}")
        raise Stop(Status.DONE_VERIFIED, "both_done_answers_agree")

    def _acted(self, st: State, plan: Plan, element: Element | None, result: Any) -> None:
        if isinstance(result, dict) and result.get("status") == "approval_pending":
            st.held = Held(plan, element.key if element else None)
            raise Stop(Status.WAITING_APPROVAL, "approval_pending", approval_id=result.get("approval_id"),
                       approval_summary=result.get("summary"))
        blocks = result.get("egress_blocked") if isinstance(result, dict) else None
        if blocks:
            st.doubt = "egress_blocked"
            hard = next((b for b in blocks if b.get("rule") in HARD_STOPS), None)
            if hard is not None:  # the meter refuses on the route, so the click itself came back ok
                raise ToolFailed(hard["rule"], str(hard.get("reason") or ""))

    async def _enter(self, req: Request, st: State, plan: Plan, element: Element | None, entry: dict[str, Any]) -> Any:
        """A supplied value into a field. A vault value is filled by the node from its entry, by
        name: its text is never here. Anything else is typed through browser_act."""
        value = plan.value
        assert value is not None
        entry["value"] = value.label
        if value.vault is None:
            result = await self.tools.call("browser_act", on=req.lane, ref=plan.target, action=ActionKind.FILL,
                                           text=value.text)
            self._acted(st, plan, element, result)
            return result
        result = await self.tools.call("vault_fill", on=req.lane, field=plan.target, entry=value.vault)
        self._acted(st, plan, element, result)
        if isinstance(result, dict) and not result.get("ok"):
            raise Stop(Status.NEEDS_HELP, "vault_fill_failed", needs=NEEDS_VAULT, message=f"needs {result.get('needs')}")
        return result

    async def _write(self, req: Request, st: State, plan: Plan, page: Page, element: Element | None) -> Plan:
        if plan.text is not None:
            return plan
        if self.writer is None:
            raise Stop(Status.NEEDS_HELP, "needs_text", needs=NEEDS_TEXT,
                       message="this step needs words and none were supplied")
        field_label = element.label if element else "a text field"
        try:
            out = await self.writer.write(goal=req.safe_goal, field=field_label, lines=page.lines,
                                          must_not_send=req.must_not_send)
        except DeciderFailed as e:
            raise (self._no_answer(e) if e.kind in (BLOCKED, DENIED, BUDGET) else
                   Stop(Status.NEEDS_HELP, "write_failed", needs=NEEDS_TEXT, message=e.message)) from None
        st.job("write").add(input_tokens=out.tokens, usd=out.usd, latency_ms=out.latency_ms)
        self._cost(st, out.latency_ms, out.usd, getattr(out, "model", None))
        return replace(plan, text=out.text)

    async def _do(self, req: Request, st: State, step: Step, plan: Plan) -> Page:
        page, row, lane = step.page, step.row, req.lane
        entry: dict[str, Any] = {"operation": plan.op}
        element = page.by_ref(plan.target) if plan.target else None
        if element is not None:
            entry["element"] = row["element"] = element.label
        worked = confirmed = False
        if plan.op in EFFECT_OPS:
            st.doubt = None
        typed = tuple(v.text for v in req.values if v.text and not v.secret)
        anchored = anchor.check(req.safe_goal, req.target, page, element, typed) if plan.op in EFFECT_OPS else None
        try:
            if plan.op == "CLICK":
                result = await self.tools.call("browser_act", on=lane, ref=plan.target, action=ActionKind.CLICK)
                self._acted(st, plan, element, result)
                worked = True
                st.filling = None
                if isinstance(result, dict):
                    entry.update(changed=result.get("changed"), detail=result.get("detail"))
                    confirmed = result.get("changed") is True
                    if confirmed and element is not None:
                        st.flipped.add(element.key)
            elif plan.op == "TYPE":
                await self._enter(req, st, plan, element, entry)
                assert plan.value is not None
                st.entered.add(plan.value.key)  # after the action: a covered field keeps its value waiting
                st.filling = element.where if element else None
                worked = True
            elif plan.op == "WRITE":
                plan = await self._write(req, st, plan, page, element)
                self.checkpoint()  # writing takes seconds
                entry["written"] = plan.text
                result = await self.tools.call("browser_act", on=lane, ref=plan.target, action=ActionKind.FILL,
                                               text=plan.text)
                self._acted(st, plan, element, result)
                st.wrote = worked = True
                st.filling = element.where if element else None
            elif plan.op == "UPLOAD":
                name = next(iter(st.files))
                result = await self.tools.call("browser_upload", on=lane, ref=plan.target, file_id=st.files[name])
                self._acted(st, plan, element, result)
                st.files.pop(name)  # attached: offering it again only invites a second upload
                worked = True
            elif plan.op == "SCROLL":
                self._acted(st, plan, element, await self.tools.call("browser_scroll", on=lane))
            else:  # WAIT
                await self.sleep(1.0)
        except ToolFailed as e:
            if e.code in LOST_LANE:
                raise self._cannot_go_on(e) from None
            row["error"] = e.code
            entry["refused"] = e.code
            if e.code in REFUSALS:
                st.refused = e.code
            if e.code in REFUSALS or plan.op in EFFECT_OPS:
                st.doubt = e.code
            if e.code in HARD_STOPS:
                st.history.append(entry)
                raise Stop(Status.REFUSED, e.code, message=e.message) from None
        st.waited = plan.op == "WAIT"
        return await self._after(req, st, page, row, entry, worked=worked, confirmed=confirmed, anchored=anchored)

    async def _after(self, req: Request, st: State, before: Page, row: dict[str, Any], entry: dict[str, Any], *,
                     worked: bool, confirmed: bool, anchored: dict[str, Any] | None = None) -> Page:
        st.history.append(entry)
        if worked:  # first: a person can take the lane right now, and what landed must still count
            st.effects += 1
        await self._settle(req)
        page = await self._observe(req)
        moved = urlsplit(page.url).path != urlsplit(before.url).path
        if moved:  # a new page gets its look, as in a flow
            await self._guarded_look()
            page = await self._observe(req)
        delta = outline_delta(before.body, page.body)
        changed = delta is None or not delta.empty
        st.last_change = "most of the page changed" if delta is None else delta.text[:1500] or None
        entry["page_changed"] = row["page_changed"] = changed
        if worked:
            st.readbacks.append({"what": entry.get("element") or entry["operation"], "passed": confirmed or changed,
                                 "confirmed": confirmed,
                                 "how": "the target's own state" if confirmed else "the page after the action",
                                 "op": entry["operation"],
                                 "anchored": anchored["anchored"] if anchored else None,
                                 "anchor": anchored["by"] if anchored else None})
        st.stale = 0 if changed else st.stale + 1
        if changed:
            st.refused = None
        if st.stale >= req.limits.no_progress:
            raise Stop(Status.REFUSED if st.refused else Status.GAVE_UP, "no_progress",
                       message=f"nothing moved in {st.stale} steps" + (f"; the last refusal was {st.refused}"
                                                                       if st.refused else ""))
        return page

    async def _resume(self, req: Request, st: State, page: Page) -> Page:
        """A person answered. Run the same action again: it goes only if it sends what they
        approved. Refs do not outlive a wait, so the element is found again by what it is."""
        held = st.held
        assert held is not None
        st.held = None
        if page.signal in CHALLENGES:
            raise Stop(Status.HELD, f"signal_{page.signal}")
        self._signals(req, page)  # a lane signed out while the person decided is a person's too
        element = page.by_key(held.key) if held.key is not None else None
        if held.key is not None and element is None:
            raise Stop(Status.GAVE_UP, "approved_target_gone",
                       message="the control the approval was for is no longer on the page; nothing was sent")
        if held.key is not None and sum(e.key == held.key for e in page.elements) > 1:
            raise Stop(Status.GAVE_UP, "approved_target_ambiguous",
                       message="more than one control on the page looks like the one the approval was for, so "
                               "nothing was sent")
        row: dict[str, Any] = {"n": st.n, "op": "RESUME", "ran": held.plan.op}
        st.rows.append(row)
        plan = replace(held.plan, target=element.ref) if element is not None else held.plan
        step = Step(page, [], [], {}, {}, row)
        try:
            return await self._do(req, st, step, plan)
        finally:
            self.on_step(row)
