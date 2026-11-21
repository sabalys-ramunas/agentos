"""The signed-out check (AUD-22, openajar.node.signed_out) on a real Camoufox lane.

Run on the node:  OPENAJAR_TEST_SLOT=51 scripts/node-test.sh devbatch tests/node/test_signed_out_node.py

Nothing leaves the node: the lane's proxy is a loopback stand-in that asks for credentials and
answers http://social.example/ itself, so the lane and the throwaway browser can reach the site
only through it. The author signs in (a cookie); one post shows to anyone, the other only with
the author's cookie, rendered by script after a delay as a JavaScript shell would. The check
must see the first and not the second, send no cookie, go through the same proxy with the same
credentials, show the site the same device as the lane (the fingerprint page's own dump), use a
display other than the lane's, and leave nothing in the lane's profile.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from test_fingerprint_pin_node import comparable, differences

from openajar.core.config import NodeConfig
from openajar.core.models import LaneSpec, ProxyConfig
from openajar.core.runtime import Runtime
from openajar.node import flow_driver as fd
from openajar.node.camoufox.engine import PIN_META, CamoufoxEngine, browser_pids_under
from openajar.node.display import DisplayManagerImpl, kill_processes
from openajar.node.lanes import LaneManagerImpl
from openajar.node.store import LaneStore

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb and Camoufox"),
]

ROOT = Path(os.environ.get("OPENAJAR_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
DISPLAY_BASE = int(os.environ.get("OPENAJAR_TEST_DISPLAY_BASE", "110"))
VNC_BASE = int(os.environ.get("OPENAJAR_TEST_VNC_BASE", "5810"))
VIEWER_BASE = int(os.environ.get("OPENAJAR_TEST_VIEWER_BASE", "6110"))
FINGERPRINT_PAGE = (Path(__file__).resolve().parents[1] / "fixtures" / "site" / "fingerprint.html").read_text()

LANE = "biz-a/social"
SITE = "http://social.example"
TEXT = "Autumn at the lake, with the first frost on the reeds"
USER, PASSWORD = "lane-user", "lane-proxy-secret"
AUTH = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

POST_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Post</title></head>
<body><main id="post">loading</main><script>
setTimeout(async () => {
  const r = await fetch("/api" + location.pathname, {credentials: "include", cache: "no-store"});
  document.getElementById("post").textContent = await r.text();
}, 700);
</script></body></html>"""

REPORT = """<script>
(function wait() {
  const dump = document.getElementById("dump");
  if (dump.dataset.state !== "done") return setTimeout(wait, 100);
  fetch("/report" + location.search, {method: "POST", body: dump.textContent}).then(() => {
    document.body.insertAdjacentHTML("beforeend", "<p>fingerprint reported</p>");
  });
})();
</script>"""


class Site:
    """What the stand-in proxy saw and was sent."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.reports: dict[str, dict] = {}

    def seen(self, path: str) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests if r["path"] == path]


def _proxy(site: Site) -> ThreadingHTTPServer:
    class Proxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, status: int, body: bytes, ctype: str = "text/html", extra: tuple = ()) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.headers.get("Proxy-Authorization") != AUTH:
                self._send(407, b"", extra=(("Proxy-Authenticate", 'Basic realm="lane"'),))
                return
            u = urlsplit(self.path)
            if u.hostname != "social.example":
                self._send(502, b"no such host here")
                return
            cookie = self.headers.get("Cookie") or ""
            with site.lock:
                site.requests.append({"path": u.path, "query": u.query, "cookie": cookie,
                                      "ua": self.headers.get("User-Agent"),
                                      "headers": {k.lower(): v for k, v in self.headers.items()
                                                  if k.lower() in ("user-agent", "accept", "accept-language",
                                                                   "accept-encoding", "upgrade-insecure-requests")}})
            if u.path == "/login":
                self._send(200, b"<p>signed in</p>", extra=(("Set-Cookie", "session=author; Path=/; Max-Age=86400"),))
            elif u.path.startswith("/p/"):
                self._send(200, POST_PAGE.encode())
            elif u.path.startswith("/api/p/"):
                shown = u.path.endswith("/shown") or "session=author" in cookie
                self._send(200, (TEXT if shown else "This post is not available").encode(), "text/plain")
            elif u.path == "/fp":
                self._send(200, FINGERPRINT_PAGE.replace("</body>", REPORT + "</body>").encode())
            elif u.path == "/headers":
                skip = {"host", "connection", "cookie", "referer", "proxy-authorization", "proxy-connection"}
                body = json.dumps({k.lower(): v for k, v in self.headers.items() if k.lower() not in skip},
                                  sort_keys=True).encode()
                self._send(200, body, "application/json")
            else:
                self._send(404, b"<p>not found</p>")

        def do_POST(self) -> None:
            if self.headers.get("Proxy-Authorization") != AUTH:
                self._send(407, b"", extra=(("Proxy-Authenticate", 'Basic realm="lane"'),))
                return
            u = urlsplit(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if u.path == "/report":
                with site.lock:
                    site.reports[parse_qs(u.query)["who"][0]] = json.loads(body)
            self._send(204, b"")

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), Proxy)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def site():
    found = Site()
    server = _proxy(found)
    found.port = server.server_address[1]
    try:
        yield found
    finally:
        server.shutdown()
        server.server_close()


class Displays(DisplayManagerImpl):
    acquired: list[str]

    async def acquire(self, lane_id, *, size=None):
        self.acquired.append(lane_id)
        return await super().acquire(lane_id, size=size)


@pytest.fixture
async def lanes(site):
    home = ROOT / f"signedout-{uuid.uuid4().hex[:8]}"
    home.mkdir(parents=True)
    config = NodeConfig(home=home, display_base=DISPLAY_BASE, vnc_base=VNC_BASE, viewer_base=VIEWER_BASE,
                        max_slots=4, max_warm_lanes=1, self_check=False, control_idle_seconds=600,
                        memory_floor_mb=0)  # the dev node runs other agents (free memory swings 200 to 1800 MB)
    engine = CamoufoxEngine(extra_options={"geoip": False})  # the stand-in has no exit IP to look up
    engine.allow_private_network = True  # the stand-in proxy is on loopback
    proxy = ProxyConfig(label="stand-in", host="127.0.0.1", port=site.port, username=USER, password=PASSWORD)
    displays = Displays(config)
    displays.acquired = []
    mgr = LaneManagerImpl(config, LaneStore(config.db_path), engine, displays, lambda: {"stand-in": proxy})
    await mgr.start()
    try:
        yield mgr
    finally:
        await mgr.shutdown()
        leftover = browser_pids_under(home)
        await kill_processes(leftover)
        shutil.rmtree(home, ignore_errors=True)
        assert not leftover, f"browsers left running after the test: {leftover}"


def _comparable(dump: dict, canvas_noise: bool) -> dict:
    """test_fingerprint_pin_node.comparable, for a page on plain http: crypto.subtle (the page's
    digests) is missing outside a secure context, so those sections are the same error in both."""
    sections = {k: v for k, v in dump.items() if isinstance(v, dict) or k != "canvas"}
    if isinstance(dump.get("canvas"), dict):
        return comparable(dump, canvas_noise)
    return sections


def _last_container(profile: Path) -> int | None:
    path = profile / "containers.json"
    return json.loads(path.read_text()).get("lastUserContextId") if path.exists() else None


def _temp_profiles() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("playwright_firefoxdev_profile-*")}


async def test_a_signed_out_visitor_sees_the_public_post_and_not_the_held_one(lanes, site):
    mgr = lanes
    await mgr.create(LaneSpec(id=LANE, proxy_label="stand-in", sites=["social.example"]))
    marker = "m" + uuid.uuid4().hex[:12]
    temp_before = _temp_profiles()
    async with mgr.using(LANE) as page:
        await page.goto(f"{SITE}/login")
        await page.goto(f"{SITE}/p/hidden")  # the author sees the held post
        await page.wait_for_function("t => document.body.innerText.includes(t)", arg=TEXT, timeout=15_000)
        await page.goto(f"{SITE}/fp?who=lane")
        await page.wait_for_function("() => document.body.innerText.includes('fingerprint reported')",
                                     timeout=60_000)
        lane_display = mgr.view(LANE).display
        containers_before = _last_container(mgr.config.profile_dir(LANE))
        driver_factory = fd.driver_factory(Runtime(lanes=mgr))
    # through the flow driver, as the runner calls it (the factory takes the lane again)
    async with driver_factory(LANE) as driver:
        shown = await driver.read_signed_out(f"{SITE}/p/shown?{marker}=1", TEXT, timeout_ms=15_000)
        held = await driver.read_signed_out(f"{SITE}/p/hidden?{marker}=2", TEXT, timeout_ms=6_000)
        fp = await mgr.read_signed_out(LANE, f"{SITE}/fp?who=signed-out&{marker}=3", "fingerprint reported",
                                       timeout_ms=60_000)
    assert shown["visible"] is True and shown["status"] == 200, shown
    assert held["visible"] is False and "did not show" in held["note"], held
    assert fp["visible"] is True, fp

    # no cookie went out signed out, every request went through the proxy with its credentials
    # (the proxy answers nothing without them), with the lane's user agent
    signed_out = [r for r in site.requests if marker in r["query"]]
    assert len(signed_out) == 3 and all(r["cookie"] == "" for r in signed_out)
    api = site.seen("/api/p/hidden")
    assert [r["cookie"] for r in api] == ["session=author", ""]  # the author's read, then the visitor's
    assert len({r["ua"] for r in site.requests}) == 1

    # the same device, as the site's own script reads it
    pin = json.loads(mgr.config.fingerprint_path(LANE).read_text())[PIN_META]
    lane_fp, visitor_fp = site.reports["lane"], site.reports["signed-out"]
    for section in ("navigator", "screen", "window", "intl"):
        assert isinstance(lane_fp.get(section), dict), (section, lane_fp.get(section))
    lane_fp, visitor_fp = ({k: v for k, v in d.items() if k != "headers"} for d in (lane_fp, visitor_fp))
    pages = site.seen("/fp")  # the headers of the page loads, as the proxy got them
    assert len(pages) == 2 and pages[0]["headers"] == pages[1]["headers"], pages
    diff = differences(_comparable(lane_fp, pin["canvas_noise"]), _comparable(visitor_fp, pin["canvas_noise"]))
    assert diff == [], "the signed-out browser is another device:\n" + "\n".join(diff)

    # its own display, given back; the lane's display stays the lane's
    assert f"{LANE}+signed-out" in mgr.displays.acquired and mgr.displays.get(f"{LANE}+signed-out") is None
    assert mgr.view(LANE).display == lane_display

    # nothing of the visit in the lane's profile, and the throwaway profile is gone
    await mgr.close(LANE)
    profile = mgr.config.profile_dir(LANE)
    hits = [str(p.relative_to(profile)) for p in profile.rglob("*")
            if p.is_file() and marker.encode() in p.read_bytes()]
    assert hits == [], f"the signed-out visit left traces in the lane's profile: {hits}"
    assert _last_container(profile) == containers_before  # no container was made in the lane's browser
    assert _temp_profiles() <= temp_before


async def test_a_cold_lane_cannot_lend_its_browser(lanes):
    from openajar.core.errors import ServiceUnavailable

    await lanes.create(LaneSpec(id=LANE, proxy_label="stand-in"))
    with pytest.raises(ServiceUnavailable, match="not open"):
        await lanes.read_signed_out(LANE, f"{SITE}/p/shown", TEXT)


def test_the_snippet_survives_a_cut_caption():
    from openajar.node.signed_out import snippet

    long = "Autumn  at the\nlake " + "and more words " * 10
    cut = snippet(long)
    assert cut.startswith("autumn at the lake and more") and len(cut) <= 60 and not cut.endswith(" ")
    assert re.fullmatch(r"[a-z ]+", cut)
