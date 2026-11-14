"""The health service (runtime name "health").

Runs HealthMonitor on a timer inside agentosd and serves the ops control socket
(`AGENTOS_HOME/run/ops.sock`, see agentos.ops.control) that `agentos backup` and
`agentos upgrade` use to pause and drain lanes. Built by `build(runtime)`; the daemon calls
`await start()` after the lane manager started and `await stop()` before it shuts down.

Runtime services it uses, all looked up on each round so registration order does not matter:
"config" (NodeConfig), "lanes" (LaneManager), "notifier" (optional).

It also registers "lane_check" (agentos.stealth.lanecheck.LaneChecker): the per-lane checklist behind
`agentos lane check`, the lane_check MCP tool and the panel's lane check routes. The health rounds
run it too (one finding per lane), and share its proxy probes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from agentos.core.config import home_from_env, load_config
from agentos.core.errors import AgentosError, InvalidRequest
from agentos.core.layout import ops_socket
from agentos.core.models import LaneState, parse_lane_id
from agentos.core.system import Host
from agentos.ops.control import start_server
from agentos.ops.health import HealthMonitor
from agentos.stealth.lanecheck import LaneChecker

log = logging.getLogger("agentos.ops.service")

_HOT = (LaneState.WARMING, LaneState.WARM, LaneState.ACTIVE)


class HealthService:
    def __init__(self, runtime: Any, *, interval: float = 60.0, proxy_every: int = 5, first_delay: float = 30.0,
                 host: Host | None = None, socket_path: Path | None = None, control: bool = True):
        self.runtime = runtime
        cfg = runtime.get("config") if runtime is not None else None
        self.config = cfg or load_config(home_from_env())
        self.interval = interval
        self.proxy_every = max(1, proxy_every)
        self.first_delay = first_delay
        self.socket_path = socket_path or ops_socket(Path(self.config.home))
        self.control = control
        self.monitor = HealthMonitor(self.config, host=host)
        # The tool and the panel call the checker directly, before the first round's _sync().
        self.checker = LaneChecker(self.config, host=host, lanes_lookup=self._lanes)
        self.monitor.lane_checker = self.checker
        self._task: asyncio.Task | None = None
        self._server: asyncio.AbstractServer | None = None
        self._round = 0
        self._lock = asyncio.Lock()
        self._background: set[asyncio.Task] = set()

    # ---- runtime lookups ----------------------------------------------------------------

    def _lanes(self) -> Any:
        return self.runtime.get("lanes") if self.runtime is not None else None

    def _sync(self) -> None:
        self.monitor.lanes = self._lanes()
        self.checker.lanes = self.monitor.lanes
        self.monitor.notifier = self.runtime.get("notifier") if self.runtime is not None else None
        self.monitor.policy = self.runtime.get("policy") if self.runtime is not None else None
        self.monitor.anchors = self.runtime.get("anchors") if self.runtime is not None else None

    # ---- lifecycle ------------------------------------------------------------------------

    async def start(self) -> None:
        if self.control and self._server is None:
            try:
                self._server = await start_server(self, self.socket_path)
            except (OSError, AgentosError) as e:
                log.error("ops socket %s not started: %s", self.socket_path, e)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="agentos-health")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for task in list(self._background):
            task.cancel()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
            with contextlib.suppress(OSError):
                self.socket_path.unlink()

    async def _loop(self) -> None:
        await asyncio.sleep(self.first_delay)
        while True:
            try:
                await self.run_round()
            except Exception:
                log.exception("health round failed")
            await asyncio.sleep(self.interval)

    async def run_round(self) -> dict[str, Any]:
        async with self._lock:
            self._sync()
            proxies = self._round % self.proxy_every == 0
            self._round += 1
            return await self.monitor.run_once(proxies=proxies, lanes=self.monitor.lanes is not None)

    # ---- what the socket and the panel call -------------------------------------------------

    def status(self) -> dict[str, Any]:
        return self.monitor.last or {"at": None, "level": "unknown", "findings": [], "quarantined": {}}

    async def lane_check(self, lane: str | None = None) -> list[dict[str, Any]]:
        """Run the lane check now: one lane, or every lane when `lane` is None."""
        self._sync()
        if lane is not None:
            try:
                parse_lane_id(lane)
            except ValueError as e:
                raise InvalidRequest(str(e)) from None
            return [(await self.checker.check(lane)).to_dict()]
        return [r.to_dict() for r in await self.checker.check_all()]

    async def lane_health(self, lane: str | None = None, *, days: int = 30, fresh: bool = False
                          ) -> list[dict[str, Any]]:
        """Lane health (agentos.stealth.health): one lane, or every lane when `lane` is None. From the
        lane check's last report when there is one, else it runs the check now."""
        from agentos.stealth import health

        self._sync()
        if not 1 <= days <= health.MAX_DAYS:
            raise InvalidRequest(f"days is 1 to {health.MAX_DAYS}")
        if lane is not None:
            try:
                parse_lane_id(lane)
            except ValueError as e:
                raise InvalidRequest(str(e)) from None
            return [(await health.gather(self.checker, lane, days=days, fresh=fresh)).to_dict()]
        return [h.to_dict() for h in await health.gather_all(self.checker, days=days, fresh=fresh)]

    async def check_now(self) -> dict[str, Any]:
        self._round = 0  # include the proxies
        return await self.run_round()

    def lanes_summary(self) -> list[dict[str, Any]]:
        lanes = self._lanes()
        if lanes is None:
            return []
        return [{"id": i.spec.id, "state": i.state.value, "proxy": i.spec.proxy_label} for i in lanes.list()]

    def holders(self) -> list[dict[str, Any]]:
        """The lanes a restart would close and who is on each (LaneManagerImpl.holders). A lane
        manager without that report (the dev fakes) is read from its lane list: the claim, a person
        in control, and nothing else."""
        lanes = self._lanes()
        if lanes is None:
            return []
        report = getattr(lanes, "holders", None)
        if callable(report):
            return report()
        out = []
        for info in lanes.list():
            if info.state not in _HOT:
                continue
            found = []
            claim = info.claim or {}
            if claim.get("holder"):
                found.append({"kind": "run" if claim.get("implicit") else "claim", "who": str(claim["holder"]),
                              "purpose": claim.get("purpose") or ""})
            state = getattr(info.control.state, "value", info.control.state)
            if state in ("human", "paused"):
                found.append({"kind": "takeover" if state == "human" else "paused",
                              "who": info.control.by or "a person"})
            out.append({"id": info.spec.id, "state": info.state.value, "holders": found})
        return out

    def _require_lanes(self) -> Any:
        lanes = self._lanes()
        if lanes is None:
            raise AgentosError("the lane manager is not running in this daemon")
        return lanes

    async def close_lane(self, lane_id: str) -> str:
        try:
            parse_lane_id(lane_id)
        except ValueError as e:
            raise InvalidRequest(str(e)) from None
        info = await self._require_lanes().close(lane_id)
        return info.state.value

    def open_lane_soon(self, lane_id: str) -> None:
        parse_lane_id(lane_id)
        lanes = self._require_lanes()

        async def warm() -> None:
            try:
                await lanes.open(lane_id)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: could not warm again after a pause: %s", lane_id, type(e).__name__)

        task = asyncio.create_task(warm(), name=f"agentos-reopen-{lane_id}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain(self, timeout: float = 60.0) -> dict[str, Any]:
        """Send every warm lane cold gracefully (each waits for its in-flight tool call)."""
        lanes = self._require_lanes()
        hot = [i.spec.id for i in lanes.list() if i.state in _HOT]
        closed: list[str] = []
        failed: dict[str, str] = {}

        async def one(lane_id: str) -> None:
            try:
                await asyncio.wait_for(lanes.close(lane_id), timeout)
                closed.append(lane_id)
            except Exception as e:  # noqa: BLE001
                failed[lane_id] = type(e).__name__

        await asyncio.gather(*(one(lane_id) for lane_id in hot))
        return {"closed": sorted(closed), "failed": failed}


def build(runtime: Any) -> HealthService:
    service = HealthService(runtime)
    if runtime is not None:
        runtime.register("health", service)
        runtime.register("lane_check", service.checker)
    return service
