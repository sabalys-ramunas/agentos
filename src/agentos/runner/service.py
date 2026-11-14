"""Composition: build(runtime) creates the runner, flows, recorder and scheduler.

Registers four runtime services:
  runner    RunnerService   (durable runs; `.scheduler` holds schedules and triggers, `.health`
                             the connector operations' health)
  flows     FlowService     (saved flow versions; `.recorder` turns actions into drafts)
  surfaces  SurfaceService  (AUD-01 surfaces: who connector runs act as, and on which lane)
  workflows WorkflowService (a trigger, then steps: docs/WORKFLOWS.md; `.sends`, `.mail`)

Every finished connector run updates its operation's health on its surface and labels its
grant family with the taint its output carries (agentos.runner.connectors.health, .taint).

The daemon calls `await runner.start()` after the lanes are up and `await runner.stop()`
before closing them. The driver comes from `driver_factory=` or, lazily, the runtime
service "flow_driver" (a DriverFactory, see agentos.core.driver).
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from agentos.core.config import home_from_env
from agentos.core.driver import DriverFactory
from agentos.policy.scrub import runtime_scrub
from agentos.runner.connectors.health import ConnectorHealth
from agentos.runner.connectors.surfaces import SurfaceService
from agentos.runner.connectors.taint import record_run
from agentos.runner.flows import FlowService
from agentos.runner.recorder import Recorder
from agentos.runner.runner import RunnerService
from agentos.runner.schedules import Scheduler
from agentos.runner.siteapis import service as siteapis
from agentos.runner.store import RunnerStore
from agentos.runner.util import Clock
from agentos.runner.workflows import service as workflows

DB_NAME = "runner.db"


def build(runtime: Any, *, home: Path | None = None, driver_factory: DriverFactory | None = None,
          clock: Clock | None = None, rng: random.Random | None = None, register: bool = True,
          **options: Any) -> RunnerService:
    cfg = runtime.get("config") if runtime is not None else None
    options_sites = options.pop("sites", None)  # the site table for surfaces (tests: a fake site)
    options_transport = options.pop("mail_transport", None)  # how mail travels (tests: a fake)
    home = Path(home or (cfg.home if cfg is not None else home_from_env()))
    clock = clock or Clock()
    store = RunnerStore(home / DB_NAME)
    flows = FlowService(store, clock)
    flows.vault_scrub = runtime_scrub(runtime) if runtime is not None else None
    flows.file_digest = _inbox_digest(runtime)
    flows.recorder = Recorder(store, clock)
    if runtime is not None:  # "Ask me first" lives in the policy (agentos.policy.ask_first)
        flows.ask_first_source = lambda: runtime.get("ask_first")
    if "max_concurrent" not in options and cfg is not None:
        # leave one warm slot free for interactive agent work
        options["max_concurrent"] = max(1, min(2, cfg.max_warm_lanes - 1))
    runner = RunnerService(store, flows, runtime=runtime, driver_factory=driver_factory, clock=clock, rng=rng,
                           shots_dir=home / "runner" / "shots", ref_base=home, **options)
    runner.scheduler = Scheduler(store, runner, flows, runtime=runtime, clock=clock)
    runner.surfaces = SurfaceService(store, flows, clock=clock, runtime=runtime, sites=options_sites)
    runner.health = ConnectorHealth(store, clock=clock, notify=runner._notify, current=lambda fid: _head(flows, fid))
    runner.on_finished.append(runner.health.record)
    runner.on_finished.append(lambda run: record_run(runtime, run))
    workflows.build(runner, runtime if register else None, transport=options_transport)
    siteapis.build(runner, runtime if register else None, sites=options_sites)
    if register and runtime is not None:
        runtime.register("flows", flows)
        runtime.register("runner", runner)
        runtime.register("surfaces", runner.surfaces)
    return runner


def _head(flows: FlowService, flow_id: str) -> str | None:
    fv = flows.store.get_flow(flow_id)
    return fv.version if fv is not None else None


def _inbox_digest(runtime: Any):
    """(lane, file ref) -> "sha256:<hex>" of the inbox file an upload step would send (the same lookup
    the flow driver makes), for the approval to bind the file's bytes."""
    import hashlib

    def digest(lane: str, ref: str) -> str | None:
        from agentos.node.flow_driver import inbox_dir, resolve_inbox_file

        files = runtime.get("files") if runtime is not None else None
        path = resolve_inbox_file(inbox_dir(runtime, lane), ref, files=files, lane_id=lane)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()

    return digest
