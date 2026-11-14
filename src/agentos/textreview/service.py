"""The `text_review` service: a loop in agentosd that starts a review when the last one is older
than the interval, and the wiring a run needs, shared with `agentos text-review run`.

It loads only while the plugin is on (agentos.plugins.entries names it). Each tick asks the plugin
registry again, so a person's `agentos plugins disable text-review` stops new runs at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from agentos.core.errors import AgentosError, Conflict, ServiceUnavailable
from agentos.plugins.errors import PluginDisabled
from agentos.plugins.service import brain_fit
from agentos.textreview import evidence as ev
from agentos.textreview import review
from agentos.textreview.brain import BrainCall
from agentos.textreview.bundle import Runs
from agentos.textreview.current import collect
from agentos.textreview.settings import JOB, PLUGIN, Settings, load_settings

log = logging.getLogger("agentos.textreview")

SERVICE_NAME = "text_review"
FIRST_TICK_S = 120.0  # let the node finish starting before the first look
WORKER = "browser-worker"


def gate(plugins: Any, *, in_daemon: bool) -> None:
    """Raise plugin_disabled unless the plugin is on. In the daemon the registry also knows whether
    it loaded; a CLI process reads the state a person set."""
    if plugins is None:
        raise PluginDisabled(f"{PLUGIN} is off: this node has no plugin registry")
    if in_daemon:
        plugins.require(PLUGIN)
        return
    state = plugins.state()
    if state.problem:
        raise PluginDisabled(f"{PLUGIN} is off, because this node's plugin state could not be read. A person must "
                             "repair it on the node.")
    if PLUGIN not in state.enabled:
        raise PluginDisabled(f"{PLUGIN} is off on this node. A person turns it on with `agentos plugins enable "
                             f"{PLUGIN}` and gives its review job a brain with `agentos plugins assign {PLUGIN} "
                             f"{JOB} <brain>`.")


def assigned_brain(plugins: Any, *, in_daemon: bool) -> str:
    """The brain a person gave the review job, checked again: a brain can change after it was assigned."""
    if in_daemon:
        return plugins.brain_for(PLUGIN, JOB)
    gate(plugins, in_daemon=False)
    brain = plugins.state().brains_of(PLUGIN).get(JOB)
    if brain is None:
        raise ServiceUnavailable(f"no brain does the {JOB} job of {PLUGIN}. A person runs `agentos plugins assign "
                                 f"{PLUGIN} {JOB} <brain>`.")
    job = plugins.get(PLUGIN).job(JOB)
    fit = brain_fit(job, brain, plugins.brains())
    if not fit.ok:
        raise ServiceUnavailable(f"{PLUGIN}: {fit.reason}")
    return brain


def labeler(runtime: Any) -> Callable[[str, str], tuple[str, str]]:
    """Who was behind a session, as far as the node knows: the brain that ran a task, the brain
    that decides for the browser worker, or the client a grant was issued for."""
    brains, plugins = runtime.get("brains"), runtime.get("plugins")
    seen: dict[str, tuple[str, str]] = {}

    def label(principal: str, kind: str) -> tuple[str, str]:
        if principal in seen:
            return seen[principal]
        who, what = ev.default_label(principal, kind)
        with contextlib.suppress(Exception):  # a name is a nicety: the session is shown either way
            task = ev.BRAIN_TASK_RE.search(principal)
            if what == "brain" and task and brains is not None:
                status = brains.status(task.group(0))
                name = status.get("brain") or next((a.get("brain") for a in reversed(status.get("attempts") or [])), None)
                if name:
                    who = f"brain {name}"
            elif what == "worker" and plugins is not None:
                decide = plugins.state().brains_of(WORKER).get("decide")
                if decide:
                    who = f"browser worker, decisions by {decide}"
        seen[principal] = (who, what)
        return who, what

    return label


def notifier_of(runtime: Any) -> Callable[[str, str, dict[str, Any]], Any] | None:
    notifier = runtime.get("notifier")
    send = getattr(notifier, "notify", None)
    if not callable(send):
        return None
    return lambda title, body, data: send(title, body, kind="text_review", data=data)


def inputs(runtime: Any, *, in_daemon: bool, trigger: str, clock: Callable[[], float] = time.time,
           traces: Any = None, settings: Settings | None = None) -> review.Inputs:
    """Everything one run needs from this node. Refuses with plugin_disabled when the plugin is off."""
    home = runtime.require("config").home
    plugins = runtime.get("plugins")
    gate(plugins, in_daemon=in_daemon)
    settings = settings or load_settings(home)
    call, brain, no_brain = None, None, ""
    try:
        brain = assigned_brain(plugins, in_daemon=in_daemon)
        brains = runtime.get("brains")
        if brains is None:
            raise ServiceUnavailable("the brains service is not available on this node")
        call = BrainCall(brains, brain=brain, workspace=settings.workspace, timeout_s=settings.timeout_s,
                         drive=not in_daemon)
    except AgentosError as e:
        no_brain = e.message
    return review.Inputs(home=home, settings=settings, traces=traces if traces is not None else runtime.get("traces"),
                         current=collect(runtime, served_only=in_daemon), call=call, brain=brain, clock=clock,
                         still_on=lambda: gate(plugins, in_daemon=in_daemon), label=labeler(runtime),
                         notify=notifier_of(runtime), trigger=trigger, no_brain=no_brain)


class TextReviewService:
    def __init__(self, runtime: Any, *, clock: Callable[[], float] = time.time):
        self.runtime = runtime
        self.home = runtime.require("config").home
        self.clock = clock
        self._task: asyncio.Task[None] | None = None

    def due(self, settings: Settings) -> bool:
        last = Runs(self.home).last_started()
        return last is None or self.clock() - last >= settings.interval_days * review.DAY

    async def tick(self) -> dict[str, Any] | None:
        """Start a run when the plugin is on and the last run is older than the interval. The meta
        of the run it started, or None."""
        try:
            gate(self.runtime.get("plugins"), in_daemon=True)
        except AgentosError:
            return None
        settings = load_settings(self.home)
        if not self.due(settings):
            return None
        try:
            return await review.run(inputs(self.runtime, in_daemon=True, trigger="schedule", clock=self.clock,
                                           settings=settings))
        except Conflict:
            return None  # a person's run holds the lock; the next tick finds it done and not due

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_TICK_S)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # a run that breaks waits for the next tick, and the node carries on
                log.exception("the text review tick failed")
            await asyncio.sleep(load_settings(self.home).check_every_s)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="text-review")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def build(runtime: Any) -> TextReviewService:
    service = TextReviewService(runtime)
    runtime.register(SERVICE_NAME, service)
    return service
