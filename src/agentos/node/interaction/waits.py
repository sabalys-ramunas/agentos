"""wait and settle: hold the lane for a bounded time until conditions hold together (a text, a
ref's state, a quiet network, the URL). It only reads.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from agentos.core.errors import InvalidRequest, RefNotFound
from agentos.core.models import Signal
from agentos.core.signals import STOP
from agentos.node.human import quietly
from agentos.node.interaction.actions import RESOLVE_TIMEOUT_MS
from agentos.node.interaction.dialogs import PageDialog, _a, _DialogOpened, _pending_error, dialog_guard
from agentos.node.interaction.pages import _PageState, _url_of
from agentos.node.interaction.readback import _state, diff_state
from agentos.node.interaction.urls import normalize_ref
from agentos.node.sites import SignalReport
from agentos.node.snapshot import SNAPSHOT_TIMEOUT_MS, find_text, outline_texts

Page = Any           # playwright.async_api.Page
ElementHandle = Any  # playwright.async_api.ElementHandle


WAIT_MAX_S = 60.0          # a wait never holds the lane longer than this
WAIT_POLL_MS = 400         # between checks; a page whose tree takes longer to read is read less often
WAIT_SIGNAL_EVERY_S = 2.0  # how often a wait re-reads the page's signal
SETTLE_QUIET_MS = 500
SETTLE_MAX_S = 2.5
READY_STATE_S = 2.0
WAIT_REPORT_S = 5.0        # the signal read that ends a wait, bounded like the wait itself
WAIT_READ_GRACE_S = 0.5    # a read a wait makes may finish this long after the wait's deadline
_UNANSWERED = object()     # a read the page did not answer in time (not the same as "gone")


# ---- waits --------------------------------------------------------------------------------

REF_STATES = ("visible", "hidden", "enabled", "disabled", "checked", "unchecked", "pressed", "unpressed",
              "expanded", "collapsed", "selected", "focused", "changed")
STOP_SIGNALS = STOP

# Whether the target renders and has focus. Reads only, in the isolated world.
_VIEW_JS = """
(el) => {
  if (!el.isConnected) return null;
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return {visible: r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none',
          focused: el.ownerDocument.activeElement === el};
}
"""


@dataclass(frozen=True)
class WaitFor:
    """What a wait waits for. Every condition given must hold at the same time."""
    for_text: tuple[str, ...] = ()   # any one of these appears
    gone_text: tuple[str, ...] = ()  # every one of these is gone
    exact: bool = False              # a text must equal a whole name or text, not just be part of one
    ref: str | None = None
    ref_state: str | None = None     # one of REF_STATES
    network_quiet_ms: int | None = None
    url_matches: str | None = None   # part of the URL, or a glob over the whole URL when it has a *
    timeout_s: float = 10.0

    @property
    def reads_tree(self) -> bool:
        return bool(self.for_text or self.gone_text)

    @property
    def empty(self) -> bool:
        return not (self.reads_tree or self.ref or self.network_quiet_ms is not None or self.url_matches)


class WaitResult(BaseModel):
    met: bool
    stopped: str           # met | timeout | dialog | signal | tab_closed | human_in_control | agent_paused
    waited_s: float
    url: str
    conditions: list[dict[str, Any]]
    reread: bool = False   # the tree was re-read like a snapshot: elements that changed have new refs
    dialog: dict[str, Any] | None = None
    signal: Signal = Signal.UNKNOWN
    detail: str | None = None
    signal_detail: str | None = None
    account: dict[str, Any] | None = None


def url_matches(url: str, pattern: str) -> bool:
    """Part of the URL, or a glob over the whole URL when the pattern has a `*`."""
    return fnmatch.fnmatchcase(url, pattern) if "*" in pattern else pattern in url


def ref_state_met(state: str, before: dict[str, str], now: dict[str, str] | None,
                  view: dict[str, Any] | None) -> tuple[bool, str]:
    """Whether the target is in `state`, and what it is now (for the result)."""
    if now is None or view is None:
        return state == "hidden", "gone from the page"
    if state == "changed":
        changes = diff_state(before, now)
        return bool(changes), "; ".join(changes) or "no change in its own state"
    flags = {
        "visible": bool(view.get("visible")),
        "enabled": not (now.get("disabled") == "true" or now.get("aria-disabled") == "true"),
        "checked": now.get("checked") == "true" or now.get("aria-checked") == "true",
        "pressed": now.get("aria-pressed") == "true",
        "expanded": now.get("aria-expanded") == "true",
        "selected": now.get("aria-selected") == "true",
        "focused": bool(view.get("focused")),
    }
    opposite = {"hidden": "visible", "disabled": "enabled", "unchecked": "checked", "unpressed": "pressed",
                "collapsed": "expanded"}
    met = not flags[opposite[state]] if state in opposite else flags[state]
    seen = ["visible" if flags["visible"] else "hidden", "enabled" if flags["enabled"] else "disabled"]
    if "checked" in now or "aria-checked" in now:
        seen.append("checked" if flags["checked"] else "unchecked")
    for key, yes, no in (("aria-pressed", "pressed", "unpressed"), ("aria-expanded", "expanded", "collapsed"),
                         ("aria-selected", "selected", "not selected")):
        if key in now:
            seen.append(yes if flags[yes] else no)
    if flags["focused"]:
        seen.append("focused")
    return met, ", ".join(seen)


class _Watch:
    """What a wait has seen so far, kept outside the loop so a dialog or a timeout that stops the
    loop still reports it."""

    def __init__(self, spec: WaitFor, ref: str | None, before: dict[str, str] | None):
        self.spec, self.before = spec, before or {}
        self.reread = False
        self.conditions: dict[str, dict[str, Any]] = {}
        if spec.for_text:
            self.conditions["for_text"] = {"for_text": list(spec.for_text), "met": False, "seen": None}
        if spec.gone_text:
            self.conditions["gone_text"] = {"gone_text": list(spec.gone_text), "met": False, "seen": None}
        if ref is not None:
            self.conditions["for_ref_state"] = {"for_ref_state": {"ref": ref, "state": spec.ref_state},
                                                "met": False, "seen": None}
        if spec.network_quiet_ms is not None:
            self.conditions["network_quiet_ms"] = {"network_quiet_ms": spec.network_quiet_ms, "met": False,
                                                   "seen": None}
        if spec.url_matches is not None:
            self.conditions["url_matches"] = {"url_matches": spec.url_matches, "met": False, "seen": None}

    def set(self, key: str, met: bool, seen: str | None, **extra: Any) -> None:
        entry = self.conditions[key]
        entry.update(met=met, seen=seen, **extra)

    @property
    def met(self) -> bool:
        return all(c["met"] for c in self.conditions.values())

    def unmet(self) -> str:
        parts = []
        for key, c in self.conditions.items():
            if not c["met"]:
                parts.append(f"{key} not met" + (f" (now: {c['seen']})" if c.get("seen") else ""))
        return "; ".join(parts)


class WaitMixin:
    """wait and settle: hold the lane until conditions on the page hold, reading only."""

    async def wait(self, page: Page, spec: WaitFor, *,
                   should_stop: Callable[[], str | None] | None = None) -> WaitResult:
        """Wait until every condition in `spec` holds, for at most `spec.timeout_s` (capped at
        WAIT_MAX_S). Returns what each condition saw last and the page's signal. A dialog that opens
        stops the wait and is reported in `dialog`; a challenge signal, the tab closing or
        `should_stop()` returning a reason (a human took the lane) stop it too."""
        if spec.empty:
            raise InvalidRequest("nothing to wait for: give for_text, gone_text, for_ref_state, network_quiet_ms "
                                 "or url_matches")
        if spec.ref is not None and spec.ref_state not in REF_STATES:
            raise InvalidRequest(f"unknown ref state {spec.ref_state!r}; use one of: {', '.join(REF_STATES)}")
        timeout = float(spec.timeout_s)
        if math.isnan(timeout) or timeout < 0:
            raise InvalidRequest("timeout_s must be 0 or more seconds")
        timeout = min(timeout, WAIT_MAX_S)
        state = self._track(page)
        guard = dialog_guard(page)
        if guard is not None and (open_dialog := guard.pending_on(page)) is not None:
            raise _pending_error(open_dialog, "wait")
        loop = asyncio.get_running_loop()
        started = loop.time()  # the deadline covers reading the ref too: a page that never answers holds the lane
        deadline = started + timeout
        ref = handle = before = None
        unanswered = False
        if spec.ref is not None:
            ref = normalize_ref(spec.ref)
            try:
                async with asyncio.timeout_at(deadline + WAIT_READ_GRACE_S):
                    handle = await self._resolve(page, ref)
                    before = await _state(handle)
            except TimeoutError:
                unanswered = True
            else:
                if before is None:
                    raise RefNotFound(f"ref {ref} is no longer on the page. Take a new snapshot.")

        watch = _Watch(spec, ref, before)
        dialog: PageDialog | None = None
        if unanswered:
            stopped = "timeout"
            watch.set("for_ref_state", False, "the page did not answer when its state was read")
        else:
            try:
                stopped = await asyncio.wait_for(
                    self._race(page, self._wait_loop(page, watch, state, handle, deadline, should_stop), guard),
                    max(0.0, deadline - loop.time()) + RESOLVE_TIMEOUT_MS / 1000)
            except _DialogOpened as opened:
                stopped, dialog = "dialog", opened.dialog
            except TimeoutError:
                stopped = "timeout"
        waited = round(loop.time() - started, 1)
        if page.is_closed():
            stopped = "tab_closed"
            report = SignalReport(Signal.UNKNOWN, "the tab closed", _url_of(page))
        else:
            try:
                report = await asyncio.wait_for(self.report(page), WAIT_REPORT_S)
            except TimeoutError:
                report = SignalReport(Signal.UNKNOWN, "the page did not answer", _url_of(page))
        met = stopped == "met"
        if met:
            detail = f"met after {waited:g} s"
        elif stopped == "timeout":
            detail = f"not met within {timeout:g} s: {watch.unmet()}"
        elif dialog is not None:
            detail = (f"{_a(dialog.type)} {dialog.type} dialog opened after {waited:g} s: "
                      f"{json.dumps(dialog.message[:300], ensure_ascii=False)}. The page is frozen until it is "
                      f"answered: call browser_dialog(accept=...). Unanswered, it is {dialog.automatic} automatically "
                      f"in {dialog.seconds_left()} s")
        elif stopped == "signal":
            detail = f"stopped after {waited:g} s: the page signal is {report.signal.value}"
        elif stopped == "tab_closed":
            detail = f"the tab closed after {waited:g} s"
        else:
            detail = f"stopped after {waited:g} s: a human took control of the browser"
        return WaitResult(met=met, stopped=stopped, waited_s=waited, url=_url_of(page),
                          conditions=list(watch.conditions.values()), reread=watch.reread,
                          dialog=dialog.public() if dialog is not None else None, detail=detail, **report.public())

    async def _wait_loop(self, page: Page, watch: _Watch, state: _PageState, handle: ElementHandle | None,
                         deadline: float, should_stop: Callable[[], str | None] | None) -> str:
        loop = asyncio.get_running_loop()
        next_signal = loop.time() + WAIT_SIGNAL_EVERY_S
        while True:
            if page.is_closed():
                return "tab_closed"
            reason = should_stop() if should_stop is not None else None
            if reason:
                return reason
            began = loop.time()
            await self._check(page, watch, state, handle, deadline)
            if watch.met:
                return "met"
            took = loop.time() - began
            if loop.time() >= next_signal:
                if (await self.report(page)).signal in STOP_SIGNALS:
                    return "signal"
                next_signal = loop.time() + WAIT_SIGNAL_EVERY_S
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "timeout"
            await asyncio.sleep(min(remaining, max(WAIT_POLL_MS / 1000, took)))

    async def _check(self, page: Page, watch: _Watch, state: _PageState, handle: ElementHandle | None,
                     deadline: float) -> None:
        spec = watch.spec
        if spec.reads_tree:
            left_ms = max(1.0, (deadline - asyncio.get_running_loop().time()) * 1000)
            raw = await quietly(page.aria_snapshot(mode="ai", timeout=min(SNAPSHOT_TIMEOUT_MS, left_ms + 500)))
            unreadable = "the page could not be read (it may be loading)"
            texts = outline_texts(raw) if raw is not None else None
            watch.reread = watch.reread or raw is not None
            if spec.for_text:
                hit = None
                for want in spec.for_text if texts is not None else ():
                    if (line := find_text(texts or [], want, exact=spec.exact)) is not None:
                        hit = (want, line)
                        break
                if hit is not None:
                    watch.set("for_text", True, hit[1], matched=hit[0])
                else:
                    watch.set("for_text", False, None if texts is not None else unreadable)
            if spec.gone_text:
                if texts is None:
                    watch.set("gone_text", False, unreadable)
                else:
                    left = [line for want in spec.gone_text
                            if (line := find_text(texts, want, exact=spec.exact)) is not None]
                    watch.set("gone_text", not left, left[0] if left else None)
        if handle is not None:
            budget = max(0.0, deadline - asyncio.get_running_loop().time()) + WAIT_READ_GRACE_S
            now = await quietly(_state(handle), _UNANSWERED, timeout=budget)
            # the inner quietly turns a failed read into None (gone); only no answer in time is _UNANSWERED
            view = await quietly(quietly(handle.evaluate(_VIEW_JS)), _UNANSWERED, timeout=budget) \
                if now is not None and now is not _UNANSWERED else None
            if now is _UNANSWERED or view is _UNANSWERED:
                watch.set("for_ref_state", False, "the page did not answer when its state was read")
            else:
                met, seen = ref_state_met(spec.ref_state or "", watch.before, now, view)
                watch.set("for_ref_state", met, seen)
        if spec.network_quiet_ms is not None:
            busy, idle = state.network()
            quiet = busy == 0 and idle * 1000 >= spec.network_quiet_ms
            if busy:
                seen = f"{busy} request{'s' if busy != 1 else ''} in flight"
            else:
                seen = "no requests seen yet" if math.isinf(idle) else f"quiet for {idle:.1f} s"
            watch.set("network_quiet_ms", quiet, seen)
        if spec.url_matches is not None:
            url = _url_of(page)
            watch.set("url_matches", url_matches(url, spec.url_matches), url)

    async def settle(self, page: Page, *, quiet_ms: int = SETTLE_QUIET_MS, max_s: float = SETTLE_MAX_S) -> None:
        """Give a page that just loaded a moment to render: until no request has started or ended
        for `quiet_ms`, at most `max_s`. Reads only; a dialog ends it at once."""
        state = self._track(page)
        guard = dialog_guard(page)
        if guard is not None and guard.pending_on(page) is not None:
            return

        async def quiet() -> None:
            while True:
                busy, idle = state.network()
                if busy == 0 and idle * 1000 >= quiet_ms:
                    return
                await asyncio.sleep(0.1)

        try:
            await asyncio.wait_for(self._race(page, quiet(), guard), max_s)
        except (TimeoutError, _DialogOpened):
            pass

    async def loading(self, page: Page) -> bool:
        """Whether the page is still loading: its document is not complete yet, or requests are in flight.
        A page that does not answer counts as loading."""
        busy, _idle = self._track(page).network()
        if busy:
            return True
        state = await quietly(page.evaluate("document.readyState"), None, timeout=READY_STATE_S)
        return state != "complete"

    async def wait_rendered(self, page: Page, max_s: float) -> None:
        """Wait, at most `max_s`, for a page that is still loading: its load event, then a quiet moment."""
        loop = asyncio.get_running_loop()
        end = loop.time() + max_s
        await quietly(page.wait_for_load_state("load", timeout=max_s * 1000), None, timeout=max_s)
        left = end - loop.time()
        if left > 0:
            await self.settle(page, max_s=min(SETTLE_MAX_S, left))
