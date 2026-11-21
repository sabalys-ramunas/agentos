"""What other packages call on agent lanes (the target interface openajar.events delivers to).

openajar.events never runs an agent. For a route whose target is an agent lane it hands the
lane a TurnRequest through AgentsPort.deliver and records the receipt. Brief B2's
AgentService implements this Protocol and registers itself as the runtime service "agents";
until then a node has no agent lanes and agent-target deliveries wait with the reason "no
agent lanes on this node".

The contract, in order:

1. `lane_state(lane)` says whether a turn could start now. `None` means there is no such
   agent lane. A state where `AgentState.queues_turns` is true makes the delivery wait (not
   drop); the dispatcher asks again on its next tick.
2. `deliver(request)` queues one turn and returns quickly. It must be idempotent on
   `request.key` (`<route>:<event>:<attempt>`): a second call with the same key returns the
   same turn with `duplicate=True`. The agents service mints the turn grant itself, as a child
   of the lane grant, narrowed to `request.grant` and seeded with `request.taint_seed`
   (policy `mint_turn_token`, brief B3). The prompt is the whole user message of the turn:
   openajar.events.frame wrote it, and the agent service adds nothing.
3. When the turn ends, the agents service calls `EventsPort.delivery_finished(delivery_id,
   ...)` with the turn's honest status, and emits `turn.finished` through `EventsPort.emit`
   with `cause={"event": <the first event id>, "turn": <turn id>, "depth": request.depth + 1}`
   and the final text as untrusted content.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from openajar.agents.models import AgentState, TurnStatus
from openajar.core.models import Effect


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TurnBudget(_Strict):
    turns: int | None = Field(default=None, ge=1)
    minutes: int | None = Field(default=None, ge=1)
    tokens: int | None = Field(default=None, ge=1)
    usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class TurnGrant(_Strict):
    """What the turn grant may reach: never more than the lane grant (policy narrows field by
    field and refuses anything wider)."""
    ceiling: Effect = Effect.DRAFT
    lanes: list[str] = Field(default_factory=list)
    flows: list[str] = Field(default_factory=list)
    ttl_s: int = Field(default=900, ge=60)
    budget: TurnBudget = Field(default_factory=TurnBudget)


class TurnRequest(_Strict):
    lane: str                                     # the agent lane, e.g. "biz-a/claude"
    delivery_id: str
    key: str                                      # "<route>:<event>:<attempt>": the idempotency key
    route_id: str | None = None                   # None for an owner message without a route
    event_ids: list[str] = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)
    depth: int = Field(default=0, ge=0)           # the deepest cause.depth among the events
    prompt: str                                   # the wake message (openajar.events.frame)
    session: Literal["new", "main", "per-route-day"] = "new"
    grant: TurnGrant = Field(default_factory=TurnGrant)
    taint_seed: list[Literal["untrusted", "private", "owner"]] = Field(default_factory=list)
    priority: Literal["urgent", "normal", "low"] = "normal"


class TurnReceipt(_Strict):
    turn_id: str | None = None
    status: TurnStatus = TurnStatus.QUEUED
    duplicate: bool = False
    reason: str | None = None


@runtime_checkable
class AgentsPort(Protocol):
    """Runtime service "agents" (brief B2)."""

    def lane_state(self, lane: str) -> AgentState | None: ...

    async def deliver(self, request: TurnRequest) -> TurnReceipt: ...
