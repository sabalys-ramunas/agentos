"""PlaywrightFlowDriver pieces that need no browser: name queries, the own filter on paths,
locator variants, the inbox, the control gate, the driver factory, take over as a pause in
the runner, and the recorder hook. The browser side runs in tests/node/test_flow_driver_node.py."""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from agentos.core.errors import AgentPaused, HumanInControl, LaneBusy, PolicyDenied
from agentos.core.models import ControlState, LaneControl, LaneInfo, LaneSpec, RunStatus
from agentos.core.runtime import Runtime
from agentos.node import flow_driver as fd
from agentos.runner.driver import ElementNotFound
from agentos.runner.fakes import FakeDriver, Harness, driver_factory, social_site
from agentos.runner.models import Locator, RunKind
from agentos.runner.recorder import Recorder
from agentos.runner.store import RunnerStore

# ---------------------------------------------------------------- names and paths


@pytest.mark.parametrize(("loc", "query"), [
    ({"role": "button"}, {}),
    ({"role": "button", "name": "  Like   it "}, {"name": "Like it", "exact": True}),
    ({"role": "button", "name": ""}, {"name": re.compile(r"^$")}),
    ({"role": "button", "name": "a.b (c)", "match": "contains"}, {"name": re.compile(r"a\.b \(c\)")}),
    ({"role": "article", "name": "^Post by", "match": "regex"}, {"name": re.compile("^Post by")}),
])
def test_name_query_matches_the_fake_semantics(loc, query):
    assert fd.name_query(Locator.model_validate(loc)) == query


def test_regex_escape_is_literal_in_python_and_safe_for_javascript():
    text = r"Price: $5.00 (50% off) [today] a/b c-d {x} ^y | z? *+ \ end"
    pattern = fd.regex_escape(text)
    assert re.fullmatch(pattern, text)
    # every escape is of punctuation, which JavaScript reads as the literal character
    assert not re.search(r"\\[A-Za-z0-9]", pattern)


def test_name_variants_put_count_tolerant_patterns_first():
    variants = fd.name_variants("Like 12")
    assert variants[0] == (r"^Like \d[\d.,]*[KkMm]?$", "regex")
    assert variants[-1] == ("Like 12", "exact")
    pattern = variants[0][0]
    for later in ("Like 13", "Like 1,204", "Like 2.5K"):
        assert re.search(pattern, later)
    assert not re.search(pattern, "Unlike 12")
    assert fd.name_variants("Share") == [("Share", "exact")]
    assert fd.name_variants(None) == [(None, "exact")]
    long = "word " * 30
    assert fd.name_variants(long)[0][1] == "contains"
    for value, match in fd.name_variants("Post (3 comments) by a.b"):
        Locator(role="article", name=value, match=match)  # each is a valid locator


def test_inside_is_strict_and_respects_segment_boundaries():
    assert fd.inside("1/2", "1/2/0")
    assert not fd.inside("1/2", "1/2")
    assert not fd.inside("1/2", "1/20/3")
    assert fd.inside("1/2", "1/2/-1/4")  # into a shadow root


def test_own_filter_mirrors_the_fake():
    anchor = "1/0"
    nested = ["1/0", "1/0/3", "1/0/3/1/0", "1/9"]  # the anchor itself, a comment, a reply, outside
    paths = ["1/0/1", "1/0/3", "1/0/3/2", "1/0/3/1/0/5", "1/0/4/0"]
    # the comment itself stays (a candidate is never inside itself); anything inside it goes
    assert fd.own_filter(paths, anchor, nested) == [True, True, False, False, True]


def test_role_and_name_of_a_ref_in_a_snapshot():
    raw = """- main [ref=e2]:
  - article "Post by Ada" [ref=e3]:
    - button "Like 12" [pressed] [ref=e12] [cursor=pointer]
    - textbox "Caption" [ref=e13]: hello
    - link "Home" [ref=e1]:
      - /url: /index.html"""
    assert fd.ref_role_and_name(raw, "e12") == ("button", "Like 12")
    assert fd.ref_role_and_name(raw, "e3") == ("article", "Post by Ada")
    assert fd.ref_role_and_name(raw, "e13") == ("textbox", "Caption")
    assert fd.ref_role_and_name(raw, "e1") == ("link", "Home")  # not e12 or e13
    assert fd.ref_role_and_name(raw, "e99") == (None, None)


# ---------------------------------------------------------------- the inbox


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    box = tmp_path / "lanes" / "biz-a" / "social" / "files" / "inbox"
    box.mkdir(parents=True)
    (box / "photo.png").write_bytes(b"png")
    (box / "f_8c1d-invoice.pdf").write_bytes(b"pdf")
    (box / "notes.txt").write_text("x")
    (box / "notes.md").write_text("y")
    (box / "sub").mkdir()
    (box / "sub" / "deep.png").write_bytes(b"deep")
    (tmp_path / "secret.txt").write_text("secret")
    os.symlink(tmp_path / "secret.txt", box / "escape.txt")
    return box


@pytest.mark.parametrize("ref", ["photo.png", "files:photo.png", "file:photo.png", "inbox/photo.png", "photo"])
def test_inbox_names_and_refs(inbox, ref):
    assert fd.resolve_inbox_file(inbox, ref) == (inbox / "photo.png").resolve()


def test_inbox_ids_subdirs_and_absolute_paths_inside(inbox):
    assert fd.resolve_inbox_file(inbox, "f_8c1d").name == "f_8c1d-invoice.pdf"
    assert fd.resolve_inbox_file(inbox, "sub/deep.png").name == "deep.png"
    assert fd.resolve_inbox_file(inbox, str(inbox / "photo.png")).name == "photo.png"


@pytest.mark.parametrize("ref", ["../../../../secret.txt", "/etc/passwd", "escape.txt", "sub/../../x.png"])
def test_inbox_refuses_anything_outside(inbox, ref):
    with pytest.raises(fd.FileOutsideInbox) as e:
        fd.resolve_inbox_file(inbox, ref)
    assert isinstance(e.value, PolicyDenied)


@pytest.mark.parametrize(("ref", "why"), [("missing.png", "no file"), ("notes", "2 inbox files"), ("", "needs a file"),
                                          ("sub", "no file")])
def test_inbox_missing_or_ambiguous(inbox, ref, why):
    with pytest.raises(fd.FileNotInInbox, match=why) as e:
        fd.resolve_inbox_file(inbox, ref)
    assert isinstance(e.value, ElementNotFound)  # so the executor knows nothing was done


def test_inbox_resolver_from_a_files_service_is_still_confined(inbox, tmp_path):
    class Files:
        def resolve(self, lane_id, ref):
            return {"f_1": inbox / "photo.png", "f_2": tmp_path / "secret.txt"}.get(ref)

    assert fd.resolve_inbox_file(inbox, "f_1", files=Files(), lane_id="biz-a/social").name == "photo.png"
    with pytest.raises(fd.FileOutsideInbox):
        fd.resolve_inbox_file(inbox, "f_2", files=Files(), lane_id="biz-a/social")


def test_inbox_dir_sources(tmp_path, monkeypatch):
    from agentos.core.config import NodeConfig

    class Files:
        def inbox_dir(self, lane_id):
            return tmp_path / "svc" / lane_id

    assert fd.inbox_dir(Runtime(files=Files()), "biz-a/social") == tmp_path / "svc" / "biz-a/social"
    cfg = NodeConfig(home=tmp_path / "home")
    assert fd.inbox_dir(Runtime(config=cfg), "biz-a/social") == tmp_path / "home/lanes/biz-a/social/files/inbox"
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path / "env"))
    assert fd.inbox_dir(Runtime(), "biz-a/social") == tmp_path / "env/lanes/biz-a/social/files/inbox"


# ---------------------------------------------------------------- the control gate and the factory


class Lanes:
    """A lane manager double: control per lane, using() that records the principal kind and
    applies the gate like the real one (before the lock and right before yielding)."""

    def __init__(self, *, with_principal: bool = True, with_control_method: bool = True):
        self.state = ControlState.AGENT
        self.entered: list[str | None] = []
        self.with_principal = with_principal
        if not with_control_method:
            self.control = None  # type: ignore[assignment]

    def control(self, lane_id: str) -> LaneControl:
        return LaneControl(state=self.state)

    def get(self, lane_id: str) -> LaneInfo:
        return LaneInfo(spec=LaneSpec(id=lane_id), control=LaneControl(state=self.state))

    def _gate(self, kind: str | None) -> None:
        if kind == "human":
            return
        if self.state == ControlState.HUMAN:
            raise HumanInControl("a human has control")
        if self.state == ControlState.PAUSED:
            raise AgentPaused("paused")

    def using(self, lane_id: str, **kw: Any):
        if not self.with_principal and kw:
            raise TypeError("using() got an unexpected keyword argument 'principal_kind'")
        kind = kw.get("principal_kind")

        @asynccontextmanager
        async def cm():
            self._gate(kind)
            self.entered.append(kind)
            yield f"page-of-{lane_id}"

        return cm()


@pytest.mark.parametrize("with_control_method", [True, False])
def test_gate_raises_for_human_and_paused(with_control_method):
    lanes = Lanes(with_control_method=with_control_method)
    gate = fd.control_gate(lanes, "biz-a/social")
    gate()
    lanes.state = ControlState.HUMAN
    with pytest.raises(fd.LaneTakenOver) as e:
        gate()
    assert isinstance(e.value, HumanInControl) and isinstance(e.value, LaneBusy)
    assert e.value.code == "human_in_control"
    lanes.state = ControlState.PAUSED
    with pytest.raises(fd.LaneAgentPaused) as e2:
        gate()
    assert isinstance(e2.value, AgentPaused) and e2.value.code == "agent_paused"


def test_gate_is_a_no_op_without_control_records():
    class Broken:
        def get(self, lane_id):
            raise RuntimeError("no store")

    fd.control_gate(Broken(), "biz-a/social")()


async def test_factory_holds_the_lane_as_a_flow_and_yields_a_driver(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    lanes = Lanes()
    factory = fd.driver_factory(Runtime(lanes=lanes, interaction="ia"))
    async with factory("biz-a/social") as driver:
        assert isinstance(driver, fd.PlaywrightFlowDriver)
        assert driver.page == "page-of-biz-a/social"
        assert driver.interaction == "ia"
        assert driver.files_dir == tmp_path / "lanes/biz-a/social/files/inbox"
        lanes.state = ControlState.HUMAN  # a take over mid-run stops the next step
        with pytest.raises(fd.LaneTakenOver):
            await driver.find(Locator(role="button"))
        with pytest.raises(fd.LaneTakenOver):
            await driver.navigate("https://social.example/")
        with pytest.raises(fd.LaneTakenOver):
            await driver.press("Enter")
    assert lanes.entered == ["flow"]


async def test_factory_works_with_a_lane_manager_from_before_take_over():
    lanes = Lanes(with_principal=False)
    async with fd.driver_factory(Runtime(lanes=lanes))("biz-a/social") as driver:
        assert driver.page == "page-of-biz-a/social"
    assert lanes.entered == [None]


@pytest.mark.parametrize(("state", "error"), [(ControlState.HUMAN, fd.LaneTakenOver),
                                              (ControlState.PAUSED, fd.LaneAgentPaused)])
async def test_factory_turns_the_lane_gate_into_busy_lane_errors(state, error):
    lanes = Lanes()
    lanes.state = state
    with pytest.raises(error) as e:
        async with fd.driver_factory(Runtime(lanes=lanes))("biz-a/social"):
            pass
    assert isinstance(e.value, LaneBusy)
    assert lanes.entered == []

    # also when the lane manager itself refuses (a take over that landed while waiting)
    class LateTakeOver(Lanes):
        def control(self, lane_id):
            return LaneControl()

    late = LateTakeOver()
    late.state = state
    with pytest.raises(error):
        async with fd.driver_factory(Runtime(lanes=late))("biz-a/social"):
            pass


# ---------------------------------------------------------------- take over in the runner (fake site)

POST = "https://social.example/p/1"
LIKE_POST = {"role": "button", "name": "Like", "within": {"role": "article", "name": "Post by Ieva"}}
LIKE_C1 = {"role": "button", "name": "Like", "within": {"role": "article", "name": "Comment by Beata"}}


def two_likes_flow() -> dict[str, Any]:
    pressed = {"condition": {"kind": "attribute", "name": "aria-pressed", "equals": "true"}}
    return {
        "id": "biz-a.social.two-likes", "lane": "biz-a/social", "effect": "commit", "domain": "social.example",
        "steps": [
            {"type": "navigate", "url": POST},
            {"type": "click", "locator": LIKE_POST, "writes": True, "readback": pressed},
            {"type": "click", "locator": LIKE_C1, "writes": True, "readback": pressed},
        ],
    }


def take_over_after_first_like(lanes: Lanes):
    """A hook on the fake driver: once the post is liked, a human takes the lane, and the fake
    driver checks the same gate the real one checks before find() and navigate()."""
    gate = fd.control_gate(lanes, "biz-a/social")

    def hook(op: str, target: Any, d: Any) -> None:
        if op in ("find", "navigate"):
            if "p1" in d.site.state["liked"] and lanes.state is ControlState.AGENT and not getattr(d, "_taken", False):
                d._taken = True
                lanes.state = ControlState.HUMAN
            gate()

    return hook


@pytest.mark.parametrize("pause_installed", [True, False])
async def test_take_over_mid_run(tmp_path, pause_installed):
    lanes = Lanes()
    site = social_site()
    h = Harness(tmp_path, site=site, services={"lanes": lanes}, backoff_base_s=1.0)
    if pause_installed:
        fd.install_take_over_pause(h.runner)
        fd.install_take_over_pause(h.runner)  # idempotent
        assert isinstance(h.runner.executors[RunKind.FLOW], fd.TakeOverPause)
        assert not isinstance(h.runner.executors[RunKind.FLOW].inner, fd.TakeOverPause)
    h.driver.hooks.append(take_over_after_first_like(lanes))
    h.save(two_likes_flow())
    run = await h.run("biz-a.social.two-likes")
    steps = {s["path"]: s for s in h.runner.steps(run.id)}
    assert site.state["liked"] == {"p1"}  # the second like never happened
    # The runner itself treats the stop as a pause (TakeOverPause is now a no-op on top): the
    # stopped step goes back to pending, like approval_pending, and runs on resume.
    assert steps["2"]["status"] == "pending" and "error" not in steps["2"]
    assert run.status == RunStatus.QUEUED
    assert run.error["code"] == "deferred" and "human has control" in run.error["message"]
    assert run.error["control"] == "human"
    assert run.attempts == 0 and run.deferrals == 1

    # Still held: the next attempt stops at the lane and defers again, doing nothing.
    h.clock.advance(3600)
    await h.runner.run_until_idle()
    run = h.runner.get(run.id)
    assert run.status == RunStatus.QUEUED and run.deferrals == 2
    assert site.state["liked"] == {"p1"}

    # Hand back: the run continues at the step that was stopped, and nothing is repeated.
    lanes.state = ControlState.AGENT
    h.clock.advance(3600)
    await h.runner.run_until_idle()
    run = h.runner.get(run.id)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert site.state["liked"] == {"p1", "c1"}
    assert len(run.proof["readbacks"]) == 2
    clicks = [entry for entry in h.driver.log if entry[0] == "click"]
    assert len(clicks) == 2


async def test_take_over_before_the_run_starts_defers_without_the_wrapper(tmp_path):
    lanes = Lanes()
    lanes.state = ControlState.HUMAN
    site = social_site()
    inner = driver_factory(FakeDriver(site))

    @asynccontextmanager
    async def factory(lane_id: str):
        fd.control_gate(lanes, lane_id)()
        async with inner(lane_id) as d:
            yield d

    h = Harness(tmp_path, site=site, services={"lanes": lanes}, factory=factory)
    h.save(two_likes_flow())
    run = await h.run("biz-a.social.two-likes")
    assert run.status == RunStatus.QUEUED and run.error["code"] == "deferred"
    assert "human has control" in run.error["message"]
    assert h.runner.steps(run.id) == []


async def test_take_over_pause_leaves_other_outcomes_alone(tmp_path):
    h = Harness(tmp_path)
    fd.install_take_over_pause(h.runner)
    h.save(two_likes_flow())
    run = await h.run("biz-a.social.two-likes")
    assert run.status == RunStatus.SUCCEEDED_VERIFIED

    h2 = Harness(tmp_path / "b")
    fd.install_take_over_pause(h2.runner)
    flow = two_likes_flow()
    flow["steps"][1]["locator"] = {"role": "button", "name": "Like"}  # ambiguous: a real failure
    h2.save(flow)
    run2 = await h2.run("biz-a.social.two-likes")
    assert run2.status == RunStatus.FAILED and run2.error["code"] == "ambiguous_locator"


# ---------------------------------------------------------------- the recorder hook


class Result:
    def __init__(self, url: str = "https://social.example/p/1", ok: bool = True):
        self.url, self.ok = url, ok


class Page:
    url = "https://social.example/p/1"

    def locator(self, selector: str):
        raise AssertionError("no browser in unit tests")


@pytest.fixture
def recorder(tmp_path) -> Recorder:
    return Recorder(RunnerStore(tmp_path / "runner.db"))


def runtime_with(recorder: Recorder) -> Runtime:
    class Flows:
        pass

    flows = Flows()
    flows.recorder = recorder
    return Runtime(flows=flows)


async def test_record_act_only_acts_when_nothing_records(recorder):
    calls = []

    async def perform():
        calls.append("act")
        return Result()

    out = await fd.record_act(runtime_with(recorder), "biz-a/social", Page(), "e5", "click", None, perform)
    assert isinstance(out, Result) and calls == ["act"]
    assert await fd.record_act(Runtime(), "biz-a/social", Page(), "e5", "click", None, perform)


async def test_record_act_pushes_an_event_the_recorder_turns_into_a_verified_step(recorder, monkeypatch):
    states = iter([{"attributes": {"aria-pressed": "false"}, "value": None, "text": "Like", "sensitive": False},
                   {"attributes": {"aria-pressed": "true"}, "value": None, "text": "Like", "sensitive": False}])

    async def locator_for(page, ref, **kw):
        assert ref == "e5"
        return Locator(role="button", name="Like", within=Locator(role="article", name="Post by Ieva"))

    class Handle:
        disposed = False

        async def dispose(self):
            Handle.disposed = True

    class FakeTarget:
        async def element_handle(self, timeout=None):
            return Handle()

    class RefPage(Page):
        def locator(self, selector):
            assert selector == "aria-ref=e5"
            return FakeTarget()

    async def handle_state(handle):
        return next(states)

    monkeypatch.setattr(fd, "structural_locator_for", locator_for)
    monkeypatch.setattr(fd, "_handle_state", handle_state)
    recorder.start("biz-a/social")

    async def perform():
        return Result()

    await fd.record_act(runtime_with(recorder), "biz-a/social", RefPage(), "e5", "click", None, perform)
    fd.record_navigate(runtime_with(recorder), "biz-a/social", POST)
    rec = recorder.stop("biz-a/social")
    click = rec["events"][0]
    assert click["action"] == "click" and click["ok"] is True
    assert click["locator"] == {"role": "button", "name": "Like", "within": {"role": "article", "name": "Post by Ieva"}}
    assert click["state_after"]["attributes"]["aria-pressed"] == "true"
    draft, _ = recorder.build([{"action": "navigate", "url": POST}, click], lane="biz-a/social")
    step = draft["steps"][-1]
    assert step["type"] == "click" and step["writes"] is True
    assert step["readback"]["condition"] == {"kind": "attribute", "name": "aria-pressed", "equals": "true"}
    assert Handle.disposed


async def test_record_act_without_a_locator_still_acts_and_records_the_gap(recorder, monkeypatch):
    async def no_locator(page, ref, **kw):
        raise fd.InvalidRequest("no role")

    monkeypatch.setattr(fd, "structural_locator_for", no_locator)
    recorder.start("biz-a/social")

    async def perform():
        return Result()

    await fd.record_act(runtime_with(recorder), "biz-a/social", Page(), "e9", "fill", "hello", perform)
    ev = recorder.stop("biz-a/social")["events"][0]
    assert ev["locator"] is None and ev["text"] == "hello"
    with pytest.raises(Exception, match="nothing was recorded"):
        recorder.build([ev], lane="biz-a/social")

