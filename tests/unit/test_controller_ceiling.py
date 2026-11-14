"""The controller ceiling (agentos.policy.ceiling): what the node's owner lets a controller do,
set in node.toml and applied to the org's grants, lane_control and stream_open."""

from __future__ import annotations

import asyncio
import time
import tomllib

import httpx2
import pytest
import tomli_w
from channel_fake_controller import NODE_ID, FakeController, node_pubkey, node_runtime, start, stop
from channel_support import AGENT_TOKEN, OWNER_TOKEN, start_controller, start_node
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_qa_controller_up import builder, until_connected

from agentos.controller.config import ControllerConfig, save_controller_config
from agentos.controller.up import run_up
from agentos.core import signing
from agentos.core.config import NodeConfig, load_config, save_config
from agentos.core.models import ControlState, Effect, Principal
from agentos.core.runtime import Runtime
from agentos.node.channel import wire
from agentos.policy import tokens
from agentos.policy.ceiling import ControllerCeiling
from agentos.policy.errors import Denied
from agentos.policy.grants import GrantBlock, Specials
from agentos.policy.issuers import SERVICE_NAME, TrustedIssuers
from agentos.policy.service import build
from agentos.policy.util import new_id


def mint(key, *, ceiling="irreversible", specials=True, **fields):
    block = {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "iat": int(time.time()),
             "sub": {"kind": "agent", "id": "helper", "owner": "owner"}, "workspaces": ["*"],
             "ceiling": ceiling, "exp": int(time.time()) + 600, **fields}
    if specials:
        block["specials"] = Specials.everything().model_dump(mode="json", exclude_none=True)
    return tokens.mint(key, GrantBlock.model_validate(block).to_data())


def reason(call):
    with pytest.raises(Denied) as raised:
        call()
    return raised.value.reason


def policy_for(tmp_path, **ceiling):
    org = Ed25519PrivateKey.generate()
    runtime = Runtime(config=NodeConfig(home=tmp_path, mcp_token="owner-token-x", **ceiling))
    issuers = TrustedIssuers(tmp_path)
    issuers.trust("org:test", signing.public_b64(org))
    runtime.register(SERVICE_NAME, issuers)
    return build(runtime), org


# ---- node.toml ----

def test_the_default_is_the_hosted_ceiling_and_bad_values_are_refused():
    config = NodeConfig()
    assert (config.controller_max_effect, config.controller_specials, config.controller_streams) == \
        ("commit", [], ["view"])
    assert ControllerCeiling.from_config(config) == ControllerCeiling()
    assert ControllerCeiling.from_config(None).report() == {"max_effect": "commit", "specials": [],
                                                            "streams": ["view"]}
    for bad in ({"controller_max_effect": "secret"}, {"controller_max_effect": "everything"},
                {"controller_specials": ["approve"]}, {"controller_specials": ["agent"]},
                {"controller_streams": ["files"]}):
        with pytest.raises(ValueError, match="controller_"):
            NodeConfig(**bad)
    full = NodeConfig(**ControllerCeiling.full().settings())
    assert ControllerCeiling.from_config(full) == ControllerCeiling.full()


def test_the_ceiling_round_trips_through_node_toml(tmp_path):
    save_config(NodeConfig(home=tmp_path, controller_max_effect="pay", controller_specials=["secret"],
                           controller_streams=["view", "control"]))
    loaded = ControllerCeiling.from_config(load_config(tmp_path))
    assert loaded.report() == {"max_effect": "pay", "specials": ["secret"], "streams": ["view", "control"]}


# ---- grants from the org ----

def test_an_org_grant_is_clamped_to_the_default_ceiling(tmp_path):
    policy, org = policy_for(tmp_path)
    _, grant = policy.resolve(mint(org))
    assert grant.ceiling == Effect.COMMIT
    assert grant.caps("secret") is None and grant.caps("exec") is None and grant.caps("brain") is None
    assert not {"secret", "exec", "install_base", "install_overlay", "service", "brain"} & grant.specials()
    policy.precheck(grant, Effect.COMMIT, lane_id="personal/reddit")
    for effect in (Effect.PAY, Effect.IRREVERSIBLE, Effect.SECRET, Effect.EXEC, Effect.INSTALL_BASE,
                   Effect.INSTALL_OVERLAY, Effect.SERVICE, Effect.BRAIN):
        assert reason(lambda e=effect: policy.precheck(grant, e, lane_id="personal/reddit")) == \
            "controller_ceiling", effect
    policy.close()


def test_specials_are_an_allowlist_memory_included(tmp_path):
    policy, org = policy_for(tmp_path)
    _, grant = policy.resolve(mint(org))
    assert grant.caps("memory") is None and not grant.specials()
    assert reason(lambda: policy.precheck(grant, Effect.AGENT, lane_id="personal/reddit")) == "controller_ceiling"
    policy.close()
    (tmp_path / "raised").mkdir()
    raised, org = policy_for(tmp_path / "raised", controller_specials=["memory"])
    _, grant = raised.resolve(mint(org))
    assert grant.specials() == {"memory"}
    # a special the node does not know yet is refused, never let through
    assert not ControllerCeiling.full().allows_special("teleport")
    raised.close()


def test_a_grant_already_below_the_ceiling_keeps_its_own_reasons(tmp_path):
    policy, org = policy_for(tmp_path)
    _, grant = policy.resolve(mint(org, ceiling="read", specials=False))
    assert reason(lambda: policy.precheck(grant, Effect.DRAFT)) == "ceiling"  # the grant's own limit, not the node's
    assert reason(lambda: policy.precheck(grant, Effect.PAY)) == "controller_ceiling"
    policy.close()


def test_a_raised_ceiling_allows_only_what_it_names(tmp_path):
    policy, org = policy_for(tmp_path, controller_max_effect="pay", controller_specials=["secret"])
    _, grant = policy.resolve(mint(org))
    policy.precheck(grant, Effect.PAY, lane_id="personal/reddit")
    policy.precheck(grant, Effect.SECRET, lane_id="personal/reddit")
    assert reason(lambda: policy.precheck(grant, Effect.IRREVERSIBLE)) == "controller_ceiling"
    assert reason(lambda: policy.precheck(grant, Effect.EXEC, lane_id="personal/reddit")) == "controller_ceiling"
    policy.close()


def test_a_narrowed_org_grant_is_clamped_too(tmp_path):
    policy, org = policy_for(tmp_path)
    token = mint(org)
    root = tokens.open_token(token, org.public_key()).data[0]
    child = tokens.append(token, {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "parent": root["id"],
                                  "iat": int(time.time()), "ceiling": "irreversible"})
    _, grant = policy.resolve(child)
    assert grant.depth == 1 and grant.ceiling == Effect.COMMIT
    assert reason(lambda: policy.precheck(grant, Effect.IRREVERSIBLE)) == "controller_ceiling"
    policy.close()


def test_the_nodes_own_grants_are_not_bounded_by_the_controller_ceiling(tmp_path):
    policy, _ = policy_for(tmp_path)
    _, own = policy.issue(subject={"id": "local"}, workspaces=["*"], ceiling="irreversible",
                          specials=Specials.everything())
    _, grant = policy.resolve(own)
    assert grant.limit is None
    policy.precheck(grant, Effect.IRREVERSIBLE, lane_id="personal/reddit")
    policy.precheck(grant, Effect.EXEC, lane_id="personal/reddit")
    policy.close()


# ---- the channel ----

def hosted_config(home, fake, **ceiling):
    return NodeConfig(home=home, name="node-t", mcp_token="node-owner-token", mcp_port=0, controller_url=fake.url,
                      node_id=fake.node_id, org_id="org_test", controller_pubkey=signing.public_b64(fake.key),
                      grant_pubkey=signing.public_b64(fake.grant_key), **ceiling)


@pytest.fixture
async def hosted(tmp_path, request):
    home = tmp_path / "node"
    ceiling = getattr(request, "param", {})
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake, config=hosted_config(home, fake, **ceiling))
        await start(runtime)
        peer = await fake.peer()
        await asyncio.wait_for(runtime.channel.welcomed.wait(), 15)  # the fake sees the peer before the node is in
        yield fake, runtime, peer
        await stop(runtime)


def refused(reply) -> bool:
    return reply.type == "error" and reply.body["code"] == "denied" and \
        reply.body["message"].startswith("controller_ceiling: ")


async def test_hello_reports_the_ceiling(hosted):
    fake, _runtime, _peer = hosted
    assert fake.hellos[0]["ceiling"] == {"max_effect": "commit", "specials": [], "streams": ["view"]}


async def test_take_needs_control_but_hand_back_does_not(hosted):
    fake, runtime, peer = hosted
    sent = await peer.send("lane_control", fake.fresh("lane_control", {"lane": "personal/reddit", "action": "take",
                                                                      "by": "u_1", "ts": wire.iso()}))
    assert refused(await peer.reply_to(sent.id))
    assert runtime.lanes.get("personal/reddit").control.state == ControlState.AGENT
    await runtime.lanes.take_control("personal/reddit", by="owner")  # a person at the node
    back = await peer.send("lane_control", fake.fresh("lane_control", {"lane": "personal/reddit",
                                                                      "action": "hand_back", "ts": wire.iso()}))
    await asyncio.sleep(0.3)
    assert not [e for e in peer.seen if e.reply_to == back.id]
    assert runtime.lanes.get("personal/reddit").control.state == ControlState.AGENT


async def test_streams_need_their_kind_in_the_ceiling(hosted):
    _fake, _runtime, peer = hosted
    for kind, params in (("vnc", {"mode": "control"}), ("terminal", {}), ("terminal", {"mode": "control"})):
        sent = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": kind, "lane": "personal/reddit",
                                               "params": params})
        assert refused(await peer.reply_to(sent.id)), (kind, params)
    # watching passes the ceiling (and then fails for its own reason: the lane is not open)
    sent = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": "vnc", "lane": "personal/reddit",
                                           "params": {"mode": "view"}})
    reply = await peer.reply_to(sent.id)
    assert reply.type == "error" and not refused(reply)


async def test_the_hosted_ceiling_serves_files_to_watch_but_not_the_logs(hosted):
    _fake, _runtime, peer = hosted
    logs = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": "logs", "params": {}})
    assert refused(await peer.reply_to(logs.id))
    # a file is watching: it passes the ceiling (and then fails for its own reason: the lane is not open)
    shot = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": "file", "lane": "personal/reddit",
                                           "params": {"path": "screenshot.png"}})
    reply = await peer.reply_to(shot.id)
    assert reply.type == "error" and not refused(reply)


@pytest.mark.parametrize("hosted", [{"controller_streams": []}], indirect=True)
async def test_a_ceiling_with_no_streams_refuses_files_too(hosted):
    _fake, _runtime, peer = hosted
    shot = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": "file", "lane": "personal/reddit",
                                           "params": {"path": "screenshot.png"}})
    assert refused(await peer.reply_to(shot.id))


@pytest.mark.parametrize("hosted", [{"controller_streams": ["view", "terminal"]}], indirect=True)
async def test_a_terminal_to_watch_does_not_open_typing(hosted):
    _fake, _runtime, peer = hosted
    typing = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": "terminal", "lane": "dev/box",
                                             "params": {"mode": "control"}})
    assert refused(await peer.reply_to(typing.id))
    watching = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": "terminal", "lane": "dev/box",
                                               "params": {}})
    assert not refused(await peer.reply_to(watching.id))  # no terminal lanes here: refused for that


@pytest.mark.parametrize("hosted", [{"controller_streams": ["view", "control", "terminal"]}], indirect=True)
async def test_typing_into_a_terminal_also_needs_exec(hosted):
    _fake, _runtime, peer = hosted
    typing = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": "terminal", "lane": "dev/box",
                                             "params": {"mode": "control"}})
    reply = await peer.reply_to(typing.id)
    assert refused(reply) and "exec" in reply.body["message"]


@pytest.mark.parametrize("hosted", [{"controller_streams": ["view", "control", "terminal"],
                                     "controller_specials": ["exec"]}], indirect=True)
async def test_typing_into_a_terminal_with_exec_passes_the_ceiling(hosted):
    _fake, _runtime, peer = hosted
    typing = await peer.send("stream_open", {"stream_id": wire.ulid(), "kind": "terminal", "lane": "dev/box",
                                             "params": {"mode": "control"}})
    assert not refused(await peer.reply_to(typing.id))  # no terminal lanes here: refused for that


def pending_action(runtime, effect, digit):
    return runtime.get("approvals").create_action(
        effect=effect, tool="post_note", lane="personal/reddit", domain=None, summary=f"An action: {effect}",
        content_hash="sha256:" + digit * 64, preview=None, requester=Principal(id="agent-1"), grant_id=None,
        grant_path=None, reason=None)


async def test_a_controller_approves_only_within_the_ceiling(hosted):
    _fake, runtime, peer = hosted
    above = {effect: pending_action(runtime, effect, str(i)) for i, effect in enumerate(("pay", "secret", "exec"))}
    for effect, approval in above.items():
        sent = await peer.send("approval_decision", {"approval_id": approval.id, "decision": "approved",
                                                     "content_hash": approval.content_hash, "decided_by": "u_1"})
        reply = await peer.reply_to(sent.id)
        assert refused(reply) and reply.body["approval_id"] == approval.id, effect
        assert runtime.approvals.get(approval.id).state == "pending", effect
    # a denial always passes: it only stops an action
    pay = above["pay"]
    await peer.send("approval_decision", {"approval_id": pay.id, "decision": "denied",
                                          "content_hash": pay.content_hash, "decided_by": "u_1"})
    await peer.recv("event", where=lambda e: e.body["kind"] == "approval" and e.body["payload"]["id"] == pay.id
                    and e.body["payload"]["state"] == "denied")
    # within the ceiling, the controller decides as before
    commit = pending_action(runtime, "commit", "8")
    await peer.send("approval_decision", {"approval_id": commit.id, "decision": "approved",
                                          "content_hash": commit.content_hash, "decided_by": "u_1"})
    await peer.recv("event", where=lambda e: e.body["kind"] == "approval" and e.body["payload"]["id"] == commit.id
                    and e.body["payload"]["state"] == "approved")
    # the owner still decides the rest on the node
    assert runtime.approvals.decide(above["exec"].id, "approve").state == "approved"


@pytest.mark.parametrize("hosted", [{"controller_max_effect": "read"}], indirect=True)
async def test_an_invoke_above_the_ceiling_is_refused_with_the_reason(hosted):
    fake, _runtime, peer = hosted
    token = mint(fake.grant_key)
    sent = await peer.send("invoke", {"call_id": wire.prefixed("call"), "tool": "post_note",
                                      "args": {"on": "personal/reddit", "text": "hi"},
                                      "principal": {"kind": "agent", "id": "helper"}, "grant_token": token,
                                      "deadline": wire.iso(time.time() + 30)})
    result = await peer.recv("result", where=lambda e: e.body.get("re") == sent.id, timeout=10)
    assert not result.body["ok"], result.body
    assert result.body["error"]["code"] == "denied", result.body
    assert result.body["error"]["message"].startswith("controller_ceiling: ")


# ---- the controller shows it; `agentos up` and `node join` write it ----

async def test_the_controller_lists_the_ceiling_a_node_reported(tmp_path):
    ctl = await start_controller(tmp_path / "ctl")
    node = await start_node(tmp_path / "n1", "n1", ctl, ["personal/reddit"])
    try:
        async with httpx2.AsyncClient(base_url=ctl.url, headers={"Authorization": f"Bearer {OWNER_TOKEN}"}) as http:
            nodes = (await http.get("/api/nodes")).json()
        listed = next(n for n in nodes if n["name"] == "n1")
        assert listed["ceiling"] == ControllerCeiling.full().report()
        assert AGENT_TOKEN  # the owner's agent works through it (test_controller.py)
    finally:
        await node.close()
        await ctl.close()


@pytest.fixture
async def up_home(tmp_path):
    node_home = tmp_path / "node"
    node_home.mkdir()
    save_config(NodeConfig(home=node_home, name="box", mcp_token="node-owner-token-up", mcp_port=0))
    ctl_cfg = ControllerConfig(home=node_home / "controller", port=0, agent_token="aga_up_ceiling_agent_token",
                               owner_token="owner-up-ceiling")
    save_controller_config(ctl_cfg)
    return node_home, ctl_cfg


async def run_once(node_home, ctl_cfg) -> None:
    built: dict = {}
    stop_up = asyncio.Event()
    task = asyncio.create_task(run_up(load_config(node_home), ctl_cfg, stop=stop_up, build_runtime=builder(built)))
    try:
        await until_connected(built)
    finally:
        stop_up.set()
        await asyncio.wait_for(task, 20)


async def test_agentos_up_writes_the_full_ceiling_and_keeps_one_the_owner_lowered(up_home):
    node_home, ctl_cfg = up_home
    await run_once(node_home, ctl_cfg)
    assert ControllerCeiling.from_config(load_config(node_home)) == ControllerCeiling.full()
    lowered = load_config(node_home).model_copy(update={"controller_streams": ["view"]})
    save_config(lowered)
    await run_once(node_home, ctl_cfg)
    assert load_config(node_home).controller_streams == ["view"]  # the owner's choice stays


async def test_agentos_up_gives_a_node_joined_before_the_ceiling_the_full_one(up_home):
    node_home, ctl_cfg = up_home
    await run_once(node_home, ctl_cfg)
    path = node_home / "node.toml"
    data = tomllib.loads(path.read_text())
    for name in ControllerCeiling.full().settings("self_hosted"):
        data.pop(name)
    path.write_text(tomli_w.dumps(data))
    assert ControllerCeiling.from_config(load_config(node_home)) == ControllerCeiling()
    await run_once(node_home, ctl_cfg)
    assert ControllerCeiling.from_config(load_config(node_home)) == ControllerCeiling.full()


async def test_agentos_up_upgrades_an_old_node_after_any_save(up_home):
    """node.toml is saved whole on any change (rotate-token and the like), so a node from before the
    ceiling has every ceiling key afterwards; up must still give it the full ceiling."""
    node_home, ctl_cfg = up_home
    await run_once(node_home, ctl_cfg)
    path = node_home / "node.toml"
    data = tomllib.loads(path.read_text())
    for name in (*ControllerCeiling.full().settings(), "controller_ceiling_by"):
        data.pop(name)
    path.write_text(tomli_w.dumps(data))
    save_config(load_config(node_home).model_copy(update={"mcp_token": "rotated-node-owner-token"}))
    assert "controller_streams" in tomllib.loads(path.read_text())  # the save wrote every key
    await run_once(node_home, ctl_cfg)
    loaded = load_config(node_home)
    assert ControllerCeiling.from_config(loaded) == ControllerCeiling.full()
    assert loaded.controller_ceiling_by == "self_hosted"


async def test_agentos_up_keeps_a_lowered_ceiling_through_saves_and_one_the_owner_pinned(up_home):
    node_home, ctl_cfg = up_home
    await run_once(node_home, ctl_cfg)
    save_config(load_config(node_home).model_copy(update=ControllerCeiling().settings()))  # lowered to hosted
    save_config(load_config(node_home).model_copy(update={"mcp_token": "rotated-node-owner-token"}))
    await run_once(node_home, ctl_cfg)
    assert ControllerCeiling.from_config(load_config(node_home)) == ControllerCeiling()
    # pinned by the owner: kept even when it is the hosted default and nothing else says so
    save_config(load_config(node_home).model_copy(update={"controller_ceiling_by": "owner"}))
    await run_once(node_home, ctl_cfg)
    assert ControllerCeiling.from_config(load_config(node_home)) == ControllerCeiling()


def test_who_set_the_ceiling_decides_whether_up_may_raise_it():
    hosted = ControllerCeiling().settings()
    assert ControllerCeiling.upgradable(NodeConfig())
    assert ControllerCeiling.upgradable(NodeConfig(**hosted, controller_ceiling_by="join"))
    for by in ("self_hosted", "owner"):
        assert not ControllerCeiling.upgradable(NodeConfig(**hosted, controller_ceiling_by=by))
    # no marker, but not the hosted default: the owner wrote it by hand
    assert not ControllerCeiling.upgradable(NodeConfig(controller_specials=["secret"]))
    with pytest.raises(ValueError, match="controller_ceiling_by"):
        NodeConfig(controller_ceiling_by="cloud")


async def test_joining_another_controller_resets_the_ceiling(tmp_path):
    from agentos.node.channel.enroll import enroll

    ctl = await start_controller(tmp_path / "ctl")
    try:
        config = NodeConfig(home=tmp_path / "n", name="n", mcp_token="t", mcp_port=0,
                            **ControllerCeiling.full().settings())
        (tmp_path / "n").mkdir()
        _, token = ctl.controller.create_join_token(ctl.org, name="n")
        joined = await enroll(config, ctl.node_url, token, ceiling=ControllerCeiling())
        assert ControllerCeiling.from_config(joined) == ControllerCeiling()
        assert ControllerCeiling.from_config(load_config(tmp_path / "n")) == ControllerCeiling()
        assert load_config(tmp_path / "n").controller_ceiling_by == "join"
    finally:
        await ctl.close()
