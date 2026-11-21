"""A flow on a lane a human may take: the control gate checked before each step's input, the
lane held as a flow principal, and take over as a pause for flow runs.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from openajar.core.errors import AgentPaused, HumanInControl, LaneBusy
from openajar.core.models import ControlState
from openajar.core.pauses import pause_why

# ---------------------------------------------------------------- errors


class LaneTakenOver(HumanInControl, LaneBusy):
    """A human has control of the lane. Raised before any input of the step, so nothing
    happened; a LaneBusy too, so a runner that has not learned about take over defers the run."""


class LaneAgentPaused(AgentPaused, LaneBusy):
    """Agents are held off the lane (a person stopped them, or openajar paused it). Raised before any input of the step."""


TAKE_OVER_CODES = frozenset({HumanInControl.code, AgentPaused.code})


Gate = Callable[[], None]


# ---------------------------------------------------------------- lanes: the factory and the gate


def control_gate(lanes: Any, lane_id: str) -> Gate:
    """A check that raises LaneTakenOver / LaneAgentPaused when a human holds the lane."""

    def state() -> tuple[ControlState | None, Any]:
        try:
            fn = getattr(lanes, "control", None)
            control = fn(lane_id) if callable(fn) else getattr(lanes.get(lane_id), "control", None)
        except Exception:  # noqa: BLE001 - no control record: nothing to enforce here
            return None, None
        st = getattr(control, "state", None)
        return (ControlState(st) if st is not None else None), control

    def check() -> None:
        st, control = state()
        if st == ControlState.HUMAN:
            raise LaneTakenOver(f"a human has control of {lane_id}; the flow waits and continues after "
                                "they hand it back")
        if st == ControlState.PAUSED:
            raise LaneAgentPaused(f"{pause_why(lane_id, control)} The flow continues after that.")

    return check


def _using(lanes: Any, lane_id: str) -> Any:
    """The lane as a flow principal. Inside a run (the runner's log context names it), the run's
    implicit claim for the whole session; outside one (a page watch, a re-check), one act."""
    from openajar.core import logs
    from openajar.node.claims import RUN, Holder

    ctx = logs.current()
    run_id = ctx.get("run")
    try:
        if run_id:
            holder = Holder.for_run(run_id)
            try:
                return lanes.using(lane_id, principal_kind="flow", holder=holder, mode=RUN,
                                   purpose=f"flow run {run_id}")
            except TypeError:  # a lane manager from before shared lanes
                pass
        return lanes.using(lane_id, principal_kind="flow")
    except TypeError:  # a lane manager from before take over: no principal_kind
        return lanes.using(lane_id)


class TakeOverPause:
    """Wraps the runner's flow executor: a run stopped by take over (or by a pause) in the
    middle defers, to continue after the hand back, instead of failing. The stop is raised
    before the step's input, so the step did not happen and runs again on resume."""

    def __init__(self, inner: Any):
        self.inner = inner

    async def execute(self, run: Any, journal: Any, control: Any) -> Any:
        outcome = await self.inner.execute(run, journal, control)
        err = outcome.error or {}
        if (outcome.action in ("finish", "retry", "decide") and err.get("code") in TAKE_OVER_CODES
                and err.get("acted") is not True):
            reason = err.get("message") or "a human has control of the lane"
            return dataclasses.replace(outcome, action="defer", status=None, error=None, reason=reason, path=None)
        return outcome


def install_take_over_pause(runner: Any) -> None:
    """Make take over a pause for flow runs on this runner (idempotent)."""
    from openajar.core.models import RunKind

    inner = runner.executors.get(RunKind.FLOW)
    if inner is not None and not isinstance(inner, TakeOverPause):
        runner.register_executor(RunKind.FLOW, TakeOverPause(inner))
