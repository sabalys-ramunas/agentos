"""Fakes for the events tests: lanes with control listeners, a policy that mints and taints,
a brains service, an agents service (the AgentsPort a later brief implements), approvals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agentos.agents.models import AgentState, TurnStatus
from agentos.agents.ports import TurnReceipt, TurnRequest
from agentos.core.errors import LaneNotFound, NotFound, PolicyDenied
from agentos.core.models import ControlState, LaneControl, LaneInfo, LaneSpec, Principal
from agentos.events.dispatcher import EventSettings
from agentos.events.models import QuietHours
from agentos.events.service import build
from agentos.runner.fakes import BASE, FakeClock, FakeNotifier, Harness

OWNER = {"kind": "human", "id": "owner", "owner": "owner"}
# 12:00 in Vilnius on a Wednesday: outside the node's default quiet hours
NOON = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)


def check_flow(fid: str = "biz-a.social.check", lane: str = "biz-a/social") -> dict:
    return {"id": fid, "lane": lane,
            "params": {"properties": {"note": {"type": "string"}, "caption": {"type": "string"}}},
            "steps": [{"type": "navigate", "url": f"{BASE}/"},
                      {"type": "readback", "locator": {"role": "heading", "name": "Feed"},
                       "condition": {"kind": "exists"}}]}


class ControlLanes:
    """A lanes service with control records and listeners, like LaneManager."""

    def __init__(self, lanes: list[str]):
        self.info = {lane: LaneInfo(spec=LaneSpec(id=lane)) for lane in lanes}
        self.controls = {lane: LaneControl() for lane in lanes}
        self.listeners: list[Any] = []

    def get(self, lane_id: str) -> LaneInfo:
        if lane_id not in self.info:
            raise LaneNotFound(lane_id)
        return self.info[lane_id]

    def control(self, lane_id: str) -> LaneControl:
        if lane_id not in self.controls:
            raise LaneNotFound(lane_id)
        return self.controls[lane_id]

    def add_control_listener(self, cb: Any) -> None:
        self.listeners.append(cb)

    def remove_control_listener(self, cb: Any) -> None:
        self.listeners.remove(cb)

    def _set(self, lane: str, new: LaneControl) -> None:
        old, self.controls[lane] = self.controls[lane], new
        for cb in list(self.listeners):
            cb(lane, old, new)

    def hold(self, lane: str, reason: str, now: datetime) -> None:
        self._set(lane, LaneControl(state=ControlState.PAUSED, by="node", since=now, reason=reason))

    def hand_back(self, lane: str, now: datetime) -> None:
        self._set(lane, LaneControl(state=ControlState.AGENT, by="owner", since=now))


class Ctx:
    def __init__(self, policy: Policy, grant_id: str):
        self.policy, self.grant_id = policy, grant_id

    def precheck(self, effect: Any, *, lane_id: str | None = None) -> None:
        self.policy.prechecks.append((self.grant_id, str(effect), lane_id))
        if (str(effect), lane_id) in self.policy.refuse:
            raise PolicyDenied(f"this grant does not cover {effect} on {lane_id}")

    def taint(self, *labels: str, source: str | None = None) -> None:
        self.policy.taints.setdefault(self.grant_id, set()).update(labels)

    async def authorize(self, *a: Any, **kw: Any) -> None:
        return None


class Policy:
    """context_for_grant / mint_child_token / revoke, the methods agentos.events uses."""

    def __init__(self) -> None:
        self.minted: list[dict[str, Any]] = []
        self.revoked: list[str] = []
        self.taints: dict[str, set[str]] = {}
        self.prechecks: list[tuple] = []
        self.refuse: set[tuple[str, str | None]] = set()
        self.dead: set[str] = set()

    def context_for_grant(self, grant_id: str, *, principal: Principal | None = None, tool: str | None = None,
                          **kw: Any) -> Ctx:
        if grant_id in self.dead:
            raise PolicyDenied(f"grant {grant_id} was revoked")
        return Ctx(self, grant_id)

    def mint_child_token(self, principal: Principal, **kw: Any) -> dict[str, Any]:
        self.minted.append({"principal": principal, **kw})
        n = len(self.minted)
        return {"token": f"child-token-{n}", "grant_id": f"gr_child{n:04d}", "expires_at": None}

    def revoke(self, grant_id: str, *, by: str = "owner", reason: str | None = None) -> dict:
        self.revoked.append(grant_id)
        return {}


class Brains:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.statuses: dict[str, str] = {}

    async def submit(self, principal: Principal, **kw: Any) -> dict[str, Any]:
        tid = f"bt_{len(self.submitted) + 1}"
        self.submitted.append({"principal": principal, "id": tid, **kw})
        self.statuses[tid] = "running"
        return {"id": tid, "status": "queued"}

    def status(self, task_id: str) -> dict[str, Any]:
        if task_id not in self.statuses:
            raise NotFound(task_id)
        return {"id": task_id, "status": self.statuses[task_id], "status_reason": None}


class Agents:
    """The AgentsPort brief B2 implements: lane states and idempotent deliver."""

    def __init__(self, lanes: list[str]):
        self.states = {lane: AgentState.COLD for lane in lanes}
        self.requests: list[TurnRequest] = []
        self.by_key: dict[str, str] = {}

    def lane_state(self, lane: str) -> AgentState | None:
        return self.states.get(lane)

    async def deliver(self, request: TurnRequest) -> TurnReceipt:
        if request.key in self.by_key:
            return TurnReceipt(turn_id=self.by_key[request.key], duplicate=True)
        self.requests.append(request)
        tid = f"tu_{len(self.requests):012x}"
        self.by_key[request.key] = tid
        return TurnReceipt(turn_id=tid, status=TurnStatus.QUEUED)


@dataclass
class Approval:
    id: str
    state: str = "pending"
    lane: str | None = None
    kind: str = "action"
    content_hash: str | None = None
    content_digest: str | None = None
    binding: str | None = None


class Approvals:
    def __init__(self) -> None:
        self.items: dict[str, Approval] = {}
        self.created: list[dict[str, Any]] = []

    def create_action(self, **kw: Any) -> Approval:
        ap = Approval(id=f"ap_{len(self.items) + 1}", lane=kw.get("lane"), content_hash=kw.get("content_hash"),
                      content_digest=kw.get("content_digest"), binding=kw.get("binding"))
        self.items[ap.id] = ap
        self.created.append(kw)
        return ap

    def get(self, approval_id: str) -> Approval:
        if approval_id not in self.items:
            raise NotFound(approval_id)
        return self.items[approval_id]

    def consume(self, approval_id: str) -> bool:
        ap = self.items.get(approval_id)
        if ap is None or ap.state != "approved":
            return False
        ap.state = "consumed"
        return True


class World:
    """A runner harness plus the events service on one home, with every fake wired."""

    def __init__(self, tmp: Any, *, start: datetime = NOON, lanes: list[str] | None = None,
                 agent_lanes: list[str] | None = None, settings: EventSettings | None = None,
                 policy: bool = True, clock: FakeClock | None = None):
        self.clock = clock or FakeClock(start)
        self.lanes = ControlLanes(lanes or ["biz-a/social", "biz-a/instagram", "biz-a/facebook", "biz-b/facebook"])
        self.notifier = FakeNotifier()
        self.policy = Policy() if policy else None
        self.brains = Brains()
        self.agents = Agents(agent_lanes or ["biz-a/claude"])
        self.approvals = Approvals()
        services = {"lanes": self.lanes, "notifier": self.notifier, "brains": self.brains, "agents": self.agents,
                    "approvals": self.approvals}
        if self.policy is not None:
            services["policy"] = self.policy
        self.h = Harness(tmp, clock=self.clock, services=services)
        self.runtime = self.h.runtime
        self.events = build(self.runtime, home=tmp, clock=self.clock, settings=settings or EventSettings())
        self.store = self.events.store

    def flow(self, definition: dict | None = None):
        return self.h.save(definition or check_flow())

    def route(self, **kw: Any):
        data = {"name": "t", "match": {"source": "watch", "workspace": "biz-a"},
                "target": {"kind": "agent", "lane": "biz-a/claude"}, "instruction": "Read it and draft a reply.",
                "grant": {"lanes": ["biz-a/instagram"]}}
        data.update(kw)
        return self.events.add_route(data, principal=OWNER)

    def emit(self, n: int | str = 1, **kw: Any):
        data = {"source": "watch", "kind": "watch.changed", "workspace": "biz-a", "lane": "biz-a/instagram",
                "dedupe_key": f"watch:sch_x:{n}", "facts": {"seq": n if isinstance(n, int) else 0},
                "content": {"text": f"comment {n}"}}
        data.update(kw)
        return self.events.emit(data)

    def deliveries(self, route_id: str | None = None):
        return list(reversed(self.store.list_deliveries(route_id=route_id, limit=1000)))  # oldest first

    async def tick(self):
        return await self.events.tick()


def no_quiet() -> EventSettings:
    """Settings with quiet hours that never apply in these tests (03:00 to 03:01 UTC)."""
    return EventSettings(quiet_hours=QuietHours.model_validate({"tz": "UTC", "from": "03:00", "to": "03:01"}))
