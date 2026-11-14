"""QA for the node channel (docs/PROTOCOL.md v1) against the scripted controller in channel_fake_controller:
clock skew, restarts in the middle of a call or an approval, an oversized tool list, a welcome for
another node, the controller going away during a call, and the controller's config range.

Bugs are strict xfails pinned to the reason they fail for today; remove the marker once fixed.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from channel_fake_controller import NODE_ID, FakeController, node_pubkey, node_runtime, start, stop

from agentos.node.channel import approvals, bridge, calls, connection, outbox, signed, wire
from agentos.node.channel import node as node_module
from agentos.policy import tokens
from agentos.policy.grants import GrantBlock, Specials
from agentos.policy.util import new_id


def mint_grant(fake, *, exp: int) -> str:
    block = {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "iat": int(time.time()),
             "sub": {"kind": "agent", "id": "tester", "owner": "owner"}, "workspaces": ["*"],
             "ceiling": "irreversible", "specials": Specials.everything().model_dump(mode="json", exclude_none=True),
             "exp": exp, "depth": 3}
    return tokens.mint(fake.grant_key, GrantBlock.model_validate(block).to_data())


def invoke(fake, tool: str, args: dict, *, call_id: str, deadline: str | None = None, exp: int | None = None) -> dict:
    return {"call_id": call_id, "tool": tool, "args": args,
            "principal": {"kind": "agent", "id": "tester", "thread": "t1"},
            "grant_token": mint_grant(fake, exp=exp or int(time.time()) + 600),
            "deadline": deadline or wire.iso(time.time() + 30)}


async def approve_post_note(peer, body: dict) -> str:
    """Start a call that needs approval, decide it (with the hash the node sent), return the approval id."""
    pending = await peer.reply_to((await peer.send("invoke", body)).id)
    approval_id = pending.body["error"]["approval_id"]
    approval = (await peer.recv("approval_request")).body["approval"]
    await peer.send("approval_decision", {"approval_id": approval_id, "decision": "approved",
                                          "content_hash": approval["content_hash"], "decided_by": "owner"})
    await peer.recv("event", where=lambda e: e.body["kind"] == "approval" and e.body["payload"]["state"] == "approved")
    return approval_id


# ---- passing: behavior that holds ----

async def test_an_approval_survives_a_node_restart(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        await start(runtime)
        peer = await fake.peer()
        body = invoke(fake, "post_note", {"on": "personal/reddit", "text": "hi"}, call_id="call_survive")
        approval_id = await approve_post_note(peer, body)
        await stop(runtime)
        restarted = await node_runtime(home, fake)
        await start(restarted)
        try:
            assert restarted.get("policy").approvals.get(approval_id).state == "approved"
        finally:
            await stop(restarted)


async def test_a_call_that_finishes_while_the_controller_is_away_is_answered_from_the_cache(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        await start(runtime)
        counter = {"n": 0}
        spec = runtime.mcp.specs["slow_tool"]
        original = spec.handler

        async def counted(ctx, *, seconds: float = 5.0):
            counter["n"] += 1
            return await original(ctx, seconds=seconds)

        spec.handler = counted
        try:
            peer = await fake.peer()
            body = invoke(fake, "slow_tool", {"seconds": 0.4}, call_id="call_away")
            await peer.send("invoke", body)
            fake.refuse_all = True  # the controller goes away while the call runs
            await peer.ws.close()
            await asyncio.sleep(0.8)  # the call finishes with no connection to answer on
            fake.refuse_all = False
            peer = await fake.peer()
            answer = await peer.reply_to((await peer.send("invoke", body)).id)
            assert answer.body["ok"] and counter["n"] == 1
        finally:
            await stop(runtime)


async def test_a_welcome_for_another_node_is_not_a_session(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        fake.welcome_extra = {"node_id": "node_01OTHERNODE000000000000000"}
        runtime = await node_runtime(home, fake)
        await start(runtime)
        try:
            channel = runtime.get("channel")
            await asyncio.sleep(0.4)
            assert not channel.welcomed.is_set() and not channel.connected
            assert "another node" in (channel.last_error or "")
        finally:
            await stop(runtime)


async def test_a_heartbeat_setting_in_range_is_applied_and_out_of_range_is_never_applied(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        channel = runtime.get("channel")
        channel._set_config({"heartbeat_seconds": 30}, replace=False)
        assert channel.controller_config == {"heartbeat_seconds": 30}
        channel._set_config({"config_unknown": 1}, replace=False)  # keys it does not know are kept
        assert channel.controller_config == {"heartbeat_seconds": 30, "config_unknown": 1}
        channel.controller_config = {}
        channel._set_config({"heartbeat_seconds": True}, replace=False)  # a bool is not a number
        channel._set_config({"heartbeat_seconds": 1}, replace=False)  # below the 5 second floor
        assert "heartbeat_seconds" not in channel.controller_config


# ---- bugs ----

async def test_a_node_with_a_fast_clock_still_runs_a_call_the_controller_has_time_for(tmp_path, monkeypatch):
    home = tmp_path / "node"
    real_time = time.time
    skew = 600.0

    def fast() -> float:
        return real_time() + skew

    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        fast_clock = SimpleNamespace(time=fast, monotonic=time.monotonic)
        for module in (node_module, connection, signed, approvals, outbox, calls):  # NodeChannel and its mixins
            monkeypatch.setattr(module, "time", fast_clock)
        runtime.get("policy").clock = fast  # the node's whole clock runs fast, policy included
        await start(runtime)
        try:
            peer = await fake.peer()
            # the controller's own numbers: a call due in 60 s, under a grant minted for it (deadline + 120 s)
            deadline = wire.iso(real_time() + 60)
            body = invoke(fake, "whoami", {}, call_id="call_skew", deadline=deadline,
                          exp=int(real_time()) + 60 + 120)
            answer = await peer.reply_to((await peer.send("invoke", body)).id)
            assert answer.body["ok"], answer.body
        finally:
            await stop(runtime)


async def test_a_retry_after_a_restart_runs_the_approved_call_once(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        await start(runtime)
        peer = await fake.peer()
        body = invoke(fake, "post_note", {"on": "personal/reddit", "text": "hi"}, call_id="call_restart")
        await approve_post_note(peer, body)
        await stop(runtime)
        restarted = await node_runtime(home, fake)
        await start(restarted)
        try:
            peer = await fake.peer()
            final = await peer.reply_to((await peer.send("invoke", body)).id)
            assert final.body["ok"], final.body
            assert json.loads(final.body["content"][0]["text"]) == {"posted": "hi", "on": "personal/reddit"}
        finally:
            await stop(restarted)


async def test_a_tool_list_over_one_mib_still_reaches_the_controller(tmp_path, monkeypatch):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        await start(runtime)
        try:
            channel = runtime.get("channel")
            wide = [{"name": f"wide_tool_{i}", "description": "d" * 4000,
                     "input_schema": {"type": "object", "properties": {}}, "annotations": None,
                     "effect": "read", "lane_arg": None, "tags": []} for i in range(300)]

            async def tool_defs(server):
                return wide

            monkeypatch.setattr(bridge, "tool_defs", tool_defs)
            channel.store.trim(channel.store.high_seq)
            await channel._on_online(None)
            frames = [json.loads(frame) for _, frame in channel.store.outbox_after(0, limit=1000)]
            events = [f["body"] for f in frames if f["type"] == "event"]
            tools = [e for e in events if e["kind"] == "node" and "tools" in e["payload"]]
            assert tools and len(tools[-1]["payload"]["tools"]) > 0
        finally:
            await stop(runtime)


async def test_a_refused_heartbeat_update_leaves_the_setting_in_force(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        channel = runtime.get("channel")
        channel._set_config({"heartbeat_seconds": 30}, replace=False)
        channel._set_config({"heartbeat_seconds": 1}, replace=False)
        assert channel.controller_config.get("heartbeat_seconds") == 30


# ---- judgment items QA left open ----

async def test_a_node_whose_channel_db_was_lost_announces_a_new_epoch(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        await start(runtime)
        await fake.peer()
        first = runtime.get("channel").store.epoch
        await stop(runtime)
        for name in ("channel.db", "channel.db-wal", "channel.db-shm"):
            (home / name).unlink(missing_ok=True)  # the file is lost; the node's identity is not
        restarted = await node_runtime(home, fake)
        await start(restarted)
        try:
            await fake.peer()
            assert fake.hellos[0]["epoch"] == first
            assert fake.hellos[-1]["epoch"] == restarted.get("channel").store.epoch != first
        finally:
            await stop(restarted)


async def test_a_signed_message_dated_too_far_from_the_controllers_now_is_refused(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        await start(runtime)
        try:
            peer = await fake.peer()
            channel = runtime.get("channel")
            stale = fake.sign("config_update", {"config": {"heartbeat_seconds": 7},
                                                "ts": wire.iso(time.time() - 3600)})
            sent = await peer.send("config_update", stale)
            assert (await peer.reply_to(sent.id)).body["code"] == "denied"
            assert channel.controller_config.get("heartbeat_seconds") != 7
            await peer.send("config_update", fake.fresh("config_update", {"config": {"heartbeat_seconds": 8},
                                                                         "ts": wire.iso()}))
            await asyncio.sleep(0.2)
            assert channel.controller_config.get("heartbeat_seconds") == 8
        finally:
            await stop(runtime)


async def test_the_clock_offset_is_bounded_and_small_ones_are_noise(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        channel = (await node_runtime(home, fake)).get("channel")
        channel._set_clock_offset(1.2)
        assert channel.clock_offset == 0.0
        channel._set_clock_offset(-600.0)
        assert channel.clock_offset == -600.0
        channel._set_clock_offset(10 * 86400.0)
        assert channel.clock_offset == node_module.MAX_CLOCK_OFFSET


async def test_a_retry_after_a_restart_with_other_arguments_does_not_run(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        await start(runtime)
        peer = await fake.peer()
        body = invoke(fake, "post_note", {"on": "personal/reddit", "text": "hi"}, call_id="call_swap")
        await approve_post_note(peer, body)
        await stop(runtime)
        restarted = await node_runtime(home, fake)
        await start(restarted)
        try:
            peer = await fake.peer()
            swapped = {**body, "args": {"on": "personal/reddit", "text": "something else"}}
            answer = await peer.reply_to((await peer.send("invoke", swapped)).id)
            assert not answer.body["ok"] and answer.body["error"]["code"] == "approval_pending"
        finally:
            await stop(restarted)
