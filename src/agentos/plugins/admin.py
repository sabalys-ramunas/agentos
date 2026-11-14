"""What only a person does: turn a plugin on or off, on the node or on one lane, and say which
brain does a job.

Only `agentos plugins` and the signed-in panel route (agentos.api.routes_worker, which refuses every
token) import this module. No tool does, and tests hold that line: an agent can read the registry,
and it cannot change it.
"""

from __future__ import annotations

import getpass
import os
from collections.abc import Mapping
from typing import Any

from agentos.core.errors import AgentosError, InvalidRequest
from agentos.core.models import LANE_ID_RE
from agentos.plugins.errors import ChangeNotRecorded, OnlyAPerson, RequirementsUnmet
from agentos.plugins.models import Change, ChangeLog, Job, Plugin
from agentos.plugins.service import LANE_WORKER, PluginService, PluginView, brain_fit
from agentos.plugins.store import Note

LANE_ENV = "AGENTOS_LANE"              # agentos.env sets it on everything a lane runs, shells included
BRAIN_TASK_ENV = "AGENTOS_BRAIN_TASK"  # agentos.brains.adapters sets it on every attempt of a brain task


def require_person(env: Mapping[str, str] | None = None) -> None:
    """Refuse a process a brain task or a lane started. This reads a variable those runners set, so
    it stops an agent that simply runs the command, and not one that clears the variable first.
    The README says what does hold then."""
    env = os.environ if env is None else env
    where = (f"brain task {env[BRAIN_TASK_ENV]}" if env.get(BRAIN_TASK_ENV) else
             f"lane {env[LANE_ENV]}" if env.get(LANE_ENV) else None)
    if where:
        raise OnlyAPerson(f"this is running inside {where}, and only a person turns a plugin on or changes who does "
                          "a job. Run it in a shell on the node, outside any lane. Nothing was changed.")


def who() -> str:
    """The person behind this command, as far as the shell can say: `sudo` keeps the name it came from."""
    try:
        return f"cli:{os.environ.get('SUDO_USER') or getpass.getuser()}"
    except (OSError, KeyError):
        return f"cli:uid {os.getuid()}"


def noter(log: ChangeLog | None, plugin_id: str, by: str | None = None) -> Note | None:
    """What the store calls inside its transaction: a record that cannot be kept undoes the change.
    `by`: who, when the caller knows better than the shell does."""
    if log is None:
        return None

    def note(**fields: Any) -> None:
        try:
            log.record(Change(plugin=plugin_id, by=by or who(), **fields))
        except AgentosError as exc:
            raise ChangeNotRecorded(f"the action log could not keep this change, so it was undone: {exc.message}") \
                from None
        except Exception as exc:  # noqa: BLE001  (whatever broke, a change with no record does not stand)
            raise ChangeNotRecorded("the action log could not keep this change, so it was undone "
                                    f"({type(exc).__name__})") from None

    return note


def enable(service: PluginService, plugin_id: str, log: ChangeLog | None = None, *,
           by: str | None = None) -> PluginView:
    """Turn it on, or say exactly why not. The state is untouched on a refusal."""
    require_person()
    state = service.state()
    plugin = service.get(plugin_id)
    unmet = [check.reason for check in service.evaluate(plugin, state) if not check.ok]
    if state.problem:
        unmet.insert(0, state.problem)
    if unmet:
        lines = "\n".join(f"  - {reason}" for reason in unmet)
        raise RequirementsUnmet(f"{plugin_id} cannot be turned on. Not met:\n{lines}")
    service.store.set_enabled(plugin_id, True, note=noter(log, plugin_id, by))
    return service.view(plugin_id)


def disable(service: PluginService, plugin_id: str, log: ChangeLog | None = None, *,
            by: str | None = None) -> PluginView:
    """Turn it off. Anyone may, from anywhere: off is the safe way, and it must always work. So a
    record that cannot be kept does not stop it, and the person is told."""
    service.get(plugin_id)
    try:
        service.store.set_enabled(plugin_id, False, note=noter(log, plugin_id, by))
    except ChangeNotRecorded as exc:
        service.store.set_enabled(plugin_id, False)
        raise ChangeNotRecorded(f"{plugin_id} is off, and the action log has no record of it: "
                                f"{exc.message.partition(': ')[2] or exc.message}") from None
    return service.view(plugin_id)


def set_lane(service: PluginService, lane: str, on: bool, log: ChangeLog | None = None, *,
             by: str | None = None) -> None:
    """Turn the worker on or off on one lane. On needs a person, as `enable` does. Off works from
    anywhere and goes through without a record, as `disable` does, and the person is told."""
    if not LANE_ID_RE.match(lane):
        raise InvalidRequest(f"{lane[:80]!r} is not a lane id (workspace/name)")
    if on:
        require_person()
        service.store.set_lane(LANE_WORKER, lane, True, note=noter(log, LANE_WORKER, by))
        return
    try:
        service.store.set_lane(LANE_WORKER, lane, False, note=noter(log, LANE_WORKER, by))
    except ChangeNotRecorded as exc:
        service.store.set_lane(LANE_WORKER, lane, False)
        raise ChangeNotRecorded(f"{LANE_WORKER} is off on {lane}, and the action log has no record of it: "
                                f"{exc.message.partition(': ')[2] or exc.message}") from None


def known_job(plugin: Plugin, job_name: str) -> Job:
    job = plugin.job(job_name)
    if job is None:
        jobs = ", ".join(job.name for job in plugin.jobs) or "none"
        raise InvalidRequest(f"{plugin.id} has no job named {job_name}. Its jobs: {jobs}")
    return job


def assign(service: PluginService, plugin_id: str, job_name: str, brain: str, log: ChangeLog | None = None, *,
           by: str | None = None) -> PluginView:
    """Give a job to a brain that can do it. A brain that cannot is refused here, and again by
    every later check, so a worker never runs on one."""
    require_person()
    job = known_job(service.get(plugin_id), job_name)
    fit = brain_fit(job, brain, service.brains())
    if not fit.ok:
        raise InvalidRequest(fit.reason)
    service.store.assign(plugin_id, job_name, brain, note=noter(log, plugin_id, by))
    return service.view(plugin_id)


def unassign(service: PluginService, plugin_id: str, job_name: str, log: ChangeLog | None = None) -> str | None:
    """Nobody does this job any more. Returns the brain that did, or None when nobody did. A
    required job left empty stops the plugin loading, and `show` says why."""
    require_person()
    known_job(service.get(plugin_id), job_name)
    return service.store.unassign(plugin_id, job_name, note=noter(log, plugin_id))
