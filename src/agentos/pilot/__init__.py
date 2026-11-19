"""agentos.pilot: a cheap worker drives one lane toward one goal (browser_task).

A frontier model should not pay for every step of browser work. So it hands the node a goal and the
strings to type, and gets back one honest result. Code owns the loop, a cheap decider picks from
what code offers, the node's own tools act, and a low-confidence pick goes up a level. The design is
docs/design/browser-and-computer-agents.md sections 5 and 6.
"""

from agentos.pilot.loop import Pilot, State
from agentos.pilot.models import Limits, Request, Result, Status, Value

__all__ = ["Limits", "Pilot", "Request", "Result", "State", "Status", "Value"]
