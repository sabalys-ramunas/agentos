"""Panel API: SSE live updates, the noVNC WebSocket relay (fake upstream) and the terminal."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from testkit.timing import wait_for

from openajar.api import ApiSettings, EventBus, PanelStore, build_api
from openajar.api.bridge import Bridge
from openajar.core.runtime import Runtime
from openajar.dev.apifakes import demo_runtime
from openajar.node import vnc_relay

H = {"Authorization": "Bearer owner"}


def make(rt, **settings):
    return TestClient(build_api(rt, ApiSettings(store=PanelStore(None), owner_token="owner", **settings)))


@pytest.fixture(autouse=True)
def tcp_upstream(monkeypatch):
    """The fake lanes and the `upstream` stand-in are TCP ports: only a test may let the relay dial one."""
    monkeypatch.setattr(vnc_relay, "TEST_TCP_UPSTREAM", True)


# ---- SSE ----

def read_events(lines, want: int) -> list[tuple[str, dict]]:
    events, kind, data = [], None, None
    for line in lines:
        if line.startswith("event: "):
            kind = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
        elif line == "" and kind:
            events.append((kind, data))
            kind = data = None
            if len(events) >= want:
                break
    return events


def test_sse_requires_auth():
    client = make(demo_runtime())
    assert client.get("/api/events").status_code == 401


def test_sse_replays_after_last_event_id_and_streams_live():
    rt = demo_runtime()
    client = make(rt, sse_max_seconds=5, sse_heartbeat=0.05)
    bus: EventBus = rt.events  # build_api registered one
    bus.publish("lane", {"id": "biz-a/instagram", "state": "warm"})
    bus.publish("approval", {"id": "ap_1", "state": "pending"})

    with client.stream("GET", "/api/events", headers={**H, "Last-Event-ID": "0"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = resp.iter_lines()
        got = read_events(lines, 2)
        assert got == [("lane", {"id": "biz-a/instagram", "state": "warm"}),
                       ("approval", {"id": "ap_1", "state": "pending"})]
        # published from another thread while the stream is open
        threading.Timer(0.05, lambda: bus.publish("run", {"id": "run_9", "status": "succeeded_verified"})).start()
        assert read_events(lines, 1) == [("run", {"id": "run_9", "status": "succeeded_verified"})]


def test_sse_resync_when_history_is_gone():
    bus = EventBus(history=2)
    rt = demo_runtime()
    rt.register("events", bus)
    client = make(rt, sse_max_seconds=2, sse_heartbeat=0.05)
    for i in range(5):
        bus.publish("trace", {"id": f"t{i}"})
    with client.stream("GET", "/api/events", headers={**H, "Last-Event-ID": "1"}) as resp:
        assert read_events(resp.iter_lines(), 1)[0][0] == "resync"


def test_sse_resync_frame_has_no_id_and_a_client_ahead_of_the_bus_resyncs():
    bus = EventBus(history=2)
    rt = demo_runtime()
    rt.register("events", bus)
    client = make(rt, sse_max_seconds=2, sse_heartbeat=0.05)
    for i in range(5):
        bus.publish("trace", {"id": f"t{i}"})
    with client.stream("GET", "/api/events", headers={**H, "Last-Event-ID": "1"}) as resp:
        raw = []
        for line in resp.iter_lines():
            raw.append(line)
            if line.startswith("data: "):
                break
    assert "event: resync" in raw and "id: 0" not in raw  # the client keeps its place
    # a Last-Event-ID from a bus that has since restarted (ahead of this one) is a gap too
    with client.stream("GET", "/api/events", headers={**H, "Last-Event-ID": "99"}) as resp:
        assert read_events(resp.iter_lines(), 1)[0][0] == "resync"


def test_sse_pings_when_the_wire_is_quiet():
    client = make(demo_runtime(), sse_max_seconds=2, sse_heartbeat=0.05)
    with client.stream("GET", "/api/events", headers=H) as resp:
        pings = [line for line in resp.iter_lines() if line == ": ping"]
    assert len(pings) >= 3


def test_api_writes_publish_events():
    rt = demo_runtime()
    client = make(rt)
    client.post("/api/lanes/biz-a/facebook/open", headers=H)
    events = rt.events.since(0)
    assert events and events[-1].kind == "lane" and events[-1].data["state"] == "warm"


def test_event_bus_rejects_bad_kind_and_overflow_resyncs():
    bus = EventBus(queue_size=2)
    with pytest.raises(ValueError):
        bus.publish("Bad Kind", {})

    async def scenario():
        async with bus.subscribe() as sub:
            for i in range(4):
                bus.publish("trace", {"i": i})
            first = await sub.get()
            second = await sub.get()
            third = await sub.get()
            return first.data, second.data, third.kind

    assert asyncio.run(scenario()) == ({"i": 0}, {"i": 1}, "resync")


# ---- noVNC relay ----

@pytest.fixture
def upstream():
    """A stand-in for the lane's x11vnc on 127.0.0.1: sends the RFB greeting, records what arrives."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    info: dict = {"seen": b""}

    async def handler(reader, writer):
        writer.write(b"RFB 003.008\n")
        await writer.drain()
        while data := await reader.read(4096):
            info["seen"] += data
        writer.close()  # the relay left: without this the server's close waits for it (5 s per test)

    async def main():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        info["port"] = server.sockets[0].getsockname()[1]
        info["server"] = server
        ready.set()
        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            pass  # closed by the fixture

    thread = threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True)
    thread.start()
    assert ready.wait(5)
    yield info
    loop.call_soon_threadsafe(info["server"].close)
    thread.join(5)


def test_vnc_relay_round_trip(upstream):
    """The panel's live view goes through the RFB relay to x11vnc: the greeting reaches the
    browser and the browser's handshake reaches x11vnc (input only while a human has control)."""
    rt = demo_runtime(viewer_port=upstream["port"])
    client = make(rt)
    with client.websocket_connect("/api/lanes/biz-a/instagram/vnc", subprotocols=["binary"], headers=H) as ws:
        assert ws.accepted_subprotocol == "binary"
        assert ws.receive_bytes() == b"RFB 003.008\n"
        ws.send_bytes(b"RFB 003.008\n")
        wait_for(lambda: upstream["seen"])
    assert upstream["seen"].startswith(b"RFB 003.008\n")


def test_vnc_relay_with_cookie_session(upstream):
    rt = demo_runtime(viewer_port=upstream["port"])
    store = PanelStore(None)
    client = TestClient(build_api(rt, ApiSettings(store=store, owner_token="owner")))
    store.add_code("ws-cookie-code")
    assert client.post("/api/session", json={"code": "ws-cookie-code"}).status_code == 200
    with client.websocket_connect("/api/lanes/biz-a/instagram/vnc", subprotocols=["binary"],
                                  headers={"Origin": "http://testserver"}) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"
    with pytest.raises(WebSocketDisconnect) as refused:
        client.websocket_connect("/api/lanes/biz-a/instagram/vnc", headers={"Origin": "https://evil.example"}).__enter__()
    assert refused.value.code == 4401


def test_vnc_relay_refuses_without_auth(upstream):
    client = make(demo_runtime(viewer_port=upstream["port"]))
    with pytest.raises(WebSocketDisconnect) as e:
        client.websocket_connect("/api/lanes/biz-a/instagram/vnc").__enter__()
    assert e.value.code == 4401
    assert upstream["seen"] == b""


def test_vnc_relay_cold_lane_and_unknown_lane():
    client = make(demo_runtime())
    with client.websocket_connect("/api/lanes/biz-a/facebook/vnc", headers=H) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_bytes()
        assert e.value.code == 4410  # "closed before it was ready" (the app's stream.ts), was 4409
    with client.websocket_connect("/api/lanes/biz-a/nope/vnc", headers=H) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_bytes()
        assert e.value.code == 4404


def test_vnc_relay_upstream_down():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    client = make(demo_runtime(viewer_port=free))
    with client.websocket_connect("/api/lanes/biz-a/instagram/vnc", headers=H) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_bytes()
        assert e.value.code == 4502


# ---- terminal ----

def test_terminal_round_trip_and_resize():
    rt = demo_runtime()
    client = make(rt)
    with client.websocket_connect("/api/lanes/personal/shell/terminal?cols=100&rows=30", headers=H) as ws:
        assert ws.receive_text() == "$ connected to personal/shell (100x30)\r\n"
        ws.send_text("ls\r")
        assert ws.receive_text() == "LS\r"
        ws.send_text('{"type":"resize","cols":120,"rows":40}')
        assert ws.receive_text() == "[120x40]"
        ws.send_text('{"type":"resize","cols":1,"rows":1,"extra":true}')  # not a control message
        assert ws.receive_text().startswith('{"TYPE"')
        ws.send_text("exit\r")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    term = rt.envs.sessions[0]
    assert term.resizes == [(120, 40)] and term.closed


def test_terminal_refuses_browser_lanes_and_missing_envs():
    rt = demo_runtime()
    client = make(rt)
    with client.websocket_connect("/api/lanes/biz-a/instagram/terminal", headers=H) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4404
    rt._services.pop("envs")
    with client.websocket_connect("/api/lanes/personal/shell/terminal", headers=H) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4503


# ---- brains: the feed the poll watches and the shapes the dashboard reads ----

class FakeBrainFeed:
    """A brains service in the shape the bridge adapts (list_brains, chip, tasks and their tails)."""

    def __init__(self):
        self.brains = [{"name": "claude-max", "kind": "subscription", "provider": "anthropic", "state": "ready",
                        "seats": {"in_use": 0, "total": 1}, "window_used_pct": 10}]
        self.tasks = {"bt_1": {"id": "bt_1", "workspace": "biz-a", "status": "running", "status_reason": None,
                               "brain": "claude-max", "attempt": 1, "tokens": 10, "turns": 1, "cost_usd": 0.0,
                               "prompt_preview": "Draft three captions", "created_at": "2026-11-04T09:00:00+00:00",
                               "ended_at": None, "resumable": False}}

    async def list_brains(self):
        return [dict(b) for b in self.brains]

    async def chip(self):
        return {"name": "claude-max", "kind": "subscription", "state": "ready", "offline_ready": True}

    def list_tasks(self, *, limit=50):
        return [dict(t) for t in self.tasks.values()][:limit]

    def status(self, task_id):
        return {**self.tasks[task_id], "attempts": []}

    def tail(self, task_id, *, limit=50):
        return {"events": [{"text": "Drafting captions"}]}


def test_brain_tasks_carry_the_dashboards_names_and_a_tail():
    client = make(Runtime(brains=FakeBrainFeed()))
    task = client.get("/api/brains/tasks", headers=H).json()[0]
    assert task["started_at"] == "2026-11-04T09:00:00+00:00" and task["finished_at"] is None
    assert task["summary"] == "Draft three captions" and task["lane"] is None and task["status"] == "running"
    detail = client.get("/api/brains/tasks/bt_1", headers=H).json()
    assert detail["tail"] == detail["transcript_tail"] == ["Drafting captions"]


def test_brain_and_task_changes_reach_the_bus_through_the_poll():
    feed = FakeBrainFeed()
    bus = EventBus()
    bridge = Bridge(Runtime(brains=feed), timezone="UTC")

    async def scenario() -> None:
        task = asyncio.create_task(bridge.pump_brains(bus, interval=0.01, brains_every=0))
        await asyncio.sleep(0.05)  # the first pass records what exists and announces nothing
        feed.tasks["bt_1"]["status"] = "succeeded"
        feed.brains[0]["state"] = "cooling_down"
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert [e.data["status"] for e in bus.since(0) if e.kind == "task"] == ["succeeded"]
    assert [e.data["state"] for e in bus.since(0) if e.kind == "brain"] == ["cooling_down"]
