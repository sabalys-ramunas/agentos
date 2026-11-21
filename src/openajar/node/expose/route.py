"""The exposed dev servers of one lane's browser: a route on its RouteDispatcher, and the rescue of a
redirect hop the route cannot see.

The route matches every URL under `expose.openajar.invalid` (names.URL_RE) and answers only an active
expose of this lane, at its exact origin (`https://<name>.expose.openajar.invalid`, port 443):

- an expose that is not active, an unknown name, another port: aborted as a name that does not
  resolve (`namenotresolved`), as it is everywhere else;
- `http://` on the exposed host: a 308 to the https address (the page stays a secure context);
- a request that is not a navigation and that no page of an exposed origin sent (another site's
  fetch, image or script): aborted (`blockedbyclient`). A page on another site may still navigate to
  the exposed host (a link, a redirect, a form post, a frame), which is what an OAuth redirect back
  is;
- everything else goes to the expose's unix socket (relay.fetch) and is fulfilled with the answer.
  The lane's proxy is never used, and nothing leaves the node except through the owner's SSH session.

Juggler never routes a redirect hop (docs/design/egress-guard.md, 3.1), so the hop from an OAuth
provider back to the exposed host leaves the browser unrouted and fails (the name does not resolve;
on a proxied lane the proxy learns the host name, never the path, since the URL is https). The
context's `request` listener sees that hop: for a GET navigation to an active expose it starts the
same navigation again in the same frame, which is routed. The first request never reached the dev
server, so a one-time OAuth code is still unused. A POST that a 307 or 308 carried there cannot be
sent again (its body is gone) and fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from openajar.node.expose import names, relay

log = logging.getLogger("openajar.node.expose")

RESCUE_WINDOW_S = 10.0  # one rescue per URL in this window: a hop that keeps failing is not retried forever
NOT_ANSWERED = """<!doctype html><meta charset="utf-8"><title>Dev server not answering</title>
<body style="font:15px system-ui,sans-serif;max-width:36em;margin:4em auto;color:#222">
<h1 style="font-size:20px">Your dev server did not answer</h1><p>{why}</p>
<p>The page is served from your computer through <code>openajar expose</code>. Check that the dev server
runs there and that the command is still running, then reload.</p></body>"""


class LaneExposeRoute:
    """The route and the hop rescue of one lane's browser session (installed while it has an expose)."""

    def __init__(self, lane_id: str, *, lookup: Callable[[str, str], Any], context: Any,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None,
                 fetch: Callable[..., Any] = relay.fetch, clock: Callable[[], float] = time.monotonic):
        self.lane_id = lane_id
        self.lookup = lookup  # (lane, host) -> the active expose or None
        self.context = context
        self.on_event = on_event
        self.fetch = fetch
        self.clock = clock
        self.stats = {"served": 0, "refused": 0, "failed": 0, "rescued": 0}
        self._rescued: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    # ---- the route -------------------------------------------------------------------------

    async def handle(self, route: Any, request: Any) -> None:
        """The dispatcher's owner handler for names.URL_RE. Never lets a request reach the network."""
        try:
            await self._handle(route, request)
        except Exception:  # fail closed: an exposed request never goes to the network
            log.warning("%s: an exposed request failed", self.lane_id, exc_info=True)
            self.stats["failed"] += 1
            with contextlib.suppress(Exception):
                await route.abort("failed")

    async def _handle(self, route: Any, request: Any) -> None:
        url = request.url
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        expose = self.lookup(self.lane_id, host)
        scheme = parts.scheme.lower()
        try:
            port = parts.port
        except ValueError:
            port = -1
        if expose is None or scheme not in ("http", "https") or port not in (None, 443 if scheme == "https" else 80):
            self.stats["refused"] += 1
            await route.abort("namenotresolved")
            return
        if scheme == "http":
            target = names.origin_for(host) + _target(parts)
            await route.fulfill(status=308, headers={"location": target, "cache-control": "no-store"}, body=b"")
            return
        if not self._sent_by_exposed(request):
            self.stats["refused"] += 1
            self._event("expose_refused", {"host": host, "path": parts.path or "/",
                                           "reason": "a page on another site asked for it"})
            await route.abort("blockedbyclient")
            return
        headers = await _headers(request)
        if "cookie" not in {k.lower() for k in headers}:
            cookie = await self._cookie_header(url, request, headers)
            if cookie:
                headers["cookie"] = cookie
        body = _body(request)
        try:
            answer = await self.fetch(expose.socket, request.method, host, _target(parts),
                                      relay.request_headers(headers, host), body)
        except relay.RelayError as e:
            self.stats["failed"] += 1
            if request.is_navigation_request():
                await route.fulfill(status=502, headers={"content-type": "text/html; charset=utf-8",
                                                         "cache-control": "no-store"},
                                    body=NOT_ANSWERED.format(why=_html(str(e))).encode())
            else:
                await route.abort("connectionrefused")
            return
        self.stats["served"] += 1
        await route.fulfill(status=answer.status, headers=answer.fulfil_headers(), body=answer.body)

    def _sent_by_exposed(self, request: Any) -> bool:
        """A navigation (from anywhere), or a request a page of one of this lane's exposed origins sent:
        Sec-Fetch-Site same-origin, an Origin header of an exposed origin, or a frame at one."""
        try:
            if request.is_navigation_request():
                return True
        except Exception:  # noqa: BLE001
            return False
        headers = _plain_headers(request)
        if headers.get("sec-fetch-site") == "same-origin":
            return True
        origin = headers.get("origin")
        if origin and origin != "null" and self._exposed_origin(origin):
            return True
        try:
            frame_url = request.frame.url
        except Exception:  # noqa: BLE001 - a service worker's request has no frame
            return False
        return self._exposed_origin(frame_url)

    def _exposed_origin(self, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        try:
            port = parts.port
        except ValueError:
            return False
        return parts.scheme.lower() == "https" and port in (None, 443) and self.lookup(self.lane_id, host) is not None

    async def _cookie_header(self, url: str, request: Any, headers: dict[str, str]) -> str | None:
        """The browser's cookies for the URL when the request's headers do not carry them (a browser that
        hides them from the driver). SameSite is honoured as the browser would: Strict only from the
        same origin, Lax also on a top-level navigation."""
        try:
            cookies = await self.context.cookies([url])
        except Exception:  # noqa: BLE001
            return None
        site = {k.lower(): v for k, v in headers.items()}.get("sec-fetch-site", "")
        same = site in ("same-origin", "none")
        try:
            nav = request.is_navigation_request() and request.frame.parent_frame is None
        except Exception:  # noqa: BLE001
            nav = False
        kept = []
        for c in cookies:
            same_site = str(c.get("sameSite") or "Lax").lower()
            if same_site == "strict" and not same:
                continue
            if same_site == "lax" and not (same or nav):
                continue
            kept.append(f"{c['name']}={c['value']}")
        return "; ".join(kept) or None

    # ---- the redirect hop rescue -----------------------------------------------------------

    def on_request(self, request: Any) -> None:
        """Context `request` listener: a redirect hop to an active expose is navigated again (routed)."""
        try:
            if request.redirected_from is None or not request.is_navigation_request():
                return
            if request.method.upper() != "GET":
                return
            url = request.url
            parts = urlsplit(url)
            host = (parts.hostname or "").lower().rstrip(".")
            if parts.scheme.lower() != "https" or self.lookup(self.lane_id, host) is None:
                return
            frame = request.frame
        except Exception:  # noqa: BLE001 - a request that went away
            return
        now = self.clock()
        self._rescued = {u: t for u, t in self._rescued.items() if now - t < RESCUE_WINDOW_S}
        if url in self._rescued:
            return
        self._rescued[url] = now
        self.stats["rescued"] += 1
        self._event("expose_redirect", {"host": host, "path": parts.path or "/"})
        task = asyncio.ensure_future(self._goto(frame, url))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _goto(self, frame: Any, url: str) -> None:
        try:
            await frame.goto(url)
        except Exception as e:  # noqa: BLE001 - the page shows its own error
            log.info("%s: the redirect to an exposed host could not be followed again: %s", self.lane_id,
                     str(e).splitlines()[0] if str(e) else type(e).__name__)

    def _event(self, kind: str, detail: dict[str, Any]) -> None:
        if self.on_event is not None:
            with contextlib.suppress(Exception):
                self.on_event(kind, detail)


def _target(parts: Any) -> str:
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def _plain_headers(request: Any) -> dict[str, str]:
    try:
        return {k.lower(): v for k, v in (request.headers or {}).items()}
    except Exception:  # noqa: BLE001
        return {}


async def _headers(request: Any) -> dict[str, str]:
    """Every header the browser sends (all_headers carries the cookies where the driver can see them)."""
    with contextlib.suppress(Exception):
        got = await request.all_headers()
        if isinstance(got, dict):
            return dict(got)
    return dict(_plain_headers(request))


def _body(request: Any) -> bytes | None:
    try:
        return request.post_data_buffer
    except Exception:  # noqa: BLE001
        return None


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
