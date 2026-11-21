"""Recorder.build_checked, pinned: the draft, the notes and the refusal for recordings that
reach every branch (a characterization test, written before build_checked was split into steps).
The outcomes are in golden/recorder_build/expected.json."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openajar.runner.recorder import Recorder

BASE = "https://x.test"
LIKE = {"role": "button", "name": "Like", "within": {"role": "article", "name": "Post by mira"}}
CAPTION = {"role": "textbox", "name": "Write a caption..."}
SHARE = {"role": "button", "name": "Share"}
CASES = {
    "every_kind_of_step": [
        {"action": "navigate", "url": f"{BASE}/a"},
        {"action": "navigate", "url": f"{BASE}/b"},
        {"action": "snapshot"}, {"action": "hover"}, {"action": "read"}, {"action": "screenshot"},
        {"action": "click", "ok": False, "locator": SHARE},
        {"action": "click", "locator": LIKE,
         "state_before": {"attributes": {"aria-pressed": "false"}, "name": "Like"},
         "state_after": {"attributes": {"aria-pressed": "true"}, "name": "Remove Like"}},
        {"action": "fill", "locator": CAPTION, "text": "hello there", "state_after": {"value": "hello there"}},
        {"action": "type", "locator": {"role": "textbox", "name": "Note"}, "text": "hello there",
         "state_after": {"text": "hello there"}},
        {"action": "navigate", "url": f"{BASE}/search?q=hello%20there"},
        {"action": "select", "locator": {"role": "combobox", "name": "Size"}, "option": "XL"},
        {"action": "select", "locator": {"role": "combobox", "name": "Colour"}, "text": "Red"},
        {"action": "scroll", "locator": {"role": "region", "name": "Feed"}},
        {"action": "upload", "locator": {"role": "button", "name": "Add photo"}},
        {"action": "press", "key": "Enter"},
        {"action": "press", "locator": CAPTION, "text": "Tab"},
        {"action": "press"},
        {"action": "click", "locator": SHARE, "state_before": {"attributes": {}}, "state_after": {"attributes": {}}},
    ],
    "dropped_kinds": [
        {"action": "navigate", "url": ""},
        {"action": "navigate", "url": f"{BASE}/x"},
        {"action": "drag", "locator": SHARE},
        {"action": "click"},
        {"action": "click", "locator": {"role": "button", "name": "Share", "nth": -5}},
        {"action": "click", "locator": {"ref": "e12"}},
        {"action": "type", "locator": {"role": "textbox", "name": "Password"}, "text": "hunter2"},
        {"action": "fill", "locator": CAPTION, "text": "x", "sensitive": True},
    ],
    "input_lost_before_send": [
        {"action": "navigate", "url": f"{BASE}/compose"},
        {"action": "fill", "text": "Hi"},
        {"action": "select", "text": "a"},
        {"action": "click", "locator": SHARE},
    ],
    "one_input_lost_before_send": [
        {"action": "navigate", "url": f"{BASE}/compose"},
        {"action": "fill", "text": "Hi"},
        {"action": "press", "key": "Enter"},
    ],
    "every_action_dropped": [
        {"action": "navigate", "url": f"{BASE}/compose"},
        {"action": "fill", "text": "Hi"},
        {"action": "click"},
    ],
    "reads_only": [{"action": "navigate", "url": f"{BASE}/a"}, {"action": "snapshot"}],
    "nothing": [{"action": "snapshot"}],
    "same_text_twice": [
        {"action": "navigate", "url": f"{BASE}/a"},
        {"action": "fill", "locator": CAPTION, "text": "same"},
        {"action": "fill", "locator": CAPTION, "text": "same"},
        {"action": "fill", "locator": CAPTION, "text": "other"},
        {"action": "upload", "locator": {"role": "button", "name": "File"}},
        {"action": "upload", "locator": {"role": "button", "name": "File"}},
    ],
}
KWARGS = [{}, {"effect": "commit", "title": "T", "flow_id": "w.l.mine"}]

AT = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)
EXPECTED = json.loads((Path(__file__).parent / "golden" / "recorder_build" / "expected.json").read_text())


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_build_checked_gives_what_it_gave(key):
    name, i = key.split("/")
    try:
        got = list(Recorder(store=None).build_checked(CASES[name], lane="w/l", started_at=AT, **KWARGS[int(i)]))
    except Exception as e:  # noqa: BLE001 - the pinned outcome may be an error
        got = ["raises", type(e).__name__, str(e)]
    assert json.loads(json.dumps(got)) == EXPECTED[key]
