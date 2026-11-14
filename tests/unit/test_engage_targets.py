"""Engagement targets and the connectors behind them: the registry holds operations no canary
verified (op_unverified), the params builders read targets, and ConnectorTargets fills its
pool from connector read operations without repeats or the owner's own accounts."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from test_engage_executor import MONDAY, RD, RD_YAML, Rig

from agentos.engage.caps import Kind
from agentos.engage.fakes import FakeContent, FakeOperations, FakeTargets
from agentos.engage.models import Program
from agentos.engage.operations import DEFAULT_SPECS, BadTarget, Registry
from agentos.engage.ports import Content, ContentOrigin, OpResult, OpStatus, RefillingTargets, TargetPort
from agentos.engage.targets import ConnectorTargets

POST_TEXT = "connector.reddit.post_text"
ONLY_POST_TEXT = {POST_TEXT: "2026-11-04 reddit.com canary: post 1demo01"}
OWNER = {"kind": "human", "id": "owner", "owner": "owner"}


def node(verified: dict[str, str] | None = None) -> Registry:
    """Every connector installed; verified as given (the repo today: only post_text)."""
    marks = ONLY_POST_TEXT if verified is None else verified
    return Registry(installed=lambda flow_id: True, verified=lambda flow_id: marks.get(flow_id, ""))


def everything_verified() -> Registry:
    return node({s.flow_id: "2026-11-06 test canary" for s in DEFAULT_SPECS})


# ---------------------------------------------------------------- op_unverified


def test_an_installed_operation_no_canary_verified_is_not_valid():
    reg = node()
    for spec in DEFAULT_SPECS:
        want = None if spec.flow_id == POST_TEXT else "op_unverified"
        assert reg.problem(spec) == want, spec
    rows = {r["flow"]: r for r in reg.describe()}
    assert rows[POST_TEXT]["verified"].startswith("2026-11-04") and rows[POST_TEXT]["valid"]
    assert rows["connector.linkedin.react"] == {**rows["connector.linkedin.react"], "valid": False,
                                                "problem": "op_unverified", "verified": ""}
    # a missing flow and an unverified rule are named first
    assert Registry(installed=lambda f: False, verified=lambda f: "x").problem(reg.get("reddit", Kind.JOIN)) == "no_flow"
    assert Registry(installed=lambda f: True).problem(reg.get("reddit", Kind.POST)) == "op_unverified"
    assert all(everything_verified().valid(s) for s in DEFAULT_SPECS)


def _first_window(rig, kinds=("comment", "join")):
    return next((d, w) for d in (MONDAY + timedelta(days=i) for i in range(7))
                for w in rig.ex.windows(RD, d) if any(k in w.kinds for k in kinds))


def test_an_unverified_operation_aborts_the_window_and_holds_until_a_verified_mark(tmp_path):
    rig = Rig(tmp_path, RD_YAML, surface=RD, registry=node())
    day, w = _first_window(rig)
    preview = rig.ex.preview(RD, day, w.idx)
    assert not preview["would_run"] and "op_unverified" in {o["problem"] for o in preview["ops"]}
    rep = rig.run(day, w.idx)
    assert rep["status"] == "aborted" and rep["end_reason"] == "no_valid_rule"
    assert rig.ops.calls == [] and rig.lanes.held == {}
    [hold] = rig.svc.store.holds(RD)
    assert hold["reason"] == "no_valid_rule" and "op_unverified" in hold["detail"]
    assert any("a canary verified" in n["text"] for n in rig.svc.store.notices(RD))
    # the owner marks the operations verified after their canaries and clears the hold
    rig.ex.registry = everything_verified()
    assert rig.ex.clear_hold(RD, by=OWNER) == 1
    later = [(d, x) for d in (day + timedelta(days=i) for i in range(1, 7)) for x in rig.ex.windows(RD, d)]
    assert later and rig.ex.preview(RD, *later[0][:1], later[0][1].idx)["would_run"]


# ---------------------------------------------------------------- params builders


def _text(text: str = "Nice work", attachments: tuple[str, ...] = ()) -> Content:
    return Content("c1", ContentOrigin.ITEM, text, attachments=attachments)


def test_the_params_builders_read_their_targets():
    reg = node()
    build = {(s.site, str(s.kind)): s.build for s in reg.specs()}
    assert build[("reddit", "comment")](_text(), "https://www.reddit.com/r/test/comments/1demo02/a_title/") == {
        "subreddit": "test", "post_id": "1demo02", "text": "Nice work"}
    assert build[("reddit", "join")](None, "r/Vilnius") == {"subreddit": "Vilnius"}
    urn = "urn:li:activity:7392000000000000001"
    assert build[("linkedin", "comment")](_text(), f"https://www.linkedin.com/feed/update/{urn}/") == {
        "post_urn": urn, "text": "Nice work"}
    assert build[("linkedin", "like")](None, urn) == {"post_urn": urn}
    assert build[("linkedin", "follow")](None, "https://www.linkedin.com/company/example-robotics/") == {
        "profile_kind": "company", "name": "example-robotics"}
    assert build[("linkedin", "post")](_text("Hello", ("files:abc",)), None) == {"text": "Hello", "image": "files:abc"}
    assert build[("linkedin", "post")](_text("Hello"), None) == {"text": "Hello"}
    for kind, target in ((("reddit", "comment")), "https://evil.example/r/x/comments/abcde/"), \
                        ((("reddit", "join")), "r/../../x"), ((("linkedin", "like")), "urn:li:activity:1"), \
                        ((("linkedin", "follow")), "https://www.linkedin.com/in/x y/"):
        with pytest.raises(BadTarget):
            build[kind](_text(), target)
    with pytest.raises(BadTarget):
        build[("reddit", "comment")](_text("  "), "https://www.reddit.com/r/test/comments/1demo02/")


def test_a_target_the_operation_cannot_use_is_skipped_and_the_words_handed_back(tmp_path):
    content = FakeContent(items=[Content("i1", ContentOrigin.ITEM, "A comment", approved=True)], endless=False)
    rig = Rig(tmp_path, RD_YAML, surface=RD, registry=everything_verified(), content=content)
    rig.ex.targets = FakeTargets({"comment": ["not a post"], "join": [], "post": []})
    day, w = _first_window(rig, ("comment",))
    rep = rig.run(day, w.idx)
    [op] = [o for o in rep["ops"] if o["kind"] == "comment" and o["reason"] == "bad_target"]
    assert op["status"] == "skipped" and op["target"] == "not a post"
    assert set(content.returned) == {"i1"} and all(c.kind != "comment" for c in rig.ops.calls)


# ---------------------------------------------------------------- ConnectorTargets


class Ops:
    """A fake engage.db: just the ops rows ConnectorTargets reads."""

    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []

    def ops(self, surface=None, day=None, *, session_id=None, status=None, kind=None):
        return [r for r in self.rows if (surface is None or r["surface"] == surface) and (kind is None or r["kind"] == kind)]


def reddit_program(*values: str) -> Program:
    return Program.model_validate({"program": "rd", "surface": RD, "phases": [
        {"kind": "steady", "daily": {"comment": 2, "join": 1},
         "targets": [{"source": "communities", "values": list(values)}]}]})


LI = "biz-a/linkedin"


def linkedin_program(targets: list[dict] | None = None) -> Program:
    steady = {"kind": "steady", "daily": {"like": 3, "follow": 1, "comment": 1}}
    if targets is not None:
        steady["targets"] = targets
    return Program.model_validate({"program": "li", "surface": LI, "phases": [steady]})


def browse(posts_by_sub: dict[str, list[tuple[str, str, str]]]):
    """FakeOperations script: browse_subreddit answers with (subreddit, id, author) posts."""
    def answer(call):
        assert call.flow_id == "connector.reddit.browse_subreddit" and call.ask is False
        posts = [{"url": f"https://www.reddit.com/r/{sub}/comments/{pid}/t/", "id": f"t3_{pid}", "author": author}
                 for sub, pid, author in posts_by_sub.get(call.params["subreddit"], [])]
        return OpResult(OpStatus.VERIFIED, output={"posts": posts})
    return answer


class Clock:
    def __init__(self) -> None:
        self.t = 1_762_300_000.0

    def __call__(self) -> float:
        return self.t


def test_connector_targets_is_a_refilling_target_port():
    t = ConnectorTargets(Ops(), FakeOperations())
    assert isinstance(t, TargetPort) and isinstance(t, RefillingTargets)
    assert not isinstance(FakeTargets(), RefillingTargets)


def test_reddit_comment_targets_come_from_the_programs_communities_without_own_posts_or_repeats():
    answer = browse({"test": [("test", "aaaaa1", "MiraKestrelDemo"),       # the owner's own post
                              ("other", "aaaaa2", "x1"),                # a link outside the communities
                              ("test", "aaaaa3", "someone"),
                              ("test", "aaaaa4", "someone"),            # the same author again
                              ("test", "aaaaa5", "another")],
                     "Vilnius": [("Vilnius", "bbbbb1", "local")]})
    ops = FakeOperations({i: answer for i in range(10)})
    store = Ops([{"surface": RD, "kind": "comment", "target": "https://www.reddit.com/r/test/comments/aaaaa5/",
                  "status": "verified"}])
    clock = Clock()
    t = ConnectorTargets(store, ops, own=lambda site: {"mirakestreldemo"} if site == "reddit" else set(), clock=clock)
    p = reddit_program("test", "Vilnius")
    assert t.next(RD, "comment", p) is None and ops.calls == []          # next never reads the site
    res = asyncio.run(t.refill(RD, "comment", p, parent="claim:engage:biz-a/reddit"))
    assert res.status is OpStatus.VERIFIED
    [call] = ops.calls
    assert call.params == {"subreddit": "test"} and call.parent == "claim:engage:biz-a/reddit" and call.ask is False
    assert call.idempotency_key.startswith(f"engage-read:{RD}:connector.reddit.browse_subreddit:test:")
    assert t.next(RD, "comment", p) == "https://www.reddit.com/r/test/comments/aaaaa3/"
    assert t.next(RD, "comment", p) is None          # aaaaa5 was engaged with already; the rest are filtered
    asyncio.run(t.refill(RD, "comment", p))           # round robin: the next community
    assert ops.calls[-1].params == {"subreddit": "Vilnius"}
    assert t.next(RD, "comment", p) == "https://www.reddit.com/r/Vilnius/comments/bbbbb1/"
    asyncio.run(t.refill(RD, "comment", p))           # r/test again, in the same hour: nothing new
    assert t.next(RD, "comment", p) is None
    # a read key is per window: another window's claim parent never meets this window's run under it
    first = ops.calls[0].idempotency_key
    asyncio.run(t.refill(RD, "comment", p, parent="claim:engage:biz-a/reddit:other-window"))
    asyncio.run(t.refill(RD, "comment", p, parent="claim:engage:biz-a/reddit:other-window"))
    assert ops.calls[-1].params == {"subreddit": "test"} and ops.calls[-1].idempotency_key != first


def test_candidates_expire_after_three_days():
    ops = FakeOperations({0: browse({"test": [("test", "ccccc1", "a")]})})
    clock = Clock()
    t = ConnectorTargets(Ops(), ops, clock=clock)
    p = reddit_program("test")
    asyncio.run(t.refill(RD, "comment", p))
    clock.t += 4 * 86400
    assert t.next(RD, "comment", p) is None


def test_reddit_join_targets_are_the_communities_not_joined_yet_and_need_no_read():
    store = Ops([{"surface": RD, "kind": "join", "target": "test", "status": "verified"},
                 {"surface": RD, "kind": "join", "target": "Kaunas", "status": "skipped"}])
    ops = FakeOperations()
    t = ConnectorTargets(store, ops)
    p = reddit_program("test", "Kaunas", "Vilnius")
    assert t.next(RD, "join", p) == "Kaunas"          # a skipped op never reached the site
    assert t.next(RD, "join", p) == "Vilnius"
    assert t.next(RD, "join", p) is None
    assert asyncio.run(t.refill(RD, "join", p)) is None and ops.calls == []
    assert t.next(RD, "post", p) is None              # posts take their target from the content


def feed_answer(call):
    assert call.flow_id == "connector.linkedin.feed" and call.params == {}
    posts = [{"urn": "urn:li:activity:7392000000000000001", "author_url": "https://www.linkedin.com/in/mira-kestrel/"},
             {"urn": "urn:li:activity:7392000000000000002", "author_url": "https://www.linkedin.com/company/example-robotics/"},
             {"urn": "urn:li:activity:7392000000000000003",
              "author_url": "https://www.linkedin.com/in/arlo-fenwick-demo/"},
             {"urn": "urn:li:activity:7392000000000000004", "author_url": None},
             {"urn": "not-an-urn", "author_url": "https://www.linkedin.com/in/someone/"}]
    return OpResult(OpStatus.VERIFIED, output={"posts": posts})


def test_linkedin_targets_come_from_the_own_feed_and_never_the_member_itself():
    ops = FakeOperations({i: feed_answer for i in range(5)})
    own = {"arlo-fenwick-demo", "Arlo Fenwick"}
    t = ConnectorTargets(Ops(), ops, own=lambda site: own)
    p = linkedin_program()                           # own_feed is the default source
    asyncio.run(t.refill(LI, "like", p))
    likes = [t.next(LI, "like", p) for _ in range(3)]
    assert likes == ["https://www.linkedin.com/feed/update/urn:li:activity:7392000000000000001/",
                     "https://www.linkedin.com/feed/update/urn:li:activity:7392000000000000002/", None]
    # ...004 has no author link: nobody to judge, so it is never a target
    asyncio.run(t.refill(LI, "follow", p))
    follows = [t.next(LI, "follow", p) for _ in range(3)]
    assert follows == ["https://www.linkedin.com/in/mira-kestrel/", "https://www.linkedin.com/company/example-robotics/",
                       None]
    # a program with no source a read operation serves reads nothing
    other = linkedin_program([{"source": "hashtags", "values": ["robotics"]}])
    n = len(ops.calls)
    assert asyncio.run(t.refill(LI, "like", other)) is None and len(ops.calls) == n


def test_one_author_is_one_candidate_however_its_link_is_written():
    """The review's probe: one person's posts with ?miniProfileUrn=..., another case or a fragment
    are one author (one post per author per read); a post with no author is dropped; the member's
    own link with a query is still the member."""
    def answer(call):
        return OpResult(OpStatus.VERIFIED, output={"posts": [
            {"urn": "urn:li:activity:7012345678901", "author_url": "https://www.linkedin.com/in/alice-example/?miniProfileUrn=a"},
            {"urn": "urn:li:activity:7012345678902", "author_url": "https://www.linkedin.com/in/alice-example/"},
            {"urn": "urn:li:activity:7012345678905", "author_url": "https://www.linkedin.com/in/Alice-Example/#about"},
            {"urn": "urn:li:activity:7012345678903", "author_url": None},
            {"urn": "urn:li:activity:7012345678904", "author_url": "https://www.linkedin.com/in/mirakestreldemo/?trk=x"},
            {"urn": "urn:li:activity:7012345678906", "author_url": "https://www.linkedin.com/company/alice-example/"}]})
    t = ConnectorTargets(Ops(), FakeOperations({0: answer}), own=lambda site: {"mirakestreldemo"})
    p = linkedin_program()
    asyncio.run(t.refill(LI, "like", p))
    assert [t.next(LI, "like", p) for _ in range(3)] == [
        "https://www.linkedin.com/feed/update/urn:li:activity:7012345678901/",
        "https://www.linkedin.com/feed/update/urn:li:activity:7012345678906/", None]   # a company is not the person


def test_reddit_authors_compare_without_u_and_case():
    ops = FakeOperations({0: browse({"test": [("test", "ddddd1", "Someone"), ("test", "ddddd2", "u/someone"),
                                              ("test", "ddddd3", "u/MiraKestrelDemo"), ("test", "ddddd4", "other")]})})
    t = ConnectorTargets(Ops(), ops, own=lambda site: {"mirakestreldemo"})
    p = reddit_program("test")
    asyncio.run(t.refill(RD, "comment", p))
    assert [t.next(RD, "comment", p) for _ in range(3)] == [
        "https://www.reddit.com/r/test/comments/ddddd1/", "https://www.reddit.com/r/test/comments/ddddd4/", None]


# ---------------------------------------------------------------- through the executor


RD_COMMUNITIES = RD_YAML.replace("    daily: {comment: 1, post: 1, join: 1}",
                                 "    daily: {comment: 1, post: 1, join: 1}\n"
                                 "    targets: [{source: communities, values: [test, Vilnius]}]")


def _site(rig, answer=None):
    """FakeOperations: reads answer from browse_subreddit, every write verifies."""
    reader = answer or browse({"test": [(("test", f"t{n:05d}", f"user{n}")) for n in range(30)],
                               "Vilnius": [("Vilnius", f"v{n:05d}", f"local{n}") for n in range(30)]})

    def script(call):
        if call.flow_id.endswith("browse_subreddit"):
            return reader(call)
        return OpResult(OpStatus.VERIFIED)
    rig.ops.script = {i: script for i in range(500)}


def test_a_week_of_sessions_comments_on_distinct_posts_and_joins_each_community_once(tmp_path):
    rig = Rig(tmp_path, RD_COMMUNITIES, surface=RD, registry=everything_verified())
    targets = ConnectorTargets(rig.svc.store, rig.ops, own=lambda site: {"user0"}, clock=lambda: rig.t)
    rig.ex.targets = targets
    _site(rig)
    rig.run_days(MONDAY, 7)
    comments = [c for c in rig.ops.calls if c.flow_id == "connector.reddit.comment"]
    joins = [c for c in rig.ops.calls if c.flow_id == "connector.reddit.join"]
    reads = [c for c in rig.ops.calls if c.flow_id.endswith("browse_subreddit")]
    assert comments and reads
    posts = [(c.params["subreddit"], c.params["post_id"]) for c in comments]
    assert len(set(posts)) == len(posts) and ("test", "t00000") not in posts       # user0 is the owner's
    joined = [c.params["subreddit"] for c in joins]
    assert joined and len(set(joined)) == len(joined) and set(joined) <= {"test", "Vilnius"}
    assert all(r.ask is False and r.parent and r.parent.startswith("claim:engage:") for r in reads)
    ops = rig.svc.store.ops(RD)
    assert {o["reason"] for o in ops if o["kind"] == "comment" and o["status"] == "skipped"} <= {"cap_daily",
                                                                                                "cap_hourly", None}


def test_a_signal_while_reading_targets_ends_the_window_and_holds(tmp_path):
    rig = Rig(tmp_path, RD_COMMUNITIES, surface=RD, registry=everything_verified())
    rig.ex.targets = ConnectorTargets(rig.svc.store, rig.ops, clock=lambda: rig.t)
    _site(rig, lambda call: OpResult(OpStatus.FAILED, signal="captcha", error="signal_captcha"))
    day, w = _first_window(rig, ("comment",))
    rep = rig.run(day, w.idx)
    assert rep["status"] == "ended_early" and rep["end_reason"] == "signal: captcha"
    [op] = [o for o in rep["ops"] if o["status"] == "failed"]
    assert op["kind"] == "comment" and op["run_id"] == "run-0"
    [entry] = [h for h in rig.svc.store.history(RD) if h["kind"] == "op"]
    assert entry["payload"]["reason"] == "signal: captcha (reading targets)"
    assert any(h["signal"] == "captcha" for h in rig.svc.store.holds(RD))
    assert not [c for c in rig.ops.calls if c.flow_id == "connector.reddit.comment"]


def test_targets_are_deterministic_under_fakes(tmp_path):
    def week(home):
        rig = Rig(home, RD_COMMUNITIES, surface=RD, registry=everything_verified())
        rig.ex.targets = ConnectorTargets(rig.svc.store, rig.ops, clock=lambda: rig.t)
        _site(rig)
        rig.run_days(MONDAY, 3)
        return [(c.flow_id, c.params) for c in rig.ops.calls]
    assert week(tmp_path / "a") == week(tmp_path / "b")

