"""read_json in real Chrome through the node's real driver, on local fixtures only. The page's own script
fetches its JSON. Most pages are served by this test's own Playwright route; what must be counted where
the site would count it (is it passive?) is served by a real TLS server on this machine, which sees every
request Chrome sends. Skips where Chrome cannot start. Camoufox is the node's engine and needs Linux:
see the `node` tests."""

from __future__ import annotations

import asyncio
import base64
import datetime
import gzip
import hashlib
import json
import ssl
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from agentos.core.driver import MAX_JSON_BYTES
from agentos.core.models import RunStatus
from agentos.node.flow_driver import JsonWatch, PlaywrightFlowDriver
from agentos.runner.fakes import FakeLanes, FakeNotifier, FakeSite, Harness
from agentos.runner.models import SUCCEEDED
from agentos.runner.util import Clock

pw = pytest.importorskip("playwright.async_api")

HOSTNAME = "portal.northsupply.example"
HOST = f"https://{HOSTNAME}"
API, EP, LANE = "harbor-supplier", "orders", "biz-a/supplier"
FLOW = f"connector.{API}.{EP}"
OWNER = {"kind": "human", "id": "owner", "owner": "owner"}
SECRET = "sk-live-do-not-keep-me"
ORDERS = {"data": {"orders": [{"id": "A-1042", "amount": {"value": 1204.5}, "apiToken": SECRET},
                              {"id": "A-1043", "amount": {"value": 88}, "apiToken": SECRET}]}}
FETCH = "<script>fetch('/api/orders?page=1').then((r) => r.text()).then((t) => { document.title = 'done'; });</script>"


def html(script: str = FETCH) -> tuple[int, dict, bytes]:
    return 200, {"content-type": "text/html"}, f"<!doctype html><link rel='icon' href='data:,'><main><h1>Orders</h1></main>{script}".encode()


def as_json(body=ORDERS, status: int = 200) -> tuple[int, dict, bytes]:
    return status, {"content-type": "application/json"}, json.dumps(body).encode()


def read(host: str = HOST, **over) -> dict:
    pattern = host.replace("https://", "").replace(".", "\\.")
    return {"type": "read_json", "save_as": "orders", "url": f"^https://{pattern}/api/orders", "items": "data.orders",
            "max_items": 100, "timeout_ms": 3000,
            "fields": {"number": {"path": "id", "type": "string", "max_chars": 40},
                       "total": {"path": "amount.value", "type": "number"}}, **over}


def definition(*steps: dict) -> dict:
    return {"api": API, "domain": HOSTNAME, "version": "1.0.0", "surfaces": [LANE],
            "endpoints": {EP: {"title": "Orders", "effect": "read", "steps": list(steps),
                               "outputs": {"orders": "orders"} if any(s["type"] == "read_json" for s in steps) else {}}}}


def harness(tmp_path, page, doc: dict) -> Harness:
    @asynccontextmanager
    async def real(lane_id: str):
        yield PlaywrightFlowDriver(page, lane_id=lane_id, dwell_ms=(0, 0))

    h = Harness(tmp_path, site=FakeSite(), factory=real, clock=Clock(),
                services={"lanes": FakeLanes([LANE]), "notifier": FakeNotifier()})
    h.svc = h.runtime.get("site_apis")
    h.svc.save(doc, by=OWNER)
    h.svc.activate(API, by=OWNER)
    return h


@pytest.fixture
async def browser():
    async with pw.async_playwright() as p:
        try:
            chrome = await p.chromium.launch(channel="chrome", headless=True, args=[f"--host-resolver-rules=MAP {HOSTNAME} 127.0.0.1"])
        except Exception as e:  # noqa: BLE001 - no Chrome here
            pytest.skip(f"no Chrome: {e}")
        yield chrome
        await chrome.close()


@pytest.fixture
async def site(browser):
    """A page in Chrome with `site.pages`: path -> (status, headers, body) or a coroutine function of the route."""
    page = await browser.new_page()
    page.pages = {}
    page.visits = []

    async def serve(route):
        url = route.request.url
        path = url[len(HOST):]
        page.visits.append(path)
        found = page.pages.get(path) or page.pages.get(path.split("?")[0])
        if not url.startswith(HOST) or found is None:
            await route.fulfill(status=404, body="")
        elif callable(found):
            await found(route)
        else:
            await route.fulfill(status=found[0], headers=found[1], body=found[2])

    await page.route("**/*", serve)
    yield page
    await page.close()


async def _run(tmp_path, page, *steps: dict):
    h = harness(tmp_path, page, definition(*steps))
    run = await h.run(FLOW, surface=LANE)
    return h, run


NAV = {"type": "navigate", "url": f"{HOST}/orders"}


async def test_the_pages_own_fetch_becomes_declared_fields(tmp_path, site):
    site.pages |= {"/orders": html(), "/api/orders": as_json()}
    _, run = await _run(tmp_path, site, NAV, read())
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert run.output["data"]["orders"] == [{"number": "A-1042", "total": 1204.5}, {"number": "A-1043", "total": 88}]
    assert run.output["untrusted_fields"] == ["orders[].number"]


async def test_a_body_reaches_nothing_on_disk(tmp_path, site):
    site.pages |= {"/orders": html(), "/api/orders": as_json()}
    await _run(tmp_path, site, NAV, read())
    assert not [p for p in tmp_path.rglob("*") if p.is_file() and SECRET.encode() in p.read_bytes()]


async def test_a_page_that_never_fetches_is_a_bad_moment_until_it_repeats(tmp_path, site):
    site.pages |= {"/orders": html("")}
    h, run = await _run(tmp_path, site, NAV, read(timeout_ms=300))
    assert run.error["code"] == "json_not_seen" and h.runner.health.get(f"{API}.{EP}", LANE)["state"] != "broken"


@pytest.mark.parametrize("status, code", [(429, "rate_limited"), (401, "signal_logged_out")])
async def test_the_sites_own_stop_is_not_a_broken_contract(tmp_path, site, status, code):
    site.pages |= {"/orders": html(), "/api/orders": as_json(status=status)}
    h, run = await _run(tmp_path, site, NAV, read())
    assert run.error["code"] == code and h.runner.health.get(f"{API}.{EP}", LANE)["state"] != "broken"
    assert h.runtime.get("lanes").get(LANE).hold  # every agent, flow and schedule stays off the lane


@pytest.mark.parametrize("answer, code", [(as_json(status=500), "json_status"), ((204, {}, b""), "json_empty")])
async def test_a_bad_moment_is_not_a_broken_contract(tmp_path, site, answer, code):
    site.pages |= {"/orders": html(), "/api/orders": answer}
    h, run = await _run(tmp_path, site, NAV, read())
    assert run.error["code"] == code and h.runner.health.get(f"{API}.{EP}", LANE)["state"] != "broken"


@pytest.mark.parametrize("answer, cause", [(as_json(status=403), "json_status"),
                                           ((200, {"content-type": "text/html"}, b"<html>nope</html>"), "json_invalid")])
async def test_a_bad_answer_is_a_broken_contract(tmp_path, site, answer, cause):
    site.pages |= {"/orders": html(), "/api/orders": answer}
    h, run = await _run(tmp_path, site, NAV, read())
    assert run.error["code"] == "connector_contract_broken" and run.error["cause"] == cause
    assert h.runner.health.get(f"{API}.{EP}", LANE)["state"] == "broken"


async def test_the_first_after_the_load_wins_and_the_second_is_never_read(tmp_path, site):
    twice = """<script>fetch('/api/orders?n=1').then((r) => r.text()).then(() => fetch('/api/orders?n=2'));</script>"""
    site.pages |= {"/orders": html(twice), "/api/orders?n=1": as_json(), "/api/orders?n=2": as_json({"data": {"orders": []}})}
    _, run = await _run(tmp_path, site, NAV, read())
    assert [r["number"] for r in run.output["data"]["orders"]] == ["A-1042", "A-1043"]


async def test_a_response_from_the_page_being_left_is_not_this_pages(tmp_path, site):
    """The old page keeps polling while the new one is on its way: a watch armed before the load must
    not take that."""
    poll = "<script>setInterval(() => fetch('/api/orders?old=1'), 20);</script>"

    async def slow_page(route):
        await asyncio.sleep(0.4)
        await route.fulfill(status=html()[0], headers=html()[1], body=html()[2])

    site.pages |= {"/poll": html(poll), "/orders": slow_page, "/api/orders?old=1": as_json({"data": {"orders": [{"id": "STALE"}]}}),
                   "/api/orders?page=1": as_json()}
    await site.goto(f"{HOST}/poll")
    _, run = await _run(tmp_path, site, NAV, read())
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert any("old=1" in v for v in site.visits)
    assert run.output["data"]["orders"][0]["number"] == "A-1042"


async def test_a_response_on_another_host_or_an_image_is_not_read(tmp_path, site):
    script = f"<img src='{HOST}/api/orders.png'><script>setTimeout(() => fetch('/api/orders?page=1'), 150);</script>"
    site.pages |= {"/orders": html(script), "/api/orders.png": (200, {"content-type": "image/png"}, b"x"),
                   "/api/orders": as_json()}
    _, run = await _run(tmp_path, site, NAV, read())
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error


async def test_a_watch_leaves_no_listener_behind(site):
    listeners = site._impl_obj.listeners
    before = len(listeners("response")), len(listeners("framenavigated"))
    watch = PlaywrightFlowDriver(site, lane_id="test/lane").watch_json(lambda url: True)
    assert len(listeners("response")) == before[0] + 1
    watch.close()
    assert (len(listeners("response")), len(listeners("framenavigated"))) == before


async def test_a_body_over_the_cap_is_refused_by_its_length_and_costs_no_read(tmp_path, site):
    site.pages |= {"/orders": html(), "/api/orders": (200, {"content-type": "application/json"}, b"[" + b"1," * MAX_JSON_BYTES + b"1]")}
    t0 = time.monotonic()
    _, run = await _run(tmp_path, site, NAV, read())
    assert run.error["cause"] == "json_too_large" and time.monotonic() - t0 < 20


async def test_a_deep_or_huge_body_never_stalls_the_node(tmp_path, site):
    site.pages |= {"/orders": html(), "/api/orders": (200, {"content-type": "application/json"}, b"[" * 2_000_000)}
    worst, done = 0.0, False

    async def beat() -> None:
        nonlocal worst
        last = time.monotonic()
        while not done:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            worst, last = max(worst, now - last), now

    task = asyncio.create_task(beat())
    _, run = await _run(tmp_path, site, NAV, read())
    done = True
    await task
    assert run.error["cause"] == "json_invalid" and worst < 1.0


async def test_discover_lists_what_the_page_fetched_as_shape_only(tmp_path, site):
    site.pages |= {"/orders": html(), "/api/orders": as_json()}
    h = harness(tmp_path, site, definition(NAV, read()))

    found = await h.svc.discover(LANE, f"{HOST}/orders")
    told = json.dumps(found)
    c = found["json"][0]
    assert c["path"] == "/api/orders" and c["items"] == "data.orders" and c["shape"]["data"]["orders"][0]["apiToken"] == "hidden"
    for word in (SECRET, "A-1042", "1204.5"):
        assert word not in told


# ---- passive: what a real server sees ----


def _certificate(tmp_path) -> tuple[ssl.SSLContext, str]:
    """The server's TLS context, and the base64 hash of its key that a browser can be told to trust."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTNAME)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOSTNAME)]), critical=False).sign(key, hashes.SHA256()))
    pem = tmp_path / "server.pem"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pem)
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return ctx, base64.b64encode(hashlib.sha256(spki).digest()).decode()


class Server:
    """A plain HTTPS server: every request Chrome sends is in `seen`, whatever made it. A page with an
    `etag` honours If-None-Match, and `sent` holds what it answered for each request."""

    def __init__(self, pages: dict, *, slow: dict | None = None):
        self.pages, self.seen, self.sent = pages, [], []
        self.slow = slow or {}  # path -> seconds between a response's headers and its body
        self.spki = ""

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while head := await reader.readuntil(b"\r\n\r\n"):
                lines = head.decode("latin-1").split("\r\n")
                method, target, _ = lines[0].split(" ", 2)
                headers = sorted(tuple(line.split(": ", 1)) for line in lines[1:] if line)
                length = next((int(v) for k, v in headers if k.lower() == "content-length"), 0)
                if length:
                    await reader.readexactly(length)
                self.seen.append((method, target, tuple(headers)))
                status, extra, body = self.pages.get(target.split("?")[0], (404, {}, b""))
                if "etag" in extra and {k.lower(): v for k, v in headers}.get("if-none-match") == extra["etag"]:
                    status, extra, body = 304, {"etag": extra["etag"]}, b""
                self.sent.append((target, status))
                head_out = f"HTTP/1.1 {status} X\r\n" + ("" if status == 304 else f"content-length: {len(body)}\r\n")
                head_out += "".join(f"{k}: {v}\r\n" for k, v in extra.items()) + "\r\n"
                writer.write(head_out.encode())
                await writer.drain()
                await asyncio.sleep(self.slow.get(target.split("?")[0], 0))
                writer.write(b"" if method == "HEAD" else body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    @asynccontextmanager
    async def running(self, tmp_path):
        ctx, self.spki = _certificate(tmp_path)
        server = await asyncio.start_server(self._client, "127.0.0.1", 0, ssl=ctx)
        try:
            yield f"https://{HOSTNAME}:{server.sockets[0].getsockname()[1]}"
        finally:
            server.close()


async def _visit(tmp_path, browser, name: str, base: str, *reads: dict) -> None:
    """One run in a fresh context, as the same visit each time."""
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    def no_route(*_a, **_k):
        raise AssertionError("the product added a route")

    page.route = context.route = no_route
    nav = {"type": "navigate", "url": f"{base}/orders"}
    home = tmp_path / name
    home.mkdir()
    h = harness(home, page, definition(nav, *reads))
    run = await h.run(FLOW, surface=LANE)
    assert run.status in SUCCEEDED, run.error
    await asyncio.sleep(0.3)  # anything late would land here
    await context.close()


PAGES = {"/orders": html(), "/api/orders": as_json(), "/favicon.ico": (404, {}, b"")}


async def test_the_site_sees_the_same_visit_with_and_without_the_step(tmp_path, browser):
    server = Server(dict(PAGES))
    async with server.running(tmp_path) as base:
        await _visit(tmp_path, browser, "without", base)
        without, server.seen = sorted(server.seen), []
        await _visit(tmp_path, browser, "with", base, read(base))
        watched = sorted(server.seen)
    assert [r[:2] for r in without] == [r[:2] for r in watched] and any(r[1].startswith("/api/orders") for r in watched)
    assert sum(r[1].startswith("/api/orders") for r in watched) == 1
    assert without == watched, "a header changed"


async def test_a_redirect_is_a_hop_and_what_it_leads_to_is_the_response(tmp_path, browser):
    server = Server({**PAGES, "/orders": html("<script>fetch('/api/orders/old');</script>"), "/api/orders/new": as_json()})
    async with server.running(tmp_path) as base:
        server.pages["/api/orders/old"] = (302, {"location": f"{base}/api/orders/new"}, b"")
        await _visit(tmp_path, browser, "hop", base, read(base))
    assert [r[1] for r in server.seen if r[1].startswith("/api")] == ["/api/orders/old", "/api/orders/new"]


async def test_a_read_of_a_post_repeats_nothing(tmp_path, browser):
    script = "<script>fetch('/api/orders', {method: 'POST', body: '{\"q\":1}'}).then((r) => r.text());</script>"
    server = Server({**PAGES, "/orders": html(script)})
    async with server.running(tmp_path) as base:
        await _visit(tmp_path, browser, "post", base, read(base))
    assert [r[0] for r in server.seen if r[1].startswith("/api/orders")] == ["POST"]


async def test_a_head_is_never_read(tmp_path, browser):
    """Playwright's body() on Chromium can go back to the network for an empty body under a
    Content-Length, so the driver does not ask where no body is due."""
    script = "<script>fetch('/api/orders', {method: 'HEAD'});</script>"
    server = Server({**PAGES, "/orders": html(script)})
    async with server.running(tmp_path) as base:
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()
        home = tmp_path / "head"
        home.mkdir()
        h = harness(home, page, definition({"type": "navigate", "url": f"{base}/orders"}, read(base)))
        run = await h.run(FLOW, surface=LANE)
        await asyncio.sleep(0.3)
        await context.close()
    assert run.error["code"] == "json_empty"
    assert [r[0] for r in server.seen if r[1].startswith("/api/orders")] == ["HEAD"]


async def test_a_body_still_on_its_way_at_the_timeout_is_waited_for(tmp_path, browser):
    """Headers at once, the body after the step's own timeout: the response came, so it is read."""
    server = Server(dict(PAGES), slow={"/api/orders": 1.0})
    async with server.running(tmp_path) as base:
        await _visit(tmp_path, browser, "slow", base, read(base, timeout_ms=200))
    assert sum(r[1].startswith("/api/orders") for r in server.seen) == 1


# ---- a site that sends ETags: what Chrome shows on a second read ----


@asynccontextmanager
async def _trusting(tmp_path, server: Server):
    """A Chrome that trusts the server's key, with its HTTP cache on. `ignore_https_errors` is no use here:
    Chrome does not cache what came over a certificate error, so nothing would ever be revalidated."""
    async with pw.async_playwright() as p:
        try:
            context = await p.chromium.launch_persistent_context(
                tmp_path / "profile", channel="chrome", headless=True,
                args=[f"--host-resolver-rules=MAP {HOSTNAME} 127.0.0.1", f"--ignore-certificate-errors-spki-list={server.spki}"])
        except Exception as e:  # noqa: BLE001 - no Chrome here
            pytest.skip(f"no Chrome: {e}")
        yield context
        await context.close()


def _api_lines(server: Server) -> list[tuple[str, int]]:
    return [(t, s) for t, s in server.sent if t.startswith("/api/orders")]


def _conditional(server: Server) -> list[bool]:
    return [any(k.lower() == "if-none-match" for k, _ in r[2]) for r in server.seen if r[1].startswith("/api/orders")]


ETAG = {"content-type": "application/json", "etag": '"v1"', "cache-control": "no-cache"}


async def test_a_read_chrome_revalidates_is_a_200_with_its_body(tmp_path, browser):
    """What Chrome does on a 304 it asked for itself: the server answers 304, and the page and Playwright
    both see a 200 with the cached body. So a site with ETags does not fail a second read."""
    server = Server({**PAGES, "/api/orders": (200, ETAG, as_json()[2])})
    async with server.running(tmp_path) as base, _trusting(tmp_path, server) as context:
        page = await context.new_page()
        statuses: list[int] = []
        page.on("response", lambda r: statuses.append(r.status) if "/api/orders" in r.url else None)
        h = harness(tmp_path, page, definition({"type": "navigate", "url": f"{base}/orders"}, read(base)))
        first, second = await h.run(FLOW, surface=LANE), await h.run(FLOW, surface=LANE)
    assert _conditional(server) == [False, True] and [s for _, s in _api_lines(server)] == [200, 304]
    assert statuses == [200, 200]
    assert first.status == second.status == RunStatus.SUCCEEDED_VERIFIED, second.error
    assert second.output["data"]["orders"] == first.output["data"]["orders"]


async def test_a_read_chrome_serves_from_its_cache_reads_and_asks_the_site_nothing(tmp_path, browser):
    fresh = {"content-type": "application/json", "cache-control": "max-age=600"}
    server = Server({**PAGES, "/api/orders": (200, fresh, as_json()[2])})
    async with server.running(tmp_path) as base, _trusting(tmp_path, server) as context:
        page = await context.new_page()
        h = harness(tmp_path, page, definition({"type": "navigate", "url": f"{base}/orders"}, read(base)))
        first, second = await h.run(FLOW, surface=LANE), await h.run(FLOW, surface=LANE)
    assert len(_api_lines(server)) == 1
    assert second.status == RunStatus.SUCCEEDED_VERIFIED, second.error
    assert second.output["data"]["orders"] == first.output["data"]["orders"]


OWN_COPY = """<script>
const known = localStorage.getItem('etag');
fetch('/api/orders?page=1', {headers: known ? {'If-None-Match': known} : {}}).then(async (r) => {
  if (r.status === 200) { localStorage.setItem('etag', r.headers.get('etag')); localStorage.setItem('rows', await r.text()); }
  document.title = 'rows ' + JSON.parse(localStorage.getItem('rows')).data.orders.length + ' on ' + r.status;
});
</script>"""


async def test_a_304_the_page_asked_for_itself_is_not_a_contract_failure(tmp_path, browser):
    """The other way a 304 comes: a page that keeps its own copy sends If-None-Match itself, and then Chrome
    passes the 304 to the page, with no body. The page got its data, the node did not."""
    server = Server({**PAGES, "/orders": html(OWN_COPY), "/api/orders": (200, ETAG, as_json()[2])})
    async with server.running(tmp_path) as base, _trusting(tmp_path, server) as context:
        page = await context.new_page()
        statuses: list[int] = []
        page.on("response", lambda r: statuses.append(r.status) if "/api/orders" in r.url else None)
        h = harness(tmp_path, page, definition({"type": "navigate", "url": f"{base}/orders"}, read(base)))
        good = await h.run(FLOW, surface=LANE)
        assert good.status == RunStatus.SUCCEEDED_VERIFIED, good.error
        run = await h.run(FLOW, surface=LANE)
        title = await page.title()
    assert statuses == [200, 304] and title == "rows 2 on 304"  # the page has its rows, and Chrome shows the 304
    assert run.status == RunStatus.FAILED and run.error["code"] == "json_unreadable" and run.attempts == 1, run.error
    assert "cause" not in run.error and h.runner.health.get(f"{API}.{EP}", LANE)["state"] == "healthy"


# ---- passive: a read sends nothing of its own, whatever the body ----


READ_IT = "<script>fetch('/api/orders?page=1').then((r) => r.text()).then(() => { document.title = 'read'; });</script>"
NOTHING_INSIDE = gzip.compress(b"")  # a Content-Length of 20 over a body of nothing: what makes Playwright go to the network


async def _read_once(tmp_path, browser, server: Server, base: str) -> Any:
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()
    home = tmp_path / "once"
    home.mkdir()
    h = harness(home, page, definition({"type": "navigate", "url": f"{base}/orders"}, read(base)))
    run = await h.run(FLOW, surface=LANE)
    await asyncio.sleep(0.5)  # anything late would land here
    await context.close()
    return run


async def test_an_empty_body_under_a_content_length_sends_no_request_of_its_own(tmp_path, browser):
    """Playwright's Response.body() on Chromium asks the network again for an empty body under a
    Content-Length (Network.loadNetworkResource), and the site would see a request the page never made."""
    answer = (200, {"content-type": "application/json", "content-encoding": "gzip"}, NOTHING_INSIDE)
    server = Server({**PAGES, "/orders": html(READ_IT), "/api/orders": answer})
    async with server.running(tmp_path) as base:
        run = await _read_once(tmp_path, browser, server, base)
    assert [r[1] for r in server.seen if r[1].startswith("/api/orders")] == ["/api/orders?page=1"]
    assert run.error["code"] == "json_unreadable", run.error


async def test_a_session_that_cannot_attach_fails_the_read_and_never_goes_through_playwrights_body(tmp_path, browser):
    server = Server({**PAGES, "/orders": html(READ_IT)})
    async with server.running(tmp_path) as base:
        context = await browser.new_context(ignore_https_errors=True)

        async def refused(_page):
            raise RuntimeError("no session")

        context.new_cdp_session = refused
        page = await context.new_page()
        home = tmp_path / "refused"
        home.mkdir()
        h = harness(home, page, definition({"type": "navigate", "url": f"{base}/orders"}, read(base)))
        run = await h.run(FLOW, surface=LANE)
        await asyncio.sleep(0.3)
        await context.close()
    assert run.error["code"] == "json_unreadable", run.error
    assert sum(r[1].startswith("/api/orders") for r in server.seen) == 1


async def test_a_body_that_does_not_finish_is_slow_and_costs_no_request(tmp_path, browser, monkeypatch):
    monkeypatch.setattr("agentos.node.flow_driver.BODY_TIMEOUT_S", 0.5)
    server = Server({**PAGES, "/orders": html(READ_IT)}, slow={"/api/orders": 3.0})
    async with server.running(tmp_path) as base:
        run = await _read_once(tmp_path, browser, server, base)
    assert run.error["code"] == "json_slow", run.error
    assert sum(r[1].startswith("/api/orders") for r in server.seen) == 1


async def test_an_old_page_polling_the_same_url_never_gives_its_body_to_the_new_one(tmp_path, site):
    """A body is found by its turn on the URL, and the page being left takes turns too."""
    poll = "<script>setInterval(() => fetch('/api/orders?page=1').then((r) => r.text()), 20);</script>"
    fresh = {"now": False}

    async def slow_page(route):
        await asyncio.sleep(0.4)
        fresh["now"] = True
        await route.fulfill(status=html()[0], headers=html()[1], body=html()[2])

    async def feed(route):
        status, headers, body = as_json() if fresh["now"] else as_json({"data": {"orders": [{"id": "STALE"}]}})
        await route.fulfill(status=status, headers=headers, body=body)

    site.pages |= {"/poll": html(poll), "/orders": slow_page, "/api/orders": feed}
    await site.goto(f"{HOST}/poll")
    await asyncio.sleep(0.1)
    _, run = await _run(tmp_path, site, NAV, read())
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert site.visits.count("/api/orders?page=1") > 2
    assert run.output["data"]["orders"][0]["number"] == "A-1042"


async def test_two_answers_on_one_url_each_read_as_their_own(site):
    script = "<script>fetch('/api/orders').then((r) => r.text()).then(() => fetch('/api/orders')).then((r) => r.text());</script>"
    served = iter((b'{"n": 1}', b'{"n": 2}'))

    async def feed(route):
        await route.fulfill(status=200, headers={"content-type": "application/json"}, body=next(served))

    site.pages |= {"/orders": html(script), "/api/orders": feed}
    driver = PlaywrightFlowDriver(site, lane_id="test/lane", dwell_ms=(0, 0))
    watch = driver.watch_json(lambda url: "/api/orders" in url, keep=2)
    await driver.navigate(f"{HOST}/orders")
    for _ in range(100):
        if len(watch.responses) == 2:
            break
        await asyncio.sleep(0.02)
    assert [r.body for r in watch.responses] == [b'{"n": 1}', b'{"n": 2}']
    watch.close()


async def test_a_closed_watch_leaves_no_session_on_the_page(site):
    watch = PlaywrightFlowDriver(site, lane_id="test/lane").watch_json(lambda url: True)
    await watch.ready
    session = watch._bodies._session
    await session.send("Network.getCookies")
    watch.close()
    await asyncio.sleep(0.2)
    with pytest.raises(Exception, match="(?i)detached|closed"):
        await session.send("Network.getCookies")


def _firefox_page() -> SimpleNamespace:
    browser = SimpleNamespace(browser_type=SimpleNamespace(name="firefox"))
    return SimpleNamespace(context=SimpleNamespace(browser=browser), on=lambda *_a: None, remove_listener=lambda *_a: None)


class _Answer:
    """A Response as Camoufox gives it, for what `body()` does."""

    url, status, headers = f"{HOST}/api/orders", 200, {}
    request = SimpleNamespace(method="GET")

    def __init__(self, body):
        self.body = body


async def test_on_firefox_a_body_is_read_as_before_and_a_failure_says_which(monkeypatch):
    monkeypatch.setattr("agentos.node.flow_driver.BODY_TIMEOUT_S", 0.05)

    async def good():
        return b"[]"

    async def gone():
        raise RuntimeError("no resource with that id")

    async def hangs():
        await asyncio.sleep(5)

    watch = JsonWatch(_firefox_page(), lambda _u: True, 3, False)
    assert watch.ready is None
    for body in (good, gone, hangs):
        await watch._read(_Answer(body), 0)
    assert [(r.body, r.problem) for r in watch.responses] == [(b"[]", None), (None, "unreadable"), (None, "slow")]

