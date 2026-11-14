"""What a workflow is made of. The shape is `RoutineDefinition` in the panel (docs/WORKFLOWS.md).

A definition has no field for who may run it, what it may do on its own or which switch applies:
unknown fields are refused, so a definition cannot carry a claim about its own safety. Whether a
value is trusted is worked out by the node from where it came from (`check.py`), never read from here.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentos.core.models import Effect, utcnow
from agentos.runner.models import FLOW_ID_RE, Locator

MAX_STEPS = 100
MAX_LOOP_DEPTH = 2
MAX_BRANCH_DEPTH = 8
MAX_LOOP_ITEMS = 1000
MAX_ROW_ACTS = 100  # acts one run may do inside loops over a site's rows, however many loops or steps
MAX_WAIT_DAYS = 90
MIN_WATCH_MINUTES = 5
MAX_WATCH_MINUTES = 7 * 1440
STEP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
TRIGGER = "trigger"  # what a reference calls the trigger, and so never a step id
RESERVED_SOURCES = frozenset({TRIGGER, "person"})  # `person`: who a send step's message is for
REF = r"\{\{\s*([\w-]+)\.([\w-]+)\s*\}\}"
REF_RE = re.compile(REF)
WHOLE_REF_RE = re.compile(rf"^\s*{REF}\s*$")
TAG_PREFIX = "tag:"
TriggerKind = Literal["schedule", "webhook", "email", "page_change", "site_change", "manual"]
Compare = Literal["is", "is_not", "contains", "more_than", "less_than", "is_empty", "not_empty"]
UNIT_SECONDS = {"minutes": 60, "hours": 3600, "days": 86_400}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WatchOptions(_Strict):
    locator: Locator | None = None  # what to read on the page; the whole page when left out
    read: Literal["text", "count"] = "text"
    every_s: int = Field(default=300, ge=30, le=86_400)


class Trigger(_Strict):
    kind: TriggerKind
    label: str | None = Field(default=None, max_length=200)
    cron: str | None = Field(default=None, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)
    lane: str | None = Field(default=None, max_length=73)  # the mailbox, or the account a page is read from
    from_: str | None = Field(default=None, alias="from", max_length=200)
    subject: str | None = Field(default=None, max_length=200)
    unseen_only: bool | None = None  # left out, not false, so a definition saved before it keeps its hash
    url: str | None = Field(default=None, max_length=2000)
    watch: WatchOptions | None = None
    operation: str | None = Field(default=None, max_length=300)  # a site_change: `<endpoint>@<surface>`
    params: dict[str, Any] | None = None
    list: str | None = Field(default=None, max_length=60)  # which output is the rows
    key: str | None = Field(default=None, max_length=60)   # which field of a row says which row it is
    every_minutes: int | None = None
    hook: dict[str, Any] | None = None  # filled by the node and the cloud; ignored when saved

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Condition(_Strict):
    left: str | None = Field(default=None, max_length=120)
    op: Compare = "is"
    right: str = Field(default="", max_length=500)


class Send(_Strict):
    list: str | None = Field(default=None, max_length=120)
    to: str | None = Field(default=None, max_length=120)


class _Step(_Strict):
    id: str
    label: str = Field(default="", max_length=200)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not STEP_ID_RE.match(v) or v in RESERVED_SOURCES:
            raise ValueError(f"a step id is 1 to 40 letters, digits, _ and -, and not 'trigger' or 'person': {v!r}")
        return v


class ActionStep(_Step):
    kind: Literal["action"] = "action"
    operation: str | None = Field(default=None, max_length=300)
    inputs: dict[str, Any] = Field(default_factory=dict)
    send: Send | None = None


class BranchStep(_Step):
    kind: Literal["branch"] = "branch"
    when: Condition = Field(default_factory=Condition)
    then: list[Step] = Field(default_factory=list)
    otherwise: list[Step] = Field(default_factory=list)


class WaitStep(_Step):
    kind: Literal["wait"] = "wait"
    amount: float = Field(default=0, ge=0, allow_inf_nan=False)
    unit: Literal["minutes", "hours", "days"] = "minutes"

    @property
    def seconds(self) -> float:
        return self.amount * UNIT_SECONDS[self.unit]


class LoopStep(_Step):
    kind: Literal["loop"] = "loop"
    over: str | None = Field(default=None, max_length=120)
    item: str = Field(default="item", max_length=60)
    body: list[Step] = Field(default_factory=list)


class AskStep(_Step):
    kind: Literal["ask"] = "ask"
    question: str = Field(default="", max_length=2000)
    on_no: Literal["stop", "continue"] = "stop"


class NotifyStep(_Step):
    kind: Literal["notify"] = "notify"
    message: str = Field(default="", max_length=2000)


class StopIfRepliedStep(_Step):
    """Stops chasing whoever answered what `after` sent. It can only ever send less."""
    kind: Literal["stop_if_replied"] = "stop_if_replied"
    after: str | None = Field(default=None, max_length=40)  # the mail.send step whose message it looks at


Step = Annotated[ActionStep | BranchStep | WaitStep | LoopStep | AskStep | NotifyStep | StopIfRepliedStep,
                 Field(discriminator="kind")]
BranchStep.model_rebuild()
LoopStep.model_rebuild()


class WorkflowDefinition(_Strict):
    id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    workspace: str | None = Field(default=None, max_length=31)
    trigger: Trigger
    steps: list[Step] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _flow_id(cls, v: str | None) -> str | None:
        if v is not None and not FLOW_ID_RE.match(v):
            raise ValueError("a workflow id looks like a flow id: 'harbor.routine.invoice-reminders'")
        return v

    def canonical(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        data["trigger"].pop("hook", None)  # the node's and the cloud's, never part of what was written
        return data

    def content_hash(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


class WorkflowVersion(BaseModel):
    definition: WorkflowDefinition
    version: str
    revision: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    saved_by: dict[str, Any] = Field(default_factory=dict)  # kind, id, owner, grant_id, person

    @property
    def id(self) -> str:
        return self.definition.id or ""

    @property
    def by_person(self) -> bool:
        return bool(self.saved_by.get("person"))


class WorkflowState(BaseModel):
    """What the node keeps beside a workflow's versions: where its head is, what runs, and the
    route and schedule that start it."""
    id: str
    head: str
    live: str | None = None     # the version a trigger runs: one a person turned on or saved
    enabled: bool = False
    revision: int = 1
    route_id: str | None = None
    schedule_id: str | None = None
    wired: str | None = None    # the trigger the route and schedule were made for
    secret_hint: str | None = None
    proposal: dict[str, Any] | None = None  # an agent asked a person to turn it on: the card and the version
    last_received_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utcnow)


def walk(steps: list[Any], depth: int = 0) -> Any:
    """Every step, parents first, with how many loops and branches surround it."""
    for step in steps:
        yield step
        if isinstance(step, BranchStep):
            yield from walk(step.then, depth)
            yield from walk(step.otherwise, depth)
        elif isinstance(step, LoopStep):
            yield from walk(step.body, depth)


def strongest(effects: list[Effect]) -> Effect:
    """The strongest effect on the ladder, for a workflow's `effect`."""
    return max(effects, key=lambda e: e.rank if e.rank is not None else -1, default=Effect.READ)
