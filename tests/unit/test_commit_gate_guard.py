"""The egress guard's side of the commit gate (AUD-03): the narrow route on the commit rules'
endpoints only while an agent acts with a gate, what goes to the gate and what never does,
settling for a publish that lags the press, uploads remembered for the media they become, and
unmatched publishes recorded. Fake requests and contexts (test_egress)."""

from __future__ import annotations

import asyncio
import json
import re
import time

import pytest
from test_egress import (
    COMMITTER,
    OWNER,
    FailingDispatcher,
    FakeFrame,
    FakeRequest,
    FakeRoute,
    make_guard,
    settle,
)
from testkit.timing import budget, slow_factor

from openajar.core.errors import EngineError
from openajar.node import egress
from openajar.node.egress import EFFECT_RULES, Refusal

# one URL each commit rule matches (the canary's real endpoints where there are some)
COMMIT_URLS = {
    "instagram.comment": "https://www.instagram.com/graphql/query",
    "instagram.post": "https://i.instagram.com/api/v1/media/configure_sidecar/",
    "instagram.comment.rest": "https://www.instagram.com/api/v1/web/comments/123456/add/",
    "facebook.post": "https://www.facebook.com/api/graphql/",
    "facebook.comment": "https://web.facebook.com/api/graphql/?q=1",
    "reddit.post": "https://www.reddit.com/svc/shreddit/graphql",
    "reddit.comment": "https://www.reddit.com/svc/shreddit/t1_abc123/create-comment",
    "reddit.post.oldreddit": "https://old.reddit.com/api/submit",
    "reddit.comment.oldreddit": "https://old.reddit.com/api/comment",
    "linkedin.post": "https://www.linkedin.com/flagship-web/rsc-action/actions/server-request",
    "linkedin.post.dispatch": "https://www.linkedin.com/flagship-web/rsc-action/actions/server-request",
    "linkedin.comment": "https://www.linkedin.com/flagship-web/rsc-action/actions/server-request",
    **{f"linkedin.profile.{f}": "https://www.linkedin.com/flagship-web/rsc-action/actions/server-request"
       for f in ("name", "headline", "about", "photo", "cover")},
    "linkedin.post.voyager": "https://www.linkedin.com/voyager/api/contentcreation/normShares",
    "linkedin.comment.voyager": "https://www.linkedin.com/voyager/api/feed/comments",
    "medium.post": "https://medium.com/_/graphql",
}
# the route covers the rule hosts whole (an unknown publish endpoint, a redirect's first hop), never other hosts
ROUTED = ("https://www.reddit.com/r/test/", "https://upload.facebook.com/ajax/react_composer/attachments/photo/upload",
          "https://www.linkedin.com/dms-uploads/abc", "https://i.instagram.com/rupload_igphoto/x",
          "https://www.reddit.com/svc/shreddit/graphql-other")
NOT_ROUTED = ("https://evil.example/svc/shreddit/graphql", "https://static.xx.fbcdn.net/rsrc.php/v3/x.js",
              "https://www.reddit.com.evil.example/svc/shreddit/graphql", "https://evil.example/www.reddit.com/",
              "https://www.redditstatic.com/shreddit/x.js", "https://scontent.cdninstagram.com/v/x.jpg")
CONFIGURE = "https://i.instagram.com/api/v1/media/configure/"


def test_the_narrow_route_covers_every_commit_rule_and_little_else():
    pattern = egress.gate_pattern()
    assert {r.id for r in EFFECT_RULES if r.effect == "commit"} == set(COMMIT_URLS)
    for rule_id, url in COMMIT_URLS.items():
        assert pattern.match(url), rule_id
    for url in ROUTED:
        assert pattern.match(url), url
    for url in NOT_ROUTED:
        assert not pattern.match(url), url
    # only syntax Python and JavaScript read alike: no inline flags, named groups or flags the driver lacks
    assert "(?i)" not in pattern.pattern and "(?P" not in pattern.pattern
    assert pattern.flags & ~re.UNICODE == 0


REDDIT_POST = json.dumps({"operation": "CreatePost", "variables": {"input": {"title": "t"}}}).encode()


@pytest.mark.parametrize("url", [
    "https://www.reddit.com./svc/shreddit/graphql",             # a trailing dot
    "https://u:p@www.reddit.com/svc/shreddit/graphql",          # userinfo
    "https://WWW.Reddit.COM/svc/shreddit/graphql",              # case
    "HTTPS://www.reddit.com/svc/shreddit/graphql",
    "https://\uff57\uff57\uff57.reddit.com/svc/shreddit/graphql",  # fullwidth letters (IDNA maps them)
    "https://www.reddit%2Ecom/svc/shreddit/graphql",            # a percent-encoded host
    "https://www.reddit.com:443/svc/shreddit/graphql",          # an explicit default port
    "https://www.reddit.com:8443/svc/shreddit/graphql",
    "https://www.reddit.com/svc/./shreddit/%67raphql",          # a dot segment and an encoded letter
])
def test_the_route_and_the_classifier_normalize_a_host_alike(url):
    rule = egress.classify(url, "POST", {"content-type": "application/json"}, body=REDDIT_POST)
    assert rule is not None and rule.id == "reddit.post"
    assert egress.gate_pattern().match(url)


def test_a_lookalike_host_is_neither_classified_nor_routed():
    for url in ("https://www.reddit.com.evil.example/svc/shreddit/graphql",
                "https://www.reddit.com@evil.example/svc/shreddit/graphql",
                "https://www.redd\u0131t.com/svc/shreddit/graphql"):  # a dotless i: another host (punycode)
        assert egress.classify(url, "POST", {"content-type": "application/json"}, body=REDDIT_POST) is None
    assert not egress.gate_pattern().match("https://www.reddit.com@evil.example/svc/shreddit/graphql")
    # a host with a non-ASCII letter is routed (a superset) and the classifier reads it normalized
    kelvin = "https://www.lin\u212aedin.com.evil.example/flagship-web/rsc-action/actions/server-request"
    assert egress.gate_pattern().match(kelvin) and egress.host_of(kelvin) == "www.linkedin.com.evil.example"
    assert egress.classify(kelvin, "POST") is None


def test_a_publish_sent_with_another_unsafe_method_is_still_the_publish():
    assert egress.classify("https://www.reddit.com/svc/shreddit/graphql", "PUT", {"content-type": "application/json"},
                           body=REDDIT_POST).id == "reddit.post"
    # a read rule keeps its own method: a PUT is never a read because a POST to that URL is
    assert egress.classify("https://medium.com/_/graphql", "PUT") is None


class Gate:
    """A PublishGate that holds everything (or lets it go), and says what it saw."""

    def __init__(self, *, allow: bool = False, fail: bool = False):
        self.allow, self.fail = allow, fail
        self.submit_like = True
        self.done = False
        self.seen: list[str] = []

    async def hold(self, rule, request, url, method):
        self.done = True
        self.seen.append(rule.id)
        if self.fail:
            raise RuntimeError("boom")
        return None if self.allow else Refusal("commit_gate", f"this publishes ({rule.id}) and waits")


async def test_the_gate_route_is_bound_for_the_agents_drive(tmp_path):
    """From the first gated act until control leaves the agent: between acts a publish is held too."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    gate = Gate()
    async with guard.acting():
        assert ctx.routes == []  # no gate: as before
    async with guard.acting(gate=gate) as mark:
        [(pattern, handler)] = ctx.routes
        assert pattern is guard.gate_route()
        held, read = FakeRoute(), FakeRoute()
        await handler(held, FakeRequest(CONFIGURE, "POST"))
        await handler(read, FakeRequest("https://www.instagram.com/graphql/query", "POST",
                                        headers={"x-fb-friendly-name": "PolarisFeedQuery"}))
        ctx.emit("request", FakeRequest(CONFIGURE, "POST"))  # the listener leaves it to the route
    assert held.aborted == "blockedbyclient" and read.continued == 1 and gate.seen == ["instagram.post"]
    [block] = guard.blocks_since(mark)
    assert (block.rule, block.prevented, block.effect_rule) == ("commit_gate", True, "instagram.post")
    # the act is over and the route stays: a page's timer publishing a minute later meets the gate
    assert [p for p, _ in ctx.routes] == [guard.gate_route()] and guard.driving()
    later = FakeRoute()
    await handler(later, FakeRequest(CONFIGURE, "POST"))
    assert later.aborted and gate.seen == ["instagram.post", "instagram.post"]
    # a second act binds its own gate to the same route
    second = Gate()
    async with guard.acting(gate=second):
        assert len(ctx.routes) == 1
    await handler(FakeRoute(), FakeRequest(CONFIGURE, "POST"))
    assert second.seen == ["instagram.post"]
    guard.end_drive("a person took over")  # control left the agent: the route goes, the cache works again
    await asyncio.sleep(0)
    assert ctx.routes == [] and not guard.driving()
    guard.drive(OWNER)
    async with guard.acting(gate=gate):
        assert ctx.routes == []  # a person drives: never gated
    guard.drive(COMMITTER)
    guard.human_in_control = lambda: True
    async with guard.acting(gate=gate):
        assert ctx.routes == []


async def test_a_person_driving_ends_the_drive(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    async with guard.acting(gate=Gate()):
        pass
    assert guard.driving()
    guard.drive(OWNER)
    await asyncio.sleep(0)
    assert ctx.routes == [] and not guard.driving()


async def test_a_person_in_control_during_the_drive_is_never_gated_charged_or_aborted(tmp_path):
    """The review's probe 12: the route is still on when a person takes over (before the lane
    manager ends the drive); their post goes, uncharged."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)

    class Meter:
        charged = 0

        def charge(self, rule, *, sent):
            Meter.charged += 1
            return "this lane is past its commit limit for the hour"

        def capped(self):
            return False

    gate = Gate()
    async with guard.acting(gate=gate, meter=Meter()):
        pass
    guard.human_in_control = lambda: True
    route = FakeRoute()
    await ctx.routes[0][1](route, FakeRequest(CONFIGURE, "POST"))
    ctx.emit("request", FakeRequest(CONFIGURE, "POST"))
    assert route.continued == 1 and not route.aborted and gate.seen == [] and Meter.charged == 0
    assert guard.blocks_since(0) == []


async def test_a_strict_lane_uses_its_full_route_and_the_gate_still_judges_publishes(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, driver=COMMITTER, strict=True)
    gate = Gate()
    async with guard.acting(gate=gate):
        assert {p if isinstance(p, str) else "gate" for p, _ in ctx.routes} == {"**/*", "gate"}
        route = FakeRoute()
        await ctx.routes[0][1](route, FakeRequest(CONFIGURE, "POST"))
    assert route.aborted and gate.seen == ["instagram.post"]  # one handler: the gate is asked once
    assert [p for p, _ in ctx.routes] == [guard.gate_route()]  # the strict route was the act's only


async def test_a_gate_that_fails_refuses_and_one_that_allows_lets_it_go_uncharged(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)

    class Meter:
        def __init__(self):
            self.charged: list[str] = []

        def charge(self, rule, *, sent):
            self.charged.append(rule.id)

        def capped(self):
            return False

    meter = Meter()
    async with guard.acting(gate=Gate(fail=True), meter=meter):
        route = FakeRoute()
        await ctx.routes[0][1](route, FakeRequest(CONFIGURE, "POST"))
    assert route.aborted and guard.blocks_since(0)[-1].rule == "commit_gate"
    async with guard.acting(gate=Gate(allow=True), meter=meter):
        route = FakeRoute()
        await ctx.routes[0][1](route, FakeRequest(CONFIGURE, "POST"))
    assert route.continued == 1 and meter.charged == []  # the gate's authorize metered it as a commit
    rows = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert rows[-1]["kind"] == "observed_commit" and rows[-1]["effect_rule"] == "instagram.post"


async def test_a_draft_grant_is_refused_by_the_rule_before_the_gate_is_asked(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=())  # AGENT: a draft ceiling
    gate = Gate()
    async with guard.acting(gate=gate) as mark:
        route = FakeRoute()
        await ctx.routes[0][1](route, FakeRequest(CONFIGURE, "POST"))
    assert route.aborted and gate.seen == [] and guard.blocks_since(mark)[0].rule == "commit"


async def test_settle_waits_for_a_publish_that_lags_the_press(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.settle_quiet_s, guard.settle_max_s = 0.3, 10.0  # the max far from the quiet end (about 0.6 s)
    gate = Gate()
    async with guard.acting(gate=gate):
        reload = FakeRequest("https://www.google.com/recaptcha/enterprise/reload", "POST")
        ctx.emit("request", reload)
        ctx.emit("requestfinished", reload)

        async def captcha_then_publish():
            await asyncio.sleep(0.15)
            clr = FakeRequest("https://www.google.com/recaptcha/enterprise/clr", "POST")
            ctx.emit("request", clr)
            ctx.emit("requestfinished", clr)
            await asyncio.sleep(0.15)
            await ctx.routes[0][1](FakeRoute(), FakeRequest(CONFIGURE, "POST"))

        task = asyncio.create_task(captcha_then_publish())
        started = time.monotonic()
        await guard.settle(gate)
        await task
    assert gate.seen == ["instagram.post"] and time.monotonic() - started < budget(1.0)  # ended at quiet, not the max
    quiet = Gate()
    async with guard.acting(gate=quiet):
        started = time.monotonic()
        await guard.settle(quiet)  # nothing but GETs: only the grace for late request events
    # the far alternative is the cold bound (2.5 s): half a second of slack (budget: more on a slow runner)
    assert guard.settle_grace_s <= time.monotonic() - started < guard.settle_grace_s + budget(0.5)


async def test_a_cold_browser_first_click_is_reported_by_its_own_act(tmp_path):
    """Measured (scripts/spikes/commit_gate.py `first`): Camoufox's content blocker holds a
    page's requests for about 1.5 s after launch, so a cold first click's publish reached the
    driver 1.2 s after the press, past the grace, and the act said ok. Until the browser has told
    of a fetch, the press waits for the held requests (at most settle_cold_s), then the grace."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.settle_quiet_s, guard.settle_cold_s = 0.2, 5.0  # the cold bound far from the grace, for the last check
    gate = Gate()
    async with guard.acting(gate=gate):
        async def held_then_released():
            await asyncio.sleep(0.8)  # past the grace: the blocker's lists loaded, the held request comes
            post = FakeRequest(CONFIGURE, "POST")
            ctx.emit("request", post)
            await ctx.routes[0][1](FakeRoute(), post)
            ctx.emit("requestfailed", post)

        task = asyncio.create_task(held_then_released())
        await guard.settle(gate)
        await task
    assert gate.seen == ["instagram.post"]  # judged inside the act: browser_act says approval_pending

    fresh, _, _, _ = make_guard(tmp_path / "b", sites=(), driver=COMMITTER)
    fresh.settle_cold_s, fresh.settle_grace_s = 0.6, 3.0  # a long grace: cold alone (0.6) vs cold + grace (3.6)
    quiet = Gate()
    async with fresh.acting(gate=quiet):
        started = time.monotonic()
        await fresh.settle(quiet)  # nothing comes: bounded by settle_cold_s, no grace after it
    assert 0.6 <= time.monotonic() - started < 0.6 + budget(0.8)  # below cold + grace even on a slow runner

    async with guard.acting(gate=(warm := Gate())):  # the browser told of a fetch: the usual grace only
        started = time.monotonic()
        await guard.settle(warm)
    assert time.monotonic() - started < guard.settle_grace_s + budget(0.5)  # not the cold bound (5.0)


async def test_the_cold_wait_is_paid_once_a_browser(tmp_path):
    """A page that never fetches would leave the browser cold for good: every act would wait the
    whole cold bound. Only the first press of a young browser waits; later ones, and the first
    press of a browser older than settle_cold_age_s, wait the usual grace."""
    guard, _, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    # cold 1.5 s and grace 0.5 s keep every case 0.4 s or more from its neighbour (cold + grace is
    # 2.0, the cold bound 1.5, the grace 0.5), so a loaded runner does not move one into the next. On a
    # runner CI marks slow (OPENAJAR_TEST_SLOW) every time here is that much longer, the gaps too.
    k = slow_factor()
    guard.settle_cold_s, guard.settle_grace_s = 1.5 * k, 0.5 * k
    for expected, below in ((1.5 * k, 1.9 * k), (0.5 * k, 1.2 * k), (0.5 * k, 1.2 * k)):
        async with guard.acting(gate=(gate := Gate())):
            started = time.monotonic()
            await guard.settle(gate)
        assert expected <= time.monotonic() - started < below

    old, _, _, _ = make_guard(tmp_path / "b", sites=(), driver=COMMITTER)
    old.settle_cold_s, old._attached_at = 5.0, time.monotonic() - old.settle_cold_age_s
    async with old.acting(gate=(gate := Gate())):
        started = time.monotonic()
        await old.settle(gate)
    assert time.monotonic() - started < old.settle_grace_s + budget(0.7)  # not the cold bound (5.0)


async def test_a_held_request_ends_the_cold_wait_early(tmp_path):
    """Any fetch the browser tells of means its requests are no longer held: the press waits the
    usual grace from then on, not the whole cold bound."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.settle_cold_s = 5.0
    gate = Gate()
    async with guard.acting(gate=gate):
        async def a_read_arrives():
            await asyncio.sleep(0.3)
            ctx.emit("request", FakeRequest("https://www.instagram.com/api/v1/feed/", "GET"))

        task = asyncio.create_task(a_read_arrives())
        started = time.monotonic()
        await guard.settle(gate)
        await task
    # ended early: far below the whole cold bound (5.0)
    assert 0.3 + guard.settle_grace_s - 0.05 <= time.monotonic() - started < 0.3 + guard.settle_grace_s + budget(0.8)


async def test_settle_waits_for_a_non_get_still_in_flight(tmp_path):
    """The review's probe 10: an upload starts on the press and the post follows when it ends,
    seconds later. The act waits for the upload, then for quiet, so the post is the act's."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.settle_quiet_s, guard.settle_max_s = 0.2, 5.0
    gate = Gate()
    async with guard.acting(gate=gate):
        upload = FakeRequest("https://www.linkedin.com/dms-uploads/abc123", "PUT", body=b"PNG" * 10)
        ctx.emit("request", upload)

        async def upload_then_post():
            await asyncio.sleep(0.6)  # longer than the quiet: only the open upload keeps the act waiting
            ctx.emit("requestfinished", upload)
            await asyncio.sleep(0.05)
            await ctx.routes[0][1](FakeRoute(), FakeRequest(CONFIGURE, "POST"))

        task = asyncio.create_task(upload_then_post())
        await guard.settle(gate)
        assert gate.seen == ["instagram.post"]  # judged inside the act, so the tool reports it
        await task


async def test_a_request_the_driver_hears_of_after_the_act_is_still_the_acts(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    gate = Gate()
    async with guard.acting(gate=gate):
        [(_, handler)] = ctx.routes
    late = FakeRoute()  # the browser paused it during the act; the driver hands it over now
    await handler(late, FakeRequest(CONFIGURE, "POST"))
    assert late.aborted and gate.seen == ["instagram.post"]


async def test_an_upload_is_remembered_with_the_ids_the_site_answers(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)

    class Response:
        async def text(self):
            return 'for (;;);{"payload":{"photoID":"122093066475549202"}}'

    request = FakeRequest("https://upload.facebook.com/ajax/react_composer/attachments/photo/upload", "POST",
                          headers={"content-type": "multipart/form-data; boundary=b"},
                          body=b'--b\r\nContent-Disposition: form-data; name="source"; filename="blob"\r\n\r\n'
                               b'PHOTO\r\n--b--\r\n')

    async def response():
        return Response()

    request.response = response
    ctx.emit("request", request)
    for _ in range(5):
        await asyncio.sleep(0)
    [up] = guard.uploads
    assert up.size == 5 and "122093066475549202" in up.ids and up.rule == "facebook.draft.upload"


async def test_an_unknown_non_get_to_the_site_is_held_and_traced_by_its_shape(tmp_path):
    """No rule reads it, so it may publish: it is held as `<site>.unknown`, and the trace says how
    it was classified by its shape (names only), so a rule can be added."""
    guard, ctx, _, _ = make_guard(tmp_path, driver=COMMITTER)
    gate = Gate()
    body = json.dumps({"operation": "CreateStoryV9", "variables": {"input": {"text": "secret words"}}}).encode()
    async with guard.acting(gate=gate):
        route = FakeRoute()
        request = FakeRequest("https://www.instagram.com/api/v2/stories/create", "POST",
                              headers={"content-type": "application/json"}, body=body)
        ctx.emit("request", request)  # the listener leaves it to the route
        await ctx.routes[0][1](route, request)
    assert route.aborted and gate.seen == ["instagram.unknown"]
    rows = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    [row] = [r for r in rows if r["kind"] == "gate_classified"]
    assert row["host"] == "www.instagram.com" and row["shape"]["op"] == "CreateStoryV9" and row["decision"] == "gated"
    assert "secret words" not in json.dumps(row)


async def test_an_act_whose_gate_route_cannot_be_bound_is_refused_before_it_runs(tmp_path):
    """Fail closed: without the drive's route the commit gate holds nothing, so the act never starts
    (the listener and check_action alone are not a gate)."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.dispatcher = FailingDispatcher()
    ran = []
    with pytest.raises(EngineError, match="route"):
        async with guard.acting(gate=Gate()):
            ran.append(True)
    assert ran == [] and not guard.driving() and ctx.routes == []


async def test_an_act_on_a_lane_with_no_route_dispatcher_is_refused(tmp_path):
    guard, _, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.dispatcher = None
    ran = []
    with pytest.raises(EngineError, match="route"):
        async with guard.acting(gate=Gate()):
            ran.append(True)
    assert ran == []


async def test_a_strict_lane_whose_route_cannot_be_bound_refuses_the_act(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER, strict=True)
    guard.dispatcher = FailingDispatcher()
    ran = []
    with pytest.raises(EngineError, match="route"):
        async with guard.acting():  # no gate: the strict route alone must be on
            ran.append(True)
    assert ran == [] and ctx.routes == []


async def test_an_act_without_a_route_runs_on_a_lane_whose_dispatcher_is_broken(tmp_path):
    guard, _, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.dispatcher = FailingDispatcher()
    async with guard.acting(route=False, gate=Gate()):
        pass  # a wait or a read: nothing it does needs the route


async def test_a_publish_that_arrives_by_a_redirect_hop_stops_the_navigation_and_holds_the_lane(tmp_path):
    """A redirect hop is never routed, so a commit that arrives by one during a drive has left: the
    listener stops the navigation it carried (as it stops any refused one) and the lane is held."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    violations = []
    guard.on_violation = violations.append
    async with guard.acting(gate=Gate()) as mark:
        pass
    frame = FakeFrame("https://www.reddit.com/")
    first = FakeRequest("https://www.reddit.com/r/test/submit", "POST", nav=True, frame=frame)
    hop = FakeRequest(CONFIGURE, "POST", nav=True, frame=frame, hop=first)
    ctx.emit("request", hop)
    await settle()
    assert "window.stop()" in frame.evaluated
    assert [(v.rule, v.prevented, v.kind) for v in violations] == [("commit_gate", False, "redirect")]
    [block] = guard.blocks_since(mark)
    assert (block.rule, block.prevented, block.effect_rule) == ("commit_gate", False, "instagram.post")
    assert guard.stats["unblocked"] == 1
