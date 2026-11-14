"""Connector operations running on the fake site (docs/design/connectors.md 4.5, Brief B):
acting as, the preflight before the commit, honest statuses around the commit, extract and
for_each, and Run.output checked against the output schema."""

import copy

import pytest
from connector_samples import (
    LANE,
    MAIN,
    SAMPLE_POSTS,
    SITES,
    bind,
    comments_op,
    commit_op,
    connector,
    connector_site,
    live,
    read_op,
)
from pydantic import ValidationError

from agentos.core.models import ControlState, RunStatus
from agentos.runner.connectors.compile import compile_connector
from agentos.runner.connectors.service import ConnectorService
from agentos.runner.driver import DriverTimeout
from agentos.runner.fakes import OWNER, FakeLanes, FakeNotifier, Harness
from agentos.runner.models import FlowDefinition, Locator, NodeInstall, StepStatus
from agentos.runner.util import TemplateError

CAPTION = "Autumn at the lake"
PARAMS = {"caption": CAPTION, "image": "photo.jpg"}  # the binding fills acts_as_id and account_language


def _harness(tmp_path, *, site=None, ops=None, lanes=True, lang="en", acts_as=None, **kw):
    site = site or connector_site()
    services = {"lanes": FakeLanes([LANE]), "notifier": FakeNotifier()} if lanes else {}
    h = Harness(tmp_path, site=site, services=services, **kw)
    live(h.driver)
    doc = connector(ops if ops is not None else {"post_photo": commit_op(), "list_comments": comments_op()})
    ConnectorService(h.flows, sites=SITES).install(compile_connector(doc, sites=SITES), by=OWNER)
    bind(h, account_language=lang, **({"acts_as": acts_as} if acts_as else {}))
    return h


async def _post(h, params=None, **kw):
    return await h.run("connector.social.post_photo", params or PARAMS, lane=LANE, **kw)


def _steps(h, run):
    return {r.path: r for r in h.store.steps(run.id)}


# ---------------------------------------------------------------- the commit path


async def test_a_commit_operation_posts_verified_with_typed_output(tmp_path):
    h = _harness(tmp_path)
    run = await _post(h)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert h.site.state["posts"] == [{"id": "1", "caption": CAPTION, "image": "photo.jpg"}]
    out = run.output
    assert out["data"] == {"post_url": "https://social.example/u/biz-a"}
    assert out["untrusted_fields"] == [] and out["private_fields"] == [] and out["truncated"] is False
    src = out["source"]
    assert src["connector"] == "social" and src["operation"] == "post_photo" and src["version"] == "1.0.0"
    assert src["contract"].startswith("sha256:") and src["lane"] == LANE
    steps = _steps(h, run)
    assert steps["1"].result["identity"] == {"acting_as": "account", "id": "biz-a", "source": "cookie uid",
                                             "status": "match"}
    pre = steps["5"].result["preflight"]
    assert [c["kind"] for c in pre] == ["value_equals", "count", "acting_as"] and all(c["passed"] for c in pre)
    assert h.runner.describe(run)["output"] == out


async def test_names_follow_the_account_language(tmp_path):
    h = _harness(tmp_path, site=connector_site(lang="lt"), lang="lt")
    run = await _post(h)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    # the same page with the binding in English: the Share button is not found, nothing is posted
    h2 = _harness(tmp_path / "en", site=connector_site(lang="lt"))
    run = await _post(h2)
    assert run.status == RunStatus.FAILED
    # the site no longer matches before anything could publish: the operation is broken
    assert run.error["code"] == "connector_contract_broken" and run.error["cause"] == "element_not_found"
    assert run.error["step"] == "6"
    assert run.error["locator_text"] == 'button "Share" within dialog'
    assert h2.site.state["posts"] == []


async def test_a_wrong_account_fails_and_holds_the_lane(tmp_path):
    site = connector_site()
    site.identity = {"site": "social", "user_id": "biz-b", "source": "cookie uid"}
    h = _harness(tmp_path, site=site)
    run = await _post(h)
    assert run.status == RunStatus.FAILED
    err = run.error
    assert err["code"] == "wrong_account" and err["expected"] == "biz-a" and err["found"] == "another account"
    assert err["step"] == "1" and err["acted"] is False
    lane = h.runtime.get("lanes").get(LANE)
    assert lane.control.state == ControlState.PAUSED and lane.control.reason == "needs_human:wrong_account"
    assert h.site.state["posts"] == []
    sent = h.runtime.get("notifier").sent
    assert [m["title"] for m in sent] == ["A lane was held"] and "biz-a" in sent[0]["body"] and "biz-b" not in sent[0]["body"]
    # the next run on the held lane does not start: it waits for the human
    nxt = h.submit("connector.social.post_photo", PARAMS, lane=LANE)
    await h.runner.dispatch_once()
    assert h.runner.get(nxt.id).status == RunStatus.QUEUED and h.runner.get(nxt.id).error["control"] == "paused"


async def test_a_wrong_surface_is_read_from_an_href(tmp_path):
    op = commit_op()
    op["acting_as"] = {"kind": "fb_page", "source": "href", "pattern": "^/u/([a-z0-9-]+)$",
                       "locator": {"role": "link", "within": {"role": "dialog"}}}
    site = connector_site()

    def compose_with_actor(s, d, inner=site.routes["https://social.example/compose"]):
        from agentos.runner.fakes import El

        page = inner(s, d)
        page.children[0].children.insert(0, El("link", "Posting as", attrs={"href": "/u/biz-other"}))
        return page

    site.route("https://social.example/compose", compose_with_actor)
    op["steps"].insert(3, {"type": "assert_identity", "label": "acting as in the composer"})
    op["steps"][1] = {"type": "wait_for", "state": "any", "locator": {"role": "main"}}
    h = _harness(tmp_path, site=site, ops={"post_photo": op}, acts_as={"kind": "fb_page", "id": "biz-a"})
    run = await _post(h)
    assert run.status == RunStatus.FAILED and run.error["code"] == "wrong_surface"
    assert run.error["expected"] == "biz-a" and run.error["found"] == "another fb page" and run.error["step"] == "3"
    assert h.runtime.get("lanes").get(LANE).control.reason == "needs_human:wrong_surface"


async def test_an_identity_that_cannot_be_read_fails_without_a_hold(tmp_path):
    site = connector_site()
    site.identity = None
    h = _harness(tmp_path, site=site)
    run = await _post(h)
    assert run.status == RunStatus.FAILED and run.error["code"] == "identity_unknown"
    assert h.runtime.get("lanes").get(LANE).control.state == ControlState.AGENT


async def test_a_preflight_mismatch_posts_nothing_and_discards_the_draft(tmp_path):
    h = _harness(tmp_path)

    def rewrite(op, target, d):  # the site appends to what was typed
        if op == "fill":
            d.values["caption"] += " #ad"

    h.driver.after_hooks.insert(0, rewrite)
    run = await _post(h)
    assert run.status == RunStatus.FAILED
    err = run.error
    assert err["code"] == "preflight_mismatch" and err["step"] == "5" and err["acted"] is False
    assert "nothing was posted" in err["message"]
    assert err["found"] == [f"another value of {len(CAPTION) + 4} characters"] and err["expected"][0]["kind"] == "value_equals"
    assert err["discard"] == "done" and ("press", "Escape") in h.driver.log
    assert h.site.state["posts"] == []
    assert "6" not in _steps(h, run)  # the commit step never started


async def test_a_missing_image_is_a_preflight_mismatch_after_polling(tmp_path):
    h = _harness(tmp_path)
    h.driver.after_hooks.clear()  # the page never re-renders, so the image never shows
    run = await _post(h)
    assert run.error["code"] == "preflight_mismatch" and run.status == RunStatus.FAILED
    assert "count img within dialog: expected 1, found 0" in run.error["message"]
    assert sum(h.clock.slept) >= 5  # polled for the preflight's timeout first


async def test_after_the_commit_a_failure_is_partial(tmp_path):
    h = _harness(tmp_path)

    def lose(op, target, d):  # the post never shows up on the profile
        if op == "click" and getattr(target, "name", "") == "Share":
            d.site.state["posts"].clear()

    h.driver.after_hooks.append(lose)
    run = await _post(h)
    assert run.status == RunStatus.PARTIAL, run.error
    assert run.error["code"] == "readback_failed" and run.error["step"] == "7"
    assert run.output is None


async def test_the_commit_step_counts_as_publishing_even_if_marked_otherwise(tmp_path):
    h = _harness(tmp_path)
    fv = h.flows.get("connector.social.post_photo")
    doc = copy.deepcopy(fv.definition.canonical())
    doc["steps"][6]["publishes"] = False  # the lint forbids this; the executor does not trust it either
    doc["steps"][7]["url"] = "https://social.example/nowhere"
    h.flows.save(doc, by=NodeInstall(fv.saved_by))
    run = await _post(h)
    assert run.status == RunStatus.PARTIAL and run.error["step"] == "7"


async def test_a_preflight_done_before_a_restart_runs_again(tmp_path):
    site = connector_site()
    h = _harness(tmp_path, site=site)

    def stop_after_preflight(op, target, d):
        if op == "read_state" and getattr(target, "role", None) == "img":
            h.runner._stopping = True  # the node is stopping: the run is requeued at the next step

    h.driver.hooks.append(stop_after_preflight)
    run = h.submit("connector.social.post_photo", PARAMS, lane=LANE)
    await h.runner.run_until_idle()
    assert h.runner.get(run.id).status == RunStatus.QUEUED
    assert _steps(h, run)["5"].status == StepStatus.DONE and "6" not in _steps(h, run)
    # a fresh process and a fresh browser: the composer is gone, so the preflight must not pass
    h2 = Harness(tmp_path, site=site, services={"lanes": FakeLanes([LANE])})
    live(h2.driver)
    await h2.runner.run_until_idle()
    done = h2.runner.get(run.id)
    assert done.status == RunStatus.FAILED and done.error["code"] == "preflight_mismatch", done.error
    assert site.state["posts"] == []


async def test_a_retry_of_the_commit_step_runs_the_preflight_again(tmp_path):
    h = _harness(tmp_path)
    tries = []

    def flaky_share(op, target, d):
        if op == "find" and getattr(target, "label", None) is None and target.describe().startswith('button "Share"'):
            tries.append(1)
            if len(tries) == 1:
                d.values["caption"] = "something else"  # the composer changed while the step waited
                raise DriverTimeout("the page did not answer")

    h.driver.hooks.append(flaky_share)
    run = h.submit("connector.social.post_photo", PARAMS, lane=LANE)
    await h.runner.run_until_idle()
    h.clock.advance(3600)
    await h.runner.run_until_idle()
    done = h.runner.get(run.id)
    assert done.status == RunStatus.FAILED and done.error["code"] == "preflight_mismatch", done.error
    assert done.attempts == 2 and h.site.state["posts"] == []


# ---------------------------------------------------------------- reads: extract and for_each


async def test_extract_and_for_each_read_typed_labelled_capped_comments(tmp_path):
    h = _harness(tmp_path, site=connector_site(posts=SAMPLE_POSTS))
    run = await h.run("connector.social.list_comments", {}, lane=LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    out = run.output
    comments = out["data"]["comments"]
    assert [c["id"] for c in comments] == ["t1_a", "t1_ar", "t1_b", "t1_c"]
    first = comments[0]
    assert first == {"id": "t1_a", "author": "ana", "created_at": "2026-11-04T08:12:00Z",
                     "permalink": "https://social.example/p/1/c/t1_a", "body": "First commen"}
    # own text and own fields: a reply nested in a comment is its own item, never part of its parent
    assert comments[1]["author"] == "bo" and comments[1]["permalink"] == "https://social.example/p/1/c/t1_ar"
    assert comments[2]["permalink"] is None  # a link outside the site's domain is dropped
    assert out["data"]["posts_seen"] == 2
    assert out["untrusted_fields"] == ["comments[].author", "comments[].body"]
    assert out["truncated"] is True  # bodies were clipped to max_chars
    steps = _steps(h, run)
    assert {"3.each0.0", "3.each0.1", "3.each1.0", "3.each1.1"} <= set(steps)
    assert steps["3"].result == {"iterations": 2}


async def test_a_value_that_breaks_its_pattern_is_a_contract_violation(tmp_path):
    site = connector_site(posts=SAMPLE_POSTS)
    site.state["comments"]["2"][0] = ("t1_c", "Not A Handle!", "2026-11-04T10:00:00Z", "/p/2/c/t1_c", "x", [])
    h = _harness(tmp_path, site=site)
    run = await h.run("connector.social.list_comments", {}, lane=LANE)
    assert run.status == RunStatus.FAILED and run.error["code"] == "output_contract_violation"
    assert run.error["step"] == "3.each1.1" and "item 0 author" in run.error["message"]
    assert run.output is None


async def test_an_output_that_breaks_the_schema_fails_the_run(tmp_path):
    op = read_op()
    op["output"]["properties"]["post_count"]["maximum"] = 1
    h = _harness(tmp_path, site=connector_site(posts=SAMPLE_POSTS), ops={"profile_summary": op})
    run = await h.run("connector.social.profile_summary", {}, lane=LANE)
    assert run.status == RunStatus.FAILED and run.error["code"] == "output_contract_violation"
    assert "post_count" in run.error["message"] and run.output is None


async def test_a_committed_run_whose_output_breaks_the_schema_is_partial(tmp_path):
    op = commit_op()
    op["readback"]["live"]["outputs"] = {"post_url": "post.text"}
    h = _harness(tmp_path, ops={"post_photo": op})
    run = await _post(h)
    assert run.status == RunStatus.PARTIAL and run.error["code"] == "output_contract_violation"
    assert h.site.state["posts"]  # it was posted: the owner must know, never a blind re-run


# ---------------------------------------------------------------- the models


def _flow(steps, **params):
    return {"id": "biz-a.social.read", "lane": LANE, "domain": "social.example", "steps": steps,
            "params": {"type": "object", "properties": params}}


@pytest.mark.parametrize(("steps", "params", "message"), [
    ([{"type": "click", "locator": {"role": "button", "names": {"en": "Share"}}}], {},
     "${account_language} is not a declared param"),
    ([{"type": "click", "locator": {"role": "button", "attr": {"name": "type", "equals": "a", "prefix": "b"}}}],
     {}, "exactly one of equals, prefix, regex"),
    ([{"type": "click", "locator": {"names": {"en": "Share"}, "css": ".x"}}], {}, "a name needs a role"),
    ([{"type": "click", "locator": {"role": "button", "name": "a", "names": {"en": "b"}}}], {}, "name or names"),
    ([{"type": "extract", "save_as": "x", "max_items": 3, "items": {"role": "article"},
       "fields": {"a": {"attr": "href"}}},
      {"type": "for_each", "over": "x", "as": "it", "max_items": 3,
       "steps": [{"type": "click", "locator": {"role": "button"}}]}], {}, "no input steps inside for_each"),
    ([{"type": "for_each", "over": "nothing", "as": "it", "max_items": 3,
       "steps": [{"type": "wait_for", "locator": {"role": "main"}}]}], {}, "over 'nothing' is never saved"),
    ([{"type": "assert_identity", "source": "url", "expect": "x"}], {}, "needs a pattern with one capture group"),
    ([{"type": "assert_identity", "source": "href", "pattern": "id=(\\d+)", "expect": "${who}"}], {},
     "source href needs the locator"),
    ([{"type": "assert_identity", "expect": "${who}"}], {}, "${who} is not a declared param"),
    ([{"type": "preflight", "checks": [{"kind": "value_equals", "locator": {"role": "textbox"}, "param": "caption"}]}],
     {}, "param 'caption' is not declared"),
    ([{"type": "preflight", "checks": [{"kind": "count", "locator": {"role": "img"}, "equals": 1,
                                        "expect": "x"}]}], {}, "takes no identity fields"),
])
def test_step_validation(steps, params, message):
    with pytest.raises(ValidationError) as e:
        FlowDefinition.model_validate(_flow(steps, **params))
    assert message in str(e.value)


def test_names_render_to_one_name_per_language():
    loc = Locator(role="button", names={"en": "Share", "lt": "Bendrinti"}, within={"role": "dialog"})
    assert loc.render({"account_language": "lt"}).name == "Bendrinti"
    assert loc.render({"account_language": "lt"}).names is None
    with pytest.raises(TemplateError, match="no entry for account language 'de'"):
        loc.render({"account_language": "de"})
    attr = Locator(attr={"name": "data-account", "equals": "${acts_as_id}"}, within=MAIN)
    assert attr.render({"acts_as_id": "biz-a"}).attr.equals == "biz-a"
    assert attr.describe() == 'element [data-account="${acts_as_id}"] within main'
