"""Confirmations over the node channel: a controller's word counts up to the owner's limit, and a
node holds the floors whatever a controller sends (docs/CONFIRMATIONS.md, The node channel)."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
from channel_fake_controller import NODE_ID, FakeController, node_pubkey, node_runtime, start, stop
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_confirm_methods import FakeWatch

from openajar.core import signing
from openajar.core.config import NodeConfig
from openajar.core.models import Principal
from openajar.policy import confirm
from openajar.policy.confirm import ConfirmLevel, MethodRegistry, Proof, controller_proof, default_registry
from openajar.policy.errors import MethodUnavailable

FULL = {"controller_max_effect": "irreversible"}


def hosted_config(home, fake):
    return NodeConfig(home=home, name="node-t", mcp_token="node-owner-token", mcp_port=0, controller_url=fake.url,
                      node_id=fake.node_id, org_id="org_test", controller_pubkey=signing.public_b64(fake.key),
                      grant_pubkey=signing.public_b64(fake.grant_key), **FULL)


@pytest.fixture
async def hosted(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake, config=hosted_config(home, fake))
        await start(runtime)
        peer = await fake.peer()
        await asyncio.wait_for(runtime.channel.welcomed.wait(), 15)
        yield fake, runtime, peer
        await stop(runtime)


def action(runtime, effect="commit", digit="1", **kw):
    return runtime.get("approvals").create_action(
        effect=effect, tool="post_note", lane="personal/reddit", domain=None, summary=f"An action: {effect}",
        content_hash="sha256:" + digit * 64, preview="the post", requester=Principal(id="agent-1"), grant_id=None,
        grant_path=None, reason=None, content_digest="sha256:" + digit * 64, **kw)


async def decide(peer, ap, *, decision="approved", **body):
    return await peer.send("approval_decision", {"approval_id": ap.id, "decision": decision,
                                                 "content_hash": ap.content_hash, "decided_by": "u_1", **body})


async def decided(peer, ap, state="approved"):
    return await peer.recv("event", where=lambda e: e.body["kind"] == "approval" and e.body["payload"]["id"] == ap.id
                           and e.body["payload"]["state"] == state)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ---- negotiation and what events carry ----

async def test_hello_says_it_supports_confirmations(hosted):
    fake, _runtime, _peer = hosted
    assert "confirmations" in fake.hellos[0]["supports"]


async def test_approval_events_carry_requires_and_methods(hosted):
    _fake, runtime, peer = hosted
    ap = action(runtime, "pay", "2")
    sent = await peer.recv("approval_request", where=lambda e: e.body["approval"]["id"] == ap.id)
    assert sent.body["approval"]["requires"] == "present" and sent.body["approval"]["methods"] == []


# ---- a decision's proof ----

async def test_no_proof_counts_as_signed_in_at_most(hosted):
    """A controller that leaves the proof out gains nothing from it."""
    _fake, runtime, peer = hosted
    commit, pay = action(runtime, "commit", "3"), action(runtime, "pay", "4")
    await decide(peer, commit)
    event = await decided(peer, commit)
    assert "decided_level" not in event.body["payload"]
    a = runtime.approvals.get(commit.id)
    assert (a.decided_method, a.decided_level, a.decided_by) == (None, None, "controller:u_1")
    sent = await decide(peer, pay)
    reply = await peer.reply_to(sent.id)
    assert reply.body["code"] == "proof_too_weak" and reply.body["requires"] == "present"
    assert runtime.approvals.get(pay.id).state == "pending"


async def test_a_proof_up_to_the_limit_is_believed(hosted):
    _fake, runtime, peer = hosted
    pay = action(runtime, "pay", "5")
    await decide(peer, pay, proof={"method": "passkey", "level": "present"})
    event = await decided(peer, pay)
    assert event.body["payload"]["decided_method"] == "passkey" and event.body["payload"]["decided_level"] == "present"


async def test_a_lowered_limit_is_held_against_the_controller(hosted):
    _fake, runtime, peer = hosted
    runtime.get("confirmation").change({"controller_max_level": "signed_in"}, by="owner", role="owner")
    pay = action(runtime, "pay", "6")
    sent = await decide(peer, pay, proof={"method": "passkey", "level": "present"})
    assert (await peer.reply_to(sent.id)).body["code"] == "proof_too_weak"
    assert runtime.approvals.get(pay.id).state == "pending"


async def test_a_controller_cannot_claim_a_device_signature(hosted):
    _fake, runtime, peer = hosted
    runtime.get("confirmation").change({"pay": {"level": "present"}}, by="owner", role="owner")
    for method in ("mac", "watch"):
        pay = action(runtime, "pay", "7" if method == "mac" else "8")
        sent = await decide(peer, pay, proof={"method": method, "level": "device_key", "signature": "made-up"})
        assert (await peer.reply_to(sent.id)).body["code"] == "method_unavailable"
        assert runtime.approvals.get(pay.id).state == "pending"


async def test_a_proof_says_no_more_than_its_method_reaches(hosted):
    _fake, runtime, peer = hosted
    pay = action(runtime, "pay", "9")
    sent = await decide(peer, pay, proof={"method": "panel", "level": "device_key"})
    assert (await peer.reply_to(sent.id)).body["code"] == "proof_too_weak"


async def test_a_controller_cannot_report_a_chat_answer(hosted):
    _fake, runtime, peer = hosted
    commit = action(runtime, "commit", "a")
    sent = await decide(peer, commit, proof={"method": "chat", "level": "chat"})
    assert (await peer.reply_to(sent.id)).body["code"] == "method_unavailable"


async def test_a_denial_needs_no_proof_and_a_bad_one_never_stops_it(hosted):
    _fake, runtime, peer = hosted
    pay = action(runtime, "pay", "b")
    await decide(peer, pay, decision="denied", proof={"method": "fax", "level": "nonsense"})
    await decided(peer, pay, "denied")
    a = runtime.approvals.get(pay.id)
    assert (a.state, a.decided_method, a.decided_level) == ("denied", None, None)


async def test_a_malformed_proof_is_refused(hosted):
    _fake, runtime, peer = hosted
    commit = action(runtime, "commit", "c")
    sent = await decide(peer, commit, proof="trust me")
    assert (await peer.reply_to(sent.id)).body["code"] == "invalid"
    sent = await decide(peer, commit, proof={"method": "panel", "level": "root"})
    assert (await peer.reply_to(sent.id)).body["code"] == "invalid"


async def test_the_ceiling_still_comes_first(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        config = NodeConfig(home=home, name="node-t", mcp_token="node-owner-token", mcp_port=0, controller_url=fake.url,
                            node_id=fake.node_id, org_id="org_test", controller_pubkey=signing.public_b64(fake.key),
                            grant_pubkey=signing.public_b64(fake.grant_key))
        runtime = await node_runtime(home, fake, config=config)
        await start(runtime)
        peer = await fake.peer()
        await asyncio.wait_for(runtime.channel.welcomed.wait(), 15)
        pay = action(runtime, "pay", "d")
        sent = await decide(peer, pay, proof={"method": "passkey", "level": "present"})
        reply = await peer.reply_to(sent.id)
        assert reply.body["message"].startswith("controller_ceiling: ")
        await stop(runtime)


# ---- a device: the node checks the signature itself ----

def watch_on(runtime, person="owner"):
    policy = runtime.get("policy")
    policy.confirm_methods.register(FakeWatch(policy.devices))
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, policy.devices.add(person=person, method="fakewatch", public_key=b64(raw), label="Watch")


async def test_a_new_device_method_needs_no_rule_or_route_change(hosted):
    """The seam: a class, a registry row and a key. The setting takes the level, the approval lists the
    method, and the decide path is the one every other proof takes."""
    _fake, runtime, peer = hosted
    key, device = watch_on(runtime)
    runtime.get("confirmation").change({"pay": {"level": "device_key"}}, by="owner", role="owner")
    pay = action(runtime, "pay", "e")
    assert pay.requires == "device_key" and pay.methods == ["fakewatch"]
    signature = b64(key.sign(confirm.signed_message(pay)))
    await decide(peer, pay, proof={"method": "fakewatch", "level": "device_key", "device": device.id,
                                   "signature": signature})
    event = await decided(peer, pay)
    assert event.body["payload"]["decided_method"] == "fakewatch"
    assert event.body["payload"]["decided_level"] == "device_key"


async def test_the_limit_does_not_bind_a_signature_the_node_checked_itself(hosted):
    _fake, runtime, peer = hosted
    key, device = watch_on(runtime)
    runtime.get("confirmation").change({"pay": {"level": "device_key"}, "controller_max_level": "signed_in"}, by="owner",
                                       role="owner")
    pay = action(runtime, "pay", "f")
    await decide(peer, pay, proof={"method": "fakewatch", "level": "device_key", "device": device.id,
                                   "signature": b64(key.sign(confirm.signed_message(pay)))})
    await decided(peer, pay)


async def test_a_compromised_cloud_cannot_sign_for_a_device(hosted):
    _fake, runtime, peer = hosted
    _key, device = watch_on(runtime)
    runtime.get("confirmation").change({"pay": {"level": "device_key"}}, by="owner", role="owner")
    pay = action(runtime, "pay", "0")
    forged = Ed25519PrivateKey.generate().sign(confirm.signed_message(pay))
    sent = await decide(peer, pay, proof={"method": "fakewatch", "level": "device_key", "device": device.id,
                                          "signature": b64(forged)})
    reply = await peer.reply_to(sent.id)
    assert reply.body["code"] == "proof_too_weak" and reply.body["requires"] == "device_key"
    assert runtime.approvals.get(pay.id).state == "pending"


async def test_a_device_of_someone_who_may_not_decide_signs_nothing(hosted):
    _fake, runtime, peer = hosted
    key, device = watch_on(runtime, person="m_stranger")
    runtime.get("confirmation").change({"pay": {"level": "device_key"}}, by="owner", role="owner")
    pay = action(runtime, "pay", "1")
    sent = await decide(peer, pay, proof={"method": "fakewatch", "level": "device_key", "device": device.id,
                                          "signature": b64(key.sign(confirm.signed_message(pay)))})
    assert "not_approver" in (await peer.reply_to(sent.id)).body["message"]
    assert runtime.approvals.get(pay.id).state == "pending"


# ---- config_update ----

async def config_update(fake, peer, config):
    sent = await peer.send("config_update", fake.fresh("config_update", {"config": config}))
    await asyncio.sleep(0.2)
    return sent


CLOUD_DEFAULT = {"commit": {"when": "auto", "level": "chat"}, "pay": {"when": "ask", "level": "present"},
                 "irreversible": {"when": "ask", "level": "present"}, "after_untrusted": {"level": "signed_in"},
                 "methods_off": []}


async def test_a_node_that_asks_first_stays_asking_after_a_default_cloud_connect(tmp_path):
    """The review's first scenario: every connect sends the cloud's defaults, and they turned commit.when off."""
    home = tmp_path / "node"
    home.mkdir()
    (home / "policy.toml").write_text("[routing]\nask_first_default = true\n")
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake, config=hosted_config(home, fake))
        await start(runtime)
        peer = await fake.peer()
        await asyncio.wait_for(runtime.channel.welcomed.wait(), 15)
        await config_update(fake, peer, {"confirmation": CLOUD_DEFAULT})
        setting = runtime.get("confirmation")
        assert setting.current().commit.when == "ask"
        assert setting.ask_first_default() is True
        await stop(runtime)


async def test_what_the_owner_set_survives_a_default_cloud_connect(hosted):
    fake, runtime, peer = hosted
    setting = runtime.get("confirmation")
    setting.change({"commit": {"when": "ask", "level": "present"}, "methods_off": ["chat"]}, by="owner", role="owner")
    await config_update(fake, peer, {"confirmation": CLOUD_DEFAULT})
    now = setting.current()
    assert (now.commit.when, now.commit.level, now.methods_off) == ("ask", "present", ["chat"])


async def test_a_controller_cannot_lower_a_row_and_settle_with_no_proof(hosted):
    """The review's second scenario: commit.level present and a limit of signed_in. Lowering the row and
    deciding with no proof used to get past the limit."""
    fake, runtime, peer = hosted
    runtime.get("confirmation").change({"commit": {"level": "present"}, "controller_max_level": "signed_in"},
                                       by="owner", role="owner")
    await config_update(fake, peer, {"confirmation": {"commit": {"level": "chat"}}})
    commit = action(runtime, "commit", "3")
    assert commit.requires == "present"
    sent = await decide(peer, commit)
    reply = await peer.reply_to(sent.id)
    assert reply.body["code"] == "proof_too_weak" and reply.body["requires"] == "present"
    assert runtime.approvals.get(commit.id).state == "pending"


async def test_a_controllers_layer_raises_and_its_null_drops_the_layer(hosted):
    fake, runtime, peer = hosted
    setting = runtime.get("confirmation")
    await config_update(fake, peer, {"confirmation": {"commit": {"level": "present", "when": "ask"}}})
    assert (setting.current().commit.when, setting.current().commit.level) == ("ask", "present")
    await config_update(fake, peer, {"heartbeat_seconds": 7})  # no `confirmation` at all: the layer stays
    assert setting.current().commit.level == "present"
    await config_update(fake, peer, {"confirmation": None})
    assert (setting.current().commit.when, setting.current().commit.level) == ("auto", "chat")


def test_leaving_a_controller_drops_its_layer(tmp_path):
    from openajar.node.channel import standing
    from openajar.policy.issuers import TrustedIssuers

    store = store_for(tmp_path)
    store.apply_controller({"commit": {"when": "ask"}}, by="controller:org:old")
    trusted = TrustedIssuers(tmp_path)
    standing.leave_org(tmp_path, "org:old", issuers=trusted)
    trusted.close()
    assert store.current().commit.when == "auto"


def test_leaving_another_controller_keeps_this_ones_layer(tmp_path):
    from openajar.node.channel import standing
    from openajar.policy.issuers import TrustedIssuers

    store = store_for(tmp_path)
    store.apply_controller({"commit": {"when": "ask"}}, by="controller:org:new")
    trusted = TrustedIssuers(tmp_path)
    standing.leave_org(tmp_path, "org:old", issuers=trusted)
    trusted.close()
    assert store.current().commit.when == "ask"


async def test_a_config_update_raises_a_row_and_a_lower_one_has_no_effect(hosted):
    fake, runtime, peer = hosted
    await config_update(fake, peer, {"confirmation": {"commit": {"level": "signed_in"}, "pay": {"level": "signed_in"}}})
    now = runtime.get("confirmation").current()
    assert (now.commit.level, now.pay.level) == ("signed_in", "present")


@pytest.mark.parametrize("patch", [{"pay": {"when": "auto"}}, {"irreversible": {"when": "auto"}},
                                   {"pay": {"level": "chat"}}, {"after_untrusted": {"level": "chat"}},
                                   {"controller_max_level": "device_key"}])
async def test_a_config_update_never_lowers_a_floor_or_its_own_limit(hosted, patch):
    fake, runtime, peer = hosted
    await config_update(fake, peer, {"confirmation": patch})
    assert runtime.get("confirmation").current().model_dump() == runtime.get("confirmation").base.model_dump()


async def test_a_config_update_cannot_lower_what_only_rises(hosted):
    fake, runtime, peer = hosted
    runtime.get("confirmation").change({"after_untrusted": {"level": "present"}}, by="owner", role="owner")
    await config_update(fake, peer, {"confirmation": {"after_untrusted": {"level": "signed_in"}}})
    assert runtime.get("confirmation").current().after_untrusted.level == "present"


async def test_the_part_that_cannot_be_read_does_not_stop_the_rest_or_the_other_config(hosted):
    fake, runtime, peer = hosted
    await config_update(fake, peer, {"heartbeat_seconds": 7, "confirmation": {"pay": "whatever",
                                                                             "commit": {"when": "ask"}}})
    assert runtime.get("confirmation").current().commit.when == "ask"
    assert runtime.channel.controller_config.get("heartbeat_seconds") == 7
    assert "confirmation" not in runtime.channel.controller_config


async def test_a_welcome_cannot_carry_it(hosted):
    """Only `config_update` is signed."""
    _fake, runtime, _peer = hosted
    runtime.channel._set_config({"confirmation": {"commit": {"level": "present"}}}, replace=True)
    assert runtime.get("confirmation").current().commit.level == "chat"


# ---- what the node reports: the cloud reads exactly this ----

async def test_the_inventory_reports_what_is_in_force_after_both_layers(hosted):
    fake, runtime, peer = hosted
    runtime.channel._throttle.min_interval = 0.0
    runtime.get("confirmation").change({"pay": {"level": "signed_in"}, "irreversible": {"level": "signed_in"}},
                                       by="owner", role="owner")
    await config_update(fake, peer, {"confirmation": {"commit": {"when": "ask", "level": "signed_in"},
                                                      "irreversible": {"level": "present"}, "methods_off": ["chat"]}})
    await runtime.channel.send_reports()

    def layered(event):
        report = event.body["payload"].get("inventory") if event.body["kind"] == "node" else None
        return bool(report) and report.get("confirmation", {}).get("commit", {}).get("when") == "ask"

    event = await peer.recv("event", where=layered, timeout=10)
    assert event.body["payload"]["inventory"]["confirmation"] == {
        "commit": {"when": "ask", "level": "signed_in"}, "pay": {"when": "ask", "level": "signed_in"},
        "irreversible": {"when": "ask", "level": "present"}, "after_untrusted": {"level": "signed_in"},
        "methods_off": ["chat"]}


# ---- the rule itself ----

def test_no_proof_never_reaches_past_the_limit():
    proof = controller_proof(None, default_registry(), ConfirmLevel.CHAT)
    assert proof.counts == ConfirmLevel.CHAT and proof.level is None


def test_the_proof_of_a_ready_method_is_held_to_the_limit():
    out = controller_proof({"method": "passkey", "level": "present"}, default_registry(), ConfirmLevel.SIGNED_IN)
    assert out == Proof("passkey", ConfirmLevel.SIGNED_IN)


def test_an_unknown_method_is_refused():
    with pytest.raises(MethodUnavailable):
        controller_proof({"method": "fax", "level": "present"}, MethodRegistry(), ConfirmLevel.PRESENT)


# ---- the shape the cloud sends: one file, tested on both sides ----

WIRE = json.loads((Path(__file__).parent.parent / "fixtures" / "protocol" / "confirmation.json").read_text())


def store_for(tmp_path):
    from openajar.policy.confirmation import ConfirmationConfig, ConfirmationStore
    from openajar.policy.store import DB

    return ConfirmationStore(DB(tmp_path / "policy.db", ""), base=ConfirmationConfig(), clock=lambda: 1.0)


def test_every_field_the_cloud_sends_is_applied(tmp_path):
    store = store_for(tmp_path)
    assert store.apply_controller(WIRE, by="controller:x")["refused"] == []
    now = store.current()
    assert now.methods_off == ["chat"] and now.workspaces["harbor"].after_untrusted.level == "present"


def test_a_workspace_only_ever_asks_for_more(tmp_path):
    store = store_for(tmp_path)
    store.apply_controller(WIRE, by="controller:x")
    now = store.current()
    assert now.requires("commit", workspace="harbor") == ConfirmLevel.SIGNED_IN
    assert now.requires("commit", workspace="elsewhere") == ConfirmLevel.CHAT
    assert now.requires("commit", untrusted=True, workspace="harbor") == ConfirmLevel.PRESENT
    assert store.ask_first_default("harbor") is True and store.ask_first_default("elsewhere") is False
    store.apply_controller({"workspaces": {"harbor": {"pay": {"level": "signed_in"}}}}, by="c")
    assert store.current().requires("pay", workspace="harbor") == ConfirmLevel.PRESENT  # the node's row still holds


def test_chat_off_in_a_workspace_means_a_person_signed_in_there(tmp_path):
    store = store_for(tmp_path)
    store.apply_controller({"workspaces": {"harbor": {"methods_off": ["chat"]}}}, by="c")
    assert store.current().requires("commit", workspace="harbor") == ConfirmLevel.SIGNED_IN


def test_a_controller_takes_back_only_its_own_chat_off(tmp_path):
    store = store_for(tmp_path)
    store.apply_controller({"methods_off": ["chat"]}, by="c")
    assert store.current().methods_off == ["chat"]
    assert store.apply_controller({"methods_off": []}, by="c")["refused"] == []
    assert store.current().methods_off == []  # its own layer, replaced whole
    store.change({"methods_off": ["chat"]}, by="owner", role="owner")
    store.apply_controller({"methods_off": []}, by="c")
    assert store.current().methods_off == ["chat"]  # what the owner turned off, it cannot turn on


def test_a_workspace_the_node_cannot_read_is_refused_and_the_rest_applies(tmp_path):
    store = store_for(tmp_path)
    result = store.apply_controller({"commit": {"when": "ask"}, "workspaces": {"harbor": {"pay": {"level": "root"}}}}, by="c")
    assert [r["field"] for r in result["refused"]] == ["workspaces"] and store.current().commit.when == "ask"


def test_a_lower_workspace_row_has_no_effect_and_is_not_refused(tmp_path):
    store = store_for(tmp_path)
    result = store.apply_controller({"workspaces": {"harbor": {"pay": {"level": "chat", "when": "auto"}}}}, by="c")
    assert result["refused"] == []
    assert store.current().requires("pay", workspace="harbor") == ConfirmLevel.PRESENT


def test_policy_toml_workspaces_survive_a_layer(tmp_path):
    from openajar.policy.confirmation import ConfirmationConfig, ConfirmationStore
    from openajar.policy.store import DB

    base = ConfirmationConfig(workspaces={"harbor": {"commit": {"when": "ask", "level": "present"}}})
    store = ConfirmationStore(DB(tmp_path / "policy.db", ""), base=base, clock=lambda: 1.0)
    store.apply_controller({"workspaces": {"harbor": {"commit": {"when": "auto", "level": "chat"}, "methods_off": ["chat"]},
                                           "aster": {"commit": {"level": "signed_in"}}}}, by="c")
    now = store.current()
    assert now.asks_in("harbor") is True and now.requires("commit", workspace="harbor") == ConfirmLevel.PRESENT
    assert now.workspaces["harbor"].methods_off == ["chat"]
    assert now.requires("commit", workspace="aster") == ConfirmLevel.SIGNED_IN


def test_an_account_nobody_switched_follows_its_workspace(tmp_path):
    from openajar.policy.ask_first import AskFirst

    store = store_for(tmp_path)
    store.apply_controller({"workspaces": {"harbor": {"commit": {"when": "ask"}}}}, by="c")
    switches = AskFirst(store.db, clock=lambda: 1.0, default=store.ask_first_default)
    assert switches.is_on("lane", "harbor/instagram") is True and switches.is_on("lane", "aster/instagram") is False
