"""Browser devices in the panel API (openajar.api.routes_devices, AUD-02): the owner lists and makes
devices, sees what a move changes before it happens, and a move a site would notice needs a
confirm (409 confirm_required without one, nothing changed)."""

from __future__ import annotations

import asyncio

import pytest
from test_node_panel import TOKEN, Node, bearer

from openajar.core.models import LaneSpec

OWNER = bearer(TOKEN)


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    yield n
    n.close()


def test_the_owner_makes_a_device_and_lanes_show_it(node):
    asyncio.run(node.lanes.create(LaneSpec(id="biz-a/facebook", proxy_label=None)))
    with node.client() as c:
        assert c.get("/api/browser-devices", headers=OWNER).json() == []
        r = c.post("/api/browser-devices", json={"name": "biz-a-pc", "proxy_label": "lt1"}, headers=OWNER)
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "biz-a-pc" and r.json()["proxy"] == "lt1"
        bad = c.post("/api/browser-devices", json={"name": "Biz A"}, headers=OWNER)
        assert bad.status_code == 400
        none = c.post("/api/browser-devices", json={"name": "biz-b-pc"}, headers=OWNER)
        assert none.status_code == 400 and "proxy" in none.json()["error"]["message"]

        lane = c.post("/api/lanes", json={"id": "biz-a/reddit", "device": "biz-a-pc"}, headers=OWNER).json()
        assert lane["device"] == "biz-a-pc"
        one = c.get("/api/browser-devices/biz-a-pc", headers=OWNER).json()
        assert [x["id"] for x in one["lanes"]] == ["biz-a/reddit"] and "health" in one
        listed = {x["id"]: x for x in c.get("/api/lanes", headers=OWNER).json()}
        assert listed["biz-a/reddit"]["device"] == "biz-a-pc" and listed["biz-a/facebook"]["device"] is None

        in_use = c.delete("/api/browser-devices/biz-a-pc", headers=OWNER)
        assert in_use.status_code == 400 and "used by" in in_use.json()["error"]["message"]


def test_moving_a_lane_its_site_has_seen_needs_a_confirm(node):
    asyncio.run(node.lanes.open("biz-a/instagram"))  # it has launched: its site knows a device
    asyncio.run(node.lanes.close("biz-a/instagram"))
    with node.client() as c:
        c.post("/api/browser-devices", json={"name": "biz-a-pc", "proxy_label": "lt1"}, headers=OWNER)
        plan = c.get("/api/lanes/biz-a/instagram/device-move?to=biz-a-pc", headers=OWNER).json()
        assert plan["confirm"] is True and "new computer" in plan["warnings"][0]
        person = node.sign_in(c)  # a move changes what the site sees: the owner in person
        refused = c.post("/api/lanes/biz-a/instagram/device", json={"device": "biz-a-pc"}, headers=person)
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "confirm_required"
        assert node.lanes.get("biz-a/instagram").spec.device is None
        moved = c.post("/api/lanes/biz-a/instagram/device", json={"device": "biz-a-pc", "confirm": True},
                       headers=person)
        assert moved.status_code == 200, moved.text
        assert moved.json()["lane"]["device"] == "biz-a-pc"


def test_a_grant_cannot_see_or_change_devices(node):
    token = node.grant(ceiling="commit")
    with node.client() as c:
        assert c.get("/api/browser-devices", headers=bearer(token)).status_code == 403
        r = c.post("/api/lanes/biz-a/instagram/device", json={"device": None}, headers=bearer(token))
        assert r.status_code == 403
