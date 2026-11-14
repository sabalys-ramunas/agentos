"""Live streams end when access does (agentos.core.live): the panel's event stream, viewer and
terminal close on a revoked grant, a freeze, a sign-out or a removed member, at once or on the
next heartbeat."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agentos.api import ApiSettings, PanelStore, build_api
from agentos.core import live
from agentos.core.errors import PolicyDenied
from agentos.core.models import Principal
from agentos.dev.apifakes import demo_runtime
from agentos.node import vnc_relay

H = {"Authorization": "Bearer owner"}
AGENT = {"Authorization": "Bearer agent-tok"}


@pytest.fixture(autouse=True)
def tcp_upstream(monkeypatch):
    monkeypatch.setattr(vnc_relay, "TEST_TCP_UPSTREAM", True)


class Ctx:
    def __init__(self, grant_id: str, lanes: set[str]):
        self.grant = SimpleNamespace(id=grant_id)
        self.admin = False
        self.lanes = lanes
        self.tool = None

    def can_access_lane(self, lane_id):
        return self.admin or lane_id in self.lanes

    async def authorize(self, effect, **kw):
        return None

    def precheck(self, effect, **kw):
        return None

    def trace(self, *a, **kw):
        return None


class FakePolicy:
    """Tokens resolve to grants; revoking one makes the next authentication fail."""

    def __init__(self):
        self.tokens = {"agent-tok": ("g_agent", {"personal/shell", "biz-a/instagram"})}

    def authenticate_panel(self, token):
        if token in self.tokens:
            return Principal(kind="agent", id="a1", owner="owner", grant_id=self.tokens[token][0])
        return None

    def context_for_token(self, token, tool=None):
        if token == "owner":  # the node token (make()'s owner_token): a device session acts through it
            return self.owner_context(tool)
        if token not in self.tokens:
            raise PolicyDenied("revoked")
        gid, lanes = self.tokens[token]
        return Ctx(gid, lanes)

    def owner_context(self, tool=None):
        ctx = Ctx("owner", set())
        ctx.admin = True
        return ctx


class FakeFreeze:
    def __init__(self):
        self.lanes: set[str] = set()
        self.node = False

    def frozen_for(self, lane_id):
        if self.node or (lane_id is not None and lane_id in self.lanes):
            return SimpleNamespace(scope="lane", target=lane_id, reason="test")
        return None


def make(rt=None, **settings):
    rt = rt or demo_runtime()
    store = PanelStore(None)
    settings.setdefault("live_recheck_seconds", 30.0)  # the event, not the heartbeat, closes it
    client = TestClient(build_api(rt, ApiSettings(store=store, owner_token="owner", **settings)))
    return rt, store, client


def later(fn, delay=0.2):
    threading.Timer(delay, fn).start()


def expect_close(ws, code):
    with pytest.raises(WebSocketDisconnect) as e:
        for _ in range(50):
            ws.receive_text()
    assert e.value.code == code
    return e.value


# ---- the registry ----

def test_close_where_matches_any_key_and_on_event_closes_revoked_grants():
    async def scenario():
        reg = live.LiveSockets()

        async def ok():
            return None

        a = reg.add(live.LiveEntry("terminal", ok, lane="biz-a/x", grant_id="g1"))
        b = reg.add(live.LiveEntry("vnc", ok, lane="biz-b/y", sid="s1", member_id="m1"))
        c = reg.add(live.LiveEntry("events", ok, grant_id="g2"))
        assert reg.close_where(code=4403, reason="r", workspace="biz-a") == 1 and a.closed.is_set()
        reg.on_event("grant", {"id": "gX", "state": "revoked", "cascades_to": ["g2"]})
        assert c.closed.is_set() and c.code == live.CLOSE_REVOKED
        reg.on_event("member", {"id": "m1"})  # a change, not a removal: rechecked, not closed outright
        assert not b.closed.is_set()
        reg.on_event("member", {"id": "m1", "removed": True})
        assert b.closed.is_set()
        reg._task.cancel()

    asyncio.run(scenario())


def test_sweep_closes_on_a_verdict_and_after_repeated_failures():
    async def scenario():
        reg = live.LiveSockets()
        verdict = {"v": None}

        async def check():
            return verdict["v"]

        async def broken():
            raise RuntimeError("database is locked")

        a = reg.add(live.LiveEntry("terminal", check))
        b = reg.add(live.LiveEntry("vnc", broken))
        assert await reg.sweep() == 0
        verdict["v"] = (4403, "frozen")
        assert await reg.sweep() == 1 and a.reason == "frozen"
        assert not b.closed.is_set()  # one blip keeps it open; it closes on the third in a row
        await reg.sweep()
        assert b.closed.is_set() and b.code == live.CLOSE_DENIED
        reg._task.cancel()

    asyncio.run(scenario())


def test_the_heartbeat_closes_without_any_event():
    async def scenario():
        reg = live.LiveSockets(interval=0.05)
        gone = {"v": None}

        async def check():
            return gone["v"]

        e = reg.add(live.LiveEntry("events", check))
        await asyncio.sleep(0.1)
        assert not e.closed.is_set()
        gone["v"] = (4401, "revoked")
        await asyncio.wait_for(e.wait_closed(), 1)
        reg.remove(e)
        await asyncio.wait_for(reg._task, 1)  # stops once nothing is open

    asyncio.run(scenario())


# ---- the terminal ----

def test_terminal_closes_when_its_grant_is_revoked():
    rt = demo_runtime()
    policy = FakePolicy()
    rt.register("policy", policy)
    _, _, client = make(rt)
    with client.websocket_connect("/api/lanes/personal/shell/terminal", headers=AGENT) as ws:
        assert ws.receive_text().startswith("$ connected to personal/shell")

        def revoke():
            policy.tokens.clear()
            rt.events.publish("grant", {"id": "g_agent", "state": "revoked", "cascades_to": []})

        later(revoke)
        err = expect_close(ws, live.CLOSE_REVOKED)
        assert "revoked" in err.reason
    assert rt.envs.sessions[0].closed


def test_terminal_closes_for_an_agent_on_a_lane_freeze_but_the_owner_keeps_theirs():
    rt = demo_runtime()
    rt.register("policy", FakePolicy())
    freeze = FakeFreeze()
    rt.register("freeze", freeze)
    _, store, client = make(rt)
    store.add_code("code-one-1")
    assert client.post("/api/session", json={"code": "code-one-1"}).status_code == 200
    origin = {"Origin": "http://testserver"}
    with client.websocket_connect("/api/lanes/personal/shell/terminal", headers=origin) as mine, \
            client.websocket_connect("/api/lanes/personal/shell/terminal", headers=AGENT) as agent:
        mine.receive_text()
        agent.receive_text()

        def freeze_it():
            freeze.lanes.add("personal/shell")
            rt.get("live_sockets").poke()  # what FreezeService.mark does

        later(freeze_it)
        err = expect_close(agent, live.CLOSE_DENIED)
        assert "frozen" in err.reason
        mine.send_text("ls\r")
        assert mine.receive_text() == "LS\r"  # the owner in person still has their shell


def test_terminal_closes_on_the_heartbeat_when_the_lane_leaves_the_grant():
    rt = demo_runtime()
    policy = FakePolicy()
    rt.register("policy", policy)
    _, _, client = make(rt, live_recheck_seconds=0.1)
    with client.websocket_connect("/api/lanes/personal/shell/terminal", headers=AGENT) as ws:
        ws.receive_text()
        later(lambda: policy.tokens["agent-tok"][1].discard("personal/shell"))  # no event at all
        err = expect_close(ws, live.CLOSE_DENIED)
        assert "outside your grant" in err.reason


# ---- sign-out ----

def test_terminal_closes_when_the_owner_signs_out():
    rt = demo_runtime()
    _, store, client = make(rt)
    store.add_code("code-two-2")
    resp = client.post("/api/session", json={"code": "code-two-2"})
    csrf = resp.json().get("csrf")
    origin = {"Origin": "http://testserver"}
    with client.websocket_connect("/api/lanes/personal/shell/terminal", headers=origin) as ws:
        ws.receive_text()

        def sign_out():
            client.delete("/api/session", headers={**origin, "X-Agentos-CSRF": csrf or ""})

        later(sign_out)
        err = expect_close(ws, live.CLOSE_REVOKED)
        assert "signed out" in err.reason


# ---- the event stream ----

def test_event_stream_ends_when_its_grant_is_revoked():
    rt = demo_runtime()
    policy = FakePolicy()
    rt.register("policy", policy)
    _, _, client = make(rt, sse_heartbeat=5.0, sse_max_seconds=20)
    t0 = time.monotonic()

    def revoke():
        policy.tokens.clear()
        rt.events.publish("grant", {"id": "g_agent", "state": "revoked"})

    later(revoke, 0.3)
    with client.stream("GET", "/api/events", headers=AGENT) as resp:
        assert resp.status_code == 200
        for _ in resp.iter_lines():
            pass  # the stream ends on its own
    assert time.monotonic() - t0 < 5  # long before the next ping or sse_max_seconds


def test_event_stream_of_a_removed_member_ends_on_the_member_event():
    async def scenario():
        reg = live.LiveSockets()

        async def fine():
            return None

        e = reg.add(live.LiveEntry("events", fine, member_id="m_7", sid="s_7"))
        reg.ended(member_id="m_7", partial="1")  # one device of theirs: rechecked, not closed outright
        assert not e.closed.is_set()
        reg.ended(member_id="m_7")
        assert e.closed.is_set()
        reg._task.cancel()

    asyncio.run(scenario())


def test_a_device_credential_is_not_the_owner_in_person_during_a_freeze():
    rt = demo_runtime()
    rt.register("policy", FakePolicy())
    freeze = FakeFreeze()
    rt.register("freeze", freeze)
    _, store, client = make(rt)
    sid, _ = store.create_session("owner", 3600, via="device")
    client.cookies.set("agentos_session", sid)
    with client.websocket_connect("/api/lanes/personal/shell/terminal",
                                  headers={"Origin": "http://testserver"}) as ws:
        ws.receive_text()

        def freeze_it():
            freeze.node = True
            rt.get("live_sockets").poke()

        later(freeze_it)
        assert "frozen" in expect_close(ws, live.CLOSE_DENIED).reason


def test_a_recheck_does_not_keep_an_idle_session_alive():
    store = PanelStore(None)
    sid, s = store.create_session("m_1", 3600, member_id="m_1", idle_seconds=60)
    with store._lock:
        store._db.execute("UPDATE sessions SET last_seen_at = ? WHERE id_hash = ?", (time.time() - 50, s.id_hash))
        store._db.commit()
    assert store.get_session(sid, touch=False) is not None
    with store._lock:
        seen = store._db.execute("SELECT last_seen_at FROM sessions WHERE id_hash = ?", (s.id_hash,)).fetchone()[0]
    assert time.time() - seen >= 49  # untouched: it still idles out on time
