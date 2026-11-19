"""PageTracker, DialogGuard and the dialog-aware interaction paths over fake pages (no browser)."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import pytest

from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Signal
from agentos.node.camoufox.engine import PageTracker, tracker_for
from agentos.node.interaction import (
    DialogGuard,
    DialogPending,
    PlaywrightInteraction,
    accept_matches,
    dialog_guard,
)


class Emitter:
    def __init__(self) -> None:
        self._handlers: dict[str, list] = defaultdict(list)

    def on(self, event: str, callback: Any) -> None:
        self._handlers[event].append(callback)

    def emit(self, event: str, *args: Any) -> None:
        for callback in list(self._handlers[event]):
            callback(*args)


class FakePage(Emitter):
    def __init__(self, context: FakeContext, url: str = "about:blank"):
        super().__init__()
        self.context = context
        self.url = url
        self._closed = False
        self.fronted = 0
        self.never: asyncio.Future | None = None

    def is_closed(self) -> bool:
        return self._closed

    async def title(self) -> str:
        return f"title of {self.url}"

    async def bring_to_front(self) -> None:
        self.fronted += 1

    async def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def close(self) -> None:
        self._closed = True
        self.context.pages.remove(self)
        self.emit("close", self)

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.never = asyncio.get_running_loop().create_future()
        return await self.never  # hangs, like a page frozen by a dialog

    async def goto(self, url: str, **kwargs: Any) -> Any:
        self.never = asyncio.get_running_loop().create_future()
        return await self.never


class FakeContext(Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.pages: list[FakePage] = []

    async def new_page(self) -> FakePage:
        return self.open("about:blank")

    def open(self, url: str, opener: FakePage | None = None) -> FakePage:
        page = FakePage(self, url)
        self.pages.append(page)
        self.emit("page", page)
        if opener is not None:
            opener.emit("popup", page)
        return page


class FakeDialog:
    def __init__(self, page: FakePage, type: str, message: str = "", default: str = ""):
        self.page, self.type, self.message, self.default_value = page, type, message, default
        self.answers: list[tuple[str, str | None]] = []

    async def accept(self, text: str | None = None) -> None:
        self.answers.append(("accept", text))

    async def dismiss(self) -> None:
        self.answers.append(("dismiss", None))


def open_dialog(page: FakePage, type: str, message: str = "Sure?", default: str = "") -> FakeDialog:
    dialog = FakeDialog(page, type, message, default)
    page.context.emit("dialog", dialog)
    return dialog


# ---- tabs -----------------------------------------------------------------------------------

async def test_the_first_page_is_active_and_page_creates_one_when_none_is_open():
    ctx = FakeContext()
    tracker = PageTracker(ctx)
    assert tracker.active is None
    page = await tracker.page()
    assert ctx.pages == [page] and tracker.active is page and await tracker.page() is page
    assert tracker_for(page) is tracker and tracker_for(ctx) is tracker


async def test_a_popup_becomes_active_and_closing_it_returns_to_its_opener():
    ctx = FakeContext()
    tracker = PageTracker(ctx)
    main = await tracker.page()
    other = ctx.open("https://site.example/other")             # an unrelated tab, now active
    await tracker.switch(0)
    popup = ctx.open("https://accounts.example/oauth", opener=main)
    assert tracker.active is popup

    tabs = await tracker.tabs()
    assert [(t["index"], t["active"], t["opened_by"]) for t in tabs] == [(0, False, None), (1, False, 0),
                                                                         (2, True, 0)]
    assert tabs[2]["title"] == "title of https://accounts.example/oauth"

    await popup.close()
    await asyncio.sleep(0)
    assert tracker.active is main  # its opener, not the most recently opened tab
    assert main.fronted >= 1
    assert other in ctx.pages


async def test_closing_a_tab_whose_opener_is_gone_falls_back_to_the_most_recent():
    ctx = FakeContext()
    tracker = PageTracker(ctx)
    a = await tracker.page()
    b = ctx.open("https://b.example", opener=a)
    c = ctx.open("https://c.example", opener=b)
    await b.close()                    # c's opener is gone; c now counts a as its opener
    assert tracker.active is c
    await c.close()
    assert tracker.active is a
    await a.close()
    assert tracker.active is None
    fresh = await tracker.page()
    assert fresh in ctx.pages and tracker.active is fresh


async def test_a_page_opened_without_an_opener_returns_to_the_page_active_then():
    ctx = FakeContext()
    tracker = PageTracker(ctx)
    a = await tracker.page()
    probe = ctx.open("about:blank")    # a scratch tab (the lane self-check) or a noopener tab
    assert tracker.active is probe
    await probe.close()
    assert tracker.active is a


async def test_switch_checks_the_index():
    ctx = FakeContext()
    tracker = PageTracker(ctx)
    with pytest.raises(InvalidRequest, match="no open tabs"):
        await tracker.switch(0)
    await tracker.page()
    ctx.open("https://x.example")
    with pytest.raises(InvalidRequest, match=r"no tab 2: there are 2 \(0 to 1\)"):
        await tracker.switch(2)
    with pytest.raises(InvalidRequest):
        await tracker.switch(-1)
    page = await tracker.switch(0)
    assert tracker.active is page and page.fronted == 1


async def test_pages_that_existed_before_the_tracker_are_adopted():
    ctx = FakeContext()
    first = FakePage(ctx, "https://restored.example")
    ctx.pages.append(first)
    tracker = PageTracker(ctx)
    assert tracker.active is first
    late = FakePage(ctx, "https://missed.example")  # appeared without a page event
    ctx.pages.append(late)
    assert [t["url"] for t in await tracker.tabs()] == ["https://restored.example", "https://missed.example"]


async def test_tabs_show_a_waiting_dialog_without_reading_the_frozen_page():
    ctx = FakeContext()
    tracker = PageTracker(ctx)
    page = await tracker.page()

    async def hang() -> str:
        await asyncio.sleep(30)
        return "never"

    page.title = hang  # a frozen page never answers
    open_dialog(page, "confirm", "Leave?")
    tabs = await asyncio.wait_for(tracker.tabs(), 1)
    assert tabs[0]["dialog"] == "confirm" and tabs[0]["title"] == ""


# ---- dialogs ----------------------------------------------------------------------------------

async def test_dialogs_queue_per_page_and_the_active_tab_is_answered_first():
    ctx = FakeContext()
    guard = DialogGuard(timeout=30).attach(ctx)
    a = await ctx.new_page()
    b = await ctx.new_page()
    first = open_dialog(a, "alert", "From a")
    second = open_dialog(b, "confirm", "From b")
    assert [d.message for d in guard.pending()] == ["From a", "From b"]
    assert guard.current(b).message == "From b" and guard.current().message == "From a"

    answered = await guard.answer(b, accept=True)
    assert answered.outcome == "accepted" and answered.answered_by == "agent"
    assert second.answers == [("accept", None)] and first.answers == []
    assert guard.pending_on(b) is None and guard.current(b).message == "From a"
    await guard.answer(None, accept=False)
    assert first.answers == [("dismiss", None)]
    with pytest.raises(NotFound, match="no dialog"):
        await guard.answer(a, accept=True)
    assert [d.message for d in guard.history()] == ["From a", "From b"]


async def test_a_prompt_gets_the_text_or_its_default():
    ctx = FakeContext()
    guard = DialogGuard().attach(ctx)
    page = await ctx.new_page()
    d = open_dialog(page, "prompt", "Name?", default="Untitled")
    assert guard.current(page).public()["default"] == "Untitled"
    await guard.answer(page, accept=True, text="Launch")
    assert d.answers == [("accept", "Launch")]
    d2 = open_dialog(page, "prompt", "Name?", default="Untitled")
    await guard.answer(page, accept=True)
    assert d2.answers == [("accept", "Untitled")]


@pytest.mark.parametrize(("kind", "expected"), [("alert", ("accept", None)), ("confirm", ("dismiss", None)),
                                                ("prompt", ("dismiss", None)), ("beforeunload", ("dismiss", None))])
async def test_unanswered_dialogs_get_the_safe_answer_after_the_timeout(kind, expected):
    ctx = FakeContext()
    guard = DialogGuard(timeout=0.05).attach(ctx)
    page = await ctx.new_page()
    d = open_dialog(page, kind)
    assert "if nobody answers" in guard.current(page).public()["auto_answer"]
    await asyncio.sleep(0.2)
    assert d.answers == [expected]
    last = guard.history()[0]
    assert last.answered_by == "timeout" and last.outcome == ("accepted" if kind == "alert" else "dismissed")
    assert guard.pending() == []


async def test_an_answer_and_the_timer_never_both_answer():
    ctx = FakeContext()
    guard = DialogGuard(timeout=0.05).attach(ctx)
    page = await ctx.new_page()
    d = open_dialog(page, "confirm")
    await guard.answer(page, accept=True)
    await asyncio.sleep(0.15)
    assert d.answers == [("accept", None)]


async def test_extend_holds_off_the_automatic_answer():
    ctx = FakeContext()
    guard = DialogGuard(timeout=0.05).attach(ctx)
    page = await ctx.new_page()
    d = open_dialog(page, "confirm")
    guard.extend(guard.current(page), 0.4)
    await asyncio.sleep(0.15)
    assert d.answers == [] and guard.current(page) is not None
    await asyncio.sleep(0.4)
    assert d.answers == [("dismiss", None)]


async def test_closing_a_tab_settles_its_dialogs_and_a_failed_answer_is_recorded():
    ctx = FakeContext()
    guard = DialogGuard().attach(ctx)
    a = await ctx.new_page()
    open_dialog(a, "alert")
    await a.close()
    assert guard.pending() == [] and guard.history()[0].answered_by == "tab_closed"

    b = await ctx.new_page()
    broken = open_dialog(b, "confirm")

    async def gone(*args: Any) -> None:
        raise RuntimeError("Target page, context or browser has been closed")

    broken.accept = gone
    answered = await guard.answer(b, accept=True)
    assert answered.outcome == "gone" and "closed" in answered.error


async def test_attach_is_once_per_context_and_close_settles_everything():
    ctx = FakeContext()
    guard = DialogGuard().attach(ctx)
    assert DialogGuard().attach(ctx) is guard
    page = await ctx.new_page()
    assert dialog_guard(page) is guard
    open_dialog(page, "alert")
    await guard.close()
    assert guard.pending() == [] and guard.history()[0].answered_by == "lane_closed"
    late = open_dialog(page, "confirm")  # a dialog after close is answered at once
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert late.answers == [("dismiss", None)]


async def test_file_choosers_are_intercepted_and_counted():
    ctx = FakeContext()
    guard = DialogGuard().attach(ctx)
    page = await ctx.new_page()
    assert "filechooser" in page._handlers  # a listener makes Playwright suppress the native picker
    page.emit("filechooser", object())
    page.emit("filechooser", object())
    assert guard.chooser_count(page) == 2


# ---- interaction while a dialog is open ---------------------------------------------------------

async def test_snapshot_shows_the_dialog_instead_of_hanging():
    ctx = FakeContext()
    guard = DialogGuard(timeout=60).attach(ctx)
    page = await ctx.new_page()
    other = await ctx.new_page()
    open_dialog(page, "confirm", "Delete this draft?")
    snap = await asyncio.wait_for(PlaywrightInteraction().snapshot(page), 1)
    lines = snap.text.splitlines()
    assert lines[0].startswith('- confirm dialog is open on this tab: "Delete this draft?"')
    assert "browser_dialog(accept=...)" in lines[0] and "dismissed automatically in 60 s" in lines[0]
    assert lines[1].startswith("- tabs: 2 open, this is tab 0")
    assert "frozen" in lines[2] and snap.ref_count == 0
    assert guard.pending_on(other) is None


@pytest.mark.parametrize("call", ["navigate", "screenshot", "act", "upload"])
async def test_page_calls_refuse_while_a_dialog_is_open(call, tmp_path):
    ctx = FakeContext()
    DialogGuard().attach(ctx)
    page = await ctx.new_page()
    page.url = "https://site.example/draft"
    open_dialog(page, "beforeunload", "")
    ia = PlaywrightInteraction()
    f = tmp_path / "a.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    calls = {"navigate": lambda: ia.navigate(page, "https://example.com/"),
             "screenshot": lambda: ia.screenshot(page),
             "act": lambda: ia.act(page, "e1", "click"),
             "upload": lambda: ia.upload(page, "e1", [f])}
    with pytest.raises(DialogPending, match="beforeunload dialog is open") as err:
        await asyncio.wait_for(calls[call](), 1)
    assert err.value.code == "dialog_pending" and "accept=true leaves the page" in err.value.message
    assert await ia.signal(page) is Signal.UNKNOWN


async def test_a_dialog_that_opens_during_a_page_call_stops_the_wait():
    ctx = FakeContext()
    DialogGuard().attach(ctx)
    page = await ctx.new_page()
    ia = PlaywrightInteraction()
    shot = asyncio.ensure_future(ia.screenshot(page))
    await asyncio.sleep(0.05)
    assert not shot.done()
    open_dialog(page, "alert", "Saved!")
    with pytest.raises(DialogPending, match='an alert dialog is open on this tab \\("Saved!"\\)'):
        await asyncio.wait_for(shot, 1)
    await asyncio.sleep(0)
    assert page.never.cancelled()  # the frozen call is not left waiting forever
    await dialog_guard(page).answer(page, accept=True)

    nav = asyncio.ensure_future(ia.navigate(page, "https://example.com/next"))
    await asyncio.sleep(0.05)
    open_dialog(page, "beforeunload", "")
    with pytest.raises(DialogPending, match="finish the navigation"):
        await asyncio.wait_for(nav, 1)


# ---- accept attribute ---------------------------------------------------------------------------

@pytest.mark.parametrize(("accept", "name", "mime", "ok"), [
    ("", "a.exe", "", True),
    ("image/*", "a.png", "image/png", True),
    ("image/*", "a.mp4", "video/mp4", False),
    ("image/png,image/jpeg", "a.jpg", "image/jpeg", True),
    ("image/jpg", "a.jpg", "image/jpeg", True),
    (".webp, video/mp4", "a.WEBP", "image/webp", True),
    (".webp, video/mp4", "a.mov", "video/quicktime", False),
    ("video/*,image/heic", "a.heic", "image/heic", True),
    ("application/pdf", "a.pdf", "application/pdf", True),
    (" , ", "a.png", "image/png", True),
])
def test_accept_matches(accept, name, mime, ok):
    assert accept_matches(accept, name, mime) is ok
