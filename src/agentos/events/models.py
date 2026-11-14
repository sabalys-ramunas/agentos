"""Events, routes and deliveries (docs/design/always-on-agents.md sections 4.3.2 to 4.3.6).

An Event is something that happened, written down by the node: where (workspace, lane),
when, a dedupe key from its source, small typed `facts` the node itself observed (safe to
show and to put in a flow's params), and optional `content` that someone outside wrote (a
comment, a webhook body). Content is untrusted unless the owner wrote it, is carried only in
a fixed frame, and never fills a flow param.

A Route is the owner's standing authority: "when this happens, wake that, with this
instruction, under this grant, within these limits". The panel calls it a trigger. Only the
owner writes routes; an agent may propose one, which becomes an approval card.

A Delivery is one target invocation for one route: one or more events (a batch, a digest)
with a state and a reason for every decision the dispatcher made about it.
"""

from __future__ import annotations

import re
from datetime import datetime, time
from enum import StrEnum
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentos.agents.models import FLOW_ID_RE, ladder_effect, lane_in_workspace
from agentos.core.models import Effect, parse_lane_id, utcnow

EVENT_ID_RE = re.compile(r"^ev_[0-9a-f]{12}$")
ROUTE_ID_RE = re.compile(r"^rt_[0-9a-f]{12}$")
DELIVERY_ID_RE = re.compile(r"^dl_[0-9a-f]{12}$")
WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
KIND_RE = re.compile(r"^[a-z]+(\.[a-z_]+){1,3}$")
FACT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PARAM_TEMPLATE_RE = re.compile(r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*\}")

SOURCES = ("cron", "watch", "webhook", "approval", "run", "lane", "agent", "owner", "mail", "queue")
Source = Literal["cron", "watch", "webhook", "approval", "run", "lane", "agent", "owner", "mail", "queue"]
TaintLabel = Literal["untrusted", "private", "owner"]
Priority = Literal["urgent", "normal", "low"]
PRIORITY_RANK = {"urgent": 0, "normal": 2, "low": 3}  # approvals decided sit at 1 (section 4.3.6, item 10)

MAX_FACTS = 40
MAX_FACT_STR = 500
MAX_CONTENT = 20_000
MAX_DEPTH = 3          # a cause chain deeper than this is dropped (loop guard)
DEFAULT_TZ = "Europe/Vilnius"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown timezone {name!r}") from e


def default_taint(source: str, *, has_content: bool) -> list[str]:
    """Labels by origin (section 4.4.3): node facts carry none, the owner's words carry
    `owner`, anything someone else wrote carries `untrusted`. Mail adds `private`."""
    if source == "owner":
        return ["owner"]
    if source == "mail":
        return ["private", "untrusted"]
    if source == "agent" or has_content:
        return ["untrusted"]  # turn.finished text is model output
    return []


# ---------------------------------------------------------------- events


class Content(_Strict):
    """What someone outside wrote. Untrusted unless the event's source is the owner."""
    text: str
    origin: str | None = Field(default=None, max_length=300)  # where it came from: a URL, "webhook body"
    truncated: bool = False
    fields: dict[str, str] | None = None  # a mail's sender and subject: as untrusted as the text

    @model_validator(mode="before")
    @classmethod
    def _clip(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("text"), str) and len(data["text"]) > MAX_CONTENT:
            data = {**data, "text": data["text"][:MAX_CONTENT], "truncated": True}
        return data


class Cause(_Strict):
    """Why this event happened, for the loop guard: the event or turn behind it and how deep
    the chain is (an event no agent or route caused has depth 0)."""
    event: str | None = None
    turn: str | None = None
    route: str | None = None   # the route whose delivery caused it (self-cause guard)
    lane: str | None = None    # the target lane of that delivery
    depth: int = Field(default=0, ge=0, le=100)


class Event(_Strict):
    id: str
    source: Source
    kind: str
    workspace: str
    lane: str | None = None
    occurred_at: datetime = Field(default_factory=utcnow)
    received_at: datetime = Field(default_factory=utcnow)
    dedupe_key: str = Field(min_length=1, max_length=300)
    facts: dict[str, str | int | float | bool] = Field(default_factory=dict)
    content: Content | None = None
    taint: list[TaintLabel] = Field(default_factory=list)
    cause: Cause = Field(default_factory=Cause)
    priority: Priority = "normal"

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not EVENT_ID_RE.match(v):
            raise ValueError("an event id looks like ev_<12 hex>")
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if not KIND_RE.match(v):
            raise ValueError(f"an event kind looks like 'watch.changed': {v!r}")
        return v

    @field_validator("workspace")
    @classmethod
    def _ws(cls, v: str) -> str:
        if not WORKSPACE_RE.match(v):
            raise ValueError(f"workspace ids are lowercase letters, digits and dashes: {v!r}")
        return v

    @field_validator("facts")
    @classmethod
    def _facts(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > MAX_FACTS:
            raise ValueError(f"an event carries at most {MAX_FACTS} facts")
        out = {}
        for k, val in v.items():
            if not FACT_KEY_RE.match(k):
                raise ValueError(f"fact names are lowercase_with_underscores: {k!r}")
            if isinstance(val, str):
                val = " ".join(val.split())[:MAX_FACT_STR]  # one line: facts never carry a frame break
            out[k] = val
        return out

    @model_validator(mode="after")
    def _check(self) -> Event:
        if self.lane is not None:
            ws, _ = parse_lane_id(self.lane)
            if ws != self.workspace:
                raise ValueError(f"lane {self.lane} is not in workspace {self.workspace}")
        if not self.kind.startswith(self.source + ".") and self.source != "agent":  # agent: turn.finished
            raise ValueError(f"a {self.source} event's kind starts with '{self.source}.', not {self.kind!r}")
        labels = set(self.taint) | set(default_taint(self.source, has_content=self.content is not None))
        if self.source != "owner" and self.content is not None:
            labels.discard("owner")  # someone else wrote it, whatever the source claims
            labels.add("untrusted")
        self.taint = sorted(labels)
        return self

    @property
    def subject(self) -> str | None:
        """What dedupe_window_s compares: `facts.subject` when the source sets one."""
        s = self.facts.get("subject")
        return str(s) if s is not None else None


# ---------------------------------------------------------------- routes


class Match(_Strict):
    source: Source
    kind: str | None = None                      # exact, or a prefix ending in ".*"
    workspace: str
    lane: str | None = None                      # a lane id or a pattern inside the workspace
    where: dict[str, str | int | float | bool] = Field(default_factory=dict)  # equality on facts
    schedule_id: str | None = None               # only events from this schedule (Schedule.route_id)

    @field_validator("workspace")
    @classmethod
    def _ws(cls, v: str) -> str:
        if not WORKSPACE_RE.match(v):
            raise ValueError(f"workspace ids are lowercase letters, digits and dashes: {v!r}")
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str | None) -> str | None:
        if v is not None and not (KIND_RE.match(v) or KIND_RE.match(v.removesuffix(".*") + ".x")):
            raise ValueError(f"match.kind is an event kind like 'watch.changed', or 'watch.*': {v!r}")
        return v

    @field_validator("where")
    @classmethod
    def _where(cls, v: dict[str, Any]) -> dict[str, Any]:
        for k in v:
            if not FACT_KEY_RE.match(k):
                raise ValueError(f"match.where names facts, which are lowercase_with_underscores: {k!r}")
        return v

    @model_validator(mode="after")
    def _lane(self) -> Match:
        if self.lane is not None:
            lane_in_workspace(self.lane, self.workspace, "match.lane")
        return self


class FlowTarget(_Strict):
    kind: Literal["flow"] = "flow"
    flow: str
    version: str | None = None
    on: str | None = None                         # the lane, when the flow's lane is a pattern
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)  # strings may use ${facts.x}

    @field_validator("flow")
    @classmethod
    def _flow(cls, v: str) -> str:
        if not FLOW_ID_RE.match(v):
            raise ValueError(f"flow ids are dotted lowercase segments like 'biz-a.instagram.post': {v!r}")
        return v

    @field_validator("params")
    @classmethod
    def _params(cls, v: dict[str, Any]) -> dict[str, Any]:
        for name, val in v.items():
            if not isinstance(val, str):
                continue
            for m in PARAM_TEMPLATE_RE.finditer(val):
                path = m.group(1).split(".")
                if path[0] != "facts" or len(path) != 2:
                    raise ValueError(f"param {name}: a trigger fills params from ${{facts.<name>}} only, never "
                                     f"from content someone else wrote (found ${{{m.group(1)}}})")
        return v


class BrainTarget(_Strict):
    kind: Literal["brain"] = "brain"
    brain: str | None = Field(default=None, max_length=64)  # pin a brain; None lets the router pick


class AgentTarget(_Strict):
    kind: Literal["agent"] = "agent"
    lane: str
    session: Literal["new", "main", "per-route-day"] = "new"   # open question 2

    @field_validator("lane")
    @classmethod
    def _lane(cls, v: str) -> str:
        parse_lane_id(v)
        return v


class WorkflowTarget(_Strict):
    """Starts a workflow's live version (agentos.runner.workflows). Its steps authorize themselves."""
    kind: Literal["workflow"] = "workflow"
    workflow: str

    @field_validator("workflow")
    @classmethod
    def _workflow(cls, v: str) -> str:
        if not FLOW_ID_RE.match(v):
            raise ValueError(f"workflow ids are dotted lowercase segments: {v!r}")
        return v


Target = Annotated[FlowTarget | BrainTarget | AgentTarget | WorkflowTarget, Field(discriminator="kind")]


class RouteBudget(_Strict):
    turns: int | None = Field(default=None, ge=1, le=500)
    minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    tokens: int | None = Field(default=None, ge=1)
    usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class RouteGrant(_Strict):
    """What a delivery's grant is narrowed to (for brain and agent targets): never wider than
    the route creator's grant, checked when the route is saved and again at delivery."""
    ceiling: Effect = Effect.DRAFT                # open question 3
    lanes: list[str] = Field(default_factory=list)
    flows: list[str] = Field(default_factory=list)
    ttl_s: int = Field(default=900, ge=60, le=86_400)
    budget: RouteBudget = Field(default_factory=RouteBudget)

    @field_validator("ceiling")
    @classmethod
    def _ceiling(cls, v: Effect) -> Effect:
        return ladder_effect(v)

    @field_validator("flows")
    @classmethod
    def _flows(cls, v: list[str]) -> list[str]:
        for f in v:
            if not FLOW_ID_RE.match(f):
                raise ValueError(f"flow ids are dotted lowercase segments: {f!r}")
        return v


class QuietHours(_Strict):
    tz: str = DEFAULT_TZ
    start: str = Field(default="22:00", alias="from")
    end: str = Field(default="08:00", alias="to")
    mode: Literal["delay", "digest", "drop"] = "delay"

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("tz")
    @classmethod
    def _tz(cls, v: str) -> str:
        _zone(v)
        return v

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not HHMM_RE.match(v):
            raise ValueError(f"quiet hours are local times like 22:00: {v!r}")
        return v

    @model_validator(mode="after")
    def _span(self) -> QuietHours:
        if self.start == self.end:
            raise ValueError("quiet hours need a start and an end that differ")
        return self

    @staticmethod
    def _t(v: str) -> time:
        h, m = v.split(":")
        return time(int(h), int(m))

    def window_end(self, now: datetime) -> datetime | None:
        """When the quiet window that contains `now` ends (an aware datetime), or None if
        `now` is outside it. A window may cross midnight (22:00 to 08:00)."""
        from datetime import timedelta

        tz = _zone(self.tz)
        local = now.astimezone(tz)
        start, end, t = self._t(self.start), self._t(self.end), local.time().replace(tzinfo=None)
        inside = (start <= t < end) if start < end else (t >= start or t < end)
        if not inside:
            return None
        day = local.date()
        if not start < end and t >= start:
            day = day + timedelta(days=1)
        naive = datetime.combine(day, end)
        return naive.replace(tzinfo=tz, fold=0)

    def public(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


class Rate(_Strict):
    per_hour: int | None = Field(default=None, ge=1, le=10_000)
    per_day: int | None = Field(default=None, ge=1, le=100_000)


class Batch(_Strict):
    window_s: int = Field(ge=1, le=86_400)
    max_items: int = Field(default=10, ge=1, le=200)


class Controls(_Strict):
    dedupe_window_s: int | None = Field(default=None, ge=1, le=7 * 86_400)
    rate: Rate | None = None
    quiet_hours: QuietHours | None = None        # None: the node default (22:00 to 08:00 Vilnius, delay)
    batch: Batch | None = None
    concurrency: int = Field(default=1, ge=1, le=20)
    max_attempts: int = Field(default=3, ge=1, le=10)
    allow_self: bool = False                      # may fire on events caused by its own target
    max_wait_s: int = Field(default=6 * 3600, ge=60, le=30 * 86_400)


class RouteState(_Strict):
    """The breaker's mark on a route (or the owner's own pause). Only the owner resumes."""
    paused: bool = False
    reason: str | None = None
    at: datetime | None = None
    failures_in_row: int = 0


class Route(_Strict):
    id: str
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    match: Match
    target: Target
    instruction: str | None = Field(default=None, max_length=8000)  # owner-written, shown verbatim
    grant: RouteGrant = Field(default_factory=RouteGrant)
    controls: Controls = Field(default_factory=Controls)
    priority: Priority = "normal"
    created_by: str                                # the creator's grant id; agents cannot create routes
    principal: dict[str, Any] = Field(default_factory=dict)  # the creator (the owner)
    proposed_by: dict[str, Any] | None = None      # the agent that proposed it, when it came from trigger_propose
    state: RouteState = Field(default_factory=RouteState)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ROUTE_ID_RE.match(v):
            raise ValueError("a route id looks like rt_<12 hex>")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("a trigger needs a name")
        return v

    @model_validator(mode="after")
    def _check(self) -> Route:
        ws = self.match.workspace
        t = self.target
        if isinstance(t, AgentTarget):
            lane_in_workspace(t.lane, ws, "the target agent lane")
        if isinstance(t, FlowTarget) and t.on is not None:
            lane_in_workspace(t.on, ws, "the flow's lane")
        for lane in self.grant.lanes:
            lane_in_workspace(lane, ws, "grant lane")
        if isinstance(t, BrainTarget | AgentTarget) and not (self.instruction or "").strip():
            raise ValueError("a trigger that wakes a brain or an agent needs the owner's instruction")
        if isinstance(t, FlowTarget) and self.instruction:
            raise ValueError("a flow trigger takes params, not an instruction (a flow runs with no model)")
        if isinstance(t, BrainTarget) and not self.grant.lanes:
            raise ValueError("a brain trigger names the lanes its grant reaches (grant.lanes)")
        return self

    @property
    def workspace(self) -> str:
        return self.match.workspace

    @property
    def target_lane(self) -> str | None:
        """The one lane the target acts in, when there is one (agent lanes, flows on a lane)."""
        t = self.target
        if isinstance(t, AgentTarget):
            return t.lane
        if isinstance(t, FlowTarget):
            return t.on
        return None

    def public(self) -> dict[str, Any]:
        d = self.model_dump(mode="json", by_alias=True)
        return d


# ---------------------------------------------------------------- deliveries


class DeliveryState(StrEnum):
    PENDING = "pending"        # created; due at due_at (a batch window may still be open)
    WAITING = "waiting"        # a control holds it, with the reason (quiet hours, rate, lane held, ...)
    DELIVERED = "delivered"    # handed to its target; waiting for the outcome
    DONE = "done"              # the target finished (its own status says how well)
    FAILED = "failed"          # the target refused for good (grant revoked, flow gone); Needs you
    DROPPED = "dropped"        # a control dropped it, with the reason
    DEAD = "dead"              # attempts exhausted: a dead letter until the owner looks

    @property
    def terminal(self) -> bool:
        return self in (DeliveryState.DONE, DeliveryState.FAILED, DeliveryState.DROPPED, DeliveryState.DEAD)


OPEN_STATES = (DeliveryState.PENDING, DeliveryState.WAITING)


class Delivery(_Strict):
    id: str
    route_id: str
    event_ids: list[str] = Field(min_length=1)    # oldest first; more than one for a batch or digest
    key: str                                      # "<route>:<first event>": what targets dedupe on
    state: DeliveryState = DeliveryState.PENDING
    reason: str | None = None
    due_at: datetime = Field(default_factory=utcnow)
    open_until: datetime | None = None            # a batch or digest still takes items until then
    max_items: int = 1
    priority: Priority = "normal"
    rank: int = 2                                 # PRIORITY_RANK, with approvals decided at 1
    subject: str | None = None
    attempts: int = 0
    target_kind: Literal["flow", "brain", "agent", "workflow"]
    target_ref: str | None = None                 # run id, brain task id or turn id
    grant_id: str | None = None                   # the grant minted for this delivery (brain targets)
    outcome: str | None = None                    # the target's own status: succeeded_verified, failed, ...
    notified: list[str] = Field(default_factory=list)  # incidents already pushed for this delivery
    history: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    delivered_at: datetime | None = None
    finished_at: datetime | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not DELIVERY_ID_RE.match(v):
            raise ValueError("a delivery id looks like dl_<12 hex>")
        return v

    def note(self, at: datetime, state: DeliveryState, reason: str | None) -> None:
        """Move to `state` and record why (the last 30 decisions are kept)."""
        self.state, self.reason, self.updated_at = state, reason, at
        self.history.append({"at": at.isoformat(), "state": state.value, "reason": reason})
        del self.history[:-30]
