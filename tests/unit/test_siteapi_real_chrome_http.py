"""One request all the way through: what discovery saw on a page in real Chrome, saved, tried and
activated on the real panel, then called with a key over real HTTP, so the node's real driver reads the
page, or the JSON the page itself fetched, and the caller gets JSON. Local fixture pages only. Skips
where Chrome cannot start."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from siteapi_http_support import SURFACE, Node

from openajar.node.flow_driver import PlaywrightFlowDriver
from openajar.node.snapshot import take_snapshot
from openajar.runner.siteapis.discover import discover, extract_step

pw = pytest.importorskip("playwright.async_api")

SITE = Path(__file__).parent.parent / "fixtures" / "site"
HOST = "https://portal.northsupply.example"
PAGE = f"{HOST}/siteapi/orders.html"
API = "north-supply"


@pytest.fixture
async def chrome():
    visits: list[str] = []
    async with pw.async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=True)
        except Exception as e:  # noqa: BLE001 - no Chrome here
            pytest.skip(f"no Chrome: {e}")
        page = await browser.new_page()

        async def serve(route):
            url = route.request.url
            if not url.startswith(HOST):
                await route.abort()
                return
            visits.append(url)
            if url.split("?")[0] in page.json:
                await route.fulfill(status=200, content_type="application/json", body=page.json[url.split("?")[0]])
                return
            file = SITE / url[len(HOST):].split("?")[0].lstrip("/")
            await route.fulfill(status=200, content_type="text/html", body=file.read_bytes()) if file.is_file() \
                else await route.fulfill(status=404, body="")

        await page.route("**/*", serve)
        page.visits = visits
        page.json = {}
        yield page
        await browser.close()


async def test_a_page_in_chrome_becomes_json_for_a_key(chrome, tmp_path):
    await chrome.goto(PAGE)
    outline = (await take_snapshot(chrome, max_chars=200_000)).text
    best = discover(outline, PAGE)["candidates"][0]
    doc = {"api": API, "title": "North Supply", "domain": "portal.northsupply.example", "version": "1.0.0",
           "surfaces": [SURFACE],
           "endpoints": {"orders": {"title": "Orders", "effect": "read", "outputs": {"orders": "orders"},
                                    "steps": [{"type": "navigate", "url": PAGE},
                                              {**extract_step(best), "save_as": "orders"}]}}}

    @asynccontextmanager
    async def real(lane_id: str):
        yield PlaywrightFlowDriver(chrome, lane_id=lane_id, dwell_ms=(0, 0))

    chrome.visits.clear()
    async with Node(tmp_path, install=False, flow_driver=real, lanes=(SURFACE,)) as node, node.client() as panel:
        csrf = await node.session(panel)
        r = await panel.post("/api/site-apis", json=doc, headers=csrf)
        assert r.status_code == 201 and r.json()["problems"] == [], r.text
        tried = await panel.post(f"/api/site-apis/{API}/try", json={"endpoint": "orders", "params": {}, "on": SURFACE},
                                 headers=csrf)
        assert tried.status_code == 200, tried.text
        r = await panel.post(f"/api/site-apis/{API}/activate", json={}, headers=csrf)
        assert r.status_code == 200, r.text

        key = node.key(apis=[API])
        async with node.client(key) as caller:
            chrome.visits.clear()
            r = await caller.get(f"/v1/sites/{API}/orders")
            assert r.status_code == 200, r.text
            body = r.json()
            rows = body["data"]["orders"]["items"] if isinstance(body["data"]["orders"], dict) else body["data"]["orders"]
            assert [o["order_number"] for o in rows] == ["A-1042", "A-1043", "A-1044", "A-1045"]
            assert rows[0]["total_eur"] == "1,204.00" and body["cache"]["hit"] is False
            assert body["untrusted_fields"] and chrome.visits == [PAGE]

            again = await caller.get(f"/v1/sites/{API}/orders")
            assert again.json()["cache"]["hit"] is True and chrome.visits == [PAGE]  # the site was left alone

            spec = (await caller.get(f"/v1/sites/{API}/openapi.json")).json()
            assert spec["openapi"].startswith("3.1") and f"/v1/sites/{API}/orders" in spec["paths"]
            assert "A-1042" not in str(spec)


async def test_the_json_a_page_fetches_becomes_json_for_a_key(chrome, tmp_path):
    feed = f"{HOST}/api/orders"
    fetched = f"{feed}?status=open"
    chrome.json[feed] = json.dumps({"data": {"orders": [{"id": "A-1042", "amount": {"value": 1204.5}, "apiToken": "sk-live-x"}]}})
    page = f"{HOST}/siteapi/orders-json.html"
    doc = {"api": API, "title": "North Supply", "domain": "portal.northsupply.example", "version": "1.0.0",
           "surfaces": [SURFACE],
           "endpoints": {"orders": {
               "title": "Orders", "effect": "read", "outputs": {"orders": "orders"}, "expect": {"min_items": {"orders": 1}},
               "steps": [{"type": "navigate", "url": page},
                         {"type": "read_json", "save_as": "orders", "url": r"^https://portal\.northsupply\.example/api/orders",
                          "items": "data.orders", "max_items": 100, "timeout_ms": 5000,
                          "fields": {"number": {"path": "id", "type": "string", "max_chars": 40},
                                     "total": {"path": "amount.value", "type": "number"}}}]}}}

    @asynccontextmanager
    async def real(lane_id: str):
        yield PlaywrightFlowDriver(chrome, lane_id=lane_id, dwell_ms=(0, 0))

    async with Node(tmp_path, install=False, flow_driver=real, lanes=(SURFACE,)) as node, node.client() as panel:
        csrf = await node.session(panel)
        r = await panel.post("/api/site-apis", json=doc, headers=csrf)
        assert r.status_code == 201 and r.json()["problems"] == [], r.text
        tried = await panel.post(f"/api/site-apis/{API}/try", json={"endpoint": "orders", "params": {}, "on": SURFACE},
                                 headers=csrf)
        assert tried.status_code == 200 and tried.json()["data"]["orders"][0]["number"] == "A-1042", tried.text
        r = await panel.post(f"/api/site-apis/{API}/activate", json={}, headers=csrf)
        assert r.status_code == 200, r.text

        async with node.client(node.key(apis=[API])) as caller:
            chrome.visits.clear()
            r = await caller.get(f"/v1/sites/{API}/orders")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["data"]["orders"] == [{"number": "A-1042", "total": 1204.5}]
            assert body["untrusted_fields"] == ["orders[].number"] and "sk-live-x" not in r.text
            assert chrome.visits == [page, fetched]  # one load, and the page's own one fetch

            again = await caller.get(f"/v1/sites/{API}/orders")
            assert again.json()["cache"]["hit"] is True and chrome.visits == [page, fetched]

            spec = (await caller.get(f"/v1/sites/{API}/openapi.json")).json()
            shape = spec["paths"][f"/v1/sites/{API}/orders"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
            assert "total" in json.dumps(shape) and "A-1042" not in str(spec)
    assert not [p for p in tmp_path.rglob("*") if p.is_file() and b"sk-live-x" in p.read_bytes()]
