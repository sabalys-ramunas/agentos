"""The hand-back note and browser_act's form diff on a real Camoufox (run on the node).

A lane manager drives the session's real page: the test takes control, changes fields and navigates
the way a person would (real input, while control is HUMAN), hands back with a note, and checks the
note: URL, title and signal before and after, the tab the human opened, the fields that changed by
ref, label and role, and never a password or code value. Then the form diff of "Save address" lists
what the form will send against what the page loaded with.

Run on the node:  OPENAJAR_TEST_SLOT=47 scripts/node-test.sh handback tests/node/test_handback_node.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from test_lanes import Clock, FakeDisplays

from openajar.core.config import NodeConfig
from openajar.core.models import LaneSpec
from openajar.node.camoufox.engine import PageTracker
from openajar.node.fields import read_form_diff
from openajar.node.interaction import PlaywrightInteraction
from openajar.node.lanes import LaneManagerImpl
from openajar.node.store import LaneStore

pytestmark = [pytest.mark.node, pytest.mark.asyncio(loop_scope="session")]

LANE = "test/handback"


class PageSession:
    """A lane session over the test's own page and its context (tabs follow the PageTracker)."""
    pid = None
    alive = True

    def __init__(self, page):
        self.tracker = PageTracker(page.context)

    async def page(self):
        return await self.tracker.page()

    async def close(self):
        pass


class PageEngine:
    name = "camoufox"

    def __init__(self, page):
        self.session = PageSession(page)

    async def launch(self, spec, *, profile_dir, fingerprint_path, display, xauthority=None, proxy):
        return self.session

    async def sweep_orphans(self, root):
        return []


@pytest.fixture
async def lanes(page, tmp_path):
    config = NodeConfig(home=tmp_path, self_check=False, display_base=470, vnc_base=5470, viewer_base=6470)
    mgr = LaneManagerImpl(config, LaneStore(config.db_path), PageEngine(page), FakeDisplays(config),
                          dict, clock=Clock(), reaper_interval=60)
    mgr.watch(PlaywrightInteraction(allow_private_targets=True))
    await mgr.create(LaneSpec(id=LANE))
    yield mgr
    await mgr.shutdown()


async def test_hand_back_note_after_a_human_changed_fields_and_navigated(lanes, page, site_url):
    ia = PlaywrightInteraction(allow_private_targets=True)
    async with lanes.using(LANE) as p:
        assert p is page
        result = await ia.navigate(page, f"{site_url}/handback.html")
        await lanes.report_signal(LANE, result.signal)

    await lanes.take_control(LANE, by="owner")
    await asyncio.sleep(0.5)  # the capture of the page as the human got it
    # the human: real input in the page
    await page.fill("#fullname", "Ana Lopez")
    await page.fill("#street", "22 New Street")
    await page.fill("#pw", "hunter2-secret")
    await page.fill("#otp", "493817")
    await page.check("#gift")
    await page.select_option("#speed", "exp")
    async with page.context.expect_page() as popup:
        await page.click("text=Open the feed")
    feed = await popup.value
    await feed.wait_for_load_state()
    await page.bring_to_front()
    await page.click("text=Review")  # same document, new URL
    lanes._lanes[LANE].session.tracker._activate(page)  # the human went back to the form tab
    await lanes.hand_back(LANE, by="owner", note="New street; post the order")

    note = await lanes.handback.claim(LANE, "agent")
    print(json.dumps(note, indent=1))
    assert note["url"]["before"] == f"{site_url}/handback.html"
    assert note["url"]["after"] == f"{site_url}/handback.html#review" and note["url"]["changed"]
    assert note["title"]["before"] == note["title"]["after"] == "Shipping details"
    assert note["signal"]["before"] == "ok" and note["signal"]["after"] == "ok"
    assert note["tabs"]["open"] == 2 and note["tabs"]["opened"][0]["url"] == f"{site_url}/feed.html"
    assert note["human_note"] == "New street; post the order"
    by = {f["label"]: f for f in note["fields"]}
    assert by["Full name"]["value"] == "Ana Lopez" and by["Full name"]["was"] == "Ana"
    assert by["Full name"]["typed_by_human"] and by["Full name"]["role"] == "textbox"
    assert by["Full name"]["ref"] and by["Street"]["ref"] and by["Full name"]["ref"] != by["Street"]["ref"]
    assert by["Street"]["value"] == "22 New Street"
    assert by["Gift wrap"]["checked"] is True and by["Gift wrap"]["role"] == "checkbox"
    assert by["Delivery"]["selected"] == ["Express"]
    assert by["Password"]["value"] is None and by["Verification code"]["value"] is None
    assert "Note" not in by  # untouched
    text = json.dumps(note)
    assert "hunter2" not in text and "493817" not in text and "tok-1" not in text
    assert "stale" in note["refs"]


async def test_form_diff_shows_what_a_submit_sends(lanes, page, site_url):
    ia = PlaywrightInteraction(allow_private_targets=True)
    await ia.navigate(page, f"{site_url}/handback.html")
    await page.fill("#street", "22 New Street")
    await page.fill("#pw", "hunter2-secret")
    await page.check("#gift")
    html = await page.evaluate("document.documentElement.outerHTML")
    diff = await read_form_diff(page, page.locator("button[type=submit]"), "click")
    assert await page.evaluate("document.documentElement.outerHTML") == html  # read only
    assert diff["action"].endswith("/handback.html") and diff["method"] == "post"
    assert all(f["bind"] for f in diff["fields"]) and diff["bind"]  # each value bound, whole and keyed
    by = {f["label"]: {k: v for k, v in f.items() if k != "bind"} for f in diff["fields"]}
    assert by["Street"] == {"label": "Street", "role": "textbox", "name": "street", "changed": True,
                            "value": "22 New Street", "was": "1 Old Road"}
    assert not by["Full name"]["changed"]
    assert by["Password"]["value"] is None and by["Password"]["changed"]
    assert by["Gift wrap"]["checked"] and not by["Gift wrap"]["was_checked"]
    assert by["csrf"]["value"] is None
    assert "hunter2" not in json.dumps(diff) and "tok-1" not in json.dumps(diff)
    await page.fill("#pw", "hunter2-other")  # a password field changing changes what is bound
    again = await read_form_diff(page, page.locator("button[type=submit]"), "click")
    binds = lambda d: {f["label"]: f["bind"] for f in d["fields"]}
    assert binds(again)["Password"] != binds(diff)["Password"] and binds(again)["Street"] == binds(diff)["Street"]
    assert await read_form_diff(page, page.locator("text=Review"), "click") is None


async def test_form_diff_for_a_key_reads_the_form_of_a_field_or_its_button(lanes, page, site_url):
    """A key or typed newline the commit gate counts as a submit (NumpadEnter, Space on the button,
    a newline typed into a field) reads the same form a click on the submit button does: Ask me first
    holds them all (review of the fix wave, finding 2)."""
    ia = PlaywrightInteraction(allow_private_targets=True)
    await ia.navigate(page, f"{site_url}/handback.html")
    by_click = await read_form_diff(page, page.locator("button[type=submit]"), "click")
    for selector in ("#street", "button[type=submit]"):
        by_key = await read_form_diff(page, page.locator(selector), "key")
        assert by_key is not None and by_key["action"] == by_click["action"] and by_key["method"] == "post"
