"""Recorder edge cases: sensitive typing, typed text that also appears in a URL, and draft shape.

BUG-13 and BUG-20 from the QA report were strict xfails here; both are fixed.
"""

import json

from agentos.runner.fakes import BASE, Harness

LANE = "biz-a/social"
PASSWORD_LOCATOR = {"role": "textbox", "name": "Password"}


def test_sensitive_typing_is_dropped_from_the_draft_flow(tmp_path):
    h = Harness(tmp_path)
    events = [
        {"action": "type", "text": "hunter2-SECRET", "sensitive": True, "locator": PASSWORD_LOCATOR},
        {"action": "click", "locator": {"role": "button", "name": "Log in"}},
    ]
    draft, notes = h.flows.recorder.build(events, lane=LANE, flow_id="biz-a.social.login")
    assert "hunter2" not in json.dumps(draft)
    assert any("sensitive" in note for note in notes)


def test_typed_passwords_are_not_kept_in_the_recording_store(tmp_path):
    h = Harness(tmp_path)
    rec = h.flows.recorder
    rec.start(LANE)
    rec.observe(LANE, {"action": "type", "text": "hunter2-SECRET", "sensitive": True,
                       "locator": PASSWORD_LOCATOR})
    assert "hunter2-SECRET" not in json.dumps(h.store.get_recording(LANE), default=str)


def test_typed_text_that_also_appears_in_a_url_only_replaces_the_param_value(tmp_path):
    h = Harness(tmp_path)
    events = [
        {"action": "type", "text": "example", "locator": {"role": "textbox", "name": "Search"}},
        {"action": "navigate", "url": f"{BASE}/u/biz-a"},  # the host itself contains "example"
    ]
    draft, _ = h.flows.recorder.build(events, lane=LANE, flow_id="biz-a.social.search")
    navigations = [s["url"] for s in draft["steps"] if s["type"] == "navigate"]
    assert navigations == [f"{BASE}/u/biz-a"]


def test_a_click_that_toggles_its_state_becomes_a_verified_write(tmp_path):
    h = Harness(tmp_path)
    events = [{"action": "click", "locator": {"role": "button", "name": "Like"},
               "state_before": {"attributes": {"aria-pressed": "false"}},
               "state_after": {"attributes": {"aria-pressed": "true"}}}]
    draft, _ = h.flows.recorder.build(events, lane=LANE, flow_id="biz-a.social.like")
    click = draft["steps"][-1]
    assert click["writes"] is True
    assert click["readback"] == {"condition": {"kind": "attribute", "name": "aria-pressed", "equals": "true"}}
