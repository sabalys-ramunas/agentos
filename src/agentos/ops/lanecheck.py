"""Moved to agentos.stealth.lanecheck. This path is the same module for one release."""

from agentos.core.moved import alias
from agentos.stealth.lanecheck import *
from agentos.stealth.lanecheck import _camou_prefs, _stat  # noqa: F401  (private names other modules read)

alias(__name__, "agentos.stealth.lanecheck")
