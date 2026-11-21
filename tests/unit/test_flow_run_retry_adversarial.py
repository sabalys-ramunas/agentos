"""Adversarial probes of the keyless retry dedupe (QA-2 #11).

Each test says what a retry should do: one run and one approval for what is really the same call,
and a new run for what is not (a new key, a new version, another slot, a decided approval).
"""

from __future__ import annotations

import asyncio

import pytest
from test_connector_approvals import LANE, _harness, _no_pace, _policy, _post
from test_flow_run_retry import PARAMS, _asking, _tool
from test_policy_support import Clock

from openajar.core.models import Effect, RunStatus
from openajar.policy.errors import Denied
from openajar.policy.service import PolicyService
from openajar.runner.fakes import Harness, post_flow
from openajar.runner.tools import tool_specs

FLOW = "biz-a.social.post"
SLOT = "2026-11-11T09:00:00+00:00"


def _typed(h: Harness):
    """The per-flow typed tool (make_flow_handler): the only tool with a flow input schema here."""
    return next(s.handler for s in tool_specs(h.runtime) if hasattr(s.handler, "__openajar_input_schema__"))


def _with_props(**props):
    flow = post_flow()
    schema = flow["params"]
    flow["params"] = {**schema, "properties": {**schema["properties"], **props}}
    return flow


def _auto(tmp_path):
    """Like _asking, but Ask me first is off: the commit posts on its own."""
    h = Harness(tmp_path / "runner")
    svc = PolicyService(tmp_path / "policy", runtime=h.runtime, clock=Clock())
    h.runtime.register("policy", svc)
    h.runtime.register("approvals", svc.approvals)
    h.save(post_flow())
    _, tok = svc.issue(subject={"id": "agent-a"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    return h, svc, tok


def _connector(tmp_path):
    """The connector harness of test_connector_approvals, wired as the node is (approvals registered)."""
    h, _inbox = _harness(tmp_path, ops={"post_photo": _no_pace()})
    svc, tok = _policy(h, tmp_path)
    h.runtime.register("approvals", svc.approvals)
    svc.ask_first.set("lane", LANE, True, by="owner", person=True)
    return h, svc, tok


def _yielding(ctx):
    """The same context with an await point in authorize, as an I/O-bound authorize would have. The
    harness's authorize never yields, so a race between open_twin and submit cannot show without it."""
    real = ctx.authorize

    async def authorize(*args, **kw):
        await asyncio.sleep(0)
        return await real(*args, **kw)

    ctx.authorize = authorize
    return ctx


# ---------------------------------------------------------------- flow_run and the typed tool

async def test_the_typed_flow_tool_after_flow_run_with_the_same_args_returns_the_waiting_run(tmp_path):
    h, svc, tok = _asking(tmp_path)
    first = await _tool(h, "flow_run")(svc.context_for_token(tok, tool="flow_run"), name=FLOW,
                                       params=PARAMS, wait_seconds=0)
    typed = await _typed(h)(svc.context_for_token(tok, tool=FLOW), wait_seconds=0, **PARAMS)
    assert typed["id"] == first["id"] and typed.get("duplicate") is True, typed
    assert len(svc.approvals.list(state="pending")) == 1
    svc.close()


async def test_two_identical_flow_runs_at_once_make_one_run_and_one_approval(tmp_path):
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")

    def call():
        return run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=dict(PARAMS), wait_seconds=0)

    a, b = await asyncio.gather(call(), call())
    assert a["id"] == b["id"], (a, b)
    assert len(h.runner.store.list_runs(flow_id=FLOW)) == 1
    assert len(svc.approvals.list(state="pending")) == 1
    svc.close()


async def test_identical_calls_at_once_with_params_in_another_key_order_make_one_run(tmp_path):
    """Passes in the harness only because authorize never yields. With an await point (as below), twin_guard
    keys its lock on repr(params), which is key-order sensitive, so the two calls do not serialise."""
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")

    def call(params):
        return run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=params, wait_seconds=0)

    a, b = await asyncio.gather(call(dict(PARAMS)), call({"image": PARAMS["image"], "caption": PARAMS["caption"]}))
    assert a["id"] == b["id"], (a, b)
    assert len(h.runner.store.list_runs(flow_id=FLOW)) == 1
    svc.close()


async def test_identical_calls_at_once_in_another_key_order_with_an_await_point_make_one_run(tmp_path):
    """Candidate double post (latent): the same as above, with an await point between open_twin and submit."""
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")

    def call(params):
        return run(_yielding(svc.context_for_token(tok, tool="flow_run")), name=FLOW, params=params, wait_seconds=0)

    a, b = await asyncio.gather(call(dict(PARAMS)), call({"image": PARAMS["image"], "caption": PARAMS["caption"]}))
    assert a["id"] == b["id"], (a["id"], b["id"], len(h.runner.store.list_runs(flow_id=FLOW)))
    svc.close()


async def test_a_typed_call_and_a_flow_run_at_once_with_an_await_point_make_one_run(tmp_path):
    """Candidate double post (latent): the typed tool orders params by signature, flow_run by the caller."""
    h, svc, tok = _asking(tmp_path)
    flow_run = _tool(h, "flow_run")
    a, b = await asyncio.gather(
        flow_run(_yielding(svc.context_for_token(tok, tool="flow_run")), name=FLOW,
                 params={"image": PARAMS["image"], "caption": PARAMS["caption"]}, wait_seconds=0),
        _typed(h)(_yielding(svc.context_for_token(tok, tool=FLOW)), wait_seconds=0, **PARAMS))
    assert a["id"] == b["id"], (a["id"], b["id"], len(h.runner.store.list_runs(flow_id=FLOW)))
    svc.close()


async def test_key_order_on_a_sequential_retry_is_the_same_run(tmp_path):
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")
    first = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    again = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW,
                      params={"image": PARAMS["image"], "caption": PARAMS["caption"]}, wait_seconds=0)
    assert again["id"] == first["id"] and again["duplicate"] is True, again
    svc.close()


async def test_on_given_or_omitted_for_the_same_lane_is_the_same_run(tmp_path):
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")
    first = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    again = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, on=LANE,
                      wait_seconds=0)
    assert again["id"] == first["id"] and again["duplicate"] is True, again
    svc.close()


async def test_defaults_the_flow_fills_are_the_same_run_however_they_are_passed(tmp_path):
    h, svc, tok = _asking(tmp_path)
    h.save(_with_props(tag={"type": "string", "default": "lake"}))
    run = _tool(h, "flow_run")
    first = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    explicit = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW,
                         params={**PARAMS, "tag": "lake"}, wait_seconds=0)
    typed = await _typed(h)(svc.context_for_token(tok, tool=FLOW), wait_seconds=0, **PARAMS)
    assert explicit["id"] == first["id"] and explicit["duplicate"] is True, explicit
    assert typed["id"] == first["id"] and typed["duplicate"] is True, typed
    svc.close()


async def test_not_before_as_the_same_instant_in_two_zones_is_the_same_run(tmp_path):
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")
    first = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0,
                      not_before="2026-11-08T09:00:00+02:00")
    again = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0,
                      not_before="2026-11-08T07:00:00+00:00")
    assert again["id"] == first["id"] and again["duplicate"] is True, again
    svc.close()


async def test_not_before_as_the_same_instant_in_two_zones_at_once_is_the_same_run(tmp_path):
    """Passes in the harness (no await point). Candidate with one: twin_guard keys on repr(start), which
    differs for the two spellings, so the lock does not serialise them. See the next test."""
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")

    def call(when):
        return run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0,
                   not_before=when)

    a, b = await asyncio.gather(call("2026-11-08T09:00:00+02:00"), call("2026-11-08T07:00:00+00:00"))
    assert a["id"] == b["id"], (a, b)
    svc.close()


async def test_not_before_as_the_same_instant_in_two_zones_at_once_with_an_await_point_is_the_same_run(tmp_path):
    """Candidate double post (latent): the concurrent two-zone case with an await point in authorize."""
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")

    def call(when):
        return run(_yielding(svc.context_for_token(tok, tool="flow_run")), name=FLOW, params=PARAMS,
                   wait_seconds=0, not_before=when)

    a, b = await asyncio.gather(call("2026-11-08T09:00:00+02:00"), call("2026-11-08T07:00:00+00:00"))
    assert a["id"] == b["id"], (a["id"], b["id"], len(h.runner.store.list_runs(flow_id=FLOW)))
    svc.close()


async def test_a_different_not_before_one_second_later_is_a_new_run(tmp_path):
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")
    first = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0,
                      not_before="2026-11-08T09:00:00+02:00")
    later = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0,
                      not_before="2026-11-08T09:00:01+02:00")
    assert later["id"] != first["id"] and not later.get("duplicate"), later
    svc.close()


# ---------------------------------------------------------------- denied, expired, finished

async def test_a_retry_after_the_owner_denied_and_the_push_landed_is_refused_not_re_asked(tmp_path):
    """Behaviour, not a bug by the commit's scope: the policy's denial memory (service.py authorize,
    recent_denied) refuses the identical action with approval_denied. No second run, no second ask."""
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")
    first = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    svc.approvals.decide(first["approval_id"], "deny", by="owner")
    await h.runner.on_approval(first["approval_id"], "denied")
    assert h.runner.get(first["id"]).status == RunStatus.FAILED
    with pytest.raises(Denied, match="approval_denied"):
        await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    assert len(h.runner.store.list_runs(flow_id=FLOW)) == 1
    assert svc.approvals.list(state="pending") == []
    svc.close()


async def test_a_retry_after_a_denial_nobody_pushed_returns_the_denied_run(tmp_path):
    """Behaviour: with no push, the retry settles the denial and returns the failed run as a duplicate."""
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")
    first = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    svc.approvals.decide(first["approval_id"], "deny", by="owner")
    again = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    assert again["id"] == first["id"] and again["duplicate"] is True and again["status"] == "failed", again
    svc.close()


async def test_a_retry_after_the_approval_expired_asks_again(tmp_path):
    """Behaviour: the run's approval timeout fails it; a keyless retry is then a new run and a new ask."""
    h, svc, tok = _asking(tmp_path)
    h.runner.approval_timeout_s = 3600
    run = _tool(h, "flow_run")
    first = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    h.clock.advance(7200)
    await h.runner.poll_approvals()
    assert h.runner.get(first["id"]).status == RunStatus.FAILED
    again = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    assert again["id"] != first["id"] and not again.get("duplicate"), again
    svc.close()


async def test_a_keyless_retry_of_a_keyed_run_is_the_same_run(tmp_path):
    """Candidate double post. open_twin skips any run with an idempotency_key, so a retry that drops the
    key gets a second run. Before the fix: both runs wait on ONE shared approval (find_pending matches the
    content hash). Approving it posts once, then the keyless run re-asks, so a second approval appears."""
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")
    first = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0,
                      idempotency_key="k1")
    again = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    assert again["id"] == first["id"] and again["duplicate"] is True, again
    assert len(svc.approvals.list(state="pending")) == 1
    svc.close()


async def test_a_retry_after_a_new_version_of_the_flow_is_a_new_run(tmp_path):
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")
    first = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    h.save({**post_flow(), "title": "Post a photo, revised"})
    again = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    assert again["id"] != first["id"] and not again.get("duplicate"), again
    svc.close()


async def test_a_retry_after_an_edited_approval_returns_the_edited_run(tmp_path):
    """Candidate double post. The owner approves with a caption edit and the push lands, so run.params is
    the edited caption. The agent retries with its original args: open_twin finds no twin, and the retry
    asks for the ORIGINAL caption. Before the fix: approving that second card posts both captions."""
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")
    first = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    svc.approvals.decide(first["approval_id"], "approve", by="owner", edit={"caption": "Edited caption"})
    await h.runner.on_approval(first["approval_id"], "approved")
    assert h.runner.get(first["id"]).params["caption"] == "Edited caption"
    again = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    assert again["id"] == first["id"] and again["duplicate"] is True, again
    svc.close()


async def test_a_queued_approved_run_older_than_an_hour_still_gets_the_retry(tmp_path):
    """Candidate double post. TWIN_WINDOW counts an hour from created_at, not from the last progress. A run
    approved and still queued (lane paused, or a pace) past that hour is not a twin, so the retry makes a
    second run. Before the fix: the retry waits on a new approval for the same post, and the first run posts."""
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")
    first = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    svc.approvals.decide(first["approval_id"], "approve", by="owner")
    await h.runner.on_approval(first["approval_id"], "approved")
    assert h.runner.get(first["id"]).status == RunStatus.QUEUED
    h.clock.advance(7200)
    again = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0)
    assert again["id"] == first["id"] and again["duplicate"] is True, again
    svc.close()


async def test_a_slot_run_queued_for_days_still_gets_the_retry_two_hours_later(tmp_path):
    """Candidate double post, the same window bug with no approval. An auto run queued for a slot three days
    out; two hours later the retry is outside TWIN_WINDOW. Before the fix: two runs queued for the same slot,
    and both post when the slot comes."""
    h, svc, tok = _auto(tmp_path)
    run = _tool(h, "flow_run")
    first = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0,
                      not_before=SLOT)
    assert h.runner.get(first["id"]).status == RunStatus.QUEUED
    h.clock.advance(7200)
    again = await run(svc.context_for_token(tok, tool="flow_run"), name=FLOW, params=PARAMS, wait_seconds=0,
                      not_before=SLOT)
    assert again["id"] == first["id"] and again["duplicate"] is True, again
    svc.close()


# ---------------------------------------------------------------- connector tool

async def test_identical_connector_calls_at_once_make_one_approval(tmp_path):
    h, svc, tok = _connector(tmp_path)
    post = _post(h)

    def call():
        return post(svc.context_for_token(tok, tool="social_post_photo"), on=LANE, caption="Autumn at the lake",
                    image="photo.jpg", wait_seconds=0)

    a, b = await asyncio.gather(call(), call())
    assert a["id"] == b["id"], (a, b)
    assert len(svc.approvals.list(state="pending")) == 1
    svc.close()


async def test_connector_calls_at_once_in_another_key_order_with_an_await_point_make_one_run(tmp_path):
    """Candidate double post (latent): run_operation keys its lock on target.params, in the caller's order."""
    h, svc, tok = _connector(tmp_path)
    post = _post(h)

    def call(**kw):
        return post(_yielding(svc.context_for_token(tok, tool="social_post_photo")), on=LANE, wait_seconds=0, **kw)

    a, b = await asyncio.gather(call(caption="Autumn at the lake", image="photo.jpg"),
                                call(image="photo.jpg", caption="Autumn at the lake"))
    assert a["id"] == b["id"], (a["id"], b["id"])
    svc.close()
