"""A node runtime for the freeze tests: the real lane manager over fakes, the real policy service,
the real freeze service, and small fakes for the runner, schedules, triggers and brain tasks."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from test_lanes import Clock, FakeDisplays, FakeEngine
from test_policy_support import OWNER

from agentos.core.config import NodeConfig
from agentos.core.models import Effect, LaneSpec
from agentos.core.runtime import Runtime
from agentos.freeze.service import FreezeService
from agentos.node.lanes import LaneManagerImpl
from agentos.node.store import LaneStore
from agentos.policy import freeze as freeze_gate
from agentos.policy.service import PolicyService


class FakeRunner:
    def __init__(self, runs: list[Any] | None = None, schedules: list[Any] | None = None):
        self.runs = runs or []
        self.cancelled: list[str] = []
        self.scheduler = SimpleNamespace(list=lambda kind=None: list(schedules or []), set_enabled=self._toggle)
        self.toggled: list[tuple[str, bool]] = []
        self._schedules = schedules or []

    def list(self, *, status=None, lane=None, workspace=None, limit=100):
        out = [r for r in self.runs if r.status in [str(getattr(s, "value", s)) for s in status or []]]
        if lane is not None:
            out = [r for r in out if r.lane == lane]
        if workspace is not None:
            out = [r for r in out if r.workspace == workspace]
        return out

    async def cancel(self, run_id: str, *, by: str | None = None):
        self.cancelled.append(run_id)

    def _toggle(self, sid: str, enabled: bool):
        self.toggled.append((sid, enabled))
        for s in self._schedules:
            if s.id == sid:
                s.enabled = enabled


def run(rid: str, lane: str | None, status: str = "queued") -> SimpleNamespace:
    return SimpleNamespace(id=rid, lane=lane, workspace=lane.split("/")[0] if lane else None, status=status)


def schedule(sid: str, lane: str | None, flow: str = "biz-a.x.post", surface: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=sid, lane=lane, surface=surface, flow_id=flow, enabled=True)


class FakeEvents:
    def __init__(self, routes: list[Any]):
        self.routes = routes
        self.paused: list[str] = []
        self.dispatcher = SimpleNamespace(pause=self._pause)

    def list_routes(self, workspace=None):
        return list(self.routes)

    def _pause(self, route, reason, now):
        route.state.paused = True
        self.paused.append(route.id)


def route(rid: str, workspace: str, *, target_lane: str | None = None, grant_lanes: list[str] | None = None):
    return SimpleNamespace(id=rid, state=SimpleNamespace(paused=False), target=SimpleNamespace(lane=target_lane),
                           match=SimpleNamespace(workspace=workspace, lane=None),
                           grant=SimpleNamespace(lanes=grant_lanes or []))


class FakeBrains:
    def __init__(self, tasks: list[Any]):
        self.tasks = tasks
        self.cancelled: list[str] = []
        self.store = SimpleNamespace(list_tasks=self._list)

    def _list(self, *, statuses=None, workspace=None, limit=100):
        return [t for t in self.tasks if workspace is None or t.workspace == workspace]

    async def cancel(self, task_id, principal=None, *, wait=False):
        self.cancelled.append(task_id)


def task(tid: str, workspace: str, lanes: list[str], status: str = "running") -> SimpleNamespace:
    return SimpleNamespace(id=tid, workspace=workspace, lanes=lanes, status=SimpleNamespace(value=status))


class Node(SimpleNamespace):
    runtime: Runtime
    lanes: LaneManagerImpl
    policy: PolicyService
    freeze: FreezeService

    def agent(self, *, workspaces=None, lanes=None, sub="agent-1", during_freeze=False):
        """An agent grant. `during_freeze`: one made while its scope is frozen, as a node before the
        refusal (agentos.policy.freeze.check_new_grant) could, to test the gates behind it."""
        with mock.patch.object(freeze_gate, "check_new_grant", lambda *_a: None) if during_freeze \
                else contextlib.nullcontext():
            g, tok = self.policy.issue(subject={"id": sub}, workspaces=workspaces, lanes=lanes, ceiling=Effect.COMMIT)
        return g, tok


def make_node(home: Path, *, lanes: tuple[str, ...] = ("biz-a/one", "biz-a/two", "biz-b/three"),
              stop_wait_s: float = 2.0, extra: dict | None = None, **cfg: Any) -> Node:
    config = NodeConfig(home=home, name="box-1", mcp_token=OWNER, **{
        "max_warm_lanes": 3, "busy_timeout": 1.0, "self_check": False, "idle_seconds": 600, "display_base": 110,
        "vnc_base": 5810, "viewer_base": 6110, **cfg})
    rt = Runtime(config=config)
    engine = FakeEngine()
    mgr = LaneManagerImpl(config, LaneStore(config.db_path), engine, FakeDisplays(config), dict, clock=Clock(),
                          reaper_interval=60, hold_policy=None)
    rt.register("lanes", mgr)
    policy = PolicyService(home, node_config=config, runtime=rt)
    for name, svc in (("policy", policy), ("approvals", policy.approvals), ("traces", policy.traces),
                      ("notifier", policy.notifier), *(extra or {}).items()):
        rt.register(name, svc)
    freeze = FreezeService(rt, stop_wait_s=stop_wait_s)
    rt.register("freeze", freeze)
    node = Node(runtime=rt, lanes=mgr, policy=policy, freeze=freeze, engine=engine, config=config, ids=lanes)
    return node


async def started(node: Node) -> Node:
    await node.lanes.start()
    for lane in node.ids:
        if node.lanes.store.get(lane) is None:
            await node.lanes.create(LaneSpec(id=lane))
    return node


async def close(node: Node) -> None:
    await node.freeze.stop()
    await node.lanes.shutdown()
    node.policy.close()


def now() -> datetime:
    return datetime.now(UTC)
