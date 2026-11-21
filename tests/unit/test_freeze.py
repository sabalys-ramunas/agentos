"""The freeze service on a node: the gate, interrupts in flight, the revocation sweep, runs, schedules,
triggers and brain tasks, durability across a restart, and the failure cases (openajar.freeze)."""

from __future__ import annotations

import asyncio
import errno
import os
import sqlite3
import time

import pytest
from freeze_support import (
    FakeBrains,
    FakeEvents,
    FakeRunner,
    close,
    make_node,
    route,
    run,
    schedule,
    started,
    task,
)
from testkit.timing import budget, until

from openajar.core import signing
from openajar.core.errors import AgentPaused, Frozen, InvalidRequest, LaneNotFound
from openajar.core.models import LaneState
from openajar.freeze.service import FreezeService
from openajar.node import human, interrupts
from openajar.policy.errors import Denied
from openajar.policy.freeze_store import FreezeRecord, FreezeStore, parse_key, scope_key

EM_DASH = "—"


@pytest.fixture
async def node(tmp_path):
    n = await started(make_node(tmp_path))
    yield n
    await close(n)


async def blocked(mgr, lane_id, kind="agent") -> str:
    with pytest.raises(Frozen) as e:
        async with mgr.using(lane_id, principal_kind=kind):
            pass
    return e.value.message


# ---- the gate ---------------------------------------------------------------------------------


async def test_a_lane_freeze_stops_agents_flows_and_brains_but_not_a_human(node):
    async with node.lanes.using("biz-a/one", principal_kind="agent"):
        pass  # warm
    report = await node.freeze.freeze("lane", "biz-a/one", reason="the page asked for the password", by="owner")
    assert report["scope"] == "lane" and report["target"] == "biz-a/one" and not report["already_frozen"]
    assert report["durable"] and report["lanes_warm"] == ["biz-a/one"]
    for kind in ("agent", "flow", "brain"):
        message = await blocked(node.lanes, "biz-a/one", kind)
        assert "frozen" in message and "the page asked for the password" in message and EM_DASH not in message
    with pytest.raises(Frozen):
        await node.lanes.open("biz-a/one")
    with pytest.raises(Frozen):
        await node.lanes.close("biz-a/one")  # an agent cannot close it either: the browser is kept
    async with node.lanes.using("biz-a/two", principal_kind="agent"):
        pass  # another lane is untouched
    async with node.lanes.using("biz-a/one", principal_kind="human") as page:
        assert page is not None  # a human takes over and looks
    info = await node.lanes.take_control("biz-a/one", by="owner")
    assert info.control.state.value == "human"
    assert node.lanes.get("biz-a/one").state in (LaneState.WARM, LaneState.ACTIVE)
    assert not any(s.closed for s in node.engine.sessions if s.lane_id == "biz-a/one")


async def test_a_frozen_lane_is_never_reaped_or_evicted(tmp_path):
    n = await started(make_node(tmp_path, max_warm_lanes=2, idle_seconds=1))
    try:
        for lane in ("biz-a/one", "biz-a/two"):
            async with n.lanes.using(lane, principal_kind="agent"):
                pass
        await n.freeze.freeze("lane", "biz-a/one", reason="inspect", by="owner")
        n.lanes.clock.advance(3600)
        closed = await n.lanes.reap_idle()
        assert "biz-a/one" not in closed and "biz-a/two" in closed
        async with n.lanes.using("biz-a/two", principal_kind="agent"):
            pass
        async with n.lanes.using("biz-b/three", principal_kind="agent"):
            pass  # needs a slot: the frozen lane is never the victim
        assert n.lanes.get("biz-a/one").state == LaneState.WARM
    finally:
        await close(n)


async def test_workspace_and_node_scopes(node):
    await node.freeze.freeze("workspace", "biz-a", reason="r", by="owner")
    await blocked(node.lanes, "biz-a/one")
    await blocked(node.lanes, "biz-a/two")
    async with node.lanes.using("biz-b/three", principal_kind="agent"):
        pass
    await node.freeze.freeze("node", None, reason="everything", by="owner")
    await blocked(node.lanes, "biz-b/three")
    with pytest.raises(Frozen):
        node.freeze.check_call(None, tool="brain_run")  # no lane: a node freeze still refuses it
    node.freeze.check_call(None, tool="browser_freeze")  # the off switch stays callable
    assert {r["scope"] for r in node.freeze.status()["frozen"]} == {"workspace", "node"}


async def test_freezing_again_is_harmless_and_unknown_targets_are_refused(node):
    first = await node.freeze.freeze("lane", "biz-a/one", reason="one", by="owner")
    second = await node.freeze.freeze("lane", "biz-a/one", reason="two", by="owner")
    assert second["already_frozen"] and second["freeze_id"] == first["freeze_id"] and second["reason"] == "one"
    assert len(node.freeze.records()) == 1
    with pytest.raises(LaneNotFound):
        await node.freeze.freeze("lane", "biz-a/nope", reason="r", by="owner")
    with pytest.raises(InvalidRequest):
        await node.freeze.freeze("planet", "x", reason="r", by="owner")
    with pytest.raises(InvalidRequest):
        await node.freeze.freeze("node", "another-node", reason="r", by="owner")


# ---- in flight --------------------------------------------------------------------------------


async def test_work_in_flight_is_interrupted_and_reported(node):
    entered = asyncio.Event()

    async def agent_work():
        async with node.lanes.using("biz-a/one", principal_kind="agent"):
            entered.set()
            await asyncio.sleep(30)  # a long wait inside the call: the next await is a safe point

    async def human_work():
        async with node.lanes.using("biz-a/two", principal_kind="human"):
            await asyncio.sleep(0.3)
            return "done"

    a = asyncio.create_task(agent_work())
    h = asyncio.create_task(human_work())
    await entered.wait()
    t0 = time.monotonic()
    report = await node.freeze.freeze("workspace", "biz-a", reason="stop", by="owner")
    assert time.monotonic() - t0 < budget(2.0)  # the agent's call would sleep 30 s
    with pytest.raises(Frozen):
        await a
    assert report["stopped"]["calls"] == 1 and report["stopped"]["still_running"] == 0
    assert report["stopped"]["ms"] < budget(2.0) * 1000
    assert await h == "done"  # a human's work is never interrupted
    assert not node.lanes.agent_tasks("biz-a/one")


async def test_a_call_waiting_for_the_lane_never_opens_it_after_the_freeze(node):
    hold = asyncio.Event()

    async def first():
        async with node.lanes.using("biz-a/one", principal_kind="human"):
            await hold.wait()

    async def second():
        async with node.lanes.using("biz-a/one", principal_kind="agent"):
            return "acted"

    f = asyncio.create_task(first())
    await asyncio.sleep(0.05)
    s = asyncio.create_task(second())
    await asyncio.sleep(0.05)
    await node.freeze.freeze("lane", "biz-a/one", reason="r", by="owner")
    hold.set()
    await f
    with pytest.raises(Frozen):
        await s


async def test_a_wedged_call_does_not_hold_the_freeze_up(tmp_path):
    n = await started(make_node(tmp_path, stop_wait_s=0.2))
    try:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def wedged():
            async with n.lanes.using("biz-a/one", principal_kind="agent"):
                entered.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    await release.wait()  # a browser call that will not let go
                    raise

        w = asyncio.create_task(wedged())
        await entered.wait()
        t0 = time.monotonic()
        report = await n.freeze.freeze("lane", "biz-a/one", reason="r", by="owner")
        assert time.monotonic() - t0 < budget(1.5)  # the wedged call never lets go on its own
        assert report["stopped"]["still_running"] == 1 and "wedged" in report["stopped"]["note"]
        await blocked(n.lanes, "biz-a/one")  # nothing new starts meanwhile
        release.set()
        with pytest.raises(Frozen):
            await w
    finally:
        await close(n)


async def test_a_click_is_never_cut_in_half():
    events = []

    class Mouse:
        async def down(self):
            events.append("down")

        async def up(self):
            events.append("up")

    class Page:
        mouse = Mouse()

        async def wait_for_timeout(self, ms):
            await asyncio.sleep(0.2)

    async def clicking():
        await asyncio.shield(human._press_release(Page()))

    t = asyncio.create_task(clicking())
    await asyncio.sleep(0.05)
    assert interrupts.interrupt(t, "frozen")
    with pytest.raises(asyncio.CancelledError):
        await t
    assert interrupts.take(t) == "frozen"  # the owner of the call sees the interrupt, not a bare cancel
    assert await until(lambda: events == ["down", "up"])


async def test_an_interrupt_never_hides_another_cancellation():
    async def sleeper():
        await asyncio.sleep(30)

    t = asyncio.create_task(sleeper())
    await asyncio.sleep(0)
    interrupts.interrupt(t, "frozen")
    t.cancel()  # a shutdown at the same moment
    with pytest.raises(asyncio.CancelledError):
        await t
    assert interrupts.take(t) is None


async def test_an_interrupt_after_an_unfreeze_still_stops_with_agent_paused(node):
    entered = asyncio.Event()

    async def agent_work():
        async with node.lanes.using("biz-a/one", principal_kind="agent"):
            entered.set()
            await asyncio.sleep(30)

    a = asyncio.create_task(agent_work())
    await entered.wait()
    node.lanes.interrupt_agents("biz-a/one", "test stop")
    with pytest.raises(AgentPaused) as e:
        await a
    assert "test stop" in e.value.message


# ---- grants -----------------------------------------------------------------------------------


async def test_a_lane_freeze_revokes_only_the_grants_whose_whole_reach_is_the_lane(node):
    p = node.policy
    lane_g, lane_tok = node.agent(lanes=["biz-a/one"])
    ws_g, ws_tok = node.agent(workspaces=["biz-a"], sub="agent-ws")
    glob_g, _ = node.agent(workspaces=["biz-*"], sub="agent-glob")
    two_g, _ = node.agent(lanes=["biz-a/one", "biz-a/two"], sub="agent-two-lanes")
    other_lane, other_tok = node.agent(lanes=["biz-a/two"], sub="agent-two")
    other_ws, _ = node.agent(workspaces=["biz-b"], sub="agent-b")
    member = p.issue_member_grant("m_alice", "biz-a", "operator")
    child = p.mint_child_token(p.resolve(lane_tok)[0], purpose="brain-task", workspace="biz-a", lanes=["biz-a/one"])
    # narrowed from a wide grant to the frozen lane alone: its whole reach is inside, so it goes
    wide_child = p.mint_child_token(p.resolve(ws_tok)[0], purpose="b1", workspace="biz-a", lanes=["biz-a/one"])
    narrowed = p.mint_child_token(p.resolve(other_tok)[0], purpose="b2", workspace="biz-a", lanes=["biz-a/two"])
    sa = p.approvals.add_standing(effects=["commit"], lanes=["biz-a/one"], until=time.time() + 3600,
                                  by="owner")
    sb = p.approvals.add_standing(effects=["commit"], workspaces=["biz-b"], until=time.time() + 3600,
                                  by="owner")

    report = await node.freeze.freeze("lane", "biz-a/one", reason="r", by="owner")
    revoked = {g["id"] for g in report["revoked"]["grants"]}
    assert revoked == {lane_g.id, wide_child["grant_id"]}
    assert child["grant_id"] in report["revoked"]["cascades_to"]
    assert report["revoked"]["standing"] == [sa.id]
    states = {g["id"]: g["state"] for g in p.list_grants(include_inactive=True)}
    for gid in (lane_g.id, child["grant_id"], wide_child["grant_id"]):
        assert states[gid] == "revoked"
    for gid in (ws_g.id, glob_g.id, two_g.id, other_lane.id, other_ws.id, member.id, narrowed["grant_id"]):
        assert states[gid] == "active"  # they reach lanes outside the freeze
    assert {s.id for s in p.approvals.list_standing()} == {sb.id}
    with pytest.raises(Denied):
        p.resolve(lane_tok)
    with pytest.raises(Frozen):  # a new child grant for a frozen lane is refused
        p.mint_child_token(p.resolve(other_tok)[0], purpose="b3", workspace="biz-a", lanes=["biz-a/one"])
    assert "reached only that were revoked" in node.freeze.message("biz-a/one", node.freeze.records()[0])

    await node.freeze.unfreeze("lane", "biz-a/one", by="owner")
    with pytest.raises(Denied):
        p.resolve(lane_tok)  # never restored: agents must be granted again


def _call(policy, token: str, lane: str | None) -> None:
    principal, grant = policy.resolve(token)
    policy.authorize_call(policy.context(grant, principal=principal, tool="t"), "read", lane_id=lane)


async def test_a_frozen_lane_takes_no_call_from_any_grant_wide_or_narrow_until_unfrozen(node):
    p = node.policy
    toks = {
        "lane": node.agent(lanes=["biz-a/one"], sub="a-lane")[1],
        "two lanes": node.agent(lanes=["biz-a/one", "biz-a/two"], sub="a-two")[1],
        "workspace": node.agent(workspaces=["biz-a"], sub="a-ws")[1],
        "glob": node.agent(workspaces=["biz-*"], sub="a-glob")[1],
        "everything": node.agent(workspaces=["*"], sub="a-star")[1],
    }
    await node.freeze.freeze("lane", "biz-a/one", reason="stop here", by="owner")
    # made during the freeze: one wider than the lane is allowed, one for the lane alone is not
    toks["new wide"] = node.agent(workspaces=["biz-a"], sub="a-new")[1]
    with pytest.raises(Frozen, match="reaches only that"):
        node.agent(lanes=["biz-a/one"], sub="a-new-lane")
    toks["new lane (older node)"] = node.agent(lanes=["biz-a/one"], sub="a-old", during_freeze=True)[1]
    for name, tok in toks.items():
        if name == "lane":
            with pytest.raises(Denied):  # revoked by the sweep
                _call(p, tok, "biz-a/one")
            continue
        with pytest.raises(Frozen, match="stop here"):
            _call(p, tok, "biz-a/one")
    with pytest.raises(Frozen), node.freeze.guard(tool="browser_act", lane_id="biz-a/one", chain=("gr_any",)):
        pass  # the MCP guard, whatever the grant
    for kind in ("agent", "flow", "brain"):
        await blocked(node.lanes, "biz-a/one", kind)
    for name in ("two lanes", "workspace", "glob", "new wide"):
        _call(p, toks[name], "biz-a/two")  # the rest of their reach keeps working
    _call(p, toks["everything"], "biz-b/three")

    await node.freeze.unfreeze("lane", "biz-a/one", by="owner")
    for name in ("two lanes", "workspace", "glob", "everything", "new wide", "new lane (older node)"):
        _call(p, toks[name], "biz-a/one")
    with pytest.raises(Denied):
        _call(p, toks["lane"], "biz-a/one")
    async with node.lanes.using("biz-a/one", principal_kind="agent"):
        pass


async def test_a_workspace_freeze_revokes_what_stays_inside_it_and_stops_the_rest_there(node):
    p = node.policy
    ws_g, ws_tok = node.agent(workspaces=["biz-a"], sub="w-ws")
    lanes_g, _ = node.agent(lanes=["biz-a/one", "biz-a/two"], sub="w-lanes")
    both_g, both_tok = node.agent(workspaces=["biz-a", "biz-b"], sub="w-both")
    glob_g, glob_tok = node.agent(workspaces=["biz-*"], sub="w-glob")
    report = await node.freeze.freeze("workspace", "biz-a", reason="ws", by="owner")
    assert {g["id"] for g in report["revoked"]["grants"]} == {ws_g.id, lanes_g.id}
    states = {g["id"]: g["state"] for g in p.list_grants(include_inactive=True)}
    assert states[both_g.id] == states[glob_g.id] == "active"
    with pytest.raises(Denied):
        _call(p, ws_tok, "biz-a/one")
    for tok in (both_tok, glob_tok):
        for lane in ("biz-a/one", "biz-a/two"):
            with pytest.raises(Frozen):
                _call(p, tok, lane)
        _call(p, tok, "biz-b/three")
    # a call that names the workspace and no lane (memory, vault) is refused there too
    with pytest.raises(Frozen):
        node.freeze.check_call(None, tool="memory_write", workspace="biz-a")
    with pytest.raises(Frozen), node.freeze.guard(tool="memory_write", lane_id=None, workspace="biz-a"):
        pass
    node.freeze.check_call(None, tool="memory_write", workspace="biz-b")
    await node.freeze.unfreeze("workspace", "biz-a", by="owner")
    _call(p, both_tok, "biz-a/one")
    node.freeze.check_call(None, tool="memory_write", workspace="biz-a")


async def test_memory_of_a_frozen_workspace_is_closed_to_a_kept_wide_grant(node):
    from openajar.core.models import Effect
    from openajar.policy.grants import MemoryCap, Specials

    p = node.policy
    _g, tok = p.issue(subject={"id": "mem-wide"}, workspaces=["biz-a", "biz-b"], ceiling=Effect.COMMIT,
                      specials=Specials(memory=MemoryCap()))
    principal, grant = p.resolve(tok)
    ctx = p.context(grant, principal=principal, tool="memory_search")
    p.memory_rules.caller(ctx, "biz-a")
    await node.freeze.freeze("workspace", "biz-a", reason="ws", by="owner")
    principal, grant = p.resolve(tok)  # kept: it reaches biz-b too
    ctx = p.context(grant, principal=principal, tool="memory_search")
    with pytest.raises(Frozen):
        p.memory_rules.caller(ctx, "biz-a")
    p.memory_rules.caller(ctx, "biz-b")


async def test_a_node_freeze_still_revokes_every_agent_grant(node):
    toks = [node.agent(lanes=["biz-a/one"], sub="n-lane")[1], node.agent(workspaces=["*"], sub="n-star")[1],
            node.agent(sub="n-none")[1]]
    member = node.policy.issue_member_grant("m_alice", "biz-a", "operator")
    report = await node.freeze.freeze("node", None, reason="all", by="owner")
    assert len(report["revoked"]["grants"]) == 3
    await node.freeze.unfreeze("node", None, by="owner")
    for tok in toks:
        with pytest.raises(Denied):
            node.policy.resolve(tok)
    states = {g["id"]: g["state"] for g in node.policy.list_grants(include_inactive=True)}
    assert states[member.id] == "active"


async def test_a_policy_database_that_cannot_write_does_not_stop_the_freeze(node, monkeypatch):
    def broken(**kw):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(node.policy, "revoke_reaching", broken)
    report = await node.freeze.freeze("lane", "biz-a/one", reason="r", by="owner")
    assert any("could not be revoked" in e for e in report["errors"])
    await blocked(node.lanes, "biz-a/one")
    principal, grant = node.policy.resolve(node.agent(lanes=["biz-a/one"], during_freeze=True)[1])
    with pytest.raises(Frozen):  # and the policy itself refuses agents on it
        node.policy.authorize_call(node.policy.context(grant, principal=principal), "read", lane_id="biz-a/one")


async def test_the_policy_refuses_agents_on_a_frozen_lane_but_not_a_person_in_person(node):
    _g, tok = node.agent(workspaces=["biz-b"])
    await node.freeze.freeze("lane", "biz-a/one", reason="r", by="owner")
    owner = node.policy.owner_context(tool="panel.lane.open")
    node.policy.authorize_call(owner, "read", lane_id="biz-a/one")  # the owner in the panel looks
    principal, grant = node.policy.resolve(tok)
    ctx = node.policy.context(grant, principal=principal, tool="t")
    node.policy.authorize_call(ctx, "read", lane_id="biz-b/three")
    await node.freeze.freeze("node", None, reason="r", by="owner")
    with pytest.raises(Denied):
        node.policy.authorize_call(ctx, "read", lane_id=None)  # its grant was revoked by the node freeze
    with pytest.raises(Frozen, match="no new agent grant"):  # none is made during the freeze
        node.agent(workspaces=["biz-b"], sub="granted-after")
    principal, grant = node.policy.resolve(node.agent(workspaces=["biz-b"], sub="granted-after", during_freeze=True)[1])
    with pytest.raises(Frozen):  # and one an older node made then is still refused
        node.policy.authorize_call(node.policy.context(grant, principal=principal), "read", lane_id=None)


# ---- runs, schedules, triggers, brain tasks ------------------------------------------------------


async def test_runs_are_cancelled_and_schedules_triggers_and_brain_tasks_paused(tmp_path):
    runner = FakeRunner(runs=[run("r1", "biz-a/one"), run("r2", "biz-a/two"), run("r3", "biz-a/one", "succeeded")],
                        schedules=[schedule("s1", "biz-a/one"), schedule("s2", "biz-a/two"),
                                   schedule("s3", None, flow="biz-a.x.post"), schedule("s4", None, flow="biz-b.y")])
    events = FakeEvents([route("rt1", "biz-a", target_lane="biz-a/one"), route("rt2", "biz-a", target_lane="biz-a/two"),
                         route("rt3", "biz-a", grant_lanes=["biz-a/*"]), route("rt4", "biz-b")])
    brains = FakeBrains([task("t1", "biz-a", ["biz-a/one"]), task("t2", "biz-a", ["biz-a/two"]),
                         task("t3", "biz-a", ["biz-a/one"], "failed")])
    n = await started(make_node(tmp_path, extra={"runner": runner, "event_queue": events, "brains": brains}))
    try:
        report = await n.freeze.freeze("lane", "biz-a/one", reason="r", by="owner")
        assert report["runs_cancelled"] == ["r1"] and runner.cancelled == ["r1"]
        assert sorted(report["schedules_paused"]) == ["s1", "s3"]  # s3 names no lane, but is in the workspace
        assert sorted(report["triggers_paused"]) == ["rt1", "rt3"]
        assert report["brain_tasks_cancelled"] == ["t1"]
        await n.freeze.unfreeze("lane", "biz-a/one", by="owner")
        assert all(enabled is False for _, enabled in runner.toggled)  # nothing resumes by itself
        report = await n.freeze.freeze("node", None, reason="r", by="owner")
        assert set(report["schedules_paused"]) == {"s2", "s4"} and set(report["triggers_paused"]) == {"rt2", "rt4"}
    finally:
        await close(n)


# ---- durable --------------------------------------------------------------------------------------


async def test_a_frozen_lane_is_still_frozen_after_a_restart(tmp_path):
    n = await started(make_node(tmp_path))
    await n.freeze.freeze("lane", "biz-a/one", reason="before the reboot", by="owner")
    await close(n)
    again = await started(make_node(tmp_path))
    try:
        assert "before the reboot" in await blocked(again.lanes, "biz-a/one")
        assert again.freeze.records()[0].applied_at is not None
        await again.freeze.start()  # nothing pending: no second sweep
    finally:
        await close(again)


async def test_a_freeze_written_by_the_cli_is_applied_before_anything_runs(tmp_path):
    store = FreezeStore(tmp_path)
    store.put(FreezeRecord("workspace", "biz-a", "frozen offline", "owner (node cli)", time.time(), via="cli"))
    n = make_node(tmp_path)
    await started(n)
    try:
        await blocked(n.lanes, "biz-a/one")  # the gate was closed at build time
        g, _ = n.agent(workspaces=["biz-a"], during_freeze=True)
        await n.freeze.start()  # the pending sweep runs now
        assert n.freeze.records()[0].applied_at is not None
        assert {x["id"]: x["state"] for x in n.policy.list_grants(include_inactive=True)}[g.id] == "revoked"
    finally:
        await close(n)


async def test_the_daemon_notices_a_freeze_and_an_unfreeze_on_disk(tmp_path):
    n = await started(make_node(tmp_path))
    n.freeze.poll_s = 0.05
    await n.freeze.start()
    try:
        FreezeStore(tmp_path).put(FreezeRecord("lane", "biz-a/two", "cli", "owner (node cli)", time.time()))
        await asyncio.sleep(0.3)
        await blocked(n.lanes, "biz-a/two")
        FreezeStore(tmp_path).remove(scope_key("lane", "biz-a/two"))
        await asyncio.sleep(0.3)
        async with n.lanes.using("biz-a/two", principal_kind="agent"):
            pass
    finally:
        await close(n)


async def test_unfreeze_lifts_only_its_scope_and_says_what_still_covers_it(node):
    await node.freeze.freeze("workspace", "biz-a", reason="ws", by="owner")
    await node.freeze.freeze("lane", "biz-a/one", reason="lane", by="owner")
    out = await node.freeze.unfreeze("lane", "biz-a/one", by="owner")
    assert out["unfrozen"] and out["still_frozen_by"]["scope"] == "workspace"
    await blocked(node.lanes, "biz-a/one")
    out = await node.freeze.unfreeze("workspace", "biz-a", by="owner")
    assert out["still_frozen_by"] is None and "Nothing was restored" in out["next"]
    async with node.lanes.using("biz-a/one", principal_kind="agent"):
        pass
    assert (await node.freeze.unfreeze("workspace", "biz-a", by="owner"))["was_frozen"] is False


async def test_freeze_and_unfreeze_are_journaled_and_traced(node):
    await node.freeze.freeze("lane", "biz-a/one", reason="journal me", by="owner")
    await node.freeze.unfreeze("lane", "biz-a/one", by="owner")
    events = [e["event"] for e in node.freeze.store.read_journal()]
    assert events == ["freeze", "unfreeze"]
    node.policy.traces.flush()
    kinds = [t.kind for t in node.policy.traces.query(limit=50)[0]]
    assert "freeze" in kinds and "unfreeze" in kinds
    traced = next(t for t in node.policy.traces.query(limit=50)[0] if t.kind == "freeze")
    assert traced.principal == "owner" and "journal me" in traced.summary and traced.lane == "biz-a/one"


# ---- the store, toward frozen ------------------------------------------------------------------


def test_marker_names_round_trip():
    for scope, target in (("lane", "biz-a/one"), ("workspace", "biz-a"), ("node", "node"), ("mesh", "mesh")):
        assert parse_key(scope_key(scope, target)) == (scope, target)
    assert parse_key("lane.a.b.c") is None and parse_key("workspace.Bad_Name") is None


def test_an_unreadable_marker_is_still_a_freeze(tmp_path):
    store = FreezeStore(tmp_path)
    store.path("lane.biz-a.one").write_text("{cut sho")
    store.path("workspace.biz-b").write_text("")
    records, problem = store.load()
    assert problem is None
    assert not records["lane.biz-a.one"].readable and "details were lost" in records["lane.biz-a.one"].reason
    assert "empty marker" in records["workspace.biz-b"].reason


def test_a_marker_this_version_does_not_understand_freezes_the_node(tmp_path):
    store = FreezeStore(tmp_path)
    (store.dir / "galaxy.far-away.frozen").write_text("{}")
    records, problem = store.load()
    assert "node" in records and problem


def test_a_directory_that_cannot_be_read_freezes_the_node(tmp_path, monkeypatch):
    store = FreezeStore(tmp_path)

    def denied(self):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(type(store.dir), "iterdir", denied)
    records, problem = store.load()
    assert list(records) == ["node"] and "cannot be read" in problem


def test_a_full_disk_gives_up_the_reserve_then_an_empty_marker(tmp_path, monkeypatch):
    store = FreezeStore(tmp_path)
    assert (store.dir / ".reserve").stat().st_size > 0
    real_write = os.write
    calls = {"n": 0}

    def full(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", full)
    durable, saved = store.put(FreezeRecord("lane", "biz-a/one", "r", "owner", time.time()))
    assert (durable, saved) == (True, True) and not (store.dir / ".reserve").exists()

    monkeypatch.setattr(os, "write", lambda fd, data: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full")))
    durable, saved = store.put(FreezeRecord("lane", "biz-a/two", "r", "owner", time.time()))
    assert (durable, saved) == (True, False)  # an empty marker: frozen, details lost
    records, _ = store.load()
    assert "lane.biz-a.two" in records and not records["lane.biz-a.two"].readable


async def test_a_disk_that_refuses_everything_still_freezes_in_memory(tmp_path, monkeypatch):
    n = await started(make_node(tmp_path))
    try:
        monkeypatch.setattr(n.freeze.store, "put", lambda rec: (False, False))
        report = await n.freeze.freeze("lane", "biz-a/one", reason="r", by="owner")
        assert report["durable"] is False and "restarts" in report["warning"]
        await blocked(n.lanes, "biz-a/one")
        n.freeze.reload()  # the next directory read does not lose it
        await blocked(n.lanes, "biz-a/one")
    finally:
        await close(n)


async def test_unfreeze_that_cannot_delete_the_marker_stays_frozen(node, monkeypatch):
    await node.freeze.freeze("lane", "biz-a/one", reason="r", by="owner")

    def stuck(key):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(node.freeze.store, "remove", stuck)
    with pytest.raises(InvalidRequest) as e:
        await node.freeze.unfreeze("lane", "biz-a/one", by="owner")
    assert "stays frozen" in e.value.message
    await blocked(node.lanes, "biz-a/one")


def test_a_service_without_lanes_or_policy_still_gates(tmp_path):
    from openajar.core.config import NodeConfig
    from openajar.core.runtime import Runtime

    svc = FreezeService(Runtime(config=NodeConfig(home=tmp_path, name="n1")))
    asyncio.run(svc.freeze("workspace", "w", reason="r", by="owner"))
    with pytest.raises(Frozen):
        svc.gate("w/x")


async def test_the_sweep_result_is_saved_even_after_the_directory_was_read_again(node):
    rec, _ = node.freeze.mark("lane", "biz-a/one", reason="r", by="owner")
    node.freeze.reload()  # the poll reads the directory between the flag and the sweep
    assert node.freeze.records()[0] is rec
    await node.freeze.freeze("lane", "biz-a/one", reason="r", by="owner")
    on_disk = FreezeStore(node.config.home).load()[0]["lane.biz-a.one"]
    assert on_disk.applied_at is not None


# ---- grants a trusted controller issued (QA-freeze-2) -------------------------------------------------


def _org_node(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from openajar.policy import tokens
    from openajar.policy.grants import GrantBlock
    from openajar.policy.issuers import SERVICE_NAME, TrustedIssuers
    from openajar.policy.util import new_id

    org = Ed25519PrivateKey.generate()
    issuers = TrustedIssuers(tmp_path)
    issuers.trust("org:test", signing.public_b64(org))

    def mint(sub: str, kind: str = "agent", **scope) -> str:
        block = {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "iat": int(time.time()),
                 "sub": {"kind": kind, "id": sub, "owner": "owner"}, "ceiling": "draft",
                 "exp": int(time.time()) + 600, "depth": 2, **scope}
        return tokens.mint(org, GrantBlock.model_validate(block).to_data())

    return make_node(tmp_path, extra={SERVICE_NAME: issuers}), issuers, mint


def _works(policy, token: str, lane: str = "biz-a/one") -> bool:
    try:
        principal, grant = policy.resolve(token)
        policy.authorize_call(policy.context(grant, principal=principal, tool="t"), "read", lane_id=lane)
    except (Denied, Frozen):
        return False
    return True


async def test_a_lane_freeze_keeps_controller_grants_that_reach_beyond_it(tmp_path):
    n, issuers, mint = _org_node(tmp_path)
    n = await started(n)
    try:
        here, other = mint("helper", lanes=["biz-a/one"]), mint("elsewhere", workspaces=["personal"])
        wide = mint("wide", workspaces=["biz-a"])
        assert _works(n.policy, here) and _works(n.policy, other, "personal/reddit") and _works(n.policy, wide)
        report = await n.freeze.freeze("lane", "biz-a/one", reason="r", by="owner")
        assert not _works(n.policy, wide) and _works(n.policy, wide, "biz-a/two")  # kept, but not on the lane
        await n.freeze.unfreeze("lane", "biz-a/one", by="owner")
        assert [g["subject"] for g in report["revoked"]["grants"]] == ["agent:helper"]
        assert not _works(n.policy, here) and _works(n.policy, other, "personal/reddit") and _works(n.policy, wide)
    finally:
        await close(n)
        issuers.close()


async def test_a_node_freeze_revokes_every_controller_grant_and_those_seen_before_chains_were_kept(tmp_path):
    n, issuers, mint = _org_node(tmp_path)
    n = await started(n)
    try:
        lanes_none, elsewhere = mint("no-lanes"), mint("elsewhere", workspaces=["personal"])
        assert _works(n.policy, elsewhere, "personal/reddit")
        n.policy.resolve(lanes_none)
        issuers._db.execute("DELETE FROM chains")  # as a node upgraded today remembers them
        issuers._chains.clear()
        await n.freeze.freeze("node", None, reason="stop", by="owner")
        await n.freeze.unfreeze("node", None, by="owner")
        assert not _works(n.policy, elsewhere, "personal/reddit")
        with pytest.raises(Denied):
            n.policy.resolve(lanes_none)
    finally:
        await close(n)
        issuers.close()
