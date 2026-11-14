"""Moved to agentos.node.camoufox. This path is the same module as agentos.node.camoufox.engine for one release."""

from agentos.core.moved import alias
from agentos.node.camoufox.config import _app_version, _oscpu  # noqa: F401  (private names other modules read)
from agentos.node.camoufox.engine import *
from agentos.node.camoufox.engine import _host_cores, _wait_for_pid  # noqa: F401  (private names other modules read)
from agentos.node.camoufox.pages import _first_line  # noqa: F401  (private names other modules read)
from agentos.node.camoufox.procs import _is_browser  # noqa: F401  (private names other modules read)

alias(__name__, "agentos.node.camoufox.engine")
