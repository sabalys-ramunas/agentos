"""The lane egress guard on a real Camoufox lane (docs/design/egress-guard.md).

Run on the node:  OPENAJAR_TEST_SLOT=41 scripts/node-test.sh egress tests/node/test_egress_node.py -q

Throwaway lanes under .node-home/ of this checkout, launched through `CamoufoxEngine`, one browser
at a time on the slot's first display. Two loopback servers on the slot's viewer ports stand in
for the lane's site (SITE, 127.0.0.1) and a foreign host (FOREIGN, 127.0.0.2); the foreign one
answers slowly, so a stopped navigation can be told from one that loaded. No real site.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from openajar.core.config import NodeConfig
from openajar.core.errors import EgressRefused
from openajar.core.models import ActionKind, LaneSpec
from openajar.node import egress
from openajar.node.camoufox.engine import CamoufoxEngine, browser_pids_under
from openajar.node.display import DisplayManagerImpl, kill_processes
from openajar.node.egress import Driver, EffectRule

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb, x11vnc and Camoufox"),
]

ROOT = Path(os.environ.get("OPENAJAR_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
DISPLAY_BASE = int(os.environ.get("OPENAJAR_TEST_DISPLAY_BASE", "410"))
VNC_BASE = int(os.environ.get("OPENAJAR_TEST_VNC_BASE", "5410"))
VIEWER_BASE = int(os.environ.get("OPENAJAR_TEST_VIEWER_BASE", "6410"))
PORT = VIEWER_BASE + 6
SITE = f"http://127.0.0.1:{PORT}"
FOREIGN = f"http://127.0.0.2:{PORT}"
ASSET = bytes(range(256)) * 40
AGENT = Driver(kind="agent", id="node-test", grant_id="g-draft", lists=None, ceiling="draft")
COMMITTER = Driver(kind="agent", id="node-test", grant_id="g-commit", lists=None, ceiling="commit")
PUBLISH = EffectRule("test.publish", "test", "127.0.0.1", r"/publish", effect="commit")


class Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []

    def add(self, row: dict[str, Any]) -> None:
        with self.lock:
            self.rows.append(row)

    def take(self) -> list[dict[str, Any]]:
        with self.lock:
            rows, self.rows = self.rows, []
        return rows


def page_html() -> bytes:
    return (f'<!doctype html><html><head><title>site</title><link rel="stylesheet" href="/s/a.css"></head>'
            f'<body><img src="/s/i.png" width="4" height="4">'
            f'<a id="off" href="{FOREIGN}/slow/link?d=secret">off</a>'
            f'<a id="on" href="/page2">on</a>'
            f'<form id="f" method="post" action="{FOREIGN}/slow/form"><input id="q" name="q" value="secret">'
            f'<button id="send">Send</button></form></body></html>').encode()


def make_server(host: str, rec: Recorder) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            u = urlsplit(self.path)
            rec.add({"host": host, "method": self.command, "path": u.path, "len": len(body), "t": time.monotonic()})
            status, ctype, payload, extra = 200, "text/html", b"", []
            if u.path in ("/page", "/page2"):
                payload = page_html()
            elif u.path == "/meta":
                payload = f'<!doctype html><meta http-equiv="refresh" content="0;url={FOREIGN}/slow/meta">'.encode()
            elif u.path == "/iframe":
                payload = f'<!doctype html><body><iframe src="{FOREIGN}/slow/iframe"></iframe></body>'.encode()
            elif u.path == "/redir":
                status, extra = 302, [("Location", parse_qs(u.query)["to"][0])]
            elif u.path.startswith("/s/"):
                ctype = "image/png" if u.path.endswith(".png") else "text/css"
                payload = ASSET if ctype == "image/png" else b"body{margin:0}"
                extra = [("Cache-Control", "public, max-age=3600"), ("ETag", f'"{u.path}"')]
            elif u.path.startswith("/slow/"):
                time.sleep(1.5)
                payload = b"<!doctype html><title>foreign</title><body>foreign</body>"
            elif u.path in ("/publish", "/collect"):
                ctype, payload = "application/json", b'{"ok": true}'
                extra = [("Access-Control-Allow-Origin", "*")]
            else:
                status = 404
            try:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(payload)
            except OSError:  # the browser stopped the request
                pass

        do_GET = do_POST = _handle

    server = ThreadingHTTPServer((host, PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def rec():
    recorder = Recorder()
    servers = [make_server("127.0.0.1", recorder), make_server("127.0.0.2", recorder)]
    try:
        yield recorder
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


@pytest.fixture
async def rig():
    home = ROOT / f"egress-{uuid.uuid4().hex[:8]}"
    home.mkdir(parents=True)
    config = NodeConfig(home=home, display_base=DISPLAY_BASE, vnc_base=VNC_BASE, viewer_base=VIEWER_BASE,
                        max_slots=4, self_check=False)
    displays = DisplayManagerImpl(config)
    await displays.sweep()
    engine = CamoufoxEngine()
    engine.allow_private_network = True  # only so these lanes can reach the loopback servers
    yield config, displays, engine
    await displays.release_all()
    leftover = browser_pids_under(home)
    await kill_processes(leftover)
    shutil.rmtree(home, ignore_errors=True)
    assert not leftover, f"browsers left running after the test: {leftover}"


class Lane:
    def __init__(self, config, displays, engine, spec):
        self.config, self.displays, self.engine, self.spec = config, displays, engine, spec
        self.session: Any = None

    async def __aenter__(self):
        fp = self.config.fingerprint_path(self.spec.id)
        viewer = await self.displays.acquire(self.spec.id, size=self.engine.display_size(fp))
        self.session = await self.engine.launch(self.spec, profile_dir=self.config.profile_dir(self.spec.id),
                                                fingerprint_path=fp, display=viewer.display,
                                                xauthority=viewer.xauthority, proxy=None)
        return self.session

    async def __aexit__(self, *exc):
        await self.session.close()
        await self.displays.release(self.spec.id)


def foreign(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r["host"] == "127.0.0.2"]


def static(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r["host"] == "127.0.0.1" and r["path"].startswith("/s/")]


async def test_the_guard_installs_no_route_and_the_cache_keeps_working(rig, rec):
    config, displays, engine = rig
    spec = LaneSpec(id="test/egress-cache", sites=["127.0.0.1"])
    async with Lane(config, displays, engine, spec) as session:
        assert session.guard is not None and session.guard.sites == ("127.0.0.1",)
        session.guard.drive(AGENT)
        page = await session.page()
        await page.goto(f"{SITE}/page", wait_until="load")
        await page.wait_for_timeout(300)
        first = static(rec.take())
        await page.goto(f"{SITE}/page2", wait_until="load")
        await page.wait_for_timeout(300)
        second = static(rec.take())
        assert session.routes.patterns() == []
    assert len(first) == 2 and second == [], (first, second)  # the second page's assets came from the cache


@pytest.mark.parametrize("case", ["js_location", "link_click", "form_post", "meta_refresh", "redirect", "iframe",
                                  "popup"])
async def test_every_kind_of_refused_navigation_is_stopped(rig, rec, case):
    config, displays, engine = rig
    spec = LaneSpec(id="test/egress-nav", sites=["127.0.0.1"])
    async with Lane(config, displays, engine, spec) as session:
        guard = session.guard
        guard.drive(AGENT)
        page = await session.page()
        await page.goto(f"{SITE}/page", wait_until="load")
        rec.take()
        pages_before = len(session.context.pages)
        trigger = {
            "js_location": lambda: page.evaluate(f"location.href = '{FOREIGN}/slow/js?d=secret'"),
            "link_click": lambda: page.click("#off"),
            "form_post": lambda: page.click("#send"),
            "meta_refresh": lambda: page.goto(f"{SITE}/meta", wait_until="commit"),
            "redirect": lambda: page.evaluate(f"location.href = '{SITE}/redir?to={FOREIGN}/slow/redir'"),
            "iframe": lambda: page.goto(f"{SITE}/iframe", wait_until="commit"),
            "popup": lambda: page.evaluate(f"window.open('{FOREIGN}/slow/popup')"),
        }[case]
        with contextlib.suppress(Exception):  # a click whose navigation was stopped can time out its wait
            await trigger()
        # A fresh lane's first meta refresh or popup can take 4 s or more to start (seen on the node).
        for _ in range(40):
            if guard.blocks_since(0):
                break
            await page.wait_for_timeout(250)
        await page.wait_for_timeout(2000)  # longer than the foreign host takes to answer
        kinds = [b.kind for b in guard.blocks_since(0)]
        url_after = page.url
        pages_after = len([p for p in session.context.pages if not p.is_closed()])
        frames = [f.url for f in page.frames]
        events = [r for r in guard.trace.read() if r.get("kind") == "blocked"]
    expected = {"redirect": "redirect", "iframe": "subframe", "popup": "popup"}.get(case, "navigation")
    assert kinds and kinds[0] == expected, (kinds, frames, url_after)
    assert url_after.startswith(SITE), url_after  # the page stayed on the lane's site
    assert not any(u.startswith(FOREIGN) for u in frames), frames
    assert pages_after == pages_before, "the refused popup was closed"
    assert events and events[0]["host"] == "127.0.0.2" and "secret" not in str(events)
    hit = foreign(rec.take())
    assert len(hit) <= 1  # the request may reach the host (containment, not prevention); nothing more follows


async def test_a_click_on_a_refused_link_or_form_sends_nothing(rig, rec):
    from openajar.node.snapshot import take_snapshot

    config, displays, engine = rig
    spec = LaneSpec(id="test/egress-act", sites=["127.0.0.1"])
    async with Lane(config, displays, engine, spec) as session:
        guard = session.guard
        guard.drive(AGENT)
        page = await session.page()
        await page.goto(f"{SITE}/page", wait_until="load")
        snap = await take_snapshot(page, max_chars=10_000)
        refs: dict[str, str] = {}
        for m in re.finditer(r'- (\w+)(?: "([^"]*)")?[^\n]*?\[ref=(\w+)\]', snap.text):
            refs.setdefault(m.group(2) or m.group(1), m.group(3))
        rec.take()
        with pytest.raises(EgressRefused, match="127.0.0.2 is not on this browser's site list"):
            await guard.check_action(page, refs["off"], ActionKind.CLICK, None)
        with pytest.raises(EgressRefused):
            await guard.check_action(page, refs["Send"], ActionKind.CLICK, None)
        textbox = refs.get("textbox") or next(v for k, v in refs.items() if k not in ("off", "on", "Send"))
        with pytest.raises(EgressRefused):
            await guard.check_action(page, textbox, ActionKind.PRESS, "Enter")
        await guard.check_action(page, refs["on"], ActionKind.CLICK, None)
        await page.wait_for_timeout(500)
        blocks = guard.blocks_since(0)
    assert [b.method for b in blocks] == ["GET", "POST", "POST"] and all(b.prevented for b in blocks)
    assert foreign(rec.take()) == []


async def test_strict_lanes_block_before_sending_and_route_only_while_acting(rig, rec):
    config, displays, engine = rig
    spec = LaneSpec(id="test/egress-strict", sites=["127.0.0.1"], egress_strict=True)
    async with Lane(config, displays, engine, spec) as session:
        guard = session.guard
        guard.drive(AGENT)
        page = await session.page()
        await page.goto(f"{SITE}/page", wait_until="load")
        rec.take()
        async with guard.acting():
            assert session.routes.patterns() == ["**/*"]
            got = await page.evaluate(f"""async () => {{
              try {{ await fetch('{FOREIGN}/collect', {{method: 'POST', body: 'secret'}}); return 'sent'; }}
              catch (e) {{ return 'blocked'; }} }}""")
            ok = await page.evaluate(f"""async () => (await fetch('{SITE}/collect', {{method: 'POST',
              body: 'fine'}})).status""")
        assert session.routes.patterns() == []
        blocks = guard.blocks_since(0)
    assert got == "blocked" and ok == 200
    assert [(b.rule, b.prevented) for b in blocks] == [("non_get", True)]
    assert foreign(rec.take()) == []


async def test_commits_are_held_outside_the_grant_and_observed_inside(rig, rec, monkeypatch):
    monkeypatch.setattr(egress.rules, "EFFECT_RULES", (PUBLISH,))
    config, displays, engine = rig
    spec = LaneSpec(id="test/egress-commit")
    async with Lane(config, displays, engine, spec) as session:
        guard = session.guard
        held: list = []
        guard.on_violation = lambda block: held.append(block)
        page = await session.page()
        await page.goto(f"{SITE}/page", wait_until="load")
        post = f"async () => (await fetch('{SITE}/publish', {{method: 'POST', body: 'a caption'}})).status"
        guard.drive(AGENT)
        assert await page.evaluate(post) == 200  # not strict: it went out, and the lane is held for it
        await page.wait_for_timeout(300)
        guard.drive(COMMITTER)
        assert await page.evaluate(post) == 200
        await page.wait_for_timeout(300)
        rows = guard.trace.read()
    assert [b.rule for b in held] == ["commit"]
    observed = [r for r in rows if r["kind"] == "observed_commit"]
    assert len(observed) == 1 and observed[0]["size"] == len("a caption") and observed[0]["path"] == "/publish"
    assert "a caption" not in str(rows)
