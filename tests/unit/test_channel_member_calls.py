"""A member acting in the controller's panel: the controller's short-lived per-call grant for a
person (subject kind human) runs over the channel's `invoke`, and nowhere else."""

import asyncio
import json
import time

import pytest
from channel_fake_controller import NODE_ID, FakeController, node_pubkey, node_runtime, start, stop
from test_channel_node import invoke_body

from agentos.mcp.registry import bind_request_token
from agentos.policy import tokens
from agentos.policy.errors import TokenInvalid
from agentos.policy.grants import GrantBlock, Subject
from agentos.policy.service import MEMBER_CALL_MAX_TTL_S
from agentos.policy.util import new_id

MEMBER = "u_0123456789abcdef"
LANE = "personal/reddit"


def member_call(fake, *, ceiling="read", ttl=40, lanes=(LANE,)):
    """A call token minted as the hosted controller mints one (grants.PolicyGrantIssuer.call_token)."""
    block = {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "iat": int(time.time()), "exp": int(time.time()) + ttl,
             "sub": {"kind": "human", "id": MEMBER, "owner": MEMBER}, "ceiling": ceiling, "lanes": list(lanes)}
    return tokens.mint(fake.grant_key, GrantBlock.model_validate(block).to_data())


def as_member(body):
    body["principal"] = {"kind": "human", "id": MEMBER, "owner": MEMBER, "thread": None}
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


async def test_a_member_call_runs_over_invoke_and_the_member_is_who_did_it(wired):
    fake, runtime, peer = wired
    sent = await peer.send("invoke", as_member(invoke_body(fake, "browser_snapshot", {"on": LANE},
                                                           token=member_call(fake))))
    result = await peer.reply_to(sent.id)
    assert result.body["ok"], result.body
    events, _ = runtime.policy.traces.query(tool="browser_snapshot", kind="tool_end")
    assert [(e.principal, e.principal_kind, e.status) for e in events] == [(MEMBER, "human", "ok")]


async def test_a_member_call_is_refused_off_the_channel(wired):
    fake, runtime, _peer = wired
    token = member_call(fake)
    policy = runtime.policy
    with pytest.raises(TokenInvalid, match="not a bearer token"):
        policy.resolve(token)
    # MCP over HTTP: the bearer gate, then the policy wrapper with the token bound as http.py binds it
    assert not runtime.get("mcp_hooks").accept_token(token)
    with bind_request_token(token):
        refused = await runtime.get("mcp").call_tool("browser_snapshot", {"on": LANE})
    assert refused.is_error and "no tool named" in refused.content[0].text  # it lists no tools for it
    assert runtime.policy.visible_tools(token, list(runtime.get("mcp").specs.values())) == []
    assert policy.authenticate_panel(token) is None  # the panel's bearer path
    # marked as another invoke's token: still refused
    with policy.channel_call(member_call(fake)), pytest.raises(TokenInvalid):
        policy.resolve(token)
    with policy.channel_call(token):
        assert policy.resolve(token)[0].id == MEMBER


async def test_a_human_grant_of_the_node_itself_is_refused_over_the_channel(wired):
    fake, runtime, peer = wired
    # short-lived and in scope, but signed by this node: a member of the node signs in to its panel
    _g, own = runtime.policy.issue(subject=Subject(kind="human", id="m_ana", owner="m_ana"), workspaces=["*"],
                                   ttl_s=60, _member=True)
    sent = await peer.send("invoke", invoke_body(fake, "browser_snapshot", {"on": LANE}, token=own))
    refused = await peer.reply_to(sent.id)
    assert refused.body["error"]["message"].startswith("invalid_token: a member's grant is not a bearer token")
    with runtime.policy.channel_call(own), pytest.raises(TokenInvalid, match="not a bearer token"):
        runtime.policy.resolve(own)


async def test_a_long_lived_member_grant_is_refused(wired):
    fake, _runtime, peer = wired
    standing = member_call(fake, ttl=MEMBER_CALL_MAX_TTL_S + 120)
    sent = await peer.send("invoke", as_member(invoke_body(fake, "browser_snapshot", {"on": LANE}, token=standing)))
    refused = await peer.reply_to(sent.id)
    assert refused.body["error"]["message"].startswith("invalid_token: a member's call grant from the controller")
    # the cloud's longest: an approved call's 300 s plus 10 s past the deadline
    ok = await peer.send("invoke", as_member(invoke_body(fake, "browser_snapshot", {"on": LANE},
                                                         token=member_call(fake, ttl=310))))
    assert (await peer.reply_to(ok.id)).body["ok"]


async def test_the_controller_ceiling_and_the_call_grant_still_limit_a_member(wired):
    fake, runtime, peer = wired
    note = {"on": LANE, "text": "hi"}
    narrow = await peer.send("invoke", as_member(invoke_body(fake, "post_note", note, token=member_call(fake))))
    # the call's own ceiling (read): a commit tool is not even listed to it
    assert "no tool named 'post_note'" in (await peer.reply_to(narrow.id)).body["error"]["message"]
    elsewhere = await peer.send("invoke", as_member(invoke_body(fake, "browser_snapshot", {"on": "work/x"},
                                                                token=member_call(fake))))
    assert "out_of_scope" in (await peer.reply_to(elsewhere.id)).body["error"]["message"]
    runtime.policy.node_config = runtime.config.model_copy(update={"controller_max_effect": "read"})
    clamped = await peer.send("invoke", as_member(invoke_body(fake, "post_note", note,
                                                              token=member_call(fake, ceiling="commit"))))
    assert "controller_ceiling" in (await peer.reply_to(clamped.id)).body["error"]["message"]


async def test_an_approval_a_member_call_asks_for_is_the_members(wired):
    fake, runtime, peer = wired
    sent = await peer.send("invoke", as_member(invoke_body(fake, "post_note", {"on": LANE, "text": "hi"},
                                                           token=member_call(fake, ceiling="commit"))))
    pending = await peer.reply_to(sent.id)
    assert pending.body["error"]["code"] == "approval_pending"  # routed as an agent's, no in-person co-sign
    approval = runtime.approvals.get(pending.body["error"]["approval_id"])
    assert approval.requester["id"] == MEMBER and approval.requester["kind"] == "human"
    assert approval.requester_owner == MEMBER
    request = await peer.recv("approval_request")
    assert request.body["approval"]["id"] == approval.id
    assert MEMBER in json.dumps(request.body["approval"])
