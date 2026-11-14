"""Moved to agentos.stealth.stealth. This path is the same module for one release."""

from agentos.core.moved import alias
from agentos.stealth.stealth import *
from agentos.stealth.stealth import _exit_geo  # noqa: F401  (private names other modules read)

alias(__name__, "agentos.stealth.stealth")
