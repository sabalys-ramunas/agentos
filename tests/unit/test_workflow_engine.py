"""A workflow run from step to step: data between steps, branches, loops, waits, questions, history."""

from __future__ import annotations

from datetime import timedelta

import pytest
from workflow_support import LANE, OWNER, POST_INPUTS, action, commit_flow, make_node, read_flow, workflow

from openajar.core.errors import InvalidRequest
from openajar.core.models import RunStatus
from openajar.runner.fakes import BASE


@pytest.fixture
def node(tmp_path):
    return make_node(tmp_path)


def owner_flow(node, definition):
    return node.h.save(definition, by={"kind": "human", "id": "owner", "owner": "owner"})


READ = "biz-a.social.read"
POST = "biz-a.social.post"


def saved(node, *steps, **kw):
    return node.wf.save(workflow(*steps, **kw), OWNER)["id"]


async def test_a_run_shows_each_step_with_its_input_output_timing_and_status(node):
    owner_flow(node, read_flow())
    owner_flow(node, commit_flow())
    wid = saved(node, action("look", READ), action("post", POST, inputs=POST_INPUTS))
    view = await node.run(wid)
    assert view["status"] == "succeeded_unverified"  # the read checks nothing, so the run says so
    assert [s["step"] for s in view["steps"]] == ["trigger", "look", "post"]
    post = node.step(view, "post")
    assert post["status"] == "succeeded_verified" and post["started_at"] and post["finished_at"] and post["attempts"] == 1
    assert post["input"] == {"caption": "hello", "image": "a.jpg"} and post["run_id"]
    assert node.site.state["posts"][0]["caption"] == "hello"
    assert node.runner.get(post["run_id"]).parent == view["id"]


async def test_a_step_reads_what_the_step_before_it_gave_and_a_whole_reference_keeps_its_type(node, monkeypatch):
    owner_flow(node, read_flow(produces=[{"key": "left", "type": "number", "untrusted": False}]))
    owner_flow(node, commit_flow())
    from openajar.runner.workflows import engine

    monkeypatch.setattr(engine.Walk, "_output", lambda self, child, op: ({"left": 7}, set()))
    wid = saved(node, action("look", READ),
                {"id": "b", "kind": "branch", "when": {"left": "{{look.left}}", "op": "more_than", "right": "5"},
                 "then": [action("post", POST, inputs={"caption": "left: {{look.left}}", "image": "a.jpg"})],
                 "otherwise": [{"id": "n", "kind": "notify", "message": "low"}]})
    view = await node.run(wid)
    assert node.site.state["posts"][0]["caption"] == "left: 7"
    assert node.step(view, "b")["output"] == {"took": "then"}
    assert node.step(view, "n")["status"] == "skipped" and node.step(view, "n")["note"] == "This path was not taken."


async def test_a_value_that_looks_like_a_reference_is_only_text(node):
    owner_flow(node, commit_flow())
    wid = saved(node, action("post", POST, inputs={"caption": "{{trigger.note}}", "image": "a.jpg"}))
    view = await node.run(wid, values={"note": "{{trigger.when}} ${caption} {{post.url}}"})
    assert view["status"] == "succeeded_verified"
    assert node.site.state["posts"][0]["caption"] == "{{trigger.when}} ${caption} {{post.url}}"


async def test_a_loop_runs_its_body_for_each_item_and_keeps_a_row_per_item(node, monkeypatch):
    owner_flow(node, read_flow(produces=[{"key": "names", "type": "list", "untrusted": False},
                                         {"key": "names_who", "untrusted": False}]))
    owner_flow(node, commit_flow())
    from openajar.runner.workflows import engine

    monkeypatch.setattr(engine.Walk, "_output", lambda self, child, op: (
        ({"names": [{"who": "a"}, {"who": "b"}]}, set()) if op.flow_id == READ else ({}, set())))
    wid = saved(node, action("look", READ),
                {"id": "each", "kind": "loop", "over": "{{look.names}}", "item": "name",
                 "body": [action("post", POST, inputs={"caption": "for {{each.who}}", "image": "a.jpg"})]})
    view = await node.run(wid)
    assert [p["caption"] for p in node.site.state["posts"]] == ["for a", "for b"]
    assert [i["status"] for i in node.step(view, "post")["items"]] == ["succeeded_verified"] * 2
    assert node.step(view, "each")["short"] == "2 done"


async def test_a_wait_gives_up_its_slot_and_carries_on_when_its_time_comes(node):
    owner_flow(node, read_flow())
    wid = saved(node, {"id": "w", "kind": "wait", "label": "wait a day", "amount": 1, "unit": "days"}, action("look", READ))
    view = await node.run(wid)
    assert node.step(view, "w")["status"] == "waiting" and node.step(view, "look")["status"] == "queued"
    assert view["status"] == "running" and not node.runner._active
    node.h.clock.advance(timedelta(hours=25).total_seconds())
    await node.settle()
    view = node.view(wid, view["id"])
    assert view["status"] == "succeeded_unverified" and node.step(view, "w")["status"] == "succeeded_unverified"


async def test_a_workflow_restarted_mid_way_does_not_do_a_finished_step_again(node, tmp_path):
    owner_flow(node, commit_flow())
    wid = saved(node, action("post", POST, inputs=POST_INPUTS), {"id": "w", "kind": "wait", "amount": 1, "unit": "minutes"},
                {"id": "n", "kind": "notify", "message": "done"})
    view = await node.run(wid)
    assert len(node.site.state["posts"]) == 1 and node.step(view, "n")["status"] == "queued"
    node.runner.recover()
    node.h.clock.advance(120)
    await node.settle()
    assert len(node.site.state["posts"]) == 1
    assert node.view(wid, view["id"])["status"] == "succeeded_verified"  # its one action was checked


async def test_asking_a_person_waits_for_the_answer_and_a_no_stops_it(node):
    owner_flow(node, commit_flow())
    wid = saved(node, {"id": "q", "kind": "ask", "label": "ask you", "question": "Go?"}, action("post", POST, inputs=POST_INPUTS))
    view = await node.run(wid)
    step = node.step(view, "q")
    assert view["status"] == "waiting_approval" and step["status"] == "waiting" and step["approval_id"]
    node.svc.decide(step["approval_id"], "approve", by="owner", answer="No")
    await node.settle()
    view = node.view(wid, view["id"])
    assert view["status"] == "cancelled" and node.site.state["posts"] == []
    assert node.step(view, "q")["output"] == {"answer": False} and node.step(view, "q")["short"] == "No"


async def test_a_yes_goes_on_and_the_answer_can_be_read(node):
    owner_flow(node, commit_flow())
    wid = saved(node, {"id": "q", "kind": "ask", "question": "Go?", "on_no": "continue"},
                {"id": "b", "kind": "branch", "when": {"left": "{{q.answer}}", "op": "is", "right": "yes"},
                 "then": [action("post", POST, inputs=POST_INPUTS)]})
    view = await node.run(wid)
    node.svc.decide(node.step(view, "q")["approval_id"], "approve", by="owner", answer="Yes")
    await node.settle()
    assert len(node.site.state["posts"]) == 1


async def test_a_failed_step_stops_the_run_honestly_and_a_retry_carries_on_from_it(node):
    owner_flow(node, read_flow())
    owner_flow(node, {**read_flow("biz-a.social.broken"), "steps": [
        {"type": "navigate", "url": f"{BASE}/nowhere"},
        {"type": "readback", "locator": {"role": "heading", "name": "Not on this page"}, "condition": {"kind": "exists"}}]})
    owner_flow(node, commit_flow())
    wid = saved(node, action("look", READ), action("try", "biz-a.social.broken"), action("post", POST, inputs=POST_INPUTS))
    view = await node.run(wid)
    assert view["status"] == "failed", view
    assert node.step(view, "look")["status"] == "succeeded_unverified"
    assert node.step(view, "try")["status"] == "failed" and node.step(view, "try")["note"]
    assert node.step(view, "post")["status"] == "skipped" and node.site.state["posts"] == []
    first_look = node.step(view, "look")["run_id"]
    owner_flow(node, read_flow("biz-a.social.broken"))  # the page is repaired
    again = node.wf.retry(wid, view["id"], OWNER)
    assert again["by"] == "retry"
    await node.settle()
    view = node.view(wid, view["id"])
    assert view["status"] == "succeeded_unverified" and view["by"] == "retry"
    assert node.step(view, "try")["attempts"] == 2 and len(node.site.state["posts"]) == 1
    assert node.step(view, "look")["run_id"] == first_look and node.step(view, "look")["attempts"] == 1


async def test_a_step_that_fails_in_a_loop_is_retried_from_that_item(node, monkeypatch):
    from openajar.runner.workflows import engine

    owner_flow(node, read_flow(produces=[{"key": "xs", "type": "list", "untrusted": False}]))
    owner_flow(node, commit_flow())
    monkeypatch.setattr(engine.Walk, "_output", lambda self, child, op: ({"xs": ["a", "b", "c"]}, set()) if op.flow_id == READ else ({}, set()))
    real = engine.Walk._submit
    broken = {"on": True}

    def submit(self, op, params, key, path, outside, **kw):
        if broken["on"] and params.get("caption") == "b":
            raise engine.StepFailed("site_down", "The site did not answer.")
        return real(self, op, params, key, path, outside, **kw)

    monkeypatch.setattr(engine.Walk, "_submit", submit)
    wid = saved(node, action("look", READ), {"id": "each", "kind": "loop", "over": "{{look.xs}}", "body": [
        action("post", POST, inputs={"caption": "{{each.value}}", "image": "a.jpg"})]})
    view = await node.run(wid)
    assert view["status"] == "partial" and [p["caption"] for p in node.site.state["posts"]] == ["a"]
    assert [i["status"] for i in node.step(view, "post")["items"]] == ["succeeded_verified", "failed"]
    broken["on"] = False
    node.wf.retry(wid, view["id"], OWNER)
    await node.settle()
    assert [p["caption"] for p in node.site.state["posts"]] == ["a", "b", "c"]  # "a" was not done twice
    assert node.view(wid, view["id"])["status"] == "succeeded_unverified"


async def test_a_page_change_starts_a_workflow_and_only_what_the_node_saw_is_trusted(node):
    owner_flow(node, read_flow())
    trigger = {"kind": "page_change", "lane": LANE, "url": f"{BASE}/u/biz-a", "label": "when the page changes"}
    wid = saved(node, action("look", READ), trigger=trigger)
    node.wf.enable(wid, OWNER)
    scheduler = node.runner.scheduler
    s = scheduler.store.get_schedule(node.wf.store.state(wid).schedule_id)
    assert s.kind == "watch" and s.lane == LANE and s.watch.url == f"{BASE}/u/biz-a" and s.route_id
    scheduler._fire(s, f"watch:{s.id}:1", {"kind": "watch", "seq": 1, "value": "what the page said", "previous": "before"})
    await node.settle()
    [view] = node.wf.runs(wid)
    assert view["cause"] == "A page changed" and view["steps"][0]["output"]["what"] == "what the page said"
    assert view["steps"][0]["untrusted"] == ["what"]


async def test_only_a_failed_run_can_be_retried(node):
    owner_flow(node, read_flow())
    wid = saved(node, action("look", READ))
    view = await node.run(wid)
    with pytest.raises(InvalidRequest):
        node.wf.retry(wid, view["id"], OWNER)


async def test_a_run_stays_on_the_version_it_started_on(node):
    owner_flow(node, commit_flow())
    wid = saved(node, {"id": "w", "kind": "wait", "amount": 1, "unit": "minutes"}, action("post", POST, inputs=POST_INPUTS))
    view = await node.run(wid)
    node.wf.save(workflow({"id": "n", "kind": "notify", "message": "new"}), OWNER, flow_id=wid)
    node.h.clock.advance(120)
    await node.settle()
    assert len(node.site.state["posts"]) == 1 and node.view(wid, view["id"])["version"] == view["version"]


async def test_cancelling_a_workflow_takes_its_waiting_step_with_it(node):
    node.svc.ask_first.set("lane", LANE, True, by="owner", person=True)
    owner_flow(node, commit_flow())
    wid = saved(node, action("post", POST, inputs=POST_INPUTS))
    view = await node.run(wid)
    child = node.step(view, "post")["run_id"]
    assert node.runner.get(child).status == RunStatus.WAITING_APPROVAL
    await node.runner.cancel(view["id"])
    await node.settle()
    assert node.runner.get(child).status == RunStatus.CANCELLED
    assert node.view(wid, view["id"])["status"] == "cancelled" and node.site.state["posts"] == []


def test_a_saved_workflow_reads_like_a_flow_row(node):
    owner_flow(node, read_flow())
    wid = saved(node, action("look", READ), trigger={"kind": "schedule", "cron": "0 9 * * 1-5", "label": "every weekday"})
    row = node.wf.public(wid)
    assert row["id"] == "biz-a.routine.a-job" == wid and row["lane"] == "biz-a/*" and row["revision"] == 1
    assert row["enabled"] is False and row["live_version"] is None and row["ask_first"] is False
    assert row["sites"] == ["social"] and row["effect"] == "read" and row["missing"] == []
    assert row["version"].startswith("sha256:")
