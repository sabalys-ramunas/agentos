"""Viewer links, the viewer routes and the owner's control API, served by a real uvicorn on
127.0.0.1:0 with the fake engine and displays from test_lanes and a fake VNC server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import stat
import threading
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount
from test_lanes import make
from test_vnc_relay import HANDSHAKE_IN, HANDSHAKE_OUT, FakeVnc, fbur, key, pointer
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from agentos.core.errors import InvalidRequest
from agentos.core.models import ControlState, LaneSpec
from agentos.node import viewer_app, viewer_tokens, vnc_relay
from agentos.node.viewer_actions import apply_action
from agentos.node.viewer_app import build_viewer_app
from agentos.node.viewer_routes import control_api_routes
from agentos.node.viewer_tokens import TokenError, ViewMode, ViewTokens, load_or_create_key, make_view_link

LANE = "personal/reddit"
EM_DASH = "\u2014"


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def tokens_at(tmp_path: Path, clock: Clock | None = None, **kw) -> ViewTokens:
    return ViewTokens(tmp_path / "secrets" / "viewer.key", clock=clock or Clock(), **kw)


# ---- the key file --------------------------------------------------------------------------


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_key_is_created_private_and_reused(tmp_path):
    path = tmp_path / "secrets" / "viewer.key"
    key = load_or_create_key(path)
    assert len(key) == 32 and mode(path) == 0o600 and mode(path.parent) == 0o700
    assert load_or_create_key(path) == key
    assert not [p for p in path.parent.iterdir() if p.name != "viewer.key"]  # no temp files left


def test_key_permissions_are_fixed(tmp_path):
    path = tmp_path / "viewer.key"
    path.write_bytes(os.urandom(32))
    path.chmod(0o644)
    load_or_create_key(path)
    assert mode(path) == 0o600


def test_a_short_key_is_replaced(tmp_path):
    path = tmp_path / "viewer.key"
    path.write_bytes(b"short")
    key = load_or_create_key(path)
    assert len(key) == 32 and key != b"short"
    path.write_bytes(b"")
    assert len(load_or_create_key(path)) == 32


def test_concurrent_creation_agrees_on_one_key(tmp_path):
    path = tmp_path / "secrets" / "viewer.key"
    keys: list[bytes] = []
    barrier = threading.Barrier(8)

    def create():
        barrier.wait()
        keys.append(load_or_create_key(path))

    threads = [threading.Thread(target=create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(keys)) == 1


# ---- tokens --------------------------------------------------------------------------------


def test_issue_and_verify(tmp_path):
    clock = Clock()
    tokens = tokens_at(tmp_path, clock, default_ttl=900)
    link = tokens.issue(LANE, ViewMode.CONTROL)
    assert link.can_control and link.expires_at == clock.now + 900
    back = tokens.verify(link.token, LANE)
    assert back.mode == ViewMode.CONTROL and back.lane_id == LANE and back.nonce == link.nonce
    view = tokens.verify(tokens.issue(LANE, "view").token, LANE)
    assert view.mode == ViewMode.VIEW and not view.can_control
    assert tokens.issue(LANE).nonce != tokens.issue(LANE).nonce
    assert link.path().startswith("/view/personal/reddit?t=")


def test_ttl_is_clamped(tmp_path):
    clock = Clock()
    tokens = tokens_at(tmp_path, clock)
    assert tokens.issue(LANE, ttl=1).expires_at == clock.now + viewer_tokens.MIN_TTL
    # no ttl outlives the link's hard end: short for a link an agent may hold, longer for an owner link
    assert tokens.issue(LANE, ttl=10**9).expires_at == clock.now + viewer_tokens.AGENT_LINK_MAX
    assert tokens.issue(LANE, "control", ttl=10**9, owner=True).expires_at == clock.now + min(
        viewer_tokens.MAX_TTL, viewer_tokens.OWNER_LINK_MAX)


def test_expired_wrong_lane_and_missing(tmp_path):
    clock = Clock()
    tokens = tokens_at(tmp_path, clock, default_ttl=60)
    token = tokens.issue(LANE).token
    with pytest.raises(TokenError) as e:
        tokens.verify(token, "personal/other")
    assert e.value.code == "wrong_lane"
    clock.now += 59
    tokens.verify(token, LANE)
    clock.now += 1
    with pytest.raises(TokenError) as e:
        tokens.verify(token, LANE)
    assert e.value.code == "expired" and "browser_view" in e.value.message
    for missing in (None, ""):
        with pytest.raises(TokenError) as e:
            tokens.verify(missing, LANE)
        assert e.value.code == "missing"


def test_tampered_tokens_are_invalid(tmp_path):
    tokens = tokens_at(tmp_path)
    token = tokens.issue(LANE, ViewMode.VIEW).token
    body, sig = token.split(".")
    payload = viewer_tokens._unb64(body)
    forged_control = viewer_tokens._b64(payload.replace(b"|view|", b"|control|")) + "." + sig
    forged_lane = viewer_tokens._b64(payload.replace(b"personal/reddit", b"personal/bank")) + "." + sig
    flipped_sig = body + "." + ("A" if sig[0] != "A" else "B") + sig[1:]
    for bad in (forged_control, forged_lane, flipped_sig, token + "x", "x" + token, token.replace(".", ""),
                token + ".x", "a.b", "!!!.???", "a" * 600, ".", f"{body}."):
        with pytest.raises(TokenError) as e:
            tokens.verify(bad, LANE if "bank" not in bad else "personal/bank")
        assert e.value.code == "invalid", bad


def test_a_token_from_another_key_is_invalid(tmp_path):
    ours = tokens_at(tmp_path / "a")
    theirs = tokens_at(tmp_path / "b")
    with pytest.raises(TokenError) as e:
        ours.verify(theirs.issue(LANE).token, LANE)
    assert e.value.code == "invalid"


def test_a_signed_but_malformed_payload_is_invalid(tmp_path):
    tokens = tokens_at(tmp_path)
    for payload in (b"personal/reddit|admin|9999999999|n", b"personal/reddit|view|soon|n", b"personal/reddit|view",
                    b"personal/reddit|view|1|n|extra", "\u00ff".encode("latin-1")):
        token = viewer_tokens._b64(payload) + "." + viewer_tokens._b64(tokens._mac(payload))
        with pytest.raises(TokenError) as e:
            tokens.verify(token, LANE)
        assert e.value.code == "invalid"


def test_user_messages_have_no_em_dash(tmp_path):
    clock = Clock()
    tokens = tokens_at(tmp_path, clock, default_ttl=60)
    token = tokens.issue(LANE).token
    messages = []
    for bad, lane in ((None, LANE), ("x.y", LANE), (token, "a/b")):
        with pytest.raises(TokenError) as e:
            tokens.verify(bad, lane)
        messages.append(e.value.message)
    clock.now += 100
    with pytest.raises(TokenError) as e:
        tokens.verify(token, LANE)
    messages.append(e.value.message)
    assert not any(EM_DASH in m for m in messages)


def test_make_view_link(tmp_path):
    tokens = tokens_at(tmp_path)
    link = make_view_link(tokens, LANE, "control", 120)
    assert link["mode"] == "control" and link["url"].startswith("/view/personal/reddit?t=")
    token = link["url"].split("?t=", 1)[1]
    assert tokens.verify(token, LANE).can_control
    assert link["expires_at"].endswith("Z")


# ---- a real server ----------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def serve(app):
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off",
                            ws="auto")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5.0)


class FakeTraces:
    def __init__(self, events=None, *, live: bool = True):
        self.events = list(events or [])
        self.subscribers = []
        self.written = []  # what the viewer wrote to the action log (TraceEvent)
        if not live:
            self.subscribe = None

    def query(self, *, lane=None, limit=100):
        found = [e for e in reversed(self.events) if e.get("lane") == lane][:limit]
        return found, None

    def subscribe(self, fn):
        self.subscribers.append(fn)
        return lambda: self.subscribers.remove(fn)

    def write(self, event):
        self.written.append(event)
        return event

    def publish(self, event):
        self.events.append(event)
        for fn in list(self.subscribers):
            fn("trace", event)


@pytest.fixture(autouse=True)
def tcp_upstream(monkeypatch):
    """The lanes here hand out the TCP port of a FakeVnc: only a test may let the relay dial one."""
    monkeypatch.setattr(vnc_relay, "TEST_TCP_UPSTREAM", True)


@pytest.fixture
async def node(tmp_path, monkeypatch):
    """A warm lane whose 'x11vnc' is a FakeVnc, behind the viewer app on a real server."""
    monkeypatch.setattr(viewer_app, "EVENT_POLL_SECONDS", 0.05)
    async with FakeVnc() as vnc:
        mgr, *_ = make(tmp_path, vnc_base=vnc.port, control_idle_seconds=600)
        await mgr.create(LaneSpec(id=LANE))
        await mgr.create(LaneSpec(id="personal/cold"))
        await mgr.open(LANE)
        _poke_on_view_changes(mgr, vnc)
        traces = FakeTraces([{"id": "tr_1", "at": 1_800_000_000.0, "tool": "browser_navigate", "lane": LANE,
                              "status": "ok", "summary": "opened reddit.com"}])
        app = build_viewer_app(mgr, mgr.config, traces=traces)
        async with serve(app) as host:
            yield Node(mgr, vnc, app, host, traces)
        await mgr.shutdown()


def _poke_on_view_changes(mgr, vnc: FakeVnc) -> None:
    """Wake vnc.until() when the relay counts a view in or out, so tests wait on the lane's
    counters and the relay's own end by event. The relay calls these last in its `finally`."""
    def poking(real):
        def hook(lane_id):
            real(lane_id)
            vnc.poke()
        return hook

    for name in ("view_opened", "view_closed", "viewer_attached", "viewer_detached"):
        setattr(mgr, name, poking(getattr(mgr, name)))


class Node:
    def __init__(self, mgr, vnc, app, host, traces):
        self.mgr, self.vnc, self.app, self.host, self.traces = mgr, vnc, app, host, traces
        self.tokens: ViewTokens = app.state.viewer.tokens

    def token(self, mode="control", lane=LANE, ttl=None) -> str:
        return self.tokens.issue(lane, mode, ttl).token

    def url(self, path: str, token: str | None) -> str:
        return f"http://{self.host}{path}" + (f"?t={token}" if token is not None else "")

    def ws(self, token: str, lane: str = LANE) -> str:
        return f"ws://{self.host}/viewer-ws/{lane}?t={token}"

    async def relays_ended(self, lane: str = LANE) -> None:
        """The server finished every relay of the lane. A client's close returns once the close
        handshake is done, which is before the server's relay runs its `finally` (the slot count)."""
        relays = self.app.state.viewer._relays
        await self.vnc.until(lambda: lane not in relays)


def boot_of(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="agentos-boot">(.*?)</script>', html, re.DOTALL)
    assert m, "no boot data in the page"
    return json.loads(m.group(1))


async def test_page_serves_with_boot_data_and_strict_headers(node):
    async with httpx.AsyncClient() as client:
        r = await client.get(node.url("/view/personal/reddit", node.token("view")))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "{{" not in r.text and "personal/reddit · agentos" in r.text
    boot = boot_of(r.text)
    assert boot["mode"] == "view" and boot["lane"]["id"] == LANE and boot["lane"]["live"] is True
    assert boot["lane"]["control"]["state"] == "agent" and boot["traces"] is True
    assert boot["paths"]["ws"] == "/viewer-ws/personal/reddit"
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and f"ws://{node.host}" in csp
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert EM_DASH not in r.text


async def test_the_page_always_has_a_place_for_the_exit_address(node):
    """The live view says where websites see the lane coming from, proxy or not: the boot data carries
    the proxy label and the last exit seen, and the page has the chip that shows them."""
    async with httpx.AsyncClient() as client:
        r = await client.get(node.url("/view/personal/reddit", node.token("view")))
    lane = boot_of(r.text)["lane"]
    assert "exit_ip" in lane and "proxy" in lane
    assert 'id="exit-chip"' in r.text and "hidden" not in r.text.split('id="exit-chip"')[1].split(">")[0]


async def test_pasting_types_into_the_lane_and_the_login_button_sits_in_the_bar(node):
    """Pasting sends key events (the VNC clipboard carries nothing to the lane's browser), so the key
    table it uses is served; the "Log in" button goes in the bar, where it cannot cover "Take control"."""
    async with httpx.AsyncClient() as client:
        js = (await client.get(f"http://{node.host}/viewer-assets/viewer.js")).text
        login_js = (await client.get(f"http://{node.host}/viewer-assets/login.js")).text
        login_css = (await client.get(f"http://{node.host}/viewer-assets/login.css")).text
        keys = await client.get(f"http://{node.host}/viewer-assets/vendor/novnc/core/input/keysymdef.js")
    assert "import keysyms from './vendor/novnc/core/input/keysymdef.js'" in js and keys.status_code == 200
    assert "clipboardPasteFrom" not in js and "addEventListener('paste'" in js
    assert "getElementById('actions')" in login_js
    launch = login_css.split(".la-launch {")[1].split("}")[0]
    assert "position: fixed" not in launch
    assert EM_DASH not in js and EM_DASH not in login_js


async def test_copying_in_the_lane_brings_it_to_this_computer_only_on_a_key_or_a_click(node):
    """Copying out reads the lane's clipboard through its own route (the VNC clipboard carries nothing): a
    moment after the person's own copy key, or on a click of "Copy from the lane"; never on a timer, and the
    write starts inside the handler (the promise form Safari asks for), with the plain form as a fallback."""
    async with httpx.AsyncClient() as client:
        r = await client.get(node.url("/view/personal/reddit", node.token("control")))
        js = (await client.get(f"http://{node.host}/viewer-assets/viewer.js")).text
    assert boot_of(r.text)["paths"]["clipboard"] == "/view/personal/reddit/clipboard"
    assert 'id="btn-copy-out"' in r.text and ">Copy from the lane<" in r.text
    assert "new ClipboardItem({ 'text/plain': text.then(" in js and "navigator.clipboard.writeText(got)" in js
    assert "copyFromLane(COPY_SETTLE_MS)" in js and "e.isTrusted" in js
    assert "setInterval(copyFromLane" not in js and js.count("copyFromLane(") == 3  # the key, the click, itself
    assert "'Copied to this computer.'" in js and "'Nothing is copied in the lane.'" in js
    assert "cannot be pasted on this computer yet" not in js and "Copy from the lane" in js
    assert "clipboardPasteFrom" not in js and EM_DASH not in js and EM_DASH not in r.text


async def test_page_rejects_bad_links_with_a_friendly_page(node):
    other = node.token("view", lane="personal/cold")
    expired = node.tokens.issue(LANE, "view", 30)
    node.tokens.clock = lambda: expired.expires_at + 1
    async with httpx.AsyncClient() as client:
        cases = {
            None: "Link not valid",
            "": "Link not valid",
            "nonsense": "Link not valid",
            other: "Wrong lane",
            expired.token: "Link expired",
        }
        for token, title in cases.items():
            r = await client.get(node.url("/view/personal/reddit", token))
            assert r.status_code == 403, token
            assert f"<h1>{title}</h1>" in r.text
            assert EM_DASH not in r.text
            assert r.headers["cache-control"] == "no-store"
        r = await client.get(node.url("/view/Bad_Lane/x", "x"))
        assert r.status_code == 403
    node.tokens.clock = __import__("time").time


async def test_page_for_a_lane_that_is_gone(node):
    token = node.token("view", lane="personal/gone")
    async with httpx.AsyncClient() as client:
        r = await client.get(node.url("/view/personal/gone", token))
    assert r.status_code == 404 and "No such lane" in r.text


async def test_assets_are_served_and_contained(node):
    async with httpx.AsyncClient() as client:
        js = await client.get(f"http://{node.host}/viewer-assets/viewer.js")
        css = await client.get(f"http://{node.host}/viewer-assets/viewer.css")
        rfb = await client.get(f"http://{node.host}/viewer-assets/vendor/novnc/core/rfb.js")
        license_ = await client.get(f"http://{node.host}/viewer-assets/vendor/novnc/LICENSE.txt")
        escape = await client.get(f"http://{node.host}/viewer-assets/..%2Fviewer_app.py")
        template = await client.get(f"http://{node.host}/viewer-assets/../page.html")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "import RFB from './vendor/novnc/core/rfb.js'" in js.text
    assert css.status_code == 200 and rfb.status_code == 200 and license_.status_code == 200
    assert "MPL" in license_.text
    assert escape.status_code == 404 and template.status_code == 404


async def test_control_actions_need_a_control_link(node):
    events: list = []
    node.mgr.add_control_listener(lambda lane, old, new: events.append(new.state))
    async with httpx.AsyncClient(headers={"origin": f"http://{node.host}"}) as client:  # as the viewer page sends
        path = "/view/personal/reddit/control"
        r = await client.post(node.url(path, node.token("view")), json={"action": "take"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "denied"
        assert "only watch" in r.json()["error"]["message"]
        r = await client.post(node.url(path, "garbage"), json={"action": "take"})
        assert r.status_code == 403

        token = node.token("control")
        r = await client.post(node.url(path, token), json={"action": "take"})
        assert r.status_code == 200, r.text
        assert r.json()["lane"]["control"]["state"] == "human" and r.json()["changed"] is True
        assert r.json()["lane"]["control"]["by"] == "owner"
        r = await client.post(node.url(path, token), json={"action": "take"})
        assert r.json()["changed"] is False

        await node.mgr.request_control(LANE, by="claude-code", reason="done logging in")
        r = await client.post(node.url(path, token), json={"action": "deny_request"})
        assert r.json()["changed"] is True and not r.json()["lane"]["control"]["takeback_requested"]
        r = await client.post(node.url(path, token), json={"action": "stop_agent"})
        assert r.json()["lane"]["control"]["state"] == "paused"
        r = await client.post(node.url(path, token), json={"action": "hand_back"})  # a person's (test_viewer_person)
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
        owner = node.tokens.issue(LANE, "control", owner=True).token
        r = await client.post(node.url(path, owner), json={"action": "hand_back"})
        assert r.json()["lane"]["control"]["state"] == "agent"

        for body in ({"action": "explode"}, {}, [], {"action": None}):
            r = await client.post(node.url(path, token), json=body)
            assert r.status_code == 400 and r.json()["error"]["code"] == "invalid"
        r = await client.post(node.url(path, token), content=b"not json", headers={"content-type": "application/json"})
        assert r.status_code == 400
        r = await client.post(node.url(path, token), json={"action": "take"},
                              headers={"origin": "https://evil.example"})
        assert r.status_code == 403 and "cross-site" in r.json()["error"]["message"]
        r = await client.post(node.url(path, token), json={"action": "take"},
                              headers={"origin": f"http://{node.host}"})
        assert r.status_code == 200
    async with httpx.AsyncClient() as bare:  # a link alone, from a script: no Origin, refused
        r = await bare.post(node.url(path, token), json={"action": "hand_back"})
        assert r.status_code == 403 and "cross-site" in r.json()["error"]["message"]
    assert events == [ControlState.HUMAN, ControlState.HUMAN, ControlState.HUMAN, ControlState.PAUSED,
                      ControlState.AGENT, ControlState.HUMAN]


async def test_renew_gives_a_fresh_token_of_the_same_mode(node):
    async with httpx.AsyncClient(headers={"origin": f"http://{node.host}"}) as client:
        for mode in ("view", "control"):
            r = await client.post(node.url("/view/personal/reddit/renew", node.token(mode)))
            assert r.status_code == 200
            fresh = node.tokens.verify(r.json()["token"], LANE)
            assert fresh.mode.value == mode
        r = await client.post(node.url("/view/personal/reddit/renew", "bad"))
        assert r.status_code == 403
    async with httpx.AsyncClient() as bare:
        r = await bare.post(node.url("/view/personal/reddit/renew", node.token("control")))
        assert r.status_code == 403


class EventReader:
    """Server-sent events from one streaming httpx response."""

    def __init__(self, response):
        self._chunks = response.aiter_text()
        self._buffer = ""
        self.events: list[tuple[str, dict]] = []

    async def until(self, predicate, limit: float = 10.0) -> list[tuple[str, dict]]:
        async def pump():
            while True:
                while "\n\n" in self._buffer:
                    block, self._buffer = self._buffer.split("\n\n", 1)
                    name = data = None
                    for line in block.splitlines():
                        if line.startswith("event: "):
                            name = line[7:]
                        elif line.startswith("data: "):
                            data = json.loads(line[6:])
                    if name:
                        self.events.append((name, data))
                        if predicate(self.events):
                            return
                self._buffer += await anext(self._chunks)

        await asyncio.wait_for(pump(), limit)
        return self.events


async def test_events_stream_control_lane_and_actions(node):
    async with httpx.AsyncClient(timeout=5) as client:
        url = node.url("/view/personal/reddit/events", node.token("view"))
        async with client.stream("GET", url) as r:
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
            reader = EventReader(r)
            events = await reader.until(lambda ev: ev[-1][0] == "hello")
            hello = events[-1][1]
            assert hello["lane"]["id"] == LANE and hello["mode"] == "view"
            assert [a["summary"] for a in hello["actions"]] == ["opened reddit.com"]
            assert hello["actions"][0]["at"].endswith("Z")

            await node.mgr.take_control(LANE, by="owner")
            events = await reader.until(lambda ev: ev[-1][0] == "control")
            assert events[-1][1]["control"]["state"] == "human"

            node.traces.publish({"id": "tr_2", "at": 1_800_000_100.0, "tool": "browser_act", "lane": LANE,
                                 "status": "error", "summary": "click e12"})
            node.traces.publish({"id": "tr_x", "tool": "browser_act", "lane": "personal/cold"})  # other lane
            events = await reader.until(lambda ev: ev[-1][0] == "action")
            assert events[-1][1]["id"] == "tr_2" and events[-1][1]["status"] == "error"

            await node.mgr.close(LANE, principal_kind="human")
            events = await reader.until(lambda ev: ev[-1][0] == "lane")
            assert events[-1][1]["state"] == "cold" and events[-1][1]["live"] is False
            assert not any(e[1].get("id") == "tr_x" for e in events if e[0] == "action")
    await asyncio.sleep(0.05)
    assert node.traces.subscribers == []  # the stream unsubscribed when the client left


async def test_events_poll_traces_without_subscribe(tmp_path, monkeypatch):
    monkeypatch.setattr(viewer_app, "EVENT_POLL_SECONDS", 0.05)
    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id=LANE))
    traces = FakeTraces(live=False)
    app = build_viewer_app(mgr, mgr.config, traces=traces)
    token = app.state.viewer.tokens.issue(LANE).token
    url = "/view/personal/reddit/events?t=" + token
    async with serve(app) as host, httpx.AsyncClient(timeout=5) as client, client.stream("GET", f"http://{host}{url}") as r:
        reader = EventReader(r)
        events = await reader.until(lambda ev: ev[-1][0] == "hello")
        assert events[-1][1]["actions"] == []
        traces.events.append({"id": "tr_9", "tool": "browser_snapshot", "lane": LANE, "status": "ok"})
        events = await reader.until(lambda ev: ev[-1][0] == "actions")
        assert [a["id"] for a in events[-1][1]["actions"]] == ["tr_9"]


async def test_events_without_traces_and_for_a_removed_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(viewer_app, "EVENT_POLL_SECONDS", 0.05)
    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id=LANE))
    app = build_viewer_app(mgr, mgr.config)
    token = app.state.viewer.tokens.issue(LANE).token
    async with serve(app) as host, httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"http://{host}/view/personal/reddit/events?t=bad")
        assert r.status_code == 403
        async with client.stream("GET", f"http://{host}/view/personal/reddit/events?t={token}") as r:
            reader = EventReader(r)
            events = await reader.until(lambda ev: ev[-1][0] == "hello")
            assert events[-1][1]["actions"] is None
            mgr.store.delete(LANE)
            await reader.until(lambda ev: ev[-1][0] == "gone")


# ---- the live view through the relay --------------------------------------------------------


async def handshake(ws) -> None:
    assert await ws.recv() == b"RFB 003.008\n"
    await ws.send(HANDSHAKE_IN)


async def test_relay_enforces_control_server_side(node):
    vnc, mgr = node.vnc, node.mgr
    base = len(HANDSHAKE_OUT)
    owner = node.tokens.issue(LANE, "control", owner=True).token  # only a person's input passes
    async with connect(node.ws(owner), subprotocols=["binary"],
                       origin=f"http://{node.host}") as ws:
        assert ws.subprotocol == "binary"
        await handshake(ws)
        await vnc.until(lambda: bytes(vnc.received) == HANDSHAKE_OUT)
        await ws.send(pointer(0, 100, 100) + key(1, 0x61) + fbur())  # agents have control: input dropped
        await vnc.until(lambda: len(vnc.received) == base + 10)
        assert bytes(vnc.received)[base:] == fbur()
        assert mgr.get(LANE).control.last_human_input is None

        await mgr.take_control(LANE, by="owner")
        await ws.send(pointer(1, 200, 150))
        await vnc.until(lambda: bytes(vnc.received).endswith(pointer(1, 200, 150)))
        assert mgr.get(LANE).control.last_human_input is not None

        await mgr.hand_back(LANE, by="owner")  # the button is still down: the relay lets go
        await vnc.until(lambda: bytes(vnc.received).endswith(pointer(0, 200, 150)))
        await ws.send(pointer(1, 5, 5))
        await ws.send(fbur())
        await vnc.until(lambda: bytes(vnc.received).endswith(fbur()))
        assert pointer(1, 5, 5) not in bytes(vnc.received)


async def test_a_view_link_never_sends_input(node):
    vnc, mgr = node.vnc, node.mgr
    await mgr.take_control(LANE, by="owner")
    async with connect(node.ws(node.token("view")), subprotocols=["binary"]) as ws:
        await handshake(ws)
        await ws.send(pointer(1, 300, 300) + key(1, 0x62) + fbur())
        await vnc.until(lambda: bytes(vnc.received).endswith(fbur()))
        assert bytes(vnc.received) == HANDSHAKE_OUT + fbur()
    assert mgr.get(LANE).control.last_human_input is None


async def test_every_live_view_counts_as_watched_but_only_control_links_keep_the_lane(node):
    lane = node.mgr._lanes[LANE]
    async with connect(node.ws(node.token("view"))) as view:
        assert await view.recv() == b"RFB 003.008\n"
        assert (lane.watchers, lane.viewers) == (1, 0)  # no exit IP probe window, but may be reaped
        async with connect(node.ws(node.token("control"))) as control:
            assert await control.recv() == b"RFB 003.008\n"
            assert (lane.watchers, lane.viewers) == (2, 1)
        await node.vnc.until(lambda: lane.watchers == 1)
    await node.vnc.until(lambda: lane.watchers == 0)
    assert lane.viewers == 0


async def test_relay_refuses_bad_links_and_cross_site_origins(node):
    for url, origin in ((node.ws("garbage"), None), (node.ws(node.token(lane="personal/cold")), None),
                        (node.ws(node.token()), "https://evil.example"), (node.ws(node.token()), "null"),
                        (node.ws(node.token(lane="personal/gone"), lane="personal/gone"), None)):
        with pytest.raises(InvalidStatus) as e:
            async with connect(url, origin=origin):
                pass
        assert e.value.response.status_code == 403
    assert node.vnc.connections == 0


async def test_relay_for_a_cold_lane_closes_with_a_reason(node):
    async with connect(node.ws(node.token(lane="personal/cold"), lane="personal/cold")) as ws:
        with pytest.raises(ConnectionClosed) as e:
            await ws.recv()
    assert e.value.rcvd.code == 4404 and "not running" in e.value.rcvd.reason


async def test_relay_limits_viewers_per_lane(node, monkeypatch):
    monkeypatch.setattr(viewer_app, "MAX_RELAYS_PER_LANE", 1)
    async with connect(node.ws(node.token())) as first:
        assert await first.recv() == b"RFB 003.008\n"
        with pytest.raises((InvalidStatus, ConnectionClosed)):
            async with connect(node.ws(node.token())) as second:
                await second.recv()
    await node.relays_ended()
    async with connect(node.ws(node.token())) as again:  # the slot is free again
        assert await again.recv() == b"RFB 003.008\n"


# ---- the owner's API --------------------------------------------------------------------------


async def test_control_api_routes(tmp_path):
    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id=LANE))
    tokens = ViewTokens.for_config(mgr.config)
    app = Starlette(routes=[Mount("/api", routes=control_api_routes(mgr, tokens))])
    async with serve(app) as host, httpx.AsyncClient() as client:
        api = f"http://{host}/api/lanes/personal/reddit"
        r = await client.post(f"{api}/control", json={"action": "take"})
        assert r.status_code == 200 and r.json()["control"]["state"] == "human" and r.json()["changed"]
        assert r.json()["id"] == LANE and r.json()["workspace"] == "personal" and r.json()["state"] == "cold"
        r = await client.post(f"{api}/control", json={"action": "hand_back"})  # the node token alone
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
        from agentos.api.auth import PRESENCE_HEADER, PanelStore

        code = "c0de-" + "y" * 20
        PanelStore.for_home(mgr.config.home).add_code(code, 60)  # the node's CLI, a person
        r = await client.post(f"{api}/control", json={"action": "hand_back"}, headers={PRESENCE_HEADER: code})
        assert r.json()["control"]["state"] == "agent"
        r = await client.post(f"{api}/control", json={"action": "fly"})
        assert r.status_code == 400
        r = await client.post(f"{api}/control", content=b"[")
        assert r.status_code == 400
        r = await client.post(f"http://{host}/api/lanes/personal/nope/control", json={"action": "take"})
        assert r.status_code == 404 and r.json()["error"]["code"] == "lane_not_found"

        r = await client.post(f"{api}/view-link", json={"mode": "control", "ttl_s": 120})
        assert r.status_code == 200 and r.json()["mode"] == "control"
        token = r.json()["url"].split("?t=", 1)[1]
        assert tokens.verify(token, LANE).can_control
        r = await client.post(f"{api}/view-link")
        assert r.status_code == 200 and r.json()["mode"] == "view"
        for body in ({"mode": "admin"}, {"ttl_s": -1}, {"ttl_s": "soon"}, {"ttl_s": True}, ["x"]):
            r = await client.post(f"{api}/view-link", json=body)
            assert r.status_code == 400, body
        r = await client.post(f"http://{host}/api/lanes/personal/nope/view-link", json={})
        assert r.status_code == 404


async def test_a_hand_back_keeps_a_challenge_hold_until_the_person_says_the_check_is_done(tmp_path):
    """A robot check or a sign-in holds the lane for a person. "Not done yet" hands the lane back but keeps
    the hold; "Done, hand back" hands back as always, and the lane manager's own re-read of the page (when
    it can read one) decides whether the check is really gone. A hand back with no answer is the same."""
    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id=LANE))
    await mgr.report_signal(LANE, "captcha")
    await mgr.hold(LANE, reason="needs_human:captcha")
    await mgr.take_control(LANE, by="owner")  # a person takes over to do the check
    info, changed = await apply_action(mgr, LANE, "hand_back", by="owner", done=False)
    assert info.control.state == ControlState.PAUSED and info.control.reason == "needs_human:captcha", info.control
    assert changed and info.signal.value == "captcha"
    info, _ = await apply_action(mgr, LANE, "take", by="owner")
    assert info.control.state == ControlState.HUMAN
    info, _ = await apply_action(mgr, LANE, "hand_back", by="owner", done=True)
    assert info.control.state == ControlState.AGENT and not info.control.reason, info.control
    # with no answer, a hand back clears the hold as before (the panel and its tools send no answer)
    await mgr.hold(LANE, reason="needs_human:captcha")
    info, _ = await apply_action(mgr, LANE, "hand_back", by="owner")
    assert info.control.state == ControlState.AGENT and not info.control.reason
    with pytest.raises(InvalidRequest):
        await apply_action(mgr, LANE, "stop_agent", by="owner", done=True)


async def test_viewer_app_from_runtime(tmp_path):
    from agentos.core.runtime import Runtime

    mgr, *_ = make(tmp_path)
    app = viewer_app.viewer_app_from_runtime(Runtime(lanes=mgr, config=mgr.config))
    assert app.state.viewer.traces is None
    assert app.state.viewer.make_view_link(LANE, "view")["url"].startswith("/view/personal/reddit?t=")
