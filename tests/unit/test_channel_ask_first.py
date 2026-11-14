""""Ask me first" over the channel (node/channel/ask_first.py, docs/PROTOCOL.md): a controller sets a
routine's or an account's switch with a signed, fresh `ask_first`; the node answers
`ask_first_result`, a commit on that account then waits, and the inventory reports the switches."""

from __future__ import annotations

import asyncio
import secrets
import time
from types import SimpleNamespace

import pytest
from channel_fake_controller import NODE_ID, FakeController, node_pubkey, node_runtime, start, stop
from channel_support import start_controller, wait_for
from protocol_capture import _start_node
from test_channel_node import invoke_body, wire_key
from test_channel_standing import hosted

from agentos.core.errors import AgentosError
from agentos.core.runtime import Runtime
from agentos.node.channel import reports, wire
from agentos.runner.fakes import post_flow
from agentos.runner.models import FlowDefinition

LANE = "personal/reddit"


def post_definition() -> FlowDefinition:
    return FlowDefinition.model_validate(post_flow())


def signed(fake, changes, **extra) -> dict:
    return fake.sign("ask_first", {"ts": wire.iso(), "nonce": secrets.token_hex(8), "changes": changes, **extra})


async def send(peer, body) -> wire.Envelope:
    sent = await peer.send("ask_first", body)
    return await peer.reply_to(sent.id)


async def post(fake, peer, text) -> wire.Envelope:
    sent = await peer.send("invoke", invoke_body(fake, "post_note", {"on": LANE, "text": text}))
    return await peer.reply_to(sent.id)


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


async def test_hello_says_the_node_applies_them(wired):
    fake, _, _ = wired
    assert "ask_first" in fake.hellos[-1]["supports"]


async def test_round_trip_turns_an_account_on_then_off(wired):
    fake, runtime, peer = wired
    reply = await send(peer, signed(fake, [{"scope": "lane", "id": LANE, "ask_first": True, "by": "usr_owner"}]))
    assert reply.type == "ask_first_result", reply.body
    [applied] = reply.body["applied"]
    assert applied["scope"] == "lane" and applied["id"] == LANE and applied["ask_first"] is True
    assert applied["set_by"] == "controller:usr_owner" and applied["by_person"] is True
    assert reply.body["refused"] == []
    waiting = await post(fake, peer, "hello")
    assert waiting.body["ok"] is False and waiting.body["error"]["code"] == "approval_pending"
    approval = runtime.get("approvals").get(waiting.body["error"]["approval_id"])
    assert approval.reason == f"Ask me first is on for account {LANE}"

    inv = await reports.inventory(runtime)
    assert inv["ask_first"] == [applied]

    off = await send(peer, signed(fake, [{"scope": "lane", "id": LANE, "ask_first": False, "by": "usr_owner"}]))
    assert off.body["applied"][0]["ask_first"] is False
    done = await post(fake, peer, "on its own")
    assert done.body["ok"] is True, done.body


async def test_a_flow_switch_and_the_last_change_wins(wired):
    fake, runtime, peer = wired
    reply = await send(peer, signed(fake, [{"scope": "flow", "id": "personal.reddit.post", "ask_first": False},
                                           {"scope": "flow", "id": "personal.reddit.post", "ask_first": True},
                                           {"scope": "flow", "id": "Not An Id", "ask_first": True},
                                           {"scope": "node", "id": "x", "ask_first": True},
                                           {"scope": "lane", "id": LANE, "ask_first": "yes"}]))
    assert [a["ask_first"] for a in reply.body["applied"]] == [True]
    assert [r["code"] for r in reply.body["refused"]] == ["invalid", "invalid", "invalid"]
    assert runtime.get("ask_first").is_on("flow", "personal.reddit.post")


async def test_an_unsigned_tampered_stale_or_replayed_message_changes_nothing(wired):
    fake, runtime, peer = wired
    changes = [{"scope": "lane", "id": LANE, "ask_first": True}]
    body = {"ts": wire.iso(), "nonce": secrets.token_hex(8), "changes": changes}
    assert (await send(peer, body)).body["code"] == "denied"
    assert (await send(peer, wire.sign_controller(wire_key(), "ask_first", NODE_ID, body))).body["code"] == "denied"
    assert (await send(peer, fake.sign("standing", body))).body["code"] == "denied"  # signed for another type
    tampered = signed(fake, changes)
    tampered["changes"][0]["id"] = "personal/other"
    assert (await send(peer, tampered)).body["code"] == "denied"
    stale = fake.sign("ask_first", {"ts": "2020-01-01T00:00:00Z", "nonce": secrets.token_hex(8), "changes": changes})
    assert (await send(peer, stale)).body["code"] == "denied"
    no_nonce = fake.sign("ask_first", {"ts": wire.iso(), "changes": changes})
    assert (await send(peer, no_nonce)).body["code"] == "invalid"
    no_changes = fake.sign("ask_first", {"ts": wire.iso(), "nonce": secrets.token_hex(8)})
    assert (await send(peer, no_changes)).body["code"] == "invalid"
    assert runtime.get("ask_first").list() == []
    once = signed(fake, changes)
    assert (await send(peer, once)).type == "ask_first_result"
    assert (await send(peer, once)).body["code"] == "denied"  # the nonce was seen


async def test_a_replayed_off_cannot_undo_a_later_on(wired):
    fake, runtime, peer = wired
    off = signed(fake, [{"scope": "lane", "id": LANE, "ask_first": False, "by": "usr_owner"}])
    assert (await send(peer, off)).type == "ask_first_result"
    await send(peer, signed(fake, [{"scope": "lane", "id": LANE, "ask_first": True, "by": "usr_owner"}]))
    replay = await send(peer, off)
    assert replay.type == "error" and replay.body["code"] == "denied"
    assert runtime.get("ask_first").is_on("lane", LANE)
    future = wire.iso(time.time() + 3600)
    ahead = fake.sign("ask_first", {"ts": future, "nonce": secrets.token_hex(8),
                                    "changes": [{"scope": "lane", "id": LANE, "ask_first": False}]})
    assert (await send(peer, ahead)).body["code"] == "denied"
    assert runtime.get("ask_first").is_on("lane", LANE)


async def test_by_keeps_80_characters(wired):
    fake, _, peer = wired
    reply = await send(peer, signed(fake, [{"scope": "lane", "id": LANE, "ask_first": True, "by": "a" * 200}]))
    assert reply.body["applied"][0]["set_by"] == "controller:" + "a" * 80
    bare = await send(peer, signed(fake, [{"scope": "lane", "id": LANE, "ask_first": True}]))
    assert bare.body["applied"][0]["set_by"] == "controller"


async def test_a_node_without_the_switches_answers_unsupported_and_keeps_working(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        runtime.get("channel")._ask_first = lambda: None
        await start(runtime)
        try:
            peer = await fake.peer()
            assert fake.hellos[-1]["supports"] == ["standing", "confirmations"]
            reply = await send(peer, signed(fake, [{"scope": "lane", "id": LANE, "ask_first": True}]))
            assert reply.type == "error" and reply.body["code"] == "unsupported"
            assert runtime.get("ask_first").list() == []
            assert (await post(fake, peer, "still works")).body["ok"] is True
        finally:
            await stop(runtime)


async def test_a_real_controller_sets_a_switch_and_an_older_node_answers_unsupported(tmp_path):
    ctl = await start_controller(tmp_path / "controller")
    node = await _start_node(tmp_path / "node", "node-a", ctl, [LANE])
    hub = ctl.controller.hub
    node_id = node.config.node_id
    try:
        await wait_for(lambda: hub.sessions)
        result = await hub.push_ask_first(ctl.org, node_id, [{"scope": "lane", "id": LANE, "ask_first": True,
                                                              "by": "usr_owner"}])
        assert [s["set_by"] for s in result["applied"]] == ["controller:usr_owner"] and result["refused"] == []
        assert node.runtime.get("ask_first").is_on("lane", LANE)
        handlers = node.channel._handlers  # a node from before this message: the type is unknown to it
        node.channel._handlers = lambda: {k: v for k, v in handlers().items() if k != "ask_first"}
        with pytest.raises(AgentosError) as refused:
            await hub.push_ask_first(ctl.org, node_id, [{"scope": "lane", "id": LANE, "ask_first": False}])
        assert refused.value.code == "unsupported"
        assert node.runtime.get("ask_first").is_on("lane", LANE)
    finally:
        await node.close()
        await ctl.close()


async def test_an_older_controller_can_ignore_all_of_it():
    """Inventory stays additive: a node with no switches sends no section, and a flow reads off."""
    flows = SimpleNamespace(list=list)
    assert await reports.inventory(Runtime(flows=flows)) == {"flows": []}
    fv = SimpleNamespace(definition=post_definition(), version="v1", id="biz-a.social.post", effect="commit")
    flow = reports._flow(SimpleNamespace(tool_names=dict), fv)
    assert flow["ask_first"] is False and flow["ask_first_by"] is None


async def test_a_controller_below_commit_turns_switches_on_but_never_off(tmp_path):
    async with FakeController(NODE_ID, node_pubkey(tmp_path / "hosted")) as fake:
        runtime = await hosted(tmp_path, fake, controller_max_effect="draft")
        await start(runtime)
        try:
            peer = await fake.peer()
            reply = await send(peer, signed(fake, [{"scope": "lane", "id": LANE, "ask_first": True},
                                                   {"scope": "flow", "id": "personal.reddit.post",
                                                    "ask_first": False}]))
            assert [a["id"] for a in reply.body["applied"]] == [LANE]
            assert [r["code"] for r in reply.body["refused"]] == ["controller_ceiling"]
        finally:
            await stop(runtime)
