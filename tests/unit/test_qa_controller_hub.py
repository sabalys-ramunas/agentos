"""QA for the controller hub (agentos.controller.hub): live node sessions and what they carry.

Driven without sockets: each NodeSession gets a fake websocket that records what the controller sends.
The store is the real ControllerStore in a temp dir. Bugs are strict xfails pinned to their reason.
"""

from __future__ import annotations

import asyncio

import pytest

from agentos.controller import hub as hub_module
from agentos.controller.hub import Hub, NodeSession, clean_heartbeat, clean_lane
from agentos.controller.store import ControllerStore
from agentos.node.channel import wire

ORG = "org_qa_one"
NODE = "node_01QAONE000000000000000000"
OTHER_NODE = "node_01QATWO000000000000000000"
HASH = "sha256:" + "a" * 64


class FakeWS:
    def __init__(self):
        self.sent: list = []
        self.closed = False

    async def send_text(self, text: str) -> None:
        self.sent.append(wire.decode(text))

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


def session(node_id: str = NODE, name: str = "box") -> NodeSession:
    return NodeSession(FakeWS(), {"id": node_id, "org_id": ORG, "name": name})


async def wait_sent(ws: FakeWS, kind: str) -> wire.Envelope:
    while True:
        for envelope in ws.sent:
            if isinstance(envelope, wire.Envelope) and envelope.type == kind:
                return envelope
        await asyncio.sleep(0.01)


@pytest.fixture
def hub(tmp_path):
    store = ControllerStore(tmp_path / "controller.db")
    yield Hub(store, keys=None)
    store.close()


# ---- passing: behavior that holds ----

async def test_a_node_cannot_answer_a_call_sent_to_another_node(hub):
    a, b = session(NODE, "a"), session(OTHER_NODE, "b")
    await hub.register(a)
    await hub.register(b)
    call = asyncio.create_task(hub.invoke(ORG, NODE, "whoami", {}, principal={}, grant_token="t", timeout=5))
    sent = await wait_sent(a.ws, "invoke")
    await hub.handle(b, wire.Envelope("result", {"re": sent.id, "ok": True, "content": [
        {"type": "text", "text": "forged"}]}, re=sent.id))
    await asyncio.sleep(0.05)
    assert not call.done()
    await hub.handle(a, wire.Envelope("result", {"re": sent.id, "ok": True, "content": [
        {"type": "text", "text": "real"}]}, re=sent.id))
    body, images = await call
    assert body["content"] == [{"type": "text", "text": "real"}] and images == []


async def test_an_image_arrives_on_the_stream_the_node_pushes_after_its_result(hub):
    node = session()
    await hub.register(node)
    call = asyncio.create_task(hub.invoke(ORG, NODE, "browser_screenshot", {}, principal={}, grant_token="t",
                                          timeout=5))
    sent = await wait_sent(node.ws, "invoke")
    stream_id = wire.ulid()
    await hub.handle(node, wire.Envelope("result", {"ok": True, "content": [
        {"type": "image", "mime_type": "image/png", "stream_id": stream_id}]}, re=sent.id))
    await node.streams.on_binary(wire.stream_frame(stream_id, b"PNG1"))
    await node.streams.on_binary(wire.stream_frame(stream_id, b"PNG2"))
    node.streams.on_close({"stream_id": stream_id, "reason": "eof"})
    body, images = await call
    assert images == [b"PNG1PNG2"]
    assert body["content"] == [{"type": "image", "mime_type": "image/png", "image": 0}]


async def test_a_decision_forwards_the_hash_the_node_sent_not_anything_the_owner_typed(hub, monkeypatch):
    monkeypatch.setattr(hub_module, "DECISION_WAIT", 0.05)
    node = session()
    await hub.register(node)
    hub.store.put_approval(ORG, NODE, {"id": "ap_qa_1", "kind": "action", "state": "pending",
                                       "content_hash": HASH, "tool": "browser_act"})
    await hub.decide(ORG, NODE, "ap_qa_1", "approved", decided_by="owner", answer=None)
    decision = next(e for e in node.ws.sent if isinstance(e, wire.Envelope) and e.type == "approval_decision")
    assert decision.body["content_hash"] == HASH and decision.body["decided_by"] == "owner"


async def test_a_decision_on_an_approval_of_another_node_is_not_found(hub):
    node = session()
    await hub.register(node)
    hub.store.put_approval(ORG, OTHER_NODE, {"id": "ap_qa_other", "state": "pending", "content_hash": HASH})
    from agentos.core.errors import NotFound

    with pytest.raises(NotFound):
        await hub.decide(ORG, NODE, "ap_qa_other", "denied", decided_by="owner")


def test_a_heartbeat_keeps_only_the_wire_shape_and_valid_lane_ids():
    beat = clean_heartbeat({
        "load": {"cpu_pct": "high", "mem_used_mb": 10},
        "lanes": [{"id": "personal/reddit", "kind": "browser", "state": "warm", "signal": "ok",
                   "last_used": None, "extra": "dropped"},
                  {"id": "../etc/passwd", "kind": "browser"}, "not a lane", {"id": "ok/lane", "kind": "k" * 500}],
        "degraded": "yes", "version": "1.0"})
    assert beat["load"]["cpu_pct"] is None and beat["load"]["mem_used_mb"] == 10
    assert [lane["id"] for lane in beat["lanes"]] == ["personal/reddit", "ok/lane"]
    assert "extra" not in beat["lanes"][0] and len(beat["lanes"][1]["kind"]) == 20
    assert beat["degraded"] is True


def test_a_lane_event_never_carries_an_exit_ip_or_a_proxy_address():
    lane = clean_lane({"id": "personal/reddit", "state": "warm", "exit_ip": "203.0.113.9",
                       "proxy": {"label": "de-1", "url": "http://user:secret@proxy.example"},
                       "control": {"state": "human", "by": "alice", "takeback_requested": 1}}, "box")
    assert lane["exit_ip"] is None and lane["proxy"] == {"label": "de-1"} and lane["node"] == "box"
    assert lane["control"]["takeback_requested"] is True
    assert clean_lane({"id": "../x"}, "box") is None


# ---- bugs ----

async def test_a_replaced_connection_going_away_does_not_report_its_node_offline(hub):
    seen: list = []
    hub.listeners.append(lambda org, kind, payload: seen.append((kind, payload)))
    old, new = session(), session()
    await hub.register(old)
    await hub.register(new)
    hub.unregister(old)
    assert hub.sessions[NODE] is new
    assert ("node", {"id": NODE, "connected": False}) not in seen


async def test_a_replaced_connection_going_away_leaves_the_new_connections_requests_alone(hub):
    old, new = session(), session()
    await hub.register(old)
    await hub.register(new)
    future = asyncio.get_running_loop().create_future()
    hub.requests["m_qa_on_new"] = (NODE, future)
    hub.unregister(old)
    try:
        assert not future.done()
    finally:
        if future.done() and not future.cancelled():
            future.exception()  # retrieved here, so asyncio does not log it as never retrieved


# ---- judgment items QA left open, and the tool list in parts (BUG-5) ----

def tool(name: str) -> dict:
    return {"name": name, "description": "", "input_schema": {"type": "object", "properties": {}},
            "effect": "read", "lane_arg": None, "tags": []}


def node_event(seq: int, payload: dict) -> wire.Envelope:
    return wire.Envelope("event", {"kind": "node", "payload": payload}, seq=seq)


async def test_a_tool_list_in_parts_is_applied_once_it_is_whole(hub):
    node = session()
    await hub.register(node)
    await hub.handle(node, node_event(1, {"tools": [tool("first_tool")]}))
    await hub.handle(node, node_event(2, {"tools": [tool("part_a")], "tools_part": {"index": 0, "count": 2}}))
    assert set(node.tools) == {"first_tool"}  # half a list changes nothing yet
    await hub.handle(node, node_event(3, {"tools": [tool("part_b")], "tools_part": {"index": 1, "count": 2}}))
    assert set(node.tools) == {"part_a", "part_b"}


async def test_a_tools_part_out_of_order_is_not_applied(hub):
    node = session()
    await hub.register(node)
    await hub.handle(node, node_event(1, {"tools": [tool("whole")]}))
    await hub.handle(node, node_event(2, {"tools": [tool("late")], "tools_part": {"index": 1, "count": 2}}))
    await hub.handle(node, node_event(3, {"tools": [tool("bad")], "tools_part": {"index": 5, "count": 2}}))
    assert set(node.tools) == {"whole"}


def test_a_new_seq_epoch_resets_the_dedupe_and_the_same_one_keeps_it(tmp_path):
    store = ControllerStore(tmp_path / "c.db")
    try:
        org = store.default_org()["id"]
        _, token = store.create_join_token(org, name="box")
        node = store.enroll(token, pubkey="k", name="box", version=None, os_name=None, arch=None,
                            capabilities=[], max_warm_lanes=None)
        assert store.set_seq_epoch(node["id"], "ep_one")
        store.set_last_seq(node["id"], 40)
        assert not store.set_seq_epoch(node["id"], "ep_one") and store.last_seq(node["id"]) == 40
        assert store.set_seq_epoch(node["id"], "ep_two") and store.last_seq(node["id"]) == 0
    finally:
        store.close()


async def test_signed_pushes_are_dated(hub):
    class Keys:
        def sign(self, kind, node_id, body):
            return {**body, "sig": "s"}

    hub.keys = Keys()
    node = session()
    await hub.register(node)
    await hub.push_signed(node, "revoke", {"revocation_ids": ["rv_x"]})
    sent = next(e for e in node.ws.sent if isinstance(e, wire.Envelope) and e.type == "revoke")
    assert abs(wire.parse_time(sent.body["ts"]) - hub.clock()) < 5
