"""What the runner stores: runs, their step records, and schedules.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    model_validator,
)

from openajar.core.locator import Locator, StrictModel
from openajar.core.models import RunKind, RunStatus, utcnow

TERMINAL = frozenset({RunStatus.SUCCEEDED_VERIFIED, RunStatus.SUCCEEDED_UNVERIFIED, RunStatus.PARTIAL,
                      RunStatus.FAILED, RunStatus.CANCELLED})
ACTIVE = frozenset({RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL})
SUCCEEDED = frozenset({RunStatus.SUCCEEDED_VERIFIED, RunStatus.SUCCEEDED_UNVERIFIED})


class Run(BaseModel):
    id: str
    kind: RunKind = RunKind.FLOW
    lane: str | None = None
    # a connector run: the AUD-01 surface it acts as. Grants, approvals and pace see the surface;
    # the lane is the login it runs on (the surface's binding), held one run at a time
    surface: str | None = None
    flow_id: str | None = None
    flow_version: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus = RunStatus.QUEUED
    attempts: int = 0
    deferrals: int = 0
    held: int = 0  # deferrals because a human held the lane (never count toward giving up)
    idempotency_key: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    not_before: datetime | None = None
    start_after: datetime | None = None  # the earliest start the caller asked for (flow_run not_before)
    parked_at: datetime | None = None
    proof: dict[str, Any] | None = None
    # a connector run's typed result: {data, untrusted_fields, private_fields, truncated, source}
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    cost: dict[str, Any] = Field(default_factory=lambda: {"tokens": 0, "usd": 0.0, "ms": 0})
    parent: str | None = None
    principal: dict[str, Any] = Field(default_factory=dict)
    grant_id: str | None = None  # the grant that created the run, re-checked before it runs
    source: str = "tool"
    schedule_id: str | None = None
    trigger: dict[str, Any] | None = None
    approval_id: str | None = None
    authorized: bool = False
    auto: bool = False  # authorized because nothing asked ("Ask me first" off), not by a person or a standing approval
    pace_id: int | None = None  # its row on the lane's pace ledger (openajar.policy.pace), once it holds one
    decision: dict[str, Any] | None = None  # {"needed": {...}} or {"choice": ..., "path": ...}
    recheck_at: datetime | None = None
    rechecks: int = 0
    cancel_requested: bool = False
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def workspace(self) -> str:
        """The workspace the run works for: its surface's (the brand), else its lane's."""
        target = self.surface or self.lane
        return target.split("/", 1)[0] if target else "_"

    @property
    def target(self) -> str | None:
        """What grants and approvals check: the surface of a connector run, else the lane."""
        return self.surface or self.lane

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def needs_decision(self) -> bool:
        return bool(self.decision and self.decision.get("needed"))


class StepStatus(StrEnum):
    PENDING = "pending"
    STARTED = "started"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class StepRecord(BaseModel):
    run_id: str
    path: str
    idx: int = 0
    type: str = ""
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


# ---------------------------------------------------------------- schedules and triggers


class ScheduleKind(StrEnum):
    CRON = "cron"
    WATCH = "watch"
    WEBHOOK = "webhook"
    EVERY = "every"


DEFAULT_TIMEZONE = "Europe/Vilnius"


class CronConfig(StrictModel):
    cron: str
    timezone: str = DEFAULT_TIMEZONE
    missed: Literal["skip", "catch_up_once"] = "skip"
    jitter_s: int = Field(default=0, ge=0, le=3600)
    grace_s: int = Field(default=300, ge=30, le=86_400)


class WatchConfig(StrictModel):
    url: str
    locator: Locator
    read: Literal["text", "count"] = "text"
    every_s: int = Field(default=300, ge=30, le=86_400)
    debounce_s: int = Field(default=0, ge=0, le=86_400)


class EveryConfig(StrictModel):
    """A run every so often, whatever the clock says. What is late runs once."""
    every_s: int = Field(ge=60, le=7 * 86_400)


class WebhookConfig(StrictModel):
    accept_params: bool = False
    max_skew_s: int = Field(default=300, ge=10, le=3600)
    token: str = ""
    secret: str = Field(default="", repr=False)


class Schedule(BaseModel):
    """A cron slot, page watch, webhook or interval. It fires either a flow (`flow_id`) or an events
    route (`route_id`, openajar.events): exactly one of them. A route schedule emits an event
    keyed exactly as the flow run would be, and the route decides what it wakes."""
    id: str
    kind: ScheduleKind
    flow_id: str | None = None
    route_id: str | None = None
    flow_version: str | None = None  # pin a version; None runs the current one
    lane: str | None = None
    surface: str | None = None  # a connector schedule's surface; each fire resolves its binding again
    params: dict[str, Any] = Field(default_factory=dict)
    cron: CronConfig | None = None
    watch: WatchConfig | None = None
    webhook: WebhookConfig | None = None
    every: EveryConfig | None = None
    deadman_minutes: int | None = Field(default=None, ge=1)
    max_failures: int = Field(default=3, ge=1, le=100)
    enabled: bool = True
    principal: dict[str, Any] = Field(default_factory=dict)
    grant_id: str | None = None  # the creator's grant; every fire runs under it
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    state: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_target(self) -> Schedule:
        if (self.flow_id is None) == (self.route_id is None):
            raise ValueError("a schedule fires exactly one of a flow (flow_id) or a trigger (route_id)")
        return self

    @property
    def target(self) -> str:
        """What it fires, for messages: the flow id or "trigger rt_..."."""
        return self.flow_id or f"trigger {self.route_id}"

    def public(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        if self.webhook:
            data["webhook"] = {"accept_params": self.webhook.accept_params,
                               "max_skew_s": self.webhook.max_skew_s,
                               "path": f"/api/hooks/{self.webhook.token[:6]}...",
                               "secret": "(hidden; shown once at creation)"}
        return data
