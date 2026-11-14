"""The action ledger: every path's likes, follows and posts on an account, counted where the egress
guard sees them, and read by the engagement engine's caps."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from test_egress import FakeContext, FakeRequest
from test_egress_effect_rules import meta_form

from agentos.node.egress import LaneGuard
from agentos.policy.ledger import ActionLedger, build, ledger_path

T0 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC).timestamp()
IG_GQL = "https://www.instagram.com/graphql/query"


def ledger(t: list[float] | None = None) -> ActionLedger:
    clock = t or [T0]
    return ActionLedger(None, clock=lambda: clock[0])


def test_it_counts_by_account_kind_window_and_actor():
    lg = ledger()
    lg.debit("a/ig", "like", actor="agent", at=T0 - 7200)
    lg.debit("a/ig", "like", actor="flow", at=T0 - 60)
    lg.debit("a/ig", "like", actor="person", at=T0 - 30)
    lg.debit("a/ig", "follow", actor="agent", at=T0 - 30)
    lg.debit("b/ig", "like", actor="agent", at=T0 - 30)
    assert lg.debit("a/ig", "read", actor="agent") is None              # no action: not stored
    assert lg.count("a/ig", "like", since=T0 - 86400) == 2              # a person's own clicks left out
    assert lg.count("a/ig", "like", since=T0 - 86400, actors=("agent", "flow", "person")) == 3
    assert lg.hour_count("a/ig", "like", T0) == 1
    assert lg.counts("a/ig", since=T0 - 86400) == {"follow": 1, "like": 2}
    with pytest.raises(ValueError):
        lg.debit("a/ig", "like", actor="robot")


def test_a_local_day_is_the_accounts_own():
    lg = ledger()
    vilnius = ZoneInfo("Europe/Vilnius")
    late = datetime(2026, 11, 2, 23, 30, tzinfo=vilnius)       # 21:30 UTC, still Monday there
    early = datetime(2026, 11, 3, 0, 30, tzinfo=vilnius)       # 22:30 UTC Monday, Tuesday there
    lg.debit("a/ig", "like", actor="agent", at=late)
    lg.debit("a/ig", "like", actor="agent", at=early)
    assert lg.day_count("a/ig", "like", late.date(), vilnius) == 1
    assert lg.day_count("a/ig", "like", early.date(), vilnius) == 1


def test_it_keeps_its_rows_across_a_restart_and_prunes_old_ones(tmp_path):
    t = [T0]
    first = ActionLedger(ledger_path(tmp_path), clock=lambda: t[0])
    first.debit("a/ig", "like", actor="agent", at=T0 - 9 * 86400)
    first.debit("a/ig", "like", actor="agent")
    first.close()
    again = ActionLedger(ledger_path(tmp_path), clock=lambda: t[0])
    assert again.count("a/ig", "like", since=0) == 1


class _Lanes:
    def __init__(self):
        self.listeners = []

    def add_effect_listener(self, fn):
        self.listeners.append(fn)


class _Runtime:
    def __init__(self, **services):
        self.services = dict(services)

    def get(self, name):
        return self.services.get(name)

    def register(self, name, svc):
        self.services[name] = svc


def test_build_registers_it_and_listens_to_every_lane():
    lanes = _Lanes()
    rt = _Runtime(lanes=lanes)
    lg = build(rt)
    assert rt.get("ledger") is lg and lanes.listeners == [lg.on_effect]
    lg.on_effect("a/ig", "like", "instagram.like", "agent")
    assert lg.count("a/ig", "like", since=0) == 1


# ---- the guard: what left, whoever sent it -----------------------------------------------------

def _guard(human: bool = False):
    guard = LaneGuard("a/ig")
    ctx = FakeContext()
    guard.attach(ctx)
    seen = []
    guard.on_effect = lambda rule, actor: seen.append((rule.id, actor))
    guard.human_in_control = lambda: human
    return guard, ctx, seen


def _like() -> FakeRequest:
    headers, body = meta_form("usePolarisLikeMediaXIGLikeMutation", {"input": {"media_id": "1"}})
    return FakeRequest(IG_GQL, "POST", headers=headers, body=body)


def test_a_like_the_site_answered_is_counted_once_and_reads_are_not():
    _, ctx, seen = _guard()
    ctx.emit("requestfinished", _like())
    ctx.emit("requestfinished", FakeRequest("https://www.instagram.com/", "GET"))
    read_headers, read_body = meta_form("PolarisFeedTimelineQuery", {})
    ctx.emit("requestfinished", FakeRequest(IG_GQL, "POST", headers=read_headers, body=read_body))
    assert seen == [("instagram.like", "agent")]


def _failed(failure: str | None) -> FakeRequest:
    req = _like()
    req.failure = failure
    return req


@pytest.mark.parametrize("failure", [None, "NS_BINDING_ABORTED", "NS_ERROR_NET_RESET", "NS_ERROR_NET_TIMEOUT",
                                     "net::ERR_ABORTED", "NS_ERROR_NET_INTERRUPT", "NS_ERROR_OFFLINE",
                                     "net::ERR_INTERNET_DISCONNECTED", "NS_ERROR_PROXY_BAD_GATEWAY"])
def test_a_failed_like_the_site_may_have_processed_is_counted(failure):
    # the page navigated away, the connection dropped or timed out after the body left: the site
    # may have acted, so it counts (over-counting ends a day early, missing one breaks the cap)
    _, ctx, seen = _guard()
    ctx.emit("requestfailed", _failed(failure))
    assert seen == [("instagram.like", "agent")]


@pytest.mark.parametrize("failure", ["NS_ERROR_UNKNOWN_HOST", "NS_ERROR_CONNECTION_REFUSED",
                                     "NS_ERROR_PROXY_CONNECTION_REFUSED", "net::ERR_NAME_NOT_RESOLVED", "net::ERR_PROXY_CONNECTION_FAILED"])
def test_a_like_that_never_reached_the_site_is_not_counted(failure):
    _, ctx, seen = _guard()
    ctx.emit("requestfailed", _failed(failure))
    assert seen == []


def test_a_like_the_guard_aborted_itself_is_not_counted():
    import asyncio

    guard, ctx, seen = _guard()
    req = _failed("NS_ERROR_FAILURE")

    class _Route:
        aborted = None

        async def abort(self, reason):
            self.aborted = reason

    route = _Route()
    asyncio.run(guard._abort(route, req, "blockedbyclient"))
    ctx.emit("requestfailed", req)
    assert route.aborted == "blockedbyclient" and seen == []
    ctx.emit("requestfailed", _failed("NS_ERROR_FAILURE"))       # another like the guard never touched
    assert seen == [("instagram.like", "agent")]


def test_every_action_in_a_batch_is_counted():
    import json

    _, ctx, seen = _guard()
    body = json.dumps([{"operationName": "usePolarisLikeMediaXIGLikeMutation", "variables": {}},
                       {"operationName": "PolarisFeedTimelineQuery", "variables": {}},
                       {"operationName": "usePolarisFollowMutation", "variables": {}},
                       {"operationName": "usePolarisLikeMediaXIGLikeMutation", "variables": {}}]).encode()
    ctx.emit("requestfinished", FakeRequest(IG_GQL, "POST", headers={"content-type": "application/json"}, body=body))
    assert seen == [("instagram.like", "agent"), ("instagram.follow", "agent"), ("instagram.like", "agent")]


def test_a_named_request_is_one_action_though_a_header_names_it_too():
    _, ctx, seen = _guard()
    req = _like()
    req.headers = {**req.headers, "x-fb-friendly-name": "usePolarisLikeMediaXIGLikeMutation"}
    ctx.emit("requestfinished", req)
    assert seen == [("instagram.like", "agent")]


def test_actions_in_a_meta_batch_endpoint_are_counted():
    import json
    from urllib.parse import urlencode

    from agentos.node.egress import effects_of

    queries = json.dumps({"q0": {"fb_api_req_friendly_name": "CometUFIFeedbackReactMutation"},
                          "q1": {"fb_api_req_friendly_name": "CometNewsFeedPaginationQuery"},
                          "q2": {"fb_api_req_friendly_name": "CometUserFollowMutation"}})
    found = effects_of("https://www.facebook.com/api/graphqlbatch/", "POST",
                       {"content-type": "application/x-www-form-urlencoded"}, body=urlencode({"queries": queries}).encode())
    assert [r.id for r in found] == ["facebook.like", "facebook.follow"]


def test_a_persons_like_is_counted_as_theirs():
    _, ctx, seen = _guard(human=True)
    ctx.emit("requestfinished", _like())
    assert seen == [("instagram.like", "person")]


def test_a_listener_that_fails_never_breaks_the_lane():
    guard, ctx, _ = _guard()

    def boom(rule, actor):
        raise RuntimeError("disk full")

    guard.on_effect = boom
    ctx.emit("requestfinished", _like())                    # no exception reaches the browser's callback


def test_a_redirect_hop_is_not_a_second_action():
    _, ctx, seen = _guard()
    hop = _like()
    target = _like()
    target.redirected_from = hop
    ctx.emit("requestfinished", hop)          # its finish comes before redirected_to is set
    hop.redirected_to = target
    ctx.emit("requestfinished", target)
    assert seen == [("instagram.like", "agent")]
    other = _like()                           # another chain is another action
    ctx.emit("requestfinished", other)
    assert len(seen) == 2


def test_the_actor_is_who_drove_the_lane_when_the_request_left():
    guard, ctx, seen = _guard()
    human = [False]
    guard.human_in_control = lambda: human[0]
    req = _like()
    ctx.emit("request", req)                  # an agent's like leaves
    human[0] = True                           # a person takes the lane before the answer
    ctx.emit("requestfinished", req)
    assert seen == [("instagram.like", "agent")]


@pytest.mark.parametrize("ctype", ["text/plain;charset=UTF-8", "multipart/form-data; boundary=x", ""])
def test_a_body_no_parser_reads_still_counts_the_actions_it_names(ctype):
    import json

    _, ctx, seen = _guard()
    payload = json.dumps({"operationName": "usePolarisLikeMediaXIGLikeMutation", "variables": {}})
    body = (f"--x\r\nContent-Disposition: form-data; name=\"operations\"\r\n\r\n{payload}\r\n--x--"
            if ctype.startswith("multipart") else payload + ("" if ctype else " trailing junk ]")).encode()
    ctx.emit("requestfinished", FakeRequest(IG_GQL, "POST", headers={"content-type": ctype} if ctype else {},
                                            body=body))
    assert seen == [("instagram.like", "agent")]


def test_a_body_too_large_to_parse_still_counts():
    import json

    from agentos.node.egress import SHAPE_PARSE_MAX

    _, ctx, seen = _guard()
    body = json.dumps({"operationName": "usePolarisFollowMutation", "variables": {"pad": "a" * SHAPE_PARSE_MAX}})
    ctx.emit("requestfinished", FakeRequest(IG_GQL, "POST", headers={"content-type": "application/json"},
                                            body=body.encode()))
    assert seen == [("instagram.follow", "agent")]
