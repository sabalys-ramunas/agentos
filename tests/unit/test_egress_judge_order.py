"""The order egress.judge decides in (agentos.node.egress.verdicts): which check answers first when
several could. Pinned when judge was split into steps, from the cases a comparison with the old
function turned up."""

from __future__ import annotations

from agentos.node.egress import EFFECT_RULES, Driver, judge

POST = next(r for r in EFFECT_RULES if r.id == "reddit.post")
AGENT = Driver(kind="agent", id="agent-1", ceiling="commit")
FLOW = Driver(kind="flow", id="f1")


def asks() -> bool:
    return True


def test_an_unreadable_address_with_no_list_is_judged_no_further():
    """A navigation whose host cannot be read, on a lane with no site list and a grant with no
    egress list, goes: not even the effect rule or Ask me first judge it."""
    for driver in (AGENT, FLOW):
        assert judge("http:///nohost", "POST", navigation=True, sites=(), driver=driver, human=False, effect=POST,
                     asks_first=asks) is None


def test_an_unreadable_address_is_refused_when_a_list_is_set():
    refusal = judge("http:///nohost", "GET", navigation=True, sites=("reddit.com",), driver=AGENT, human=False)
    assert refusal is not None and refusal.rule == "unreadable"
    listed = Driver(kind="agent", id="a", lists=(("reddit.com",),), ceiling="commit")
    refusal = judge("http:///nohost", "GET", navigation=True, sites=(), driver=listed, human=False)
    assert refusal is not None and refusal.rule == "unreadable"


def test_a_post_to_an_unreadable_address_still_meets_the_effect_checks():
    """A non-GET with no readable host and no list set is not refused for its address, so the
    publish checks after it still apply."""
    refusal = judge("http:///nohost", "POST", navigation=False, sites=(), driver=AGENT, human=False, effect=POST,
                    asks_first=asks)
    assert refusal is not None and refusal.rule == "ask_first"


def test_the_site_list_answers_before_the_ceiling_and_ask_me_first():
    low = Driver(kind="agent", id="a", ceiling="read")
    refusal = judge("https://evil.example/x", "POST", navigation=False, sites=("reddit.com",), driver=low,
                    human=False, effect=POST, asks_first=asks)
    assert refusal is not None and refusal.rule == "non_get"
    refusal = judge("https://www.reddit.com/svc/shreddit/graphql", "POST", navigation=False, sites=("www.reddit.com",),
                    driver=low, human=False, effect=POST, asks_first=asks)
    assert refusal is not None and refusal.rule == "commit"  # the ceiling, before Ask me first


def test_an_exposed_dev_server_skips_the_host_lists_but_not_the_effect_checks():
    refusal = judge("https://localhost:5173/api", "POST", navigation=False, sites=("reddit.com",), driver=AGENT,
                    human=False, effect=POST, asks_first=asks, exposed=("localhost",))
    assert refusal is not None and refusal.rule == "ask_first"
    assert judge("https://localhost:5173/", "GET", navigation=True, sites=("reddit.com",), driver=AGENT,
                 human=False, exposed=("localhost",)) is None
