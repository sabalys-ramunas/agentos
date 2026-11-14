"""How a pilot run that did not finish reads in `run.error.code`. Their entries are in the catalog."""

from __future__ import annotations

from agentos.core.errors import AgentosError


class PilotHeld(AgentosError):
    code = "pilot_held"


class PilotLaneUncovered(AgentosError):
    code = "pilot_lane_uncovered"


class PilotLaneOff(AgentosError):
    code = "pilot_lane_off"


class PilotNeedsHelp(AgentosError):
    code = "pilot_needs_help"


class PilotGaveUp(AgentosError):
    code = "pilot_gave_up"


class PilotRefused(AgentosError):
    code = "pilot_refused"


class PilotDayLimit(AgentosError):
    code = "pilot_day_limit"


class PilotInterrupted(AgentosError):
    code = "pilot_interrupted"
