"""The lane check against a real Camoufox: what the DNS and cookie rules assume, measured.

Run on the node:  OPENAJAR_TEST_SLOT=45 scripts/node-test.sh selfcheck tests/node/test_lanecheck_node.py
Uses slot 45's display (:450 + n) and ports (6450 to 6459). Throwaway profiles under tmp_path,
a local proxy and local pages only: no test reaches a real website.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid

import pytest

from openajar.core.models import LaneSpec, ProxyConfig
from openajar.node.camoufox.engine import CamoufoxEngine

pytestmark = pytest.mark.node

DISPLAY = f":{int(os.environ.get('OPENAJAR_TEST_DISPLAY_BASE', '450')) + 1}"
PORT_BASE = int(os.environ.get("OPENAJAR_TEST_VIEWER_BASE", "6450"))
PROXY_PORT = PORT_BASE + 1
SITE_PORT = PORT_BASE + 2
# HTTPS-First would CONNECT to port 443 first; the fixture proxy only serves plain pages
NO_HTTPS_FIRST = {"dom.security.https_first": False, "dom.security.https_first_schemeless": False}


@pytest.fixture(scope="module")
def display():
    if shutil.which("Xvfb") is None:
        pytest.skip("Xvfb not installed")
    num = DISPLAY.lstrip(":")
    proc = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + 10
    while not os.path.exists(f"/tmp/.X11-unix/X{num}"):
        if time.time() > deadline:
            proc.kill()
            pytest.fail("Xvfb did not start")
        time.sleep(0.1)
    yield DISPLAY
    proc.terminate()
    proc.wait(timeout=5)


class _PageProxy:
    """An HTTP proxy that answers every plain GET with one page and records what it was asked."""

    def __init__(self, body: bytes):
        self.body = body
        self.requests: list[str] = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", PROXY_PORT))
        self.sock.listen(32)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        self.sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                try:
                    first = conn.recv(8192).split(b"\r\n", 1)[0].decode("latin-1")
                    self.requests.append(first)
                    if first.startswith("GET "):
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                                     b"Content-Length: " + str(len(self.body)).encode() + b"\r\n\r\n" + self.body)
                    else:
                        conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
        self.sock.close()


class _DnsWatch:
    """tcpdump on every interface, port 53: which of our made-up names the node itself asked for."""

    def __init__(self):
        if shutil.which("tcpdump") is None or os.geteuid() != 0:
            pytest.skip("needs tcpdump as root")
        self.proc = subprocess.Popen(["tcpdump", "-i", "any", "-n", "-l", "-A", "-s", "0", "port", "53"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                     errors="replace")
        self.lines: list[str] = []
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        time.sleep(1.0)

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line)

    def seen(self, token: str) -> bool:
        return any(token in line for line in self.lines)

    def close(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)


async def _launch(tmp_path, display, proxy, extra_prefs=None):
    opts = {"geoip": False}
    engine = CamoufoxEngine(extra_options=opts)
    if extra_prefs:
        base = engine._options

        def with_prefs(*args, **kwargs):
            out = base(*args, **kwargs)
            out["firefox_user_prefs"] = {**out["firefox_user_prefs"], **extra_prefs}
            return out

        engine._options = with_prefs  # type: ignore[method-assign]
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    return await engine.launch(LaneSpec(id="qa/check"), profile_dir=profile,
                               fingerprint_path=tmp_path / "fp.json", display=display, proxy=proxy)


async def test_proxied_lane_resolves_no_names_on_the_node(tmp_path, display):
    """What a proxied lane's browser resolves through the node's own resolver: the page's host,
    a dns-prefetch hint, a link on the page and a fetch(). All four go to the proxy by name."""
    token = uuid.uuid4().hex[:10]
    names = {kind: f"openajar-{kind}-{token}.example.com" for kind in ("page", "hint", "link", "fetch")}
    body = (f'<!doctype html><html><head><link rel="dns-prefetch" href="//{names["hint"]}">'
            f'<link rel="preconnect" href="http://{names["hint"]}"></head><body>'
            f'<a href="http://{names["link"]}/">a link</a></body></html>').encode()
    proxy_server = _PageProxy(body)
    watch = _DnsWatch()
    session = None
    try:
        session = await _launch(tmp_path, display, ProxyConfig(label="t", host="127.0.0.1", port=PROXY_PORT),
                                extra_prefs=NO_HTTPS_FIRST)
        page = await session.page()
        await page.goto(f"http://{names['page']}/", wait_until="load")
        await page.hover("a")
        await page.evaluate("u => fetch(u, {mode: 'no-cors'}).catch(() => null)", f"http://{names['fetch']}/")
        await asyncio.sleep(3.0)
        try:  # the control: a name this process resolves on the node must show in the capture
            socket.getaddrinfo(f"openajar-control-{token}.example.com", 80)
        except OSError:
            pass
        await asyncio.sleep(1.0)
    finally:
        if session is not None:
            await session.close()
        watch.close()
        proxy_server.close()
    leaked = sorted(kind for kind, name in names.items() if watch.seen(name.split(".")[0]))
    asked = proxy_server.requests
    print("proxy saw:", asked)
    print("resolved on the node:", leaked)
    assert watch.seen(f"openajar-control-{token}"), "the capture saw nothing; the test proves nothing"
    assert any(names["page"] in r for r in asked)
    assert leaked == [], f"the node's resolver was asked for {leaked}"


class _StunSink:
    """A UDP port that counts the STUN binding requests a browser sends it."""

    def __init__(self, port: int):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(0.2)
        self.packets = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        while not self.stop.is_set():
            try:
                self.sock.recvfrom(2048)
            except OSError:
                continue
            self.packets += 1

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
        self.sock.close()


ICE = """async (stun) => {
  const pc = new RTCPeerConnection({iceServers: [{urls: stun}]});
  const found = [];
  pc.onicecandidate = (e) => { if (e.candidate) found.push(e.candidate.candidate); };
  pc.createDataChannel('x');
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise((r) => setTimeout(r, 4000));
  pc.close();
  return found;
}"""


def _node_address() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outgoing interface
        return s.getsockname()[0]


IFRAME_ICE = """async (stun) => {
  const f = document.createElement('iframe');
  document.body.appendChild(f);
  const w = f.contentWindow;
  const pc = new w.RTCPeerConnection({iceServers: [{urls: stun}]});
  const found = [];
  pc.onicecandidate = (e) => { if (e.candidate) found.push(e.candidate.candidate); };
  pc.createDataChannel('x');
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise((r) => setTimeout(r, 4000));
  pc.close();
  return found;
}"""
PAGE = "http://openajar-rtc.example.com/"
SHIPPED = {"media.peerconnection.ice.proxy_only": False}  # Camoufox's own default, without the engine's pref


# (proxied, page, script, extra prefs, does UDP reach the STUN port). The rule in
# openajar.stealth.lanerules.judge_webrtc rests on these rows: Camoufox's shipped
# proxy_only_if_behind_proxy holds on a page loaded through the proxy, but not in an about:blank
# document or a blank iframe a page makes, so it is a warning; proxy_only closes both, and the
# engine sets it on every proxied lane (WEBRTC_PROXY_PREFS). If a Camoufox update stops the leak,
# the "leaks" rows fail here and the rule can relax.
WEBRTC_ROWS = [
    (True, PAGE, ICE, SHIPPED, False),
    (True, PAGE, IFRAME_ICE, SHIPPED, True),
    (True, "about:blank", ICE, SHIPPED, True),
    (True, "about:blank", ICE, {}, False),
    (True, PAGE, IFRAME_ICE, {}, False),
    (False, "about:blank", ICE, {}, True),
]


@pytest.mark.parametrize(("proxied", "where", "script", "prefs", "leaks"), WEBRTC_ROWS,
                         ids=["shipped-page", "shipped-blank-iframe-leaks", "shipped-blank-leaks",
                              "engine-blank", "engine-page-blank-iframe", "control-no-proxy"])
async def test_webrtc_and_the_proxy(tmp_path, display, proxied, where, script, prefs, leaks):
    """Whether WebRTC sends STUN over UDP from the node itself, around the lane's proxy. The STUN
    server is a UDP port on the node's own address, so a packet that reaches it is one a site's
    STUN server would see from the node's IP. Without a proxy the same page reaches it (the control)."""
    from openajar.stealth import lanerules

    sink = _StunSink(SITE_PORT)
    proxy_server = _PageProxy(b"<!doctype html><title>x</title><body></body>")
    node_ip = _node_address()
    session = None
    try:
        proxy = ProxyConfig(label="t", host="127.0.0.1", port=PROXY_PORT) if proxied else None
        session = await _launch(tmp_path, display, proxy, extra_prefs={**NO_HTTPS_FIRST, **prefs})
        page = await session.page()
        await page.goto(where)
        candidates = await page.evaluate(script, f"stun:{node_ip}:{SITE_PORT}")
    finally:
        if session is not None:
            await session.close()
        proxy_server.close()
        sink.close()
    print(where, prefs, "candidates:", len(candidates), "stun packets:", sink.packets)
    host_ips = [c for c in candidates if " typ host" in c and ".local" not in c]
    assert host_ips == [], f"a host candidate shows a real address: {host_ips}"
    assert (sink.packets > 0) is leaks, f"expected leaks={leaks}, saw {sink.packets} STUN packets"
    if proxied:  # what the lane check says about these prefs matches what happened
        from openajar.node.camoufox.engine import WEBRTC_PROXY_PREFS

        judged = {"media.peerconnection.enabled": True,
                  "media.peerconnection.ice.proxy_only_if_behind_proxy": True,
                  "media.peerconnection.ice.obfuscate_host_addresses": True, **WEBRTC_PROXY_PREFS, **prefs}
        verdict = lanerules.judge_webrtc(judged, proxied=True, known=True, user_js=tmp_path / "user.js").level
        closed = judged["media.peerconnection.ice.proxy_only"] is True
        assert verdict == (lanerules.OK if closed else lanerules.WARN)


COOKIE_PAGE = b"""<!doctype html><title>cookies</title><script>
let i = 0;
setInterval(() => { i++; document.cookie = "c" + (i % 100) + "=v" + i + "; max-age=86400; path=/"; }, 15);
</script>"""


async def test_cookie_file_reads_while_the_browser_writes(tmp_path, display):
    """The reader copies cookies.sqlite while Camoufox keeps writing it (a page rewrites one of 100
    cookies every 15 ms): every read works, sees new values as they are flushed, leaves nothing next
    to the file, and the browser goes on writing (no lock of ours ever stops it)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from openajar.core.system import Host
    from openajar.stealth.lanecheck import _camou_prefs
    from openajar.stealth.lanecookies import read_cookie_db

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", "sessionid=abc; Max-Age=86400; Path=/")
            self.end_headers()
            self.wfile.write(COOKIE_PAGE)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", SITE_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    engine = CamoufoxEngine(extra_options={"geoip": False})
    engine.allow_private_network = True
    profile = tmp_path / "profile"
    profile.mkdir()
    session = await engine.launch(LaneSpec(id="qa/cookies"), profile_dir=profile,
                                  fingerprint_path=tmp_path / "fp.json", display=display, proxy=None)
    reads: list[int] = []
    seen: list[frozenset] = []
    try:
        pids = Host().browser_pids(profile)
        assert pids, "the browser process was not found"
        assert isinstance(_camou_prefs(Host().environ(pids[0]) or b""), dict)  # the running browser's launch prefs
        page = await session.page()
        await page.goto(f"http://127.0.0.1:{SITE_PORT}/")
        names_before = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and len(reads) < 60:
            found = await asyncio.to_thread(read_cookie_db, profile, ["127.0.0.1"])
            assert found is not None, f"read {len(reads)} failed while the browser wrote"
            reads.append(len(found))
            seen.append(frozenset((c["name"], c["value"]) for c in found))
            names_before = sorted(p.name for p in profile.iterdir() if p.name.startswith("cookies"))
            await asyncio.sleep(0.2)
        before = await page.evaluate("() => document.cookie")
        await asyncio.sleep(1.0)
        assert await page.evaluate("() => document.cookie") != before  # still writing after all those reads
    finally:
        await session.close()
        server.shutdown()
        server.server_close()
    print("cookies seen per read:", reads[:5], "...", reads[-5:], "files:", names_before)
    assert len(reads) >= 30 and reads[-1] > reads[0], reads
    assert len(set(seen[len(seen) // 2:])) > 3  # later reads keep seeing new values: the copy is fresh
    assert all(n.startswith("cookies.sqlite") for n in names_before or [])
    final = read_cookie_db(profile, ["127.0.0.1"])
    assert final is not None and any(c["name"] == "sessionid" for c in final)
    assert not list(tmp_path.glob("**/openajar-cookies-*"))


async def test_only_a_crash_reads_as_a_crash(tmp_path, display):
    """Firefox leaves its `lock` link behind on a clean exit as well, so the lane check cannot go
    by it: every lane ever closed would warn of a crash. What a kill leaves and a close does not
    is the session recovery file."""
    from openajar.core.system import Host
    from openajar.stealth.lanecheck import left_by_a_crash

    engine = CamoufoxEngine(extra_options={"geoip": False})
    engine.allow_private_network = True
    profile = tmp_path / "profile"
    profile.mkdir()
    recovery = profile / "sessionstore-backups" / "recovery.jsonlz4"

    async def launch_until_recovery_is_written():
        session = await engine.launch(LaneSpec(id="qa/crash"), profile_dir=profile,
                                      fingerprint_path=tmp_path / "fp.json", display=display, proxy=None)
        page = await session.page()
        await page.goto("data:text/html,<title>one</title>")
        await page.goto("data:text/html,<title>two</title>")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not recovery.exists():
            await asyncio.sleep(0.5)
        assert recovery.exists(), "the session was not saved while the browser ran"
        return session

    session = await launch_until_recovery_is_written()
    await session.close()
    assert not Host().browser_pids(profile)
    assert (profile / "lock").is_symlink()
    assert not recovery.exists()
    assert not left_by_a_crash(profile)

    session = await launch_until_recovery_is_written()
    try:
        for pid in Host().browser_pids(profile):
            os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and Host().browser_pids(profile):
            await asyncio.sleep(0.1)
        assert left_by_a_crash(profile)
    finally:
        await session.close()
