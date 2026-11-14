"""Dialogs never freeze a lane: the DialogGuard owns every alert, confirm, prompt and
beforeunload of a context, queues them per page, answers one left too long, and catches file
choosers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from collections import deque
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agentos.core.errors import AgentosError, NotFound
from agentos.core.logs import url_for_log
from agentos.core.models import utcnow
from agentos.core.text import first_line
from agentos.node.interaction.pages import _url_of

log = logging.getLogger("agentos.interaction")


DIALOG_TIMEOUT = 60.0
DIALOG_HISTORY = 20
_DIALOG_OPEN = "a dialog is open, so the page cannot be read until it is answered"


# ---- dialogs and file choosers ------------------------------------------------------------

class DialogPending(AgentosError):
    """A page dialog (alert, confirm, prompt, beforeunload) waits for an answer and the page is
    frozen until it gets one. Answer it with browser_dialog_answer."""
    code = "dialog_pending"


# What a dialog does when nobody answers it in time: alerts are acknowledged, everything that
# asks a question gets the safe answer (do not delete, do not leave the page).
AUTO_ACCEPT = frozenset({"alert"})


@dataclass(eq=False)
class PageDialog:
    id: str
    type: str                      # alert | confirm | prompt | beforeunload
    message: str
    default: str | None
    url: str
    opened_at: datetime
    deadline: float                # event loop time of the automatic answer
    page: Any = field(repr=False)
    dialog: Any = field(repr=False)
    outcome: str | None = None     # accepted | dismissed | gone
    answered_by: str | None = None  # agent | timeout | tab_closed | lane_closed
    answered_at: datetime | None = None
    text: str | None = None        # the text a prompt was answered with
    error: str | None = None

    @property
    def pending(self) -> bool:
        return self.outcome is None

    @property
    def automatic(self) -> str:
        return "accepted" if self.type in AUTO_ACCEPT else "dismissed"

    def seconds_left(self) -> int:
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            return 0
        return max(0, round(self.deadline - now))

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "type": self.type, "message": self.message, "url": self.url,
                               "opened_at": self.opened_at.isoformat()}
        if self.type == "prompt":
            out["default"] = self.default or ""
        if self.pending:
            out["auto_answer"] = f"{self.automatic} in {self.seconds_left()} s if nobody answers"
        else:
            out.update(outcome=self.outcome, answered_by=self.answered_by,
                       answered_at=self.answered_at.isoformat() if self.answered_at else None)
            if self.text is not None:
                out["text"] = self.text
            if self.error:
                out["error"] = self.error
        return out


def dialog_notice(d: PageDialog, where: str = "on this tab") -> str:
    """The one line agents see in a snapshot or an error while a dialog is open."""
    what = "asks to confirm leaving the page" if d.type == "beforeunload" else "is open"
    return (f"- {d.type} dialog {what} {where}: {json.dumps(d.message[:300], ensure_ascii=False)} "
            f"(answer with browser_dialog_answer; {d.automatic} automatically in {d.seconds_left()} s)")


def _pending_error(d: PageDialog, doing: str) -> DialogPending:
    if d.type == "beforeunload":
        how = ("accept=true leaves the page (unsaved input is lost; a navigation that was asked for then "
               "continues), accept=false stays")
    elif d.type == "prompt":
        how = "accept=true with text answers it, accept=false cancels"
    elif d.type == "confirm":
        how = "accept=true confirms, accept=false cancels"
    else:
        how = "accept=true acknowledges it"
    return DialogPending(f"cannot {doing}: {_a(d.type)} {d.type} dialog is open on this tab "
                         f"({json.dumps(d.message[:300], ensure_ascii=False)}) and the page is frozen until it is "
                         f"answered. Call browser_dialog_answer: {how}. Unanswered, it is {d.automatic} "
                         f"automatically in {d.seconds_left()} s.")


def _a(word: str) -> str:
    return "an" if word[:1] in "aeiou" else "a"


class _DialogOpened(Exception):  # internal control flow, never leaves this module
    def __init__(self, dialog: PageDialog):
        super().__init__(dialog.id)
        self.dialog = dialog


@dataclass
class _Chooser:
    count: int = 0
    chooser: Any = None


class DialogGuard:
    """Owns every dialog and file chooser of one browser context (one lane)."""

    def __init__(self, *, timeout: float = DIALOG_TIMEOUT, history: int = DIALOG_HISTORY):
        self.timeout = timeout
        self._pending: list[PageDialog] = []
        self._history: deque[PageDialog] = deque(maxlen=history)
        self._waiters: list[tuple[Any, asyncio.Future]] = []
        self._timers: set[asyncio.Task] = set()
        self._choosers: dict[Any, _Chooser] = {}
        self._watched: set[Any] = set()
        self._seq = 0
        self._closed = False

    def attach(self, context: Any) -> DialogGuard:
        """Start owning the context's dialogs. A context gets one guard; attaching again returns it."""
        existing = guard_for_context(context)
        if existing is not None:
            return existing
        context.on("dialog", self._on_dialog)
        context.on("page", self._watch_page)
        for page in list(context.pages):
            self._watch_page(page)
        _GUARDS[context] = self
        return self

    def _watch_page(self, page: Any) -> None:
        if page in self._watched:
            return
        self._watched.add(page)
        page.on("close", lambda *_: self._on_page_closed(page))
        # A filechooser listener makes Playwright intercept the chooser, so the native picker never
        # opens on the lane's display (where it would sit until a human closed it).
        page.on("filechooser", lambda chooser: self._on_chooser(page, chooser))

    # ---- events ------------------------------------------------------------------------

    def _on_dialog(self, dialog: Any) -> None:
        loop = asyncio.get_running_loop()
        page = dialog.page
        self._seq += 1
        dtype = str(dialog.type)
        entry = PageDialog(id=f"d{self._seq}", type=dtype, message=str(dialog.message or "")[:2000],
                           default=(dialog.default_value or "") if dtype == "prompt" else None,
                           url=_url_of(page), opened_at=utcnow(), deadline=loop.time() + self.timeout,
                           page=page, dialog=dialog)
        if self._closed:
            self._spawn(self._answer(entry, accept=dtype in AUTO_ACCEPT, text=None, by="lane_closed"))
            return
        self._pending.append(entry)
        log.info("dialog %s (%s) opened on %s: %r", entry.id, dtype, url_for_log(entry.url), entry.message[:120])
        for owner, fut in self._waiters:
            if (owner is page or page is None) and not fut.done():
                fut.set_result(entry)
        self._spawn(self._expire(entry))

    def _on_page_closed(self, page: Any) -> None:
        self._watched.discard(page)
        self._choosers.pop(page, None)
        for entry in [d for d in self._pending if d.page is page]:
            self._settle(entry, "gone", "tab_closed")

    def _on_chooser(self, page: Any, chooser: Any) -> None:
        seen = self._choosers.setdefault(page, _Chooser())
        seen.count += 1
        seen.chooser = chooser
        log.info("file chooser intercepted on %s", _url_of(page))

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    async def _expire(self, entry: PageDialog) -> None:
        loop = asyncio.get_running_loop()
        while entry.pending and loop.time() < entry.deadline:  # extend() may move the deadline
            await asyncio.sleep(entry.deadline - loop.time())
        if entry.pending:
            log.info("dialog %s unanswered for %g s; %s automatically", entry.id, self.timeout, entry.automatic)
            await self._answer(entry, accept=entry.type in AUTO_ACCEPT, text=None, by="timeout")

    def _settle(self, entry: PageDialog, outcome: str, by: str, text: str | None = None) -> None:
        entry.outcome, entry.answered_by, entry.answered_at, entry.text = outcome, by, utcnow(), text
        if entry in self._pending:
            self._pending.remove(entry)
        self._history.append(entry)

    async def _answer(self, entry: PageDialog, *, accept: bool, text: str | None, by: str) -> PageDialog:
        if not entry.pending and entry in self._history:
            return entry
        reply = None
        if accept and entry.type == "prompt":
            reply = text if text is not None else (entry.default or "")
        # settle first, so a timer and an agent can never both answer
        self._settle(entry, "accepted" if accept else "dismissed", by, reply)
        try:
            if accept:
                await (entry.dialog.accept(reply) if reply is not None else entry.dialog.accept())
            else:
                await entry.dialog.dismiss()
        except Exception as e:  # noqa: BLE001 - the tab closed or navigated away meanwhile
            entry.outcome, entry.error = "gone", first_line(e, 300)
        log.info("dialog %s %s by %s", entry.id, entry.outcome, by)
        return entry

    # ---- queries -----------------------------------------------------------------------

    def pending(self, page: Any = None) -> list[PageDialog]:
        return [d for d in self._pending if page is None or d.page is page]

    def pending_on(self, page: Any) -> PageDialog | None:
        found = self.pending(page)
        return found[0] if found else None

    def current(self, page: Any = None) -> PageDialog | None:
        """The dialog browser_dialog_answer acts on: the active tab's first, else the oldest."""
        return (self.pending_on(page) if page is not None else None) or (self._pending[0] if self._pending else None)

    def history(self, limit: int = 5) -> list[PageDialog]:
        return list(self._history)[-limit:][::-1]

    async def answer(self, page: Any, *, accept: bool, text: str | None = None, by: str = "agent") -> PageDialog:
        entry = self.current(page)
        if entry is None:
            raise NotFound("no dialog is open in this lane")
        return await self._answer(entry, accept=accept, text=text, by=by)

    def extend(self, entry: PageDialog, seconds: float) -> None:
        """Hold off the automatic answer (a human is deciding on an approval for it)."""
        if entry.pending:
            entry.deadline = max(entry.deadline, asyncio.get_running_loop().time() + seconds)

    def chooser_count(self, page: Any) -> int:
        seen = self._choosers.get(page)
        return seen.count if seen else 0

    # ---- racing page calls against dialogs ---------------------------------------------

    def waiter(self, page: Any) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((page, fut))
        return fut

    def discard_waiter(self, fut: asyncio.Future) -> None:
        self._waiters = [(p, f) for p, f in self._waiters if f is not fut]
        if not fut.done():
            fut.cancel()

    async def close(self) -> None:
        """The lane is closing: settle what is open, stop the timers."""
        self._closed = True
        for entry in list(self._pending):
            self._settle(entry, "gone", "lane_closed")
        for task in list(self._timers):
            task.cancel()


# One guard per browser context, so a lane never gets two sets of dialog listeners.
_GUARDS: weakref.WeakKeyDictionary[Any, DialogGuard] = weakref.WeakKeyDictionary()


def guard_for_context(context: Any) -> DialogGuard | None:
    try:
        return _GUARDS.get(context)
    except TypeError:  # not weak-referenceable (a test double)
        return None


def dialog_guard(page: Any) -> DialogGuard | None:
    """The guard of the page's context, if the lane attached one."""
    context = getattr(page, "context", None)
    return guard_for_context(context) if context is not None else None
