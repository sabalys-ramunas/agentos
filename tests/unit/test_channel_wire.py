"""The wire format, the node key, the channel store, and the keepalive."""

import asyncio
import json
import os
import stat
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentos.core import signing
from agentos.core.signing import verify_controller
from agentos.node.channel import wire
from agentos.node.channel.identity import KeyFileError, NodeKey, load_or_create_key
from agentos.node.channel.node import Connection, NodeChannel
from agentos.node.channel.store import ChannelStore


def test_envelope_round_trip_and_reply_lookup():
    envelope = wire.Envelope("event", {"kind": "lane", "payload": {}}, seq=7, re="m_1")
    data = json.loads(envelope.encode())
    assert data["v"] == 1 and data["type"] == "event" and data["seq"] == 7 and data["id"].startswith("m_")
    assert len(data["id"]) == 2 + wire.ULID_LENGTH and data["ts"].endswith("Z")
    back = wire.decode(envelope.encode())
    assert (back.type, back.seq, back.re, back.body) == ("event", 7, "m_1", envelope.body)
    assert wire.Envelope("result", {"re": "m_body"}).reply_to == "m_body"
    assert wire.Envelope("result", {}, re="m_env").reply_to == "m_env"
    assert "seq" not in json.loads(wire.Envelope("heartbeat").encode())


@pytest.mark.parametrize(("frame", "code"), [
    ("{nope", "invalid"),
    ("[1, 2]", "invalid"),
    (json.dumps({"v": 2, "type": "x", "id": "m_a", "body": {}}), "unsupported"),
    (json.dumps({"v": 1, "id": "m_a", "body": {}}), "invalid"),
    (json.dumps({"v": 1, "type": "", "body": {}}), "invalid"),
    (json.dumps({"v": 1, "type": "x", "body": [1]}), "invalid"),
    (json.dumps({"v": 1, "type": "x", "body": {"pad": "y" * wire.MAX_JSON_BYTES}}), "invalid"),
    (b"\xff\xfe", "invalid"),
])
def test_malformed_frames_raise_with_the_reply_code(frame, code):
    with pytest.raises(wire.WireError) as raised:
        wire.decode(frame)
    assert raised.value.code == code


def test_a_message_is_never_encoded_past_one_mib():
    with pytest.raises(wire.Oversize):
        wire.Envelope("traces", {"events": ["x" * wire.MAX_JSON_BYTES]}).encode()


def test_odd_seq_and_re_are_ignored():
    back = wire.decode(json.dumps({"v": 1, "type": "x", "id": "m_a", "seq": True, "re": 5, "body": {}}))
    assert back.seq is None and back.re is None


def test_ulids_and_stream_frames():
    a, b = wire.ulid(), wire.ulid()
    assert a != b and len(a) == 26 and wire.is_ulid(a) and wire.ulid_text(wire.ulid_bytes(a)) == a
    assert not wire.is_ulid("not-a-ulid") and not wire.is_ulid("Z" * 26) and not wire.is_ulid(None)
    frame = wire.stream_frame(a, b"payload")
    assert len(frame) == 16 + 7 and wire.parse_stream_frame(frame) == (a, b"payload")
    with pytest.raises(wire.WireError):
        wire.parse_stream_frame(b"short")


def test_base64url_is_unpadded_and_strict():
    raw = os.urandom(32)
    text = signing.b64e(raw)
    assert "=" not in text and signing.b64d(text) == raw
    for bad in ("a+b/", "abc=", "**"):
        with pytest.raises(ValueError):
            signing.b64d(bad)


def test_signatures_use_the_pinned_strings():
    key = Ed25519PrivateKey.generate()
    pub = signing.public_b64(key)
    assert wire.hello_message("n0nce", "node_1") == b"agentos-node-hello:v1:n0nce:node_1"
    body = {"revocation_ids": ["rv_a"], "é": "ü"}
    signed = signing.sign_controller(key, "revoke", "node_1", body)
    expected = (b"agentos-controller-msg:v1:revoke:node_1:"
                + json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    assert signing.controller_message("revoke", "node_1", signed) == expected
    assert verify_controller(pub, "revoke", "node_1", signed)
    assert not verify_controller(pub, "revoke", "node_2", signed)          # another node
    assert not verify_controller(pub, "config_update", "node_1", signed)   # another type
    assert not verify_controller(pub, "revoke", "node_1", {**signed, "revocation_ids": []})
    assert not verify_controller(pub, "revoke", "node_1", {"revocation_ids": ["rv_a"]})
    assert not verify_controller(pub, "revoke", "node_1", {**signed, "sig": "!!"})
    assert not signing.verify("not a key", signed["sig"], expected)


@pytest.mark.parametrize(("typed", "url"), [
    ("ws://localhost:7401", "ws://localhost:7401/v1/node/connect"),
    ("http://127.0.0.1:7401/", "ws://127.0.0.1:7401/v1/node/connect"),
    ("https://ctl.example.com", "wss://ctl.example.com/v1/node/connect"),
    ("ctl.example.com", "wss://ctl.example.com/v1/node/connect"),
    ("wss://ctl.example.com/custom/path", "wss://ctl.example.com/custom/path"),
])
def test_controller_urls(typed, url):
    assert wire.connect_url(typed) == url


@pytest.mark.parametrize("bad", ["ws://ctl.example.com", "ws://10.0.0.5:7401", "ftp://x", "wss://user:pw@ctl.example",
                                 "wss://"])
def test_plain_ws_only_to_localhost(bad):
    with pytest.raises(ValueError):
        wire.connect_url(bad)


def test_time_parsing():
    assert wire.parse_time("2026-11-04T03:00:00Z") == wire.parse_time("2026-11-04T03:00:00+00:00")
    assert wire.parse_time(12.5) == 12.5 and wire.parse_time(True) is None and wire.parse_time("soon") is None


# ---- the node key ----

def test_the_node_key_is_private_and_stable(tmp_path):
    key = NodeKey.load_or_create(tmp_path / "secrets")
    assert stat.S_IMODE(os.stat(key.path).st_mode) == 0o600
    assert NodeKey.load_or_create(tmp_path / "secrets").public == key.public
    os.chmod(key.path, 0o644)
    with pytest.raises(KeyFileError):
        load_or_create_key(key.path)


# ---- the store ----

def test_the_outbox_survives_a_restart_and_seq_is_never_reset(tmp_path):
    store = ChannelStore(tmp_path / "channel.db")
    seqs = [store.enqueue("event", {"kind": "run", "payload": {"n": i}}) for i in range(3)]
    assert seqs == [1, 2, 3]
    frames = store.outbox_after(0)
    store.close()
    again = ChannelStore(tmp_path / "channel.db")
    assert again.outbox_after(0) == frames  # same ids and seqs, byte for byte
    assert [json.loads(f)["seq"] for _, f in again.outbox_after(1)] == [2, 3]
    again.trim(3)
    assert again.depth() == 0
    assert again.enqueue("traces", {"events": []}) == 4
    with pytest.raises(ValueError):
        again.enqueue("heartbeat", {})
    with pytest.raises(wire.Oversize):
        again.enqueue("event", {"pad": "x" * wire.MAX_JSON_BYTES})
    assert again.high_seq == 4 and again.depth() == 1
    assert stat.S_IMODE(os.stat(tmp_path / "channel.db").st_mode) == 0o600


def test_the_call_cache_expires(tmp_path):
    now = [1000.0]
    store = ChannelStore(tmp_path / "channel.db", clock=lambda: now[0])
    store.put_call("c1", "done", tool="t", result={"ok": True, "content": []})
    store.put_call("c2", "running", tool="t")
    assert store.get_call("c1").result == {"ok": True, "content": []} and store.running_calls() == ["c2"]
    now[0] += 301
    assert store.purge_calls(300) == 1 and store.get_call("c1") is None and store.get_call("c2") is not None


def test_a_call_running_at_a_crash_reads_as_interrupted_after_it(tmp_path):
    from agentos.core.config import NodeConfig
    from agentos.core.runtime import Runtime

    store = ChannelStore(tmp_path / "channel.db")
    store.put_call("call_crashed", "running", tool="slow_tool")
    config = NodeConfig(home=tmp_path, controller_url="ws://localhost:1", node_id="node_x", controller_pubkey="x")
    NodeChannel(config, Runtime(config=config), store=store)
    cached = store.get_call("call_crashed")
    assert cached.state == "done" and cached.result["error"]["code"] == "unavailable"


# ---- keepalive ----

class StubTransport:
    def __init__(self):
        self.aborted = False

    def abort(self):
        self.aborted = True


class StubSocket:
    def __init__(self):
        self.transport = StubTransport()
        self.pings = 0

    async def ping(self):
        self.pings += 1
        return asyncio.get_running_loop().create_future()  # a pong that never comes


async def test_a_silent_connection_is_declared_dead(tmp_path):
    from agentos.core.config import NodeConfig
    from agentos.core.runtime import Runtime
    from agentos.node.channel.node import Timings

    config = NodeConfig(home=tmp_path, controller_url="ws://localhost:1", node_id="node_x", controller_pubkey="x")
    channel = NodeChannel(config, Runtime(config=config), timings=Timings(ping_interval=0.1, dead_after=0.5))
    conn = Connection(StubSocket())
    started = time.monotonic()
    await asyncio.wait_for(channel._keepalive(conn), 3)
    assert conn.ws.transport.aborted and conn.ws.pings >= 3 and time.monotonic() - started >= 0.5
