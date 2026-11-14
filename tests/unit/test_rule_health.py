"""Effect rule health (agentos.node.rulehealth, docs/design/egress-guard.md 8.12): the trace rows a
lane's guard writes for it (rule_hit, unruled_request), the report built from the traces (when each
rule last matched, the shapes no rule names, the drift flags), and where the owner sees it (agentos
lane rules, agentos doctor, GET /api/rules/health, the site list's note)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount
from starlette.testclient import TestClient
from test_egress import COMMITTER, OWNER, FakeRequest, make_guard

from agentos.core.config import NodeConfig
from agentos.core.models import LaneSpec
from agentos.node import egress, gateshapes, rulehealth
from agentos.node.viewer_app import rule_health_api_routes

SECRET = "my private words 42"
EM_DASH = "—"
DAY = 86400.0
T0 = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
JSON = {"content-type": "application/json"}


def at(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def gql(op: str) -> bytes:
    return json.dumps({"operationName": op, "variables": {"input": {"text": SECRET}}}).encode()


# ---- what the guard traces ----------------------------------------------------------------------

def test_a_write_rule_match_leaves_one_rule_hit_row_a_second(tmp_path):
    guard, ctx, _events, clock = make_guard(tmp_path, driver=COMMITTER)
    like = FakeRequest("https://www.reddit.com/svc/shreddit/graphql", "POST", headers=JSON,
                       body=gql("UpdatePostVoteState"))
    ctx.emit("request", like)
    ctx.emit("request", like)  # the same rule within a second: one row
    clock.now += 2
    ctx.emit("request", like)
    rows = [r for r in guard.trace.read() if r["kind"] == "rule_hit"]
    assert len(rows) == 2 and rows[0]["rule"] == "reddit.like.post" and rows[0]["effect"] == "write"
    assert (rows[0]["host"], rows[0]["path"], rows[0]["method"]) == ("www.reddit.com", "/svc/shreddit/graphql", "POST")
    assert SECRET not in (tmp_path / "trace.jsonl").read_text()


def test_a_non_get_to_a_lane_host_no_rule_names_is_traced_by_shape(tmp_path):
    guard, ctx, _events, clock = make_guard(tmp_path, sites=("x.com", "*.x.com", "accounts.google.com",
                                                             "frame:www.google.com"), driver=COMMITTER)
    tweet = FakeRequest("https://x.com/i/api/graphql/abc/CreateTweet?t=t0k3n", "POST", headers=JSON,
                        body=json.dumps({"queryId": "abc", "variables": {"tweet_text": SECRET}}).encode())
    ctx.emit("request", tweet)
    ctx.emit("request", tweet)  # the same shape within a minute: one row
    ctx.emit("request", FakeRequest("https://accounts.google.com/signin", "POST", headers=JSON, body=b"{}"))
    ctx.emit("request", FakeRequest("https://x.com/home", "GET"))
    [row] = guard.trace.read()
    assert row["kind"] == "unruled_request" and row["host"] == "x.com" and row["flow"] is False
    assert row["path"] == "/i/api/graphql/abc/CreateTweet" and "variables.tweet_text" in row["shape"]["fields"]
    text = (tmp_path / "trace.jsonl").read_text()
    assert SECRET not in text and "t0k3n" not in text
    assert guard.blocks_since(0) == []  # it went: unknown requests are never blocked there
    clock.now += egress.UNRULED_EVERY_S
    ctx.emit("request", tweet)
    assert len(guard.trace.read()) == 2


def test_a_person_in_control_and_a_lane_with_no_list_leave_no_unruled_row(tmp_path):
    guard, ctx, _events, _clock = make_guard(tmp_path, sites=("x.com",), driver=OWNER)
    guard.human_in_control = lambda: True
    ctx.emit("request", FakeRequest("https://x.com/i/api/1.1/statuses/update.json", "POST", headers=JSON, body=b"{}"))
    assert guard.trace.read() == []
    other, ctx2, _e, _c = make_guard(tmp_path / "b", sites=(), driver=COMMITTER)
    (tmp_path / "b").mkdir()
    ctx2.emit("request", FakeRequest("https://x.com/i/api/1.1/statuses/update.json", "POST", headers=JSON, body=b"{}"))
    assert other.trace.read() == []


def test_a_rule_host_is_never_unruled(tmp_path):
    guard, ctx, _events, _clock = make_guard(tmp_path, driver=COMMITTER)  # instagram's list
    ctx.emit("request", FakeRequest("https://www.instagram.com/ajax/something", "POST", headers=JSON, body=b"{}"))
    assert [r for r in guard.trace.read() if r["kind"] == "unruled_request"] == []


# ---- the report -------------------------------------------------------------------------------

def classified(ts: float, op: str | None, *, host="www.reddit.com", path="/svc/shreddit/graphql",
               decision="gated", lane="biz-a/reddit") -> dict:
    shape = {"type": "application/json", "fields": ["variables.input.text"]}
    if op:
        shape["op"] = op
    return {"at": at(ts), "kind": "gate_classified", "lane": lane, "host": host, "path": path, "method": "POST",
            "decision": decision, "why": f"an unknown operation {op}", "rule": "reddit.read.graphql", "shape": shape}


def blocked(ts: float, rule: str, lane="biz-a/reddit") -> dict:
    return {"at": at(ts), "kind": "blocked", "lane": lane, "rule": "commit_gate", "host": "www.reddit.com",
            "path": "/svc/shreddit/t3_x/create-comment", "method": "POST", "effect_rule": rule}


def test_a_verified_rule_quiet_since_a_shape_of_its_kind_appeared_is_flagged():
    rows = [blocked(T0, "reddit.comment"), classified(T0 + DAY, "CreateCommentV2"),
            classified(T0 + DAY, "FetchGoldBalance")]
    done = rulehealth.report({"biz-a/reddit": rows}, now=T0 + 2 * DAY)
    rule = next(r for r in done["rules"] if r["id"] == "reddit.comment")
    assert rule["count"] == 1 and rule["last"] == at(T0) and rule["lanes"] == ["biz-a/reddit"]
    [flag] = done["flags"]
    sid = gateshapes.shape_id("www.reddit.com", "/svc/shreddit/graphql", "POST", "CreateCommentV2")
    assert flag["kind"] == "stale_rule" and flag["rule"] == "reddit.comment" and flag["shapes"] == [sid]
    assert "CreateCommentV2" in flag["message"] and "a comment" in flag["message"]
    assert f"agentos lane gate-log biz-a/reddit --as-commit comment.new --shape {sid}" in flag["message"]
    gold = next(e for e in done["unknown"] if e["op"] == "FetchGoldBalance")
    assert gold["write_like"] is False and gold["kind"] is None  # a read no rule names: listed, never a flag
    assert EM_DASH not in json.dumps(done)


def test_a_shape_seen_before_the_rule_last_matched_is_not_its_replacement():
    rows = [classified(T0, "CreateCommentDraft"), blocked(T0 + DAY, "reddit.comment")]
    assert rulehealth.report({"biz-a/reddit": rows}, now=T0 + 2 * DAY)["flags"] == []


def test_a_long_quiet_rule_is_flagged_for_any_unknown_write_and_a_short_one_is_not():
    hit = {"at": at(T0), "kind": "rule_hit", "lane": "biz-a/reddit", "rule": "reddit.join", "effect": "write",
           "host": "www.reddit.com", "path": "/svc/shreddit/graphql", "method": "POST"}
    rows = [hit, classified(T0 + DAY, "SaveSomethingNew")]
    assert rulehealth.report({"biz-a/reddit": rows}, now=T0 + 5 * DAY)["flags"] == []
    [flag] = rulehealth.report({"biz-a/reddit": rows}, now=T0 + 20 * DAY)["flags"]
    assert flag["kind"] == "stale_rule" and flag["rule"] == "reddit.join" and "looks like writes" in flag["message"]


def test_rules_of_one_kind_flagged_by_the_same_shapes_are_one_flag():
    rows = [classified(T0 + DAY, "UpdatePostVoteStateV2")]
    [flag] = rulehealth.report({"biz-a/reddit": rows}, now=T0 + 2 * DAY)["flags"]
    assert flag["rules"] == ["reddit.like.post", "reddit.like.comment"]
    assert "never matched in what the traces keep" in flag["message"]


def test_unverified_rules_and_promoted_shapes_are_never_flagged():
    rows = [classified(T0 + DAY, "CreateCommentV2"), classified(T0 + DAY, "SubmitPost", host="medium.com",
                                                                path="/_/graphql")]
    sid = gateshapes.shape_id("www.reddit.com", "/svc/shreddit/graphql", "POST", "CreateCommentV2")
    promoted = [{"id": sid, "as": "commit", "rule": "reddit.comment.v2", "site": "reddit", "host": "www.reddit.com",
                 "by": "owner:cli", "at": at(T0)}]
    done = rulehealth.report({"biz-a/reddit": rows}, promoted=promoted, now=T0 + 2 * DAY)
    assert done["flags"] == []  # medium.post is unverified; the comment shape is a rule now
    owner = next(r for r in done["rules"] if r["id"] == "reddit.comment.v2")
    assert owner["owner"] is True and owner["kind"] == "comment"


def test_a_burst_of_new_unknown_writes_on_a_site_is_flagged():
    rows = [classified(T0, op, host="www.facebook.com", path="/api/graphql/", lane="biz-a/facebook")
            for op in ("CometSaveThingMutation", "CometEditThingMutation", "CometPinThingMutation")]
    [flag] = rulehealth.report({"biz-a/facebook": rows}, now=T0 + 3600)["flags"]
    assert flag["kind"] == "new_unknowns" and flag["site"] == "facebook" and len(flag["shapes"]) == 3
    assert rulehealth.report({"biz-a/facebook": rows}, now=T0 + 3 * DAY)["flags"] == []  # not new any more


def test_observe_mode_rows_count_as_hits_and_unknown_reads_are_told_apart():
    rows = [{"at": at(T0), "kind": "observed_request", "lane": "biz-a/instagram", "host": "www.instagram.com",
             "path": "/api/graphql", "method": "POST", "effect_rule": "instagram.like", "effect": "write",
             "shape": {"op": "usePolarisLikeMediaXIGLikeMutation"}},
            {"at": at(T0), "kind": "observed_request", "lane": "biz-a/instagram", "host": "www.instagram.com",
             "path": "/api/graphql", "method": "POST", "effect_rule": "instagram.read.graphql", "effect": "read",
             "shape": {"op": "PolarisFeedTimelineQuery"}},
            {"at": at(T0), "kind": "observed_request", "lane": "biz-a/instagram", "host": "www.instagram.com",
             "path": "/api/graphql", "method": "POST", "effect_rule": "instagram.read.graphql", "effect": "read",
             "shape": {"op": "PolarisPostCommentNewMutation"}}]
    done = rulehealth.report({"biz-a/instagram": rows}, now=T0 + DAY)
    assert next(r for r in done["rules"] if r["id"] == "instagram.like")["count"] == 1
    assert [e["op"] for e in done["unknown"]] == ["PolarisPostCommentNewMutation"]
    [flag] = done["flags"]
    assert flag["rule"] == "instagram.comment" and done["unknown"][0]["sources"] == {"observed": 1}


def test_a_lane_on_a_site_with_no_rules_is_flagged():
    x = LaneSpec(id="biz-a/x", sites=["x.com", "*.x.com", "accounts.google.com", "frame:www.google.com"])
    reddit = LaneSpec(id="biz-a/reddit", sites=list(egress.SUGGESTED_SITES["reddit"]))
    bsky = LaneSpec(id="biz-a/bluesky", sites=["bsky.app"])
    named = LaneSpec(id="biz-b/twitter")  # no list: the site it is expected to be signed into
    unruled = {"at": at(T0), "kind": "unruled_request", "lane": "biz-a/x", "host": "x.com",
               "path": "/i/api/graphql/abc/CreateTweet", "method": "POST", "shape": {"fields": ["queryId"]}}
    done = rulehealth.report({"biz-a/x": [unruled]}, specs=[x, reddit, bsky, named], now=T0 + DAY)
    flags = {f["lanes"][0]: f for f in done["flags"] if f["kind"] == "no_rules"}
    assert set(flags) == {"biz-a/x", "biz-a/bluesky", "biz-b/twitter"}
    assert "no effect rules for X (x.com, *.x.com), so" in flags["biz-a/x"]["message"]
    assert "1 request shape no rule reads seen there" in flags["biz-a/x"]["message"]
    assert "for X," in flags["biz-b/twitter"]["message"] and "bsky.app" in flags["biz-a/bluesky"]["message"]
    tweet = next(e for e in done["unknown"] if e["site"] == "x")
    assert tweet["write_like"] and tweet["kind"] == "post" and tweet["decisions"] == {"let go": 1}
    assert EM_DASH not in json.dumps(done)


def test_write_words_and_kinds():
    assert rulehealth.words("usePolarisLikeMediaXIGLikeMutation") == ["use", "polari", "like", "media", "xig",
                                                                      "like", "mutation"]
    assert rulehealth.kind_of("PolarisPostCommentInputRevampedMutation", "/") == "comment"
    assert rulehealth.kind_of("UpdatePostVoteState", "/") == "like"
    assert rulehealth.kind_of("UpdateSubredditSubscriptions", "/") == "join"
    assert rulehealth.kind_of(None, "/svc/shreddit/t3_x/create-comment") == "comment"
    assert not rulehealth.write_like("POST", "ProfilePageQuery", "/api/graphql")
    assert not rulehealth.write_like("POST", None, "/ajax/bz")
    assert rulehealth.write_like("DELETE", None, "/anything")


# ---- where the owner sees it --------------------------------------------------------------------

def _node_with_trace(tmp_path, rows):
    cfg = NodeConfig(home=tmp_path)
    lane_dir = cfg.lane_dir("biz-a/reddit")
    lane_dir.mkdir(parents=True)
    trace = egress.EgressTrace(lane_dir / egress.TRACE_NAME)
    for row in rows:
        trace.write(row)
    return cfg


def test_for_node_reads_every_lanes_trace(tmp_path):
    cfg = _node_with_trace(tmp_path, [blocked(T0, "reddit.comment")])
    done = rulehealth.for_node(cfg.home, [LaneSpec(id="biz-a/reddit")])
    assert next(r for r in done["rules"] if r["id"] == "reddit.comment")["count"] == 1


class _Owner(BaseHTTPMiddleware):
    def __init__(self, app, kind):
        super().__init__(app)
        self.kind = kind

    async def dispatch(self, request, call_next):
        if self.kind is not None:
            # a grant: an agent's credential, not the owner (agentos.core.presence)
            from agentos.core.presence import Presence

            request.state.principal = type("P", (), {"kind": self.kind})()
            access = type("A", (), {"owner": False, "member": None, "presence": Presence.AGENT,
                                    "trace": lambda *a, **k: None})()
            request.state.auth = type("Auth", (), {"access": access})()
        return await call_next(request)


class _Lanes:
    def __init__(self, cfg):
        self.config = cfg

    def get(self, lane_id):
        return None

    def list(self):
        return [type("Info", (), {"spec": LaneSpec(id="biz-a/reddit")})()]


def test_the_api_serves_the_owner_and_refuses_a_grant(tmp_path):
    cfg = _node_with_trace(tmp_path, [blocked(T0, "reddit.comment")])
    for kind, status in ((None, 200), ("grant", 403)):
        app = Starlette(routes=[Mount("/api", routes=rule_health_api_routes(_Lanes(cfg)))])
        app.add_middleware(_Owner, kind=kind)
        with TestClient(app) as client:
            r = client.get("/api/rules/health?stale_days=7")
        assert r.status_code == status, r.text
        if status == 200:
            assert r.json()["stale_days"] == 7 and any(x["id"] == "reddit.comment" for x in r.json()["rules"])


def test_the_doctor_warns_on_drift(tmp_path):
    from agentos.ops import doctor

    _node_with_trace(tmp_path, [blocked(T0, "reddit.comment"), classified(T0 + DAY, "CreateCommentV2")])
    [result] = doctor.check_effect_rules(doctor.DoctorContext(home=tmp_path))
    assert result.status == doctor.WARN and result.data["rule"] == "reddit.comment"
    assert "gate-log" in (result.fix or "")
