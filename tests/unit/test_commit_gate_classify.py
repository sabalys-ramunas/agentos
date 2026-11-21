"""The commit gate classifies fail closed (AUD-03, docs/design/egress-guard.md section 8): while an
agent drives, a GraphQL request is a read only when every operation in it is a known read, and a
non-GET to a rule host that no rule reads is held as a possible publish. A normal feed session
still triggers no gate. Fake requests and contexts (test_egress)."""

from __future__ import annotations

import json
from urllib.parse import quote, urlencode

import pytest
from test_commit_gate_guard import Gate
from test_egress import COMMITTER, FakeRequest, FakeRoute, make_guard

from openajar.node import egress
from openajar.node.egress import classify, gql_verdict

JSON = {"content-type": "application/json"}
FORM = {"content-type": "application/x-www-form-urlencoded"}
REDDIT = "https://www.reddit.com/svc/shreddit/graphql"
FB = "https://www.facebook.com/api/graphql/"
IG = "https://www.instagram.com/graphql/query"
LINKEDIN = "https://www.linkedin.com/flagship-web/rsc-action/actions/server-request"
MEDIUM = "https://medium.com/_/graphql"


def meta(op: str | None, *, header: bool = True, doc_id: str = "1") -> tuple[dict[str, str], bytes]:
    fields = {"av": "1", "fb_dtsg": "t", "variables": json.dumps({"input": {"message": {"text": "x"}}}), "doc_id": doc_id}
    if op:
        fields["fb_api_req_friendly_name"] = op
    return ({**FORM, "x-fb-friendly-name": op} if op and header else dict(FORM)), urlencode(fields).encode()


def reddit(op: str) -> bytes:
    return json.dumps({"operation": op, "variables": {"input": {}}, "csrf_token": "x"}).encode()


def linkedin(request_id: str) -> bytes:
    return json.dumps({"requestId": request_id, "serverRequest": {"requestId": request_id}}).encode()


@pytest.mark.parametrize(("site", "headers", "body", "why"), [
    # the review's probes: each went as a read before
    ("facebook", *meta(None), "unknown doc_id"),                                     # no friendly name
    ("instagram", *meta("PolarisPostCommentV2Mutation"), "unknown mutation"),         # a renamed comment
    ("facebook", FORM, b"queries=" + quote(json.dumps({"q0": {"doc_id": "1", "query_name": "ComposerStoryCreateMutation"}})).encode(),
     "unknown mutation"),                                                             # Meta's batch form
    ("reddit", JSON, json.dumps([{"operation": "ExposeVariant"}, {"operation": "Ping"}]).encode(),
     "unknown operation Ping in a batch"),                                            # one non-read in a batch
    ("reddit", JSON, reddit("CreatePostV2"), "unknown operation CreatePostV2"),
    ("medium", JSON, json.dumps({"operationName": "PublishPost", "variables": {}}).encode(),
     "unknown operation PublishPost"),                                                # a publish without its header
    ("medium", JSON, json.dumps({"operationName": "PostViewerQuery",
                                 "query": "query PostViewerQuery { a } mutation M { b }"}).encode(), "a mutation"),
    ("reddit", JSON, json.dumps({"variables": {}}).encode(), "no operation name"),
    ("reddit", JSON, b"not json", "cannot read"),
    ("linkedin", JSON, linkedin("com.linkedin.sdui.requests.sharing.CreatePostV2"), "unknown operation"),
    ("facebook", {**FORM, "x-fb-friendly-name": "CometNewsFeedPaginationQuery"},
     urlencode({"fb_api_req_friendly_name": "useCometUFICreateCommentMutation"}).encode(), "unknown mutation"),
])
def test_a_request_with_any_operation_not_known_as_a_read_is_not_a_read(site, headers, body, why):
    read, reason = gql_verdict(site, headers, body)
    assert not read and why in reason


@pytest.mark.parametrize(("site", "headers", "body"), [
    ("instagram", *meta("PolarisFeedRootPaginationCachedQuery_subscribe")),
    ("instagram", *meta("PolarisUserHoverCardContentV2Query", header=False)),
    ("facebook", *meta("CometNewsFeedPaginationQuery")),
    ("facebook", *meta("CometUFILiveTypingBroadcastMutation_StartMutation")),  # the canary's typing indicator
    ("reddit", JSON, reddit("ExposeVariant")),
    ("reddit", JSON, reddit("CreateCaptchaToken")),
    ("reddit", JSON, reddit("EvaluateCommentAutomationsByPostId")),
    ("reddit", JSON, json.dumps([{"operation": "ExposeVariant"}, {"operation": "AchievementUnlockedNotifications"}]).encode()),
    ("linkedin", JSON, linkedin("com.linkedin.sdui.requests.comments.courtesyReminder")),
    ("linkedin", JSON, linkedin("com.linkedin.sdui.feed.update.comments.fetchComment")),
    ("medium", JSON, json.dumps([{"operationName": "HomeFeed", "query": "query HomeFeed { feed { id } }"}]).encode()),
])
def test_known_reads_and_queries_are_reads(site, headers, body):
    read, _ = gql_verdict(site, headers, body)
    assert read


# A feed session as the canary's lanes made it: documents, scripts and images, the sites' GraphQL
# reads, their typing and captcha calls, and their telemetry. None of it may meet the gate.
FEED = [
    FakeRequest("https://www.instagram.com/", nav=True),
    FakeRequest("https://www.instagram.com/static/bundles/x.js"),
    FakeRequest(IG, "POST", headers=meta("PolarisFeedRootPaginationCachedQuery_subscribe")[0],
                body=meta("PolarisFeedRootPaginationCachedQuery_subscribe")[1]),
    FakeRequest(IG, "POST", headers=meta("PolarisUserHoverCardContentV2Query")[0],
                body=meta("PolarisUserHoverCardContentV2Query")[1]),
    FakeRequest("https://www.instagram.com/ajax/bz", "POST", headers=FORM, body=b"q=%5B%5D&ts=1"),
    FakeRequest("https://www.instagram.com/logging/falco", "POST", headers=FORM, body=b"x=1"),
    FakeRequest("https://www.facebook.com/", nav=True),
    FakeRequest(FB, "POST", headers=meta("CometNewsFeedPaginationQuery")[0], body=meta("CometNewsFeedPaginationQuery")[1]),
    FakeRequest(FB, "POST", headers=meta("CometUFILiveTypingBroadcastMutation_StartMutation")[0],
                body=meta("CometUFILiveTypingBroadcastMutation_StartMutation")[1]),
    FakeRequest("https://www.facebook.com/ajax/bulk-route-definitions/", "POST", headers=FORM, body=b"route_urls[0]=/x"),
    FakeRequest("https://www.facebook.com/ajax/bz", "POST", headers=FORM, body=b"q=1"),
    FakeRequest("https://www.reddit.com/r/lithuania/", nav=True),
    FakeRequest(REDDIT, "POST", headers=JSON, body=reddit("ExposeVariant")),
    FakeRequest(REDDIT, "POST", headers=JSON, body=reddit("CreateCaptchaToken")),
    FakeRequest(REDDIT, "POST", headers=JSON, body=reddit("EvaluateCommentAutomationsByPostId")),
    FakeRequest("https://www.reddit.com/svc/shreddit/events", "POST", headers=JSON, body=b"[]"),
    FakeRequest("https://w3-reporting.reddit.com/reports", "POST", headers=JSON, body=b"[]"),
    FakeRequest("https://www.linkedin.com/feed/", nav=True),
    FakeRequest(LINKEDIN, "POST", headers=JSON, body=linkedin("com.linkedin.sdui.requests.comments.courtesyReminder")),
    FakeRequest(LINKEDIN, "POST", headers=JSON, body=linkedin("com.linkedin.sdui.feed.update.comments.fetchComment")),
    FakeRequest("https://www.linkedin.com/li/track", "POST", headers=JSON, body=b"[]"),
]


async def test_a_normal_feed_session_triggers_no_gate(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    gate = Gate()
    async with guard.acting(gate=gate):
        [(_, handler)] = ctx.routes
        routes = []
        for request in FEED:
            ctx.emit("request", request)
            if egress.gate_pattern().match(request.url):
                route = FakeRoute()
                routes.append(route)
                await handler(route, request)
                assert route.continued == 1 and not route.aborted, request.url
    assert gate.seen == [] and guard.blocks_since(0) == []


async def test_an_unknown_mutation_a_put_and_a_delete_are_held(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    gate = Gate()
    held = [FakeRequest(IG, "POST", headers=meta("PolarisPostCommentV2Mutation")[0],
                        body=meta("PolarisPostCommentV2Mutation")[1]),
            FakeRequest("https://www.reddit.com/api/v1/me/prefs", "PATCH", headers=JSON, body=b'{"x": 1}'),
            FakeRequest("https://www.linkedin.com/voyager/api/x/123", "PUT", headers=JSON, body=b"{}"),
            FakeRequest("https://www.facebook.com/some/thing/9", "DELETE"),
            FakeRequest("https://www.reddit.com/submit-form", "POST", headers=FORM, body=b"title=x")]
    async with guard.acting(gate=gate):
        [(_, handler)] = ctx.routes
        for request in held:
            route = FakeRoute()
            await handler(route, request)
            assert route.aborted and not route.continued, request.url
    assert gate.seen == ["instagram.unknown", "reddit.unknown", "linkedin.unknown", "facebook.unknown",
                         "reddit.unknown"]
    rows = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert sum(1 for r in rows if r["kind"] == "gate_classified" and r["decision"] == "gated") == 5


async def test_unknown_posts_can_be_let_go_traced_but_a_put_never(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.gate_unknown_posts = False
    gate = Gate()
    async with guard.acting(gate=gate):
        [(_, handler)] = ctx.routes
        post, put = FakeRoute(), FakeRoute()
        await handler(post, FakeRequest("https://www.reddit.com/submit-form", "POST", headers=FORM, body=b"t=x"))
        await handler(put, FakeRequest("https://www.reddit.com/submit-form", "PUT", headers=FORM, body=b"t=x"))
    assert post.continued == 1 and put.aborted and gate.seen == ["reddit.unknown"]
    rows = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert [r["decision"] for r in rows if r["kind"] == "gate_classified"] == ["let go", "gated"]


def test_a_draft_grant_reads_the_catch_all_as_before():
    """The fail closed reading is the gate's, during an agent's drive: classify (the grant's
    ceiling, the listener) still takes an unknown operation on a GraphQL URL for a read."""
    headers, body = meta("PolarisPostCommentV2Mutation")
    assert classify(IG, "POST", headers, body=body).id == "instagram.read.graphql"
