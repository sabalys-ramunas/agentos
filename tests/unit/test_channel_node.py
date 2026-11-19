"""The node end of the channel against a scripted controller: one test per protocol behavior."""

import asyncio
import base64
import inspect
import json
import re
import sqlite3
import time

import pytest
from channel_fake_controller import NODE_ID, FakeController, node_pubkey, node_runtime, start, stop

from agentos.core import signing
from agentos.core.models import ControlState
from agentos.node.channel import node as channel_node
from agentos.node.channel import signed as channel_signed
from agentos.node.channel import wire
from agentos.node.channel.node import Backoff
from agentos.policy import tokens
from agentos.policy.grants import GrantBlock, Specials
from agentos.policy.util import new_id


def grant(fake, *, workspaces=("*",), ceiling="irreversible", exp=None, admin=None):
    block = {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "iat": int(time.time()),
             "sub": {"kind": "agent", "id": "tester", "owner": "owner"}, "workspaces": list(workspaces),
             "ceiling": ceiling, "specials": Specials.everything().model_dump(mode="json", exclude_none=True),
             "exp": exp or int(time.time()) + 600, "depth": 3}
    if admin is not None:
        block["admin"] = admin
    token = tokens.mint(fake.grant_key, GrantBlock.model_validate(block).to_data())
    return token, block["id"], block["rid"]


def invoke_body(fake, tool, args, *, call_id=None, token=None, deadline=None):
    body = {"call_id": call_id or wire.prefixed("call"), "tool": tool, "args": args,
            "principal": {"kind": "agent", "id": "tester", "thread": "t1"},
            "grant_token": token or grant(fake)[0], "deadline": deadline or wire.iso(time.time() + 30)}
    return body


@pytest.fixture
async def wired(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        await start(runtime)
        peer = await fake.peer()
        await asyncio.wait_for(runtime.channel.welcomed.wait(), 15)  # the fake sees the peer before the node is in
        yield fake, runtime, peer
        await stop(runtime)


async def test_hello_is_signed_with_the_exact_string_and_welcome_starts_the_session(wired):
    fake, runtime, peer = wired
    hello = fake.hellos[0]
    assert hello["node_id"] == NODE_ID
    assert signing.verify(node_pubkey(runtime.config.home), hello["sig"],
                       f"agentos-node-hello:v1:{hello['nonce']}:{NODE_ID}".encode())
    beat = await peer.recv("heartbeat")
    assert {"load", "lanes", "degraded", "version"} <= set(beat.body)
    assert beat.body["lanes"] == [{"id": "personal/reddit", "kind": "browser", "state": "cold", "signal": "unknown",
                                   "last_used": None}]
    tools = await peer.recv("event", where=lambda e: e.body["kind"] == "node" and "tools" in e.body["payload"])
    names = {t["name"] for t in tools.body["payload"]["tools"]}
    assert {"browser_snapshot", "post_note", "browsers_list"} <= names
    snapshot = next(t for t in tools.body["payload"]["tools"] if t["name"] == "browser_snapshot")
    assert snapshot["lane_arg"] == "on" and snapshot["effect"] == "read"
    assert tools.seq is not None and tools.id.startswith("m_")
    lane = await peer.recv("event", where=lambda e: e.body["kind"] == "lane")
    assert lane.body["payload"]["exit_ip"] is None and lane.body["payload"]["id"] == "personal/reddit"


async def test_heartbeat_comes_again_on_a_lane_change(wired):
    _fake, runtime, peer = wired
    await peer.recv("heartbeat")
    await runtime.lanes.open("personal/reddit")
    beat = await peer.recv("heartbeat", where=lambda e: e.body["lanes"][0]["state"] == "warm", timeout=3)
    assert beat.body["lanes"][0]["state"] == "warm"
    event = await peer.recv("event", where=lambda e: e.body["kind"] == "lane"
                            and e.body["payload"]["state"] == "warm")
    assert event.body["payload"]["id"] == "personal/reddit"


async def test_unknown_types_and_bad_frames_get_errors_and_the_connection_stays(wired):
    _fake, runtime, peer = wired
    sent = await peer.send("teleport", {"to": "mars"})
    reply = await peer.reply_to(sent.id)
    assert reply.type == "error" and reply.body["code"] == "unsupported" and reply.body["re"] == sent.id
    await peer.send_raw("{not json")
    assert (await peer.recv("error", where=lambda e: e.body["code"] == "invalid")).body["message"]
    await peer.send_raw(json.dumps({"v": 2, "type": "ack", "id": "m_x", "body": {}}))
    assert (await peer.recv("error", where=lambda e: e.body["code"] == "unsupported")).reply_to == "m_x"
    await peer.send_raw(json.dumps({"v": 1, "type": "ack", "id": "m_y", "body": []}))
    assert (await peer.recv("error", where=lambda e: e.reply_to == "m_y")).body["code"] == "invalid"
    big = json.dumps({"v": 1, "type": "ack", "id": "m_big", "body": {"pad": "x" * (wire.MAX_JSON_BYTES + 10)}})
    await peer.send_raw(big)
    assert (await peer.recv("error", where=lambda e: "1 MiB" in e.body["message"])).body["code"] == "invalid"
    await peer.send_raw(b"\x00" * 5)  # a binary frame shorter than the stream header is dropped
    ping = await peer.send("ack", {"upto_seq": 0})
    await asyncio.sleep(0.2)
    assert not [e for e in peer.seen if e.reply_to == ping.id]  # a valid ack has no reply: still connected
    assert runtime.channel.connected


async def test_invoke_runs_through_the_policy_wrapper_with_the_grant_bound(wired):
    fake, _runtime, peer = wired
    sent = await peer.send("invoke", invoke_body(fake, "browser_snapshot", {"on": "personal/reddit"}))
    result = await peer.reply_to(sent.id)
    assert result.type == "result" and result.body["ok"] and result.body["re"] == sent.id
    assert "[ref=e1]" in result.body["content"][0]["text"]
    # a token signed by another key is refused by the node's own policy
    forged = tokens.mint(wire_key(), GrantBlock.model_validate({
        "v": 1, "id": new_id("gr"), "rid": new_id("rv"), "iat": 1, "sub": {"id": "x"}, "workspaces": ["*"],
        "exp": int(time.time()) + 60}).to_data())
    bad = await peer.send("invoke", invoke_body(fake, "browser_snapshot", {"on": "personal/reddit"}, token=forged))
    refused = await peer.reply_to(bad.id)
    assert not refused.body["ok"] and refused.body["error"]["code"] == "denied"
    out = await peer.send("invoke", invoke_body(fake, "browser_snapshot", {"on": "personal/reddit"},
                                                token=grant(fake, workspaces=("work",))[0]))
    scoped = await peer.reply_to(out.id)
    assert not scoped.body["ok"] and "out_of_scope" in scoped.body["error"]["message"]
    admin = await peer.send("invoke", invoke_body(fake, "browsers_list", {}, token=grant(fake, admin=True)[0]))
    assert "foreign_admin" in (await peer.reply_to(admin.id)).body["error"]["message"]


def wire_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.generate()


async def test_invoke_is_at_most_once_per_call_id(wired):
    fake, runtime, peer = wired
    counter = runtime.get("_counter") or {"n": 0}
    original = runtime.mcp.specs["slow_tool"].handler

    async def counted(ctx, *, seconds: float = 5.0):
        counter["n"] += 1
        return await original(ctx, seconds=seconds)

    runtime.mcp.specs["slow_tool"].handler = counted
    # the registered SDK function closes over the wrapper, which calls spec.handler at call time
    body = invoke_body(fake, "slow_tool", {"seconds": 0.4}, call_id="call_once")
    first = await peer.send("invoke", body)
    retry = await peer.send("invoke", body)  # while it runs: joins the running call
    one = await peer.reply_to(first.id)
    two = await peer.reply_to(retry.id)
    assert one.body["ok"] and two.body["ok"] and one.body["content"] == two.body["content"]
    later = await peer.send("invoke", body)  # after: the cached result, without running again
    three = await peer.reply_to(later.id, timeout=1)
    assert three.body["content"] == one.body["content"]
    assert counter["n"] == 1


async def test_cancel_and_deadlines(wired):
    fake, _runtime, peer = wired
    body = invoke_body(fake, "slow_tool", {"seconds": 10})
    sent = await peer.send("invoke", body)
    await asyncio.sleep(0.2)
    await peer.send("cancel", {"call_id": body["call_id"]})
    cancelled = await peer.reply_to(sent.id)
    assert cancelled.body["error"]["code"] == "cancelled"
    late = await peer.send("invoke", invoke_body(fake, "slow_tool", {"seconds": 10},
                                                 deadline=wire.iso(time.time() - 5)))
    assert (await peer.reply_to(late.id)).body["error"]["code"] == "deadline_exceeded"
    short = await peer.send("invoke", invoke_body(fake, "slow_tool", {"seconds": 10},
                                                  deadline=wire.iso(time.time() + 1.5)))
    assert (await peer.reply_to(short.id, timeout=6)).body["error"]["code"] == "deadline_exceeded"
    bad = await peer.send("invoke", {"call_id": "", "tool": "slow_tool"})
    assert (await peer.reply_to(bad.id)).body["code"] == "invalid"


async def test_signed_revoke_cascades_and_a_bad_signature_changes_nothing(wired):
    fake, runtime, peer = wired
    token, grant_id, rid = grant(fake)
    issuers = runtime.get("grant_issuers")
    forged = signing.sign_controller(wire_key(), "revoke", NODE_ID, {"revocation_ids": [rid]})
    sent = await peer.send("revoke", forged)
    reply = await peer.reply_to(sent.id)
    assert reply.type == "error" and reply.body["code"] == "denied"
    assert not issuers.is_revoked([rid])
    # a narrowed child of the grant
    child = tokens.append(token, {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "parent": grant_id,
                                  "iat": int(time.time()), "ceiling": "read"})
    running = invoke_body(fake, "slow_tool", {"seconds": 10}, token=child)
    call = await peer.send("invoke", running)
    await asyncio.sleep(0.2)
    await peer.send("revoke", fake.fresh("revoke", {"revocation_ids": [rid]}))
    stopped = await peer.reply_to(call.id)
    assert stopped.body["error"]["code"] == "cancelled" and "revoked" in stopped.body["error"]["message"]
    assert issuers.is_revoked([rid])
    after = await peer.send("invoke", invoke_body(fake, "browsers_list", {}, token=child))
    assert "revoked" in (await peer.reply_to(after.id)).body["error"]["message"]
    # tampering with a signed body breaks the signature
    body = fake.fresh("revoke", {"revocation_ids": ["rv_other00000"]})
    body["revocation_ids"] = ["rv_changed0000"]
    tampered = await peer.send("revoke", body)
    assert (await peer.reply_to(tampered.id)).body["code"] == "denied"


async def test_config_update_needs_the_pinned_signature(wired):
    fake, runtime, peer = wired
    sent = await peer.send("config_update", signing.sign_controller(wire_key(), "config_update", NODE_ID,
                                                                {"config": {"heartbeat_seconds": 5}}))
    assert (await peer.reply_to(sent.id)).body["code"] == "denied"
    assert "heartbeat_seconds" not in runtime.channel.controller_config
    await peer.send("config_update", fake.fresh("config_update", {"config": {"heartbeat_seconds": 5, "x": 1}}))
    await asyncio.sleep(0.2)
    assert runtime.channel.controller_config == {"heartbeat_seconds": 5, "x": 1}
    await peer.send("config_update", fake.fresh("config_update", {"config": {"heartbeat_seconds": 9999}}))
    await asyncio.sleep(0.2)
    # out of range is not applied: the value in force stays
    assert runtime.channel.controller_config == {"heartbeat_seconds": 5, "x": 1}


async def test_ack_trims_the_outbox_but_never_past_what_was_delivered(wired):
    _fake, runtime, peer = wired
    channel = runtime.channel
    last = await peer.recv("event", where=lambda e: e.body["kind"] == "lane")
    await asyncio.sleep(0.2)
    assert channel.store.depth() >= 2
    await peer.send("ack", {"upto_seq": 10**9})  # bogus: only what this connection delivered goes
    await asyncio.sleep(0.2)
    assert channel.store.depth() == 0
    channel._enqueue_event("run", {"id": "run_1"})
    event = await peer.recv("event", where=lambda e: e.body["kind"] == "run")
    assert event.seq > last.seq
    bad = await peer.send("ack", {"upto_seq": "all"})
    assert (await peer.reply_to(bad.id)).body["code"] == "invalid"


async def test_reconnect_redelivers_the_outbox_in_seq_order_with_the_same_ids(wired):
    fake, runtime, peer = wired
    channel = runtime.channel
    await peer.recv("event", where=lambda e: e.body["kind"] == "lane")
    first = [e for e in peer.seen if e.type in wire.OUTBOX_TYPES]
    await peer.ws.close()
    second = await fake.peer()
    assert len(fake.hellos) == 2 and fake.hellos[0]["nonce"] != fake.hellos[1]["nonce"]
    await second.recv("event", where=lambda e: e.body["kind"] == "node" and e.body["payload"].get("degraded"))
    again = [e for e in second.seen if e.type in wire.OUTBOX_TYPES]
    seqs = [e.seq for e in again]
    assert seqs == sorted(seqs)
    assert {(e.id, e.seq) for e in first} <= {(e.id, e.seq) for e in again}
    # degraded until everything from before is acknowledged, then a recovery event
    beat = await second.recv("heartbeat")
    assert beat.body["degraded"] is True and channel.degraded
    await second.send("ack", {"upto_seq": max(seqs)})
    recovered = await second.recv("event", where=lambda e: e.body["kind"] == "node"
                                  and e.body["payload"].get("degraded") is False)
    assert recovered.body["payload"]["since"]
    assert (await second.recv("heartbeat", where=lambda e: e.body["degraded"] is False)).body["degraded"] is False


async def test_refused_hello_backs_off_and_tries_again(wired):
    fake, runtime, peer = wired
    fake.refuse_next = True
    await peer.ws.close()
    peer2 = await fake.peer(timeout=10)
    await asyncio.wait_for(runtime.channel.welcomed.wait(), 5)
    assert len(fake.hellos) == 3 and runtime.channel.sessions == 2
    assert "refused" in (runtime.channel.last_error or "")
    assert peer2 is not peer


def test_backoff_grows_to_the_cap_with_jitter():
    import random

    backoff = Backoff(1.0, 60.0, random.Random(4))
    delays = [backoff.next() for _ in range(10)]
    assert 1.0 <= delays[0] <= 1.25 and 2.0 <= delays[1] <= 2.5
    assert all(60.0 <= d <= 75.0 for d in delays[6:])
    backoff.reset()
    assert backoff.next() <= 1.25


async def test_degraded_mode_refuses_grants_the_node_has_never_seen(wired):
    fake, runtime, peer = wired
    seen_token = grant(fake)[0]
    runtime.policy.resolve(seen_token)  # seen while connected
    fake.refuse_all = True
    await peer.ws.close()
    await asyncio.sleep(0.3)
    assert not runtime.channel.connected
    with pytest.raises(Exception) as unseen:
        runtime.policy.resolve(grant(fake)[0])
    assert getattr(unseen.value, "reason", "") == "degraded"
    principal, _ = runtime.policy.resolve(seen_token)  # cached grants keep working until they expire
    assert principal.id == "tester"
    expired = grant(fake, exp=int(time.time()) - 3600)[0]
    with pytest.raises(Exception) as late:
        runtime.policy.resolve(expired)
    assert getattr(late.value, "reason", "") in ("expired", "degraded")
    fake.refuse_all = False
    await asyncio.wait_for(runtime.channel.welcomed.wait(), 5)
    runtime.policy.resolve(grant(fake)[0])  # back online: new grants are accepted again


async def test_approvals_go_out_and_decisions_must_match_the_content_hash(wired):
    fake, runtime, peer = wired
    body = invoke_body(fake, "post_note", {"on": "personal/reddit", "text": "hi"}, call_id="call_note")
    sent = await peer.send("invoke", body)
    pending = await peer.reply_to(sent.id)
    approval_id = pending.body["error"]["approval_id"]
    assert pending.body["error"]["code"] == "approval_pending"
    request = await peer.recv("approval_request")
    approval = request.body["approval"]
    assert approval["id"] == approval_id and approval["content_hash"].startswith("sha256:") and request.seq

    wrong = await peer.send("approval_decision", {"approval_id": approval_id, "decision": "approved",
                                                  "content_hash": "sha256:" + "0" * 64, "decided_by": "owner"})
    refused = await peer.reply_to(wrong.id)
    assert refused.body["code"] == "invalid" and refused.body["approval_id"] == approval_id
    assert runtime.approvals.get(approval_id).state == "pending"
    missing = await peer.send("approval_decision", {"approval_id": "ap_nothere000000", "decision": "denied"})
    assert (await peer.reply_to(missing.id)).body["code"] == "not_found"

    await peer.send("approval_decision", {"approval_id": approval_id, "decision": "approved",
                                          "content_hash": approval["content_hash"], "decided_by": "owner"})
    event = await peer.recv("event", where=lambda e: e.body["kind"] == "approval")
    assert event.body["payload"] == {"id": approval_id, "state": "approved", "decided_by": "controller:owner"}
    twice = await peer.send("approval_decision", {"approval_id": approval_id, "decision": "denied",
                                                  "content_hash": approval["content_hash"]})
    refused = (await peer.reply_to(twice.id)).body
    assert refused["code"] == "already_decided" and refused["state"] == "approved"  # what happened, not only that it did

    # the same call_id, retried after the approval, runs and returns the final result
    retry = await peer.send("invoke", body)
    final = await peer.reply_to(retry.id)
    assert final.body["ok"], final.body
    assert json.loads(final.body["content"][0]["text"]) == {"posted": "hi", "on": "personal/reddit"}
    consumed = await peer.recv("event", where=lambda e: e.body["kind"] == "approval"
                               and e.body["payload"]["state"] == "consumed")
    assert consumed.body["payload"]["id"] == approval_id


async def test_traces_are_batched_redacted_and_carry_no_grant_path(wired):
    fake, _runtime, peer = wired
    sent = await peer.send("invoke", invoke_body(fake, "browser_snapshot", {"on": "personal/reddit"}))
    await peer.reply_to(sent.id)
    batch = await peer.recv("traces", where=lambda e: any(t["tool"] == "browser_snapshot"
                                                          for t in e.body["events"]))
    assert 1 <= len(batch.body["events"]) <= wire.MAX_TRACES_PER_BATCH
    for event in batch.body["events"]:
        assert "grant_path" not in event and "args" not in event


async def test_lane_control_is_signed_and_takes_the_lane(wired):
    fake, runtime, peer = wired
    forged = signing.sign_controller(wire_key(), "lane_control", NODE_ID,
                                  {"lane": "personal/reddit", "action": "take", "by": "owner"})
    sent = await peer.send("lane_control", forged)
    assert (await peer.reply_to(sent.id)).body["code"] == "denied"
    assert runtime.lanes.get("personal/reddit").control.state == ControlState.AGENT
    await peer.send("lane_control", fake.fresh("lane_control", {"lane": "personal/reddit", "action": "take",
                                                               "by": "owner"}))
    event = await peer.recv("event", where=lambda e: e.body["kind"] == "lane"
                            and (e.body["payload"]["control"] or {}).get("state") == "human")
    assert event.body["payload"]["control"]["by"] == "controller:owner"


async def test_results_with_images_push_a_stream_under_credit(wired):
    fake, runtime, peer = wired
    big = bytes(range(256)) * 2048  # 512 KiB, twice the starting credit

    async def shot(page):
        return big

    runtime.interaction.screenshot = shot
    sent = await peer.send("invoke", invoke_body(fake, "browser_screenshot", {"on": "personal/reddit"}))
    result = await peer.reply_to(sent.id)
    assert result.body["ok"], result.body
    image = next(c for c in result.body["content"] if c["type"] == "image")
    assert "data" not in image and wire.is_ulid(image["stream_id"])
    received = b""
    while len(received) < wire.INITIAL_CREDIT:
        stream_id, payload = wire.parse_stream_frame(await asyncio.wait_for(peer.binary.get(), 5))
        assert stream_id == image["stream_id"]
        received += payload
    await asyncio.sleep(0.3)
    assert len(received) == wire.INITIAL_CREDIT and peer.binary.empty()  # stalls at the credit
    await peer.send("stream_credit", {"stream_id": image["stream_id"], "bytes": len(big)})
    while len(received) < len(big):
        received += wire.parse_stream_frame(await asyncio.wait_for(peer.binary.get(), 5))[1]
    assert received == big
    closed = await peer.recv("stream_close", where=lambda e: e.body["stream_id"] == image["stream_id"])
    assert closed.body["reason"] == "eof"


async def test_stream_open_refusals(wired):
    _fake, _runtime, peer = wired
    cold = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": "vnc", "lane": "personal/reddit"})
    reply = await peer.reply_to(cold.id)
    assert reply.type == "error" and reply.body["code"] == "unavailable"
    nope = await peer.send("stream_open", {"stream_id": "not-a-ulid", "kind": "vnc", "lane": "personal/reddit"})
    assert (await peer.reply_to(nope.id)).body["code"] == "invalid"
    odd = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": "telepathy", "lane": "personal/reddit"})
    assert (await peer.reply_to(odd.id)).body["code"] == "invalid"
    term = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": "terminal", "lane": "personal/reddit"})
    assert (await peer.reply_to(term.id)).body["code"] == "unavailable"  # no env service on this node


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def test_the_outbox_survives_a_node_restart(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        fake.refuse_all = True  # the controller is unreachable for this node
        runtime = await node_runtime(home, fake)
        await start(runtime)
        runtime.channel._enqueue_event("run", {"id": "run_offline", "status": "succeeded_verified"})
        before = {(json.loads(frame)["id"], seq) for seq, frame in runtime.channel.store.outbox_after(0)}
        await stop(runtime)
        fake.refuse_all = False
        restarted = await node_runtime(home, fake)
        await start(restarted)
        peer = await fake.peer(timeout=10)
        run = await peer.recv("event", where=lambda e: e.body["kind"] == "run")
        assert (run.id, run.seq) in before and run.body["payload"]["id"] == "run_offline"
        delivered = [e for e in peer.seen if e.type in wire.OUTBOX_TYPES]
        assert [e.seq for e in delivered] == sorted(e.seq for e in delivered)
        await stop(restarted)


# ---- freshness and replay (docs/PROTOCOL.md "Freshness and replay") ----

@pytest.mark.parametrize("kind, body", [
    ("config_update", {"config": {"heartbeat_seconds": 7}}),
    ("lane_control", {"lane": "personal/reddit", "action": "take", "by": "owner"}),
    ("mesh_freeze", {"frozen": False}),
])
async def test_every_signed_kind_needs_ts_and_a_nonce_and_refuses_a_replay(wired, kind, body):
    fake, _runtime, peer = wired
    undated = fake.sign(kind, {**body, "nonce": "n0nce-undated-1"})
    assert (await peer.reply_to((await peer.send(kind, undated)).id)).body["code"] == "invalid"
    no_nonce = fake.sign(kind, {**body, "ts": wire.iso()})
    assert (await peer.reply_to((await peer.send(kind, no_nonce)).id)).body["code"] == "invalid"
    stale = fake.sign(kind, {**body, "ts": wire.iso(time.time() - 3600), "nonce": "n0nce-stale-01"})
    assert (await peer.reply_to((await peer.send(kind, stale)).id)).body["code"] == "denied"
    good = fake.fresh(kind, body)
    first = await peer.send(kind, good)
    await asyncio.sleep(0.2)
    assert not [e for e in peer.seen if e.reply_to == first.id and e.type == "error"]
    replay = await peer.reply_to((await peer.send(kind, good)).id)
    assert replay.body["code"] == "denied" and "already applied" in replay.body["message"]


@pytest.mark.parametrize("ts", [float("nan"), float("inf"), float("-inf"), 10 ** 400])
async def test_a_signed_message_dated_with_a_time_that_is_not_finite_is_refused(wired, ts):
    """JSON carries NaN and Infinity (Python reads `1e999` as inf), and NaN would pass any
    `abs(now - ts) > window` check: a correctly signed message with one is refused as undated."""
    fake, _runtime, peer = wired
    body = fake.sign("config_update", {"config": {"heartbeat_seconds": 9}, "ts": ts, "nonce": "n0nce-nan-001"})
    reply = await peer.reply_to((await peer.send("config_update", body)).id)
    assert reply.body["code"] == "invalid" and "needs ts" in reply.body["message"]
    assert wire.parse_time(ts) is None


async def test_a_replayed_config_update_cannot_drop_the_confirmation_layer(wired):
    fake, runtime, peer = wired
    drop = fake.fresh("config_update", {"config": {"confirmation": None}})
    await peer.send("config_update", drop)
    await asyncio.sleep(0.2)
    await peer.send("config_update", fake.fresh("config_update", {"config": {"heartbeat_seconds": 6}}))
    await asyncio.sleep(0.2)
    replay = await peer.reply_to((await peer.send("config_update", drop)).id)
    assert replay.body["code"] == "denied"
    assert runtime.channel.controller_config.get("heartbeat_seconds") == 6


def test_signed_types_names_every_kind_the_node_checks():
    source = inspect.getsource(channel_node)
    checked = set(re.findall(r'self\._signed\("([a-z_]+)"', source))
    assert checked and checked <= wire.SIGNED_TYPES
    assert {"ask_first", "workflow", "lane_control", "config_update", "revoke", "mesh_freeze", "standing"} \
        <= wire.SIGNED_TYPES


# ---- one bad message never ends the session ----

async def test_an_unexpected_error_fails_that_message_only(wired, monkeypatch):
    fake, runtime, peer = wired
    channel = runtime.channel
    sessions = channel.sessions

    def locked(_upto):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(channel.store, "trim", locked)
    for _ in range(3):  # a frame that keeps failing does not loop the session either
        sent = await peer.send("ack", {"upto_seq": 1})
        reply = await peer.reply_to(sent.id)
        assert reply.type == "error" and reply.body["code"] == "engine_error"
    await peer.send("config_update", fake.fresh("config_update", {"config": {"heartbeat_seconds": 6}}))
    await asyncio.sleep(0.2)
    assert channel.controller_config.get("heartbeat_seconds") == 6  # the same session still reads
    assert channel.sessions == sessions and channel.conn is not None


async def test_a_slow_lane_control_does_not_stall_the_reader_and_keeps_its_order(wired, monkeypatch):
    fake, runtime, peer = wired
    from agentos.node import viewer_actions

    applied = []
    real = viewer_actions.apply_action

    async def slow(lanes, lane_id, action, **kw):
        if action == "take":
            await asyncio.sleep(1.0)  # a control listener that takes its time
        applied.append(action)
        return await real(lanes, lane_id, action, **kw)

    monkeypatch.setattr(viewer_actions, "apply_action", slow)
    lane = {"lane": "personal/reddit", "by": "owner"}
    t0 = time.monotonic()
    await peer.send("lane_control", fake.fresh("lane_control", {**lane, "action": "take"}))
    await peer.send("lane_control", fake.fresh("lane_control", {**lane, "action": "hand_back"}))
    await peer.send("config_update", fake.fresh("config_update", {"config": {"heartbeat_seconds": 7}}))
    async with asyncio.timeout(0.8):
        while runtime.channel.controller_config.get("heartbeat_seconds") != 7:
            await asyncio.sleep(0.02)
    assert time.monotonic() - t0 < 0.9 and applied == []  # read and applied while take still runs
    async with asyncio.timeout(5):
        while len(applied) < 2:
            await asyncio.sleep(0.05)
    assert applied == ["take", "hand_back"]
    assert runtime.lanes.get("personal/reddit").control.state == ControlState.AGENT


async def test_a_revoke_is_applied_even_undated_but_a_dated_one_is_checked(wired):
    fake, _runtime, peer = wired
    body = {"revocation_ids": ["rv_other00000"]}
    undated = await peer.send("revoke", fake.sign("revoke", body))  # a replay could only revoke again
    await asyncio.sleep(0.2)
    assert not [e for e in peer.seen if e.reply_to == undated.id and e.type == "error"]
    stale = fake.sign("revoke", {**body, "ts": wire.iso(time.time() - 3600), "nonce": "n0nce-stale-02"})
    assert (await peer.reply_to((await peer.send("revoke", stale)).id)).body["code"] == "denied"
    good = fake.fresh("revoke", body)
    await peer.send("revoke", good)
    await asyncio.sleep(0.2)
    assert (await peer.reply_to((await peer.send("revoke", good)).id)).body["code"] == "denied"


async def test_a_nan_server_time_does_not_switch_freshness_off(wired):
    fake, runtime, peer = wired
    runtime.channel._set_clock_offset(float("nan"))
    assert runtime.channel.clock_offset == 0.0
    stale = fake.sign("lane_control", {"lane": "personal/reddit", "action": "take", "by": "owner",
                                       "ts": wire.iso(time.time() - 3600), "nonce": "n0nce-stale-03"})
    assert (await peer.reply_to((await peer.send("lane_control", stale)).id)).body["code"] == "denied"
    assert wire.NONCE_KEEP_S >= 2 * (channel_signed.SIGNED_FRESHNESS + channel_node.MAX_CLOCK_OFFSET)
    assert not wire.NONCE_RE.fullmatch("abcdefgh\n")
