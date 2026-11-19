"""In-memory stand-ins for the node's lane manager, so the workspace layer can be tested without a browser.

Same pattern as runner/fakes.py. `FakeLanes` implements the three LaneManager calls this package makes
(create, list, get) and checks proxy labels the way the real manager does.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentos.core.config import NodeConfig
from agentos.core.errors import EngineError, InvalidRequest, LaneExists, LaneNotFound
from agentos.core.models import LaneInfo, LaneSpec, LaneState, Principal
from agentos.core.runtime import Runtime
from agentos.workspaces.service import WorkspaceService, build


class FakeLanes:
    """LaneManager over a dict. `before_create(spec)` runs just before each create, so a test can make
    another lane appear in between (a race). `fail_on` makes creating those lane ids raise an engine error."""

    def __init__(self, proxies: set[str] | None = None):
        self.proxies: set[str] = set(proxies or ())
        self.lanes: dict[str, LaneInfo] = {}
        self.created: list[str] = []
        self.before_create: Callable[[LaneSpec], None] | None = None
        self.fail_on: set[str] = set()

    async def create(self, spec: LaneSpec) -> LaneInfo:
        if self.before_create is not None:
            self.before_create(spec)
        if spec.id in self.lanes:
            raise LaneExists(f"lane {spec.id} already exists")
        if spec.proxy_label and spec.proxy_label not in self.proxies:
            raise InvalidRequest(f"unknown proxy {spec.proxy_label!r}. Add it first with: agentos proxy add")
        if spec.id in self.fail_on:
            raise EngineError(f"the browser for {spec.id} did not start")
        info = LaneInfo(spec=spec)
        self.lanes[spec.id] = info
        self.created.append(spec.id)
        return info

    def add(self, lane_id: str, *, proxy: str | None = None, state: LaneState = LaneState.COLD) -> LaneInfo:
        """Put a lane on the node without going through create (a lane made before the workspace layer)."""
        info = LaneInfo(spec=LaneSpec(id=lane_id, proxy_label=proxy), state=state)
        self.lanes[lane_id] = info
        return info

    def list(self) -> list[LaneInfo]:
        return [self.lanes[key] for key in sorted(self.lanes)]

    def get(self, lane_id: str) -> LaneInfo:
        if lane_id not in self.lanes:
            raise LaneNotFound(f"no lane {lane_id!r}; see browsers_list")
        return self.lanes[lane_id]


class FakeCtx:
    """A ToolContext for tool tests: records authorize calls, and can be told which lanes the grant covers."""

    def __init__(self, runtime: Runtime, *, allowed: Callable[[str], bool] | None = None):
        self.runtime = runtime
        self.principal = Principal(kind="agent", id="claude-code", thread="T-1")
        self.allowed = allowed or (lambda lane_id: True)
        self.authorized: list[dict[str, Any]] = []
        self.traces: list[dict[str, Any]] = []

    async def authorize(self, effect: Any, *, lane_id: str | None = None, domain: str | None = None,
                        summary: str | None = None, content: Any = None, cost: Any = None,
                        facts: dict | None = None) -> None:
        self.authorized.append({"effect": effect, "lane_id": lane_id, "summary": summary, "content": content})

    def trace(self, kind: str, **fields: Any) -> None:
        self.traces.append({"kind": kind, **fields})

    def can_access_lane(self, lane_id: str) -> bool:
        return self.allowed(lane_id)


class Harness:
    """A workspace service on a temporary home, with a FakeLanes and a list of proxy labels."""

    def __init__(self, home: Path, *, proxies: tuple[str, ...] = ("lt1", "lt2"), clock: Any = None):
        self.home = home
        self.lanes = FakeLanes(set(proxies))
        self.runtime = Runtime(config=NodeConfig(home=home), lanes=self.lanes)
        kwargs: dict[str, Any] = {"home": home, "labels": lambda: set(self.lanes.proxies)}
        if clock is not None:
            kwargs["clock"] = clock
        self.service: WorkspaceService = build(self.runtime, **kwargs)

    def close(self) -> None:
        self.service.store.close()
