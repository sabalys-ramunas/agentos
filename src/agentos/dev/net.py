"""The dev tabs' network: the requests the fake site sends, through the lane's egress guard and
route dispatcher the way a Camoufox context sends them (agentos.node.egress, agentos.node.routes).

Each dev tab gets a DevContext: a browser context with no browser. The fake site's composer sends
its publish request through it when Share is pressed (DevDriver.click): the context tells its
`request` listeners (the guard's listener), then hands the request to the route whose pattern
matches it, last installed first, as Playwright does. A route that aborts it keeps the post from
happening: the composer stays open with what was typed, so the same click can be made again.

DEV_RULES is the fake site's effect rule (its composer posts a GraphQL-style JSON body to
`/api/graphql`, operation CreatePost), added to the dev lanes' guards on top of EFFECT_RULES, so
the commit gate runs on the dev node exactly as on a real lane.

Each context also has the lane's DevtoolsCapture (agentos.node.devtools), and every page the tab
loads plays its traffic through the context (`load`, page_requests, page_console): the document, the site's
script, and on some pages what a broken web app shows (a page error with its stack, a warning, an
API call that answers 500, an image whose host refuses the connection), so browser_console and
browser_network have something to find on `agentos dev up` and in the unit tests.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

from agentos.node import egress, publish
from agentos.node.devtools import DevtoolsCapture
from agentos.node.routes import RouteDispatcher, glob_match

DEV_HOST = "social.example"
DEV_RULES: tuple[egress.EffectRule, ...] = (
    egress.EffectRule("social.post", "social", DEV_HOST, r"/api/graphql$", op=r"^CreatePost$",
                      note="the dev site's composer: variables.input.{caption, media[], audience}"),
    egress.EffectRule("social.draft.upload", "social", DEV_HOST, r"/api/upload$", effect="write",
                      note="the composer's photo: the file's bytes; the answer names it {media_id}"),
)
publish.DISPLAY.setdefault("social.post", {
    "text": ("variables.input.caption",), "media": ("variables.input.media[]",),
    "visibility": ("variables.input.audience",), "as": ("variables.input.actor_id",)})
publish.RULE_VOLATILE.setdefault("social.post", ("variables.input.attribution_id",))
PUBLISH_URL = f"https://{DEV_HOST}/api/graphql"
UPLOAD_URL = f"https://{DEV_HOST}/api/upload"


class DevFrame:
    parent_frame = None
    url = f"https://{DEV_HOST}/compose"

    def __init__(self) -> None:
        self.page: Any = None  # the tab that sent it (DevContext.send): the guard tells a flow's page apart


class DevResponse:
    """A response as the guard (its text) and the devtools capture (status, headers, body) read it.
    `gone`: the browser let go of the body (a page that navigated away), so body() fails as
    Playwright's does."""

    def __init__(self, text: str, *, status: int = 200, url: str = "", request: Any = None,
                 headers: dict[str, str] | None = None):
        self._text = text
        self.status = status
        self.status_text = STATUS_TEXT.get(status, "")
        self.url = url
        self.request = request
        self.headers = dict(headers or {"content-type": "application/json"})
        self.gone = False

    async def text(self) -> str:
        return self._text

    async def body(self) -> bytes:
        if self.gone:
            raise RuntimeError("Response body is unavailable: the page navigated away")
        return self._text.encode()


STATUS_TEXT = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}


class DevRequest:
    """What the guard and the devtools capture read of a request (url, method, headers, body, its
    frame, its type, why it failed)."""

    def __init__(self, url: str, method: str = "POST", *, headers: dict[str, str] | None = None,
                 body: bytes | None = None, answer: str = '{"data": {"ok": true}}', resource_type: str = "fetch",
                 status: int = 200, answer_headers: dict[str, str] | None = None, failure: str | None = None):
        self.url = url
        self.answer = answer
        self.method = method
        self.headers = dict(headers or {})
        self.post_data_buffer = body
        self.redirected_from = None
        self.frame = DevFrame()
        self.resource_type = resource_type
        self.status = status
        self.answer_headers = answer_headers
        self.failure = failure  # set: the request gets no answer (requestfailed with this text)
        self._response: DevResponse | None = None

    def is_navigation_request(self) -> bool:
        return False

    async def response(self) -> DevResponse:
        return self._answered()

    def _answered(self) -> DevResponse:
        if self._response is None:
            self._response = DevResponse(self.answer, status=self.status, url=self.url, request=self,
                                         headers=self.answer_headers)
        return self._response


class DevRoute:
    def __init__(self) -> None:
        self.decision: str | None = None

    async def abort(self, error_code: str = "failed") -> None:
        self.decision = "abort"

    async def continue_(self, **kw: Any) -> None:
        self.decision = "continue"

    async def fallback(self, **kw: Any) -> None:
        self.decision = None


class DevContext:
    """A browser context for one dev tab: listeners, routes, and send()."""

    def __init__(self) -> None:
        self.pages: list[Any] = []
        self.page: Any = None  # the one tab this context has (attach)
        self._listeners: dict[str, list[Callable[[Any], Any]]] = {}
        self._routes: list[tuple[Any, Any]] = []
        self.sent: list[DevRequest] = []  # what reached the "site"
        self.sent_pages: list[DevRequest] = []  # what the page on show loaded (load)
        self.devtools: DevtoolsCapture | None = None

    def on(self, event: str, fn: Callable[[Any], Any]) -> None:
        self._listeners.setdefault(event, []).append(fn)

    def remove_listener(self, event: str, fn: Callable[[Any], Any]) -> None:
        if fn in self._listeners.get(event, []):
            self._listeners[event].remove(fn)

    async def route(self, pattern: Any, handler: Any) -> None:
        self._routes.append((pattern, handler))

    async def unroute(self, pattern: Any, handler: Any = None) -> None:
        self._routes = [(p, h) for p, h in self._routes if not (p == pattern and (handler is None or h == handler))]

    async def cookies(self, urls: Any = None) -> list[dict[str, Any]]:
        return []

    def routes(self) -> list[Any]:
        return [p for p, _ in self._routes]

    async def send(self, request: DevRequest) -> bool:
        """Send `request`: True when it reached the site, False when a route aborted it."""
        request.frame.page = self.page
        self._emit("request", request)
        for pattern, handler in reversed(list(self._routes)):
            hit = pattern.match(request.url) if isinstance(pattern, re.Pattern) else glob_match(pattern, request.url)
            if not hit:
                continue
            route = DevRoute()
            await handler(route, request)
            if route.decision == "abort":
                request.failure = request.failure or "NS_ERROR_ABORT"
                self._emit("requestfailed", request)
                return False
            if route.decision == "continue":
                break
        self.sent.append(request)
        self._emit("response", request._answered())
        self._emit("requestfinished", request)
        return True

    def fetch(self, request: DevRequest) -> None:
        """A request the page sends on its own (a script, an image, an API read): heard by every
        listener, answered (or failed) at once, never routed. Only GETs come this way, which the
        guard lets go without a look."""
        request.frame.page = self.page
        self._emit("request", request)
        if request.failure is not None:
            self._emit("requestfailed", request)
            return
        self._emit("response", request._answered())
        self._emit("requestfinished", request)

    def load(self, url: str, *, found: bool = True) -> None:
        """The tab loaded `url`: the bodies of the page before it are gone, as in a browser, then the
        new page's traffic and console plays (page_requests, page_console)."""
        for old in self.sent_pages:
            old._answered().gone = True
        self.sent_pages.clear()
        doc = DevRequest(url, "GET", resource_type="document", status=200 if found else 404,
                         answer=f"<!doctype html><title>{url}</title>",
                         answer_headers={"content-type": "text/html; charset=utf-8",
                                         "set-cookie": "sessionid=dev-session-7f3a; Secure; HttpOnly"})
        for request in (doc, *page_requests(url)):
            self.fetch(request)
            self.sent_pages.append(request)
        for kind, text, stack in page_console(url):
            if kind == "pageerror":
                self._emit("weberror", DevWebError(self.page, text, stack))
            else:
                self._emit("console", DevConsoleMessage(self.page, kind, text))

    def _emit(self, event: str, request: Any) -> None:
        for fn in list(self._listeners.get(event, [])):
            fn(request)


class DevConsoleMessage:
    """A console call as Playwright's ConsoleMessage shows it."""

    def __init__(self, page: Any, type: str, text: str, location: dict[str, Any] | None = None):
        self.page, self.type, self.text = page, type, text
        self.location = location or {"url": APP_JS, "lineNumber": 87, "columnNumber": 12}


class DevError:
    def __init__(self, message: str, stack: str):
        name, sep, rest = message.partition(": ")
        self.name, self.message = (name, rest) if sep else ("Error", message)
        self.stack = stack


class DevWebError:
    """An uncaught page error as Playwright's WebError shows it."""

    def __init__(self, page: Any, message: str, stack: str):
        self.page = page
        self.error = DevError(message, stack)


APP_JS = f"https://{DEV_HOST}/static/app.js"
CDN_HOST = f"cdn.{DEV_HOST}"


def page_requests(url: str) -> list[DevRequest]:
    """What a page of the fake site fetches after its document."""
    if not url.startswith(f"https://{DEV_HOST}/"):
        return []
    out = [DevRequest(APP_JS, "GET", resource_type="script", answer="console.log('app ready');",
                      answer_headers={"content-type": "text/javascript"})]
    path = url[len(f"https://{DEV_HOST}"):].split("?", 1)[0]
    if path == "/":
        out.append(DevRequest(f"https://{DEV_HOST}/api/feed?cursor=0&access_token=dev-feed-token-5c1e", "GET",
                              answer='{"items": [], "next_cursor": null}'))
    elif path.startswith("/u/"):
        name = path[3:] or "me"
        out.append(DevRequest(f"https://{DEV_HOST}/api/users/{name}/stats", "GET", status=500,
                              answer='{"error": "stats service timed out", "request_id": "req-4021"}'))
        out.append(DevRequest(f"https://{CDN_HOST}/avatars/{name}.jpg", "GET", resource_type="image",
                              failure="NS_ERROR_CONNECTION_REFUSED"))
    return out


def page_console(url: str) -> list[tuple[str, str, str]]:
    """What a page of the fake site logs as it loads: (kind, text, stack); kind is the console call,
    or pageerror for an uncaught exception."""
    if not url.startswith(f"https://{DEV_HOST}/"):
        return []
    path = url[len(f"https://{DEV_HOST}"):].split("?", 1)[0]
    out = [("log", "app ready", "")]
    if path.startswith("/u/"):
        out.append(("warning", "[stats] request failed with 500, retrying in 5 s", ""))
        out.append(("pageerror", "TypeError: user.stats is undefined",
                    f"renderStats@{APP_JS}:212:17\nrenderProfile@{APP_JS}:180:5\nonLoad@{APP_JS}:40:3"))
    return out


def publish_request(caption: str, media: list[str], *, actor: str = "biz-a") -> DevRequest:
    """The composer's publish request: what a person typed, the files it attached, and what a real
    client adds that changes every time (a mutation id, a timestamp, a CSRF token)."""
    body = {"operation": "CreatePost", "csrf_token": secrets.token_hex(8),
            "variables": {"input": {"caption": caption, "media": media, "audience": "public", "actor_id": actor,
                                    "client_mutation_id": secrets.token_hex(6),
                                    "attribution_id": f"compose.{time.time():.3f}"}}}
    return DevRequest(PUBLISH_URL, "POST", headers={"content-type": "application/json"},
                      body=json.dumps(body).encode())


def upload_request(data: bytes, mime: str) -> tuple[DevRequest, str]:
    """The composer's photo upload: the file's bytes, answered with the media id the post then
    names (as a real site's upload answers with a photo id). Returns the request and that id."""
    media_id = "dm_" + secrets.token_hex(6)
    request = DevRequest(UPLOAD_URL, "POST", headers={"content-type": mime or "application/octet-stream"},
                         body=data, answer=json.dumps({"media_id": media_id}))
    return request, media_id


def attach(page: Any, driver: Any) -> egress.LaneGuard:
    """Give a dev tab its context, devtools capture, route dispatcher and egress guard (with DEV_RULES)."""
    context = DevContext()
    context.page = page
    page.context = context
    driver.net = context
    context.devtools = DevtoolsCapture().attach(context)  # attached first: it hears every request the guard does
    guard = egress.LaneGuard(page.lane_id).attach(context, RouteDispatcher(context))
    guard.rules = egress.EFFECT_RULES + DEV_RULES
    guard.settle_quiet_s = 0.05
    guard.settle_grace_s = 0.0  # the fake site sends inside the click: no request is told of late
    return guard
