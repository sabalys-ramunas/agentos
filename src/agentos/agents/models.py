"""Shared shapes of agent lanes (docs/design/always-on-agents.md sections 4.2 and 4.4).

An agent lane is a lane of kind `agent`: one isolated place on a node where one hosted agent
(one runner, one owner) lives, with its working files, its CLI's own login and sessions, a
charter the owner writes, and a grant. It belongs to one workspace, so `biz-a/claude` can
never reach `biz-b` lanes: every lane it names must be in its own workspace.

A turn is one run of the agent's CLI for one delivery (an event or an owner message), under
its own turn grant: a child of the lane grant narrowed by the route that woke it and seeded
with the event's taint. Turns end `done_unverified`, `waiting_approval`, `failed` or
`interrupted`, never "done" (goal G6).
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentos.core.models import LANE_ID_RE, Effect, parse_lane_id, utcnow

TURN_ID_RE = re.compile(r"^tu_[0-9a-f]{12}$")
SESSION_ID_RE = re.compile(r"^se_[0-9a-f]{12}$")
LANE_PATTERN_RE = re.compile(r"^[a-z0-9*?\[\]-]{1,31}/[a-z0-9*?\[\]-]{1,41}$")
VOLUME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
FLOW_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}(\.[a-z0-9][a-z0-9_-]{0,40}){1,5}$")
HOST_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)*(:\d{1,5})?$")
RUNNERS = ("claude", "codex", "hermes", "openclaw")
PHASE_1_RUNNERS = ("claude", "codex")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def ladder_effect(v: Effect) -> Effect:
    if v.rank is None:
        raise ValueError("a ceiling must be one of read, draft, commit, pay, irreversible")
    return v


def lane_in_workspace(pattern: str, workspace: str, what: str) -> str:
    """A lane id or pattern whose workspace part is exactly `workspace` (no glob there)."""
    if not (LANE_ID_RE.match(pattern) or LANE_PATTERN_RE.match(pattern)):
        raise ValueError(f"{what} {pattern!r} must be a lane id like 'biz-a/instagram' or a pattern like 'biz-a/*'")
    ws = pattern.split("/", 1)[0]
    if ws != workspace:
        raise ValueError(f"{what} {pattern!r} is outside workspace {workspace!r}; a lane never reaches another "
                         "workspace")
    return pattern


class AgentState(StrEnum):
    """Where an agent lane is (design section 4.2.3)."""
    COLD = "cold"                  # folder on disk, no process
    TURN = "turn"                  # a turn process is running for a delivery
    ATTACHED = "attached"          # a human holds the session in the CLI's own UI; turns queue
    PAUSED = "paused"              # the owner stopped agents on this lane, or a kill switch fired
    NEEDS_AUTH = "needs_auth"      # the CLI reported a login problem; turns queue
    COOLING_DOWN = "cooling_down"  # the subscription hit a usage limit; turns queue until the reset
    QUARANTINED = "quarantined"    # the lane box failed its check; nothing runs

    @property
    def accepts_turns(self) -> bool:
        """A turn may start now (cold or between turns)."""
        return self == AgentState.COLD

    @property
    def queues_turns(self) -> bool:
        """Deliveries wait (not drop) while the lane is in this state."""
        return self in (AgentState.TURN, AgentState.ATTACHED, AgentState.NEEDS_AUTH, AgentState.COOLING_DOWN,
                        AgentState.PAUSED, AgentState.QUARANTINED)


class TurnStatus(StrEnum):
    """Honest turn outcomes. A turn finishing is never proof its work is right."""
    QUEUED = "queued"
    RUNNING = "running"
    DONE_UNVERIFIED = "done_unverified"
    WAITING_APPROVAL = "waiting_approval"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (TurnStatus.DONE_UNVERIFIED, TurnStatus.WAITING_APPROVAL, TurnStatus.FAILED,
                        TurnStatus.INTERRUPTED, TurnStatus.CANCELLED)


class Repo(_Strict):
    remote: str = Field(min_length=1, max_length=500)
    ref: str | None = Field(default=None, max_length=200)
    deploy_key_ref: str | None = Field(default=None, max_length=200)  # a vault secret bound to the remote's host

    @field_validator("remote")
    @classmethod
    def _remote(cls, v: str) -> str:
        if not re.match(r"^(git@[A-Za-z0-9.-]+:|https://|ssh://)[^\s]+$", v):
            raise ValueError("a repo remote looks like git@host:org/repo.git or https://host/org/repo.git")
        return v


class Volume(_Strict):
    name: str
    mode: Literal["ro", "rw"] = "ro"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not VOLUME_RE.match(v):
            raise ValueError(f"volume names are lowercase letters, digits and dashes: {v!r}")
        return v


class Reach(_Strict):
    """The lane grant's scope: which lanes the agent's agentos tools reach, and how far."""
    lanes: list[str] = Field(default_factory=list)
    ceiling: Effect = Effect.DRAFT
    flows: list[str] | None = None

    @field_validator("ceiling")
    @classmethod
    def _ceiling(cls, v: Effect) -> Effect:
        return ladder_effect(v)

    @field_validator("flows")
    @classmethod
    def _flows(cls, v: list[str] | None) -> list[str] | None:
        for f in v or []:
            if not FLOW_ID_RE.match(f):
                raise ValueError(f"flow ids are dotted lowercase segments like 'biz-a.instagram.reply': {f!r}")
        return v


class SessionPolicy(_Strict):
    owner: Literal["continue"] = "continue"           # owner messages continue the main session
    events: Literal["new", "per-route-day"] = "new"   # open question 2: a fresh session per event


class NativeTools(_Strict):
    allow: list[str] = Field(default_factory=list)  # empty: the runner's defaults (section 4.4.1)
    deny: list[str] = Field(default_factory=list)


class LaneLimits(_Strict):
    turns_per_hour: int = Field(default=6, ge=1, le=600)   # open question 9
    turns_per_day: int = Field(default=48, ge=1, le=10_000)
    memory_mb: int = Field(default=1536, ge=256, le=65_536)
    turn_minutes_max: int = Field(default=90, ge=1, le=24 * 60)


class RemoteChannels(_Strict):
    vendor_remote_control: bool = False  # Claude Remote Control / codex --remote: off (open question 5)


class AgentLaneSpec(_Strict):
    """What the owner sets up once per agent lane (design section 4.2.1)."""
    id: str
    runner: Literal["claude", "codex", "hermes", "openclaw"]
    credential: Literal["cli-native"] | dict[str, str] = "cli-native"  # {"brain": name}: vault custody, self-host
    model: str | None = Field(default=None, max_length=120)  # passed to the CLI; agentos never picks it
    preset: Literal["agent"] = "agent"
    egress_extra: list[str] = Field(default_factory=list)
    repos: list[Repo] = Field(default_factory=list, max_length=20)
    volumes: list[Volume] = Field(default_factory=list, max_length=20)
    reach: Reach = Field(default_factory=Reach)
    grant_ttl_days: int = Field(default=30, ge=1, le=365)
    sessions: SessionPolicy = Field(default_factory=SessionPolicy)
    native_tools: NativeTools = Field(default_factory=NativeTools)
    idle_stop_s: int = Field(default=900, ge=30, le=86_400)
    limits: LaneLimits = Field(default_factory=LaneLimits)
    remote: RemoteChannels = Field(default_factory=RemoteChannels)
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        parse_lane_id(v)
        return v

    @field_validator("credential")
    @classmethod
    def _credential(cls, v: str | dict[str, str]) -> str | dict[str, str]:
        if isinstance(v, dict) and (set(v) != {"brain"} or not v["brain"]):
            raise ValueError('credential is "cli-native" or {"brain": "<brain name>"}')
        return v

    @field_validator("egress_extra")
    @classmethod
    def _egress(cls, v: list[str]) -> list[str]:
        out = []
        for h in v:
            h = h.strip().lower().rstrip(".")
            if not HOST_RE.match(h):
                raise ValueError(f"not a host name for the egress allowlist: {h!r}")
            out.append(h)
        return sorted(set(out))

    @model_validator(mode="after")
    def _scope(self) -> AgentLaneSpec:
        ws = self.workspace
        for lane in self.reach.lanes:
            lane_in_workspace(lane, ws, "reach lane")
        if self.id in self.reach.lanes:
            raise ValueError("an agent lane does not list itself in its reach; its own lane is always its own")
        if self.runner not in PHASE_1_RUNNERS and self.credential != "cli-native":
            raise ValueError(f"{self.runner} configures its own providers inside the lane; credential must be "
                             "cli-native")
        return self

    @property
    def workspace(self) -> str:
        return parse_lane_id(self.id)[0]

    @property
    def name(self) -> str:
        return parse_lane_id(self.id)[1]


class Session(_Strict):
    """One CLI session in an agent lane. `taint` is the union of its turns' families, so a
    continued session never launders what it read (design section 4.4.2)."""
    id: str
    lane: str
    runner_session: str | None = None  # the CLI's own session id, once it reported one
    kind: Literal["main", "event", "route-day"] = "event"
    route_id: str | None = None        # for route-day sessions
    taint: list[Literal["untrusted", "private", "owner"]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    last_turn_at: datetime | None = None

    @field_validator("id")
    @classmethod
    def _sid(cls, v: str) -> str:
        if not SESSION_ID_RE.match(v):
            raise ValueError("a session id looks like se_<12 hex>")
        return v

    @field_validator("lane")
    @classmethod
    def _lane(cls, v: str) -> str:
        parse_lane_id(v)
        return v


class Turn(_Strict):
    """One turn: a CLI run for one delivery. Keyed by `route:event:attempt` so a duplicate
    start after a crash adopts or skips (design section 4.5)."""
    id: str
    lane: str
    session_id: str | None = None
    delivery_id: str | None = None   # the events delivery it answers; None for an attach
    event_ids: list[str] = Field(default_factory=list)
    route_id: str | None = None
    attempt: int = Field(default=1, ge=1, le=10)
    key: str                          # "<route>:<event>:<attempt>" or "owner:<event>:<attempt>"
    grant_id: str | None = None       # the turn grant
    status: TurnStatus = TurnStatus.QUEUED
    reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    final_text: str | None = None     # model output: untrusted when it becomes a turn.finished event

    @field_validator("id")
    @classmethod
    def _tid(cls, v: str) -> str:
        if not TURN_ID_RE.match(v):
            raise ValueError("a turn id looks like tu_<12 hex>")
        return v

    @field_validator("lane")
    @classmethod
    def _lane(cls, v: str) -> str:
        parse_lane_id(v)
        return v
