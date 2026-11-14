"""read_json's response reading on real Camoufox, which reads a body through Juggler where Chrome reads it
through CDP: tests/unit/test_siteapi_json_chrome.py is the same on Chrome. Local fixture pages only.

Run on the node:  scripts/node-test.sh tests/node/test_read_json_node.py
"""

from __future__ import annotations

import asyncio

import pytest

from agentos.core.driver import parse_json
from agentos.node.flow_driver import PlaywrightFlowDriver
from agentos.node.interaction import PlaywrightInteraction

pytestmark = [pytest.mark.node, pytest.mark.asyncio(loop_scope="session")]


async def _responses(watch, *, keep: int = 1) -> list:
    for _ in range(100):
        if len(watch.responses) >= keep:
            return list(watch.responses)
        await asyncio.sleep(0.05)
    return list(watch.responses)


def _driver(page) -> PlaywrightFlowDriver:
    return PlaywrightFlowDriver(page, lane_id="fd/json", interaction=PlaywrightInteraction(allow_private_targets=True),
                                dwell_ms=(0, 0))


async def test_camoufox_reads_the_body_of_the_pages_own_fetch(page, site_url):
    driver = _driver(page)
    watch = driver.watch_json(lambda url: "orders-feed" in url)
    await driver.navigate(f"{site_url}/siteapi/fetches-json.html")
    seen = await _responses(watch)
    assert len(seen) == 1 and seen[0].status == 200 and seen[0].problem is None
    assert parse_json(seen[0].body)["data"]["orders"][0]["id"] == "A-1042"
    watch.close()


async def test_a_revalidated_response_still_reads_as_a_body(page, site_url):
    """The lane's profile keeps an HTTP cache. Chrome shows a response it revalidated itself as a 200 with
    its body (tests/unit/test_siteapi_json_chrome.py, against a real server). What must still be checked on
    Camoufox, which reads through Juggler and has not run this:

    - that this test revalidates at all: the fixture server must send an ETag and honour If-None-Match,
      and the second load must reach it as a conditional request, or this only proves a plain reload;
    - what status Playwright shows then. A 304 here is not a failure of the contract, but with no body the
      read fails as json_unreadable on every second load of a healthy site, so it must be a 200 or carry
      its body;
    - that `response.body()` gives the cached body and sends no request of its own;
    - a page that sends its own If-None-Match: a 304 with no body, as on Chrome, and json_unreadable."""
    driver = _driver(page)
    url = f"{site_url}/siteapi/fetches-json.html"
    await driver.navigate(url)
    await asyncio.sleep(0.3)
    watch = driver.watch_json(lambda u: "orders-feed" in u)
    await driver.navigate(url)
    seen = await _responses(watch)
    assert len(seen) == 1 and seen[0].status == 200 and seen[0].body
    watch.close()
