"""Turning a routine on checks it is finished, the same way the app does: every param a step needs is filled in,
and nothing that posts or commits comes before the step that asks a person. Saving a draft is always allowed."""

from __future__ import annotations

import pytest
from workflow_support import OWNER, action, commit_flow, make_node, read_flow, workflow

from openajar.runner.workflows.service import WorkflowInvalid

OWNER_P = {"kind": "human", "id": "owner", "owner": "owner"}
POST = "biz-a.social.post"
ASK = {"id": "read", "kind": "ask", "label": "ask you to read it", "question": "Is this post ready to go out?"}


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.h.save(read_flow(), by=OWNER_P)
    n.h.save(commit_flow(), by=OWNER_P)
    return n


def test_the_catalog_says_which_params_a_post_needs(node):
    ops = {op["id"]: op for op in node.wf.catalog(person=True)["operations"]}
    assert ops[POST]["params"]["caption"]["required"] is True and ops[POST]["params"]["image"]["required"] is True


def test_turning_on_refuses_a_post_with_an_empty_caption_and_no_image_and_names_them(node):
    wid = node.wf.save(workflow(action("post", POST, label="post it", inputs={"caption": ""})), OWNER)["id"]
    assert [m["words"] for m in node.wf.public(wid)["missing"]] == ['Fill in caption and image for "post it".']
    with pytest.raises(WorkflowInvalid) as refused:
        node.wf.enable(wid, OWNER)
    assert refused.value.problems == [{"target": "post", "code": "input_missing", "words": 'Fill in caption and image for "post it".'}]
    assert node.wf.public(wid)["enabled"] is False

    node.wf.save(workflow(action("post", POST, label="post it", inputs={"caption": "Hello", "image": "a.jpg"})), OWNER, flow_id=wid)
    assert node.wf.enable(wid, OWNER)["enabled"] is True


def test_turning_on_refuses_a_post_that_comes_before_the_question_and_saving_a_draft_still_works(node):
    post = action("post", POST, label="post it", inputs={"caption": "Hello", "image": "a.jpg"})
    wid = node.wf.save(workflow(post, ASK), OWNER)["id"]  # a draft: saved, and says what is missing
    assert node.wf.public(wid)["enabled"] is False
    assert [m["words"] for m in node.wf.public(wid)["missing"]] == [
        '"post it" would run before anyone is asked. Move it after "ask you to read it".']
    with pytest.raises(WorkflowInvalid):
        node.wf.enable(wid, OWNER)

    node.wf.save(workflow(ASK, post), OWNER, flow_id=wid)
    assert node.wf.enable(wid, OWNER)["enabled"] is True


def test_a_version_already_running_before_these_rules_keeps_running(node):
    """An upgrade never stops a routine someone relies on: the new rules gate turning a version on, not its runs."""
    post = action("post", POST, label="post it", inputs={"caption": ""})
    wid = node.wf.save(workflow(post, ASK), OWNER)["id"]
    state = node.wf._state(wid)  # what an older node left: this version on and live
    state.enabled, state.live = True, node.wf.get_version(wid).version
    node.wf.store.put_state(state)
    node.wf.check_runnable(wid)  # no refusal for the turn-on-only rules
    assert {m["words"] for m in node.wf.public(wid)["missing"]} == {
        'Fill in caption and image for "post it".', '"post it" would run before anyone is asked. Move it after "ask you to read it".'}
