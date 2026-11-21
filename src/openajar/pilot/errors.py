"""How a pilot run that did not finish reads in `run.error.code`. Their entries are in the catalog."""

from __future__ import annotations

from openajar.core.errors import OpenajarError


class PilotHeld(OpenajarError):
    code = "pilot_held"


class PilotLaneUncovered(OpenajarError):
    code = "pilot_lane_uncovered"


class PilotLaneOff(OpenajarError):
    code = "pilot_lane_off"


class PilotNeedsHelp(OpenajarError):
    code = "pilot_needs_help"


class PilotGaveUp(OpenajarError):
    code = "pilot_gave_up"


class PilotRefused(OpenajarError):
    code = "pilot_refused"


class PilotDayLimit(OpenajarError):
    code = "pilot_day_limit"


class PilotInterrupted(OpenajarError):
    code = "pilot_interrupted"
