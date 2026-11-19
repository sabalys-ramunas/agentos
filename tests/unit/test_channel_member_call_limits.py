"""A controller's member call is a person acting remotely in the cloud panel: every gate that lets
"a person at the node" through holds it like an agent, its token lives only for its one call (on
this node's own clock), and an approved call resumes with the retry's fresh token."""

import asyncio
import time

import pytest
from channel_fake_controller import NODE_ID, FakeController, node_pubkey, node_runtime, start, stop
from channel_support import wait_for
from test_channel_node import invoke_body
from test_lanes import make

from agentos.core.errors import AgentPaused, Frozen, HumanInControl, LaneBusy
from agentos.core.models import LaneSpec
from agentos.core.tools import gate_kind, in_person
from agentos.node import egress
from agentos.node.claims import Holder
from agentos.node.tools import caller, check_control, control_stop
from agentos.policy import tokens
from agentos.policy.errors import Denied, TokenInvalid
from agentos.policy.grants import GrantBlock
from agentos.policy.util import new_id

MEMBER = "u_0123456789abcdef"
LANE = "personal/reddit"


def member_call(fake, *, ceiling="read", ttl=40, iat=None, gid=None, rid=None, egress_hosts=None, child=None):
    """A call token as the hosted controller mints one: a stable grant id per person, a fresh token per call."""
    now = int(time.time())
    block = {"v": 1, "id": gid or new_id("gr"), "rid": rid or new_id("rv"), "iat": now if iat is None else iat,
             "exp": now + ttl, "sub": {"kind": "human", "id": MEMBER, "owner": MEMBER}, "ceiling": ceiling,
             "lanes": [LANE]}
    if egress_hosts is not None:
        block["egress"] = list(egress_hosts)
    token = tokens.mint(fake.grant_key, GrantBlock.model_validate(block).to_data())
    if child is not None:
        data = {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "parent": block["id"], "iat": now, **child}
        token = tokens.append(token, GrantBlock.model_validate(data).to_data())
    return token


def as_member(body):
    body["principal"] = {"kind": "human", "id": MEMBER, "owner": MEMBER, "thread": None}
    return body


def member_ctx(runtime, token):
    """The ToolContext a member call's tool runs with (as the policy wrapper builds it)."""
    policy = runtime.policy
    with policy.channel_call(token):
        principal, grant = policy.resolve(token)
    return policy.context(grant, principal=principal, tool="browser_snapshot", token=token)


@pytest.fixture
async def wired(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        await start(runtime)
        peer = await fake.peer()
        await wait_for(lambda: runtime.channel.connected)  # online: a new member call root is admitted
        yield fake, runtime, peer
        await stop(runtime)


@pytest.fixture
async def manager(tmp_path):
    """A real LaneManager (fake engine and displays): its control gate, freeze gates and claims."""
    mgr, *_ = make(tmp_path / "lanes")
    mgr.queue_timeout = 0.3  # a caller kept out by a claim gives up quickly
    await mgr.create(LaneSpec(id=LANE))
    yield mgr
    await mgr.shutdown()


async def invoke(fake, peer, tool, args, token):
    sent = await peer.send("invoke", as_member(invoke_body(fake, tool, args, token=token)))
    return (await peer.reply_to(sent.id)).body


# ---- a member call is not a person at the node ----------------------------------------


async def test_a_member_call_is_not_in_person_and_the_node_panel_still_is(wired):
    fake, runtime, _ = wired
    ctx = member_ctx(runtime, member_call(fake))
    assert ctx.principal.kind == "human" and ctx.grant.extra.get("member_call")
    assert not in_person(ctx) and gate_kind(ctx) == "member"
    assert caller(ctx).kind == "member" and not caller(ctx).human
    owner = runtime.policy.owner_context(tool="panel")  # the node's own panel session
    assert in_person(owner) and gate_kind(owner) == "human"
    token_ctx = runtime.policy.context_for_token("node-owner-token")  # the owner's own token, unchanged
    assert in_person(token_ctx) and gate_kind(token_ctx) == "human"


async def test_stop_agent_holds_a_member_call_off_the_lane(wired):
    fake, runtime, peer = wired
    await runtime.lanes.stop_agent(LANE, by="owner")
    runtime.config.lane_dir(LANE).mkdir(parents=True, exist_ok=True)
    token = member_call(fake, ceiling="draft")
    put = await invoke(fake, peer, "browser_file_put", {"on": LANE, "name": "note.txt", "content_base64": "aGk="}, token)
    assert put["error"]["code"] == "agent_paused", put  # io_tools: the paused gate
    snap = await invoke(fake, peer, "browser_snapshot", {"on": LANE}, member_call(fake))
    assert snap["error"]["code"] == "agent_paused", snap  # the lane's own control gate
    with pytest.raises(AgentPaused):
        check_control(runtime.lanes.get(LANE), gate_kind(member_ctx(runtime, member_call(fake))))  # browser_close


async def test_a_human_take_over_holds_a_member_call_off_the_lane(wired):
    fake, runtime, peer = wired
    await runtime.lanes.take_control(LANE, by="owner")
    snap = await invoke(fake, peer, "browser_snapshot", {"on": LANE}, member_call(fake))
    assert snap["error"]["code"] == "human_in_control", snap
    ctx = member_ctx(runtime, member_call(fake))
    with pytest.raises(HumanInControl):
        check_control(runtime.lanes.get(LANE), gate_kind(ctx))
    should_stop = control_stop(ctx, LANE)  # a wait ends when a person takes the lane
    assert should_stop is not None and should_stop()


async def test_the_lane_manager_gates_and_claims_hold_a_member_call(wired, manager):
    fake, runtime, _ = wired
    ctx = member_ctx(runtime, member_call(fake))
    await manager.stop_agent(LANE, by="owner")
    with pytest.raises(AgentPaused):
        async with manager.using(LANE, principal_kind=gate_kind(ctx), holder=caller(ctx)):
            pass
    async with manager.using(LANE, principal_kind="human"):  # the owner at the node still looks
        pass
    await manager.hand_back(LANE, by="owner")

    agent = Holder(key="agent:codex", label="agent codex", identity="agent:codex")
    await manager.claim(LANE, agent, purpose="a job", wait_s=1)
    with pytest.raises(LaneBusy):  # another agent's claim keeps the member's act out
        async with manager.using(LANE, principal_kind=gate_kind(ctx), holder=caller(ctx)):
            pass
    await manager.release(LANE, agent)

    def frozen(lane_id, kind):
        raise Frozen(f"{lane_id} is frozen")

    manager.add_gate(frozen)
    with pytest.raises(Frozen):
        async with manager.using(LANE, principal_kind=gate_kind(ctx), holder=caller(ctx)):
            pass


async def test_the_grant_egress_list_holds_a_member_call(wired):
    fake, runtime, _ = wired
    ctx = member_ctx(runtime, member_call(fake, egress_hosts=["reddit.com"]))
    driver = egress.Driver.from_context(ctx)
    assert not driver.human and driver.lists == (("reddit.com",),)
    refused = egress.judge("https://evil.example/", "GET", navigation=True, sites=(), driver=driver, human=False)
    assert refused is not None and refused.rule == "grant_egress"
    assert egress.judge("https://reddit.com/", "GET", navigation=True, sites=(), driver=driver, human=False) is None


# ---- the member call token lives for its one call ---------------------------------------


async def test_a_task_started_during_the_call_loses_the_member_token_when_it_ends(wired):
    fake, runtime, _ = wired
    policy = runtime.policy
    token = member_call(fake)
    go, seen = asyncio.Event(), {}

    async def child():
        seen["inside"] = policy.resolve(token)[0].id
        await go.wait()
        try:
            policy.resolve(token)
            seen["after"] = "resolved"
        except TokenInvalid:
            seen["after"] = "refused"

    with policy.channel_call(token):
        task = asyncio.create_task(child())  # what a tool's background work does
        await asyncio.sleep(0)
    go.set()
    await task
    assert seen == {"inside": MEMBER, "after": "refused"}


async def test_a_controller_clock_offset_does_not_stretch_a_member_call(wired):
    fake, runtime, _ = wired
    policy = runtime.policy
    token = member_call(fake, ttl=3000)
    base = policy.clock
    policy.clock = lambda: base() + 2700  # a welcome's offset, under the channel's 3600 s clamp
    try:
        with policy.channel_call(token), pytest.raises(TokenInvalid, match="lives at most"):
            policy.resolve(token)
    finally:
        policy.clock = base
    # issued long ago, within its exp: still more than one call's life from its issue
    old = member_call(fake, ttl=200, iat=int(time.time()) - 600)
    with policy.channel_call(old), pytest.raises(TokenInvalid, match="lives at most"):
        policy.resolve(old)


async def test_a_member_call_issued_in_the_future_is_refused(wired):
    fake, runtime, _ = wired
    future = member_call(fake, ttl=300, iat=int(time.time()) + 10**6)
    with runtime.policy.channel_call(future), pytest.raises(TokenInvalid, match="issued in the future"):
        runtime.policy.resolve(future)
    slightly = member_call(fake, ttl=300, iat=int(time.time()) + 20)  # inside the clock skew
    with runtime.policy.channel_call(slightly):
        assert runtime.policy.resolve(slightly)[0].id == MEMBER


async def test_the_clock_skew_never_extends_a_member_call_past_its_expiry(wired):
    fake, runtime, _ = wired
    assert runtime.policy.cfg.clock_skew_s >= 30
    lapsed = member_call(fake, ttl=-5)
    with runtime.policy.channel_call(lapsed), pytest.raises(Denied, match="expired"):
        runtime.policy.resolve(lapsed)


async def test_a_bearer_scheme_is_read_the_same_by_the_mark_and_resolve(wired):
    fake, runtime, peer = wired
    token = member_call(fake)
    with runtime.policy.channel_call("Bearer " + token):
        assert runtime.policy.resolve("Bearer " + token)[0].id == MEMBER
        assert runtime.policy.resolve(token)[0].id == MEMBER
    body = await invoke(fake, peer, "browser_snapshot", {"on": LANE}, "Bearer " + token)
    assert body["ok"], body


async def test_a_member_call_narrowed_into_an_agent_is_refused(wired):
    fake, runtime, peer = wired
    token = member_call(fake, child={"sub": {"kind": "agent", "id": f"{MEMBER}/bot", "owner": MEMBER}})
    with runtime.policy.channel_call(token), pytest.raises(TokenInvalid, match="cannot be narrowed into"):
        runtime.policy.resolve(token)
    body = await invoke(fake, peer, "browser_snapshot", {"on": LANE}, token)
    assert not body["ok"] and "cannot be narrowed into" in body["error"]["message"], body


# ---- an approved member call resumes with the retry's token -----------------------------


async def test_an_approved_member_call_runs_with_the_retrys_fresh_token(wired):
    fake, runtime, peer = wired
    runtime.policy.cfg.clock_skew_s = 0  # exp is exact here
    gid, rid = new_id("gr"), new_id("rv")  # the cloud's stable per-person grant id
    first = member_call(fake, ceiling="commit", ttl=2, gid=gid, rid=rid)
    body = as_member(invoke_body(fake, "post_note", {"on": LANE, "text": "hi"}, token=first))
    sent = await peer.send("invoke", body)
    pending = (await peer.reply_to(sent.id)).body
    assert pending["error"]["code"] == "approval_pending", pending
    await asyncio.sleep(2.5)  # the person approves after the first token lapsed
    runtime.approvals.decide(pending["error"]["approval_id"], "approve", by="owner")
    retry = dict(body, grant_token=member_call(fake, ceiling="commit", ttl=40, gid=gid, rid=rid))
    again = await peer.send("invoke", retry)
    result = (await peer.reply_to(again.id)).body
    assert result["ok"], result


async def test_a_retry_token_of_another_family_does_not_resume_the_call(wired):
    fake, runtime, peer = wired
    runtime.policy.cfg.clock_skew_s = 0
    first = member_call(fake, ceiling="commit", ttl=2)
    body = as_member(invoke_body(fake, "post_note", {"on": LANE, "text": "hi"}, token=first))
    sent = await peer.send("invoke", body)
    pending = (await peer.reply_to(sent.id)).body
    await asyncio.sleep(2.5)
    runtime.approvals.decide(pending["error"]["approval_id"], "approve", by="owner")
    other = member_call(fake, ceiling="commit", ttl=40)  # a new root: not the call that waited
    again = await peer.send("invoke", dict(body, grant_token=other))
    result = (await peer.reply_to(again.id)).body
    assert not result["ok"] and "expired" in result["error"]["message"], result
