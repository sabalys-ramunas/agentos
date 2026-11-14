"""The program executor, brief B1 (agentos.engage.executor): approved programs run as sessions
through the ports, against fakes."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentos.engage.caps import Kind
from agentos.engage.errors import OwnerOnly
from agentos.engage.executor import Executor
from agentos.engage.fakes import FakeContent, FakeLanes, FakeOperations, FakeTargets
from agentos.engage.models import Comments, Mode
from agentos.engage.operations import DEFAULT_SPECS, OpSpec, Registry
from agentos.engage.persona import draw_persona, week_of
from agentos.engage.ports import Content, ContentOrigin, LaneUnavailable, OpResult, OpStatus, claim_parent
from agentos.engage.service import EngageService
from agentos.node.egress import EFFECT_RULES, EffectRule

OWNER = {"kind": "human", "id": "owner", "owner": "owner"}
MONDAY = date(2026, 11, 2)
IG = "biz-a/instagram"
RD = "biz-a/reddit"
SEED = "executor-seed-1"

IG_YAML = """
program: biz-a-ig
surface: biz-a/instagram
timezone: Europe/Vilnius
phases:
  - kind: steady
    daily: {like: 15, comment: 3, follow: 2, story_view: 10}
comments: {mode: approve, topics: [coffee], max_chars: 140}
"""

RD_YAML = """
program: biz-a-rd
surface: biz-a/reddit
timezone: Europe/Vilnius
phases:
  - kind: steady
    daily: {comment: 1, post: 1, join: 1}
"""

IG_RULES = [EffectRule(f"instagram.{k}", "instagram", "www.instagram.com", "/x", verified="test")
            for k in ("like", "follow", "comment")] + [
    EffectRule("instagram.read.story", "instagram", "www.instagram.com", "/s", effect="read", verified="test")]
IG_SPECS = [OpSpec("instagram", Kind.LIKE, "connector.instagram.like", "instagram.like", needs_target=True),
            OpSpec("instagram", Kind.FOLLOW, "connector.instagram.follow", "instagram.follow", needs_target=True),
            OpSpec("instagram", Kind.COMMENT, "connector.instagram.comment", "instagram.comment", needs_text=True,
                   needs_target=True),
            OpSpec("instagram", Kind.STORY_VIEW, "connector.instagram.view_story", "instagram.read.story",
                   needs_target=True)]


def ig_registry() -> Registry:
    return Registry(IG_SPECS, installed=lambda flow_id: True, verified=lambda flow_id: "test", rules=IG_RULES)


class Rig:
    def __init__(self, home, yaml_text: str = IG_YAML, *, surface: str = IG, seed: str = SEED,
                 registry: Registry | None = None, ops: FakeOperations | None = None,
                 content: FakeContent | None = None, lanes: FakeLanes | None = None):
        self.surface = surface
        self.t = datetime(2026, 11, 1, 23, 0, tzinfo=UTC).timestamp()      # Monday 01:00 in Vilnius
        self.svc = EngageService(home=home, clock=lambda: self.t)
        self.svc.store.put_persona(draw_persona(surface, seed))
        out = self.svc.draft(surface, yaml_text, by=OWNER)
        self.svc.approve(out["program"], by=OWNER)
        self.ops = ops or FakeOperations()
        self.content = content or FakeContent()
        self.targets = FakeTargets()
        self.lanes = lanes or FakeLanes()

        async def sleep(s: float) -> None:
            self.t += s

        self.ex = Executor(self.svc.store, registry=registry or ig_registry(), operations=self.ops,
                           targets=self.targets, content=self.content, lanes=self.lanes, clock=lambda: self.t,
                           sleep=sleep, plan_for=self.svc.day_plan, tz_for=self.svc.timezone)

    def at(self, t: datetime) -> None:
        self.t = t.timestamp()

    def run(self, day: date, idx: int) -> dict:
        w = self.ex.window(self.surface, day, idx)
        self.at(w.start)
        return asyncio.run(self.ex.run_window(self.surface, day, idx))

    def run_days(self, start: date, days: int) -> None:
        for d in range(days):
            day = start + timedelta(days=d)
            done: set[int] = set()
            while True:
                todo = [w for w in self.ex.windows(self.surface, day) if w.idx not in done]
                if not todo:
                    break
                w = min(todo, key=lambda x: x.start)
                done.add(w.idx)
                self.run(day, w.idx)

    def performed(self) -> dict[tuple[str, int], Counter]:
        out: dict[tuple[str, int], Counter] = {}
        for c in self.ops.calls:
            _, _, day, idx, _, _ = c.idempotency_key.rsplit(":", 5)
            out.setdefault((day, int(idx)), Counter())[c.kind] += 1
        return out


def planned(plans) -> dict[tuple[str, int], Counter]:
    return {(p.day.isoformat(), i): Counter({str(k): n for k, n in s.actions.items()})
            for p in plans for i, s in enumerate(p.sessions) if s.actions}


# ---------------------------------------------------------------- the registry


def test_the_registry_links_reddit_post_text_to_its_verified_rule_and_nothing_else_is_valid_yet():
    reg = Registry(installed=lambda flow_id: flow_id == "connector.reddit.post_text",
                   verified=lambda flow_id: "2026-11-04 reddit.com canary: test")
    post = reg.get("reddit", Kind.POST)
    assert post is not None and post.flow_id == "connector.reddit.post_text" and post.rule_id == "reddit.post"
    assert reg.valid(post)
    rules = {r.id: r for r in EFFECT_RULES}
    for spec in DEFAULT_SPECS:
        assert spec.rule_id in rules and rules[spec.rule_id].verified, spec   # every listed rule is verified
        if spec is not post:
            assert reg.problem(spec) == "no_flow"
    assert reg.problem(reg.get("reddit", Kind.LIKE)) == "no_spec"
    assert Registry(installed=lambda f: False).problem(post) == "no_flow"
    bogus = OpSpec("reddit", "profile_edit", "connector.reddit.edit", "reddit.edit.nothing")
    reg.register(bogus)                              # extensible: B2 adds profile edits
    assert reg.problem(bogus) == "no_rule"
    unverified = Registry([OpSpec("reddit", Kind.POST, "connector.reddit.post_text", "reddit.post.oldreddit")],
                          installed=lambda f: True)
    assert unverified.problem(unverified.get("reddit", Kind.POST)) == "rule_unverified"
    with pytest.raises(ValueError):
        reg.register(post)
    params = post.build(Content("c1", ContentOrigin.ITEM, "Hello\nbody", targets=("r/test",)), "r/test")
    assert params == {"subreddit": "test", "title": "Hello", "body": "Hello\nbody"}


# ---------------------------------------------------------------- the plan, exactly


def test_a_fixed_seed_run_performs_exactly_the_planned_operations(tmp_path):
    rig = Rig(tmp_path)
    plans = rig.svc.plan(IG, days=7, start=MONDAY)           # the dry run, before anything is saved
    rig.run_days(MONDAY, 7)
    assert rig.performed() == planned(plans)
    assert sum(sum(c.values()) for c in planned(plans).values()) > 40
    ops = rig.svc.store.ops(IG)
    assert ops and {o["status"] for o in ops} == {"verified"}
    for p in plans:
        done = {k.value: c["done"] for k, c in rig.svc.store.counters(IG, p.day).items() if c["done"]}
        assert done == {k.value: n for k, n in p.totals().items()}
        assert rig.svc.store.plans(IG, p.day, p.day)[0] == p        # the executor ran the plan it saved
    assert all(c.parent.startswith(claim_parent(f"engage:{IG}:")) for c in rig.ops.calls)
    by_window = {}
    for c in rig.ops.calls:                                       # one claim nonce per window, never reused
        by_window.setdefault(c.idempotency_key.rsplit(":", 2)[0], set()).add(c.parent)
    assert all(len(v) == 1 for v in by_window.values())
    assert len(set().union(*by_window.values())) == len(by_window)
    assert all(c.ask for c in rig.ops.calls if c.kind == "comment")   # approve mode
    assert not any(c.ask for c in rig.ops.calls if c.kind != "comment")
    quiet = [p for p in plans if p.quiet]
    assert all(not rig.svc.store.sessions(IG, p.day) for p in quiet)


def test_a_replay_is_deterministic_and_never_acts_twice(tmp_path):
    def trace(home):
        rig = Rig(home)
        rig.run_days(MONDAY, 3)
        # the key's last part is a tag made with the node's own secret: each home has its own
        calls = [(c.idempotency_key.rsplit(":", 1)[0], c.kind, json.dumps(c.params, sort_keys=True))
                 for c in rig.ops.calls]
        rows = [{k: v for k, v in o.items() if k != "id"} for o in rig.svc.store.ops(IG)]
        return rig, calls, rows

    rig, a_calls, a_rows = trace(tmp_path / "a")
    _, b_calls, b_rows = trace(tmp_path / "b")
    assert a_calls == b_calls and a_rows == b_rows and a_calls
    before = len(rig.ops.calls)
    for d in range(3):
        day = MONDAY + timedelta(days=d)
        for w in rig.ex.windows(IG, day):
            again = asyncio.run(rig.ex.run_window(IG, day, w.idx))
            assert again["status"] == "done"
    assert len(rig.ops.calls) == before


def test_dry_run_parity_with_the_plan_command(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentos.engage.cli import app

    rig = Rig(tmp_path)
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    out = CliRunner().invoke(app, ["plan", IG, "--dry-run", "--days", "7", "--start", MONDAY.isoformat(), "--json"])
    assert out.exit_code == 0, out.output
    printed = json.loads(out.output)
    rig.run_days(MONDAY, 7)
    want = {(p["day"], i): Counter(s["actions"]) for p in printed for i, s in enumerate(p["sessions"]) if s["actions"]}
    assert rig.performed() == want and want


# ---------------------------------------------------------------- refusals


def test_an_operation_without_a_valid_rule_aborts_the_window_and_holds_the_surface(tmp_path):
    reg = Registry(installed=lambda flow_id: flow_id == "connector.reddit.post_text")   # the real table
    rig = Rig(tmp_path, RD_YAML, surface=RD, registry=reg)
    day, w = next((d, w) for d in (MONDAY + timedelta(days=i) for i in range(7))
                  for w in rig.ex.windows(RD, d) if "comment" in w.kinds or "join" in w.kinds)
    preview = rig.ex.preview(RD, day, w.idx)
    assert not preview["would_run"] and {o["problem"] for o in preview["ops"]} >= {"no_flow"}
    rep = rig.run(day, w.idx)
    assert rep["status"] == "aborted" and rep["end_reason"] == "no_valid_rule"
    assert {o["status"] for o in rep["ops"]} == {"skipped"} and {o["reason"] for o in rep["ops"]} == {"no_valid_rule"}
    assert rig.ops.calls == []
    [hold] = rig.svc.store.holds(RD)
    assert hold["reason"] == "no_valid_rule" and hold["kind"] is None and hold["until"] is None
    assert "no_flow" in hold["detail"]
    kinds = [h["kind"] for h in rig.svc.store.history(RD)]
    assert "hold" in kinds and "session" in kinds
    assert rig.lanes.held == {} and len(rig.lanes.releases) == len(rig.lanes.claims) == 1
    assert rig.svc.status(RD)["surfaces"][0]["holds"][0]["reason"] == "no_valid_rule"
    later = [(d, x) for d in (MONDAY + timedelta(days=i) for i in range(7)) for x in rig.ex.windows(RD, d)
             if (d, x.idx) != (day, w.idx) and x.start > w.start]
    if later:                                                     # the hold refuses what comes next
        d, x = later[0]
        assert rig.run(d, x.idx)["status"] == "refused"
    with pytest.raises(OwnerOnly):
        rig.ex.clear_hold(RD, by={"kind": "agent", "id": "a1"})
    assert rig.ex.clear_hold(RD, by=OWNER) == 1 and rig.svc.store.holds(RD) == []


def test_a_program_that_is_not_approved_does_not_run(tmp_path):
    from agentos.core.errors import InvalidRequest

    rig = Rig(tmp_path)
    rig.svc.draft("biz-b/instagram", IG_YAML.replace("biz-a", "biz-b"), by=OWNER)
    with pytest.raises(InvalidRequest):
        asyncio.run(rig.ex.run_window("biz-b/instagram", MONDAY, 0))


# ---------------------------------------------------------------- signals


def _busy_window(rig: Rig, min_ops: int = 4):
    for d in range(7):
        day = MONDAY + timedelta(days=d)
        for w in rig.ex.windows(rig.surface, day):
            if len(w.kinds) >= min_ops and day.weekday() < 4:
                return day, w
    raise AssertionError("no busy window in the week")


@pytest.mark.parametrize("signal", ["captcha", "checkpoint", "logged_out", "blocked", "rate_limited", "wrong_account"])
def test_a_signal_holds_ends_early_and_reschedules_within_the_week_under_the_caps(tmp_path, signal):
    rig = Rig(tmp_path, ops=FakeOperations({1: OpResult(OpStatus.FAILED, signal=signal, error="signal", acted=False)}))
    day, w = _busy_window(rig)
    rep = rig.run(day, w.idx)
    assert rep["status"] == "ended_early" and rep["end_reason"] == f"signal: {signal}"
    assert len(rig.ops.calls) == 2 and rig.lanes.held == {}
    [hold] = rig.svc.store.holds(IG)
    assert hold["signal"] == signal
    hit = datetime.fromisoformat(rep["ops"][1]["at"])
    if signal == "rate_limited":
        assert hold["reason"] == "backoff:rate_limited"
        assert datetime.fromisoformat(hold["until"]) == hit + timedelta(hours=6)
    else:
        assert hold["reason"] == f"needs_human:{signal}" and hold["until"] is None
    earliest = datetime.fromisoformat(hold["until"]) if hold["until"] else hit + timedelta(minutes=45)

    rows = rig.svc.store.ops(session_id=rig.svc.store.session(IG, day, w.idx)["id"])
    assert [o["status"] for o in rows[:2]] == ["verified", "failed"]     # the one that met the signal keeps it
    rest = rows[1:]                                         # everything after the signal, and the failed one
    assert all(o["status"] == "skipped" for o in rest[1:])
    moved = {o["moved_from"]: o for o in rig.svc.store.ops(IG) if o["moved_from"]}
    for o in rest[1:]:
        assert (o["reason"] == "rescheduled") == (o["id"] in moved)
        assert o["reason"] in ("rescheduled", "week_over")
    assert rest[0]["id"] in moved or "week_over" in rest[0]["reason"]
    assert moved                                               # a Monday to Thursday window finds room
    sunday = week_of(day) + timedelta(days=6)
    persona = rig.svc.store.persona(IG)
    tz = ZoneInfo("Europe/Vilnius")
    for s in rig.svc.store.sessions(IG, origin="reschedule"):
        d = date.fromisoformat(s["day"])
        start = datetime.fromisoformat(s["planned_start"]).astimezone(tz)
        assert day <= d <= sunday and start >= earliest
        lo, hi = persona.for_day(d).window(d)
        assert lo <= start.hour + start.minute / 60 + start.second / 3600 <= hi       # waking hours
        plan = rig.svc.store.plans(IG, d, d)[0]
        assert not plan.quiet
        for w2 in rig.ex.windows(IG, d):
            if w2.idx != s["idx"]:
                assert abs(w2.start - start) >= timedelta(minutes=45)
        for kind, cap in plan.caps.items():                      # the day's caps hold with the moved work
            total = sum(rig.ex._committed(x, kind.value) for x in rig.ex.windows(IG, d))
            assert total <= cap, (d, kind, total, cap)
    hist = [h for h in rig.svc.store.history(IG) if h["kind"] == "rescheduled"]
    assert hist and sum(len(m["ops"]) for m in hist[0]["payload"]["moved"]) == len(moved)


def test_work_that_cannot_fit_by_sunday_is_dropped_with_a_reason(tmp_path):
    rig = Rig(tmp_path, ops=FakeOperations({0: OpResult(OpStatus.FAILED, signal="rate_limited", acted=False)}))
    sunday = MONDAY + timedelta(days=6)
    windows = [w for w in rig.ex.windows(IG, sunday) if w.kinds]
    assert windows, "the fixed seed has a Sunday with sessions"
    w = max(windows, key=lambda x: x.start)
    rep = rig.run(sunday, w.idx)                       # rests 6 hours: past Sunday's waking hours
    assert rep["status"] == "ended_early"
    assert {o["reason"] for o in rep["ops"][1:]} == {"week_over"} and rep["ops"][0]["status"] == "failed"
    assert rep["ops"][0]["reason"] == "signal, then week_over"
    assert rig.svc.store.sessions(IG, origin="reschedule") == []
    assert rig.svc.store.notices(IG)[0]["kind"] == "dropped"
    [hist] = [h for h in rig.svc.store.history(IG) if h["kind"] == "rescheduled"]
    assert len(hist["payload"]["dropped"]) == len(w.kinds) and hist["payload"]["moved"] == []


def test_rescheduled_windows_run_once_the_hold_ends(tmp_path):
    rig = Rig(tmp_path, ops=FakeOperations({1: OpResult(OpStatus.FAILED, signal="rate_limited")}))
    day, w = _busy_window(rig)
    rig.run(day, w.idx)
    added = rig.svc.store.sessions(IG, origin="reschedule")
    assert added
    first = added[0]
    rep = rig.run(date.fromisoformat(first["day"]), first["idx"])
    assert rep["status"] == "done" and {o["status"] for o in rep["ops"]} <= {"verified", "skipped"}
    assert any(o["status"] == "verified" for o in rep["ops"])


# ---------------------------------------------------------------- approvals and modes


def test_approval_pending_is_recorded_and_the_window_goes_on(tmp_path):
    ops = FakeOperations()
    ops.script = {i: (lambda c: OpResult(OpStatus.AWAITING_APPROVAL) if c.kind == "comment" else
                      OpResult(OpStatus.VERIFIED)) for i in range(200)}
    rig = Rig(tmp_path, ops=ops)
    day, w = next((d, w) for d in (MONDAY + timedelta(days=i) for i in range(7)) for w in rig.ex.windows(IG, d)
                  if "comment" in w.kinds[:-1])
    rep = rig.run(day, w.idx)
    assert rep["status"] == "done"
    waiting = [o for o in rep["ops"] if o["kind"] == "comment"]
    assert waiting and {o["status"] for o in waiting} == {"awaiting_approval"}
    assert all(o["status"] == "verified" for o in rep["ops"] if o["kind"] != "comment")
    assert all(c.ask for c in ops.calls if c.kind == "comment")
    assert all(o["content_origin"] == "draft" and o["content_id"] for o in waiting)
    assert {cid for cid, _ in rig.content.used} >= {o["content_id"] for o in waiting}
    for o in waiting:
        ops.later[o["run_id"]] = OpResult(OpStatus.VERIFIED, run_id=o["run_id"])
    assert asyncio.run(rig.ex.reconcile()) == len(waiting)
    assert rig.svc.store.counters(IG, day)[Kind.COMMENT]["done"] == len(waiting)


def test_auto_is_refused_on_linkedin_and_reddit_and_honored_elsewhere(tmp_path):
    from agentos.engage.errors import ProgramInvalid
    from agentos.engage.service import parse_program

    for site in ("linkedin", "reddit"):
        for part in ("comments: {mode: auto}", "community: {reply_to_comments: {mode: auto}}"):
            with pytest.raises(ProgramInvalid, match="approve mode"):
                parse_program(f"program: x1\nsurface: biz-a/{site}\n{part}\nphases: [{{kind: steady}}]")
    rig = Rig(tmp_path)
    program = rig.svc.store.active(IG).definition
    auto_ig = program.model_copy(update={"comments": Comments(mode=Mode.AUTO)})
    assert rig.ex._ask(auto_ig, "comment", True, None) is False           # owner-chosen auto on Instagram
    assert rig.ex._ask(program, "comment", True, None) is True             # approve mode
    off = program.model_copy(update={"comments": Comments(mode=Mode.OFF)})
    assert rig.ex._ask(off, "comment", True, None) is None
    approved = Content("i1", ContentOrigin.ITEM, "hi", approved=True)
    assert rig.ex._ask(program, "post", True, approved) is False
    assert rig.ex._ask(program, "post", True, Content("d1", ContentOrigin.DRAFT, "hi")) is True
    # defense in depth: even a program that got past validation never runs auto there
    for site in ("linkedin", "reddit"):
        sneaky = program.model_copy(update={"site": site, "comments": Comments(mode=Mode.AUTO)})
        assert rig.ex._ask(sneaky, "comment", True, None) is True
        assert rig.ex._ask(sneaky.model_copy(update={"community": sneaky.community.model_copy(
            update={"reply_to_comments": sneaky.community.reply_to_comments.model_copy(
                update={"mode": Mode.AUTO})})}), "reply", True, None) is True


def test_operations_without_content_or_target_are_skipped_with_a_reason(tmp_path):
    rig = Rig(tmp_path, content=FakeContent(items=[]))
    rig.targets.pool["like"] = []
    day, w = next((d, w) for d in (MONDAY + timedelta(days=i) for i in range(7)) for w in rig.ex.windows(IG, d)
                  if "comment" in w.kinds and "like" in w.kinds)
    rep = rig.run(day, w.idx)
    reasons = {o["kind"]: o["reason"] for o in rep["ops"] if o["status"] == "skipped"}
    assert reasons["comment"] == "no_content" and reasons["like"] == "no_target"
    assert rep["status"] == "done"


def test_the_day_cap_from_the_plan_is_enforced_at_run_time(tmp_path):
    rig = Rig(tmp_path)
    day, w = next((d, w) for d in (MONDAY + timedelta(days=i) for i in range(7)) for w in rig.ex.windows(IG, d)
                  if w.kinds.count("like") >= 2)
    plan = rig.svc.day_plan(IG, day)
    plan.caps[Kind.LIKE] = 1
    rig.svc.store.put_plan(plan)
    rep = rig.run(day, w.idx)
    likes = [o for o in rep["ops"] if o["kind"] == "like"]
    assert [o["status"] for o in likes].count("verified") == 1
    assert {o["reason"] for o in likes if o["status"] == "skipped"} == {"cap_daily"}


# ---------------------------------------------------------------- the lane


def test_the_lane_is_always_released(tmp_path):
    class Boom(FakeOperations):
        async def run(self, *a, **kw):
            raise RuntimeError("port broke")

    rig = Rig(tmp_path, ops=Boom())
    day, w = _busy_window(rig, 1)
    rep = rig.run(day, w.idx)
    assert rep["status"] == "aborted" and rep["end_reason"] == "error: RuntimeError"
    assert rig.lanes.held == {} and len(rig.lanes.releases) == len(rig.lanes.claims) == 1

    refused = Rig(tmp_path / "b", lanes=FakeLanes(refuse="human_in_control"))
    rep = refused.run(day, w.idx)
    assert rep["status"] == "refused" and rep["end_reason"] == "lane: human_in_control"
    assert {o["reason"] for o in rep["ops"]} == {"lane_unavailable"} and refused.ops.calls == []

    ok = Rig(tmp_path / "c")
    ok.run_days(MONDAY, 2)
    assert ok.lanes.held == {} and len(ok.lanes.claims) == len(ok.lanes.releases) > 0
    assert all(h.startswith(f"engage:{IG}:") for _, h in ok.lanes.claims)
    assert len({h for _, h in ok.lanes.claims}) == len(ok.lanes.claims)       # a fresh nonce each window


# ---------------------------------------------------------------- scheduling


def test_the_loop_fires_due_windows_only_with_the_switch_on_and_marks_missed_ones(tmp_path):
    rig = Rig(tmp_path)
    rig.svc.executor = rig.ex
    day, w = _busy_window(rig, 1)
    later = [x for x in rig.ex.windows(IG, day) if x.start > w.start]
    rig.at(w.start + timedelta(minutes=1))

    async def tick():
        started = await rig.svc.tick()
        for t in list(rig.svc.running.values()):
            await t
        return started

    assert asyncio.run(tick()) == []                    # the switch is off by default
    rig.svc.sessions_enabled = lambda: True
    earlier = [x for x in rig.ex.windows(IG, day) if x.start < w.start]
    started = asyncio.run(tick())
    assert [(s["idx"]) for s in started] == [w.idx]
    assert rig.svc.store.session(IG, day, w.idx)["status"] == "done"
    for x in earlier:                                   # more than 30 minutes late: missed, never run late
        assert rig.svc.store.session(IG, day, x.idx)["status"] == "missed"
    assert asyncio.run(tick()) == []
    if later:
        rig.at(later[0].start + timedelta(seconds=5))
        assert [s["idx"] for s in asyncio.run(tick())] == [later[0].idx]


def test_build_wires_no_executor_without_a_runner_and_the_switch_defaults_off(tmp_path):
    from agentos.core.config import NodeConfig
    from agentos.core.runtime import Runtime
    from agentos.engage import service

    svc = service.build(Runtime(), home=tmp_path)
    assert svc.executor is None and not svc.sessions_enabled()
    assert NodeConfig().engage_sessions is False
    assert svc.status() == {"dry_run": True, "surfaces": [], "note": svc.status()["note"]}


# ---------------------------------------------------------------- the CLI


def test_the_cli_previews_moves_a_session_to_now_and_clears_holds(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentos.engage.cli import app

    rig = Rig(tmp_path, RD_YAML, surface=RD, registry=Registry(installed=lambda f: False))
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    run = CliRunner().invoke
    day = next(MONDAY + timedelta(days=i) for i in range(7) if rig.ex.windows(RD, MONDAY + timedelta(days=i)))
    out = run(app, ["run", RD, "--dry-run", "--day", day.isoformat()])
    assert out.exit_code == 0, out.output
    assert "would not run" in out.output and "cannot run: no flow" in out.output and "Dry run" in out.output
    assert run(app, ["run", RD]).exit_code == 2
    out = run(app, ["run", RD, "--now", "--day", day.isoformat(), "--session", "1"])
    assert out.exit_code == 0 and "moved to now" in out.output and "off on this node" in out.output
    assert rig.svc.store.session(RD, day, 0)["status"] == "scheduled"
    rig.svc.store.add_hold(RD, reason="needs_human:captcha", signal="captcha", since=datetime.now(UTC))
    out = run(app, ["holds", RD])
    assert out.exit_code == 0 and "needs_human:captcha" in out.output and "until you clear it" in out.output
    out = run(app, ["hold", "clear", RD])
    assert out.exit_code == 0 and "1 hold cleared" in out.output
    assert run(app, ["holds", RD]).output.strip() == f"{RD}: no holds."
    for o in (out,):
        assert "—" not in o.output


# ---------------------------------------------------------------- the production adapters


class _StubRunner:
    def __init__(self, final: str = "succeeded_verified", error: dict | None = None, raises: Exception | None = None):
        from types import SimpleNamespace as NS

        self.flows = NS(get=lambda fid: NS(id=fid, effect="commit"))
        self.surfaces = NS(approval=lambda fv, target: (f"Post as u/biz on {target.surface}",
                                                       {"operation": fv.id, "surface": target.surface,
                                                        "params": dict(target.params)}))
        self.final, self.error, self.raises = final, error, raises
        self.submits: list[dict] = []
        self.cancelled: list[str] = []

    def connector_target(self, fv, surface, params):
        from types import SimpleNamespace as NS

        return NS(lane="biz-a/reddit", surface=surface, params=params)

    def existing_run(self, key, **kw):
        return None

    def _run(self, status):
        from types import SimpleNamespace as NS

        return NS(id="run_1", status=status, error=self.error, output={"data": {"post_url": "https://x"}})

    def submit(self, **kw):
        if self.raises is not None:
            raise self.raises
        self.submits.append(kw)
        return self._run("waiting_approval" if kw.get("approval_id") else "queued"), True

    async def wait(self, run_id, timeout, stop=None):
        return self._run(self.final)

    async def cancel(self, run_id, by=None):
        self.cancelled.append(run_id)

    def get(self, run_id):
        return self._run(self.final)


def test_the_runner_adapter_never_authorizes_and_asks_through_the_approval_store():
    from types import SimpleNamespace as NS

    from agentos.core.runtime import Runtime
    from agentos.engage.adapters import RunnerOperations
    from agentos.runner.connectors.surfaces import PaceLimited

    spec = Registry().get("reddit", Kind.POST)
    params = {"subreddit": "test", "title": "Hi", "body": "Hello"}
    made: list[dict] = []
    approvals = NS(create_action=lambda **kw: made.append(kw) or NS(id="ap_1"))

    def call(runner, ask):
        port = RunnerOperations(Runtime(runner=runner, approvals=approvals))
        return asyncio.run(port.run(RD, spec, params, idempotency_key="engage:k1", ask=ask, parent="claim:engage:x"))

    runner = _StubRunner()
    res = call(runner, True)                                     # approve mode: parked on an approval card
    assert res.status is OpStatus.AWAITING_APPROVAL and res.run_id == "run_1"
    [sub] = runner.submits
    assert sub["authorized"] is False and sub["approval_id"] == "ap_1" and sub["surface"] == RD
    assert sub["parent"] == "claim:engage:x" and sub["idempotency_key"] == "engage:k1" and sub["source"] == "engage"
    assert made[0]["effect"] == "commit" and made[0]["lane"] == RD and made[0]["content"]["params"] == params
    assert "approve mode" in made[0]["reason"]

    runner = _StubRunner()
    res = call(runner, False)                                    # the policy decides at dispatch
    assert res.status is OpStatus.VERIFIED and res.output == {"post_url": "https://x"}
    assert runner.submits[0]["authorized"] is False and runner.submits[0]["approval_id"] is None
    assert len(made) == 1

    res = call(_StubRunner("failed", {"code": "signal_captcha"}), False)
    assert res.status is OpStatus.FAILED and res.signal == "captcha"
    res = call(_StubRunner("failed", {"code": "rate_limited"}), False)
    assert res.signal == "rate_limited"
    res = call(_StubRunner("failed", {"code": "wrong_account"}), False)
    assert res.signal == "wrong_account"
    res = call(_StubRunner(raises=PaceLimited("used up")), False)
    assert res.status is OpStatus.HELD and res.error == "pace_limited" and res.signal is None
    held = _StubRunner("queued", {"code": "connector_broken"})
    res = call(held, False)
    assert res.status is OpStatus.HELD and held.cancelled == ["run_1"]   # a held run never acts after its window


# ---------------------------------------------------------------- review fixes

LIKE_YAML = """
program: biz-a-ig
surface: biz-a/instagram
timezone: Europe/Vilnius
phases:
  - kind: steady
    daily: {like: 15}
"""


class _Runs:
    """A runner whose runs end as `status` with `error`, and which can hold a run under a key ahead
    of the engine (`planted`)."""

    def __init__(self, status="succeeded_verified", error=None, planted=None, race=None):
        from types import SimpleNamespace as NS

        self.flows = NS(get=lambda fid: NS(id=fid, effect="commit"))
        self.surfaces = NS(approval=lambda fv, target: ("summary", {"params": dict(target.params)}))
        self.status, self.error, self.planted, self.race = status, error, planted, race
        self.submits, self.cancelled, self.runs = [], [], {}
        self.stops: list = []

    def connector_target(self, fv, surface, params):
        from types import SimpleNamespace as NS

        return NS(lane=surface, surface=surface, params=params)

    def existing_run(self, key, **kw):
        return self.planted

    def submit(self, **kw):
        from types import SimpleNamespace as NS

        if self.race is not None:
            return self.race, False
        self.submits.append(kw)
        run = NS(id=f"run_{len(self.submits)}", status="waiting_approval" if kw.get("approval_id") else "queued",
                 error=None, output=None, source=kw["source"], principal=kw["principal"], parent=kw["parent"],
                 flow_id=kw["flow_id"], params=dict(kw["params"]))
        self.runs[run.id] = run
        return run, True

    async def wait(self, run_id, timeout, stop=None):
        run = self.runs[run_id]
        run.status, run.error = self.status, self.error
        self.stops.append(stop(run) if stop else None)
        return run

    async def cancel(self, run_id, by=None):
        self.cancelled.append(run_id)

    def get(self, run_id):
        return self.runs[run_id]


def _runner_rig(home, runner, yaml_text=LIKE_YAML, approvals=None):
    from types import SimpleNamespace as NS

    from agentos.core.runtime import Runtime
    from agentos.engage.adapters import RunnerOperations

    made: list[dict] = []
    approvals = approvals or NS(create_action=lambda **kw: made.append(kw) or NS(id=f"ap_{len(made)}"))
    rig = Rig(home, yaml_text, ops=RunnerOperations(Runtime(runner=runner, approvals=approvals), wait_s=600.0))
    rig.made = made
    return rig


def _first(rig, day=MONDAY):
    return min((w for w in rig.ex.windows(rig.surface, day) if w.kinds), key=lambda w: w.start)


@pytest.mark.parametrize("code,signal", [("rate_limited", "rate_limited"), ("signal_checkpoint", "checkpoint")])
def test_a_run_the_runner_requeues_after_a_signal_is_that_signal(tmp_path, code, signal):
    runner = _Runs(status="queued", error={"code": code, "retrying": True})
    rig = _runner_rig(tmp_path, runner)
    w = _first(rig)
    rep = rig.run(MONDAY, w.idx)
    assert rep["status"] == "ended_early" and rep["end_reason"] == f"signal: {signal}"
    assert rep["ops"][0]["status"] == "failed" and len(runner.submits) == 1
    assert runner.cancelled == ["run_1"]                       # the runner does not retry it behind our back
    assert runner.stops == [True]                              # the wait stops on it, not after 600 s
    [hold] = rig.svc.store.holds(IG)
    assert hold["signal"] == signal
    assert hold["until"] is not None if signal == "rate_limited" else hold["until"] is None
    assert not [o for o in rig.svc.store.ops(IG) if o["moved_from"]]   # it may have acted: never redone


@pytest.mark.parametrize("acted", [None, True])
def test_a_failed_op_that_may_have_acted_is_not_queued_again(tmp_path, acted):
    rig = Rig(tmp_path, ops=FakeOperations({1: OpResult(OpStatus.FAILED, signal="captcha", error="signal",
                                                        acted=acted)}))
    day, w = _busy_window(rig)
    rig.run(day, w.idx)
    rows = rig.svc.store.ops(session_id=rig.svc.store.session(IG, day, w.idx)["id"])
    moved = {o["moved_from"] for o in rig.svc.store.ops(IG) if o["moved_from"]}
    assert rows[1]["status"] == "failed" and rows[1]["id"] not in moved   # no new key, target or words for it
    assert moved and moved <= {o["id"] for o in rows[2:]}                  # the ones it never reached still move


def test_a_run_queued_for_another_reason_is_held_not_a_signal(tmp_path):
    from types import SimpleNamespace as NS

    from agentos.engage.adapters import result_of, signalled

    paced = NS(id="r", status="queued", error={"code": "pace_limited"}, output=None)
    assert not signalled(paced) and result_of(paced).status is OpStatus.HELD
    fresh = NS(id="r", status="queued", error=None, output=None)
    assert not signalled(fresh) and result_of(fresh).error == "not_started"


def test_a_run_under_the_key_that_the_engine_did_not_submit_is_never_adopted(tmp_path):
    from types import SimpleNamespace as NS

    planted = NS(id="agent_run", status="succeeded_verified", error=None, output={"data": {}}, source="tool",
                 principal={"kind": "agent", "id": "a1"}, parent=None, flow_id="connector.instagram.comment",
                 params={})
    for kw in ({"planted": planted}, {"race": planted}):
        runner = _Runs(**kw)
        rig = _runner_rig(tmp_path / str(len(kw) + id(kw) % 7), runner, IG_YAML)
        day, w = next((d, w) for d in (MONDAY + timedelta(days=i) for i in range(7))
                      for w in rig.ex.windows(IG, d) if "comment" in w.kinds)
        rep = rig.run(day, w.idx)
        comments = [o for o in rep["ops"] if o["kind"] == "comment"]
        assert comments and all(o["status"] == "failed" and o["reason"] == "foreign_run" for o in comments)
        assert all(o["run_id"] == "agent_run" for o in comments)
        assert not rig.svc.store.counters(IG, day).get(Kind.COMMENT, {}).get("done")
        assert any(n["kind"] == "foreign_run" for n in rig.svc.store.notices(IG, limit=50))
        if "planted" in kw:
            assert rig.made == [] and not runner.submits         # no approval card, nothing submitted


def test_the_engine_adopts_only_its_own_run_and_the_approval_binds_the_params():
    from types import SimpleNamespace as NS

    from agentos.core.runtime import Runtime
    from agentos.engage.adapters import PRINCIPAL, RunnerOperations, ours

    spec = Registry().get("reddit", Kind.POST)
    params = {"subreddit": "test", "title": "Hi", "body": "Hello"}
    mine = NS(id="run_9", status="succeeded_verified", error=None, output={"data": {}}, source="engage",
              principal=dict(PRINCIPAL), parent="claim:engage:x:n1", flow_id=spec.flow_id,
              params={**params, "acts_as_id": "u1"})            # the binding adds its own params
    assert ours(mine, spec.flow_id, params, "claim:engage:x:n1")
    assert not ours(mine, spec.flow_id, params, "claim:engage:x:n2")           # another window's nonce
    assert not ours(mine, spec.flow_id, {**params, "body": "other"}, "claim:engage:x:n1")
    assert not ours(mine, "connector.reddit.comment", params, "claim:engage:x:n1")
    port = RunnerOperations(Runtime(runner=_Runs(planted=mine), approvals=NS()))
    res = asyncio.run(port.run(RD, spec, params, idempotency_key="k", ask=True, parent="claim:engage:x:n1"))
    assert res.status is OpStatus.VERIFIED and res.run_id == "run_9"

    made: list[dict] = []
    approvals = NS(create_action=lambda **kw: made.append(kw) or NS(id="ap"))
    for body in ("Hello", "Hello!"):
        port = RunnerOperations(Runtime(runner=_Runs(), approvals=approvals))
        asyncio.run(port.run(RD, spec, {**params, "body": body}, idempotency_key="k", ask=True, parent="claim:p"))
    assert made[0]["content_hash"] != made[1]["content_hash"]


def test_operation_keys_carry_a_tag_only_this_node_can_make(tmp_path):
    a, b = Rig(tmp_path / "a"), Rig(tmp_path / "b")
    ka = a.ex.op_key(IG, MONDAY, 0, 1)
    assert ka == a.ex.op_key(IG, MONDAY, 0, 1) and ka.startswith(f"engage:{IG}:{MONDAY.isoformat()}:0:1:")
    assert ka != b.ex.op_key(IG, MONDAY, 0, 1) and ka != a.ex.op_key(IG, MONDAY, 0, 2)


def test_a_cap_the_owner_lowers_after_the_plan_is_saved_holds_at_once(tmp_path):
    rig = Rig(tmp_path, LIKE_YAML)
    wins = sorted((w for w in rig.ex.windows(IG, MONDAY) if w.kinds), key=lambda w: w.start)
    assert len(wins) >= 2
    plan = rig.svc.day_plan(IG, MONDAY + timedelta(days=1))      # drawn and saved before the change
    assert plan.caps[Kind.LIKE] > 1
    rig.run(MONDAY, wins[0].idx)
    before = len(rig.ops.calls)
    rig.svc.set_cap(IG, Kind.LIKE, 0, by=OWNER)
    assert rig.svc.store.plans(IG, MONDAY, MONDAY)[0].caps[Kind.LIKE] > 0         # the saved plan is unchanged
    rep = rig.run(MONDAY, wins[1].idx)
    assert len(rig.ops.calls) == before
    assert {o["reason"] for o in rep["ops"]} == {"cap_daily"}
    program = rig.svc.store.active(IG).definition
    left = [{"id": i, "kind": "like"} for i in range(3)]
    tue = MONDAY + timedelta(days=1)
    start = datetime.combine(tue, datetime.min.time(), tzinfo=UTC) + timedelta(hours=10)
    assert rig.ex._fit(program, plan, rig.ex.windows(IG, tue), start, 10.0, left) == []   # the reschedule too
    rig.svc.set_cap(IG, Kind.LIKE, 1, by=OWNER)
    assert rig.ex._limits(program, plan)[0]["like"] == 1 and rig.ex._limits(program, plan)[1]["like"] <= 1


def test_a_cancelled_window_is_closed_as_aborted_and_the_cancel_goes_on(tmp_path):
    class Stop(FakeOperations):
        async def run(self, *a, **kw):
            raise asyncio.CancelledError()

    rig = Rig(tmp_path, LIKE_YAML, ops=Stop())
    w = min((w for w in rig.ex.windows(IG, MONDAY) if len(w.kinds) >= 2), key=lambda w: w.start)
    rig.at(w.start)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rig.ex.run_window(IG, MONDAY, w.idx))
    row = rig.svc.store.session(IG, MONDAY, w.idx)
    assert row["status"] == "aborted" and row["end_reason"].startswith("cancelled")
    ops = rig.svc.store.ops(session_id=row["id"])
    assert ops[0]["status"] == "attempted" and {o["reason"] for o in ops[1:]} == {"interrupted"}
    assert rig.lanes.held == {}


def test_sessions_a_stopped_node_left_running_are_recovered_at_start(tmp_path):
    rig = Rig(tmp_path, LIKE_YAML)
    rig.svc.executor = rig.ex
    w = _first(rig)
    row = rig.svc.store.put_session(IG, MONDAY, w.idx, planned_start=w.start, minutes=w.minutes)
    rig.svc.store.add_ops(row["id"], IG, MONDAY, list(w.kinds))
    rig.svc.store.set_session(row["id"], status="running", started_at=w.start.isoformat())
    rig.at(w.start + timedelta(minutes=1))
    assert rig.ex.recover() == 0                                  # before its planned end: left alone
    rig.at(w.end + timedelta(minutes=1))
    assert rig.ex.recover(busy={IG}) == 0                         # its window runs in this process
    assert rig.svc.store.session(IG, MONDAY, w.idx)["status"] == "running"

    async def start():
        await rig.svc.start()
        for t in (rig.svc._task, rig.svc._sessions_task):
            t.cancel()

    asyncio.run(start())
    row = rig.svc.store.session(IG, MONDAY, w.idx)
    assert row["status"] == "aborted" and row["end_reason"].startswith("interrupted")
    assert {o["status"] for o in rig.svc.store.ops(session_id=row["id"])} == {"skipped"}
    assert any(n["kind"] == "interrupted" for n in rig.svc.store.notices(IG))
    assert any(h["kind"] == "session" and h["payload"]["status"] == "aborted" for h in rig.svc.store.history(IG))
    assert rig.ex.recover() == 0


def test_a_long_pause_renews_the_claim_and_a_lost_claim_ends_the_window(tmp_path):
    from agentos.engage import executor as ex_mod

    rig = Rig(tmp_path, LIKE_YAML)
    w = _first(rig)
    rig.ex._gaps = lambda persona, w, n: [1000.0] * n            # longer than the 900 s lease
    rig.run(MONDAY, w.idx)
    assert len(rig.lanes.renewals) == 4 * len(w.kinds)           # every 300 s of each pause, and before each op
    assert rig.t - w.start.timestamp() >= 1000.0 * len(w.kinds) - 1e-6   # the pauses keep their length
    assert {h for _, h in rig.lanes.renewals} == {h for _, h in rig.lanes.claims}

    lost = Rig(tmp_path / "b", LIKE_YAML, lanes=FakeLanes(lost="claim_lost"))
    lost.ex._gaps = lambda persona, w, n: [ex_mod.RENEW_EVERY_S * 2] * n
    rep = lost.run(MONDAY, w.idx)
    assert rep["status"] == "ended_early" and rep["end_reason"] == "lane: claim_lost"
    assert lost.ops.calls == [] and {o["reason"] for o in rep["ops"]} == {"rescheduled"}
    # the operations not run move to later sessions (the waiting caller gets the lane now), in
    # waking hours and no sooner than LANE_LOST_GAP after the claim ended
    ended = lost.t
    moved = [r for d in range(7) for r in lost.svc.store.sessions(IG, MONDAY + timedelta(days=d))
             if r["origin"] == "reschedule"]
    assert moved, "the lost session's operations were dropped"
    moved_ops = [o for r in moved for o in lost.svc.store.ops(session_id=r["id"])]
    assert sorted(o["kind"] for o in moved_ops) == sorted(w.kinds)
    for r in moved:
        assert datetime.fromisoformat(r["planned_start"]).timestamp() >= ended + ex_mod.LANE_LOST_GAP.total_seconds()
    assert any(n["kind"] == "lane_lost" and "moved to later sessions" in n["text"]
               for n in lost.svc.store.notices(IG))


class LeasedLanes(FakeLanes):
    """A lease that runs out, as the node's does: at most 900 s from the claim or the last renewal."""

    def __init__(self, clock):
        super().__init__()
        self.clock, self.expires = clock, {}
        self.lapsed: list[float] = []

    def live(self, lane: str) -> bool:
        return self.clock() <= self.expires.get(lane, 0.0)

    async def claim(self, lane, *, holder, purpose, ttl_s):
        await super().claim(lane, holder=holder, purpose=purpose, ttl_s=ttl_s)
        self.expires[lane] = self.clock() + min(ttl_s, 900.0)

    async def renew(self, lane, *, holder, ttl_s):
        if not self.live(lane):
            self.lapsed.append(self.clock())
            raise LaneUnavailable("claim_lost")
        await super().renew(lane, holder=holder, ttl_s=ttl_s)
        self.expires[lane] = self.clock() + min(ttl_s, 900.0)


def test_slow_operations_and_short_pauses_never_let_the_lease_lapse(tmp_path):
    """Each operation takes 600 s (as long as a window waits for a connector run) and every pause is under
    300 s: renewing only inside long pauses let the lease run out between likes."""
    rig = Rig(tmp_path, LIKE_YAML)
    lanes = LeasedLanes(lambda: rig.t)
    rig.ex.lanes = rig.lanes = lanes
    w = max((w for w in rig.ex.windows(rig.surface, MONDAY)), key=lambda w: len(w.kinds))
    rig.ex._gaps = lambda persona, w, n: [250.0] * n
    held_at_op: list[bool] = []

    def slow(call):
        held_at_op.append(lanes.live(call.surface))
        rig.t += 600.0
        return OpResult(OpStatus.VERIFIED)

    rig.ops.script = {i: slow for i in range(len(w.kinds))}
    rep = rig.run(MONDAY, w.idx)
    assert len(w.kinds) >= 2 and len(held_at_op) == len(w.kinds)
    assert all(held_at_op) and lanes.lapsed == [] and rep["status"] == "done"


def test_windows_refused_by_a_hold_say_what_they_dropped(tmp_path):
    rig = Rig(tmp_path, LIKE_YAML)
    w = _first(rig)
    rig.svc.store.add_hold(IG, reason="needs_human:captcha", signal="captcha", since=w.start - timedelta(hours=1))
    rep = rig.run(MONDAY, w.idx)
    assert rep["status"] == "refused" and {o["reason"] for o in rep["ops"]} == {"held"}
    [h] = [h for h in rig.svc.store.history(IG) if h["kind"] == "dropped"]
    assert h["payload"]["reason"] == "held" and h["payload"]["ops"] == list(w.kinds)
    [n] = [n for n in rig.svc.store.notices(IG) if n["kind"] == "dropped"]
    assert "did not run" in n["text"] and "—" not in n["text"]
    assert rig.svc.store.sessions(IG, origin="reschedule") == []          # still not carried forward


def test_an_unverified_operation_counts_once_when_it_verifies_later(tmp_path):
    ops = FakeOperations({0: OpResult(OpStatus.UNVERIFIED, error="still_running")})
    rig = Rig(tmp_path, LIKE_YAML, ops=ops)
    w = _first(rig)
    rig.run(MONDAY, w.idx)
    first = rig.svc.store.ops(session_id=rig.svc.store.session(IG, MONDAY, w.idx)["id"])[0]
    assert first["status"] == "unverified"
    done = rig.svc.store.counters(IG, MONDAY)[Kind.LIKE]["done"]
    assert asyncio.run(rig.ex.reconcile()) == 0                     # not proved yet: nothing changes
    ops.later[first["run_id"]] = OpResult(OpStatus.VERIFIED, run_id=first["run_id"])
    assert asyncio.run(rig.ex.reconcile()) == 1
    assert asyncio.run(rig.ex.reconcile()) == 0                     # idempotent
    assert rig.svc.store.counters(IG, MONDAY)[Kind.LIKE]["done"] == done + 1
    assert rig.svc.store.ops(status="unverified") == []


def test_a_window_missed_across_midnight_is_marked_missed(tmp_path):
    rig = Rig(tmp_path, LIKE_YAML)
    mon = [w for w in rig.ex.windows(IG, MONDAY) if w.kinds]
    assert mon
    tz = ZoneInfo("Europe/Vilnius")
    tue = datetime.combine(MONDAY + timedelta(days=1), datetime.min.time(), tzinfo=tz) + timedelta(minutes=20)
    rig.ex.due(tue.astimezone(UTC))
    assert {rig.svc.store.session(IG, MONDAY, w.idx)["status"] for w in mon} == {"missed"}


def test_a_surface_without_a_time_zone_says_so_once_and_in_status(tmp_path):
    rig = Rig(tmp_path, LIKE_YAML.replace("timezone: Europe/Vilnius\n", ""))
    program = rig.svc.store.active(IG).definition
    for _ in range(3):
        assert rig.svc.timezone(program).key == "UTC"
    tz_notices = [n for n in rig.svc.store.notices(IG, limit=50) if n["kind"] == "timezone"]
    assert len(tz_notices) == 1 and "UTC" in tz_notices[0]["text"] and "—" not in tz_notices[0]["text"]
    st = rig.svc.status(IG)["surfaces"][0]
    assert st["timezone"] == "UTC" and "time zone" in st["timezone_fallback"]
    assert Rig(tmp_path / "b", LIKE_YAML).svc.status(IG)["surfaces"][0]["timezone_fallback"] is None


def test_an_approved_comment_goes_in_the_next_session_not_the_hour_it_was_approved(tmp_path):
    rig = Rig(tmp_path)
    tz = ZoneInfo("Europe/Vilnius")
    late = datetime(2026, 11, 2, 23, 40, tzinfo=tz)                  # Monday 23:40, after the day's sessions
    slots = {rig.ex.approved_slot(IG, late.astimezone(UTC), key=f"run_{n}") for n in range(8)}
    persona = rig.svc.store.persona(IG)
    for slot in slots:
        assert slot > late
        local = slot.astimezone(tz)
        w = next(w for w in rig.ex.windows(IG, local.date()) if w.start <= slot <= w.end)
        assert w.start > late                                         # inside a session that had not begun
        lo, hi = persona.for_day(local.date()).window(local.date())
        assert lo <= local.hour + local.minute / 60 <= hi             # waking hours
    assert len(slots) > 1                                             # spread inside it, not all at once


def test_a_session_takes_in_the_approved_runs_sent_to_it(tmp_path):
    class Adopting(FakeOperations):
        def __init__(self):
            super().__init__()
            self.adopted = []

        def adopt(self, surface, *, parent, until):
            self.adopted.append((surface, parent, until))
            return ["run_9"]

    rig = Rig(tmp_path, LIKE_YAML, ops=Adopting())
    w = _first(rig)
    rig.run(MONDAY, w.idx)
    [(surface, parent, until)] = rig.ops.adopted
    assert surface == IG and until == w.end
    assert parent == claim_parent(rig.lanes.claims[0][1])
    assert any(h["kind"] == "approved_released" and h["payload"]["runs"] == ["run_9"]
               for h in rig.svc.store.history(IG))


def test_likes_another_path_made_on_the_account_count_against_the_cap(tmp_path):
    from agentos.policy.ledger import ActionLedger

    rig = Rig(tmp_path, LIKE_YAML)
    lg = ActionLedger(None, clock=lambda: rig.t)
    rig.ex.ledger = lg
    w = _first(rig)
    plan = rig.svc.day_plan(IG, MONDAY)
    cap = plan.caps[Kind.LIKE]
    for n in range(cap):                                          # an MCP agent and a flow, earlier that day
        lg.debit(IG, "like", actor="agent" if n % 2 else "flow", at=w.start - timedelta(hours=1, minutes=n))
    lg.debit(IG, "like", actor="person", at=w.start - timedelta(minutes=5))   # the owner's own: not counted
    rep = rig.run(MONDAY, w.idx)
    assert rig.ops.calls == [] and {o["reason"] for o in rep["ops"]} == {"cap_daily"}
    shared = [n for n in rig.svc.store.notices(IG) if n["kind"] == "cap_shared"]
    assert len(shared) == 1 and "another way" in shared[0]["text"] and "—" not in shared[0]["text"]

    fresh = Rig(tmp_path / "b", LIKE_YAML)
    fresh.ex.ledger = ActionLedger(None, clock=lambda: fresh.t)
    fresh.run(MONDAY, w.idx)
    assert fresh.ops.calls                                        # an empty ledger changes nothing


# ---- approved runs never go late, and a held surface never sends one --------------------------

def _run(run_id="run_1", error=None, approval_id="ap_1"):
    from types import SimpleNamespace

    return SimpleNamespace(id=run_id, error=error, approval_id=approval_id)


def test_an_approved_run_goes_only_inside_a_session(tmp_path):
    rig = Rig(tmp_path)
    w = _first(rig)
    inside = w.start + timedelta(minutes=w.minutes / 2)
    assert rig.ex.approved_gate(IG, inside, key="run_1", run=_run()) is None
    assert rig.ex.approved_gate(IG, w.end + timedelta(seconds=1), key="run_1", run=_run()) is not None


def test_an_approved_run_that_missed_its_slot_moves_to_the_next_session_never_the_night(tmp_path):
    """The node was down (or a person held the lane) through the slot: at 03:10 the run is due, but
    it goes inside the next session, in waking hours, not now."""
    rig = Rig(tmp_path)
    tz = ZoneInfo("Europe/Vilnius")
    night = datetime(2026, 11, 3, 3, 10, tzinfo=tz).astimezone(UTC)
    persona = rig.svc.store.persona(IG)
    for n in range(6):
        verdict = rig.ex.approved_gate(IG, night, key=f"run_{n}", run=_run(f"run_{n}"))
        assert verdict is not None and "end" not in verdict and not verdict.get("hold")
        slot = verdict["until"]
        local = slot.astimezone(tz)
        w = next(w for w in rig.ex.windows(IG, local.date()) if w.start <= slot <= w.end)
        assert w.start > night
        lo, hi = persona.for_day(local.date()).window(local.date())
        assert lo <= local.hour + local.minute / 60 <= hi
        assert rig.ex.approved_gate(IG, slot, key=f"run_{n}", run=_run(f"run_{n}")) is None   # then it goes
        assert "missed its session" in verdict["reason"] and "—" not in verdict["reason"]


def test_a_run_a_challenge_stranded_waits_for_the_hold_then_gets_a_later_session(tmp_path):
    """A run adopted into a session that a checkpoint ended: while the hold stands it never goes
    (not even inside a session), then it takes the next session after the owner clears it."""
    rig = Rig(tmp_path)
    w = _first(rig)
    signal_at = w.start + timedelta(minutes=2)
    rig.ex._hold(IG, reason="needs_human:checkpoint", now=signal_at, signal="checkpoint", text="held")
    inside = w.start + timedelta(minutes=w.minutes / 2)
    first = rig.ex.approved_gate(IG, inside, key="run_1", run=_run())
    assert first["hold"] and first["until"] == inside + timedelta(hours=1) and "checkpoint" in first["reason"]
    later = rig.ex.approved_gate(IG, inside + timedelta(days=2), key="run_1",
                                 run=_run(error={"held_since": inside.isoformat()}))
    assert later["hold"]                                              # still held: still waiting
    cleared_at = inside + timedelta(days=2, hours=1)
    rig.ex.clear_hold(IG, by=OWNER)
    after = rig.ex.approved_gate(IG, cleared_at, key="run_1", run=_run(error={"held_since": inside.isoformat()}))
    assert not after.get("hold") and after["until"] > cleared_at


def test_a_run_held_past_a_week_ends_with_a_clear_status(tmp_path):
    from agentos.engage.executor import APPROVED_HELD_MAX

    rig = Rig(tmp_path)
    w = _first(rig)
    rig.ex._hold(IG, reason="needs_human:captcha", now=w.start, signal="captcha", text="held")
    since = w.start
    verdict = rig.ex.approved_gate(IG, since + APPROVED_HELD_MAX, key="run_1",
                                   run=_run(error={"held_since": since.isoformat()}))
    assert verdict["end"] == "engage_held" and "not sent" in verdict["reason"]
    assert any(n["kind"] == "approved_ended" for n in rig.svc.store.notices(IG))


def test_a_rate_limit_moves_an_approved_run_past_its_end(tmp_path):
    rig = Rig(tmp_path)
    w = _first(rig)
    until = w.start + timedelta(hours=30)
    rig.ex._hold(IG, reason="rate_limited", now=w.start, signal="rate_limited", until=until, text="rests")
    verdict = rig.ex.approved_gate(IG, w.start + timedelta(minutes=1), key="run_1", run=_run())
    assert verdict["until"] > until and not verdict.get("hold")
    assert rig.ex.approved_slot(IG, w.start, key="run_1") > until


def test_an_approved_run_of_a_surface_with_no_approved_program_waits(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    w = _first(rig)
    monkeypatch.setattr(rig.svc.store, "active", lambda surface: None)
    verdict = rig.ex.approved_gate(IG, w.start + timedelta(minutes=1), key="run_1", run=_run())
    assert verdict["hold"] and "not approved" in verdict["reason"]


def test_a_sessions_own_run_left_over_after_it_ends_and_is_never_sent_later(tmp_path):
    """The node stopped mid-session: the run the session started stays queued, and the runner's
    recover would send it at restart time. The gate ends it instead, unless a session is on."""
    rig = Rig(tmp_path)
    w = _first(rig)
    own = _run(approval_id=None)
    assert rig.ex.approved_gate(IG, w.start + timedelta(minutes=1), run=own) is None      # inside its session
    night = datetime(2026, 11, 3, 2, 0, tzinfo=ZoneInfo("Europe/Vilnius")).astimezone(UTC)
    assert rig.ex.approved_gate(IG, night, run=own)["end"] == "session_over"
    rig.ex._hold(IG, reason="needs_human:checkpoint", now=w.start, signal="checkpoint", text="held")
    assert rig.ex.approved_gate(IG, w.start + timedelta(minutes=1), run=own)["end"] == "session_over"


def test_an_approved_run_waits_for_room_under_the_caps(tmp_path):
    from agentos.policy.ledger import ActionLedger

    rig = Rig(tmp_path)
    lg = ActionLedger(None, clock=lambda: rig.t)
    rig.ex.ledger = lg
    w = _first(rig)
    inside = w.start + timedelta(minutes=1)
    program = rig.svc.store.active(IG).definition
    caps, _ = rig.ex._limits(program, rig.svc.day_plan(IG, MONDAY))
    for n in range(caps["comment"]):                       # comments made another way earlier that day
        lg.debit(IG, "comment", actor="agent", at=inside - timedelta(hours=3, minutes=n))

    rig.svc.store.op_by_run = lambda run_id: {"kind": "comment", "day": MONDAY.isoformat(), "status": "awaiting_approval",
                                              "at": (inside - timedelta(days=1)).isoformat()}
    verdict = rig.ex.approved_gate(IG, inside, key="run_1", run=_run())
    assert "cap" in verdict["reason"] and verdict["until"].astimezone(ZoneInfo("Europe/Vilnius")).date() > MONDAY


def test_an_approved_run_counts_itself_once_against_its_own_day(tmp_path):
    from agentos.engage.store import iso

    rig = Rig(tmp_path)
    w = _first(rig)
    inside = w.start + timedelta(minutes=1)
    # the approved comment itself is the day's only comment (counted when it was planned): it may go
    sid = rig.svc.store.put_session(IG, MONDAY, w.idx, planned_start=w.start, minutes=w.minutes, origin="plan")["id"]
    rig.svc.store.add_ops(sid, IG, MONDAY, ["comment"])
    op = rig.svc.store.ops(session_id=sid)[0]
    rig.svc.store.set_op(op["id"], status="awaiting_approval", run_id="run_1", at=iso(inside - timedelta(hours=2)))
    program = rig.svc.store.active(IG).definition
    caps, _ = rig.ex._limits(program, rig.svc.day_plan(IG, MONDAY))
    assert caps.get("comment", 0) >= 1
    assert rig.ex.approved_gate(IG, inside, key="run_1", run=_run()) is None


def test_approved_runs_wait_while_sessions_are_switched_off(tmp_path):
    rig = Rig(tmp_path)
    w = _first(rig)
    rig.ex.enabled = lambda: False
    verdict = rig.ex.approved_gate(IG, w.start + timedelta(minutes=1), key="run_1", run=_run())
    assert verdict["hold"] and "switched off" in verdict["reason"]


def test_a_pause_keeps_what_the_owner_approved_however_long(tmp_path):
    from agentos.engage.executor import APPROVED_HELD_MAX

    rig = Rig(tmp_path)
    w = _first(rig)
    rig.ex.enabled = lambda: False
    since = w.start
    verdict = rig.ex.approved_gate(IG, since + APPROVED_HELD_MAX * 3, key="run_1",
                                   run=_run(error={"held_since": since.isoformat()}))
    assert verdict["hold"] and "end" not in verdict


def test_a_session_row_a_crash_left_running_is_not_a_live_session(tmp_path):
    from agentos.engage.executor import SESSION_GRACE

    rig = Rig(tmp_path)
    w = _first(rig)
    row = rig.svc.store.put_session(IG, MONDAY, w.idx, planned_start=w.start, minutes=w.minutes, origin="plan")
    rig.svc.store.set_session(row["id"], status="running")
    own = _run(approval_id=None)
    assert rig.ex.approved_gate(IG, w.end + SESSION_GRACE / 2, run=own) is None          # overrunning a little
    assert rig.ex.approved_gate(IG, w.end + SESSION_GRACE * 2, run=own)["end"] == "session_over"
