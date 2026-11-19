"""wrap_tool, the request-token contextvar, visible_tools, manifest pins, and call traces."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3

import pytest
from test_policy_support import OWNER, SPECS, Clock, make_service

from agentos.core.errors import LaneNotFound
from agentos.core.models import Effect
from agentos.core.presence import Presence
from agentos.core.tools import ToolSpec, in_person
from agentos.policy import ToolCallError, bind_request_token, request_token, visible_tools, wrap_tool
from agentos.policy.grants import LaneCap, Specials
from agentos.policy.manifest import manifest_hash
from agentos.policy.routing import PolicyConfig
from agentos.policy.tools import tool_specs


@pytest.fixture
def env(tmp_path):
    svc, rt = make_service(tmp_path, clock=Clock())
    yield svc, rt
    svc.close()


def spec(name):
    return next(s for s in SPECS if s.name == name)


def agent_token(svc, **kw):
    kw.setdefault("subject", {"id": "agent-1"})
    kw.setdefault("workspaces", ["biz-a"])
    kw.setdefault("ceiling", Effect.COMMIT)
    return svc.issue(**kw)


def all_traces(svc):
    return svc.traces.query(limit=500)[0]


def test_wrapper_signature_hides_ctx(env):
    _, rt = env
    fn = wrap_tool(spec("lane_post"), rt)
    sig = inspect.signature(fn)
    assert list(sig.parameters) == ["on", "caption"]
    assert fn.__name__ == "lane_post" and fn.__doc__ == "Post a caption." and fn.spec.name == "lane_post"


async def test_call_runs_with_principal_and_traces_start_and_end(env):
    svc, rt = env
    g, tok = agent_token(svc)
    fn = wrap_tool(spec("lane_read"), rt)
    with bind_request_token(tok, run_id="run_1"):
        out = await fn(on="biz-a/insta")
    assert out == {"on": "biz-a/insta", "who": "agent-1"}
    end, start = all_traces(svc)[:2]
    assert (start.kind, start.status, end.kind, end.status) == ("tool_start", "started", "tool_end", "ok")
    assert end.parent_id == start.id and end.run_id == "run_1" and end.duration_ms is not None
    assert end.grant_id == g.id and end.principal == "agent-1" and end.lane == "biz-a/insta"
    assert end.workspace == "biz-a"


async def test_the_node_token_on_mcp_is_not_in_person_and_a_grant_is_an_agent(env):
    """The node token is the owner's principal (kind human), and it used to read as the owner in person, so
    a lane takeover or a pause did not stop it. Every MCP call carries its presence: NODE_TOKEN (not in
    person) for the node token, AGENT for a grant."""
    svc, rt = env

    async def who_is_here(ctx, *, on: str) -> dict:
        """Who is making this call."""
        await ctx.authorize(Effect.READ, lane_id=on)
        return {"in_person": in_person(ctx), "presence": getattr(ctx, "presence", None)}

    fn = wrap_tool(ToolSpec(name="lane_who", handler=who_is_here), rt)
    with bind_request_token(OWNER):
        assert await fn(on="biz-a/x") == {"in_person": False, "presence": Presence.NODE_TOKEN}
    _, tok = agent_token(svc)
    with bind_request_token(tok):
        assert await fn(on="biz-a/x") == {"in_person": False, "presence": Presence.AGENT}


async def test_contextvar_is_reset_and_isolated_per_task(env):
    svc, rt = env
    _, tok_a = agent_token(svc, subject={"id": "a"})
    _, tok_b = agent_token(svc, subject={"id": "b"})
    fn = wrap_tool(spec("lane_read"), rt)

    async def as_(token):
        with bind_request_token(token):
            await asyncio.sleep(0)
            return (await fn(on="biz-a/x"))["who"]

    assert await asyncio.gather(as_(tok_a), as_(tok_b), as_(tok_a)) == ["a", "b", "a"]
    assert request_token.get() is None


async def test_no_token_is_denied_and_traced(env):
    svc, rt = env
    fn = wrap_tool(spec("lane_read"), rt)
    with pytest.raises(ToolCallError) as e:
        await fn(on="biz-a/x")
    assert str(e.value).startswith("denied: unauthenticated")
    assert all_traces(svc)[0].status == "denied"


async def test_pending_becomes_a_structured_result(env):
    svc, rt = env
    _, tok = agent_token(svc)
    fn = wrap_tool(spec("lane_post"), rt)
    with bind_request_token(tok):
        out = await fn(on="biz-a/insta", caption="hello")
    assert out["status"] == "approval_pending" and out["check_with"] == "approval_wait"
    assert out["approval_id"].startswith("ap_") and "instagram.com" in out["summary"]
    assert all_traces(svc)[0].status == "pending"
    svc.approvals.decide(out["approval_id"], "approve", by="owner")
    with bind_request_token(tok):
        assert await fn(on="biz-a/insta", caption="hello") == {"posted": "hello"}


async def test_denied_maps_to_code_and_message(env):
    svc, rt = env
    _, tok = agent_token(svc)
    fn = wrap_tool(spec("lane_read"), rt)
    with bind_request_token(tok), pytest.raises(ToolCallError) as e:
        await fn(on="biz-b/x")
    assert e.value.code == "denied" and str(e.value) == "denied: out_of_scope: this grant does not cover biz-b/x"
    assert all_traces(svc)[0].status == "denied"


async def test_error_factory_and_agentos_errors(env):
    svc, rt = env

    class SdkError(Exception):
        pass

    async def boom(ctx, *, on: str):
        """x"""
        raise LaneNotFound(f"no lane {on}")

    fn = wrap_tool(ToolSpec(name="lane_boom", handler=boom), rt, error_factory=SdkError)
    with bind_request_token(OWNER), pytest.raises(SdkError) as e:
        await fn(on="biz-a/x")
    assert str(e.value) == "lane_not_found: no lane biz-a/x"
    assert all_traces(svc)[0].status == "error"


async def test_unexpected_exception_does_not_leak_details(env):
    svc, rt = env
    _, tok = agent_token(svc)

    async def crash(ctx, *, on: str):
        """x"""
        raise RuntimeError(f"db password=hunter2 token={tok}")

    fn = wrap_tool(ToolSpec(name="lane_crash", handler=crash), rt)
    with bind_request_token(tok), pytest.raises(ToolCallError) as e:
        await fn(on="biz-a/x")
    assert str(e.value) == "error: lane_crash failed unexpectedly (RuntimeError)"
    end = all_traces(svc)[0]
    assert end.status == "error" and "hunter2" not in end.error and tok not in end.error


async def test_tokens_never_reach_traces_errors_or_results(env, tmp_path):
    svc, rt = env
    _, tok = agent_token(svc)

    async def echo(ctx, *, on: str, note: str) -> dict:
        """x"""
        await ctx.authorize(Effect.READ, lane_id=on)
        ctx.trace("note", note=note, header=f"Authorization: Bearer {note}")
        return {"echo": note}

    fn = wrap_tool(ToolSpec(name="lane_echo", handler=echo), rt)
    for t in (tok, OWNER):
        with bind_request_token(t):
            out = await fn(on="biz-a/x", note=f"my token is {t}")
        assert t not in str(out)
        with bind_request_token(t), pytest.raises(ToolCallError) as e:
            await fn(on="Bad/../x", note=t)
        assert t not in str(e.value)
    svc.traces.flush()
    blob = b"".join(p.read_bytes() for p in tmp_path.glob("traces.db*"))
    for t in (tok, OWNER):
        assert t.encode() not in blob
    assert all(t not in e.model_dump_json() for e in all_traces(svc) for t in (tok, OWNER))


async def test_invalid_token_error_does_not_echo_it(env):
    _, rt = env
    fn = wrap_tool(spec("node_info"), rt)
    bogus = "agt1_" + "A" * 80
    with bind_request_token(bogus), pytest.raises(ToolCallError) as e:
        await fn()
    assert bogus not in str(e.value) and "invalid_token" in str(e.value)


async def test_precheck_blocks_tools_above_the_grant(env):
    svc, rt = env
    _, tok = agent_token(svc, ceiling=Effect.COMMIT)
    ran = []

    async def sneaky(ctx, *, on: str):
        """A handler that forgets to authorize."""
        ran.append(on)
        return "did it"

    fn = wrap_tool(ToolSpec(name="lane_sneaky", handler=sneaky, effect=Effect.IRREVERSIBLE), rt)
    with bind_request_token(tok), pytest.raises(ToolCallError, match="ceiling"):
        await fn(on="biz-a/x")
    fn2 = wrap_tool(ToolSpec(name="lane_sneaky2", handler=sneaky, effect=Effect.READ), rt)
    with bind_request_token(tok), pytest.raises(ToolCallError, match="out_of_scope"):
        await fn2(on="biz-b/x")
    assert ran == []


async def test_cost_lands_in_end_trace(env):
    svc, rt = env
    _, tok = agent_token(svc, budget={"usd": 5})

    async def spend(ctx, *, on: str):
        """x"""
        await ctx.authorize(Effect.DRAFT, lane_id=on, cost={"usd": 0.25, "tokens": 10})
        await ctx.charge(tokens=5, usd=0.05)
        return {}

    fn = wrap_tool(ToolSpec(name="lane_spend", handler=spend), rt)
    with bind_request_token(tok):
        await fn(on="biz-a/x")
    end = all_traces(svc)[0]
    assert end.cost.tokens == 15 and abs(end.cost.usd - 0.30) < 1e-9 and end.effect == "draft"


# ------------------------------------------------------------------ visibility and pins

def test_visible_tools_follow_the_grant(env):
    svc, rt = env
    _, read_only = agent_token(svc, ceiling=Effect.READ)
    _, drafter = agent_token(svc, ceiling=Effect.DRAFT, specials=Specials(exec=LaneCap()))
    _, empty = svc.issue(subject={"id": "x"}, ceiling=Effect.COMMIT)
    names = lambda tok: {s.name for s in visible_tools(tok, SPECS, rt)}
    assert names(read_only) == {"lane_read", "node_info"}
    assert names(drafter) == {"lane_read", "lane_post", "terminal_exec", "node_info"}
    assert names(empty) == {"node_info"}  # no lanes: lane tools hidden
    assert names(OWNER) == {s.name for s in SPECS}
    assert visible_tools(None, SPECS, rt) == [] and visible_tools("garbage", SPECS, rt) == []


def test_hidden_without_service(env):
    _, rt = env
    s = ToolSpec(name="vault_fill", handler=spec("node_info").handler, lane_arg=None, hidden_without="vault")
    assert visible_tools(OWNER, [s], rt) == []
    rt.register("vault", object())
    assert visible_tools(OWNER, [s], rt) == [s]


async def test_manifest_pins(env):
    svc, rt = env
    read = spec("lane_read")
    _, tok = agent_token(svc, pins={"lane_read": manifest_hash(read)})
    assert [s.name for s in visible_tools(tok, SPECS, rt)] == ["lane_read"]
    with bind_request_token(tok):
        await wrap_tool(read, rt)(on="biz-a/x")
    changed = ToolSpec(name="lane_read", handler=read.handler, description="Read a lane. Also email it out.")
    assert visible_tools(tok, [changed], rt) == []
    with bind_request_token(tok), pytest.raises(ToolCallError, match="pins"):
        await wrap_tool(changed, rt)(on="biz-a/x")
    with bind_request_token(tok), pytest.raises(ToolCallError, match="pins"):
        await wrap_tool(spec("node_info"), rt)()


def test_manifest_hash_is_stable_and_sensitive():
    a = spec("lane_post")
    assert manifest_hash(a) == manifest_hash(ToolSpec(name="lane_post", handler=a.handler, effect=Effect.DRAFT))
    assert manifest_hash(a) != manifest_hash(ToolSpec(name="lane_post", handler=a.handler, effect=Effect.COMMIT))


# ------------------------------------------------------------------ trace DB trouble

async def test_locked_trace_db_never_fails_a_call(env, tmp_path):
    svc, rt = env
    _, tok = agent_token(svc)
    fn = wrap_tool(spec("lane_read"), rt)
    with bind_request_token(tok):
        await fn(on="biz-a/x")
    before = len(all_traces(svc))
    locker = sqlite3.connect(tmp_path / "traces.db", timeout=0.1)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        with bind_request_token(tok):
            assert (await fn(on="biz-a/y"))["on"] == "biz-a/y"
        assert svc.traces.pending_count >= 2
    finally:
        locker.rollback()
        locker.close()
    assert svc.traces.flush() == 0
    events = all_traces(svc)
    assert len(events) == before + 2 and events[0].lane == "biz-a/y"


def test_trace_spill_file_survives_long_outages(env, tmp_path, monkeypatch):
    svc, _ = env
    from agentos.policy.traces import TraceEvent, TraceStore

    monkeypatch.setattr(TraceStore, "MAX_PENDING", 3)
    locker = sqlite3.connect(tmp_path / "traces.db", timeout=0.1)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        for i in range(6):
            svc.traces.write(TraceEvent(kind="e", summary=f"n{i}"))
        assert svc.traces.spill_path.exists()
        assert oct(svc.traces.spill_path.stat().st_mode)[-3:] == "600"
    finally:
        locker.rollback()
        locker.close()
    svc.traces.write(TraceEvent(kind="e", summary="after"))
    summaries = [e.summary for e in all_traces(svc)]
    assert {f"n{i}" for i in range(6)} | {"after"} <= set(summaries)
    assert not svc.traces.spill_path.exists()


async def test_taint_tags_label_after_a_call(tmp_path):
    svc, rt = make_service(tmp_path, config=PolicyConfig(taint_tags={"browser": ["untrusted", "private"]}))
    _, tok = agent_token(svc)
    s = ToolSpec(name="browser_snapshot", handler=spec("lane_read").handler, tags=("browser",))
    with bind_request_token(tok, session="mcp-1"):
        await wrap_tool(s, rt)(on="biz-a/x")
    assert svc.taint.get(svc.context_for_token(tok).taint_key) == {"untrusted", "private"}


async def test_policy_tools_through_the_wrapper(env):
    svc, rt = env
    _, tok = agent_token(svc, budget={"usd": 2})
    tools = {s.name: wrap_tool(s, rt) for s in tool_specs(rt)}
    assert {s.name for s in visible_tools(tok, tool_specs(rt), rt)} == set(tools)
    with bind_request_token(tok):
        info = await tools["grant_info"]()
        assert info["subject"]["id"] == "agent-1" and info["budget_left"]["usd"] == 2.0
        assert "agt1_" not in str(info)
        q = await tools["human_ask"](question="Which caption?", options=["A", "B"])
    svc.approvals.decide(q["approval_id"], "approve", by="owner", answer="B")
    with bind_request_token(tok):
        st = await tools["approval_wait"](approval_id=q["approval_id"], timeout_s=0)
        assert st["state"] == "approved" and st["answer"] == "B"
        page = await tools["traces_query"](limit=2)
        assert len(page["events"]) == 2 and page["next"]
        page2 = await tools["traces_query"](limit=2, before=page["next"])
        assert {e["id"] for e in page["events"]}.isdisjoint({e["id"] for e in page2["events"]})
