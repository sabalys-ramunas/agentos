"""browser_console and browser_network on real Camoufox (run on the node).

Run on the node:  OPENAJAR_TEST_SLOT=37 scripts/node-test.sh devtools tests/node/test_devtools_node.py -q

A small app on its own port (the slot's viewer base + 5) logs at every level, throws an uncaught
error, calls an API that answers 500 with a token in its URL, reads one that answers JSON with a
session token in it, and loads an image from a host the context refuses. Each test gets a fresh
context set up as a lane's (a PageTracker, which attaches the DevtoolsCapture), and the tools run
over it through a stub lane manager, so the capture, the masking and the body read are the
engine's own.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import pytest_asyncio

from openajar.core.models import Principal
from openajar.core.runtime import Runtime
from openajar.node import devtools_tools
from openajar.node.camoufox.engine import PageTracker
from openajar.node.devtools import MASKED, capture_for

pytestmark = [pytest.mark.node, pytest.mark.asyncio(loop_scope="session")]

PORT = int(os.environ.get("OPENAJAR_TEST_DEVTOOLS_PORT") or int(os.environ.get("OPENAJAR_TEST_VIEWER_BASE", "6120")) + 5)
LANE = "dev/app"

APP = """<!doctype html><html lang="en"><head><title>Broken app</title></head><body>
<h1>Broken app</h1>
<img src="http://refused.invalid/avatar.png" alt="avatar">
<script>
console.log("app ready");
console.info("build 42");
console.warn("deprecated option used");
console.error("could not load profile, token=abc123secret");
fetch("/api/fail?page=2&access_token=sekret-token-value").then(r => r.text());
fetch("/api/ok").then(r => r.json()).then(j => console.log("loaded", j.name));
fetch("/api/ok", {method: "POST", headers: {"Authorization": "Bearer hidden-bearer"},
                  body: JSON.stringify({password: "hunter2"})});
setTimeout(() => { renderStats(); }, 50);
</script></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/app.html":
            return self._send(200, APP.encode(), "text/html; charset=utf-8",
                              {"Set-Cookie": "sessionid=node-session-1; HttpOnly"})
        if path == "/other.html":
            return self._send(200, b"<!doctype html><title>Other</title><p>other</p>", "text/html")
        if path == "/api/fail":
            return self._send(500, b'{"error": "stats backend timed out"}', "application/json")
        if path == "/api/ok":
            return self._send(200, json.dumps({"name": "Ana", "session_token": "st-node-77"}).encode(),
                              "application/json")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        return self._send(200, b'{"ok": true}', "application/json")


@pytest.fixture(scope="session")
def app_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="devtools-app", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{PORT}"
    finally:
        server.shutdown()
        server.server_close()


@pytest_asyncio.fixture(loop_scope="session")
async def tracker(browser: Any, app_url: str) -> AsyncIterator[PageTracker]:
    context = await browser.new_context()
    await context.route(lambda url: not url.startswith(app_url + "/"), lambda route: route.abort())
    tracker = PageTracker(context)
    try:
        yield tracker
    finally:
        await tracker.close()
        await context.close()


class Lanes:
    def __init__(self, tracker: PageTracker):
        self.tracker = tracker

    @asynccontextmanager
    async def using(self, lane_id: str) -> AsyncIterator[Any]:
        yield await self.tracker.page()


class Ctx:
    def __init__(self, tracker: PageTracker):
        self.principal = Principal(id="tester", kind="agent")
        self.runtime = Runtime(lanes=Lanes(tracker))

    async def authorize(self, *args: Any, **kwargs: Any) -> None:
        return None


async def until(predicate: Any, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while not (value := predicate()):
        if time.monotonic() > deadline:
            raise AssertionError("not seen in time")
        await asyncio.sleep(0.1)
    return value


async def load_app(tracker: PageTracker, app_url: str) -> Any:
    page = await tracker.page()
    await page.goto(f"{app_url}/app.html", wait_until="load")
    capture = capture_for(page)
    assert capture is not None
    await until(lambda: any(e.kind == "pageerror" for e in capture.console_entries()))
    await until(lambda: sum(1 for e in capture.network_entries() if e.done) >= 5)
    return page


async def test_the_console_shows_every_level_and_the_uncaught_error(tracker, app_url):
    await load_app(tracker, app_url)
    out = await devtools_tools.browser_console(Ctx(tracker), on=LANE)
    rows = out["messages"]
    texts = [m["text"] for m in rows]
    assert "app ready" in texts and "build 42" in texts and "deprecated option used" in texts, rows
    err = next(m for m in rows if m.get("kind") == "pageerror")
    assert "renderStats" in err["text"] and err["level"] == "error", err
    assert "app.html:13:20" in err["stack"] and err["source"].endswith("/app.html:13:20"), err  # counted from 1
    assert not err["stack"].startswith("ReferenceError"), err  # the message is not repeated
    logged = next(m for m in rows if "could not load profile" in m["text"])
    assert logged["level"] == "error" and "abc123secret" not in logged["text"], logged
    assert logged["source"].startswith(f"{app_url}/app.html:"), logged
    only_errors = await devtools_tools.browser_console(Ctx(tracker), on=LANE, level="error")
    assert {m["level"] for m in only_errors["messages"]} == {"error"} and len(only_errors["messages"]) == 2
    print("console:", json.dumps(out, indent=1)[:3000])


async def test_the_network_shows_failures_and_masks_secrets(tracker, app_url):
    await load_app(tracker, app_url)
    ctx = Ctx(tracker)
    failed = await devtools_tools.browser_network(ctx, on=LANE, failed_only=True)
    by_url = {r["url"]: r for r in failed["requests"]}
    fail = by_url.get(f"{app_url}/api/fail?page=2&access_token={MASKED}")
    assert fail is not None and fail["status"] == 500 and fail["type"] == "fetch", failed
    refused = next(r for r in failed["requests"] if "refused.invalid" in r["url"])
    assert refused["failure"] and refused["type"] == "image", refused

    body = (await devtools_tools.browser_network(ctx, on=LANE, id=fail["id"]))["request"]
    assert body["body"] == '{"error": "stats backend timed out"}', body

    every = (await devtools_tools.browser_network(ctx, on=LANE, url_contains="/api/ok"))["requests"]
    get_ok = next(r for r in every if r["method"] == "GET")
    post_ok = next(r for r in every if r["method"] == "POST")
    got = (await devtools_tools.browser_network(ctx, on=LANE, id=get_ok["id"]))["request"]
    assert got["body"] == f'{{"name": "Ana", "session_token": "{MASKED}"}}', got
    posted = (await devtools_tools.browser_network(ctx, on=LANE, id=post_ok["id"]))["request"]
    text = json.dumps(posted)
    assert "hunter2" not in text and "hidden-bearer" not in text, posted
    assert posted["request_headers"].get("authorization") == MASKED, posted
    assert "never returned" in posted["request_body"], posted

    doc = (await devtools_tools.browser_network(ctx, on=LANE, type="document"))["requests"][-1]
    detail = (await devtools_tools.browser_network(ctx, on=LANE, id=doc["id"]))["request"]
    assert detail["response_headers"]["set-cookie"] == MASKED and "node-session-1" not in json.dumps(detail)
    print("network:", json.dumps(failed, indent=1)[:3000])


async def test_after_a_navigation_an_old_body_is_read_or_plainly_gone(tracker, app_url):
    page = await load_app(tracker, app_url)
    ctx = Ctx(tracker)
    first = await devtools_tools.browser_network(ctx, on=LANE, url_contains="/api/fail")
    old = first["requests"][-1]["id"]
    await page.goto(f"{app_url}/other.html", wait_until="load")
    later = (await devtools_tools.browser_network(ctx, on=LANE, id=old))["request"]
    assert "body" in later or "no longer has this body" in later["body_withheld"], later
    print("after navigation:", {k: later.get(k) for k in ("body", "body_withheld")})
    newer = await devtools_tools.browser_network(ctx, on=LANE, since=first["cursor"])
    assert any(r["url"].endswith("/other.html") for r in newer["requests"]), newer
