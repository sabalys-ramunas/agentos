"""An approved run of a source with a slot hook waits for that slot (the engagement engine's next
session), and a session can take queued runs into its claim."""

from datetime import timedelta

from events_support import World, check_flow

from agentos.core.models import RunStatus
from agentos.runner.fakes import FakeApprovals, FakeCtx, FakePolicy
from agentos.runner.tools import flow_run


async def _approved(tmp_path, hook):
    w = World(tmp_path, policy=False)
    approvals = FakeApprovals()
    w.runtime.register("approvals", approvals)
    fv = w.h.save({**check_flow(), "effect": "commit"})
    out = await flow_run(FakeCtx(w.runtime, policy=FakePolicy(approvals)), name=fv.id)
    assert out["status"] == "waiting_approval"
    run = w.h.runner.get(out["id"])
    if hook is not None:
        w.h.runner.approved_slot[run.source] = hook
    approvals.set(out["approval_id"], "approved")
    await w.h.runner.poll_approvals()
    return w, w.h.runner.get(out["id"])


async def test_an_approved_run_waits_for_the_slot_its_source_names(tmp_path):
    seen = []

    def hook(run, now):
        seen.append(run.id)
        return now + timedelta(hours=9)

    w, run = await _approved(tmp_path, hook)
    assert seen == [run.id] and run.status == RunStatus.QUEUED
    assert run.not_before == w.clock.now() + timedelta(hours=9)
    await w.h.runner.run_until_idle()
    assert w.h.runner.get(run.id).status == RunStatus.QUEUED          # not before its slot


async def test_a_slot_in_the_past_or_none_goes_now_and_a_broken_hook_waits_a_day(tmp_path):
    _, now_run = await _approved(tmp_path / "a", lambda run, now: None)
    assert now_run.status == RunStatus.QUEUED and now_run.not_before is None

    def broken(run, now):
        raise RuntimeError("no plan")

    w, late = await _approved(tmp_path / "b", broken)
    assert late.not_before == w.clock.now() + timedelta(days=1)


async def test_a_session_takes_queued_runs_into_its_claim(tmp_path):
    w, run = await _approved(tmp_path, lambda run, now: now + timedelta(hours=1))
    assert w.h.runner.adopt([run.id, "run_missing"], parent="claim:engage:s:abc") == [run.id]
    assert w.h.runner.get(run.id).parent == "claim:engage:s:abc"


async def test_the_gate_lets_a_run_go_only_when_it_says_so(tmp_path):
    w, run = await _approved(tmp_path, None)
    asked = []

    def gate(r, now):
        asked.append(r.id)

    w.h.runner.approved_gate[run.source] = gate
    await w.h.runner.run_until_idle()
    assert asked and w.h.runner.get(run.id).status != RunStatus.QUEUED


async def test_a_run_the_gate_moves_waits_out_of_its_old_session(tmp_path):
    w, run = await _approved(tmp_path, None)
    w.h.runner.adopt([run.id], parent="claim:engage:s:abc")
    slot = w.clock.now() + timedelta(hours=7)
    w.h.runner.approved_gate[run.source] = lambda r, now: {"until": slot, "reason": "it missed its session"}
    await w.h.runner.run_until_idle()
    moved = w.h.runner.get(run.id)
    assert moved.status == RunStatus.QUEUED and moved.not_before == slot and moved.parent is None
    assert moved.error["control"] == "approved_slot" and moved.error["message"] == "it missed its session"
    assert moved.deferrals == moved.held                     # waiting for a slot never counts toward giving up

    class Control:
        def __init__(self, state):
            self.state = state

    w.h.runner.on_lane_control(moved.lane, Control("human"), Control("agent"))
    assert w.h.runner.get(run.id).not_before == slot         # a hand back does not pull it forward


async def test_a_held_run_keeps_when_its_hold_began_and_the_gate_can_end_it(tmp_path):
    w, run = await _approved(tmp_path, None)
    seen = []

    def gate(r, now):
        seen.append((r.error or {}).get("held_since"))
        return {"until": now + timedelta(hours=1), "reason": "held", "hold": True}

    w.h.runner.approved_gate[run.source] = gate
    await w.h.runner.dispatch_once()
    began = w.h.runner.get(run.id).error["held_since"]
    w.clock.set(w.clock.now() + timedelta(hours=2))
    await w.h.runner.dispatch_once()
    assert w.h.runner.get(run.id).error["held_since"] == began and seen[-1] == began
    w.h.runner.approved_gate[run.source] = lambda r, now: {"end": "engage_held", "reason": "held for a week"}
    w.clock.set(w.clock.now() + timedelta(hours=2))
    await w.h.runner.dispatch_once()
    ended = w.h.runner.get(run.id)
    assert ended.status == RunStatus.CANCELLED and ended.error == {"code": "engage_held", "message": "held for a week"}


async def test_a_broken_gate_never_lets_a_run_go(tmp_path):
    w, run = await _approved(tmp_path, None)

    def broken(r, now):
        raise RuntimeError("no plan")

    w.h.runner.approved_gate[run.source] = broken
    await w.h.runner.dispatch_once()
    held = w.h.runner.get(run.id)
    assert held.status == RunStatus.QUEUED and held.not_before == w.clock.now() + timedelta(hours=1)


async def test_a_gate_that_keeps_failing_ends_the_run_with_a_clear_status(tmp_path):
    from agentos.runner.runner import GATE_FAILURES_MAX

    w, run = await _approved(tmp_path, None)

    def broken(r, now):
        raise RuntimeError("no plan")

    w.h.runner.approved_gate[run.source] = broken
    for _ in range(GATE_FAILURES_MAX):
        await w.h.runner.dispatch_once()
        w.clock.set(w.clock.now() + timedelta(hours=1, seconds=1))
    ended = w.h.runner.get(run.id)
    assert ended.status == RunStatus.CANCELLED and ended.error["code"] == "gate_failed"
