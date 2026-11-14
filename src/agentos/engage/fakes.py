"""In-memory fakes of the executor's ports (agentos.engage.ports), for tests and dry runs.

    ops = FakeOperations()                       # every operation verifies
    ops.script[3] = OpResult(OpStatus.FAILED, signal="captcha")   # the 4th call hits a captcha
    targets = FakeTargets()                      # an endless supply: "<kind>-target-<n>"
    content = FakeContent()                      # an endless supply of items, or FakeContent(items=[...])
    lanes = FakeLanes()                          # records claims; refuse="human_in_control" refuses
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from agentos.engage.ports import Content, ContentOrigin, LaneUnavailable, OpResult, OpStatus


@dataclass
class Call:
    surface: str
    flow_id: str
    kind: str
    params: dict[str, Any]
    idempotency_key: str
    ask: bool
    parent: str | None


class FakeOperations:
    """Every call verifies unless `script` (call index -> OpResult, or a callable of the Call)
    says otherwise. A repeated idempotency key returns the first result and is not a new call."""

    def __init__(self, script: dict[int, OpResult | Callable[[Call], OpResult]] | None = None):
        self.script = dict(script or {})
        self.calls: list[Call] = []
        self.by_key: dict[str, OpResult] = {}
        self.later: dict[str, OpResult] = {}     # run id -> what check() returns

    async def run(self, surface: str, spec: Any, params: dict[str, Any], *, idempotency_key: str,
                  ask: bool = True, parent: str | None = None) -> OpResult:
        if idempotency_key in self.by_key:
            return self.by_key[idempotency_key]
        call = Call(surface, spec.flow_id, str(spec.kind), dict(params), idempotency_key, ask, parent)
        n = len(self.calls)
        self.calls.append(call)
        got = self.script.get(n)
        res = got(call) if callable(got) else got
        if res is None:
            res = OpResult(OpStatus.VERIFIED, run_id=f"run-{n}")
        elif res.run_id is None:
            res = replace(res, run_id=f"run-{n}")
        self.by_key[idempotency_key] = res
        return res

    async def check(self, run_id: str) -> OpResult | None:
        return self.later.get(run_id)

    async def lookup(self, idempotency_key: str, flow_id: str, surface: str) -> OpResult | None:
        """The finished run an idempotency key started, or None (`by_key`, `later` by run id)."""
        res = self.by_key.get(idempotency_key)
        return self.later.get(res.run_id or "", res) if res is not None else None


class FakeTargets:
    """`pool[kind]` lists targets in order; a kind with no pool gets "<kind>-target-<n>" forever,
    unless `endless` is False (then None)."""

    def __init__(self, pool: dict[str, list[str]] | None = None, *, endless: bool = True):
        self.pool = {k: list(v) for k, v in (pool or {}).items()}
        self.endless = endless
        self.n: dict[str, int] = {}

    def next(self, surface: str, kind: str, program: Any) -> str | None:
        kind = str(kind)
        if kind in self.pool:
            return self.pool[kind].pop(0) if self.pool[kind] else None
        if not self.endless:
            return None
        self.n[kind] = self.n.get(kind, 0) + 1
        return f"{kind}-target-{self.n[kind]}"


class FakeContent:
    """`items` are handed out in order (any kind); without items, an endless supply of drafts."""

    def __init__(self, items: list[Content] | None = None, *, endless: bool = True,
                 origin: ContentOrigin = ContentOrigin.DRAFT):
        self.items = list(items or [])
        self.endless = endless and items is None
        self.origin = origin
        self.n = 0
        self.used: list[tuple[str, str | None]] = []
        self.returned: list[str] = []

    def take(self, surface: str, kind: str, n: int) -> list[Content]:
        out = []
        while len(out) < n:
            if self.items:
                out.append(self.items.pop(0))
            elif self.endless:
                self.n += 1
                out.append(Content(id=f"c{self.n}", origin=self.origin, text=f"{kind} text {self.n}",
                                   title=f"{kind} title {self.n}"))
            else:
                break
        return out

    def settle(self, surface: str, content: Content, *, used: bool, run_id: str | None = None) -> None:
        if used:
            self.used.append((content.id, run_id))
        else:
            self.returned.append(content.id)
            self.items.insert(0, content)


@dataclass
class FakeLanes:
    """Each surface acts through a lane of the same name. `refuse` (a reason) makes claim fail,
    `lost` (a reason) makes renew fail."""
    refuse: str | None = None
    claims: list[tuple[str, str]] = field(default_factory=list)
    releases: list[tuple[str, str]] = field(default_factory=list)
    held: dict[str, str] = field(default_factory=dict)       # lane -> holder
    renewals: list[tuple[str, str]] = field(default_factory=list)
    lost: str | None = None                                  # a reason: renew fails (the claim is gone)

    def lane_for(self, surface: str) -> str:
        return surface

    async def claim(self, lane: str, *, holder: str, purpose: str, ttl_s: float) -> None:
        if self.refuse:
            raise LaneUnavailable(self.refuse)
        self.claims.append((lane, holder))
        self.held[lane] = holder

    async def renew(self, lane: str, *, holder: str, ttl_s: float) -> None:
        if self.lost or self.held.get(lane) != holder:
            raise LaneUnavailable(self.lost or "claim_lost")
        self.renewals.append((lane, holder))

    async def release(self, lane: str, *, holder: str) -> None:
        self.releases.append((lane, holder))
        if self.held.get(lane) == holder:
            del self.held[lane]
