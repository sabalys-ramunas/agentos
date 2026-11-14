"""Node test harness: the fixture site, an Xvfb display and one headful Camoufox.

Runs only on a Linux node (tests here are marked `node`). The display and the site port are the
first of the slot's ranges that scripts/node-test.sh exports (the interaction
agent's slot 12 without them) and can be overridden with AGENTOS_TEST_DISPLAY and
AGENTOS_TEST_SITE_PORT. One browser for the whole session (RAM on the node is shared); every
test gets a fresh context whose requests to anything but the fixture site are aborted, so no
test can ever reach a real website.

Camoufox and Playwright are imported inside the fixtures: on the Mac this file is collected
(and its tests deselected) without a browser.

Two switches for CI (.github/workflows/ci.yml, job "node"):
- AGENTOS_TEST_SMOKE=<file> runs only the tests the file lists (tests/node/ci-smoke.txt) and those
  marked `ci_smoke`. A listed id that matches no test fails the run, so a rename cannot drop one.
- AGENTOS_TEST_OFFLINE=1 is for a machine with no way out: Camoufox's geoip asks public services
  for this host's IP before every launch, so the lookup answers AGENTOS_TEST_EXIT_IP (default
  8.8.8.8, located from the local GeoIP database) and this host has no IPv6. No request is made.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
import pytest_asyncio

SITE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "site"
# scripts/node-test.sh exports the slot's ranges; without them this is the interaction slot (12).
DISPLAY = os.environ.get("AGENTOS_TEST_DISPLAY") or f":{os.environ.get('AGENTOS_TEST_DISPLAY_BASE', '120')}"
SITE_PORT = int(os.environ.get("AGENTOS_TEST_SITE_PORT") or os.environ.get("AGENTOS_TEST_VIEWER_BASE", "6120"))
SCREEN = "1280x900x24"
SMOKE_LIST = os.environ.get("AGENTOS_TEST_SMOKE")
OFFLINE = os.environ.get("AGENTOS_TEST_OFFLINE") == "1"
OFFLINE_EXIT_IP = os.environ.get("AGENTOS_TEST_EXIT_IP", "8.8.8.8")


def _smoke_ids(path: str) -> set[str]:
    lines = (line.split("#", 1)[0].strip() for line in Path(path).read_text().splitlines())
    return {line for line in lines if line}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not SMOKE_LIST:
        return
    wanted = _smoke_ids(SMOKE_LIST)
    keep, drop, matched = [], [], set()
    for item in items:
        ids = {item.nodeid, item.nodeid.split("[", 1)[0]} & wanted
        if ids or item.get_closest_marker("ci_smoke"):
            keep.append(item)
            matched |= ids
        else:
            drop.append(item)
    if missing := sorted(wanted - matched):
        raise pytest.UsageError(f"{SMOKE_LIST} lists tests that were not collected: {', '.join(missing)}")
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep


@pytest.fixture(scope="session", autouse=True)
def _offline_exit_ip() -> Iterator[None]:
    if not OFFLINE:
        yield
        return
    import camoufox.ip  # offline was asked for: a missing browser is a broken setup, not a skip
    import camoufox.utils
    saved = [(m, name, getattr(m, name)) for m in (camoufox.ip, camoufox.utils) for name in ("public_ip", "public_ipv6")]
    for module in (camoufox.ip, camoufox.utils):
        module.public_ip = lambda proxy=None: OFFLINE_EXIT_IP
        module.public_ipv6 = lambda: None
    try:
        yield
    finally:
        for module, name, value in saved:
            setattr(module, name, value)


class _SiteHandler(SimpleHTTPRequestHandler):
    """Static fixture pages, plus /status/<code> pages for rate limits and blocks. A POST to /like
    is answered and recorded in `posts`, so a test can tell a write that reached the site."""

    posts: ClassVar[list[str]] = []

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/status/"):
            tail = path.removeprefix("/status/")
            code = int(tail) if tail.isdigit() and 200 <= int(tail) <= 599 else 404
            body = (f"<!doctype html><html lang=\"en\"><head><title>Status {code}</title></head>"
                    f"<body><h1>Status {code}</h1><p>A fixture page.</p></body></html>").encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if code == 429:
                self.send_header("Retry-After", "30")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/like":
            self.send_error(501, "Unsupported method ('POST')")
            return
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _SiteHandler.posts.append(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


@pytest.fixture
def site_posts(site_url: str) -> list[str]:
    """The POSTs to /like the fixture site received during the test."""
    _SiteHandler.posts.clear()
    return _SiteHandler.posts


@pytest.fixture(scope="session")
def site_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", SITE_PORT), _SiteHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="fixture-site", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{SITE_PORT}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="session")
def xvfb_display() -> Iterator[str]:
    if shutil.which("Xvfb") is None:
        pytest.skip("Xvfb is not installed")
    number = DISPLAY.lstrip(":")
    socket = Path(f"/tmp/.X11-unix/X{number}")
    if socket.exists():
        pytest.fail(f"display {DISPLAY} is already in use; stop it or set AGENTOS_TEST_DISPLAY")
    proc = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", SCREEN, "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not socket.exists():
            if proc.poll() is not None:
                pytest.fail(f"Xvfb {DISPLAY} exited with code {proc.returncode}")
            if time.monotonic() > deadline:
                pytest.fail(f"Xvfb {DISPLAY} did not start")
            time.sleep(0.1)
        yield DISPLAY
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def browser(xvfb_display: str) -> AsyncIterator[Any]:
    from camoufox.async_api import AsyncCamoufox

    env = {**os.environ, "DISPLAY": xvfb_display}
    async with AsyncCamoufox(headless=False, humanize=True, geoip=False, env=env,
                             i_know_what_im_doing=True) as launched:
        yield launched


@pytest_asyncio.fixture(loop_scope="session")
async def page(browser: Any, site_url: str) -> AsyncIterator[Any]:
    """A fresh page in its own context. Only the fixture site is reachable."""
    pg = await browser.new_page()
    await pg.context.route(lambda url: not url.startswith(site_url + "/"), lambda route: route.abort())
    try:
        yield pg
    finally:
        await pg.context.close()
