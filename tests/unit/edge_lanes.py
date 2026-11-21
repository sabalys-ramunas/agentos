"""The real LaneManagerImpl (locks, eviction, shutdown) over a fake engine and fake displays.

For edge-case tests that need the lane manager's own concurrency rules. No browser, no X.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from openajar.core.config import NodeConfig
from openajar.core.models import ProxyConfig, ViewerInfo
from openajar.node.lanes import LaneManagerImpl
from openajar.node.store import LaneStore


class FakePage:
    def __init__(self, engine: FakeEngine):
        self.engine = engine
        self.url = "about:blank"

    async def evaluate(self, js: str, arg=None):
        return self.engine.exit_ip

    def locator(self, selector: str) -> FormlessLocator:
        return FormlessLocator()


class FormlessLocator:
    """What browser_act reads before a click (openajar.node.fields.read_form_diff): no form here."""

    async def evaluate(self, js: str, arg=None) -> None:
        return None


class FakeSession:
    def __init__(self, engine: FakeEngine, lane_id: str):
        self.engine = engine
        self.lane_id = lane_id
        self.alive = True
        self.closed = False
        self._page = FakePage(engine)

    async def page(self):
        return self._page

    async def close(self):
        await asyncio.sleep(0)
        self.closed = True
        self.alive = False
        self.engine.running.discard(self.lane_id)


class FakeEngine:
    name = "camoufox"

    def __init__(self, exit_ip: str = "203.0.113.7"):
        self.exit_ip = exit_ip
        self.running: set[str] = set()
        self.launches: list[str] = []
        self.sessions: list[FakeSession] = []

    async def launch(self, spec, *, profile_dir: Path, fingerprint_path: Path, display: str, xauthority=None,
                     proxy):
        self.launches.append(spec.id)
        session = FakeSession(self, spec.id)
        self.sessions.append(session)
        self.running.add(spec.id)
        return session


class FakeDisplays:
    def __init__(self, config: NodeConfig):
        self.config = config
        self.slots: dict[str, int] = {}

    async def acquire(self, lane_id: str, *, size: tuple[int, int] | None = None) -> ViewerInfo:
        if lane_id not in self.slots:
            used = set(self.slots.values())
            self.slots[lane_id] = min(i for i in range(self.config.max_slots) if i not in used)
        return self.get(lane_id)

    async def release(self, lane_id: str) -> None:
        self.slots.pop(lane_id, None)

    def get(self, lane_id: str) -> ViewerInfo | None:
        if lane_id not in self.slots:
            return None
        i, c = self.slots[lane_id], self.config
        return ViewerInfo(display=f":{c.display_base + i}", vnc_port=c.vnc_base + i, viewer_port=c.viewer_base + i)


def make_manager(
    tmp_path: Path,
    *,
    engine: FakeEngine | None = None,
    proxies: dict[str, ProxyConfig] | None = None,
    self_check: bool = False,
    **cfg,
) -> tuple[LaneManagerImpl, FakeEngine, NodeConfig]:
    defaults = {"max_warm_lanes": 3, "busy_timeout": 1.0, "self_check": self_check}
    config = NodeConfig(home=tmp_path, **{**defaults, **cfg})
    engine = engine or FakeEngine()
    mgr = LaneManagerImpl(
        config,
        LaneStore(config.db_path),
        engine,
        FakeDisplays(config),
        lambda: dict(proxies or {}),
        self_check_url="https://example.test/ip",
        reaper_interval=60,
    )
    return mgr, engine, config
