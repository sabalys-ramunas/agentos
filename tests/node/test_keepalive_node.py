"""A keepalive visit in a real Camoufox, against the fixture site (docs/design/keepalive.md).

Run on the node:  scripts/node-test.sh keepalive tests/node/test_keepalive_node.py -q
(or in the local agentos-stealth-node image, the same way the other node tests run).

The visit drives the page's egress guard with the keepalive grant (read only), scrolls the long fixture
feed with the wheel, may open one post and come back, and never presses a Like. A scripted Like is
refused before it is pressed. A write the page sends during the visit never reaches the site (the
guard's effect route aborts it on a lane that is not strict), and the same write after the visit does,
so the check is real. A post link with a Like nested inside it is opened by its address, never pressed.
A visit whose lane leaves the node's control stops before its next step.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from agentos.core.errors import EgressRefused
from agentos.keepalive import visit
from agentos.keepalive.sites import Entry
from agentos.node import egress
from agentos.node.egress import EffectRule, EgressTrace, LaneGuard
from agentos.node.interaction import PlaywrightInteraction
from agentos.node.routes import RouteDispatcher

pytestmark = [pytest.mark.node, pytest.mark.asyncio(loop_scope="session")]

LIKE = EffectRule("test.like", "test", "127.0.0.1", r"/like$", effect="write")
PRESSED_JS = "() => document.querySelectorAll('button.like[aria-pressed=\"true\"]').length"
SEND_LIKE_JS = "() => fetch('/like', {method: 'POST', body: 'x'}).then((r) => r.status, () => 'aborted')"
PACE = visit.Pace(first_look=(0.5, 1.0), scrolls=(4, 5), between=(0.3, 0.6), open_item=1.0,
                  item_look=(0.5, 1.0), item_scrolls=(1, 1), last_look=(0.3, 0.5), max_seconds=90)


def fixture_entry(site_url: str, page: str = "bigfeed.html") -> Entry:
    return Entry("site", "Fixture", f"{site_url}/{page}", ("127.0.0.1",), (3.0, 5.0),
                 items=r"^/index\.html$")


def guard_on(page, tmp_path) -> LaneGuard:
    """The guard as a lane has it: on the context, with the lane's route dispatcher, not strict."""
    guard = LaneGuard("test/keepalive", sites=("127.0.0.1",), trace=EgressTrace(tmp_path / "trace.jsonl"))
    guard.events = lambda lane, kind, detail: None
    guard.attach(page.context, RouteDispatcher(page.context))
    guard.drive(egress.Driver(kind="agent", id="claude", grant_id="g", ceiling="commit"))
    return guard


async def test_a_real_visit_scrolls_opens_one_link_comes_back_and_likes_nothing(page, site_url, tmp_path):
    guard = guard_on(page, tmp_path)
    ia = PlaywrightInteraction(allow_private_targets=True)  # the fixture site is on 127.0.0.1
    out = await visit.run(page, ia, fixture_entry(site_url), pace=PACE, r=random.Random(11), sleep=asyncio.sleep)
    assert out.outcome == "ok", out.reason
    assert out.scrolls >= 4 and out.opened_item and out.url_host == "127.0.0.1"
    assert page.url.startswith(f"{site_url}/bigfeed.html")  # came back with the browser's own Back
    assert await page.evaluate(PRESSED_JS) == 0
    assert out.blocks == []
    assert guard.driver == egress.Driver(kind="agent", id="claude", grant_id="g", ceiling="commit")
    assert guard.dispatcher.patterns() == []  # the visit's route is gone with it


async def test_a_scripted_like_is_refused_and_a_write_the_page_sends_never_reaches_the_site(
        page, site_url, site_posts, tmp_path, monkeypatch):
    monkeypatch.setattr(egress.rules, "EFFECT_RULES", (LIKE,))
    guard = guard_on(page, tmp_path)
    assert not guard.strict
    ia = PlaywrightInteraction(allow_private_targets=True)
    ro = visit.ReadOnly(ia, page, fixture_entry(site_url), guard)
    guard.drive(visit.DRIVER)
    async with guard.acting(route=True, effects_route=True) as mark:
        await ro.open(f"{site_url}/bigfeed.html")
        nodes = await ro.look()
        like = next(n for n in nodes if n.role == "button" and n.name.startswith("Like"))
        with pytest.raises(EgressRefused, match="read-only"):
            await ro.open_item(like.ref)
        assert await page.evaluate(PRESSED_JS) == 0
        assert await page.evaluate(SEND_LIKE_JS) == "aborted"
    assert [(b.rule, b.effect_rule, b.prevented) for b in guard.blocks_since(mark)] == [("write", "test.like", True)]
    assert site_posts == []  # the site never received the write
    assert guard.dispatcher.patterns() == []
    # after the visit there is no route: the same write goes out (and is only recorded), so the check is real
    assert await page.evaluate(SEND_LIKE_JS) == 200
    assert site_posts == ["/like"]


async def test_a_post_link_with_a_like_inside_is_opened_by_its_address(page, site_url, site_posts, tmp_path,
                                                                        monkeypatch):
    monkeypatch.setattr(egress.rules, "EFFECT_RULES", (LIKE,))
    guard_on(page, tmp_path)
    ia = PlaywrightInteraction(allow_private_targets=True)
    pace = visit.Pace(first_look=(0.2, 0.3), scrolls=(1, 1), between=(0.2, 0.3), open_item=1.0,
                      item_look=(0.3, 0.5), item_scrolls=(0, 0), last_look=(0.2, 0.3), max_seconds=60)
    for seed in range(3):
        out = await visit.run(page, ia, fixture_entry(site_url, "nestedfeed.html"), pace=pace,
                              r=random.Random(seed), sleep=asyncio.sleep)
        assert out.outcome == "ok" and out.opened_item, out.reason
        assert out.blocks == []
    assert site_posts == []  # no Like was ever pressed, so none was ever sent


async def test_a_visit_stops_before_its_next_step_once_the_lane_leaves_the_node(page, site_url, tmp_path):
    guard_on(page, tmp_path)
    ia = PlaywrightInteraction(allow_private_targets=True)
    asked = 0

    def may_act() -> bool:  # control leaves the node after the feed loaded and two scrolls
        nonlocal asked
        asked += 1
        return asked <= 6

    pace = visit.Pace(first_look=(0.2, 0.3), scrolls=(8, 8), between=(0.2, 0.3), open_item=0.0, max_seconds=60)
    out = await visit.run(page, ia, fixture_entry(site_url), pace=pace, r=random.Random(5), sleep=asyncio.sleep,
                          may_act=may_act)
    assert out.outcome == "stopped" and out.scrolls < 8
    at = await page.evaluate("() => window.scrollY")
    await asyncio.sleep(1.0)
    assert await page.evaluate("() => window.scrollY") == at  # nothing moved after it stopped
