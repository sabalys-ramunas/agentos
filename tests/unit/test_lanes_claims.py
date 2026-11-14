"""Shared lanes (agentos.node.claims): reads share, acts take turns in a FIFO queue, a claim keeps
other callers' acts out for a multi-step job, and every way a claim ends without its holder is told
to the holder on its next call.

The real LaneManagerImpl over the fakes of test_lanes.py (no browser, no X).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from test_lanes import FakeEngine
from test_lanes_holds import again, build
from test_lanes_resources import Memory

from agentos.api.lanes_view import lane_json
from agentos.core import logs
from agentos.core.errors import ClaimLost, HumanInControl, LaneBusy
from agentos.core.models import ActionKind, LaneSpec, LaneState, Principal
from agentos.core.runtime import Runtime
from agentos.node import tools
from agentos.node.claims import READ, RUN, Holder
from agentos.node.flow_driver import _using

LANE = "biz-a/social"


def agent(name: str, session: str = "s1", **kw) -> Holder:
    return Holder.for_principal(Principal(kind=kw.pop("kind", "agent"), id=name, grant_id=f"g_{name}"),
                                session=session, **kw)


A, B, C = agent("claude-code"), agent("codex"), agent("third")
BRAIN = agent("brain-task-7", kind="brain")


async def lane(tmp_path, **kw) -> SimpleNamespace:
    ids = kw.pop("lanes", (LANE,))
    t = build(tmp_path, queue_timeout=kw.pop("queue_timeout", 0.4), **kw)
    for lid in ids:
        await t.mgr.create(LaneSpec(id=lid))
    return t


async def act(mgr, holder: Holder, log: list | None = None, tag: str = "", hold: asyncio.Event | None = None,
              mode: str = "act", lane_id: str = LANE) -> None:
    async with mgr.using(lane_id, holder=holder, mode=mode):
        if log is not None:
            log.append(f"in {tag}")
        if hold is not None:
            await hold.wait()
        else:
            await asyncio.sleep(0.01)
        if log is not None:
            log.append(f"out {tag}")


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ---- reader/writer without a claim ---------------------------------------------------------


async def test_reads_share_the_lane_and_acts_take_turns(tmp_path):
    t = await lane(tmp_path)
    inside, most = 0, 0

    async def read(h):
        nonlocal inside, most
        async with t.mgr.using(LANE, holder=h, mode=READ):
            inside += 1
            most = max(most, inside)
            await asyncio.sleep(0.03)
            inside -= 1

    await asyncio.gather(read(A), read(B), read(C))
    assert most == 3, "reads must share the lane"

    most = 0

    async def write(h):
        nonlocal inside, most
        async with t.mgr.using(LANE, holder=h):
            inside += 1
            most = max(most, inside)
            await asyncio.sleep(0.02)
            inside -= 1

    await asyncio.gather(write(A), write(B), read(C))
    assert most == 1, "an act never shares the lane, not even with a read"
    assert len(t.engine.launches) == 1


async def test_the_queue_is_fifo_and_a_read_never_overtakes_an_earlier_act(tmp_path):
    t = await lane(tmp_path)
    log: list[str] = []
    gate = asyncio.Event()
    first = asyncio.create_task(act(t.mgr, A, log, "a1", hold=gate))
    await settle()
    later = [asyncio.create_task(act(t.mgr, B, log, "b-act")), asyncio.create_task(act(t.mgr, C, log, "c-read",
                                                                                       mode=READ)),
             asyncio.create_task(act(t.mgr, A, log, "a2"))]
    await settle()
    q = t.mgr.queue_info(LANE)
    assert [w["mode"] for w in q["queue"]] == ["act", "read", "act"] and q["queue_length"] == 3
    assert [w["position"] for w in q["queue"]] == [1, 2, 3]
    gate.set()
    await asyncio.gather(first, *later)
    assert [x.split()[1] for x in log if x.startswith("in")] == ["a1", "b-act", "c-read", "a2"]


# ---- claims --------------------------------------------------------------------------------


async def test_a_claim_keeps_other_acts_out_and_lets_their_reads_in(tmp_path):
    t = await lane(tmp_path, queue_timeout=5)
    out = await t.mgr.claim(LANE, A, purpose="posting a reply", ttl_s=60)
    assert out["holder"] == "agent claude-code" and out["purpose"] == "posting a reply" and out["lease"]
    log: list[str] = []
    other = asyncio.create_task(act(t.mgr, B, log, "b-act"))
    await settle()
    await act(t.mgr, B, log, "b-read", mode=READ)  # a read passes another caller's claim
    for step in range(3):  # the holder's multi-step job is never interleaved
        await act(t.mgr, A, log, f"a{step}")
    assert "in b-act" not in log
    q = t.mgr.queue_info(LANE, viewer=B)
    assert q["claim"]["holder"] == "agent claude-code" and q["claim"]["yours"] is False
    assert q["queue"][0]["kept_out_by_claim"] and q["queue"][0]["yours"]
    released = await t.mgr.release(LANE, A)
    assert released["released"] is True
    await other
    assert log[-2:] == ["in b-act", "out b-act"]
    assert t.mgr.queue_info(LANE)["claim"] is None


async def test_a_claim_is_the_agents_on_any_of_its_sessions_and_never_another_agents(tmp_path):
    t = await lane(tmp_path, queue_timeout=0.3)
    await t.mgr.claim(LANE, A, purpose="a post", ttl_s=60)
    again = agent("claude-code", session="s2")  # the same grant, reconnected
    await act(t.mgr, again)  # passes its own claim at once
    assert t.mgr.queue_info(LANE, viewer=again)["claim"]["yours"] is True
    with pytest.raises(LaneBusy):
        await act(t.mgr, B)  # another agent still waits behind it, then gives up
    stranger = Holder.for_principal(Principal(kind="agent", id="claude-code", grant_id="g_other"), session="s1")
    with pytest.raises(LaneBusy):  # the same name on another grant is another agent
        await act(t.mgr, stranger)
    assert (await t.mgr.release(LANE, again))["released"] is True
    assert t.mgr.queue_info(LANE)["claim"] is None


async def test_claim_lost_reaches_the_agent_on_its_new_session(tmp_path):
    t = await lane(tmp_path)
    await t.mgr.claim(LANE, A, ttl_s=60)
    await t.mgr.take_control(LANE, by="owner")
    await t.mgr.hand_back(LANE, by="owner")
    with pytest.raises(ClaimLost):
        await act(t.mgr, agent("claude-code", session="s2"))
    await act(t.mgr, agent("claude-code", session="s3"))  # told once


async def test_busy_names_the_holder_since_when_and_the_place_never_a_token(tmp_path):
    t = await lane(tmp_path, queue_timeout=0.3)
    await t.mgr.claim(LANE, A, purpose="posting", ttl_s=60)
    with pytest.raises(LaneBusy) as e:
        await act(t.mgr, B)
    msg = e.value.message
    assert "agent claude-code has claimed it since 03:00:00 UTC (posting)" in msg
    assert "number 1 in its queue" in msg and "lane_queue" in msg
    assert e.value.details["holder"]["label"] == "agent claude-code" and e.value.details["position"] == 1
    assert "g_claude" not in msg and "s1" not in msg and "—" not in msg  # no grant id, no session
    assert t.mgr.queue_info(LANE)["queue_length"] == 0  # the caller that gave up left the queue

    # the same without a claim: an act in flight that outlasts the wait
    await t.mgr.release(LANE, A)
    gate = asyncio.Event()
    long = asyncio.create_task(act(t.mgr, C, hold=gate))
    await settle()
    with pytest.raises(LaneBusy, match="agent third is using it since"):
        await act(t.mgr, B)
    gate.set()
    await long


async def test_the_lane_json_shows_the_claim_and_the_queue_length(tmp_path):
    t = await lane(tmp_path, queue_timeout=5)
    await t.mgr.claim(LANE, A, purpose="posting", ttl_s=60)
    waiting = asyncio.create_task(act(t.mgr, B))
    await settle()
    shown = lane_json(t.mgr.get(LANE), hints=None, proxies={}, node="box-1")
    assert shown["claim"]["holder"] == "agent claude-code" and shown["claim"]["purpose"] == "posting"
    assert shown["claim"]["since"].startswith("2026-11-04T03:00") and shown["claim"]["expires_in_s"] == 60
    assert shown["queue_length"] == 1
    await t.mgr.release(LANE, A)
    await waiting
    shown = lane_json(t.mgr.get(LANE), hints=None, proxies={}, node="box-1")
    assert shown["claim"] is None and shown["queue_length"] == 0


async def test_the_lease_renews_on_each_call_and_expires_on_its_own(tmp_path):
    t = await lane(tmp_path)
    await t.mgr.claim(LANE, A, purpose="a long job", ttl_s=30)
    for _ in range(4):  # 4 x 20 s = 80 s, but every call renews the 30 s lease
        t.clock.advance(20)
        await act(t.mgr, A)
    assert t.mgr.queue_info(LANE)["claim"]["expires_in_s"] == 30
    t.clock.advance(31)  # the holder went quiet (it crashed or disconnected)
    assert await t.mgr.reap_claims() == [LANE]
    assert t.mgr.queue_info(LANE)["claim"] is None
    await act(t.mgr, B)  # others go on at once
    with pytest.raises(ClaimLost, match="no call from you for 30 s"):
        await act(t.mgr, A)  # the holder is told, once, and nothing was done
    await act(t.mgr, A)
    kinds = [e.kind for e in t.mgr.events(LANE)]
    assert "claimed" in kinds and "claim_ended" in kinds


async def test_a_disconnected_holder_lets_the_waiting_caller_in_when_its_lease_ends(tmp_path):
    t = await lane(tmp_path, queue_timeout=5)
    await t.mgr.claim(LANE, A, purpose="job", ttl_s=10)
    waiting = asyncio.create_task(act(t.mgr, B))
    await settle()
    assert not waiting.done()
    t.clock.advance(11)
    await t.mgr.reap_claims()  # the reaper runs every few seconds; a waiter also looks every second
    await asyncio.wait_for(waiting, 1)


async def test_the_starvation_guard_stops_renewals_while_others_wait(tmp_path):
    t = await lane(tmp_path, queue_timeout=30, claim_max_total_s=60)
    await t.mgr.claim(LANE, A, purpose="hog", ttl_s=20)
    for _ in range(3):  # 45 s held; nobody waits, so every call renews the lease
        t.clock.advance(15)
        await act(t.mgr, A)
    assert t.mgr.queue_info(LANE)["claim"]["expires_in_s"] == 20
    waiting = asyncio.create_task(act(t.mgr, B))
    await settle()
    t.clock.advance(10)  # 55 s held and others wait: no renewal past 60 s from the start
    await act(t.mgr, A)
    claim = t.mgr.queue_info(LANE)["claim"]
    assert claim["expires_in_s"] == 10 and claim["renewable_until_s"] == 5  # the lease it had, no more
    t.clock.advance(11)
    await t.mgr.reap_claims()
    await asyncio.wait_for(waiting, 1)
    with pytest.raises(ClaimLost, match="held for 60 s, the most"):
        await act(t.mgr, A)


async def test_two_claims_race_and_exactly_one_wins(tmp_path):
    t = await lane(tmp_path, queue_timeout=5)
    results = await asyncio.gather(t.mgr.claim(LANE, A, purpose="one", wait_s=0),
                                   t.mgr.claim(LANE, B, purpose="two", wait_s=0), return_exceptions=True)
    won = [r for r in results if isinstance(r, dict)]
    lost = [r for r in results if isinstance(r, LaneBusy)]
    assert len(won) == 1 and len(lost) == 1
    assert "has claimed it since" in lost[0].message
    # a claim that waits gets the lane next, ahead of acts that came after it
    winner = A if won[0]["holder"] == "agent claude-code" else B
    loser = B if winner is A else A
    second = asyncio.create_task(t.mgr.claim(LANE, loser, purpose="next", wait_s=5))
    await settle()
    late = asyncio.create_task(act(t.mgr, C))
    await settle()
    await t.mgr.release(LANE, winner)
    got = await second
    assert got["holder"] == loser.label and not late.done()
    await t.mgr.release(LANE, loser)
    await late


async def test_claiming_again_renews_and_a_claim_on_a_cold_lane_warms_it_once(tmp_path):
    t = await lane(tmp_path)
    assert t.mgr.get(LANE).state == LaneState.COLD
    first = await t.mgr.claim(LANE, A, purpose="job", ttl_s=30)
    assert first["state"] == "warm" and len(t.engine.launches) == 1
    again_ = await t.mgr.claim(LANE, A, purpose="job, part two", ttl_s=60)
    assert again_["renewed"] and again_["lease"] == first["lease"] and again_["purpose"] == "job, part two"
    for _ in range(3):
        await act(t.mgr, A)
    assert len(t.engine.launches) == 1


async def test_a_claimed_lane_is_never_closed_for_idleness_or_the_warm_cap(tmp_path):
    t = await lane(tmp_path, max_warm_lanes=1, busy_timeout=0.2, idle_seconds=60, lanes=(LANE, "biz-a/other"))
    await t.mgr.claim(LANE, A, purpose="job", ttl_s=900)
    t.clock.advance(120)
    assert await t.mgr.reap_idle() == []
    with pytest.raises(LaneBusy, match="warm lanes are busy"):
        await t.mgr.open("biz-a/other")  # the only warm lane is claimed: it is not evicted
    assert t.mgr.get(LANE).state == LaneState.WARM


async def test_a_full_memory_floor_still_closes_a_claimed_lane_and_tells_the_holder(tmp_path):
    engine = FakeEngine()
    t = await lane(tmp_path, engine=engine, memory_probe=Memory(engine, total=1300), memory_floor_mb=800,
                   lanes=(LANE, "biz-a/other"))
    await t.mgr.claim(LANE, A, purpose="job", ttl_s=900)
    await t.mgr.open("biz-a/other")  # 700 MB free: the claimed lane is the only one that can go
    assert t.mgr.get(LANE).state == LaneState.COLD and t.mgr.get("biz-a/other").state == LaneState.WARM
    with pytest.raises(ClaimLost, match="short of memory"):
        await act(t.mgr, A)


async def test_claims_are_gone_after_a_restart_and_the_holder_is_told_once(tmp_path):
    t = await lane(tmp_path)
    await t.mgr.start()
    await t.mgr.claim(LANE, A, purpose="job", ttl_s=900)
    await t.mgr.shutdown()
    mgr = again(t, queue_timeout=0.4)
    await mgr.start()
    assert mgr.queue_info(LANE)["claim"] is None
    await act(mgr, B)  # nobody holds it now
    reconnected = agent("claude-code", session="s2")  # the same agent on a new MCP session
    with pytest.raises(ClaimLost, match="the node restarted"):
        await act(mgr, reconnected)
    await act(mgr, reconnected)
    await mgr.shutdown()


async def test_a_human_taking_control_ends_the_claim(tmp_path):
    t = await lane(tmp_path)
    await t.mgr.claim(LANE, A, purpose="job", ttl_s=900)
    await t.mgr.take_control(LANE, by="owner")
    assert t.mgr.queue_info(LANE)["claim"] is None
    with pytest.raises(HumanInControl):
        await act(t.mgr, A)
    await t.mgr.hand_back(LANE, by="owner")
    with pytest.raises(ClaimLost, match="a human took control"):
        await act(t.mgr, A)  # the page may have changed under the job: told once
    await act(t.mgr, A)


async def test_a_take_over_while_waiting_in_the_queue_wins_at_once(tmp_path):
    t = await lane(tmp_path, queue_timeout=30)
    await t.mgr.claim(LANE, A, purpose="job", ttl_s=900)
    waiting = asyncio.create_task(act(t.mgr, B))
    await settle()
    await t.mgr.take_control(LANE, by="owner")
    with pytest.raises(HumanInControl):
        await asyncio.wait_for(waiting, 2)  # not after the 30 s queue timeout
    assert t.mgr.queue_info(LANE)["queue_length"] == 0


async def test_a_revoked_grant_ends_the_claim_at_its_next_renew(tmp_path):
    t = await lane(tmp_path, queue_timeout=5)
    revoked: list[str] = []
    holder = agent("claude-code", check=lambda: "your grant is revoked" if revoked else None)
    await t.mgr.claim(LANE, holder, purpose="job", ttl_s=900)
    await act(t.mgr, holder)
    waiting = asyncio.create_task(act(t.mgr, B))
    await settle()
    revoked.append("yes")
    await asyncio.wait_for(waiting, 2)  # the waiter's next look sees the grant is gone
    with pytest.raises(ClaimLost, match="your grant is revoked"):
        await act(t.mgr, holder)


async def test_a_cancelled_waiter_leaves_the_queue(tmp_path):
    t = await lane(tmp_path, queue_timeout=30)
    await t.mgr.claim(LANE, A, purpose="job", ttl_s=900)
    waiting = asyncio.create_task(act(t.mgr, B))
    await settle()
    assert t.mgr.queue_info(LANE)["queue_length"] == 1
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert t.mgr.queue_info(LANE)["queue_length"] == 0 and t.mgr._lanes[LANE].users == 0


# ---- flows, the brain and the freeze ---------------------------------------------------------


async def test_a_flow_run_claims_its_lane_and_the_node_brain_waits_or_reads(tmp_path):
    t = await lane(tmp_path, queue_timeout=5)
    gate, log = asyncio.Event(), []

    async def flow_run():
        with logs.log_context(run="run_42"):
            async with _using(t.mgr, LANE):  # what driver_factory enters for a flow or connector run
                log.append("flow in")
                await gate.wait()
                log.append("flow out")

    run = asyncio.create_task(flow_run())
    await settle()
    claim = t.mgr.queue_info(LANE)["claim"]
    assert claim["implicit"] and claim["run_id"] == "run_42" and claim["holder"] == "flow run run_42"
    await act(t.mgr, BRAIN, log, "brain-read", mode=READ)  # the brain may read during the run
    brain_act = asyncio.create_task(act(t.mgr, BRAIN, log, "brain-act"))
    await settle()
    assert not brain_act.done()
    with pytest.raises(LaneBusy):
        await t.mgr.claim(LANE, A, purpose="post", wait_s=0)  # nobody claims over a run
    gate.set()
    await asyncio.gather(run, brain_act)
    assert log == ["flow in", "in brain-read", "out brain-read", "flow out", "in brain-act", "out brain-act"]
    assert t.mgr.queue_info(LANE)["claim"] is None


async def test_a_flow_run_waits_for_the_brains_claim_then_fails_busy(tmp_path):
    t = await lane(tmp_path, queue_timeout=0.3)
    await t.mgr.claim(LANE, BRAIN, purpose="triage", ttl_s=900)
    with logs.log_context(run="run_43"), pytest.raises(LaneBusy, match="agent|brain brain-task-7 has claimed it"):
        async with _using(t.mgr, LANE):  # the runner turns busy into a deferral
            pass
    await t.mgr.release(LANE, BRAIN)
    with logs.log_context(run="run_43"):
        async with t.mgr.using(LANE, principal_kind="flow", holder=Holder.for_run("run_43"), mode=RUN):
            assert t.mgr.queue_info(LANE)["claim"]["run_id"] == "run_43"


def test_a_run_never_inherits_a_parent_from_an_outer_log_context():
    with logs.log_context(run="run_a", parent="claim:node:biz-a/social:x"):
        with logs.log_context(run="run_b", parent=None):
            assert logs.current()["parent"] == "claim:node:biz-a/social:x"     # plain merge keeps it
        with logs.log_context(drop=("parent",), run="run_b", parent=None):
            assert "parent" not in logs.current() and logs.current()["run"] == "run_b"
        with logs.log_context(drop=("parent",), run="run_c", parent="claim:own"):
            assert logs.current()["parent"] == "claim:own"
        assert logs.current()["parent"] == "claim:node:biz-a/social:x"
    with pytest.raises(ValueError), logs.log_context(drop=("nope",)):
        pass


async def test_the_runner_gives_a_run_only_its_own_parent():
    from agentos.runner.runner import RunnerService

    seen = []

    class Me:
        async def _execute_run(self, run):
            seen.append(logs.current().get("parent"))

    with logs.log_context(parent="claim:node:biz-a/social:x"):     # whatever context dispatched it
        await RunnerService._execute(Me(), SimpleNamespace(id="run_1", lane=LANE, grant_id=None, parent=None))
        await RunnerService._execute(Me(), SimpleNamespace(id="run_2", lane=LANE, grant_id=None, parent="claim:y"))
    assert seen == [None, "claim:y"]


async def test_a_freeze_ends_every_claim(tmp_path):
    from freeze_support import close, make_node, started

    node = await started(make_node(tmp_path))
    try:
        await node.lanes.claim("biz-a/one", A, purpose="job", ttl_s=900)
        await node.lanes.claim("biz-a/two", B, purpose="job", ttl_s=900)
        await node.freeze.freeze("workspace", "biz-a", reason="inspect", by="owner")
        assert node.lanes.queue_info("biz-a/one")["claim"] is None
        assert node.lanes.queue_info("biz-a/two")["claim"] is None
    finally:
        await close(node)


# ---- the tools -------------------------------------------------------------------------------


class Ctx:
    def __init__(self, runtime, principal, session="s1"):
        self.runtime, self.principal = runtime, principal
        self.meta = SimpleNamespace(session=session)

    async def authorize(self, *a, **kw):
        return None


async def test_the_claim_tools_end_to_end(tmp_path):
    t = await lane(tmp_path, queue_timeout=5)
    rt = Runtime(config=t.config)
    rt.register("lanes", t.mgr)
    me = Ctx(rt, Principal(id="claude-code", grant_id="g_1"))
    other = Ctx(rt, Principal(id="codex", grant_id="g_2"))
    out = await tools.lane_claim(me, on=LANE, purpose="posting a reply", ttl_s=60)
    assert out["lease"].startswith("lease_") and "lane_release" in out["next"]
    q = await tools.lane_queue(other, on=LANE)
    assert q["claim"]["yours"] is False and "Another caller holds the claim" in q["next"]
    assert (await tools.lane_queue(me, on=LANE))["claim"]["yours"] is True
    assert (await tools.lane_release(other, on=LANE))["released"] is False
    assert (await tools.lane_release(me, on=LANE))["released"] is True


class Spy:
    """A lane manager that records how each tool enters the lane."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def using(self, on, *, principal_kind="agent", holder=None, mode="act"):
        spy = self

        class Session:
            async def __aenter__(self):
                spy.calls.append((on, mode, holder.label if holder else ""))
                raise RuntimeError("stop here")

            async def __aexit__(self, *exc):
                return False

        return Session()


@pytest.mark.parametrize(("tool", "args", "mode"), [
    ("browser_snapshot", {}, "read"),
    ("browser_screenshot", {}, "read"),
    ("browser_wait", {"for_text": "Posted"}, "read"),
    ("browser_navigate", {"url": "https://example.com/"}, "act"),
    ("browser_act", {"ref": "e1", "action": ActionKind.CLICK}, "act"),
])
async def test_which_browser_tools_read_and_which_act(tool, args, mode):
    spy = Spy()
    rt = Runtime(lanes=spy, interaction=SimpleNamespace(wait=lambda *a, **kw: None))
    ctx = Ctx(rt, Principal(id="claude-code", grant_id="g_1"))
    with pytest.raises(RuntimeError, match="stop here"):
        await getattr(tools, tool)(ctx, on=LANE, **args)
    assert spy.calls == [(LANE, mode, "agent claude-code")]


@pytest.mark.parametrize(("tool", "args", "mode"), [
    ("browser_tabs", {}, "read"),
    ("browser_dialog", {}, "read"),
    ("browser_tab", {"index": 1}, "act"),
    ("browser_dialog_answer", {"accept": True}, "act"),
])
async def test_which_io_tools_read_and_which_act(tool, args, mode):
    from agentos.core.models import LaneInfo, LaneKind
    from agentos.node import io_tools

    spy = Spy()
    spy.get = lambda on: LaneInfo(spec=LaneSpec(id=on, kind=LaneKind.BROWSER))
    rt = Runtime(lanes=spy)
    ctx = Ctx(rt, Principal(id="claude-code", grant_id="g_1"))
    with pytest.raises(RuntimeError, match="stop here"):
        await getattr(io_tools, tool)(ctx, on=LANE, **args)
    assert spy.calls == [(LANE, mode, "agent claude-code")]


@pytest.mark.parametrize(("tool", "args"), [("browser_console", {}), ("browser_network", {}),
                                           ("browser_network", {"id": "r1"})])
async def test_the_devtools_tools_only_read(tool, args):
    from agentos.node import devtools_tools

    spy = Spy()
    ctx = Ctx(Runtime(lanes=spy), Principal(id="claude-code", grant_id="g_1"))
    with pytest.raises(RuntimeError, match="stop here"):
        await getattr(devtools_tools, tool)(ctx, on=LANE, **args)
    assert spy.calls == [(LANE, "read", "agent claude-code")]


async def test_the_grant_check_ends_a_claim_only_for_a_grant_that_is_no_longer_active(tmp_path):
    states = {"g_live": "active", "g_gone": "revoked"}

    def record(grant_id):
        if grant_id not in states:
            raise LookupError(grant_id)
        return {"state": states[grant_id]}

    rt = Runtime(policy=SimpleNamespace(grant_record=record))
    assert tools.grant_check(Ctx(rt, Principal(id="a", grant_id="g_live")))() is None
    assert tools.grant_check(Ctx(rt, Principal(id="a", grant_id="g_gone")))() == "your grant is revoked"
    assert tools.grant_check(Ctx(rt, Principal(id="a", grant_id="owner")))() is None  # no record: kept
    assert tools.grant_check(Ctx(Runtime(), Principal(id="a", grant_id="g_live"))) is None


async def test_the_claims_file_is_private_and_holds_no_session(tmp_path):
    import stat

    t = await lane(tmp_path)
    await t.mgr.claim(LANE, A, purpose="job", ttl_s=60)
    path = tmp_path / "run" / "claims.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    text = path.read_text()
    assert "claude-code" in text and "|s1" not in text  # the identity only, never the MCP session
    await t.mgr.release(LANE, A)
    assert not path.exists()
