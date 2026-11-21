"""What is kept per page and per browser context: the main document's status, open requests,
navigations, the lane each context is bound to and its latest signal report.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import weakref
from dataclasses import dataclass, field
from typing import Any

from openajar.core.errors import EngineError
from openajar.core.models import LaneSpec, Signal
from openajar.node.human import quietly
from openajar.node.interaction.urls import _strip_fragment
from openajar.node.sites import LaneExpectation, SignalReport, expectation_for

log = logging.getLogger("openajar.interaction")
Page = Any           # playwright.async_api.Page


LONG_REQUEST_S = 20.0      # a request open longer than this is a stream or a long poll, not page work
NET_TRACKED = 500          # requests remembered per page


class _Navigations:
    """The main frame's navigations while a link is followed, told apart by where they lead.
    `began` resolves when a navigation request starts (or a script shows the target in the same
    document); `landed` when the latest one commits, or with an EngineError when it fails. `ours`:
    it started at the target (fragment aside) or committed there. A same-document address change to
    anything else (a script tidying the address bar) is not the link's navigation and is ignored."""

    def __init__(self, page: Page, target: str, want: str):
        loop = asyncio.get_running_loop()
        self.page, self.main, self.target, self.want = page, page.main_frame, target, want
        self.began: asyncio.Future[None] = loop.create_future()
        self.landed: asyncio.Future[None] = loop.create_future()
        self.chain: list[Any] = []  # the latest navigation's requests, redirects included
        self.start = ""
        self.ours = False

    def on_request(self, request: Any) -> None:
        if self.landed.done() or not _is_main_navigation(request, self.main):
            return
        try:
            url, previous = str(request.url), request.redirected_from
        except Exception:  # noqa: BLE001
            return
        if previous is not None and any(previous is r for r in self.chain):
            self.chain.append(request)  # a redirect: the same navigation, now somewhere else
            return
        self.chain, self.start = [request], url  # a new navigation replaces one still in flight
        if not self.began.done():
            self.began.set_result(None)

    def on_navigated(self, frame: Any) -> None:
        if frame != self.main or self.landed.done():
            return
        url = _strip_fragment(_url_of(self.page))
        if self.chain:
            if url != _strip_fragment(str(getattr(self.chain[-1], "url", ""))):
                return  # not the commit of the navigation in flight (a pushState while it loads)
            self.ours = _strip_fragment(self.start) == self.want or url == self.want
        elif url == self.want:
            self.ours = True  # a script's router showed the target in this document, as a person sees it
            if not self.began.done():
                self.began.set_result(None)
        else:
            return
        self.landed.set_result(None)

    def on_failed(self, request: Any) -> None:
        if self.landed.done() or not any(request is r for r in self.chain):
            return
        self.ours = _strip_fragment(self.start) == self.want
        self.landed.set_exception(EngineError(f"navigation to {self.target} failed: "
                                              f"{getattr(request, 'failure', None) or 'the request failed'}"))


@dataclass(slots=True)
class _PageState:
    status: int | None = None
    status_url: str | None = None
    inflight: dict[Any, float] = field(default_factory=dict)  # request -> when it started (monotonic)
    last_request: float = 0.0  # when a request last started or ended

    def request_started(self, request: Any) -> None:
        if _resource_type(request) in _STREAMS:
            return
        now = time.monotonic()
        self.inflight[request] = self.last_request = now
        while len(self.inflight) > NET_TRACKED:
            self.inflight.pop(next(iter(self.inflight)))

    def request_ended(self, request: Any) -> None:
        if self.inflight.pop(request, None) is not None:
            self.last_request = time.monotonic()

    def network(self, now: float | None = None) -> tuple[int, float]:
        """(requests in flight that count, seconds since a request started or ended). Requests
        open longer than LONG_REQUEST_S do not count: streams and long polls never end."""
        now = time.monotonic() if now is None else now
        busy = sum(1 for started in self.inflight.values() if now - started < LONG_REQUEST_S)
        return busy, (now - self.last_request) if self.last_request else math.inf


_STREAMS = frozenset({"eventsource", "websocket", "media"})


def _resource_type(request: Any) -> str:
    try:
        return str(request.resource_type)
    except Exception:  # noqa: BLE001
        return ""


# Per page, not per PlaywrightInteraction, so a page never gets a second response listener.
_PAGES: weakref.WeakKeyDictionary[Any, _PageState] = weakref.WeakKeyDictionary()


# ---- lanes: what a context expects, and its latest signal ---------------------------------------

# Keyed by browser context (one per lane), so popups and new tabs share their lane's expectation.
_EXPECT: weakref.WeakKeyDictionary[Any, LaneExpectation] = weakref.WeakKeyDictionary()
_REPORTS: weakref.WeakKeyDictionary[Any, SignalReport] = weakref.WeakKeyDictionary()


def _context_of(page_or_context: Any) -> Any:
    return getattr(page_or_context, "context", page_or_context)


def bind_lane(page_or_context: Any, spec: LaneSpec | LaneExpectation | None) -> None:
    """Tell the signal which lane a browser context belongs to: the sites it should be signed into
    and the pinned account (sites.expectation_for). Call it again when the spec changes; None unbinds.
    An unbound context still gets every signal except logged_out from a missing session."""
    context = _context_of(page_or_context)
    try:
        if spec is None:
            _EXPECT.pop(context, None)
        else:
            _EXPECT[context] = spec if isinstance(spec, LaneExpectation) else expectation_for(spec)
    except TypeError:  # not weak-referenceable (a test double)
        log.debug("cannot bind %r to a lane", context)


def lane_expectation(page_or_context: Any) -> LaneExpectation | None:
    try:
        return _EXPECT.get(_context_of(page_or_context))
    except TypeError:
        return None


def last_report(page_or_context: Any) -> SignalReport | None:
    """The latest signal computed for this lane's context (by any tool, flow or login step)."""
    try:
        return _REPORTS.get(_context_of(page_or_context))
    except TypeError:
        return None


def take_report(page_or_context: Any) -> SignalReport | None:
    """The latest signal for this context, cleared, so the next lane use starts fresh. The lane
    manager reads it when a use ends: the page state the use left behind decides a hold, so a login
    step that passes through the login page and ends signed in never holds the lane."""
    try:
        return _REPORTS.pop(_context_of(page_or_context), None)
    except TypeError:
        return None


def _remember(page: Any, report: SignalReport) -> SignalReport:
    """Keep the latest report for the lane manager. An unknown read (a dialog froze the page) never
    replaces a signal already seen in this lane use."""
    try:
        context = _context_of(page)
        if report.signal is not Signal.UNKNOWN or context not in _REPORTS:
            _REPORTS[context] = report
    except TypeError:
        pass
    return report


async def _read_cookies(page: Any, url: str) -> list[dict[str, Any]] | None:
    """The cookies the browser would send to `url`, from the context (the page cannot see this)."""
    try:
        return await page.context.cookies([url])
    except Exception:  # noqa: BLE001 - a closed context, or a page double with no cookie jar
        return None


def _url_of(page: Any) -> str:
    try:
        return str(page.url) if page is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _open_pages(page: Any) -> list[Any]:
    try:
        return [p for p in page.context.pages if not p.is_closed()]
    except Exception:  # noqa: BLE001
        return []


async def _title(page: Page) -> str:
    return await quietly(page.title(), "")


def _is_main_navigation(request: Any, main: Any) -> bool:
    try:
        return bool(request.is_navigation_request()) and request.frame == main
    except Exception:  # noqa: BLE001 - e.g. a service worker request has no frame
        return False


def _consume(task: asyncio.Task) -> None:
    """Retrieve the outcome of a page call we stopped waiting for, so asyncio does not warn."""
    if not task.cancelled():
        task.exception()
