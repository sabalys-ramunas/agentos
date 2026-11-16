"""Shared rules of a run: the approval states, the grant codes, the twin window, the retired sources,
and what a run's trigger says (a workflow step, a schedule's fire).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol

from agentos.core import catalog
from agentos.core.models import RunStatus
from agentos.runner.execution import Journal, Outcome, RunControl
from agentos.runner.records import Run

HOLD_MESSAGES = {
    "human": "a human has control of {lane}; the run continues after they hand it back",
    "paused": "agents are held off {lane}; the run continues after that",  # dispatch words it (core.pauses)
}

APPROVED = {"approved", "granted", "allowed"}
DENIED = {"denied", "rejected"}
EXPIRED = {"expired", "cancelled", "canceled", "revoked"}
CONSUMED = {"consumed"}  # used by another run or a retried call: ask again
OWNER_GRANT = "owner"  # agentos.policy.grants.OWNER_GRANT_ID: the node owner's grant
# a keyless call identical to a run still open hands that run back (open_twin): one that asked for an
# approval or waits for its slot however long it waits, any other one queued or running for this long
TWIN_WINDOW = timedelta(hours=1)
OPEN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL)
SAME_RUN = ("The same call already has a run that has not finished, so nothing new was started and nothing "
            "asks again. Follow it with run_status. To start another one on purpose, pass a new idempotency_key.")
GRANT_CODES = {"revoked": "grant_revoked", "expired": "grant_expired", "unknown_grant": "grant_unknown",
               "not_yet_valid": "grant_not_yet_valid",
               "frozen": "grant_revoked"}  # a freeze revoked it: the message names the freeze
# Run sources that no longer run: a run of one still queued from before the upgrade (an approved
# comment of the engagement engine, which was removed, waiting for its session) is cancelled at
# dispatch, never sent at whatever hour it comes up.
RETIRED_SOURCES = frozenset({"engage"})
HOLD_BACKOFF_MAX_S = 300.0  # a held lane is re-checked at least this often (no lane is opened)
COMMIT_EFFECT_NAMES = frozenset({"commit", "pay", "irreversible"})  # agentos.policy.meters.COMMIT_EFFECTS
# a learned operation (saved_by kind `learned`) runs only for the profile sync and learner
# (agentos.profiles.sync / learner): their Run.source and principal, which no tool passes
LEARNED_SOURCE = "profiles"


def with_fix(error: dict[str, Any]) -> dict[str, Any]:
    """A run's error with the catalog's next step for an agent (`fix`), as MCP error results carry it."""
    found = catalog.entry(error.get("code"))
    if found is None or "fix" in error:
        return error
    return {**error, "fix": found.agent_step(str(error.get("message") or ""))}


class RunExecutor(Protocol):
    """Executes one kind of run (flow, brain, script). Flows are built in; other packages
    register theirs with RunnerService.register_executor."""

    async def execute(self, run: Run, journal: Journal, control: RunControl) -> Outcome: ...


def is_person(principal: dict[str, Any]) -> bool:
    return principal.get("kind") == "human" and bool(principal.get("id"))


def workflow_of(run: Run) -> str | None:
    """The workflow this run is a step of. Only the workflow engine sets a run's trigger this way."""
    trigger = run.trigger or {}
    return trigger.get("workflow") if trigger.get("kind") == "workflow" else None


SCHEDULE_TRIGGERS = frozenset({"cron", "every"})


def started_by_schedule(run: Run) -> bool:
    """Did a schedule start this run, with nobody asking: a flow's cron or interval fire, a workflow
    its schedule fired (trigger `scheduled` on its event run, set by WorkflowService.start_from_event),
    or a step of one (its trigger `scheduled`). A retry or a run by hand is not. Read from the trigger
    only, which the runner and the workflow engine set, never from a run's params: a caller writes
    those (an agent's flow_run could pass {"by": "trigger", "scheduled": true})."""
    trigger = run.trigger or {}
    if trigger.get("kind") in SCHEDULE_TRIGGERS:
        return True
    if trigger.get("kind") in ("workflow", "event"):
        return trigger.get("scheduled") is True
    return False


def bind_workflow(ctx: Any, run: Run) -> None:
    """A step of a workflow reads the workflow's own switch too, and the overlay reads what filled
    its inputs. Both can only make the call ask; neither can make it ask less. A run a schedule
    started is judged on its own: an earlier denial does not refuse it unasked (it asks again)."""
    workflow = workflow_of(run)
    if workflow:
        ctx.routine = workflow
        ctx.origins = list((run.trigger or {}).get("origins") or [])
    if started_by_schedule(run):
        ctx.scheduled = True
