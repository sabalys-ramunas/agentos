"""The `plugins` runtime service: what this node's workers are, and what they may do right now.

Everything here reads. Turning a plugin on or off, on the node or on one lane, and changing an
assignment live in openajar.plugins.admin, which only the CLI and the signed-in route import, so
nothing holding the runtime has a method for it. The daemon asks this service what to load (`to_load`), tells it what failed (`mark_failed`),
and wraps a plugin's tools with `gated`, so a call stops working as soon as a person disables the
plugin.

Privacy stays the brains router's job. A job's brain is checked for capability only.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openajar.core.errors import InvalidRequest, NotFound, ServiceUnavailable
from openajar.core.tools import ToolSpec
from openajar.plugins.entries import PLUGINS
from openajar.plugins.errors import PluginDisabled
from openajar.plugins.models import Check, Job, Plugin, Skill
from openajar.plugins.requirements import LaneKindSupported, NodeFacts
from openajar.plugins.store import PluginState, PluginStore

log = logging.getLogger(__name__)

SERVICE_NAME = "plugins"
LANE_WORKER = "browser-worker"  # the worker `openajar plugins lane` switches: the one that drives a lane


def brain_skills(spec: Any) -> frozenset[Skill]:
    """What a brain can do, from its spec's Capabilities. A decision brain answers typed questions
    and cannot run a task, so it can do nothing else."""
    caps = getattr(spec, "caps", None)
    if caps is None:
        return frozenset()
    if getattr(caps, "decisions", False):
        return frozenset({Skill.DECISION})
    return frozenset({Skill.TEXT, Skill.TOOLS} if getattr(caps, "tools", False) else {Skill.TEXT})


def brain_fit(job: Job, name: str, brains: Any) -> Check:
    """Can this brain do this job? The one rule, used by `assign`, by every check and by `brain_for`."""
    label = f"{job.name} job"
    if brains is None:
        return Check(label, False, f"the brains service is not available, so {name} cannot be checked")
    try:
        spec = brains.get_spec(name)
    except NotFound:
        return Check(label, False, f"there is no brain named {name}. `openajar brains list` shows the brains here")
    except Exception as exc:  # noqa: BLE001  (a broken brain store must read as unmet, never as met)
        return Check(label, False, f"{name} could not be read ({type(exc).__name__})")
    if not brain_skills(spec) & set(job.skills):
        return Check(label, False, f"{name} cannot do the {job.name} job, which needs {job.needs}")
    return Check(label, True, f"{name} does the {job.name} job")


@dataclass(frozen=True)
class PluginView:
    """One plugin as every surface shows it. `status` is one of:
    off (can be enabled), unavailable (cannot, see `checks`), enabled (loads at the next start),
    running (loaded in this daemon), blocked (enabled, and something it needs is gone, so it does
    not load), failed (it was loaded and did not come up)."""
    id: str
    name: str
    description: str
    stage: str
    status: str
    enabled: bool
    lane_kinds: tuple[str, ...]
    jobs: tuple[dict[str, Any], ...]
    checks: tuple[Check, ...]
    failure: str | None = None
    lanes_off: tuple[str, ...] = ()  # lanes a person turned it off on; every other lane is on

    @property
    def unmet(self) -> list[str]:
        return [check.reason for check in self.checks if not check.ok]

    def to_dict(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        out["unmet"] = self.unmet
        return out


class PluginService:
    def __init__(self, runtime: Any, *, home: Path | str, plugins: tuple[Plugin, ...] = PLUGINS,
                 facts: NodeFacts | None = None):
        self.runtime = runtime
        self.plugins = plugins
        self.store = PluginStore(home)
        self._facts = facts or NodeFacts(runtime=runtime)
        self._loaded: set[str] = set()
        self._failed: dict[str, str] = {}

    # ---- reading ----

    def get(self, plugin_id: str) -> Plugin:
        found = next((plugin for plugin in self.plugins if plugin.id == plugin_id), None)
        if found is None:
            known = ", ".join(plugin.id for plugin in self.plugins)
            raise NotFound(f"no plugin named {plugin_id}. This build ships {known}")
        return found

    def state(self) -> PluginState:
        return self.store.read()

    def brains(self) -> Any:
        return self.runtime.get("brains")

    def evaluate(self, plugin: Plugin, state: PluginState, *, running: bool = False) -> tuple[Check, ...]:
        """Every requirement, checked now: what it declares, its lane kinds, and a capable brain on
        each required job. An optional job can stay empty, but a brain given one must still fit:
        a person chose it, so it is fixed or cleared, never skipped."""
        facts = dataclasses.replace(self._facts, running=running)
        checks = [need.check(facts) for need in plugin.needs]
        checks += [LaneKindSupported(kind).check(facts) for kind in plugin.lane_kinds]
        for job in plugin.jobs:
            brain = state.brains_of(plugin.id).get(job.name)
            if brain is not None:
                checks.append(brain_fit(job, brain, self.brains()))
            elif not job.optional:
                checks.append(Check(f"{job.name} job", False, f"no brain is assigned to the {job.name} job, which "
                                                              f"needs {job.needs}"))
        return tuple(checks)

    def view(self, plugin_id: str, state: PluginState | None = None) -> PluginView:
        plugin = self.get(plugin_id)
        state = state or self.state()
        loaded = plugin.id in self._loaded
        failure = self._failed.get(plugin.id)
        checks = self.evaluate(plugin, state, running=loaded and failure is None)
        enabled = plugin.id in state.enabled and state.problem is None
        unmet = any(not check.ok for check in checks)
        if failure:
            status = "failed"
        elif enabled:
            status = "blocked" if unmet else "running" if loaded else "enabled"
        else:
            status = "unavailable" if unmet else "off"
        brains = state.brains_of(plugin.id)
        jobs = tuple({"job": job.name, "description": job.description, "needs": job.needs, "optional": job.optional,
                      "without": job.without, "brain": brains.get(job.name)} for job in plugin.jobs)
        return PluginView(id=plugin.id, name=plugin.name, description=plugin.description, stage=plugin.stage.value,
                          status=status, enabled=enabled, lane_kinds=plugin.lane_kinds, jobs=jobs, checks=checks,
                          failure=failure, lanes_off=tuple(sorted(state.off_on(plugin.id))))

    def views(self) -> list[PluginView]:
        state = self.state()
        return [self.view(plugin.id, state) for plugin in self.plugins]

    def require(self, plugin_id: str) -> Plugin:
        """The plugin, when a worker may run for this call. It reads the state each time, so a
        person's `disable` counts at once. The error tells an agent that a person has to act."""
        plugin = self.get(plugin_id)
        state = self.state()
        if state.problem:
            raise PluginDisabled(f"{plugin_id} is off, because this node's plugin state could not be read. "
                                 "A person must repair it on the node.")
        if plugin_id not in state.enabled:
            raise PluginDisabled(f"{plugin_id} is off on this node. Only a person can turn it on, on the node, with "
                                 f"`openajar plugins enable {plugin_id}`. No tool can do that.")
        if plugin_id in self._failed:
            raise ServiceUnavailable(f"{plugin_id} is enabled, but it did not start: {self._failed[plugin_id]}")
        if plugin_id not in self._loaded:
            raise ServiceUnavailable(f"{plugin_id} is enabled, but this daemon has not loaded it. It loads when "
                                     "agentosd next starts, if its requirements are met then.")
        return plugin

    def lane_stop(self, plugin_id: str, lane: str) -> str | None:
        """Why this worker may not act on this lane right now, in words, or None when it may. It
        reads the state each time, so a person's off counts at the worker's next step. A state that
        cannot be read reads as off, as it does for the node. Direct control never asks this: an
        agent's browser tools and a person's take over are not the worker."""
        state = self.state()
        if state.problem:
            return f"{plugin_id} is off, because this node's plugin state could not be read. A person must repair it on the node."
        if lane not in state.off_on(plugin_id):
            return None
        name = self.get(plugin_id).name.lower()
        return (f"A person turned the {name} off on {lane}. Agents can still drive this lane with the browser tools, "
                "or a person can take over.")

    def brain_for(self, plugin_id: str, job_name: str) -> str:
        """The brain a running worker uses for a job, checked again: a brain can change after it
        was assigned. Whether this workspace's prompts may go to it is the router's call."""
        plugin = self.require(plugin_id)
        job = plugin.job(job_name)
        if job is None:
            raise InvalidRequest(f"{plugin_id} has no job named {job_name}")
        brain = self.state().brains_of(plugin_id).get(job_name)
        if brain is None:
            raise ServiceUnavailable(f"no brain is assigned to the {job_name} job of {plugin_id}. A person must "
                                     f"run `openajar plugins assign {plugin_id} {job_name} <brain>`.")
        fit = brain_fit(job, brain, self.brains())
        if not fit.ok:
            raise ServiceUnavailable(f"{plugin_id}: {fit.reason}")
        return brain

    # ---- the daemon's side ----

    def _unmet(self, plugin: Plugin, state: PluginState, *, running: bool = False) -> list[str]:
        try:
            return [check.reason for check in self.evaluate(plugin, state, running=running) if not check.ok]
        except Exception as exc:  # noqa: BLE001  (a check that breaks must not stop the node, and must not pass)
            return [f"a check failed ({type(exc).__name__})"]

    def to_load(self) -> list[Plugin]:
        """What this start loads: enabled, known and with every requirement met right now. The state
        is only ever a list of ids, so what loads is always what a manifest in this repo names."""
        state = self.state()
        if state.problem:
            log.error("no plugin is loaded: %s", state.problem)
            return []
        ready: list[Plugin] = []
        for plugin in self.plugins:
            if plugin.id not in state.enabled:
                continue
            unmet = self._unmet(plugin, state)
            if unmet:
                log.error("plugin %s is enabled but not loaded: %s", plugin.id, "; ".join(unmet))
                continue
            self._loaded.add(plugin.id)
            ready.append(plugin)
        return ready

    def confirm(self, plugin: Plugin) -> None:
        """After its services are built: everything it needs must now really be on the runtime."""
        if plugin.id in self._failed:
            return
        unmet = self._unmet(plugin, self.state(), running=True)
        if unmet:
            self.mark_failed(plugin.id, unmet[0])

    def mark_failed(self, plugin_id: str, reason: str) -> None:
        log.error("plugin %s is not available: %s", plugin_id, reason)
        self._failed.setdefault(plugin_id, reason)

    def has_failed(self, plugin_id: str) -> bool:
        return plugin_id in self._failed

    def loaded(self) -> list[Plugin]:
        return [plugin for plugin in self.plugins if plugin.id in self._loaded and plugin.id not in self._failed]

    def service_owners(self) -> dict[str, str]:
        """Module name to plugin id, for every service this daemon built for a plugin."""
        return {module: plugin.id for plugin in self.plugins if plugin.id in self._loaded
                for module in plugin.services}

    def gated(self, plugin_id: str, spec: Any) -> Any:
        """A plugin's tool that checks the plugin on every call. A prompt passes through unchanged."""
        if not isinstance(spec, ToolSpec):
            return spec
        handler = spec.handler

        @functools.wraps(handler)
        async def guarded(*args: Any, **kwargs: Any) -> Any:
            self.require(plugin_id)
            return await handler(*args, **kwargs)

        return dataclasses.replace(spec, handler=guarded)


def build(runtime: Any) -> PluginService:
    """Registers `plugins` on the runtime. Opens nothing: the state is read when it is asked for."""
    service = PluginService(runtime, home=runtime.require("config").home)
    runtime.register(SERVICE_NAME, service)
    return service


def missing_tool(runtime: Any, name: str) -> str | None:
    """Why `name` is not served, when it is a worker's tool and the worker is not running here:
    which worker it comes with, its state, what it still needs, and what to do meanwhile. None for
    any other name (it is simply not a tool)."""
    from openajar.plugins.entries import PLUGINS as SHIPPED

    plugin = next((p for p in SHIPPED if name in p.tool_names), None)
    if plugin is None:
        return None
    worker = plugin.name if plugin.name.lower().endswith("worker") else f"{plugin.name} worker"
    lead = f"{name} comes with the {worker} ({plugin.id}), which is not running on this node"
    registry = runtime.get(SERVICE_NAME) if runtime is not None else None
    view = None
    if registry is not None:
        try:
            view = registry.view(plugin.id)
        except Exception:  # noqa: BLE001 - the hint never fails the call it explains
            view = None
    parts = [lead + (f" (it is {view.status})" if view is not None else "")]
    if view is not None and view.unmet:
        parts.append("It still needs: " + "; ".join(view.unmet))
    # the steps in the order they work: `enable` is refused until every requirement is met (QA
    # 2026-11-10, tester B 6), and an enabled worker loads when agentosd starts again
    steps = []
    unmet_jobs = {c.label for c in (view.checks if view is not None else ()) if not c.ok}
    for job in plugin.jobs:
        if f"{job.name} job" in unmet_jobs:
            steps.append(f"`openajar plugins assign {plugin.id} {job.name} BRAIN` with {job.needs} "
                         "(`openajar brains list` shows the brains, `openajar brains add` registers one)")
    if view is None or view.status in ("off", "unavailable"):
        steps.append(f"`openajar plugins enable {plugin.id}`")
    if view is None or view.status != "running":
        steps.append("restart agentosd (`sudo systemctl restart agentosd`) so the worker loads")
    numbered = ", then ".join(steps)
    parts.append(f"Tell the human: the owner runs, on the node, {numbered}. Until then, do the work yourself "
                 "with browser_snapshot and browser_act")
    return ". ".join(parts) + "."
