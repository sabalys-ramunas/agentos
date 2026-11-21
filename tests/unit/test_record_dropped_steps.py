"""flow_record_stop never saves a flow that lost what it records (QA-2 #15).

On the dev node every click and keystroke reached the recorder without a structural locator, so the
draft was a navigate only, and with effect "commit" it was saved (`saved: true`): a "post" flow that
posts nothing. It is refused with a plain message, as is a draft whose commit step lost its inputs."""

from __future__ import annotations

from test_runner_tools import FakeCtx, spec

from openajar.runner.fakes import BASE, Harness
from openajar.runner.tools import tool_specs

LANE = "biz-a/social"
CAPTION = {"role": "textbox", "name": "Write a caption..."}
SHARE = {"role": "button", "name": "Share"}


async def _stop(tmp_path, events, **kw):
    h = Harness(tmp_path)
    specs = tool_specs(h.runtime)
    ctx = FakeCtx(h.runtime)
    await spec(specs, "flow_record_start").handler(ctx, on=LANE)
    for ev in events:
        h.flows.recorder.observe(LANE, ev)
    out = await spec(specs, "flow_record_stop").handler(ctx, on=LANE, name="biz-a.social.recorded", **kw)
    return h, out


async def test_a_recording_whose_actions_were_all_dropped_is_not_saved(tmp_path):
    h, out = await _stop(tmp_path, [
        {"action": "navigate", "url": f"{BASE}/compose"},
        {"action": "fill", "text": "Hi"},             # no locator: dropped
        {"action": "click"},                          # no locator: dropped
    ], effect="commit")
    assert out["saved"] is False, out
    assert "nothing was saved" in out["problem"] and "2" in out["problem"] and "—" not in out["problem"]
    assert h.flows.list("biz-a.social") == []


async def test_a_commit_step_that_lost_its_inputs_is_not_saved(tmp_path):
    h, out = await _stop(tmp_path, [
        {"action": "navigate", "url": f"{BASE}/compose"},
        {"action": "fill", "text": "Hi"},                    # the caption: no locator, dropped
        {"action": "click", "locator": SHARE},              # the commit: recorded
    ])
    assert out["saved"] is False, out
    assert "input" in out["problem"] and "nothing was saved" in out["problem"]
    assert h.flows.list("biz-a.social") == []


async def test_a_whole_recording_is_still_saved(tmp_path):
    _h, out = await _stop(tmp_path, [
        {"action": "navigate", "url": f"{BASE}/compose"},
        {"action": "fill", "locator": CAPTION, "text": "Hi"},
        {"action": "click", "locator": SHARE},
    ])
    assert out["saved"] is True and "problem" not in out, out


async def test_a_navigation_only_recording_with_nothing_dropped_is_still_a_read_flow(tmp_path):
    _h, out = await _stop(tmp_path, [{"action": "navigate", "url": f"{BASE}/u/biz-a"}])
    assert out["saved"] is True and out["effect"] == "read", out
