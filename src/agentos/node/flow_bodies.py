"""Response bodies the page's own code caused, read on Chromium from what the browser kept (CDP),
without a second request (see flow_driver's docstring, watch_json).
"""

from __future__ import annotations

import asyncio
import base64
from collections import Counter
from contextlib import suppress
from typing import Any

Page = Any     # playwright.async_api.Page


# ---------------------------------------------------------------- the page's own responses


def _on_chromium(page: Page) -> bool:
    browser = page.context.browser
    return browser is not None and browser.browser_type.name == "chromium"


def _bare(url: str) -> str:
    return url.split("#", 1)[0]  # Playwright's URL keeps a fragment, CDP's does not


class _ChromiumBodies:
    """A page's bodies read with Network.getResponseBody on a CDP session of its own, which only listens:
    Network.enable shows nothing to the page or the site, and the session never sends a request. Network
    events carry an id that Playwright's Response does not, so a response is found by its turn: the nth
    fetch or XHR to answer on that URL since the page committed is the nth here. A response of the page
    being left takes no turn, by its loader: JsonWatch counts from the commit and this from the load, and
    those agree only because of that. Failing to attach means a read fails, never that it goes through
    Response.body()."""

    def __init__(self, page: Page):
        self._page = page
        self._session: Any = None
        self._main: str | None = None  # the main frame
        self._left: set[str] = set()  # loaders of the page being left
        self._loaders: set[str] = set()  # loaders seen since this load began
        self._turns: Counter[str] = Counter()
        self._ids: dict[tuple[str, int], asyncio.Future[str]] = {}
        self._ends: dict[str, asyncio.Future[bool]] = {}
        self.ready = asyncio.get_running_loop().create_task(self._attach())

    async def _attach(self) -> None:
        session = await self._page.context.new_cdp_session(self._page)
        frames = [(await session.send("Page.getFrameTree"))["frameTree"]]
        self._main = frames[0]["frame"]["id"]
        while frames:
            tree = frames.pop()
            self._left.add(tree["frame"]["loaderId"])
            frames += tree.get("childFrames", [])
        session.on("Network.requestWillBeSent", self._on_request)
        session.on("Network.responseReceived", self._on_response)
        session.on("Network.loadingFinished", lambda event: self._ended(event, True))
        session.on("Network.loadingFailed", lambda event: self._ended(event, False))
        await session.send("Network.enable")
        self._session = session

    def _id_at(self, url: str, turn: int) -> asyncio.Future[str]:
        return self._ids.setdefault((url, turn), asyncio.get_running_loop().create_future())

    def _on_request(self, event: dict[str, Any]) -> None:
        if event.get("type") == "Document" and event.get("frameId") == self._main:
            self._left |= self._loaders
            self._loaders = set()
            self._turns.clear()

    def _on_response(self, event: dict[str, Any]) -> None:
        if event.get("type") not in ("Fetch", "XHR") or event.get("loaderId") in self._left:
            return
        self._loaders.add(event.get("loaderId", ""))
        url, request_id = _bare(event["response"]["url"]), event["requestId"]
        self._ends[request_id] = asyncio.get_running_loop().create_future()
        turn = self._turns[url]
        self._turns[url] += 1
        if not (found := self._id_at(url, turn)).done():
            found.set_result(request_id)

    def _ended(self, event: dict[str, Any], whole: bool) -> None:
        end = self._ends.get(event["requestId"])
        if end is not None and not end.done():
            end.set_result(whole)

    async def read(self, url: str, turn: int) -> bytes:
        await self.ready
        request_id = await self._id_at(_bare(url), turn)
        if not await self._ends[request_id]:
            raise ConnectionError("the request failed before its body ended")
        got = await self._session.send("Network.getResponseBody", {"requestId": request_id})
        return base64.b64decode(got["body"]) if got.get("base64Encoded") else got["body"].encode()

    async def _detach(self) -> None:
        with suppress(Exception):  # the page may be gone
            await self.ready
            await self._session.detach()

    def close(self) -> None:
        for waiting in (*self._ids.values(), *self._ends.values()):
            waiting.cancel()
        with suppress(RuntimeError):  # no loop left: the session goes with the browser
            task = asyncio.get_running_loop().create_task(self._detach())
            _DETACHING.add(task)
            task.add_done_callback(_DETACHING.discard)


_DETACHING: set[asyncio.Task[None]] = set()
