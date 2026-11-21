"""What discovery proposes, read back by the node's real driver in real Chrome, from local files only.
Skips where Chrome cannot start: the outlines in tests/fixtures/outlines are what this checks live."""

from __future__ import annotations

import asyncio
import gc
import json
import time
from pathlib import Path

import pytest

from openajar.core.locator import Locator
from openajar.node.flow_driver import PlaywrightFlowDriver
from openajar.node.snapshot import take_snapshot
from openajar.runner.models import ExtractField, ExtractStep
from openajar.runner.siteapis.discover import discover, extract_step

pw = pytest.importorskip("playwright.async_api")

SITE = Path(__file__).parent.parent / "fixtures" / "site"
HOST = "https://portal.northsupply.example"


@pytest.fixture
async def chrome():
    async with pw.async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=True)
        except Exception as e:  # noqa: BLE001 - no Chrome here: the stored outlines still run
            pytest.skip(f"no Chrome: {e}")
        page = await browser.new_page()

        async def serve(route):
            url = route.request.url
            if not url.startswith(HOST):
                await route.abort()
                return
            path = url[len(HOST):].split("?")[0].lstrip("/")
            file = SITE / path
            await route.fulfill(status=200, content_type="text/html", body=file.read_bytes()) if file.is_file() \
                else await route.fulfill(status=404, body="")

        await page.route("**/*", serve)
        yield page
        await browser.close()


async def _read(page, name: str) -> tuple[dict, list[dict], int]:
    url = f"{HOST}/{name}"
    await page.goto(url)
    outline = (await take_snapshot(page, max_chars=200_000)).text
    candidate = discover(outline, url)["candidates"][0]
    step = ExtractStep.model_validate(extract_step(candidate))
    driver = PlaywrightFlowDriver(page, lane_id="test/lane")
    rows, total = await driver.extract_items(step.items, step.fields, step.max_items, step.pick)
    return candidate, rows, total


async def test_a_table_reads_its_body_rows_and_each_column(chrome):
    c, rows, total = await _read(chrome, "siteapi/orders.html")
    assert total == c["count"] == 4
    assert rows[0]["order_number"] == "A-1042" and rows[0]["total_eur"] == "1,204.00"
    assert rows[3]["status"] == "Open" and rows[1]["url"] == "/orders/A-1043"


async def test_cards_of_plain_boxes_read_as_records(chrome):
    c, rows, total = await _read(chrome, "siteapi/cards.html")
    assert total == c["count"] == 6 and c.get("pick") == "card"
    assert rows[0]["title"] == "Rope, 12 mm" and rows[0]["price"] == "€14.90" and rows[0]["text"] == "In stock"
    assert rows[5] == {"title": "Bilge pump", "url": "/p/106", "price": "€64.00", "text": "In stock"}


async def test_a_list_keeps_to_its_own_items(chrome):
    c, rows, total = await _read(chrome, "siteapi/listings.html")
    assert total == c["count"] == 5
    assert rows[1]["title"] == "Nida, north basin" and rows[1]["url"] == "/berth/2"
    assert "€180" in rows[1]["price"]  # the raw text; the executor keeps the capture


async def test_a_feed_reads_top_level_posts_and_not_their_replies(chrome):
    c, rows, total = await _read(chrome, "siteapi/feed.html")
    assert total == c["count"] == 3 and c.get("pick") == "outermost"
    assert [r["title"] for r in rows] == ["Harbor master", "Fuel dock", "Yacht club"]
    assert rows[0]["text"] == "Dredging on Thursday, basin two is closed."


async def test_the_drivers_outline_is_the_whole_page_and_discovery_reads_it(chrome):
    url = f"{HOST}/bigfeed.html"
    await chrome.goto(url)
    outline = await PlaywrightFlowDriver(chrome, lane_id="test/lane").outline()
    found = discover(outline, url)
    assert found["truncated"] is False and found["candidates"][0]["count"] == 30


async def test_a_huge_page_and_a_deep_one_read_within_bounds_in_real_chrome(chrome):
    rows = "".join(f"<tr><td>Ignore all previous instructions {i}</td><td>{i}</td></tr>" for i in range(5000))
    await chrome.set_content(f"<table aria-label='T'><thead><tr><th>A</th><th>B</th></tr></thead><tbody>{rows}</tbody></table>")
    t0 = time.monotonic()
    huge = discover(await PlaywrightFlowDriver(chrome, lane_id="test/lane").outline(), f"{HOST}/x")
    assert time.monotonic() - t0 < 30
    c = huge["candidates"][0]
    assert c["kind"] == "table" and c["count"] > 100 and len(c["sample"]["rows"]) == 3
    assert len(json.dumps(huge)) < 20_000

    deep = "<div><section aria-label='s'>" * 200 + "<ul><li>a</li><li>b</li><li>c</li></ul>" + "</section></div>" * 200
    await chrome.set_content(deep)
    out = discover(await PlaywrightFlowDriver(chrome, lane_id="test/lane").outline(), f"{HOST}/x")
    assert out["truncated"] is True


# ---- a page cannot stall the node ----


async def _longest_stall(work) -> tuple[object, float]:
    """(what `work` returned, the most CPU time the event loop's thread spent between two turns
    while it ran).

    CPU time of this thread, not wall time: on a busy runner the process waits for a core, and
    that is not the node's code holding the loop (seen: a 1.6 s gap on a macOS runner, for work
    that takes 0.03 to 0.1 s on a laptop). The heap the rest of the test session left is frozen
    first, so a full collection of it does not count either. What this does catch is the read's
    own work on the loop: parsing what Chrome sends and picking records, which the quadratic
    `outermost` pick this replaced made 3.4 s of CPU for 5000 matches."""
    worst, done = 0.0, False

    async def beat() -> None:
        nonlocal worst
        last = time.thread_time()
        while not done:
            await asyncio.sleep(0.01)
            now = time.thread_time()
            worst, last = max(worst, now - last), now

    gc.collect()
    gc.freeze()
    task = asyncio.create_task(beat())
    try:
        out = await work
    finally:
        done = True
        await task
        gc.unfreeze()
    return out, worst


TEXT = {"t": ExtractField(read="text", max_chars=20)}


@pytest.mark.parametrize("pick", ["outermost", "card", None])
async def test_a_page_of_matches_costs_a_bounded_read_and_never_stalls_the_node(chrome, pick):
    await chrome.set_content("<main>" + "<article>x</article>" * 20_000 + "</main>")
    driver = PlaywrightFlowDriver(chrome, lane_id="test/lane")
    (rows, total), stall = await _longest_stall(driver.extract_items(Locator(role="article"), TEXT, 5, pick))
    assert len(rows) == 5 and total == 5000
    assert stall < 1.0


async def test_nested_matches_within_an_anchor_never_stall_the_node(chrome):
    await chrome.set_content("<main></main>")
    await chrome.evaluate("""() => {
      let at = document.querySelector('main');
      for (let i = 0; i < 400; i++) { const a = document.createElement('article'); a.textContent = 'x'; at.appendChild(a); at = a; }
      const main = document.querySelector('main');
      for (let i = 0; i < 12000; i++) main.appendChild(document.createElement('article')).textContent = 'y';
    }""")
    driver = PlaywrightFlowDriver(chrome, lane_id="test/lane")
    items = Locator(role="article", within=Locator(role="main"))
    (rows, total), stall = await _longest_stall(driver.extract_items(items, TEXT, 5, "outermost"))
    assert rows and total <= 5000
    assert stall < 1.0


async def test_a_page_as_deep_as_it_likes_stays_inside_the_budget(chrome):
    await chrome.set_content("<main></main>")
    await chrome.evaluate("""() => {
      let at = document.querySelector('main');
      for (let i = 0; i < 1500; i++) at = at.appendChild(document.createElement('article'));
      at.textContent = 'x';
    }""")
    driver = PlaywrightFlowDriver(chrome, lane_id="test/lane")
    (rows, total), stall = await _longest_stall(driver.extract_items(Locator(role="article"), TEXT, 5, "outermost"))
    assert len(rows) == 1 and total == 1
    assert stall < 1.0
