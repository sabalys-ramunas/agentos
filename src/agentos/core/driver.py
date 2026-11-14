"""The FlowDriver protocol: how a flow touches a browser lane.

The runner never imports Playwright or the node's interaction module. The lead implements
FlowDriver on top of agentos.node.interaction (human input: wheel scroll, pauses, a random
point inside the target, a real press, typing at human rhythm) and hands the runner a
DriverFactory, an async context manager per lane:

    @asynccontextmanager
    async def flow_driver(lane_id: str):
        async with lanes.using(lane_id) as page:     # warms the lane, holds its lock
            yield NodeFlowDriver(page, interaction)

The factory may raise LaneBusy or LaneQuarantined on enter; the runner requeues the run
with backoff. Locator resolution must follow the rules in `Locator`'s docstring
(agentos.core.locator), and agentos.runner.fakes.resolve_all / resolve_one are the reference implementation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from agentos.core.errors import AgentosError
from agentos.core.locator import Locator
from agentos.core.models import NavigateResult, Signal

Element = Any  # an opaque handle returned by find(); only passed back to the same driver


class ElementState(BaseModel):
    """The target's own state, read without changing the page.

    count:      matches for the locator under the scope rules, ignoring nth
    found:      the locator resolved to exactly one element (after nth)
    ambiguous:  more than one match and no nth, so attribute/text reads are refused
    text:       the element's own visible text, EXCLUDING text inside nested elements that
                have the same role as the element (a post's text never includes its comments)
    lines:      the same text with its line breaks kept (a br, a block's edges, newlines where
                the page keeps them), for a check that compares lines (a profile's About)
    value:      form value (input, textarea, select, contenteditable text)
    attributes: the element's own attributes (aria-pressed, aria-checked, href, data-*)
    url:        the page URL at read time
    """

    count: int = 0
    found: bool = False
    ambiguous: bool = False
    text: str | None = None
    lines: str | None = None
    value: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    url: str = ""


class DriverError(AgentosError):
    """A driver failure. `transient` errors (timeouts, a dropped connection) are retried
    when it is safe to do so."""

    code = "driver_error"
    transient = False

    def __init__(self, message: str, *, transient: bool | None = None):
        super().__init__(message)
        if transient is not None:
            self.transient = transient


class ElementNotFound(DriverError):
    code = "element_not_found"


class AmbiguousLocator(DriverError):
    code = "ambiguous_locator"

    def __init__(self, message: str, *, count: int):
        super().__init__(message)
        self.count = count


class DriverTimeout(DriverError):
    code = "timeout"
    transient = True


MAX_JSON_BYTES = 5_000_000  # of one response body, held in memory only while a step reads it


@dataclass(slots=True)
class JsonResponse:
    """One response a page's own code caused. `body` is None for a status other than 2xx, or with
    `problem`: too_large (over MAX_JSON_BYTES), unreadable (the browser no longer has it) or slow (it
    did not finish in time). A body
    lives in memory only: nothing may log, store or show this object, so its repr leaves the body out."""

    url: str
    status: int
    body: bytes | None = field(default=None, repr=False)
    problem: str | None = None


class ResponseWatch(Protocol):
    """What FlowDriver.watch_json hands back. `responses` fills as bodies arrive."""

    @property
    def responses(self) -> list[JsonResponse]: ...

    def close(self) -> None:
        """Stop listening and drop every body held."""


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def parse_json(body: bytes) -> Any:
    """Strict JSON, or a ValueError: no NaN or Infinity, and nesting no deeper than the parser's stack."""
    try:
        return json.loads(body, parse_constant=_no_constant)
    except RecursionError as e:
        raise ValueError("nested too deep") from e


class FlowDriver(Protocol):
    async def navigate(self, url: str) -> NavigateResult:
        """Load the URL; report the final URL, HTTP status and the page signal."""

    async def current_url(self) -> str: ...

    async def find(self, locator: Locator, *, timeout_ms: int = 0) -> Element:
        """Wait up to timeout_ms for the locator to resolve to exactly one element.
        Raises ElementNotFound, AmbiguousLocator, or DriverTimeout."""

    async def click(self, element: Element) -> None: ...

    async def type(self, element: Element, text: str) -> None:
        """Type at human rhythm, appending to what is there."""

    async def fill(self, element: Element, text: str) -> None:
        """Clear the field, then type."""

    async def select(self, element: Element, option: str) -> None:
        """Pick an option by its visible label."""

    async def press(self, key: str, element: Element | None = None) -> None: ...

    async def scroll_to(self, element: Element) -> None:
        """Wheel-scroll until the element is comfortably in view."""

    async def upload(self, element: Element, file_ref: str) -> None:
        """Set a file input from a path on the node or a files ref."""

    async def read_state(self, locator: Locator) -> ElementState:
        """Read the target's own state (see ElementState). Never raises for zero or many
        matches; reports them in count/found/ambiguous instead."""

    async def extract_items(self, items: Locator, fields: dict[str, Any], max_items: int,
                            pick: str | None = None) -> tuple[list[dict[str, str | None]], int]:
        """(records, total): every element `items` resolves to under the scope rules (no
        ambiguity error; `nth` keeps one) and `pick` (ExtractStep), in page order, the first `max_items` read into a
        record of raw strings, and how many matched in all. Per field (an ExtractField): the
        item, or the one element `field.locator` finds inside it (the item is its anchor; zero
        or several matches give None), then `attr` (the attribute, None if absent) or `read`:
        own_text (as ElementState.text), text (all visible text, nested roles too), value (as
        ElementState.value). Raises ElementNotFound / AmbiguousLocator for a bad anchor only.
        Never marks the page."""

    def watch_json(self, wanted: Callable[[str], bool], *, keep: int = 1, only_json: bool = False) -> ResponseWatch:
        """Listen, from now until close(), to the responses of the fetch and XHR requests the page's own
        code makes after its next navigation commits, and keep the first `keep` whose URL `wanted` accepts
        (`only_json`: and whose content type says JSON). A redirect hop is not a response here. Passive:
        no request is made, repeated, altered or delayed, no header is sent, and no route is added."""

    async def account(self, site: str | None = None, domain: str | None = None) -> dict[str, str] | None:
        """{site, user_id, source} for the account the lane is signed into on `site` (a
        agentos.node.sites key; None: the site of `domain`, else of the current page), read
        from the site's identity cookie without a request. None when signed out or when the
        site shows no id without a request."""

    async def outline(self) -> str:
        """The page's accessibility outline, as browser_snapshot reads it. A site API's discovery
        works from this, so it runs no JavaScript in the page."""

    async def screenshot(self) -> bytes: ...

    async def signal(self) -> Signal:
        """Deterministic page state (captcha, logged_out, ...), never a model's guess."""


DriverFactory = Callable[[str], AbstractAsyncContextManager[FlowDriver]]
