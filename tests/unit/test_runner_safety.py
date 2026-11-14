"""Flow runs are as safe as direct tool calls: grants on runs, effective effects, hidden params,
holds while a human has the lane, and the recorder's readback for a control that is renamed.

The policy tests use the real PolicyService (as production does), the others the fakes.
"""

from __future__ import annotations

import json

import pytest
from test_policy_support import Clock

from agentos.core.errors import InvalidRequest, PolicyDenied
from agentos.core.models import ControlState, Effect, LaneControl, RunStatus
from agentos.policy.service import PolicyService
from agentos.runner.fakes import BASE, OWNER, El, FakeCtx, FakeLanes, FakePolicy, Harness, post_flow
from agentos.runner.models import FlowDefinition, RunKind, steps_effect
from agentos.runner.schedules import sign_webhook
from agentos.runner.tools import flow_run, flow_save, flows_list, tool_specs

PARAMS = {"caption": "Autumn", "image": "a.jpg"}
LOOK = {"id": "biz-a.social.look", "lane": "biz-a/social", "effect": "draft",
        "steps": [{"type": "navigate", "url": f"{BASE}/"}]}


def with_policy(tmp_path, **options) -> tuple[Harness, PolicyService, Clock]:
    h = Harness(tmp_path / "runner", **options)
    clock = Clock()
    svc = PolicyService(tmp_path / "policy", runtime=h.runtime, clock=clock)
    for name, service in (("policy", svc), ("approvals", svc.approvals), ("traces", svc.traces),
                          ("notifier", svc.notifier)):
        h.runtime.register(name, service)
    return h, svc, clock


def agent(svc: PolicyService, **kw):
    kw.setdefault("subject", {"id": "agent-1"})
    kw.setdefault("workspaces", ["biz-a"])
    kw.setdefault("ceiling", Effect.IRREVERSIBLE)
    g, tok = svc.issue(**kw)
    return g, svc.context_for_token(tok, tool="flow_run")


# ---------------------------------------------------------------- runs carry their grant


async def test_a_queued_run_of_a_revoked_grant_fails_as_grant_revoked_and_never_acts(tmp_path):
    h, svc, _ = with_policy(tmp_path)
    h.save(LOOK)
    g, ctx = agent(svc, ceiling=Effect.DRAFT)
    out = await flow_run(ctx, name=LOOK["id"], wait_seconds=0)
    run = h.runner.get(out["id"])
    assert run.grant_id == g.id and run.authorized
    svc.revoke(g.id)
    await h.runner.run_until_idle()
    run = h.runner.get(run.id)
    assert run.status == RunStatus.FAILED and run.error["code"] == "grant_revoked", run.error
    assert h.driver.log == []  # it never touched the lane


async def test_a_queued_run_of_an_expired_grant_fails_as_grant_expired(tmp_path):
    h, svc, clock = with_policy(tmp_path)
    h.save(LOOK)
    _, ctx = agent(svc, ceiling=Effect.DRAFT, ttl_s=600)
    out = await flow_run(ctx, name=LOOK["id"], wait_seconds=0)
    clock.advance(601 + svc.cfg.clock_skew_s)
    await h.runner.run_until_idle()
    run = h.runner.get(out["id"])
    assert run.status == RunStatus.FAILED and run.error["code"] == "grant_expired", run.error
    assert h.driver.log == []
    # the run's error carries the catalog's next step, as an MCP error result does
    from agentos.core import catalog
    shown = h.runner.describe(run)["error"]
    assert shown["fix"] == catalog.entry("grant_expired").agent and "fix" not in run.error


async def test_a_run_needs_a_grant_that_covers_it_when_it_is_queued(tmp_path):
    h, svc, _ = with_policy(tmp_path)
    fv = h.save(post_flow())
    _, ctx = agent(svc, ceiling=Effect.DRAFT)
    with pytest.raises(PolicyDenied):  # the post flow is commit: over this grant's ceiling
        h.runner.submit(flow_id=fv.id, params=PARAMS, principal=ctx.principal, authorized=False)
    with pytest.raises(PolicyDenied):  # a principal without a grant is not the owner
        h.runner.submit(flow_id=fv.id, params=PARAMS, principal={"kind": "agent", "id": "stray"})
    other, _ = agent(svc, workspaces=["biz-b"])
    with pytest.raises(PolicyDenied):
        h.runner.submit(flow_id=fv.id, params=PARAMS, grant_id=other.id)
    assert h.store.list_runs() == []


async def test_panel_and_cli_runs_carry_the_owner_grant_and_still_wait_for_a_co_sign(tmp_path):
    h, svc, _ = with_policy(tmp_path)
    fv = h.save(post_flow())
    run = await h.run(fv.id, PARAMS, principal=OWNER, source="api", authorized=False)
    assert run.grant_id == "owner" and run.status == RunStatus.WAITING_APPROVAL
    assert h.site.state["posts"] == []
    pending = svc.approvals.list(state="pending")
    assert len(pending) == 1 and pending[0].grant_id == "owner"
    assert "caption='Autumn'" in pending[0].summary  # post_flow marks its params "show"


async def test_one_approval_resumes_one_parked_run_and_the_other_asks_again(tmp_path):
    h, svc, _ = with_policy(tmp_path)
    fv = h.save(post_flow())
    a = await h.run(fv.id, PARAMS, authorized=False)
    b = await h.run(fv.id, PARAMS, authorized=False)
    assert a.approval_id == b.approval_id  # identical asks share one pending approval
    svc.approvals.decide(a.approval_id, "approve", by="owner")
    assert await h.runner.poll_approvals() == 2
    await h.runner.run_until_idle()
    a, b = h.runner.get(a.id), h.runner.get(b.id)
    assert a.status == RunStatus.SUCCEEDED_VERIFIED and len(h.site.state["posts"]) == 1
    assert b.status == RunStatus.WAITING_APPROVAL and b.approval_id != a.approval_id
    assert svc.approvals.get(a.approval_id).state == "consumed"


# ---------------------------------------------------------------- schedules and webhooks


async def test_a_schedule_runs_under_its_creators_grant_and_stops_when_it_is_revoked(tmp_path):
    h, svc, _ = with_policy(tmp_path)
    fv = h.save(LOOK)
    g, ctx = agent(svc, ceiling=Effect.DRAFT)
    s, _ = h.scheduler.add(flow=fv.id, cron="* * * * *", timezone="UTC", principal=ctx.principal)
    assert s.grant_id == g.id
    h.clock.advance(61)
    await h.scheduler.tick()
    first = h.store.list_runs(schedule_id=s.id)
    assert len(first) == 1 and first[0].grant_id == g.id
    await h.runner.run_until_idle()
    assert h.runner.get(first[0].id).status == RunStatus.SUCCEEDED_UNVERIFIED
    svc.revoke(g.id)
    h.clock.advance(60)
    await h.scheduler.tick()
    assert len(h.store.list_runs(schedule_id=s.id)) == 1  # refused when it would be queued
    assert h.store.get_schedule(s.id).state["last_error"]["code"] == "denied"


async def test_a_schedule_needs_a_grant_that_covers_its_flow(tmp_path):
    h, svc, _ = with_policy(tmp_path)
    fv = h.save(post_flow())
    _, ctx = agent(svc, ceiling=Effect.DRAFT)
    with pytest.raises(PolicyDenied):
        h.scheduler.add(flow=fv.id, cron="0 9 * * *", params=PARAMS, principal=ctx.principal)
    s, _ = h.scheduler.add(flow=fv.id, cron="0 9 * * *", params=PARAMS, principal=OWNER)
    assert s.grant_id == "owner"


async def test_a_webhook_of_a_revoked_grant_is_refused_and_a_sender_retry_is_a_duplicate(tmp_path):
    h, svc, _ = with_policy(tmp_path)
    fv = h.save(LOOK)
    g, ctx = agent(svc, ceiling=Effect.DRAFT)
    s, extras = h.scheduler.add(flow=fv.id, webhook=True, principal=ctx.principal)
    token, secret = extras["url_path"].rsplit("/", 1)[-1], extras["secret"]
    ts = str(int(h.clock.now().timestamp()))
    headers = {"X-Agentos-Timestamp": ts, "X-Agentos-Signature": sign_webhook(secret, ts, b"{}"),
               "X-Agentos-Delivery": "d-1"}
    assert (await h.scheduler.handle_webhook(token, b"{}", headers))[0] == 202
    h.clock.advance(30)  # the sender retries the same delivery, signed with a new timestamp
    ts2 = str(int(h.clock.now().timestamp()))
    retry = {"X-Agentos-Timestamp": ts2, "X-Agentos-Signature": sign_webhook(secret, ts2, b"{}"),
             "X-Agentos-Delivery": "d-1"}
    status, out = await h.scheduler.handle_webhook(token, b"{}", retry)
    assert status == 200 and out["duplicate"]
    svc.revoke(g.id)
    fresh = {**retry, "X-Agentos-Delivery": "d-2"}
    status, out = await h.scheduler.handle_webhook(token, b"{}", fresh)
    assert status == 403 and out["error"]["code"] == "denied"
    assert len(h.store.list_runs(schedule_id=s.id)) == 1


# ---------------------------------------------------------------- the effective effect


def mark_inputs_not_publishing(flow: dict) -> dict:
    for step in flow["steps"]:
        if step["type"] in ("click", "fill", "upload", "type", "press", "select"):
            step["publishes"] = False
    return flow


def test_steps_set_the_effect_and_only_the_owner_can_lower_an_input_step():
    defn = FlowDefinition.model_validate(post_flow(effect="read"))
    assert steps_effect(defn.steps, owner_saved=False) == (Effect.COMMIT, "step 3 (click)")
    assert defn.effective_effect(owner_saved=False) == (Effect.COMMIT, "step 3 (click)")
    marked = FlowDefinition.model_validate(mark_inputs_not_publishing(post_flow(effect="read")))
    assert marked.effective_effect(owner_saved=False)[0] == Effect.COMMIT  # an agent's marks count for nothing
    assert marked.effective_effect(owner_saved=True) == (Effect.DRAFT, "step 3 (click)")
    pay = FlowDefinition.model_validate(mark_inputs_not_publishing(post_flow(effect="pay")))
    assert pay.effective_effect(owner_saved=True) == (Effect.PAY, None)  # never below the declaration
    look = FlowDefinition.model_validate(LOOK)
    assert look.effective_effect(owner_saved=False) == (Effect.DRAFT, None)
    assert FlowDefinition.model_validate({**LOOK, "effect": "read"}).effective_effect(owner_saved=False)[0] == \
        Effect.READ


async def test_flows_list_and_the_flow_tool_show_the_effective_effect(tmp_path):
    h = Harness(tmp_path)
    ctx = FakeCtx(h.runtime)
    saved = await flow_save(ctx, definition=mark_inputs_not_publishing(post_flow(effect="read")))
    assert saved["effect"] == "commit" and saved["declared_effect"] == "read"
    assert "step 3 (click)" in saved["effect_note"] and "owner" in saved["effect_note"]
    listing = await flows_list(ctx, prefix="biz-a.social")
    shown = listing["flows"][0]
    assert shown["effect"] == "commit" and shown["declared_effect"] == "read" and shown["effect_note"]
    spec = next(s for s in tool_specs(h.runtime) if s.name == "flow_biz_a_social_post")
    assert spec.effect == Effect.COMMIT and "effect commit" in spec.description
    assert "—" not in json.dumps(saved) + json.dumps(listing)

    # an agent holding the owner's own token is still an agent when it saves over MCP
    owner_token_agent = FakeCtx(h.runtime, principal=OWNER)
    again = await flow_save(owner_token_agent, definition=mark_inputs_not_publishing(post_flow(effect="read")))
    assert again["effect"] == "commit"
    assert not h.flows.get("biz-a.social.post").owner_saved

    # the owner saving the same version (panel, CLI) vouches for its marks
    fv = h.flows.save(mark_inputs_not_publishing(post_flow(effect="read")), by=OWNER)[0]
    assert fv.owner_saved and fv.effect == Effect.DRAFT
    assert h.flows.summary(h.flows.get(fv.id))["effect"] == "draft"


async def test_an_owner_marked_flow_runs_at_draft_and_an_agent_one_asks_for_commit(tmp_path):
    policy = FakePolicy(approve_above=Effect.DRAFT)
    h = Harness(tmp_path, services={"policy": policy})
    marked = mark_inputs_not_publishing(post_flow(effect="read"))
    fv = h.save(marked, by=OWNER)
    run = await h.run(fv.id, PARAMS, authorized=False)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED and policy.calls[-1]["effect"] == Effect.DRAFT

    h2 = Harness(tmp_path / "b", services={"policy": policy})
    fv2 = h2.save(marked, by={"kind": "agent", "id": "claude-code"})
    run2 = await h2.run(fv2.id, PARAMS, authorized=False)
    assert run2.status == RunStatus.WAITING_APPROVAL and policy.calls[-1]["effect"] == Effect.COMMIT
    assert h2.site.state["posts"] == []


async def test_an_acted_input_step_the_owner_marked_does_not_make_a_failure_partial(tmp_path):
    flow = {"id": "biz-a.social.open", "lane": "biz-a/social", "effect": "draft", "steps": [
        {"type": "navigate", "url": f"{BASE}/"},
        {"type": "click", "locator": {"role": "button", "name": "New post"}, "publishes": False},
        {"type": "click", "locator": {"role": "button", "name": "Gone"}}]}
    h = Harness(tmp_path)
    assert (await h.run(h.save(flow, by=OWNER).id)).status == RunStatus.FAILED
    h2 = Harness(tmp_path / "b")
    assert (await h2.run(h2.save(flow).id)).status == RunStatus.PARTIAL  # unmarked: it may have published


# ---------------------------------------------------------------- hidden params


SECRET_FLOW = {
    "id": "biz-a.social.note", "lane": "biz-a/social", "effect": "draft", "domain": "social.example",
    "params": {"type": "object", "required": ["who", "note"],
               "properties": {"who": {"type": "string"}, "note": {"type": "string"},
                              "tag": {"type": "string", "show": True}}},
    "steps": [
        {"type": "navigate", "url": BASE + "/u/${who}"},
        {"type": "navigate", "url": f"{BASE}/compose"},
        {"type": "fill", "locator": {"role": "textbox", "name": "Write a caption..."}, "param": "note",
         "readback": {"condition": {"kind": "value", "equals": "${note}"}}},
        {"type": "readback", "url": BASE + "/u/${who}", "locator": {"role": "heading", "name": "${who}"},
         "condition": {"kind": "exists"}, "timeout_ms": 0},
    ],
}


def test_summaries_and_approvals_never_show_a_hidden_param(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(SECRET_FLOW)
    params = {"who": "biz-a", "note": "meet at the lake 0427", "tag": "autumn"}
    summary = h.flows.run_summary(fv, "biz-a/social", params)
    assert "note=(hidden)" in summary and "tag='autumn'" in summary and "lake" not in summary
    content = h.flows.approval_content(fv, "biz-a/social", params)
    assert content["params"]["tag"] == "autumn" and content["params"]["note"].startswith("hidden:")
    assert "lake" not in json.dumps(content)
    again = h.flows.approval_content(fv, "biz-a/social", params)
    other = h.flows.approval_content(fv, "biz-a/social", {**params, "note": "another text"})
    assert again == content and other["params"]["note"] != content["params"]["note"]


async def test_hidden_params_stay_out_of_proof_steps_and_errors_but_a_recheck_still_works(tmp_path):
    h = Harness(tmp_path)
    h.site.state["account"] = "biz-a"
    fv = h.save(SECRET_FLOW)
    params = {"who": "biz-a", "note": "meet at the lake 0427"}
    unread = {"n": 1}

    def blind_once(op, target, d):  # the profile read fails once, so the run ends unverified
        if op == "navigate" and target == f"{BASE}/u/biz-a" and len(d.log) > 2 and unread["n"]:
            unread["n"] -= 1
            raise OSError("network down")

    h.driver.hooks.append(blind_once)
    run = await h.run(fv.id, params)
    assert run.status == RunStatus.SUCCEEDED_UNVERIFIED and run.recheck_at, (run.status, run.error)
    shown = json.dumps({"proof": run.proof, "steps": h.runner.steps(run.id), "run": h.runner.describe(run)})
    assert "lake" not in shown and "/u/biz-a" not in shown and "${note}" in shown
    row = h.store._rows("SELECT group_concat(result) AS r FROM steps WHERE run_id=?", (run.id,))[0]["r"]
    assert "lake" not in row and "u/biz-a" not in row  # the journal itself holds placeholders

    h.clock.advance(400)
    await h.runner.run_until_idle(rechecks=True)  # the re-check uses the private resolved check
    final = h.runner.get(run.id)
    assert final.status == RunStatus.SUCCEEDED_VERIFIED, final.proof
    assert "/u/biz-a" not in json.dumps(final.proof)


async def test_a_url_param_may_choose_a_page_inside_the_flows_domain_only(tmp_path):
    flow = {"id": "biz-a.social.visit", "lane": "biz-a/social", "effect": "read", "domain": "social.example",
            "params": {"type": "object", "required": ["target"],
                       "properties": {"target": {"type": "string", "show": True}}},
            "steps": [{"type": "navigate", "url": "${target}"}]}
    h = Harness(tmp_path)
    fv = h.save(flow)
    assert (await h.run(fv.id, {"target": f"{BASE}/p/1"})).status == RunStatus.SUCCEEDED_UNVERIFIED
    for bad in ("https://social.example.evil.test/", "javascript:alert(1)", "https://user@evil.test/"):
        run = await h.run(fv.id, {"target": bad})
        assert run.status == RunStatus.FAILED and run.error["code"] == "url_outside_domain", (bad, run.error)
    nodomain = h.save({**flow, "id": "biz-a.social.visit2", "domain": None})
    run = await h.run(nodomain.id, {"target": f"{BASE}/p/1"})
    assert run.error["code"] == "url_outside_domain" and "declare its domain" in run.error["message"]


def test_params_that_are_not_an_object_are_refused_everywhere(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    for bad in (["caption"], "caption=x", 7):
        with pytest.raises(InvalidRequest):
            h.runner.submit(flow_id=fv.id, params=bad)
        with pytest.raises(InvalidRequest):
            h.scheduler.add(flow=fv.id, cron="0 9 * * *", params=bad)
    with pytest.raises(InvalidRequest):
        h.runner.submit(kind=RunKind.SCRIPT, params=["x"])


# ---------------------------------------------------------------- a human holds the lane


async def test_holds_never_count_toward_giving_up_and_hand_back_makes_the_run_due(tmp_path):
    lanes = FakeLanes(["biz-a/social"])
    held = {"state": ControlState.PAUSED}
    listeners = []
    lanes.control = lambda lane_id: LaneControl(state=held["state"])
    lanes.add_control_listener = listeners.append
    h = Harness(tmp_path, services={"lanes": lanes}, max_deferrals=1)
    assert h.runner.listen_for_lane_control() and h.runner.listen_for_lane_control()
    assert listeners == [h.runner.on_lane_control]
    fv = h.save(LOOK)
    run = h.submit(fv.id)
    for _ in range(4):
        await h.runner.run_until_idle()
        h.clock.advance(3600)
    run = h.runner.get(run.id)
    assert run.status == RunStatus.QUEUED and run.held == run.deferrals == 4
    assert run.error["control"] == "paused" and "stopped agents" in run.error["message"]
    await h.runner.run_until_idle()  # deferred again: not due for a while
    run = h.runner.get(run.id)
    assert run.not_before is not None
    held["state"] = ControlState.AGENT
    assert h.runner.on_lane_control("biz-a/social", LaneControl(state=ControlState.PAUSED),
                                    LaneControl(state=ControlState.AGENT)) == 1
    await h.runner.run_until_idle()  # no clock change needed
    assert h.runner.get(run.id).status == RunStatus.SUCCEEDED_UNVERIFIED


async def test_a_busy_lane_still_gives_up_after_max_deferrals(tmp_path):
    lanes = FakeLanes(["biz-a/social"])
    lanes.add("biz-a/social", reason="proxy down")
    lanes.info["biz-a/social"].state = "quarantined"
    h = Harness(tmp_path, services={"lanes": lanes}, max_deferrals=2)
    run = h.submit(h.save(LOOK).id)
    for _ in range(4):
        await h.runner.run_until_idle()
        h.clock.advance(7200)
    run = h.runner.get(run.id)
    assert run.status == RunStatus.FAILED and run.error["code"] == "lane_unavailable"


# ---------------------------------------------------------------- the recorder


def renaming_like_site(h: Harness) -> None:
    """A post whose Like is named "Remove Like" once pressed (the name changes, not just the state)."""
    def page(s, d):
        pressed = "p2" in s.state["liked"]

        def toggle(s, el, d):
            s.state["liked"].symmetric_difference_update({"p2"})

        return El("document", "Post").add(El("main").add(El("article", "Post by Ada").add(
            El("button", "Remove Like" if pressed else "Like", key="like-p2",
               attrs={"aria-label": "Remove Like" if pressed else "Like",
                      "aria-pressed": "true" if pressed else "false"}, on_click=toggle))))

    h.site.route(f"{BASE}/p/2", page)


async def test_a_recorded_toggle_that_renames_its_control_reads_back_under_the_new_name(tmp_path):
    h = Harness(tmp_path)
    renaming_like_site(h)
    like = {"role": "button", "name": "Like", "within": {"role": "article", "name": "Post by Ada"}}
    draft, _ = h.flows.recorder.build([
        {"action": "navigate", "url": f"{BASE}/p/2"},
        {"action": "click", "locator": like,
         "state_before": {"attributes": {"aria-label": "Like", "aria-pressed": "false"}, "text": "Like"},
         "state_after": {"attributes": {"aria-label": "Remove Like", "aria-pressed": "true"}, "text": "Like"}},
    ], lane="biz-a/social", flow_id="biz-a.social.like-ada", effect="commit")
    rb = draft["steps"][-1]["readback"]["locator"]
    assert rb["name"] == "Remove Like" and rb["within"]["name"] == "Post by Ada" and "nth" not in rb
    run = await h.run(h.save(draft).id)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert h.site.state["liked"] == {"p2"}


def test_a_toggle_that_keeps_its_name_reads_back_on_its_own_locator(tmp_path):
    h = Harness(tmp_path)
    count_tolerant = {"role": "button", "name": r"^Like \d[\d.,]*[KkMm]?$", "match": "regex"}
    draft, _ = h.flows.recorder.build([
        {"action": "click", "locator": count_tolerant,
         "state_before": {"attributes": {"aria-pressed": "false"}, "text": "Like 12"},
         "state_after": {"attributes": {"aria-pressed": "true"}, "text": "Like 13"}},
    ], lane="biz-a/social", flow_id="biz-a.social.like-count")
    assert "locator" not in draft["steps"][-1]["readback"]
    renamed, _ = h.flows.recorder.build([
        {"action": "click", "locator": count_tolerant,
         "state_before": {"attributes": {"aria-pressed": "false"}, "text": "Like 12"},
         "state_after": {"attributes": {"aria-pressed": "true"}, "text": "Liked 13"}},
    ], lane="biz-a/social", flow_id="biz-a.social.like-count2")
    rb = renamed["steps"][-1]["readback"]["locator"]
    assert rb["match"] == "regex" and rb["name"] == r"^Liked \d[\d.,]*[KkMm]?$"


def test_sensitive_events_keep_no_typed_text_or_values_while_recording(tmp_path):
    h = Harness(tmp_path)
    rec = h.flows.recorder
    rec.start("biz-a/social")
    rec.observe("biz-a/social", {"action": "fill", "text": "123456", "locator": {"role": "textbox", "name": "Code"},
                                 "state_after": {"value": "123456", "attributes": {}}})
    stored = json.dumps(h.store.get_recording("biz-a/social"), default=str)
    assert "123456" not in stored and '"sensitive": true' in stored


def test_a_security_check_stops_a_flow_and_asks_the_human():
    from agentos.core.models import Signal
    from agentos.runner.executor import NEEDS_YOU
    from agentos.runner.proof import BAD_SIGNALS

    assert Signal.CHECKPOINT in BAD_SIGNALS
    assert "lane_view" in NEEDS_YOU[Signal.CHECKPOINT] and "—" not in NEEDS_YOU[Signal.CHECKPOINT]
