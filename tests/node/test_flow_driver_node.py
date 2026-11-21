"""PlaywrightFlowDriver on real Camoufox (headful on Xvfb) against the local fixture site.

Real flows run through the real runner service: like the second post of a feed whose posts
hold nested comments with their own Like buttons; a readback that only a nested comment could
satisfy; take over in the middle of a run; uploads from the lane's inbox; a form filled with
human input. The locator parity cases from tests/unit run here on real pages, and recorded
locators round trip back to the element a snapshot ref pointed at.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from openajar.core.locator import Locator
from openajar.core.models import ControlState, LaneControl, LaneInfo, LaneSpec, LaneState, RunStatus, utcnow
from openajar.core.runtime import Runtime
from openajar.node import flow_driver as fd
from openajar.node import flow_recorder
from openajar.node.interaction import PlaywrightInteraction
from openajar.node.snapshot import find_refs
from openajar.runner.models import StepStatus
from openajar.runner.recorder import Recorder
from openajar.runner.service import build
from openajar.runner.store import RunnerStore
from openajar.runner.util import Clock

pytestmark = [pytest.mark.node, pytest.mark.asyncio(loop_scope="session")]

_spec = importlib.util.spec_from_file_location(
    "flow_driver_parity_cases", Path(__file__).resolve().parent.parent / "unit" / "test_flow_driver_parity.py")
parity = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = parity  # dataclasses look their module up
_spec.loader.exec_module(parity)

LANE = "fd/feed"
MAIN = {"role": "main"}


def post(n: int) -> dict[str, Any]:
    """The n-th post (0-based): articles named "Post by ..." in main. Comments are named
    "Comment by" / "Reply by", so they never count as posts."""
    return {"role": "article", "name": "^Post by ", "match": "regex", "nth": n, "within": MAIN}


LIKES_JS = """
() => Array.from(document.querySelectorAll('.like'), (b) => ({
  pk: b.dataset.pk, pressed: b.getAttribute('aria-pressed'), label: b.getAttribute('aria-label'),
  count: Number(b.querySelector('.count').textContent),
}))
"""


def changed(before: list[dict], after: list[dict]) -> dict[str, tuple[dict, dict]]:
    a = {x["pk"]: x for x in after}
    return {x["pk"]: (x, a[x["pk"]]) for x in before if a[x["pk"]] != x}


@pytest.fixture
def ia() -> PlaywrightInteraction:
    return PlaywrightInteraction(allow_private_targets=True)  # fixtures are served on 127.0.0.1


class NodeLanes:
    """A lane manager double around the test's real page, with the real take-over gate:
    non-human callers get HumanInControl / AgentPaused at using(), and the driver's gate
    reads control() before every step."""

    def __init__(self, page: Any):
        self.page = page
        self._control = LaneControl()
        self._lock = asyncio.Lock()
        self.kinds: list[str] = []

    def get(self, lane_id: str) -> LaneInfo:
        return LaneInfo(spec=LaneSpec(id=lane_id), state=LaneState.WARM, control=self._control)

    def control(self, lane_id: str) -> LaneControl:
        return self._control

    async def take_control(self, lane_id: str, *, by: str) -> LaneInfo:
        self._control = LaneControl(state=ControlState.HUMAN, by=by, since=utcnow())
        return self.get(lane_id)

    async def hand_back(self, lane_id: str, *, by: str) -> LaneInfo:
        self._control = LaneControl(state=ControlState.AGENT, by=by, since=utcnow())
        return self.get(lane_id)

    def _gate(self, kind: str) -> None:
        if kind == "human":
            return
        fd.control_gate(self, "x/y")()

    @asynccontextmanager
    async def using(self, lane_id: str, *, principal_kind: str = "agent"):
        self._gate(principal_kind)
        async with self._lock:
            self._gate(principal_kind)
            self.kinds.append(principal_kind)
            yield self.page


class Inbox:
    def __init__(self, root: Path):
        self.root = root

    def inbox_dir(self, lane_id: str) -> Path:
        return self.root


def make_runner(tmp_path: Path, lanes: NodeLanes, ia: PlaywrightInteraction, *, files: Any = None):
    rt = Runtime(lanes=lanes, interaction=ia, **({"files": files} if files else {}))
    runner = build(rt, home=tmp_path, driver_factory=fd.driver_factory(rt, dwell_ms=(150, 400)), clock=Clock(),
                   poll_s=0.2, backoff_base_s=5.0, max_concurrent=1)
    fd.install_take_over_pause(runner)
    return runner, rt


async def run_flow(runner: Any, rt: Runtime, flow: dict[str, Any], params: dict[str, Any] | None = None):
    rt.flows.save(flow)
    run = runner.submit(flow_id=flow["id"], params=params or {}, authorized=True)[0]
    await asyncio.wait_for(runner.run_until_idle(), 180)
    return runner.get(run.id)


def pressed_readback(locator: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"locator": locator, "condition": {"kind": "attribute", "name": "aria-pressed", "equals": "true"}, **kw}


def like_flow(flow_id: str, site_url: str, n: int, *, readback_own: bool = True) -> dict[str, Any]:
    target = post(n)
    pressed = {"role": "button", "name": "Remove Like", "within": target, **({} if readback_own else {"own": False})}
    return {
        "id": flow_id, "lane": LANE, "effect": "commit", "domain": "127.0.0.1",
        "steps": [
            {"type": "navigate", "url": f"{site_url}/flowfeed.html"},
            {"type": "click", "locator": {"role": "button", "name": "Like", "within": target}, "writes": True,
             "readback": pressed_readback(pressed, timeout_ms=2500)},
        ],
    }


# ---------------------------------------------------------------- parity with the fake


@pytest.mark.parametrize(("case_id", "page_name", "loc", "expected", "diverges"), parity.CASES, ids=parity.CASE_IDS)
async def test_parity_case_on_a_real_page(page, ia, case_id, page_name, loc, expected, diverges):
    await page.set_content(parity.to_html(parity.PAGES[page_name]))
    driver = fd.PlaywrightFlowDriver(page, lane_id="fd/parity", interaction=ia, dwell_ms=(0, 0))
    observed = await parity.observe(driver, loc)
    assert not parity.mismatches(observed, expected), observed


# ---------------------------------------------------------------- flows through the runner


async def test_like_the_second_post_is_verified_and_only_the_post_changed(page, site_url, ia, tmp_path):
    lanes = NodeLanes(page)
    runner, rt = make_runner(tmp_path, lanes, ia)
    await ia.navigate(page, f"{site_url}/flowfeed.html")
    before = await page.evaluate(LIKES_JS)

    run = await run_flow(runner, rt, like_flow("fd.feed.like-second", site_url, 1))

    assert run.status == RunStatus.SUCCEEDED_VERIFIED, (run.error, run.proof)
    rb = run.proof["readbacks"][0]
    assert rb["passed"] is True and rb["found"] == "true"
    diff = changed(before, await page.evaluate(LIKES_JS))
    assert list(diff) == ["like-post-2"], diff  # not the top comment above it, not a reply
    old, new = diff["like-post-2"]
    assert (old["pressed"], new["pressed"]) == ("false", "true")
    assert (old["count"], new["count"]) == (5, 6) and new["label"] == "Remove Like"
    assert lanes.kinds == ["flow"]
    assert await page.evaluate("document.body.dataset.leak || ''") == ""  # nothing the page can see


async def test_a_readback_only_a_nested_comment_satisfies_is_not_verified(page, site_url, ia, tmp_path):
    lanes = NodeLanes(page)
    runner, rt = make_runner(tmp_path, lanes, ia)

    # Post 3's own Like is broken; one of its comments is already liked ("Remove Like").
    run = await run_flow(runner, rt, like_flow("fd.feed.like-third", site_url, 2))
    assert run.status == RunStatus.PARTIAL, (run.status, run.error)
    assert run.error["code"] == "readback_failed" and run.error["acted"] is True
    assert run.proof["readbacks"][0]["passed"] is False
    likes = {x["pk"]: x for x in await page.evaluate(LIKES_JS)}
    assert likes["like-post-3"]["pressed"] == "false"
    assert likes["like-comment-3-1"]["pressed"] == "true"

    # The control experiment: the same readback reaching into comments (own: false) "passes",
    # which is exactly the false proof of the PoC. The default (own) is what prevents it.
    naive = await run_flow(runner, rt, like_flow("fd.feed.like-third-naive", site_url, 2, readback_own=False))
    assert naive.status == RunStatus.SUCCEEDED_VERIFIED


async def test_own_text_of_a_post_excludes_its_comments(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/flowfeed.html")
    driver = fd.PlaywrightFlowDriver(page, lane_id=LANE, interaction=ia, dwell_ms=(0, 0))
    st = await driver.read_state(Locator.model_validate(post(1)))
    assert st.found and st.count == 3
    assert "Errors using inadequate data" in st.text and "Charles Babbage" in st.text
    for comment_text in ("Top comment", "short distance ahead", "Like what you do"):
        assert comment_text not in st.text
    assert st.attributes["data-pk"] == "post-2"
    likes = await driver.read_state(Locator(role="button", name="Like", within=Locator.model_validate(post(1)),
                                            own=False))
    assert likes.count == 4 and likes.ambiguous


async def test_take_over_pauses_the_flow_and_hand_back_resumes_it(page, site_url, ia, tmp_path):
    lanes = NodeLanes(page)
    runner, rt = make_runner(tmp_path, lanes, ia)
    flow = {
        "id": "fd.feed.two-likes", "lane": LANE, "effect": "commit", "domain": "127.0.0.1",
        "steps": [
            {"type": "navigate", "url": f"{site_url}/flowfeed.html"},
            {"type": "click", "locator": {"role": "button", "name": "Like", "within": post(0)}, "writes": True,
             "readback": pressed_readback({"role": "button", "name": "Remove Like", "within": post(0)})},
            {"type": "pause", "min_ms": 2500, "max_ms": 2500},
            {"type": "click", "locator": {"role": "button", "name": "Like", "within": post(1)}, "writes": True,
             "readback": pressed_readback({"role": "button", "name": "Remove Like", "within": post(1)})},
        ],
    }
    rt.flows.save(flow)
    run = runner.submit(flow_id=flow["id"], authorized=True)[0]
    task = asyncio.create_task(runner.run_until_idle())
    deadline = time.monotonic() + 90
    while not any(s.path == "1" and s.status == StepStatus.DONE for s in runner.store.steps(run.id)):
        assert time.monotonic() < deadline and not task.done(), runner.get(run.id).error
        await asyncio.sleep(0.05)
    await lanes.take_control(LANE, by="owner")  # lands during the pause
    await asyncio.wait_for(task, 60)

    run = runner.get(run.id)
    assert run.status == RunStatus.QUEUED, (run.status, run.error)
    assert run.error["code"] == "deferred" and "human has control" in run.error["message"]
    assert run.error["control"] == "human" and run.held == 1
    steps = {s.path: s for s in runner.store.steps(run.id)}
    # stopped before its input: back to pending (like approval_pending), so it runs on resume
    assert steps["3"].status == StepStatus.PENDING and steps["3"].error is None
    likes = {x["pk"]: x["pressed"] for x in await page.evaluate(LIKES_JS)}
    assert likes["like-post-1"] == "true" and likes["like-post-2"] == "false"

    await lanes.hand_back(LANE, by="owner")
    run.not_before = None  # do not wait out the backoff in a test
    assert runner.store.save_run(run, expect={RunStatus.QUEUED})
    await asyncio.wait_for(runner.run_until_idle(), 90)
    run = runner.get(run.id)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, (run.status, run.error)
    assert [rb["passed"] for rb in run.proof["readbacks"]] == [True, True]
    after = {x["pk"]: x for x in await page.evaluate(LIKES_JS)}
    assert after["like-post-1"]["pressed"] == "true" and after["like-post-1"]["count"] == 13  # liked once
    assert after["like-post-2"]["pressed"] == "true" and after["like-post-2"]["count"] == 6
    assert [pk for pk, x in after.items() if x["pressed"] == "true"] == ["like-post-1", "like-post-2",
                                                                         "like-comment-3-1"]


async def test_a_form_filled_with_human_input(page, site_url, ia, tmp_path):
    lanes = NodeLanes(page)
    runner, rt = make_runner(tmp_path, lanes, ia)

    def value(v: str) -> dict[str, Any]:
        return {"condition": {"kind": "value", "equals": v}}

    flow = {
        "id": "fd.form.signup", "lane": LANE, "effect": "commit", "domain": "127.0.0.1",
        "params": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        "steps": [
            {"type": "navigate", "url": f"{site_url}/form.html"},
            {"type": "fill", "locator": {"role": "textbox", "name": "Name"}, "param": "name",
             "readback": value("${name}")},
            {"type": "fill", "locator": {"role": "textbox", "name": "Bio"}, "text": "Poet of science",
             "readback": value("Poet of science")},
            {"type": "type", "locator": {"role": "textbox", "name": "Bio"}, "text": " and math",
             "readback": value("Poet of science and math")},
            {"type": "select", "locator": {"role": "combobox", "name": "Favourite color"}, "option": "Green",
             "readback": value("Green")},
            {"type": "click", "locator": {"role": "checkbox", "name": "Subscribe to news"}, "writes": True,
             "readback": {"condition": {"kind": "attribute", "name": "checked", "equals": "true"}}},
            {"type": "press", "key": "Enter", "locator": {"role": "textbox", "name": "Name"}, "writes": True,
             "readback": {"locator": {"role": "status"},
                          "condition": {"kind": "text", "contains": '"name":"${name}"'}}},
        ],
    }
    run = await run_flow(runner, rt, flow, {"name": "Ada Lovelace"})
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, (run.status, run.error)
    result = await page.locator("#result").text_content()
    assert '"bio":"Poet of science and math"' in result and '"color":"g"' in result and '"subscribe":true' in result
    keys = await page.evaluate("[document.body.dataset.keydowns, document.body.dataset.trustedKeydowns]")
    assert int(keys[0]) > 20 and keys[0] == keys[1]  # typed key by key, every one a real (trusted) event


# ---------------------------------------------------------------- uploads from the inbox


async def test_upload_from_the_inbox_with_readback(page, site_url, ia, tmp_path):
    box = tmp_path / "inbox"
    box.mkdir()
    (box / "photo.png").write_bytes(b"\x89PNG....")
    (box / "f_7a1-doc.txt").write_text("hello")
    (box / "bare.bin").write_bytes(b"0123456789")
    (tmp_path / "secret.txt").write_text("secret")
    lanes = NodeLanes(page)
    runner, rt = make_runner(tmp_path / "home", lanes, ia, files=Inbox(box))
    dialog = {"role": "dialog", "name": "Create new post"}

    def status(name: str, text: str) -> dict[str, Any]:
        return {"locator": {"role": "status", "name": name, "within": dialog},
                "condition": {"kind": "text", "contains": text}}

    flow = {
        "id": "fd.upload.post", "lane": LANE, "effect": "draft", "domain": "127.0.0.1",
        "params": {"type": "object", "required": ["photo", "doc", "bare"],
                   "properties": {"photo": {"type": "string"}, "doc": {"type": "string"},
                                  "bare": {"type": "string"}}},
        "steps": [
            {"type": "navigate", "url": f"{site_url}/flow-upload.html"},
            # a visible button that opens a hidden input's file chooser
            {"type": "upload", "locator": {"role": "button", "name": "Select from computer", "within": dialog},
             "param": "photo", "readback": status("Selected photo", "Selected photo.png (8 bytes)")},
            # a visible file input, found through the CSS fallback
            {"type": "upload", "locator": {"role": "button", "name": "Attachment file", "css": "#attachment"},
             "param": "doc", "readback": {"condition": {"kind": "value", "equals": "f_7a1-doc.txt"}}},
            # a hidden input with no visible control
            {"type": "upload", "locator": {"css": "#bare"}, "param": "bare",
             "readback": status("Selected bare file", "Selected bare.bin (10 bytes)")},
        ],
    }
    run = await run_flow(runner, rt, flow, {"photo": "photo.png", "doc": "f_7a1", "bare": "files:bare.bin"})
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, (run.status, run.error)
    assert await page.locator("#attachment-status").text_content() == "Selected f_7a1-doc.txt (5 bytes)"

    for i, ref in enumerate(["../secret.txt", str(tmp_path / "secret.txt")]):
        refused = dict(flow, id=f"fd.upload.refused{i}", steps=flow["steps"][:2])
        out = await run_flow(runner, rt, refused, {"photo": ref, "doc": "x", "bare": "x"})
        assert out.status == RunStatus.FAILED and out.error["code"] == "denied", out.error
        assert "outside the lane's inbox" in out.error["message"]
        assert await page.locator("#photo-status").text_content() == "No photo selected"


# ---------------------------------------------------------------- recorded locators


async def round_trip(page: Any, ia: PlaywrightInteraction, ref: str, *, snapshot: str | None = None) -> Locator:
    loc = await flow_recorder.structural_locator_for(page, ref, snapshot=snapshot)
    driver = fd.PlaywrightFlowDriver(page, lane_id=LANE, interaction=ia, dwell_ms=(0, 0))
    el = await driver.find(loc)
    want = await page.locator(f"aria-ref={ref}").evaluate(fd.PATH_JS)
    assert el.path == want, (loc.model_dump(exclude_none=True), el.path, want)
    assert await page.locator(f"aria-ref={ref}").count() == 1  # the ref still works afterwards
    return loc


def one_ref(text: str, role: str, name: str, within: str | None = None) -> str:
    refs = find_refs(text, role=role, name=name, within=within)
    assert len(refs) == 1, (role, name, within, refs)
    return refs[0]


async def test_structural_locators_round_trip_on_the_feed(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/flowfeed.html")
    snap = (await ia.snapshot(page)).text

    loc = await round_trip(page, ia, one_ref(snap, "button", "Like", "Post by Charles Babbage"))
    assert loc.model_dump(exclude_none=True, exclude_defaults=True) == {
        "role": "button", "name": "Like", "within": {"role": "article", "name": "Post by Charles Babbage"}}

    # "Comment by Grace Hopper" exists under post 2 and post 3: the anchor nests in its post.
    loc = await round_trip(page, ia, one_ref(snap, "button", "Remove Like", "Comment by Grace Hopper"),
                           snapshot=snap)
    assert loc.within.name == "Comment by Grace Hopper"
    assert loc.within.within is not None and loc.within.within.name == "Post by Hedy Lamarr"

    loc = await round_trip(page, ia, one_ref(snap, "button", "Like", "Reply by Katherine Johnson"))
    assert loc.within.name == "Reply by Katherine Johnson"

    loc = await round_trip(page, ia, one_ref(snap, "link", "Form"))
    assert (loc.role, loc.name) == ("link", "Form") and loc.within.role == "navigation"


async def test_structural_locators_round_trip_on_a_form(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/form.html")
    snap = (await ia.snapshot(page)).text
    for role, name in [("textbox", "Name"), ("textbox", "Bio"), ("combobox", "Favourite color"),
                       ("checkbox", "Subscribe to news"), ("button", "Submit")]:
        loc = await round_trip(page, ia, one_ref(snap, role, name))
        assert (loc.role, loc.name) == (role, name)


async def test_a_recorded_locator_survives_a_count_change(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/feed.html")
    snap = (await ia.snapshot(page)).text
    ref = one_ref(snap, "button", "Like 12", "Post by Ada Lovelace")
    loc = await round_trip(page, ia, ref)
    assert loc.match == "regex" and loc.within.name == "Post by Ada Lovelace"
    result = await ia.act(page, ref, "click")
    assert result.changed
    driver = fd.PlaywrightFlowDriver(page, lane_id=LANE, interaction=ia, dwell_ms=(0, 0))
    st = await driver.read_state(loc)
    assert st.found and st.attributes["aria-pressed"] == "true" and st.text == "Like 13"


async def test_the_recorder_hook_records_a_flow_that_replays_verified(page, site_url, ia, tmp_path):
    recorder = Recorder(RunnerStore(tmp_path / "rec.db"))

    class Flows:
        pass

    flows = Flows()
    flows.recorder = recorder
    rec_rt = Runtime(flows=flows)
    recorder.start(LANE)
    nav = await ia.navigate(page, f"{site_url}/feed.html")
    flow_recorder.record_navigate(rec_rt, LANE, nav.url)
    snap = (await ia.snapshot(page)).text
    ref = one_ref(snap, "button", "Like 12", "Post by Ada Lovelace")  # a comment Like shows 12 too
    result = await flow_recorder.record_act(rec_rt, LANE, page, ref, "click", None, lambda: ia.act(page, ref, "click"))
    assert result.ok and result.changed
    events = recorder.stop(LANE)["events"]
    draft, _ = recorder.build(events, lane=LANE, flow_id="fd.feed.recorded", effect="commit")
    click = draft["steps"][-1]
    assert click["writes"] is True and click["readback"]["condition"]["name"] == "aria-pressed"

    lanes = NodeLanes(page)
    runner, rt = make_runner(tmp_path / "home", lanes, ia)
    draft["domain"] = "127.0.0.1"
    run = await run_flow(runner, rt, draft)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, (run.status, run.error, draft)
    pressed = await page.evaluate("Array.from(document.querySelectorAll('button.like'), "
                                  "(b) => b.closest('article').id + ':' + b.getAttribute('aria-pressed'))")
    assert [p for p in pressed if p.endswith(":true")] == ["post-1:true"]


@pytest.mark.parametrize(("case_id", "page_name", "items", "fields", "max_items", "rows", "total"),
                         parity.EXTRACT_CASES, ids=parity.EXTRACT_IDS)
async def test_extract_case_on_a_real_page(page, ia, case_id, page_name, items, fields, max_items, rows, total):
    await page.set_content(parity.to_html(parity.PAGES[page_name]))
    driver = fd.PlaywrightFlowDriver(page, lane_id="fd/parity", interaction=ia, dwell_ms=(0, 0))
    assert await parity.observe_extract(driver, items, fields, max_items) == {"rows": rows, "total": total}


async def test_parity_pages_build_their_shadow_roots(page):
    await page.set_content(parity.to_html(parity.CUSTOM))
    hosts = await page.evaluate("() => Array.from(document.querySelectorAll('x-post, x-comment'), "
                                "(e) => [e.getAttribute('thingid'), !!e.shadowRoot])")
    assert hosts == [["t3_p1", True], ["t3_p2", False]]  # the comment and its reply sit inside p1's shadow root
