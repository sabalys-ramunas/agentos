"""What of a run leaves the node. Content stays: what a stranger or a mailbox said is on the node, and
the channel carries that a run happened, how it went, and how much arrived."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from workflow_support import MAIL, OWNER, action, make_node, read_flow, workflow

from openajar.core.models import RunStatus
from openajar.node.channel import reports
from openajar.node.channel.node import NodeChannel
from openajar.node.channel.workflows import WorkflowSync
from openajar.runner.workflows.engine import StepFailed, Walk
from openajar.runner.workflows.mail import Received
from openajar.runner.workflows.mirror import WITHHELD_WORDS, mirror

OWNER_P = {"kind": "human", "id": "owner", "owner": "owner"}
READ = "biz-a.social.read"
SHOWN = "biz-a.social.shown"
SENDER = "jane.stranger@acme.example"
SUBJECT = "Subject only the mailbox knows"
BODY = "Body only the mailbox knows"
SAID = (SENDER, SUBJECT, BODY, "only the mailbox knows", "acme.example")


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.h.save(read_flow(READ, [{"key": "said", "type": "text", "untrusted": True}]), by=OWNER_P)
    return n


def sent_for(node, run_id: str, flow_id: str) -> list[tuple[str, dict]]:
    """What the channel queues for this run, from the channel's own code."""
    sent: list[tuple[str, dict]] = []
    stub = SimpleNamespace(runtime=node.h.runtime, _enqueue_event=lambda kind, payload: sent.append((kind, payload)))
    NodeChannel._enqueue_workflow_run(stub, {"kind": "workflow", "flow": flow_id, "id": run_id})
    return sent


def holds_nothing_said(value) -> None:
    text = json.dumps(value, default=str)
    for words in SAID:
        assert words not in text


async def email_run(node, *steps) -> dict:
    wid = node.wf.save(workflow(*steps, trigger={"kind": "email", "lane": MAIL}), OWNER)["id"]
    node.wf.enable(wid, OWNER)
    box = node.wf.mail.require(MAIL)
    await node.wf.poll_mailbox(box)
    node.mail.inbox.append(Received(7, SENDER, SUBJECT, BODY))
    await node.wf.poll_mailbox(box)
    await node.settle()
    [view] = node.wf.runs(wid)
    return view


NOTIFY = {"id": "n", "kind": "notify", "label": "Tell me", "message": "{{trigger.from}} wrote {{trigger.subject}}: {{trigger.body}}"}


async def test_the_channel_carries_no_word_of_an_email_that_started_a_run(node):
    view = await email_run(node, NOTIFY)
    assert SUBJECT in json.dumps(view)  # it is all there on the node
    [(kind, payload)] = sent_for(node, view["id"], view["flow"])
    assert kind == "workflow_run"
    holds_nothing_said(payload)


async def test_what_arrived_is_a_mark_with_its_kind_and_size(node):
    view = await email_run(node, NOTIFY)
    [(_, payload)] = sent_for(node, view["id"], view["flow"])
    trigger, notify = payload["run"]["steps"]
    assert trigger["arrived"] == "email"
    assert trigger["output"]["withheld"] is True and trigger["output"]["size"] > len(BODY)
    assert notify["input"]["withheld"] is True and notify["input"]["size"] > len(BODY)
    assert (notify["label"], notify["short"], notify["status"]) == ("Tell me", "Told you", "succeeded_unverified")
    assert "untrusted" not in trigger and "untrusted" not in notify


async def test_a_webhooks_names_stay_too_because_a_stranger_chose_them(node):
    wid = node.wf.save(workflow(NOTIFY, trigger={"kind": "webhook"}), OWNER)["id"]
    event = SimpleNamespace(kind="hook", source="webhook", facts={}, content=SimpleNamespace(
        text=json.dumps({"jane_stranger": "only the mailbox knows", "from": SENDER, "subject": SUBJECT}), fields=None))
    run, _ = node.runner.submit(kind="workflow", flow_id=wid, version=node.wf.get_version(wid).version,
                                params=node.wf.trigger_data(event), principal=OWNER_P, source="route:r")
    await node.settle()
    [(_, payload)] = sent_for(node, run.id, wid)
    holds_nothing_said(payload)
    assert "jane_stranger" not in json.dumps(payload)
    assert payload["run"]["steps"][0]["output"]["fields"] == 4 and payload["run"]["steps"][0]["arrived"] == "webhook"


async def test_an_input_a_flow_shows_stays_when_a_stranger_filled_it(node):
    node.h.save({**read_flow(SHOWN), "params": {"properties": {"said": {"type": "string", "show": True}}}}, by=OWNER_P)
    view = await email_run(node, action("look", SHOWN, inputs={"said": "{{trigger.body}}"}))
    assert node.step(view, "look")["input"] == {"said": BODY}
    [(_, payload)] = sent_for(node, view["id"], view["flow"])
    holds_nothing_said(payload)
    assert payload["run"]["steps"][1]["input"]["withheld"] is True


async def test_an_ask_keeps_the_question_it_was_filled_with(node):
    step = {"id": "q", "kind": "ask", "label": "Check", "question": "Answer {{trigger.subject}}?"}
    view = await email_run(node, step)
    [(_, payload)] = sent_for(node, view["id"], view["flow"])
    holds_nothing_said(payload)
    assert view["steps"][1].get("untrusted") is None  # still waiting: it is marked once it has an answer
    card = node.svc.approvals.list(state="pending")[0]
    node.svc.decide(card.id, "approve", answer="Yes")
    await node.settle()
    [view] = node.wf.runs(view["flow"])
    assert node.step(view, "q")["untrusted"] == ["question"]
    [(_, payload)] = sent_for(node, view["id"], view["flow"])
    holds_nothing_said(payload)
    assert payload["run"]["steps"][1]["output"] == {"answer": True} and payload["run"]["steps"][1]["short"] == "Yes"


def sync(node) -> WorkflowSync:
    return WorkflowSync(node.wf, get_meta=lambda k, d: d, set_meta=lambda k, v: None)


async def test_a_run_started_over_the_channel_is_answered_without_content(node):
    wid = node.wf.save(workflow({"id": "n", "kind": "notify", "message": "{{trigger.note}}"}), OWNER)["id"]
    out = sync(node).apply("run", {"id": wid, "values": {"note": BODY}})
    assert out["ok"] is True
    holds_nothing_said(out)


async def test_a_flow_row_says_how_it_went_and_not_what_it_read(node):
    view = await email_run(node, NOTIFY)
    holds_nothing_said(node.wf.public(view["flow"]))
    holds_nothing_said((await reports.inventory(node.h.runtime))["flows"])


# ---- a failure's words ----

async def failed_run(node, monkeypatch, failure: StepFailed) -> dict:
    def stop(self, step, path):
        self._begin(path, step)
        raise failure

    monkeypatch.setattr(Walk, "notify", stop)
    view = await email_run(node, NOTIFY)
    assert view["status"] == "failed"
    return view


async def test_words_that_may_quote_something_stay(node, monkeypatch):
    view = await failed_run(node, monkeypatch, StepFailed("step_failed", f"the site said: {BODY}", outside=True))
    assert node.step(view, "n")["note"] == f"the site said: {BODY}" and view["result"] == f"the site said: {BODY}"
    [(_, payload)] = sent_for(node, view["id"], view["flow"])
    holds_nothing_said(payload)
    assert payload["run"]["steps"][1]["note"] == WITHHELD_WORDS and payload["run"]["result"] == WITHHELD_WORDS


async def test_a_run_retried_over_the_channel_is_answered_without_content(node, monkeypatch):
    view = await failed_run(node, monkeypatch, StepFailed("step_failed", "It stopped.", outside=True))
    out = sync(node).apply("retry", {"id": view["flow"], "run": view["id"]})
    assert out["ok"] is True
    holds_nothing_said(out)


async def test_a_row_does_not_quote_a_failure_either(node, monkeypatch):
    view = await failed_run(node, monkeypatch, StepFailed("step_failed", f"the site said: {BODY}", outside=True))
    row = node.wf.public(view["flow"])
    holds_nothing_said(row)
    assert row["stuck"]["words"] == WITHHELD_WORDS and row["latest"]["result"] == WITHHELD_WORDS


async def test_the_words_the_walk_wrote_itself_cross(node, monkeypatch):
    words = "This message has no way to unsubscribe, so it was not sent."
    view = await failed_run(node, monkeypatch, StepFailed("no_unsubscribe", words))
    [(_, payload)] = sent_for(node, view["id"], view["flow"])
    assert payload["run"]["steps"][1]["note"] == words and payload["run"]["result"] == words


async def test_a_step_whose_run_crashed_says_nothing_of_how(node):
    wid = node.wf.save(workflow(NOTIFY), OWNER)["id"]
    run, _ = node.runner.submit(kind="workflow", flow_id=wid, version=node.wf.get_version(wid).version,
                                params={"trigger": {"from": "a", "subject": "b", "body": "c"}}, principal=OWNER_P, source="x")
    run.status, run.error = RunStatus.FAILED, {"code": "internal", "message": f"ValueError: {BODY}"}
    node.runner.store.save_run(run)
    holds_nothing_said(mirror(node.wf.get_run_view(wid, run.id)))


# ---- the rule ----

def step(**rest) -> dict:
    return {"step": "s", "label": "It", "kind": "action", "status": "failed", "short": None, "started_at": "t", "finished_at": "t",
            "input": None, "output": None, "note": None, "attempts": 1, "approval_id": None, "run_id": None, **rest}


def view(*steps, **rest) -> dict:
    return {"id": "r", "flow": "f", "version": "v", "revision": 1, "status": "failed", "cause": "It started", "result": None,
            "by": "trigger", "started_at": "t", "finished_at": None, "output": None, "steps": list(steps), "cost": {}, **rest}


def test_anything_not_named_stays_so_a_new_field_cannot_leak():
    out = mirror(view(step(said=BODY), said=BODY))
    holds_nothing_said(out)
    assert "said" not in out and "said" not in out["steps"][0]


def test_a_sites_rows_cross_as_a_count():
    rows = [{"id": "A-1", "who": SENDER}, {"id": "A-2", "who": SENDER}]
    out = mirror(view(step(step="trigger", kind="trigger", arrived="web_page",
                           output={"added": rows, "removed": rows[:1], "changed": [], "count": 3, "truncated": False})))
    assert out["steps"][0]["arrived"] == "web_page"
    assert out["steps"][0]["output"]["rows"] == 3 and out["steps"][0]["output"]["fields"] == 5
    holds_nothing_said(out)


def test_what_the_node_itself_counted_crosses():
    out = mirror(view(step(kind="loop", input={"items": 3}, output={"count": 3, "stopped": 1}),
                      step(kind="stop_if_replied", input={"after": "first"}, output={"replied": 1, "waiting": 2}),
                      step(kind="wait", input={"amount": 3, "unit": "days"}),
                      step(kind="branch", input={"left": BODY, "op": "is", "right": "x"}, output={"took": "then"})))
    loop, chase, wait, branch = out["steps"]
    assert loop["input"] == {"items": 3} and loop["output"] == {"count": 3, "stopped": 1}
    assert chase["input"] == {"after": "first"} and chase["output"] == {"replied": 1, "waiting": 2}
    assert wait["input"] == {"amount": 3, "unit": "days"} and branch["output"] == {"took": "then"}
    assert branch["input"]["withheld"] is True and BODY not in json.dumps(branch)


def test_a_flows_count_named_like_the_nodes_stays_with_its_step():
    out = mirror(view(step(kind="action", output={"count": BODY, "took": BODY})))
    holds_nothing_said(out)


def test_a_rows_note_and_a_steps_note_of_its_own_cross():
    out = mirror(view(step(status="skipped", note="This path was not taken.",
                           items=[{"label": "1", "status": "skipped", "note": "They replied, so it stops here", "said": BODY}])))
    assert out["steps"][0]["note"] == "This path was not taken."
    assert out["steps"][0]["items"] == [{"label": "1", "status": "skipped", "note": "They replied, so it stops here"}]


def test_an_output_card_crosses_as_words_the_node_wrote():
    card = {"kind": "email", "title": "Sent", "more": "3 sent", "body": BODY, "who": SENDER}
    assert mirror(view(output=card))["output"] == {"kind": "email", "title": "Sent", "more": "3 sent"}
