"""The review browser's failures (studio-ax.md, rule 6): a page that did not load, an error page, a
blank page, a theme that did not apply and a state that was not reached are failures of the call,
never a page with zero findings. Each code has an entry in src/agentos/core/error_catalog.toml, and
each message ends with what to do next. `details` carries the same as data (the url, the status,
the step), so a tool can put it in its result.

An engine that is not installed is `unavailable` (rule 16), with the reason `engine_missing` and
the command that installs it.
"""

from __future__ import annotations

from typing import Any

from agentos.core.errors import AgentosError, DeadlineExceeded, ServiceUnavailable


class StudioError(AgentosError):
    """A review browser call that failed. Every subclass sets its own code."""

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = {key: value for key, value in details.items() if value is not None}


class PageNotLoaded(StudioError):
    """The browser could not reach the url (refused, no such host, a timeout)."""
    code = "studio_page_not_loaded"


class HttpErrorPage(StudioError):
    """The url answered with an HTTP error (4xx or 5xx)."""
    code = "studio_http_error"


class BlankPage(StudioError):
    """The page loaded but shows nothing: no text, image or control is visible."""
    code = "studio_blank_page"


class ThemeNotApplied(StudioError):
    """The theme switch was applied but the page did not take it (read back after settling)."""
    code = "studio_theme_not_applied"


class StateNotReached(StudioError):
    """A step on the way to a named state failed."""
    code = "studio_state_failed"


class StudioTimeout(StudioError, DeadlineExceeded):
    """The call used up its time limit before the page was ready."""
    code = "studio_timeout"


class EngineMissing(ServiceUnavailable):
    """The engine (or playwright itself) is not installed. Code `unavailable`, reason
    `engine_missing`: the catalog's agent step for that reason tells the agent what to do."""

    def __init__(self, message: str, *, engine: str, install: str):
        super().__init__(f"engine_missing: {message} Install it with `{install}`.")
        self.engine = engine
        self.install = install
        self.details = {"engine": engine, "install": install}
