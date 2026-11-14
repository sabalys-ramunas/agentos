"""PlaywrightInteraction.wait and settle over fake pages (no browser): conditions, caps, dialogs,
signals, a human taking the lane, and that ref and network waits never re-read the tree."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import pytest

from agentos.core.errors import InvalidRequest, RefNotFound
from agentos.core.models import Signal
from agentos.node.interaction import (
    DialogGuard,
    DialogPending,
    PlaywrightInteraction,
    WaitFor,
    ref_state_met,
    url_matches,
)
from agentos.node.interaction import pages as ia_pages
from agentos.node.interaction import waits as ia_waits
from agentos.node.interaction.readback import _MARKERS_JS, _STATE_JS
from agentos.node.interaction.waits import _VIEW_JS

UPLOADING = "- main:\n  - paragraph [ref=e3] [box=1,2,3,4]: Uploading 45%\n  - button \"Post\" [ref=e7] [disabled]"
SHARED = "- main:\n  - paragraph [ref=e3] [box=1,2,3,4]: Your post was shared\n  - button \"Post\" [ref=e7]"
NO_MARKERS = {"frames": [], "scripts": [], "elements": {}}


class Emitter:
    def __init__(self) -> None:
        self._handlers: dict[str, list] = defaultdict(list)

    def on(self, event: str, callback: Any) -> None:
        self._handlers[event].append(callback)

    def emit(self, event: str, *args: Any) -> None:
        for callback in list(self._handlers[event]):
            callback(*args)


class FakeContext(Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.pages: list[Any] = []

    async def cookies(self, urls: Any = None) -> list:
        return []


class FakeHandle:
    def __init__(self, state: dict[str, str] | None = None, visible: bool = True):
        self.state = dict(state or {"name": "Post"})
        self.visible, self.focused, self.connected = visible, False, True

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        if js == "(el) => el.isConnected":
            return self.connected
        if js is _STATE_JS:
            return dict(self.state) if self.connected else None
        if js is _VIEW_JS:
            return {"visible": self.visible, "focused": self.focused} if self.connected else None
        raise AssertionError(f"unexpected script: {js[:60]}")


class FakeLocator:
    def __init__(self, handle: FakeHandle | None):
        self.handle = handle

    async def count(self) -> int:
        return 1 if self.handle else 0

    async def element_handle(self, timeout: Any = None) -> FakeHandle | None:
        return self.handle


class WaitPage(Emitter):
    """Reads return `trees` in order (the last one repeats); scripts other than the read-only
    signal facts are refused, so a wait that wrote to the page would fail."""

    def __init__(self, trees: list[str] | None = None, url: str = "https://site.example/feed"):
        super().__init__()
        self.context = FakeContext()
        self.context.pages.append(self)
        self.url, self.trees, self.reads, self.frames = url, trees or [UPLOADING], 0, []
        self.handles: dict[str, FakeHandle] = {}
        self.markers: dict[str, Any] = dict(NO_MARKERS)
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def aria_snapshot(self, *, mode: str | None = None, timeout: Any = None, **kwargs: Any) -> str:
        assert mode == "ai"  # the mode browser_snapshot uses, so refs of unchanged elements stay
        tree = self.trees[min(self.reads, len(self.trees) - 1)]
        self.reads += 1
        return tree

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        if js is _MARKERS_JS:
            return self.markers
        raise AssertionError(f"unexpected page script: {js[:60]}")

    async def title(self) -> str:
        return "Fixture"

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.handles.get(selector.removeprefix("aria-ref=")))


class FakeDialog:
    def __init__(self, page: WaitPage, type: str, message: str):
        self.page, self.type, self.message, self.default_value = page, type, message, ""
        self.answers: list[str] = []

    async def accept(self, text: str | None = None) -> None:
        self.answers.append("accept")

    async def dismiss(self) -> None:
        self.answers.append("dismiss")


class FakeRequest:
    def __init__(self, resource_type: str = "xhr"):
        self.resource_type = resource_type


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ia_waits, "WAIT_POLL_MS", 10)
    monkeypatch.setattr(ia_waits, "WAIT_SIGNAL_EVERY_S", 0.02)


@pytest.fixture
def inter() -> PlaywrightInteraction:
    return PlaywrightInteraction()


def later(seconds: float, fn: Any) -> None:
    asyncio.get_running_loop().call_later(seconds, fn)


# ---- text ---------------------------------------------------------------------------------------

async def test_a_text_wait_resolves_when_the_text_appears(inter):
    page = WaitPage([UPLOADING, UPLOADING, SHARED])
    result = await inter.wait(page, WaitFor(for_text=("was shared",), timeout_s=5))
    assert result.met and result.stopped == "met" and result.reread
    [cond] = result.conditions
    assert cond == {"for_text": ["was shared"], "met": True, "seen": "paragraph: Your post was shared",
                    "matched": "was shared"}
    assert page.reads == 3 and result.signal is Signal.OK and result.detail.startswith("met after")


async def test_any_one_of_several_texts_ends_the_wait_and_says_which(inter):
    error = "- main:\n  - alert: Something went wrong"
    result = await inter.wait(WaitPage([UPLOADING, error]),
                              WaitFor(for_text=("Your post was shared", "something went WRONG"), timeout_s=5))
    assert result.met and result.conditions[0]["matched"] == "something went WRONG"


async def test_gone_text_waits_until_every_text_is_gone(inter):
    page = WaitPage([UPLOADING, SHARED])
    result = await inter.wait(page, WaitFor(gone_text=("Uploading", "Processing"), timeout_s=5))
    assert result.met and result.conditions[0]["seen"] is None and page.reads == 2


async def test_exact_texts_must_be_a_whole_name(inter):
    result = await inter.wait(WaitPage([SHARED]), WaitFor(for_text=("Pos",), exact=True, timeout_s=0))
    assert not result.met
    result = await inter.wait(WaitPage([SHARED]), WaitFor(for_text=("post",), exact=True, timeout_s=0))
    assert result.met and result.conditions[0]["seen"] == 'button "Post" [ref=e7]'


async def test_a_timeout_returns_what_was_seen_and_the_signal(inter):
    result = await inter.wait(WaitPage([UPLOADING]), WaitFor(gone_text=("uploading",), timeout_s=0.15))
    assert not result.met and result.stopped == "timeout"
    assert result.conditions[0] == {"gone_text": ["uploading"], "met": False, "seen": "paragraph: Uploading 45%"}
    assert result.signal is Signal.OK and result.waited_s >= 0.1
    assert result.detail == "not met within 0.15 s: gone_text not met (now: paragraph: Uploading 45%)"


async def test_the_timeout_is_capped(inter, monkeypatch):
    monkeypatch.setattr(ia_waits, "WAIT_MAX_S", 0.2)
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await inter.wait(WaitPage([UPLOADING]), WaitFor(for_text=("never",), timeout_s=9999))
    assert result.stopped == "timeout" and loop.time() - started < 1.0


async def test_a_zero_timeout_checks_once(inter):
    page = WaitPage([UPLOADING])
    result = await inter.wait(page, WaitFor(for_text=("never",), timeout_s=0))
    assert result.stopped == "timeout" and page.reads == 1


async def test_a_page_that_cannot_be_read_is_not_a_match(inter):
    page = WaitPage([UPLOADING])

    async def broken(**kwargs: Any) -> str:
        raise RuntimeError("Execution context was destroyed")

    page.aria_snapshot = broken
    result = await inter.wait(page, WaitFor(gone_text=("Uploading",), timeout_s=0))
    assert not result.met and "could not be read" in result.conditions[0]["seen"] and not result.reread


# ---- dialogs ------------------------------------------------------------------------------------

async def test_a_dialog_that_opens_while_waiting_is_reported_not_swallowed(inter):
    page = WaitPage([UPLOADING])
    guard = DialogGuard(timeout=30).attach(page.context)
    dialog = FakeDialog(page, "confirm", "Leave this page?")
    later(0.05, lambda: page.context.emit("dialog", dialog))
    result = await inter.wait(page, WaitFor(for_text=("never",), timeout_s=5))
    assert result.stopped == "dialog" and not result.met and result.waited_s < 2
    assert result.dialog["type"] == "confirm" and result.dialog["message"] == "Leave this page?"
    assert "auto_answer" in result.dialog  # still open: the agent answers it
    assert dialog.answers == [] and guard.pending_on(page) is not None
    assert result.signal is Signal.UNKNOWN and "browser_dialog_answer" in result.detail
    await guard.close()


async def test_a_dialog_already_open_is_an_error(inter):
    page = WaitPage([UPLOADING])
    guard = DialogGuard(timeout=30).attach(page.context)
    page.context.emit("dialog", FakeDialog(page, "alert", "Saved!"))
    with pytest.raises(DialogPending):
        await inter.wait(page, WaitFor(for_text=("x",), timeout_s=1))
    await guard.close()


# ---- refs ---------------------------------------------------------------------------------------

async def test_a_ref_reaching_a_state_ends_the_wait_without_reading_the_tree(inter):
    page = WaitPage()
    handle = page.handles["e7"] = FakeHandle({"name": "Post", "disabled": "true"})
    later(0.05, lambda: handle.state.pop("disabled"))
    result = await inter.wait(page, WaitFor(ref="e7", ref_state="enabled", timeout_s=5))
    assert result.met and result.conditions[0]["seen"] == "visible, enabled"
    assert result.conditions[0]["for_ref_state"] == {"ref": "e7", "state": "enabled"}
    assert page.reads == 0 and not result.reread  # the agent's refs are untouched


async def test_hidden_is_met_when_the_element_leaves_the_page(inter):
    page = WaitPage()
    handle = page.handles["e3"] = FakeHandle({"name": "Uploading 45%"})
    later(0.05, lambda: setattr(handle, "connected", False))
    result = await inter.wait(page, WaitFor(ref="e3", ref_state="hidden", timeout_s=5))
    assert result.met and result.conditions[0]["seen"] == "gone from the page"


async def test_changed_reports_the_targets_own_change(inter):
    page = WaitPage()
    handle = page.handles["e12"] = FakeHandle({"aria-pressed": "false", "name": "Like 12"})
    later(0.05, lambda: handle.state.update({"aria-pressed": "true", "name": "Like 13"}))
    result = await inter.wait(page, WaitFor(ref="e12", ref_state="changed", timeout_s=5))
    assert result.met and result.conditions[0]["seen"] == 'aria-pressed false -> true; name "Like 12" -> "Like 13"'


async def test_an_unknown_ref_is_ref_not_found(inter):
    with pytest.raises(RefNotFound):
        await inter.wait(WaitPage(), WaitFor(ref="e99", ref_state="visible", timeout_s=1))


@pytest.mark.parametrize("state, now, view, met", [
    ("visible", {}, {"visible": True}, True),
    ("hidden", {}, {"visible": False}, True),
    ("hidden", None, None, True),
    ("visible", None, None, False),
    ("enabled", {"aria-disabled": "true"}, {"visible": True}, False),
    ("disabled", {"disabled": "true"}, {"visible": True}, True),
    ("checked", {"checked": "true"}, {"visible": True}, True),
    ("unchecked", {"aria-checked": "true"}, {"visible": True}, False),
    ("pressed", {"aria-pressed": "true"}, {"visible": True}, True),
    ("unpressed", {"aria-pressed": "true"}, {"visible": True}, False),
    ("expanded", {"aria-expanded": "false"}, {"visible": True}, False),
    ("collapsed", {"aria-expanded": "false"}, {"visible": True}, True),
    ("selected", {"aria-selected": "true"}, {"visible": True}, True),
    ("focused", {}, {"visible": True, "focused": True}, True),
    ("changed", {"name": "a"}, {"visible": True}, False),
])
def test_ref_states(state, now, view, met):
    assert ref_state_met(state, {"name": "a"}, now, view)[0] is met


def test_every_ref_state_is_known():
    for state in ia_waits.REF_STATES:
        ref_state_met(state, {}, {}, {"visible": True})


# ---- network and url ----------------------------------------------------------------------------

async def test_network_quiet_waits_for_requests_in_flight(inter):
    page = WaitPage()
    inter._track(page)
    xhr, stream = FakeRequest("xhr"), FakeRequest("eventsource")
    page.emit("request", xhr)
    page.emit("request", stream)  # a stream never ends, so it never counts
    later(0.1, lambda: page.emit("requestfinished", xhr))
    result = await inter.wait(page, WaitFor(network_quiet_ms=100, timeout_s=5))
    assert result.met and result.waited_s >= 0.15 and page.reads == 0
    assert result.conditions[0]["seen"].startswith("quiet for")


async def test_a_long_poll_does_not_keep_the_network_busy(inter, monkeypatch):
    monkeypatch.setattr(ia_pages, "LONG_REQUEST_S", 0.05)
    page = WaitPage()
    inter._track(page)
    page.emit("request", FakeRequest("fetch"))
    result = await inter.wait(page, WaitFor(network_quiet_ms=100, timeout_s=5))
    assert result.met


async def test_network_quiet_times_out_with_the_requests_still_open(inter):
    page = WaitPage()
    inter._track(page)
    page.emit("request", FakeRequest("xhr"))
    result = await inter.wait(page, WaitFor(network_quiet_ms=100, timeout_s=0.1))
    assert not result.met and result.conditions[0]["seen"] == "1 request in flight"


def test_url_matches_part_of_the_url_or_a_glob():
    assert url_matches("https://www.reddit.com/r/x/comments/abc/", "/comments/")
    assert url_matches("https://www.reddit.com/r/x/comments/abc/", "https://www.reddit.com/r/*/comments/*")
    assert not url_matches("https://www.reddit.com/r/x/", "https://www.reddit.com/r/*/comments/*")


async def test_a_url_wait_follows_the_page(inter):
    page = WaitPage()
    later(0.05, lambda: setattr(page, "url", "https://site.example/p/123?posted=1"))
    result = await inter.wait(page, WaitFor(url_matches="posted=1", timeout_s=5))
    assert result.met and result.conditions[0]["seen"] == "https://site.example/p/123?posted=1"


async def test_conditions_must_hold_together(inter):
    page = WaitPage([SHARED])
    later(0.08, lambda: setattr(page, "url", "https://site.example/p/1"))
    result = await inter.wait(page, WaitFor(for_text=("shared",), url_matches="/p/", timeout_s=5))
    assert result.met and result.waited_s >= 0.05 and len(result.conditions) == 2


# ---- what stops a wait --------------------------------------------------------------------------

async def test_a_human_taking_the_lane_stops_the_wait(inter):
    calls = []

    def should_stop() -> str | None:
        calls.append(1)
        return "human_in_control" if len(calls) > 2 else None

    result = await inter.wait(WaitPage(), WaitFor(for_text=("never",), timeout_s=5), should_stop=should_stop)
    assert result.stopped == "human_in_control" and not result.met and "human" in result.detail


async def test_a_challenge_signal_stops_the_wait(inter):
    page = WaitPage()
    later(0.05, lambda: page.markers.update(frames=[
        {"src": "https://www.google.com/recaptcha/api2/bframe?k=x", "visible": True}]))
    result = await inter.wait(page, WaitFor(for_text=("never",), timeout_s=5))
    assert result.stopped == "signal" and result.signal is Signal.CAPTCHA and result.waited_s < 2


async def test_the_tab_closing_stops_the_wait(inter):
    page = WaitPage()
    later(0.05, lambda: setattr(page, "closed", True))
    result = await inter.wait(page, WaitFor(for_text=("never",), timeout_s=5))
    assert result.stopped == "tab_closed" and result.signal is Signal.UNKNOWN


@pytest.mark.parametrize("spec, message", [
    (WaitFor(), "nothing to wait for"),
    (WaitFor(ref="e7", ref_state="shiny"), "unknown ref state"),
    (WaitFor(for_text=("x",), timeout_s=-1), "0 or more"),
    (WaitFor(for_text=("x",), timeout_s=float("nan")), "0 or more"),
])
async def test_bad_waits_are_refused(inter, spec, message):
    with pytest.raises(InvalidRequest, match=message):
        await inter.wait(WaitPage(), spec)


# ---- settle -------------------------------------------------------------------------------------

async def test_settle_returns_once_the_network_is_quiet_and_never_waits_long(inter):
    page = WaitPage()
    inter._track(page)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await inter.settle(page, quiet_ms=50, max_s=2)
    assert loop.time() - started < 1.5  # quiet, not the 2 s cap
    page.emit("request", FakeRequest())
    started = loop.time()
    await asyncio.wait_for(inter.settle(page, quiet_ms=50, max_s=0.2), 5)  # the request never ends: bounded
    assert 0.15 < loop.time() - started


async def test_a_tab_closing_mid_action_is_fine_but_a_browser_closing_is_an_engine_error(inter):
    """A popup that closes itself leaves its opener; a browser that dies leaves no tab, and the
    action may be half done (found by the chaos run: tests/soak/test_chaos.py)."""
    from agentos.core.errors import EngineError
    from agentos.core.models import ActionKind

    opener, popup = WaitPage(), WaitPage()
    popup.context = opener.context
    opener.context.pages.append(popup)
    popup.closed = True
    result = await inter._after(popup, ActionKind.CLICK, "e4", {opener, popup}, 0, None)
    assert result.ok and "tab closed" in result.detail

    opener.closed = True  # the whole browser went away
    with pytest.raises(EngineError, match="half done"):
        await inter._after(popup, ActionKind.FILL, "e4", {opener, popup}, 0, None)
