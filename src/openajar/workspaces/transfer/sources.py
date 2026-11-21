"""Where an export's data comes from and an import's goes to: the node's stores and services
(Sources), opened from the config (the CLI) or taken from the running daemon, and what pauses a
warm lane while its profile is copied."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openajar.core.config import NodeConfig
from openajar.core.errors import OpenajarError
from openajar.core.system import Host
from openajar.ops.backup import LaneCoordinator, NoDaemon
from openajar.workspaces.transfer.format import ExportRefused


@dataclass
class Sources:
    """One node's stores, as an export reads them and an import writes them. Built by
    `node_sources` (the CLI, from the node's files) or by the panel routes (the daemon's runtime).
    Any store may be None on a node that does not have it."""
    config: NodeConfig
    lanes: Any                          # the node's lane manager: list, get, create, import_profile
    workspaces: Any = None              # openajar.workspaces.store.WorkspaceStore
    vault: Any = None                   # openajar.vault.service.VaultService
    flows: Any = None                   # openajar.runner.flows.FlowService
    scheduler: Any = None               # openajar.runner.schedules.Scheduler
    policy: Any = None                  # openajar.policy.service.PolicyService
    memory: Any = None                  # openajar.memory.service.MemoryService
    agenda: Any = None                  # openajar.agenda.store.AgendaStore
    coordinator: LaneCoordinator = field(default_factory=NoDaemon)
    host: Host = field(default_factory=Host)

    @property
    def home(self) -> Path:
        return Path(self.config.home)

    def close(self) -> None:
        for store in (self.workspaces, getattr(self.flows, "store", None), self.memory, self.policy, self.agenda):
            closer = getattr(store, "close", None)
            if callable(closer):
                with contextlib.suppress(Exception):
                    closer()


def node_sources(cfg: NodeConfig, lanes: Any, *, create: bool, policy: bool = False,
                 host: Host | None = None) -> Sources:
    """The node's own stores, opened from its files, the way the other CLI commands do. With
    create=False (an export) a store whose file does not exist is left out instead of created.
    The policy service is opened only when asked (it creates the node's root key)."""
    from openajar.core.runtime import Runtime
    from openajar.ops.backup import DaemonCoordinator
    from openajar.vault.service import VaultService
    from openajar.workspaces.store import WorkspaceStore

    home = Path(cfg.home)
    host = host or Host()
    src = Sources(config=cfg, lanes=lanes, vault=VaultService(home), coordinator=DaemonCoordinator(home, host),
                  host=host)
    if create or (home / "workspaces.db").exists():
        src.workspaces = WorkspaceStore(home / "workspaces.db")
    if create or (home / "runner.db").exists():
        from openajar.runner.service import build as build_runner

        runner = build_runner(Runtime(), home=home, register=False)
        src.flows, src.scheduler = runner.flows, runner.scheduler
    if policy or (not create and (home / "policy.db").exists()):
        from openajar.policy.service import PolicyService

        src.policy = PolicyService(home, node_config=cfg)
    if create or (home / "memory").is_dir():
        src.memory = _memory_service(home, src.policy, cfg)
    if create or (home / "agenda.db").exists():
        from openajar.agenda.store import DB_NAME, AgendaStore

        src.agenda = AgendaStore(home / DB_NAME)
    return src


def _memory_service(home: Path, policy: Any, cfg: NodeConfig) -> Any:
    """The node's memory over its files; with the policy, owner entries pin their evidence."""
    from openajar.core.runtime import Runtime
    from openajar.memory.ports import MemoryPorts, RuntimePolicyPort
    from openajar.memory.service import MemoryService

    runtime = Runtime(config=cfg)
    if policy is not None:
        runtime.register("policy", policy)
        runtime.register("traces", policy.traces)
    return MemoryService(home, MemoryPorts(policy=RuntimePolicyPort(runtime), node_id=cfg.name))


def runtime_sources(runtime: Any) -> Sources:
    """The daemon's own services, for the panel routes (call it on the daemon's event loop). Never
    close these Sources: the stores belong to the daemon."""
    lanes = runtime.require("lanes")
    runner = runtime.get("runner")
    return Sources(config=runtime.require("config"), lanes=lanes,
                   workspaces=getattr(runtime.get("workspaces"), "store", None), vault=runtime.get("vault"),
                   flows=runtime.get("flows"), scheduler=getattr(runner, "scheduler", None),
                   policy=runtime.get("policy"), memory=runtime.get("memory"),
                   agenda=getattr(runtime.get("agenda"), "store", None),
                   coordinator=ManagerCoordinator(lanes, asyncio.get_running_loop()),
                   host=Host())


class ManagerCoordinator:
    """Pauses lanes through a lane manager living on an event loop (the daemon, for the panel
    route), from the thread that writes the export."""

    def __init__(self, lanes: Any, loop: asyncio.AbstractEventLoop, timeout: float = 120.0):
        self.lanes, self.loop, self.timeout = lanes, loop, timeout
        self._background: set[Any] = set()

    def available(self) -> bool:
        return True

    def warm(self) -> set[str]:
        return {i.spec.id for i in self.lanes.list() if i.state.value in ("warming", "warm", "active")}

    def close(self, lane_id: str) -> None:
        future = asyncio.run_coroutine_threadsafe(self.lanes.close(lane_id), self.loop)
        try:
            future.result(self.timeout)
        except OpenajarError as e:
            raise ExportRefused(f"{lane_id} could not be paused for the export: {e.message}") from None

    def open(self, lane_id: str) -> None:
        future = asyncio.run_coroutine_threadsafe(self.lanes.open(lane_id), self.loop)
        self._background.add(future)
        future.add_done_callback(self._background.discard)
