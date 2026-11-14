"""Standing approvals a controller sends over the channel (node/channel/standing.py): signed and
fresh, checked with the node's own rules and bounded by the controller ceiling, consumed exactly as
a node-local one, reported back, and ended by a revoke, a freeze, the owner or an untrusted issuer."""

from __future__ import annotations

import asyncio
import json
import secrets
import time

import pytest
from channel_fake_controller import NODE_ID, FakeController, node_pubkey, node_runtime, start, stop
from test_channel_node import grant, invoke_body, wire_key
from typer.testing import CliRunner

from agentos.core.config import NodeConfig
from agentos.node.channel import standing, wire
from agentos.policy.approvals import content_digest
from agentos.policy.ceiling import ControllerCeiling
from agentos.policy.freeze import Reach, revoke_reaching

LANE = "personal/reddit"
DAY = 86400


def item(**kw) -> dict:
    base = {"id": "sa_posts1", "workspace": "personal", "effect": "commit", "lanes": [LANE], "count": 2,
            "used": 0, "until": wire.iso(time.time() + DAY), "targets": ["tool:post_note"],
            "contents": [content_digest({"text": "hi"})], "note": "two posts", "created_by": "usr_owner"}
    return {**base, **kw}


def signed(fake, **parts) -> dict:
    return fake.sign("standing", {"ts": wire.iso(), "nonce": secrets.token_hex(8), **parts})


async def send(peer, body) -> wire.Envelope:
    sent = await peer.send("standing", body)
    return await peer.reply_to(sent.id)


async def post(fake, peer, text, *, call_id=None, token=None) -> wire.Envelope:
    sent = await peer.send("invoke", invoke_body(fake, "post_note", {"on": LANE, "text": text}, call_id=call_id,
                                                 token=token))
    return await peer.reply_to(sent.id)


def standing_event(peer, **match):
    return peer.recv("event", where=lambda e: e.body["kind"] == "standing"
                     and all(e.body["payload"].get(k) == v for k, v in match.items()))


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


async def hosted(tmp_path, fake, **ceiling) -> object:
    home = tmp_path / "hosted"
    settings = {**ControllerCeiling().settings(), **ceiling}
    config = NodeConfig(home=home, name="node-h", mcp_token="node-owner-token", mcp_port=0,
                        controller_url=fake.url, node_id=fake.node_id, org_id="org_test",
                        controller_pubkey=wire.public_b64(fake.key), grant_pubkey=wire.public_b64(fake.grant_key),
                        **settings)
    return await node_runtime(home, fake, config=config)


# ---- the message itself ----

async def test_hello_says_the_node_applies_them(wired):
    fake, _, _ = wired
    assert fake.hellos[-1]["supports"] == ["standing", "ask_first", "confirmations"]


async def test_an_unsigned_wrongly_signed_or_tampered_message_changes_nothing(wired):
    fake, runtime, peer = wired
    body = {"ts": wire.iso(), "nonce": secrets.token_hex(8), "upsert": [item()]}
    assert (await send(peer, body)).body["code"] == "denied"
    assert (await send(peer, wire.sign_controller(wire_key(), "standing", NODE_ID, body))).body["code"] == "denied"
    # signed for another message type
    assert (await send(peer, fake.sign("config_update", body))).body["code"] == "denied"
    tampered = signed(fake, upsert=[item()])
    tampered["upsert"][0]["count"] = 2000
    assert (await send(peer, tampered)).body["code"] == "denied"
    assert runtime.approvals.issued_standing("org:org_test") == []


async def test_a_replayed_stale_or_undated_message_is_refused(wired):
    fake, runtime, peer = wired
    body = signed(fake, upsert=[item()])
    assert (await send(peer, body)).type == "standing_result"
    runtime.approvals.revoke_standing("sa_posts1", by="owner")
    replay = await send(peer, body)
    assert replay.type == "error" and replay.body["code"] == "denied" and "already applied" in replay.body["message"]
    stale = fake.sign("standing", {"ts": wire.iso(time.time() - 3600), "nonce": secrets.token_hex(8),
                                   "upsert": [item(id="sa_posts2")]})
    assert (await send(peer, stale)).body["code"] == "denied"
    undated = fake.sign("standing", {"nonce": secrets.token_hex(8), "upsert": [item(id="sa_posts2")]})
    assert (await send(peer, undated)).body["code"] == "invalid"
    no_nonce = fake.sign("standing", {"ts": wire.iso(), "upsert": [item(id="sa_posts2")]})
    assert (await send(peer, no_nonce)).body["code"] == "invalid"
    both = signed(fake, set=[], upsert=[item(id="sa_posts2")])
    assert (await send(peer, both)).body["code"] == "invalid"
    assert [sa.id for sa in runtime.approvals.issued_standing("org:org_test")] == ["sa_posts1"]


async def test_a_refused_body_does_not_use_up_its_nonce(wired):
    fake, _runtime, peer = wired
    nonce = secrets.token_hex(8)
    both = fake.sign("standing", {"ts": wire.iso(), "nonce": nonce, "set": [], "upsert": [item()]})
    assert (await send(peer, both)).body["code"] == "invalid"
    fixed = fake.sign("standing", {"ts": wire.iso(), "nonce": nonce, "upsert": [item()]})
    assert (await send(peer, fixed)).type == "standing_result"
    assert (await send(peer, fixed)).body["code"] == "denied"  # the replay check still comes first


# ---- the rules ----

async def test_each_item_is_checked_with_the_nodes_rules(wired):
    fake, runtime, peer = wired
    soon = wire.iso(time.time() + DAY)
    bad = {
        "sa_pay": item(id="sa_pay", effect="pay", until=soon),
        "sa_irrev": item(id="sa_irrev", effect="irreversible"),
        "sa_base": item(id="sa_base", effect="install_base"),
        **{f"sa_{e}": item(id=f"sa_{e}", effect=e, targets=[], contents=[])
           for e in ("exec", "secret", "service", "brain", "install_overlay")},
        "sa_outside": item(id="sa_outside", lanes=["work/linkedin"]),
        "sa_everywhere": item(id="sa_everywhere", lanes=["*"]),
        "sa_wildws": item(id="sa_wildws", workspace="pers*", lanes=[]),
        "sa_old": item(id="sa_old", until=wire.iso(time.time() - 60)),
        "sa_long": item(id="sa_long", until=wire.iso(time.time() + 91 * DAY)),
        "sa_untargeted": item(id="sa_untargeted", targets=[]),
        "sa_nodigest": item(id="sa_nodigest", contents=[]),
        "sa_nocount": item(id="sa_nocount", count=None),
        "sa_baddigest": item(id="sa_baddigest", contents=["md5:abc"]),
    }
    result = (await send(peer, signed(fake, upsert=[*bad.values(), item(id="Not An Id"), "x"]))).body
    codes = {r["id"]: r["code"] for r in result["refused"]}
    assert codes == {"sa_pay": "effect_not_allowed", "sa_irrev": "effect_not_allowed",
                     "sa_base": "effect_not_allowed", "sa_exec": "effect_not_allowed",
                     "sa_secret": "effect_not_allowed", "sa_service": "effect_not_allowed",
                     "sa_brain": "effect_not_allowed", "sa_install_overlay": "effect_not_allowed", "sa_outside": "outside_workspace",
                     "sa_everywhere": "outside_workspace", "sa_wildws": "invalid", "sa_old": "expired",
                     "sa_long": "too_long", "sa_untargeted": "unbound", "sa_nodigest": "unbound",
                     "sa_nocount": "invalid", "sa_baddigest": "invalid", "Not An Id": "invalid", "": "invalid"}
    assert all(r["message"] for r in result["refused"]) and result["applied"] == []
    assert runtime.approvals.issued_standing("org:org_test") == []
    # a flow version with no contents is a template, as on the node; the whole workspace is lanes: []
    ok = (await send(peer, signed(fake, upsert=[item(id="sa_flow", targets=["flow:story@v1"], contents=[],
                                                     lanes=[])]))).body
    assert ok["applied"] == [{"id": "sa_flow", "used": 0, "remaining": 2, "state": "active"}]
    sa = runtime.approvals.issued_standing("org:org_test")[0]
    assert (sa.workspaces, sa.lanes, sa.created_by, sa.issuer) == (["personal"], [], "controller:usr_owner",
                                                                   "org:org_test")


async def test_the_controller_ceiling_bounds_what_a_controller_may_pre_approve(tmp_path):
    async with FakeController(NODE_ID, node_pubkey(tmp_path / "hosted")) as fake:
        runtime = await hosted(tmp_path, fake, controller_max_effect="draft")
        await start(runtime)
        try:
            peer = await fake.peer()
            result = (await send(peer, signed(fake, upsert=[item()]))).body
            assert {r["id"]: r["code"] for r in result["refused"]} == {"sa_posts1": "controller_ceiling"}
        finally:
            await stop(runtime)


async def test_the_owners_limits_bound_what_a_controller_can_hold(tmp_path):
    async with FakeController(NODE_ID, node_pubkey(tmp_path / "hosted")) as fake:
        runtime = await hosted(tmp_path, fake, controller_standing_max_count=3, controller_standing_max_active=5,
                               controller_standing_max_days=7)
        await start(runtime)
        try:
            peer = await fake.peer()
            work = {"workspace": "work", "lanes": ["work/linkedin"]}
            result = (await send(peer, signed(fake, upsert=[
                item(id="sa_many", count=4), item(id="sa_long", until=wire.iso(time.time() + 8 * DAY)),
                item(count=3), item(id="sa_more", count=3, contents=[content_digest({"text": "yo"})]),
                item(id="sa_work", count=3, **work)]))).body
            assert {r["id"]: r["code"] for r in result["refused"]} == {
                "sa_many": "over_limit", "sa_long": "too_long", "sa_more": "over_limit"}
            assert sorted(a["id"] for a in result["applied"]) == ["sa_posts1", "sa_work"]
            # one use spent leaves room for two more in the workspace, and a refill past it is refused
            assert (await post(fake, peer, "hi")).body["ok"]
            room = (await send(peer, signed(fake, upsert=[item(id="sa_more", count=3, used=1,
                                                               contents=[content_digest({"text": "yo"})])]))).body
            assert room["applied"][0]["remaining"] == 2
            third = (await send(peer, signed(fake, upsert=[item(count=3, used=0),  # used 0 never refills it
                                                           item(id="sa_third", count=2)]))).body
            assert third["applied"][0]["remaining"] == 2
            assert [(r["id"], r["code"]) for r in third["refused"]] == [("sa_third", "over_limit")]
            longer = (await send(peer, signed(fake, upsert=[item(count=3, until=wire.iso(time.time() + 8 * DAY))])))
            assert longer.body["refused"][0]["code"] == "too_long" and longer.body["withdrawn"] == ["sa_posts1"]
        finally:
            await stop(runtime)


async def test_a_lowered_ceiling_stops_one_already_held(wired):
    fake, runtime, peer = wired
    await send(peer, signed(fake, upsert=[item()]))
    policy = runtime.get("policy")
    policy.node_config = policy.node_config.model_copy(update={"controller_max_effect": "draft"})
    # the controller's own call is clamped first; a node-local agent's call is not covered either
    assert "controller_ceiling" in (await post(fake, peer, "hi")).body["error"]["message"]
    assert policy.approvals.consume_standing("commit", LANE, [], tool="post_note", content={"text": "hi"}) is None
    assert runtime.approvals.issued_standing("org:org_test")[0].uses == 0


# ---- consumption ----

async def test_a_matching_call_consumes_a_use_bound_to_the_content_and_reports_it(wired):
    fake, _runtime, peer = wired
    result = (await send(peer, signed(fake, upsert=[item()]))).body
    assert result == {"applied": [{"id": "sa_posts1", "used": 0, "remaining": 2, "state": "active"}],
                      "refused": [], "revoked": [], "withdrawn": []}
    done = await post(fake, peer, "hi")
    assert done.body["ok"], done.body
    assert json.loads(done.body["content"][0]["text"]) == {"posted": "hi", "on": LANE}
    event = await standing_event(peer, id="sa_posts1")
    assert event.body["payload"] == {"id": "sa_posts1", "used": 1, "remaining": 1, "state": "active"}
    assert event.seq  # outboxed, so it survives a lost connection
    # another post text is not what the owner saw
    other = await post(fake, peer, "buy my stuff")
    assert other.body["error"]["code"] == "approval_pending"
    assert (await post(fake, peer, "hi")).body["ok"]
    assert (await standing_event(peer, used=2)).body["payload"]["state"] == "used_up"
    assert (await post(fake, peer, "hi")).body["error"]["code"] == "approval_pending"


async def test_a_node_local_one_is_not_reported_to_the_controller(wired):
    fake, runtime, peer = wired
    runtime.approvals.add_standing(effects=["commit"], lanes=[LANE], until=time.time() + DAY, count=3, by="owner",
                                   targets=["tool:post_note"], contents=[content_digest({"text": "hi"})])
    assert (await post(fake, peer, "hi")).body["ok"]
    await asyncio.sleep(0.3)
    assert not [e for e in peer.seen if e.type == "event" and e.body["kind"] == "standing"]


async def test_the_count_survives_a_reconnect_whatever_the_controller_counted(wired):
    fake, _runtime, peer = wired
    await send(peer, signed(fake, upsert=[item(count=1)]))
    assert (await post(fake, peer, "hi")).body["ok"]
    await standing_event(peer, state="used_up")
    # the controller missed the event and sends the set again in the next welcome, used: 0
    fake.welcome_extra = {"standing": signed(fake, set=[item(count=1, used=0)])}
    await peer.ws.close()
    second = await fake.peer()
    reply = await second.recv("standing_result")
    assert reply.body["applied"] == [{"id": "sa_posts1", "used": 1, "remaining": 0, "state": "used_up"}]
    assert (await post(fake, second, "hi")).body["error"]["code"] == "approval_pending"
    # the controller raises the count: one more use
    await send(second, signed(fake, upsert=[item(count=2)]))
    assert (await post(fake, second, "hi")).body["ok"]


async def test_one_its_controller_stops_confirming_covers_nothing_until_it_resends_the_set(wired):
    fake, runtime, peer = wired
    runtime.approvals.issued_confirm_s = 1.0  # controller_standing_confirm_hours, made short
    await send(peer, signed(fake, upsert=[item(count=9)]))
    assert (await post(fake, peer, "hi")).body["ok"]
    await asyncio.sleep(1.1)
    # an upsert confirms only what it carries, and only a whole set lets a live session confirm the rest
    runtime.channel._confirm_standing()
    assert (await post(fake, peer, "hi")).body["error"]["code"] == "approval_pending"
    await send(peer, signed(fake, set=[item(count=9)]))
    assert (await post(fake, peer, "hi")).body["ok"]
    await asyncio.sleep(1.1)
    runtime.channel._confirm_standing()  # what housekeeping does while the controller stays connected
    assert (await post(fake, peer, "hi")).body["ok"]
    # offline, nothing confirms it
    await peer.ws.close()
    await asyncio.sleep(1.1)
    runtime.channel._confirm_standing()
    assert runtime.get("policy").approvals.consume_standing("commit", LANE, [], tool="post_note",
                                                            content={"text": "hi"}) is None
    assert runtime.approvals.issued_standing("org:org_test")[0].state(time.time()) == "active"


async def test_a_refused_welcome_set_is_answered_with_an_error(wired):
    fake, runtime, peer = wired
    fake.welcome_extra = {"standing": wire.sign_controller(wire_key(), "standing", NODE_ID,
                                                           {"ts": wire.iso(), "nonce": "n" * 12, "set": [item()]})}
    await peer.ws.close()
    second = await fake.peer()
    refused = await second.recv("error", where=lambda e: e.body["code"] == "denied")
    assert refused.reply_to
    assert runtime.approvals.issued_standing("org:org_test") == []


# ---- set, withdraw and revoke ----

async def test_a_set_withdraws_what_it_leaves_out_and_brings_it_back_with_its_uses(wired):
    fake, _runtime, peer = wired
    await send(peer, signed(fake, set=[item(), item(id="sa_other", contents=[content_digest({"text": "yo"})])]))
    assert (await post(fake, peer, "yo")).body["ok"]
    result = (await send(peer, signed(fake, set=[item()]))).body
    assert result["withdrawn"] == ["sa_other"]
    assert (await standing_event(peer, id="sa_other", state="withdrawn")).body["payload"]["used"] == 1
    assert (await post(fake, peer, "yo")).body["error"]["code"] == "approval_pending"
    back = (await send(peer, signed(fake, set=[item(), item(id="sa_other", contents=[content_digest({"text": "yo"})])])))
    assert {"id": "sa_other", "used": 1, "remaining": 1, "state": "active"} in back.body["applied"]
    assert (await post(fake, peer, "yo")).body["ok"]


async def test_a_refused_update_withdraws_the_version_held(wired):
    fake, runtime, peer = wired
    two = {"id": "sa_two", "contents": [content_digest({"text": "yo"})]}
    await send(peer, signed(fake, upsert=[item(count=5), item(**two, count=5)]))
    zero = (await send(peer, signed(fake, upsert=[item(count=0)]))).body
    assert [r["code"] for r in zero["refused"]] == ["invalid"] and zero["withdrawn"] == ["sa_posts1"]
    assert (await post(fake, peer, "hi")).body["error"]["code"] == "approval_pending"
    past = (await send(peer, signed(fake, set=[item(), item(**two, until=wire.iso(time.time() - 60))]))).body
    assert [(r["id"], r["code"]) for r in past["refused"]] == [("sa_two", "expired")]
    assert past["withdrawn"] == ["sa_two"] and past["applied"][0]["id"] == "sa_posts1"
    held = {sa.id: sa.state(time.time()) for sa in runtime.approvals.issued_standing("org:org_test")}
    assert held == {"sa_posts1": "active", "sa_two": "withdrawn"}
    # an id this controller does not hold is only refused
    assert (await send(peer, signed(fake, upsert=[item(id="sa_new", count=0)]))).body["withdrawn"] == []


async def test_a_revoke_always_applies_and_an_id_never_comes_back(wired):
    fake, runtime, peer = wired
    await send(peer, signed(fake, upsert=[item()]))
    # revoked before it ever arrived
    early = (await send(peer, signed(fake, revoke=["sa_later"]))).body
    assert early["revoked"] == ["sa_later"]
    late = (await send(peer, signed(fake, upsert=[item(id="sa_later")]))).body
    assert late["refused"][0]["code"] == "revoked"
    # revoked after it was applied, idempotently
    gone = (await send(peer, signed(fake, revoke=["sa_posts1"]))).body
    assert gone["revoked"] == ["sa_posts1"]
    assert (await standing_event(peer, id="sa_posts1")).body["payload"]["state"] == "revoked"
    assert (await send(peer, signed(fake, revoke=["sa_posts1"]))).body["revoked"] == ["sa_posts1"]
    assert (await post(fake, peer, "hi")).body["error"]["code"] == "approval_pending"
    assert (await send(peer, signed(fake, set=[item()]))).body["refused"][0]["code"] == "revoked"
    # the node's own are not the controller's to revoke or replace
    own = runtime.approvals.add_standing(effects=["commit"], lanes=[LANE], until=time.time() + DAY, count=1,
                                         by="owner", targets=["flow:story@v1"])
    mine = (await send(peer, signed(fake, revoke=[own.id], upsert=[item(id=own.id)]))).body
    assert [r["code"] for r in mine["refused"]] == ["conflict", "conflict"]
    assert runtime.approvals.list_standing()[0].revoked_at is None


async def test_an_id_revoked_before_it_arrived_stays_revoked_after_many_more(wired, monkeypatch):
    fake, runtime, peer = wired
    monkeypatch.setattr(standing, "MAX_TOMBSTONES", 2, raising=False)  # the old cap, made small
    await send(peer, signed(fake, revoke=["sa_early"]))
    await send(peer, signed(fake, revoke=["sa_a", "sa_b", "sa_c"]))
    late = (await send(peer, signed(fake, set=[item(id="sa_early")]))).body
    assert late["refused"][0]["code"] == "revoked" and late["applied"] == []
    assert (await post(fake, peer, "hi")).body["error"]["code"] == "approval_pending"
    # kept as revoked rows of its issuer, which cover nothing
    rows = {sa.id: sa for sa in runtime.approvals.issued_standing("org:org_test")}
    assert set(rows) == {"sa_early", "sa_a", "sa_b", "sa_c"} and all(sa.revoked_at for sa in rows.values())


async def test_a_revoke_while_a_call_waits_leaves_it_for_a_person(wired):
    fake, runtime, peer = wired
    waiting = await post(fake, peer, "later", call_id="call_wait")
    assert waiting.body["error"]["code"] == "approval_pending"
    covers = item(contents=[content_digest({"text": "later"})])
    await send(peer, signed(fake, upsert=[covers]))
    await send(peer, signed(fake, revoke=["sa_posts1"]))
    again = await post(fake, peer, "later", call_id="call_wait")
    assert again.body["error"]["code"] == "approval_pending"
    assert runtime.approvals.issued_standing("org:org_test")[0].uses == 0


async def test_the_owner_revokes_one_locally_and_sees_where_it_came_from(wired, monkeypatch):
    fake, runtime, peer = wired
    await send(peer, signed(fake, upsert=[item()]))
    monkeypatch.setenv("AGENTOS_HOME", str(runtime.get("config").home))
    from agentos.policy.cli import app

    listed = CliRunner().invoke(app, ["approvals", "standing", "list"])
    assert listed.exit_code == 0 and "sa_posts1" in listed.stdout and "from org:org_test" in listed.stdout
    revoked = CliRunner().invoke(app, ["approvals", "standing", "revoke", "sa_posts1"])
    assert revoked.exit_code == 0, revoked.output
    runtime.channel.check_standing()  # what housekeeping does: the CLI is another process
    assert (await standing_event(peer, id="sa_posts1")).body["payload"]["state"] == "revoked"
    runtime.channel.check_standing()
    await asyncio.sleep(0.2)
    assert len([e for e in peer.seen if e.type == "event" and e.body["kind"] == "standing"]) == 1
    assert (await post(fake, peer, "hi")).body["error"]["code"] == "approval_pending"
    assert (await send(peer, signed(fake, upsert=[item()]))).body["refused"][0]["code"] == "revoked"


# ---- freezes and the issuer ----

async def test_a_freeze_revokes_them_like_the_nodes_own_and_nothing_comes_back(wired):
    fake, runtime, peer = wired
    await send(peer, signed(fake, upsert=[item()]))
    out = revoke_reaching(runtime.get("policy"), Reach(everything=True), by="freeze", reason="mesh freeze")
    assert out["standing"] == ["sa_posts1"]
    assert (await standing_event(peer, id="sa_posts1")).body["payload"]["state"] == "revoked"
    assert (await send(peer, signed(fake, set=[item()]))).body["refused"][0]["code"] == "revoked"


async def test_a_lane_freeze_revokes_a_withdrawn_one_too(wired):
    fake, runtime, peer = wired
    await send(peer, signed(fake, set=[item()]))
    await send(peer, signed(fake, set=[]))
    out = revoke_reaching(runtime.get("policy"), Reach(lanes=[LANE]), by="freeze", reason="lane freeze")
    assert out["standing"] == ["sa_posts1"]
    assert (await send(peer, signed(fake, set=[item()]))).body["refused"][0]["code"] == "revoked"


async def test_one_from_an_issuer_the_node_no_longer_trusts_covers_nothing(wired):
    fake, runtime, peer = wired
    await send(peer, signed(fake, upsert=[item(count=5)]))
    token = grant(fake)[0]
    assert (await post(fake, peer, "hi", token=token)).body["ok"]
    issuers = runtime.get("grant_issuers")
    kid = issuers.keys("org:org_test")[0]["kid"]
    issuers.retire("org:org_test", kid)
    assert not issuers.live("org:org_test")
    # the grant itself is refused now too; a node-local agent's call is not covered either
    policy = runtime.get("policy")
    assert policy.approvals.consume_standing("commit", LANE, [], tool="post_note", content={"text": "hi"}) is None
    assert runtime.approvals.issued_standing("org:org_test")[0].uses == 1


# ---- the parts ----

def test_event_payload_states():
    now = time.time()
    base = {"id": "sa_x", "uses": 1, "remaining": 1, "expires_at": wire.iso(now + 60), "revoked_at": None,
            "withdrawn_at": None}
    assert standing.event_payload(base, now)["state"] == "active"
    assert standing.event_payload({**base, "remaining": 0}, now)["state"] == "used_up"
    assert standing.event_payload({**base, "withdrawn_at": wire.iso(now)}, now)["state"] == "withdrawn"
    assert standing.event_payload({**base, "expires_at": wire.iso(now - 1)}, now)["state"] == "expired"
    assert standing.event_payload({**base, "revoked_at": wire.iso(now)}, now)["state"] == "revoked"
    assert standing.event_payload({"id": "sa_x"}, now) is None


def test_the_issuer_follows_the_org():
    assert standing.org_issuer(NodeConfig(org_id="org_a")) == "org:org_a"
    assert standing.org_issuer(NodeConfig()) == "org:controller"


def test_the_limits_are_read_from_node_toml_and_checked():
    assert standing.Limits.from_config(NodeConfig()) == standing.Limits(50, 200, 30)
    assert NodeConfig().controller_standing_confirm_hours == 24
    for bad in ({"controller_standing_max_days": 91}, {"controller_standing_max_count": 0},
                {"controller_standing_confirm_hours": 0},
                {"controller_standing_max_count": 300, "controller_standing_max_active": 200}):
        with pytest.raises(ValueError):
            NodeConfig(**bad)
