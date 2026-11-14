"""One API from a draft to a caller, over real HTTP on the real panel and /v1/sites: the two halves
agree on what an activated API is. The fake portal stands in for a site; no browser, no real site."""

from __future__ import annotations

import pytest
from siteapi_http_support import API, READ_PATH, SURFACE, TOKEN, Node, definition

from agentos.runner.siteapis.models import compile_for_test

PORTAL_DOC = definition()
del PORTAL_DOC["endpoints"]["send_note"]
PORTAL_DOC["surfaces"] = [SURFACE]
FLOW = f"connector.{API}.open_orders"


@pytest.fixture
async def node(tmp_path):
    async with Node(tmp_path, install=False) as n:
        yield n


async def _owner(node, client):
    return await node.session(client)


async def test_a_draft_goes_from_the_panel_to_a_paying_caller_and_away(node):
    async with node.client() as panel:
        csrf = await _owner(node, panel)
        r = await panel.post("/api/site-apis", json=PORTAL_DOC, headers=csrf)
        assert r.status_code == 201 and r.json()["state"] == "draft" and r.json()["problems"] == []
        assert (await panel.get(f"/api/site-apis/{API}")).json()["definition"]["api"] == API

        tried = await panel.post(f"/api/site-apis/{API}/try", json={"endpoint": "open_orders", "params": {"status": "open"}, "on": SURFACE}, headers=csrf)
        assert tried.status_code == 200 and tried.json()["data"]["orders"][0]["number"] == "A-1042", tried.text
        assert node.flows.store.get_flow(FLOW) is None  # a try leaves no route

        r = await panel.post(f"/api/site-apis/{API}/activate", json={}, headers=csrf)
        assert r.status_code == 200 and r.json()["state"] == "active", r.text

        async with node.client(node.key()) as caller:
            r = await caller.get(READ_PATH)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["data"]["orders"][0]["number"] == "A-1042" and body["status"].startswith("succeeded")
            assert body["untrusted_fields"] == ["orders[].number", "orders[].total"]
            assert body["source"]["surface"] == SURFACE

            assert (await panel.post(f"/api/site-apis/{API}/pause", headers=csrf)).json()["state"] == "paused"
            r = await caller.get(READ_PATH)
            assert r.status_code == 409 and r.json()["code"] == "paused"

            assert (await panel.post(f"/api/site-apis/{API}/activate", json={}, headers=csrf)).json()["state"] == "active"
            assert (await caller.get(READ_PATH)).status_code == 200

            r = await panel.delete(f"/api/site-apis/{API}", headers=csrf)
            assert r.status_code == 200 and r.json()["removed"] == API
            assert (await caller.get(READ_PATH)).status_code == 404
        assert (await panel.get(f"/api/site-apis/{API}")).status_code == 404


async def test_a_token_never_activates_on_the_real_app(node):
    async with node.client(TOKEN) as token:
        assert (await token.post("/api/site-apis", json=PORTAL_DOC)).status_code == 201
        r = await token.post(f"/api/site-apis/{API}/activate", json={"vouch": True})
        assert r.status_code == 403, r.text
        assert (await token.post(f"/api/site-apis/{API}/pause")).status_code == 403
    assert node.flows.store.get_flow(FLOW) is None


async def test_a_grant_does_not_open_the_owner_routes(node):
    async with node.client(node.grant()) as agent:
        assert (await agent.get("/api/site-apis")).status_code == 403
        assert (await agent.post("/api/site-apis", json=PORTAL_DOC)).status_code == 403
        assert (await agent.post(f"/api/site-apis/{API}/activate", json={})).status_code == 403


@pytest.mark.parametrize("saved_by", [{"kind": "local", "id": API, "installed_by": "owner", "owner": "owner"},
                                      {"kind": "catalog", "id": "agentos", "installed_by": "owner", "owner": "owner"}])
async def test_post_flows_refuses_a_connector_flow_even_from_the_owner(node, saved_by):
    flow = compile_for_test(PORTAL_DOC).operations["open_orders"].definition
    async with node.client() as panel:
        csrf = await _owner(node, panel)
        for body in ({"definition": flow}, {"definition": flow, "saved_by": saved_by}, {"definition": flow, "by": saved_by}):
            r = await panel.post("/api/flows", json=body, headers=csrf)
            assert r.status_code in (400, 403), r.text
    async with node.client(TOKEN) as token:
        assert (await token.post("/api/flows", json={"definition": flow, "saved_by": saved_by})).status_code in (400, 403)
    assert node.flows.store.get_flow(FLOW) is None
