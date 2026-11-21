"""Flow execution and proof of done against the fake social site."""

from openajar.core.driver import DriverTimeout
from openajar.core.models import RunStatus, Signal
from openajar.runner.fakes import BASE, Harness, post_flow
from openajar.runner.util import json_size

POST = {"role": "article", "name": "Post by Ieva"}
POST_LIKE = {"role": "button", "name": "Like", "within": POST}
PRESSED = {"kind": "attribute", "name": "aria-pressed", "equals": "true"}


def like_flow(click_locator=None, readback_locator=None, **kw):
    """Like the post (not a comment), skipping it if the human already liked it."""
    flow = {
        "id": "biz-a.social.like", "lane": "biz-a/social", "effect": "commit",
        "steps": [
            {"type": "navigate", "url": f"{BASE}/p/1"},
            {"type": "branch", "if": {"locator": POST_LIKE, "condition": PRESSED},
             "then": [],
             "else": [{"type": "click", "locator": click_locator or POST_LIKE, "writes": True,
                       "readback": {"locator": readback_locator or POST_LIKE, "condition": PRESSED,
                                    "timeout_ms": 1000}}]},
            {"type": "readback", "locator": POST_LIKE, "condition": PRESSED, "timeout_ms": 1000},
        ],
    }
    flow.update(kw)
    return flow


async def test_post_flow_is_verified_by_the_targets_own_state(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    run = await h.run(fv.id, {"caption": "Autumn in Vilnius", "image": "files/photo-1.jpg"})
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert h.site.state["posts"] == [{"id": "1", "caption": "Autumn in Vilnius", "image": "files/photo-1.jpg"}]
    rbs = run.proof["readbacks"]
    assert [rb["passed"] for rb in rbs] == [True, True]
    assert rbs[-1]["found"] == "count 1 (changed by +1)"
    assert run.proof["writes"] == 1 and run.proof["url"] == f"{BASE}/u/biz-a"
    assert (tmp_path / run.proof["screenshot"]).exists()
    ops = [op for op, _ in h.driver.log]
    assert ops.count("upload") == 1 and ops.count("fill") == 1
    assert 0.3 <= sum(h.clock.slept[:1]) <= 0.9  # the pause step, from the seeded rng


async def test_like_targets_the_post_not_a_comment(tmp_path):
    h = Harness(tmp_path)
    h.site.state["liked"] = {"c1"}  # the human liked Beata's comment earlier
    fv = h.save(like_flow())
    run = await h.run(fv.id)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert h.site.state["liked"] == {"c1", "p1"}
    assert ("click", 'button "Like" [like-p1]') in h.driver.log


async def test_already_liked_post_is_skipped(tmp_path):
    h = Harness(tmp_path)
    h.site.state["liked"] = {"p1"}
    fv = h.save(like_flow())
    run = await h.run(fv.id)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED
    assert not [e for e in h.driver.log if e[0] == "click"]
    assert h.site.state["liked"] == {"p1"}
    branch = next(s for s in h.runner.steps(run.id) if s["type"] == "branch")
    assert branch["taken"] == "then"


async def test_readback_that_passes_only_because_of_a_nested_element_fails(tmp_path):
    """The PoC bug: the flow clicked a comment's Like, and a check of 'some Like inside the
    post' would have passed. The post's own state is what must be read."""
    h = Harness(tmp_path)
    wrong_click = {"role": "button", "name": "Like", "within": POST, "own": False, "nth": 1}  # Beata's comment
    fv = h.save(like_flow(click_locator=wrong_click))
    run = await h.run(fv.id)
    assert h.site.state["liked"] == {"c1"}  # the comment got liked, the post did not
    assert run.status == RunStatus.PARTIAL  # something was clicked, the intended effect is not there
    err = run.error
    assert err["code"] == "readback_failed" and err["step"] == "1.else.0"
    assert err["expected"] == "attribute aria-pressed equals 'true'" and err["found"] == "false"
    assert "within" in err["locator"] and err["locator_text"].startswith('button "Like" within article')
    assert err["screenshot"] and (tmp_path / err["screenshot"]).exists()
    assert "flow_save" in err["repair"]


async def test_naive_readbacks_do_not_pass_on_nested_state(tmp_path):
    h = Harness(tmp_path)
    h.site.state["liked"] = {"c1"}  # only a comment is liked
    checks = {
        "any_like_in_post": {"locator": {**POST_LIKE, "own": False}, "condition": PRESSED},
        "post_text_says_liked": {"locator": POST, "condition": {"kind": "text", "contains": "Liked"}},
        "first_like_on_page": {"locator": {"role": "button", "name": "Like"}, "condition": PRESSED},
        "post_own_like": {"locator": POST_LIKE, "condition": PRESSED},
    }
    for name, check in checks.items():
        fv = h.save({"id": f"biz-a.check.{name.replace('_', '-')}", "lane": "biz-a/social",
                     "steps": [{"type": "navigate", "url": f"{BASE}/p/1"},
                               {"type": "readback", "timeout_ms": 0, **check}]})
        run = await h.run(fv.id)
        assert run.status == RunStatus.FAILED, name
        assert run.error["code"] == "readback_failed", name
    # and the comment's own state does read as liked, so the checks are not vacuous
    comment_like = {"role": "button", "name": "Like", "within": {"role": "article", "name": "Comment by Beata"}}
    fv = h.save({"id": "biz-a.check.comment", "lane": "biz-a/social",
                 "steps": [{"type": "navigate", "url": f"{BASE}/p/1"},
                           {"type": "readback", "locator": comment_like, "condition": PRESSED}]})
    assert (await h.run(fv.id)).status == RunStatus.SUCCEEDED_VERIFIED


async def test_failure_is_structured_for_repair(tmp_path):
    h = Harness(tmp_path)
    flow = post_flow()
    flow["steps"][3] = {"type": "click", "locator": {"role": "button", "name": "Create"}}  # renamed on the site
    fv = h.save(flow)
    run = await h.run(fv.id, {"caption": "x", "image": "a.jpg"})
    assert run.status == RunStatus.FAILED and run.attempts == 1
    err = run.error
    assert err["code"] == "element_not_found" and err["index"] == 3 and err["type"] == "click"
    assert err["expected"] == 'exactly one button "Create"' and err["found"] == "no match"
    assert err["acted"] is False and err["screenshot"]
    steps = h.runner.steps(run.id)
    assert [s["status"] for s in steps] == ["done", "done", "done", "failed"]

    # the agent repairs it with a new version and the next run passes
    flow["steps"][3] = {"type": "click", "locator": {"role": "button", "name": "New post"}}
    fixed = h.save(flow)
    assert fixed.version != fv.version
    assert (await h.run(fv.id, {"caption": "x", "image": "a.jpg"})).status == RunStatus.SUCCEEDED_VERIFIED


async def test_ambiguous_locator_is_an_error_not_the_first_match(tmp_path):
    h = Harness(tmp_path)
    fv = h.save({"id": "biz-a.social.like-any", "lane": "biz-a/social", "steps": [
        {"type": "navigate", "url": f"{BASE}/p/1"},
        {"type": "click", "locator": {"role": "button", "name": "Like"}}]})
    run = await h.run(fv.id)
    assert run.status == RunStatus.FAILED
    assert run.error["code"] == "ambiguous_locator" and run.error["found"] == "3 matches"
    assert h.site.state["liked"] == set()


async def test_huge_page_text_is_clipped_everywhere(tmp_path):
    h = Harness(tmp_path)
    h.driver.big_text = "spam " * 200_000  # a 1 MB text node
    fv = h.save({"id": "biz-a.social.read", "lane": "biz-a/social", "steps": [
        {"type": "navigate", "url": f"{BASE}/p/1"},
        {"type": "readback", "locator": POST, "condition": {"kind": "text", "equals": "Lovely"}, "timeout_ms": 0}]})
    run = await h.run(fv.id)
    assert run.status == RunStatus.FAILED
    assert len(run.error["found"]) < 1000 and "truncated" in run.error["found"]
    assert json_size(h.runner.describe(run, steps=True)) < 20_000
    row = h.store._rows("SELECT length(doc) AS n FROM runs WHERE id=?", (run.id,))[0]
    assert row["n"] < 50_000


async def test_captcha_needs_a_human_and_is_not_retried(tmp_path):
    h = Harness(tmp_path)
    h.site.signal = Signal.CAPTCHA
    fv = h.save(post_flow())
    run = await h.run(fv.id, {"caption": "x", "image": "a.jpg"})
    assert run.status == RunStatus.FAILED and run.attempts == 1
    assert run.error["code"] == "signal_captcha" and "needs you" in run.error["message"]


async def test_unchecked_readback_is_unverified_then_rechecked(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    flaky = {"on": True}

    def profile_read_times_out(op, target, d):
        if op == "read_state" and flaky["on"] and d.url.endswith("/u/biz-a") and h.site.state["posts"]:
            raise DriverTimeout("profile did not load")

    h.driver.hooks.append(profile_read_times_out)
    run = await h.run(fv.id, {"caption": "x", "image": "a.jpg"})
    assert run.status == RunStatus.SUCCEEDED_UNVERIFIED
    assert run.proof["readbacks"][-1]["passed"] is None and "read failed" in run.proof["readbacks"][-1]["note"]
    assert run.recheck_at is not None and "could not be checked" in run.proof["note"]

    flaky["on"] = False
    await h.runner.run_until_idle(rechecks=True)  # not due yet
    assert h.runner.get(run.id).status == RunStatus.SUCCEEDED_UNVERIFIED
    h.clock.advance(301)
    await h.runner.run_until_idle(rechecks=True)
    again = h.runner.get(run.id)
    assert again.status == RunStatus.SUCCEEDED_VERIFIED and again.recheck_at is None
    assert again.proof["readbacks"][-1]["rechecked"] is True
    assert len(h.site.state["posts"]) == 1  # a re-check reads, it never acts


async def test_recheck_gives_up_and_stays_honest(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    h.driver.hooks.append(lambda op, t, d: (_ for _ in ()).throw(DriverTimeout("slow"))
                          if op == "read_state" and h.site.state["posts"] and d.url.endswith("/u/biz-a") else None)
    run = await h.run(fv.id, {"caption": "x", "image": "a.jpg"})
    for _ in range(4):
        h.clock.advance(7300)
        await h.runner.run_until_idle(rechecks=True)
    final = h.runner.get(run.id)
    assert final.status == RunStatus.SUCCEEDED_UNVERIFIED and final.recheck_at is None
    assert "still unverified after 3 re-checks" in final.proof["note"]


async def test_flow_without_readback_is_never_verified(tmp_path):
    h = Harness(tmp_path)
    fv = h.save({"id": "biz-a.social.open", "lane": "biz-a/social", "effect": "draft", "steps": [
        {"type": "navigate", "url": f"{BASE}/"},
        {"type": "click", "locator": {"role": "button", "name": "New post"}}]})
    run = await h.run(fv.id)
    assert run.status == RunStatus.SUCCEEDED_UNVERIFIED
    assert "declares no readback" in run.proof["note"] and run.recheck_at is None


async def test_recorder_turns_actions_into_a_runnable_draft(tmp_path):
    h = Harness(tmp_path)
    rec = h.flows.recorder
    lane = "biz-a/social"
    rec.start(lane, {"id": "claude-code"})
    caption = {"role": "textbox", "name": "Write a caption..."}
    events = [
        {"action": "navigate", "url": f"{BASE}/"},
        {"action": "click", "locator": {"role": "button", "name": "New post"}},
        {"action": "upload", "locator": {"role": "button", "name": "Select from computer"}, "text": "a.jpg"},
        {"action": "fill", "locator": caption, "text": "First light", "state_after": {"value": "First light"}},
        {"action": "type", "locator": {"role": "textbox", "name": "Password"}, "text": "hunter2"},
        {"action": "click", "ref": "e9"},
        {"action": "snapshot"},
        {"action": "click", "locator": {"role": "button", "name": "Share"}},
    ]
    for ev in events:
        assert rec.observe(lane, ev)
    assert not rec.observe("biz-b/social", events[0])  # nothing recording there
    stopped = rec.stop(lane)
    draft, notes = rec.build(stopped["events"], lane=lane, flow_id="biz-a.social.recorded")
    assert draft["params"]["required"] == ["file", "caption"]
    assert draft["params"]["properties"]["caption"]["examples"] == ["First light"]
    assert "hunter2" not in str(draft)
    assert any("sensitive" in n for n in notes) and any("no structural locator" in n for n in notes)
    types = [s["type"] for s in draft["steps"]]
    assert types == ["navigate", "wait_for", "click", "upload", "fill", "click"]
    assert draft["steps"][4]["readback"]["condition"] == {"kind": "value", "equals": "${caption}"}
    assert draft["effect"] == "draft"

    fv = h.save(draft)
    run = await h.run(fv.id, {"file": "b.jpg", "caption": "Replayed"})
    assert run.status == RunStatus.SUCCEEDED_VERIFIED  # the fill readback is the only check
    assert h.site.state["posts"][-1]["caption"] == "Replayed"


async def test_recorder_derives_a_readback_from_a_state_toggle(tmp_path):
    h = Harness(tmp_path)
    draft, _ = h.flows.recorder.build([
        {"action": "navigate", "url": f"{BASE}/p/1"},
        {"action": "click", "locator": POST_LIKE,
         "state_before": {"attributes": {"aria-pressed": "false"}},
         "state_after": {"attributes": {"aria-pressed": "true"}}},
    ], lane="biz-a/social", effect="commit")
    click = draft["steps"][-1]
    assert click["writes"] is True and click["readback"]["condition"] == PRESSED
    fv = h.save(draft)
    assert (await h.run(fv.id)).status == RunStatus.SUCCEEDED_VERIFIED
    assert h.site.state["liked"] == {"p1"}
