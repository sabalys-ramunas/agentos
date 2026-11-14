"""The durable runner: journal, restarts, retries, approvals, queueing, cancel."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from agentos.core.driver import DriverTimeout
from agentos.core.errors import InvalidRequest, LaneBusy, PolicyDenied
from agentos.core.models import Effect, LaneState, RunStatus
from agentos.runner.fakes import (
    BASE,
    FakeApprovals,
    FakeDriver,
    FakeLanes,
    FakeNotifier,
    FakePolicy,
    Harness,
    SimulatedCrash,
    driver_factory,
    post_flow,
)

PARAMS = {"caption": "Autumn", "image": "files/a.jpg"}


def crash_once(op_name, *, after=False, when=lambda target: True):
    state = {"fired": False}

    def hook(op, target, d):
        if op == op_name and not state["fired"] and when(target):
            state["fired"] = True
            raise SimulatedCrash(f"node died during {op}")

    return hook, state


async def crash_run(h, fv_id, params=None, **kw):
    run = h.submit(fv_id, params if params is not None else PARAMS, **kw)
    started = await h.runner.dispatch_once()
    results = await asyncio.gather(*started, return_exceptions=True)
    assert any(isinstance(r, SimulatedCrash) for r in results)
    return run


def restart(h, **kw):
    """A new process on the same home: fresh runner, fresh browser (about:blank)."""
    return Harness(h.home, site=h.site, clock=h.clock, **kw)


async def test_restart_mid_idempotent_step_resumes_without_repeating_done_steps(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    hook, _ = crash_once("fill")
    h.driver.hooks.append(hook)
    run = await crash_run(h, fv.id)
    assert h.store.get_run(run.id).status == RunStatus.RUNNING  # the process "died"
    journal = {s.path: s.status for s in h.store.steps(run.id)}
    assert journal["4"] == "done" and journal["5"] == "started"

    h2 = restart(h)
    assert h2.runner.recover() == {"requeued": 1, "needs_decision": 0}
    await h2.runner.run_until_idle()
    done = h2.runner.get(run.id)
    assert done.status == RunStatus.SUCCEEDED_VERIFIED, done.error
    ops = [op for op, _ in h2.driver.log]
    assert "upload" not in ops and ("click", 'button "New post"') not in h2.driver.log  # done steps not repeated
    assert ("navigate", f"{BASE}/compose") in h2.driver.log  # put back where the last done step left it
    assert len(h.site.state["posts"]) == 1 and h.site.state["posts"][0]["caption"] == "Autumn"
    assert [rb["passed"] for rb in done.proof["readbacks"]] == [True, True]


async def test_restart_mid_non_idempotent_step_asks_for_a_decision(tmp_path):
    notifier = FakeNotifier()
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    hook, _ = crash_once("click", when=lambda el: el.name == "Share")
    h.driver.after_hooks.append(hook)  # the post went out, then the node died
    run = await crash_run(h, fv.id)
    assert len(h.site.state["posts"]) == 1

    h2 = restart(h, services={"notifier": notifier})
    assert h2.runner.recover() == {"requeued": 0, "needs_decision": 1}
    await asyncio.sleep(0)
    partial = h2.runner.get(run.id)
    assert partial.status == RunStatus.PARTIAL and partial.needs_decision
    assert partial.decision["needed"]["path"] == "7" and partial.error["code"] == "in_flight_unknown"
    assert "not safe to repeat" in partial.error["message"]
    assert notifier.sent and "decision" in notifier.sent[0]["title"]
    await h2.runner.run_until_idle()
    assert h2.runner.get(run.id).status == RunStatus.PARTIAL  # nothing runs until a human decides

    # the owner looked at the lane: the post is there, so "skip" (it happened)
    await h2.runner.decide(run.id, "skip", by="owner")
    await h2.runner.run_until_idle()
    final = h2.runner.get(run.id)
    assert final.status == RunStatus.SUCCEEDED_VERIFIED, final.error
    assert len(h.site.state["posts"]) == 1  # never posted twice
    with pytest.raises(InvalidRequest):
        await h2.runner.decide(run.id, "retry")


async def test_decision_retry_and_fail(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    hook, _ = crash_once("click", when=lambda el: el.name == "Share")
    h.driver.hooks.append(hook)  # died just before the click
    run = await crash_run(h, fv.id)
    h2 = restart(h)
    h2.runner.recover()
    await h2.runner.decide(run.id, "retry")
    await h2.runner.run_until_idle()
    assert h2.runner.get(run.id).status == RunStatus.SUCCEEDED_VERIFIED
    assert len(h.site.state["posts"]) == 1

    hook2, _ = crash_once("click", when=lambda el: el.name == "Share")
    h2.driver.hooks.append(hook2)
    run2 = await crash_run(h2, fv.id)
    h3 = restart(h2)
    h3.runner.recover()
    failed = await h3.runner.decide(run2.id, "fail")
    assert failed.status == RunStatus.FAILED


async def test_a_token_never_decides_a_run_whose_grant_is_unknown(tmp_path):
    """A run with no grant (a schedule under a non-owner principal) cannot be shown to sit outside the
    token's family, so a token does not decide it. The owner, in a panel session, does."""
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    hook, _ = crash_once("click", when=lambda el: el.name == "Share")
    h.driver.after_hooks.append(hook)  # the post went out, then the node died
    run = await crash_run(h, fv.id, principal={"id": "agent-1"})  # not the owner, and no grant of its own
    h2 = restart(h)
    h2.runner.recover()
    partial = h2.runner.get(run.id)
    assert partial.needs_decision and partial.grant_id is None
    with pytest.raises(PolicyDenied, match="self_approval"):
        await h2.runner.decide(run.id, "skip", by="agent-1", refuse_family="root-grant-of-the-token")
    assert h2.runner.get(run.id).needs_decision  # refused: nothing changed
    await h2.runner.decide(run.id, "skip", by="owner")
    assert h2.runner.get(run.id).decision["by"] == "owner"


async def test_transient_errors_retry_with_backoff(tmp_path):
    h = Harness(tmp_path, max_attempts=3, backoff_base_s=30)
    fv = h.save(post_flow())
    fails = {"left": 2}

    def flaky(op, target, d):
        if op == "find" and target.name == "New post" and fails["left"]:
            fails["left"] -= 1
            raise DriverTimeout("page slow")

    h.driver.hooks.append(flaky)
    run = await h.run(fv.id, PARAMS)
    assert run.status == RunStatus.QUEUED and run.attempts == 1 and run.error["retrying"]
    assert run.not_before > h.clock.now()
    await h.runner.run_until_idle()
    assert h.runner.get(run.id).status == RunStatus.QUEUED  # not due yet
    first_wait = (run.not_before - h.clock.now()).total_seconds()
    assert 15 <= first_wait <= 30
    h.clock.advance(31)
    await h.runner.run_until_idle()
    second = h.runner.get(run.id)
    assert second.status == RunStatus.QUEUED and second.attempts == 2
    assert 30 <= (second.not_before - h.clock.now()).total_seconds() <= 60  # exponential
    h.clock.advance(61)
    await h.runner.run_until_idle()
    final = h.runner.get(run.id)
    assert final.status == RunStatus.SUCCEEDED_VERIFIED and final.attempts == 3
    assert ("navigate", f"{BASE}/u/biz-a") in h.driver.log
    assert len(h.site.state["posts"]) == 1


async def test_retries_give_up(tmp_path):
    h = Harness(tmp_path, max_attempts=2)
    fv = h.save(post_flow())
    h.driver.hooks.append(lambda op, t, d: (_ for _ in ()).throw(DriverTimeout("down"))
                          if op == "find" and t.name == "New post" else None)
    run = await h.run(fv.id, PARAMS)
    h.clock.advance(3600)
    await h.runner.run_until_idle()
    final = h.runner.get(run.id)
    assert final.status == RunStatus.FAILED and "gave up after 2 attempts" in final.error["message"]


async def test_policy_denied_is_never_retried(tmp_path):
    policy = FakePolicy(deny={Effect.COMMIT})
    h = Harness(tmp_path, services={"policy": policy})
    fv = h.save(post_flow())
    run = await h.run(fv.id, PARAMS, authorized=False, principal={"kind": "flow", "id": "schedule:x"})
    assert run.status == RunStatus.FAILED and run.error["code"] == "denied" and run.attempts == 1
    h.clock.advance(86_400)
    await h.runner.run_until_idle()
    assert h.runner.get(run.id).attempts == 1
    assert h.site.state["posts"] == [] and len(policy.calls) == 1


async def test_approval_pending_for_hours_then_approved(tmp_path):
    approvals = FakeApprovals()
    policy = FakePolicy(approvals)
    h = Harness(tmp_path, services={"policy": policy, "approvals": approvals})
    fv = h.save(post_flow())
    run = await h.run(fv.id, PARAMS, authorized=False, principal={"kind": "flow", "id": "schedule:x"})
    assert run.status == RunStatus.WAITING_APPROVAL and run.approval_id == "ap_1"
    assert "biz-a.social.post" in approvals.items["ap_1"]["summary"]
    assert h.site.state["posts"] == []

    h.clock.advance(5 * 3600)
    assert await h.runner.poll_approvals() == 0
    assert h.runner.get(run.id).status == RunStatus.WAITING_APPROVAL

    approvals.set("ap_1", "approved")
    assert await h.runner.poll_approvals() == 1
    await h.runner.run_until_idle()
    final = h.runner.get(run.id)
    assert final.status == RunStatus.SUCCEEDED_VERIFIED
    assert len(policy.calls) == 1  # the approval is the co-sign; no second ask


async def test_approval_expired_denied_or_timed_out(tmp_path):
    approvals = FakeApprovals()
    h = Harness(tmp_path, services={"policy": FakePolicy(approvals), "approvals": approvals})
    fv = h.save(post_flow())
    r1 = await h.run(fv.id, PARAMS, authorized=False)
    r2 = await h.run(fv.id, {**PARAMS, "caption": "other"}, authorized=False)
    approvals.set(r1.approval_id, "expired")
    approvals.set(r2.approval_id, "denied")
    await h.runner.poll_approvals()
    assert h.runner.get(r1.id).error["code"] == "approval_expired"
    assert h.runner.get(r2.id).error["code"] == "denied"

    # no approvals service at all: the runner's own timeout still ends the wait
    h2 = Harness(tmp_path / "b", services={"policy": FakePolicy(None)}, approval_timeout_s=3600)
    fv2 = h2.save(post_flow())
    r3 = await h2.run(fv2.id, PARAMS, authorized=False)
    assert r3.status == RunStatus.WAITING_APPROVAL
    h2.clock.advance(3601)
    await h2.runner.poll_approvals()
    assert h2.runner.get(r3.id).status == RunStatus.FAILED
    assert "no decision within 1 hours" in h2.runner.get(r3.id).error["message"]


async def test_push_approval_resumes(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    run, _ = h.runner.submit(flow_id=fv.id, params=PARAMS, approval_id="ap_77")
    assert run.status == RunStatus.WAITING_APPROVAL
    assert await h.runner.on_approval("ap_77", "approved") == 1
    await h.runner.run_until_idle()
    assert h.runner.get(run.id).status == RunStatus.SUCCEEDED_VERIFIED


async def test_idempotency_keys_dedupe_duplicate_fires(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    a, created_a = h.runner.submit(flow_id=fv.id, params=PARAMS, idempotency_key="post-2026-11-04", authorized=True)
    b, created_b = h.runner.submit(flow_id=fv.id, params=PARAMS, idempotency_key="post-2026-11-04", authorized=True)
    assert created_a and not created_b and a.id == b.id
    await h.runner.run_until_idle()
    c, created_c = h.runner.submit(flow_id=fv.id, params=PARAMS, idempotency_key="post-2026-11-04", authorized=True)
    assert not created_c and c.status == RunStatus.SUCCEEDED_VERIFIED
    assert len(h.store.list_runs()) == 1 and len(h.site.state["posts"]) == 1


async def test_queued_run_keeps_its_flow_version(tmp_path):
    h = Harness(tmp_path)
    v1 = h.save(post_flow())
    run = h.submit(v1.id, PARAMS)
    broken = post_flow()
    broken["steps"][3]["locator"]["name"] = "Compose"  # a change that would fail on this site
    v2 = h.save(broken)
    assert h.flows.get(v1.id).version == v2.version
    await h.runner.run_until_idle()
    done = h.runner.get(run.id)
    assert done.flow_version == v1.version and done.status == RunStatus.SUCCEEDED_VERIFIED


async def test_invalid_params_create_no_run(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    for bad in ({"image": "a.jpg"}, {"caption": 7, "image": "a.jpg"}, {"caption": "x" * 3000, "image": "a"}):
        with pytest.raises(InvalidRequest, match="invalid params"):
            h.runner.submit(flow_id=fv.id, params=bad)
    assert h.store.list_runs() == []


async def test_quarantined_lane_defers_with_backoff(tmp_path):
    lanes = FakeLanes(["biz-a/social"])
    lanes.add("biz-a/social", LaneState.QUARANTINED, "exit IP did not match the proxy")
    h = Harness(tmp_path, services={"lanes": lanes})
    fv = h.save(post_flow())
    run = await h.run(fv.id, PARAMS)
    assert run.status == RunStatus.QUEUED and run.deferrals == 1 and run.attempts == 0
    assert "quarantined" in run.error["message"] and run.not_before > h.clock.now()
    lanes.add("biz-a/social", LaneState.WARM)
    h.clock.advance(3600)
    await h.runner.run_until_idle()
    assert h.runner.get(run.id).status == RunStatus.SUCCEEDED_VERIFIED


async def test_busy_lane_requeues_and_unknown_lane_fails(tmp_path):
    busy = {"left": 1}

    def enter(lane):
        if busy["left"]:
            busy["left"] -= 1
            raise LaneBusy("all warm slots are active")

    site_h = Harness(tmp_path / "a")
    h = Harness(tmp_path / "b", site=site_h.site, factory=driver_factory(site_h.driver, enter=enter))
    fv = h.save(post_flow())
    run = await h.run(fv.id, PARAMS)
    assert run.status == RunStatus.QUEUED and run.attempts == 0 and run.error["code"] == "deferred"
    h.clock.advance(120)
    await h.runner.run_until_idle()
    assert h.runner.get(run.id).status == RunStatus.SUCCEEDED_VERIFIED

    lanes = FakeLanes([])
    h3 = Harness(tmp_path / "c", services={"lanes": lanes})
    fv3 = h3.save(post_flow())
    r3 = await h3.run(fv3.id, PARAMS)
    assert r3.status == RunStatus.FAILED and r3.error["code"] == "lane_not_found"


def read_flow():
    return {"id": "any.social.read", "lane": "*/*", "steps": [
        {"type": "navigate", "url": f"{BASE}/"},
        {"type": "readback", "locator": {"role": "heading", "name": "Feed"}, "condition": {"kind": "exists"}}]}


async def test_one_run_per_lane_node_limit_and_fair_queue(tmp_path):
    gate = asyncio.Event()
    entered: list[str] = []
    base = Harness(tmp_path / "site")

    @asynccontextmanager
    async def gated(lane):
        entered.append(lane)
        await gate.wait()
        yield FakeDriver(base.site)

    h = Harness(tmp_path, site=base.site, factory=gated, max_concurrent=2)
    fv = h.save(read_flow())
    a1 = h.submit(fv.id, lane="biz-a/one")
    a1b = h.submit(fv.id, lane="biz-a/one")  # same lane as a1
    a2 = h.submit(fv.id, lane="biz-a/two")
    a3 = h.submit(fv.id, lane="biz-a/three")
    b1 = h.submit(fv.id, lane="biz-b/one")

    first = await h.runner.dispatch_once()
    await asyncio.sleep(0)
    assert len(first) == 2 and entered == ["biz-a/one", "biz-b/one"]  # biz-b is not starved by biz-a
    assert await h.runner.dispatch_once() == []  # node limit reached
    gate.set()
    await h.runner.run_until_idle()
    assert entered == ["biz-a/one", "biz-b/one", "biz-a/one", "biz-a/two", "biz-a/three"]
    statuses = {h.runner.get(r.id).status for r in (a1, a1b, a2, a3, b1)}
    assert statuses == {RunStatus.SUCCEEDED_VERIFIED}


async def test_same_lane_never_runs_twice_at_once(tmp_path):
    gate = asyncio.Event()
    active = {"n": 0, "max": 0}
    base = Harness(tmp_path / "site")

    @asynccontextmanager
    async def counting(lane):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await gate.wait()
        try:
            yield FakeDriver(base.site)
        finally:
            active["n"] -= 1

    h = Harness(tmp_path, site=base.site, factory=counting, max_concurrent=4)
    fv = h.save(read_flow())
    for _ in range(3):
        h.submit(fv.id, lane="biz-a/one")
    started = await h.runner.dispatch_once()
    assert len(started) == 1
    gate.set()
    await h.runner.run_until_idle()
    assert active["max"] == 1


async def test_cancel_queued_waiting_and_running(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    queued = h.submit(fv.id, PARAMS)
    assert (await h.runner.cancel(queued.id)).status == RunStatus.CANCELLED
    waiting, _ = h.runner.submit(flow_id=fv.id, params=PARAMS, approval_id="ap_9")
    assert (await h.runner.cancel(waiting.id)).status == RunStatus.CANCELLED
    await h.runner.run_until_idle()
    assert h.site.state["posts"] == []

    running = h.submit(fv.id, PARAMS)

    def cancel_after_new_post(op, el, d):
        if op == "click" and el.name == "New post":
            h.store.set_cancel_requested(running.id)

    h.driver.after_hooks.append(cancel_after_new_post)
    await h.runner.run_until_idle()
    final = h.runner.get(running.id)
    assert final.status == RunStatus.CANCELLED and final.error["code"] == "cancelled"
    assert "upload" not in [op for op, _ in h.driver.log] and h.site.state["posts"] == []


async def test_stop_drains_at_a_step_boundary_and_resumes(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())

    def stop_after_upload(op, el, d):
        if op == "upload":
            h.runner._stopping = True  # what stop() sets

    h.driver.after_hooks.append(stop_after_upload)
    run = h.submit(fv.id, PARAMS)
    await h.runner.run_until_idle()
    paused = h.runner.get(run.id)
    assert paused.status == RunStatus.QUEUED and paused.attempts == 0

    h2 = restart(h)
    h2.runner.recover()
    await h2.runner.run_until_idle()
    assert h2.runner.get(run.id).status == RunStatus.SUCCEEDED_VERIFIED
    assert "upload" not in [op for op, _ in h2.driver.log]


async def test_start_and_stop_hooks_run_the_queue(tmp_path):
    h = Harness(tmp_path, dispatch_interval_s=0.05)
    fv = h.save(post_flow())
    await h.runner.start()
    try:
        run = h.submit(fv.id, PARAMS)
        done = await h.runner.wait(run.id, 5)
        assert done.status == RunStatus.SUCCEEDED_VERIFIED
    finally:
        await h.runner.stop()
    assert h.runner._loops == [] and h.runner.scheduler._task is None


async def test_crash_loop_is_stopped(tmp_path):
    h = Harness(tmp_path, max_attempts=1)
    fv = h.save(read_flow())
    run = h.submit(fv.id, lane="biz-a/one")
    run.status, run.attempts = RunStatus.RUNNING, 3
    h.store.save_run(run)
    h.runner.recover()
    assert h.runner.get(run.id).error["code"] == "crash_loop"
